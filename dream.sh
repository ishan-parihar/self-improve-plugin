#!/usr/bin/env bash
# Dream stage for the self-improvement loop. Runs on session_end.
#
# The turn loop is the awake phase: it extracts lessons and records project
# facts. It has no time to reconcile anything. The dream stage is where the
# ledger meets memory: it runs memory-wire's hygiene pass over the project bank
# and appends a consolidation record to the SAME ledger the turn loop writes.
#
# Paths, the env file, the JSON writer, the ledger cap and the bank ladder all
# come from lib/common.sh. They used to be copied here, and the copies had
# drifted: a different ledger, no env file, a bank ladder that disagreed on case,
# no cap and no diagnostics.
#
# What it deliberately does NOT do:
#   * It does not invent memories. Hygiene returns candidates; applying them is a
#     separate, explicit step, because a wrong merge silently corrupts recall and
#     there is no undo. The record carries applied=false so a reader can tell a
#     report from an action.
#   * It does not fail the session. Every failure path exits 0 with a diagnostic.
#   * It does not run the hygiene pass more than once per bank per day. Hygiene is
#     O(n^2) over every row: 28,226 candidates from 1,339 rows in 4.5s, and ~7
#     minutes at 4,288. The call is timeboxed, and a timeout leaves the day-stamp
#     unset so the next session retries instead of losing the day.
set -uo pipefail

SELF_IMPROVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SELF_IMPROVE_DIR/lib/common.sh"

log() { printf '%s\n' "$*" >>"${SELF_IMPROVE_LOG:-/tmp/self-improve.log}"; }

# Session context. Absent under a manual run, which is a supported use.
hook_id="${JCODE_HOOK_ID:-manual-$$}"
hook_cwd="${JCODE_HOOK_CWD:-${PWD:-}}"

# The env file is read AFTER common.sh sets the defaults, so a value set there
# wins over the default and loses to an explicit environment variable.
self_improve_load_env

bank="$(memory_wire_resolve_bank "$hook_cwd")"
mem_url="${SELF_IMPROVE_MEMORY_URL:-}"
dream_dir="$SELF_IMPROVE_STATE_ROOT/dream"
mkdir -p "$dream_dir" 2>/dev/null

# One record writer for this stage, built on the shared ledger_append so the write
# is atomic and both stages land in one file.
record() {
  # $1 = outcome, $2 = detail, $3 = optional extra JSON object fragment
  local outcome="$1" detail="${2:-}" extra="${3:-}"
  local ts
  ts=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  REC_TS="$ts" REC_HOOK="$hook_id" REC_BANK="$bank" REC_OUTCOME="$outcome" \
  REC_DETAIL="$detail" REC_EXTRA="$extra" python3 -c '
import json, os
entry = {
    "ts": os.environ["REC_TS"],
    "kind": "dream",
    "hook_id": os.environ["REC_HOOK"],
    "bank": os.environ["REC_BANK"],
    "outcome": os.environ["REC_OUTCOME"],
    "detail": os.environ["REC_DETAIL"][:400],
}
extra = os.environ.get("REC_EXTRA") or ""
if extra:
    try:
        entry.update(json.loads(extra))
    except ValueError:
        pass
print(json.dumps(entry))
' > "$dream_dir/.rec" 2>/dev/null || return 0
  ledger_append "$(cat "$dream_dir/.rec")"
  rm -f "$dream_dir/.rec"
}

# Already dreamed for this bank today? The pass is too expensive to repeat.
stamp="$dream_dir/${bank//\//_}.last"
today=$(date -u +%Y-%m-%d)
if [ -f "$stamp" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$today" ]; then
  log "dream $hook_id: bank=$bank already consolidated today, skipping"
  record "skipped" "already ran today"
  log_diag "dream-skipped" "bank=$bank"
  cap_file "$SELF_IMPROVE_LEDGER"
  exit 0
fi

# No memory-wire configured or reachable is not an error: sharing is opt-in, and a
# host without memory-wire must still close its session cleanly.
if [ -z "$mem_url" ]; then
  log "dream $hook_id: no SELF_IMPROVE_MEMORY_URL, nothing to consolidate"
  record "disabled" "no memory url configured"
  exit 0
fi

# The bank must exist before hygiene can run over it. There is no index route: a
# bank is addressable exactly when its memory list answers 200, so that is the
# probe. The status code is captured explicitly, because a 404 (unknown bank) and a
# connection failure (server down) both produce empty output and must be told
# apart: one is "nothing to do", the other is a diagnostic worth keeping.
mem_code=$(curl -sS --max-time 10 -o "$dream_dir/memories.json" -w '%{http_code}' \
  "$mem_url/banks/$bank/memories" 2>>"${SELF_IMPROVE_LOG:-/tmp/self-improve.log}") || mem_code="000"

if [ "$mem_code" != "200" ]; then
  if [ "$mem_code" = "000" ]; then
    log "dream $hook_id: memory-wire unreachable at $mem_url"
    record "error" "memory-wire unreachable"
    log_diag "dream-error" "reason=unreachable" "bank=$bank"
    exit 0
  fi
  log "dream $hook_id: bank '$bank' answered $mem_code, nothing to consolidate"
  record "empty" "bank has no memories"
  # No stamp: the first memory of the session may land after this hook, so a
  # later session should still get its dream.
  exit 0
fi

rows=$(python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(-1); raise SystemExit
rows = d if isinstance(d, list) else d.get("memories", d.get("rows", []))
print(len(rows))' "$dream_dir/memories.json" 2>/dev/null)
rows="${rows:--1}"

if [ "$rows" -lt 2 ]; then
  log "dream $hook_id: bank=$bank has $rows row(s), nothing to consolidate"
  record "empty" "fewer than 2 memories"
  date -u +%Y-%m-%d >"$stamp" 2>/dev/null
  exit 0
fi

log "dream $hook_id: scanning bank=$bank rows=$rows"

# Hygiene is five deterministic detectors over the bank, not an LLM pass. The call
# is timeboxed so a large bank cannot hold the session open; a timeout records a
# diagnostic and leaves the stamp unset.
out="$dream_dir/${bank//\//_}.hygiene.json"
rc=0
curl -sS --max-time 120 "$mem_url/banks/$bank/hygiene" >"$out" \
  2>>"${SELF_IMPROVE_LOG:-/tmp/self-improve.log}" || rc=$?
if [ "$rc" -ne 0 ]; then
  log "dream $hook_id: hygiene failed rc=$rc (curl timeout is 124)"
  record "error" "hygiene rc=$rc"
  log_diag "dream-error" "reason=hygiene" "rc=$rc" "bank=$bank"
  cap_file "$SELF_IMPROVE_LEDGER"
  exit 0
fi

# applied=false is the safety contract, and it is a real field rather than a
# promise: a reader can tell a report from an action, and nothing in this stage
# writes to memory. Applying candidates stays an explicit human act.
python3 - "$out" "$rows" "$hook_id" "$bank" > "$dream_dir/.rec" 2>/dev/null <<'PY'
import datetime, json, sys
path, rows, hid, bank = sys.argv[1:5]
try:
    d = json.load(open(path, encoding="utf-8"))
except Exception:
    raise SystemExit(0)
cands = d.get("candidates", []) if isinstance(d, dict) else (d or [])
counts = {}
for c in cands:
    k = c.get("kind") or c.get("op") or "unknown"
    counts[k] = counts.get(k, 0) + 1
entry = {
    "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    "kind": "dream",
    "hook_id": hid,
    "bank": bank,
    "outcome": "candidates" if cands else "clean",
    "detail": json.dumps({
        "total": len(cands),
        "by_kind": counts,
        "rows_scanned": d.get("rows_scanned") if isinstance(d, dict) else None,
    })[:400],
    "applied": False,
    "rows": int(rows),
}
print(json.dumps(entry))
PY
if [ -s "$dream_dir/.rec" ]; then
  ledger_append "$(cat "$dream_dir/.rec")"
  log_diag "dream-recorded" "bank=$bank" "rows=$rows"
fi
rm -f "$dream_dir/.rec"

date -u +%Y-%m-%d >"$stamp" 2>/dev/null
cap_file "$SELF_IMPROVE_LEDGER"
exit 0
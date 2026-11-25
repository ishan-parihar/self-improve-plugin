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
#
# Usage:
#   dream.sh              run the stage (what session_end calls)
#   dream.sh --report     read the ledger back: what each dream pass found
#
# --report exists because a recorded field with no reader is a promise, not a
# contract. It is read-only: it never runs a pass and never writes.
set -uo pipefail

SELF_IMPROVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SELF_IMPROVE_DIR/lib/common.sh"

# Report mode: read the ledger back, grouped by bank, and say what is still
# unapplied. Handled before anything else so it cannot claim the day or write.
if [ "${1:-}" = "--report" ]; then
  self_improve_load_env
  # The reader lives in lib/report.py: 100 lines of ledger parsing have no business
  # inside a hook, and keeping them here made the stage's one job hard to read.
  LEDGER="$SELF_IMPROVE_LEDGER" python3 "$SELF_IMPROVE_DIR/lib/report.py"
  exit 0
fi

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
#
# The JSON is captured into a variable and piped, never written to a file first.
# A fixed temp name (the earlier `$dream_dir/.rec`) is shared by every session, so
# two sessions ending together clobbered each other and the loser appended a
# two-line file that parses as ONE merged record. A variable cannot be clobbered by
# another process, and the printf feeds ledger_append's lock directly.
record() {
  # $1 = outcome, $2 = detail, $3 = optional extra JSON object fragment
  local outcome="$1" detail="${2:-}" extra="${3:-}"
  local ts json
  ts=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  json=$(REC_TS="$ts" REC_HOOK="$hook_id" REC_BANK="$bank" REC_OUTCOME="$outcome" \
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
' 2>/dev/null) || return 0
  [ -n "$json" ] || return 0
  ledger_append "$json"
}

# Already dreamed for this bank today? The pass is too expensive to repeat.
#
# The stamp is the claim, and it is written BEFORE the work. A lock around the read
# cannot fix the race on its own: two sessions each take the lock in turn, both see
# no stamp, both release, and both run the pass. A real two-process probe measured 2
# passes and 2 ledger rows where 1 was expected.
#
# The stamp is written first and KEPT. It is not keyed on a pid: the claiming
# process exits moments later, so a pid-keyed stamp reads as stale immediately and
# the next session reclaims the day (which is exactly the bug that produced two
# 'clean' rows in a row). A day is either spent or it is not, and only a pass that
# did not complete releases it.
#
# The lock covers the read and the write together, so two sessions starting at the
# same instant cannot both see an empty stamp: the loser finds today's date and
# skips. A lock held across the pass itself would serialise every session's exit for
# nothing, so the lock is taken only for the check-and-claim.
#
# The claim is abandoned, not absolute: the stamp is written before the pass, so a
# session killed mid-pass (OOM, power loss, `kill -9`) leaves a stamp for a pass that
# never finished, and every later session today would skip a day that was never
# spent. A stamp older than the hygiene timebox plus a margin is treated as
# abandoned and re-claimed. The margin covers a slow but live pass, so only a
# genuinely dead claim is taken over.
stamp="$dream_dir/${bank//\//_}.last"
today=$(date -u +%Y-%m-%d)
# 120s timebox + 60s margin. A pass still running is not abandoned.
claim_max_age=${SELF_IMPROVE_DREAM_CLAIM_TTL_SECS:-180}
mkdir -p "$dream_dir" 2>/dev/null
claim_state="none"
{
  flock 8
  if [ "$(cat "$stamp" 2>/dev/null)" = "$today" ]; then
    # Today's claim. Keep it only if it is young enough to still be a live pass.
    # Age is read from the mtime rather than compared with `find -mmin`: integer
    # seconds-per-minute turns any TTL under 60s into a zero-minute window that
    # matches nothing, so every stamp reads as abandoned.
    age=$(( $(date +%s) - $(stat -c %Y "$stamp" 2>/dev/null || date +%s) ))
    if [ "$age" -lt "$claim_max_age" ]; then
      claim_state="held"
    else
      log "dream $hook_id: bank=$bank claim is ${age}s old (>${claim_max_age}s), reclaiming"
      printf '%s\n' "$today" >"$stamp" 2>/dev/null && claim_state="claimed"
    fi
  else
    printf '%s\n' "$today" >"$stamp" 2>/dev/null && claim_state="claimed"
  fi
} 8>"$dream_dir/${bank//\//_}.lock"

if [ "$claim_state" != "claimed" ]; then
  if [ "$claim_state" = "held" ]; then
    log "dream $hook_id: bank=$bank already consolidated today, skipping"
    record "skipped" "already ran today"
    log_diag "dream-skipped" "bank=$bank"
  else
    # The claim could not be written. That is not "already done": the pass did not
    # run, so the day is not spent and a later session should still get it.
    log "dream $hook_id: bank=$bank could not claim the day, skipping"
    record "error" "could not claim the day"
    log_diag "dream-error" "reason=claim" "bank=$bank"
  fi
  cap_file "$SELF_IMPROVE_LEDGER"
  exit 0
fi

# From here this session owns the day. Every exit path below either completes the
# pass or drops the claim. A path that cannot run (SIGKILL, power loss) drops
# nothing, which is why the claim carries an age: an abandoned stamp is reclaimed
# rather than trusted.
release_claim() {
  {
    flock 8
    # Only drop it if it is still today's: a later session may have re-claimed.
    if [ "$(cat "$stamp" 2>/dev/null)" = "$today" ]; then
      rm -f "$stamp" 2>/dev/null
    fi
  } 8>"$dream_dir/${bank//\//_}.lock"
}

# No memory-wire configured or reachable is not an error: sharing is opt-in, and a
# host without memory-wire must still close its session cleanly.
if [ -z "$mem_url" ]; then
  log "dream $hook_id: no SELF_IMPROVE_MEMORY_URL, nothing to consolidate"
  record "disabled" "no memory url configured"
  release_claim
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
    release_claim
    exit 0
  fi
  log "dream $hook_id: bank '$bank' answered $mem_code, nothing to consolidate"
  record "empty" "bank has no memories"
  # The bank does not exist, so there is nothing to consolidate and the day is not
  # spent: release it so a later session with rows still gets its dream.
  release_claim
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
  # Fewer than two rows can never produce a candidate, and a memory may still
  # land later today, so the pass has nothing to mark. The claimed day stays: a
  # second session today would reach the same empty bank and write the same row.
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
  # The pass did not complete, so the day is not spent and the next session should
  # retry rather than be told it already consolidated.
  release_claim
  cap_file "$SELF_IMPROVE_LEDGER"
  exit 0
fi

# applied=false is the safety contract, and it is a real field rather than a
# promise: a reader can tell a report from an action, and nothing in this stage
# writes to memory. Applying candidates stays an explicit human act.
#
# The JSON goes into a variable, not a file. A fixed temp name is shared by every
# session, so the earlier version let two sessions clobber each other and append a
# merged two-line record that parses as one.
json=$(python3 - "$out" "$rows" "$hook_id" "$bank" 2>/dev/null <<'PY'
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
)
if [ -n "$json" ]; then
  ledger_append "$json"
  log_diag "dream-recorded" "bank=$bank" "rows=$rows"
fi

# The pass completed, so the claim this session wrote stays in place as today's
# stamp. Nothing to rewrite: that is the point of writing the claim first.
cap_file "$SELF_IMPROVE_LEDGER"
exit 0
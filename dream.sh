#!/usr/bin/env bash
# Dream stage for the self-improvement loop. Runs on session_end.
#
# The turn loop is the awake phase: it extracts lessons and records project
# facts. It has no time to reconcile anything. The dream stage is where the
# ledger meets memory: it runs memory-wire's hygiene candidates over the
# project bank and emits a consolidation record.
#
# What it deliberately does NOT do:
#   * It does not invent memories. Hygiene returns candidates; writing them is a
#     separate, explicit step. A wrong merge silently corrupts recall, and there
#     is no undo, so auto-applying candidates is refused.
#   * It does not fail the session. Every failure path exits 0 with a diagnostic.
#   * It does not run the hygiene pass more than once per bank per day. Hygiene
#     is O(n^2) over every row: 9.19M comparisons and ~7 minutes at 4,288 rows.
#     A session_end hook that blocked for seven minutes would be unusable, so the
#     call is timeboxed and a timeout leaves the day-stamp unset so the next
#     session retries.
set -uo pipefail

log() { printf '%s\n' "$*" >>"${SELF_IMPROVE_LOG:-/tmp/self-improve.log}"; }

# Session context. Absent under a manual run, which is a supported use.
hook_id="${JCODE_HOOK_ID:-manual-$$}"
hook_cwd="${JCODE_HOOK_CWD:-${PWD:-}}"

# The bank follows memory-wire's own ladder, so the dream stage and the CLI
# always address the same namespace. Resolution order mirrors src/paths.rs.
bank="${SELF_IMPROVE_MEMORY_BANK:-}"
if [ -z "$bank" ]; then
  bank="${MEMORY_WIRE_BANK:-}"
fi
if [ -z "$bank" ] && [ -n "$hook_cwd" ]; then
  bank=$(python3 - "$hook_cwd" <<'PY' 2>/dev/null
import os, re, subprocess, sys
start = sys.argv[1]
d = os.path.abspath(start)
while True:
    cfg = os.path.join(d, ".git", "config")
    if os.path.isfile(cfg):
        try:
            txt = open(cfg, encoding="utf-8", errors="replace").read()
        except OSError:
            txt = ""
        m = re.search(r'\[remote\s+"origin"\][^\[]*?url\s*=\s*(\S+)', txt, re.S)
        if m:
            url = m.group(1).strip()
            if url.startswith("git@"):
                url = url.split(":", 1)[1]
            url = re.sub(r"\.git$", "", url)
            parts = [p for p in url.split("/") if p]
            if len(parts) >= 2:
                print(re.sub(r"[^A-Za-z0-9._-]", "-", f"{parts[-2]}-{parts[-1]}"))
                raise SystemExit
        break
    parent = os.path.dirname(d)
    if parent == d:
        break
    d = parent
print(re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(os.path.abspath(start))) or "memory-wire")
PY
)
fi
bank="${bank:-memory-wire}"

mem_url="${SELF_IMPROVE_MEMORY_URL:-http://127.0.0.1:8888}"
ledger="${SELF_IMPROVE_LEDGER:-$HOME/.jcode/hooks/self-improve/ledger.jsonl}"
state_dir="${SELF_IMPROVE_STATE_DIR:-$HOME/.jcode/hooks/self-improve/dream}"
mkdir -p "$state_dir" 2>/dev/null

record() {
  # $1 = outcome, $2 = detail
  python3 - "$ledger" "$hook_id" "$bank" "$1" "${2:-}" <<'PY' 2>/dev/null
import datetime, json, os, sys
path, hid, bank, outcome, detail = sys.argv[1:6]
try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "kind": "dream",
            "hook_id": hid,
            "bank": bank,
            "outcome": outcome,
            "detail": detail[:400],
        }) + "\n")
except Exception:
    pass
PY
}

# Already dreamed for this bank today? The scan is too expensive to repeat.
stamp="$state_dir/${bank//\//_}.last"
today=$(date -u +%Y-%m-%d)
if [ -f "$stamp" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$today" ]; then
  log "dream $hook_id: bank=$bank already consolidated today, skipping"
  record "skipped" "already ran today"
  exit 0
fi

# The bank must exist before hygiene can run over it. There is no index route:
# a bank is addressable exactly when its memory list answers 200, so that is the
# probe. The status code is captured explicitly, because a 404 (unknown bank) and
# a connection failure (server down) both produce empty output and must be told
# apart: one is 'nothing to do', the other is a diagnostic worth keeping.
mem_code=$(curl -sS --max-time 10 -o "$state_dir/memories.json" -w '%{http_code}' \
  "$mem_url/banks/$bank/memories" 2>>"${SELF_IMPROVE_LOG:-/tmp/self-improve.log}") || mem_code="000"

if [ "$mem_code" != "200" ]; then
  if [ "$mem_code" = "000" ]; then
    log "dream $hook_id: memory-wire unreachable at $mem_url"
    record "error" "memory-wire unreachable"
    exit 0
  fi
  log "dream $hook_id: bank '$bank' answered $mem_code, nothing to consolidate"
  record "empty" "bank has no memories"
  # Not a failure, and not a stamp: the first memory of the session may land
  # after this hook, so a later session should still get its dream.
  exit 0
fi

rows=$(python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(-1); raise SystemExit
rows = d if isinstance(d, list) else d.get("memories", d.get("rows", []))
print(len(rows))' "$state_dir/memories.json" 2>/dev/null)
rows="${rows:--1}"

if [ "$rows" -lt 2 ]; then
  log "dream $hook_id: bank=$bank has $rows row(s), nothing to consolidate"
  record "empty" "fewer than 2 memories"
  date -u +%Y-%m-%d >"$stamp" 2>/dev/null
  exit 0
fi

log "dream $hook_id: scanning bank=$bank rows=$rows"

# Hygiene is five deterministic detectors over the bank, not an LLM pass. It is
# O(n^2) in the worst case and took ~7 minutes at 4,288 rows, so the call is
# timeboxed and the session never blocks on it: a timeout records a diagnostic
# and leaves the stamp unset, so the next session retries.
out="$state_dir/${bank//\//_}.hygiene.json"
rc=0
curl -sS --max-time 120 "$mem_url/banks/$bank/hygiene" >"$out" \
  2>>"${SELF_IMPROVE_LOG:-/tmp/self-improve.log}" || rc=$?
if [ "$rc" -ne 0 ]; then
  log "dream $hook_id: hygiene failed rc=$rc (curl timeout is 124)"
  record "error" "hygiene rc=$rc"
  exit 0
fi

python3 - "$out" "$ledger" "$hook_id" "$bank" <<'PY' 2>/dev/null
import datetime, json, os, sys
path, ledger, hid, bank = sys.argv[1:5]
try:
    d = json.load(open(path, encoding="utf-8"))
except Exception:
    raise SystemExit(0)
cands = d.get("candidates", []) if isinstance(d, dict) else (d or [])
counts = {}
for c in cands:
    k = c.get("kind") or c.get("op") or "unknown"
    counts[k] = counts.get(k, 0) + 1
rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "kind": "dream",
    "hook_id": hid,
    "bank": bank,
    "outcome": "candidates" if cands else "clean",
    "detail": json.dumps({
        "total": len(cands),
        "by_kind": counts,
        "rows_scanned": d.get("rows_scanned") if isinstance(d, dict) else None,
    })[:400],
    # Candidates are reported, never applied. A wrong merge silently corrupts
    # recall and there is no undo, so applying them stays an explicit act.
    "applied": 0,
}
try:
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
except Exception:
    pass
PY

date -u +%Y-%m-%d >"$stamp" 2>/dev/null
exit 0
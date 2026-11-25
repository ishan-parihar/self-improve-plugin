#!/usr/bin/env bash
# Shared paths and writers for the self-improve loop.
#
# Both stages source this: self-improve.sh (turn_end) and dream.sh (session_end).
# It exists because the two stages had drifted into two ledgers, two env files and
# two bank-derivation ladders that disagreed on the answer. One loop, one book.
#
# Sourcing this defines nothing on its own; it sets variables and functions and
# returns. Callers keep their own `set -u` and their own exit behaviour.

# The one ledger. Every stage of the loop appends here, so a turn and the
# consolidation that follows it are readable together. The dream stage originally
# wrote ~/.jcode/hooks/self-improve/ledger.jsonl while the turn loop wrote here,
# which made the consolidation invisible to every reader of the real ledger.
: "${SELF_IMPROVE_LEDGER:=$HOME/.jcode/self-improve/ledger.jsonl}"
: "${SELF_IMPROVE_DIAGNOSTICS:=$HOME/.jcode/self-improve/diagnostics.jsonl}"
: "${SELF_IMPROVE_STATE_ROOT:=$HOME/.jcode/self-improve}"
: "${SELF_IMPROVE_MAX_LEDGER_ENTRIES:=500}"
: "${SELF_IMPROVE_STATE_TTL_SECS:=86400}"
export SELF_IMPROVE_LEDGER SELF_IMPROVE_DIAGNOSTICS SELF_IMPROVE_STATE_ROOT \
  SELF_IMPROVE_MAX_LEDGER_ENTRIES SELF_IMPROVE_STATE_TTL_SECS

# Load the plugin env file. A hook entry in config.toml is a path, not a command
# with arguments, so the knobs live in one file next to the scripts. Both stages
# read the SAME file: when only one did, the URL and bank were set twice and the
# copy that mattered at session end was the one nobody edited.
self_improve_load_env() {
  local cfg
  for cfg in "$HOME/.jcode/self-improve/env" "$HOME/.jcode/plugins/self-improve/env"; do
    # shellcheck source=/dev/null
    if [[ -f "$cfg" ]]; then set -a; . "$cfg"; set +a; fi
  done
  # `[[ -f ]] && ...` as the last statement returns 1 when neither file exists,
  # and a caller running `set -e` (the turn script does) then dies before it can
  # write a diagnostic. An explicit return makes "no env file" a success.
  return 0
}

# One JSON writer. Callers pass "key=value" chunks; a value containing "=" stays
# whole because the split is on the FIRST "=" only. A second printf anywhere else
# would be the exact duplication this file exists to remove: two writers, one of
# them unescaped.
log_diag() {
  local kind="$1"; shift
  LOG_KIND="$kind" DIAG_PATH="$SELF_IMPROVE_DIAGNOSTICS" python3 -c '
import json, os, sys, datetime
entry = {"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
         "kind": os.environ["LOG_KIND"]}
for chunk in sys.argv[1:]:
    key, sep, value = chunk.partition("=")
    if not sep:
        raise SystemExit("log_diag: chunk has no '=' -> %r" % chunk)
    entry[key] = value
with open(os.environ["DIAG_PATH"], "a") as fh:
    fh.write(json.dumps(entry) + "\n")
' "$@"
}

# Cap one JSONL file at SELF_IMPROVE_MAX_LEDGER_ENTRIES via an atomic rewrite. Both
# stages append to files this plugin owns, so they share one cap. The dream stage
# had none, so its ledger grew one row per session forever.
cap_file() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  local lines
  lines=$(wc -l < "$file" 2>/dev/null || echo 0)
  (( lines > SELF_IMPROVE_MAX_LEDGER_ENTRIES )) || return 0
  local tmp="$file.tmp.$$"
  if tail -n "$SELF_IMPROVE_MAX_LEDGER_ENTRIES" "$file" > "$tmp" 2>/dev/null; then
    mv "$tmp" "$file"
  else
    rm -f "$tmp"
  fi
}

# Append one record to the ledger under an exclusive lock. Two stages can run
# close together (a session ending as the last turn lands), and an append that
# interleaves with another produces a merged line that parses as ONE record whose
# fields are the union of both, silently losing an entry. The lock file lives in
# the state root so both stages contend on the same lock.
ledger_append() {
  local json="$1"
  mkdir -p "$(dirname "$SELF_IMPROVE_LEDGER")" 2>/dev/null
  (
    flock 9
    printf '%s\n' "$json" >> "$SELF_IMPROVE_LEDGER"
  ) 9>"$SELF_IMPROVE_STATE_ROOT/ledger.lock"
}

# The memory bank, derived exactly the way memory-wire derives it (src/paths.rs):
# explicit SELF_IMPROVE_MEMORY_BANK, then MEMORY_WIRE_BANK, then owner/repo from
# the origin remote, then the work-tree basename, then "memory-wire".
#
# This is the ONLY copy. dream.sh carried a 25-line regex version that returned
# "Owner-Repo" where this returns "owner-repo", and real banks are all lowercase,
# so the two stages addressed different banks for the same project.
memory_wire_derive_bank() {
  local dir="${1:-$PWD}"
  MEM_DERIVE_DIR="$dir" python3 - <<'PY' 2>/dev/null || echo memory-wire
import os, re, sys

def sanitize(raw):
    mapped = "".join(
        c.lower() if (c.isascii() and (c.isalnum() or c in "-_.")) else "-"
        for c in raw
    )
    trimmed = mapped.strip("-.")
    return trimmed or None

def worktree_root(d):
    cur = os.path.abspath(d)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent

def origin_url(root):
    cfg = os.path.join(root, ".git", "config")
    # A worktree's .git is a file pointing elsewhere; read through it.
    if os.path.isfile(cfg):
        try:
            target = open(cfg).read().strip()
            if target.startswith("gitdir:"):
                gd = target.split(":", 1)[1].strip()
                cfg = os.path.join(root, gd, "config")
        except OSError:
            return None
    try:
        text = open(cfg, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    in_origin = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_origin = line.lower().startswith('[remote "origin"]')
            continue
        if not in_origin or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip().lower() == "url" and v.strip():
            return v.strip()
    return None

def owner_repo(url):
    url = url.strip().rstrip("/")
    if "://" in url:
        rest = url.split("://", 1)[1]
        if "/" not in rest:
            return None
        path = rest.split("/", 1)[1]
    else:
        if ":" not in url:
            return None
        path = url.rsplit(":", 1)[1]
    if path.endswith(".git"):
        path = path[: -len(".git")]
    segs = [s for s in path.split("/") if s]
    if len(segs) < 2:
        return None
    # sanitize() is applied to the JOINED owner/repo, not to each segment: a '/'
    # maps to '-', which is what makes "ishan-parihar/self-improve-plugin" one
    # bank name instead of a path. Dropping this produced a bank with a slash in
    # it, which the dream stage then used as a filename.
    return sanitize(f"{segs[-2]}-{segs[-1]}")

d = os.environ.get("MEM_DERIVE_DIR") or os.getcwd()
root = worktree_root(d)
bank = None
if root:
    url = origin_url(root)
    if url:
        bank = owner_repo(url)
if not bank:
    bank = sanitize(os.path.basename(os.path.abspath(d)))
print(bank or "memory-wire")
PY
}

# Resolve the bank through the full ladder. Callers use this instead of a local
# copy, so the env file, the two env vars and the git derivation are consulted in
# one order everywhere.
memory_wire_resolve_bank() {
  local dir="${1:-$PWD}"
  if [[ -n "${SELF_IMPROVE_MEMORY_BANK:-}" ]]; then
    printf '%s' "$SELF_IMPROVE_MEMORY_BANK"
    return 0
  fi
  if [[ -n "${MEMORY_WIRE_BANK:-}" ]]; then
    printf '%s' "$MEMORY_WIRE_BANK"
    return 0
  fi
  memory_wire_derive_bank "$dir"
}
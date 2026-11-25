# self-improve-plugin

A jcode plugin that turns each turn's content into curated, durable lessons.
Ported from the `self-improve` mechanism in dsh (deepseek harness) to jcode's
lifecycle-hook surface.

## What it does

`self-improve.sh` is wired to `turn_end` in `~/.jcode/config.toml`. After each
turn it:

1. builds a digest of the turn's assistant text and a cue from the transcript;
2. routes the digest through the aroute gateway's `small-stack` classifier to
   score it (skips trivial turns, rate-limits the rest);
3. dispatches a curator call through the same gateway, which returns a
   `CURATION: <verb>|<target>|<detail>` line;
4. appends the parsed lesson to `~/.jcode/self-improve/ledger.jsonl` with a
   dedup guard, so the same cue never lands twice.

Every step is defensive: a dead gateway, a truncated response, a rate limit or
a recursion guard each leave the ledger untouched and exit 0, because a lesson
capture that fails must never take down the turn that produced it.

## The dream stage

`dream.sh` is wired to `session_end`. The turn loop is the awake phase: it records
lessons and project facts and has no time to reconcile anything. The dream stage is
where the ledger meets memory. It runs memory-wire's hygiene pass over the
project's bank and appends one consolidation record to the **same** ledger.

It runs at most once per bank per day. Hygiene is O(n²) over every row: 28,226
candidates from 1,339 rows in 4.5s, and about 7 minutes at 4,288. The stamp is the
claim and is written **before** the pass, under a per-bank lock, so two sessions
ending together cannot both run it. A pass that does not complete releases the
claim, so a failure never burns the day.

It does not apply candidates. Hygiene emits merge, supersede, anchor, promote and
split; a wrong merge silently corrupts recall and there is no undo, so the record
carries `applied: false` and consolidation stays an explicit act.

Every failure path exits 0 with a ledger entry and a diagnostics line: no memory
URL, unreachable server, unknown bank, and fewer than two rows are distinct
outcomes.

## Files

| path | role |
|---|---|
| `self-improve.sh` | the turn stage; run per turn on `turn_end` |
| `dream.sh` | the dream stage; run per session on `session_end` |
| `lib/common.sh` | shared paths, env loading, JSON writer, ledger cap, bank ladder |
| `lib/report.py` | the `--report` reader: dream records grouped by bank |
| `lib/test_self_improve.py` | the suite: parser, routing, dedup, housekeeping, concurrency |
| `genome.toml` | project genome (core + records + integrity) |
| `env.example` | the plugin-local settings file, copied to `~/.jcode/plugins/self-improve/env` |

Both stages source `lib/common.sh`, so the ledger path, the env file, the record
writer and the bank derivation exist once. They used to be copied, and the copies
had drifted: the dream stage kept a different ledger, skipped the env file, and
derived bank names that disagreed on case.

State lives outside the repo at `~/.jcode/self-improve/` (ledger, diagnostics,
state, dream stamps) so the plugin stays stateless and the capture survives
reinstalls.

A hook entry in `config.toml` is a path, not a command with arguments, so the
knobs live in `~/.jcode/plugins/self-improve/env` — the same pattern as the
aroute credential file. Copy `env.example` there to point the lesson share at a
memory-wire server. Only `env.example` is tracked; `env` is machine-local and
gitignored, so a fresh clone ships no host-specific bank or port.

## Docs

| doc | answers |
|---|---|
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | every environment variable and state file |
| [`docs/LEDGER.md`](docs/LEDGER.md) | the ledger entry shape, the parser contract, and how to read it back |

## Install

```bash
git clone https://github.com/ishanparihar/self-improve-plugin
# then add to ~/.jcode/config.toml:
#   [[hook]]
#   event = "turn_end"
#   command = "/path/to/self-improve.sh"
```

Requires `curl`, `jq`, and a provider credential at
`~/.config/jcode/provider-aroute.env` (see `/router-ops`).
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

## Files

| path | role |
|---|---|
| `self-improve.sh` | the plugin; one file, sourced nowhere, run per turn |
| `lib/test_self_improve.py` | the suite: parser, routing, dedup, housekeeping |
| `genome.toml` | project genome (core + records + integrity) |

State lives outside the repo at `~/.jcode/self-improve/` (ledger, diagnostics,
status) so the plugin stays stateless and the capture survives reinstalls.

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
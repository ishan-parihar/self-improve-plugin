# STATE

Living snapshot. The newest accurate statement of where this project is; history belongs in the worklog, plans and archive.

## As of 2026-11-24

First public release candidate. `self-improve.sh` is feature-complete against
the dsh original, and the suite proves the parser, gates, dedup, housekeeping
and the full fake-gateway path.

## Resume here

Nothing in flight. The next piece of work is a release check, not a feature.

## What is done

- Plugin written and wired to `turn_end` in `~/.jcode/config.toml`.
- Full suite in `lib/test_self_improve.py` (10 cases) green: guards, rate
  limiter, ledger cap, spawn pruning, and end-to-end through a loopback
  gateway including the dedup contract.
- `genome lint` green on the shipped modules (core, records, integrity).

## What is not done

- The operator's real ledger and diagnostic files are empty; the plugin has
  been verified against the fake gateway only. A first live run under a real
  turn will be the acceptance test.
- Semantic dedup (near-duplicate cues) is not implemented: exact cue match
  only. This is a decision, not a bug; see the ledger doc.
- `JCODE_HOOK_PAYLOAD`'s user-message field names are unverified guesses. The
  digest currently leans on assistant text, so a wrong guess degrades context
  and does not break the write.

## Known risks

- Two curator branches can fire on the same turn; both are backgrounded and
  awaited, and the rate limiter counts one spawn per turn.
- The router and curator both go through the public aroute gateway. If the
  gateway is down, the turn still ends clean, and `router-failed` lands in
  diagnostics.

## Wiring

```toml
[[hook]]
event = "turn_end"
command = "~/.jcode/plugins/self-improve/self-improve.sh"
```
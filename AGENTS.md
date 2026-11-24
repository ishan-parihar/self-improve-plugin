# AGENTS.md — self-improve-plugin

A jcode lifecycle-hook plugin that turns a completed turn into a curated, durable
lesson. Ported from dsh's `self-improve` plugin to jcode's `turn_end` hook.

## Routing — one owner per question

| Question | Owner |
|---|---|
| What does the plugin do and why | `README.md` |
| How to configure every knob | `docs/CONFIGURATION.md` |
| How the ledger entry is shaped and read back | `docs/LEDGER.md` |
| What has been decided about the design | `docs/decisions/` (`genome rec list`) |
| What failed and how it was fixed | `docs/regressions/` |
| Current work and the resume point | `docs/handoff/STATE.md` |
| Append-only log of what happened | `docs/kb/WORKLOG.md` |
| Closed adoptions for dated plans | `docs/kb/CLOSED.md` |
| Operator questions and rulings | `docs/kb/ASKS.md` |

Do not answer a question answered above by writing a new document: amend the owner.

## The gate

```bash
bash -n self-improve.sh && shellcheck -S warning self-improve.sh && python3 lib/test_self_improve.py
```

All three must pass before any push. The suite runs with a fake `HOME` and a
loopback gateway, so it never touches the operator's real ledger and needs no
credential or network.

## Working rules

- `self-improve.sh` is one file with no sourcing. Keep it that way.
- Every failure path exits 0 and logs a diagnostic. A lesson capture that
  fails must never take down the turn that produced it.
- State lives at `~/.jcode/self-improve/`, outside this repo. The plugin stays
  stateless so the capture survives reinstalls.
- Defaults are safe. A new knob must default to the current behaviour.
- Test before you assert. A change to the parser, the gates, the dedup or the
  rate limiter needs a case in `lib/test_self_improve.py` that fails without it.
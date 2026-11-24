# Transients — the lifecycle register for dated plans and audits

One row per dated file under `docs/plans/` and `docs/audit/`. A row is added by
`genome tadopt <path>`, never by hand — the gate reads this table, so a row that
drifts from it is a row that lies.

| path | status | consolidation_target | evidence | adopted |
|---|---|---|---|---|

| `docs/plans/2026-11-25-close-the-self-improve-audit-gaps-quoting-safe-records-calla.md` | open | — | — | 2026-11-24T23:42 |

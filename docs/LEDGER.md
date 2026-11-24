# LEDGER

`ledger.jsonl` is the plugin's product: one JSON object per line, append-only,
capped to the last `SELF_IMPROVE_MAX_LEDGER_ENTRIES` (default 500).

## Entry shape

```json
{
  "cue": "debugging a flaky test",
  "lesson": "Wait on a condition variable, never sleep-and-poll, because sleep is racy",
  "evidence": "the test failed intermittently and passed with a cv wait",
  "confidence": "high",
  "verdict": "not observed",
  "kind": "skill",
  "origin": "direct",
  "createdAt": "2026-11-24T14:13:35.110Z",
  "sourceTurn": "session-id-or-unknown"
}
```

| field | meaning |
|---|---|
| `cue` | the situation the lesson applies to; also the dedup key |
| `lesson` | the generalizable rule, imperative, one sentence with WHY |
| `evidence` | what in the turn supported it |
| `confidence` | `high` / `medium` / `low`; the last field, defaults `medium` |
| `verdict` | always `not observed` on write; a later observer may promote it |
| `kind` | `skill` or `genome` |
| `origin` | `direct` (a curator wrote it); dsh's other origins are unused here yet |
| `createdAt` | UTC RFC 3339 with milliseconds |
| `sourceTurn` | `JCODE_HOOK_SESSION_ID`, so a lesson traces back to its turn |

## How the fields are derived

The curator answers with a single line:

```
CURATION: <cue> | <lesson> | <evidence> | <confidence>
```

The parser splits on `|`, trims each part, and treats the last part as the
confidence only if it spells `high`, `medium` or `low` (case-sensitive,
comma-delimited). Everything between the first and last part is the lesson, so a
lesson may itself contain `|`. Fewer than three parts is a parse failure and
writes nothing. A field that is literally `<...>` is a template placeholder and
writes nothing (log kind: `curation-template`).

The genome branch may answer with a `GENOME:` line and no `CURATION:` line:

```
GENOME: <what recorded> | <what skipped>
```

That is mapped onto `cue` = what recorded, `lesson` = a fixed clause,
`evidence` = what skipped.

## Dedup

The ledger is exact-match per `cue`. A turn whose cue is already in the ledger
writes nothing (diagnostic kind `curation-duplicate`). This is deliberately
conservative: the same situation recurring does not deepen a lesson, it copies
it, so the copy is refused. Semantic near-duplicates are not detected; that
needs an embedding, which is a separate decision.

## Reading it back

```bash
# newest lessons first
tac ~/.jcode/self-improve/ledger.jsonl | head

# every cue, one per line, for eyeballing the library
jq -r .cue ~/.jcode/self-improve/ledger.jsonl | sort | uniq -c | sort -rn
```

## Diagnostics

`diagnostics.jsonl` is the audit trail, also append-only and uncapped. One line
per decision, with `kind` in: `skip`, `router`, `cap`, `router-failed`,
`no-curation`, `curation-nothing`, `curation-parse-failed`,
`curation-incomplete`, `curation-template`, `curation-duplicate`,
`curation-recorded`, `curator-failed`.

```bash
# what was recorded, most recent last
jq -r 'select(.kind=="curation-recorded")' ~/.jcode/self-improve/diagnostics.jsonl
```
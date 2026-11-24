# CONFIGURATION

Every knob is an env var read by `self-improve.sh`. Defaults are the current
behaviour; an unset variable means the default.

## Identity and gating

| var | default | what it does |
|---|---|---|
| `JCODE_HOOK_STATUS` | — | must be `ok`; any other value is a non-reflection exit |
| `JCODE_HOOKS_DISABLED` | — | set to `1` to disable the plugin entirely (recursion guard) |
| `JCODE_HOOK_LAST_ASSISTANT_TEXT` | — | the digest source; empty means skip |
| `JCODE_HOOK_PAYLOAD` | — | optional JSON; a user message is extracted for context |
| `JCODE_HOOK_SESSION_ID` | `unknown` | turn ref, and the per-session rate-limit key |
| `JCODE_HOOK_MODEL` | `unknown` | recorded in the digest header only |

## Gateway and routing

| var | default | what it does |
|---|---|---|
| `AROUTE_API_KEY` | — | required; loaded from `~/.config/jcode/provider-aroute.env` if unset |
| `SELF_IMPROVE_ROUTER_URL` | `https://aroute.ishanparihar.com/v1/chat/completions` | gateway endpoint for both router and curator |
| `SELF_IMPROVE_ROUTER_MODEL` | `small-stack` | scores the digest |
| `SELF_IMPROVE_CURATOR_MODEL` | `large-stack` | writes the lesson |
| `SELF_IMPROVE_ROUTER_MAX_TOKENS` | `2000` | router `max_tokens` |
| `SELF_IMPROVE_SKILL_THRESHOLD` | `0.65` | skill branch fires at or above this |
| `SELF_IMPROVE_GENOME_THRESHOLD` | `0.70` | genome branch fires at or above this |

## Budget and limits

| var | default | what it does |
|---|---|---|
| `SELF_IMPROVE_DIGEST_MAX_CHARS` | `4000` | assistant text truncated to this, at a sentence boundary |
| `SELF_IMPROVE_MAX_PER_HOUR` | `6` | per-session spawns per rolling hour |
| `SELF_IMPROVE_MAX_PROFILE_PER_HOUR` | `24` | profile-wide spawns per rolling hour |
| `SELF_IMPROVE_MAX_LEDGER_ENTRIES` | `500` | ledger is capped to the last N entries |
| `SELF_IMPROVE_STATE_TTL_SECS` | `86400` | per-session spawn files older than this are pruned |

## Optional integration

| var | default | what it does |
|---|---|---|
| `SELF_IMPROVE_MEMORY_URL` | unset | if set, POST each lesson to this URL as JSON; unset = no call, no failure mode |

## State files (created under `$HOME/.jcode/self-improve/`)

| path | role |
|---|---|
| `ledger.jsonl` | append-only lessons, capped at `MAX_LEDGER_ENTRIES` |
| `diagnostics.jsonl` | one JSON line per decision: `skip`, `router`, `cap`, `curation-*` |
| `state/spawns-<session>` | rolling-hour timestamps for the per-session limiter |
| `state/spawns-profile` | rolling-hour timestamps for the profile limiter |
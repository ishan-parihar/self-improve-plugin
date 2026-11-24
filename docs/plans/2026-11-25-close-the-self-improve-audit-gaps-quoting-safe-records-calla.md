# Close the self-improve audit gaps: quoting-safe records, callable arm tests, concurrent-write safety

Dated-frozen: published `2026-11-25`. A plan that changes is a new plan (or declare it
in genome.toml `core.living_plans` and keep a 'Resume here' section).

## Mechanism

The plugin's contract is "a lesson capture that fails must never take down the turn
that produced it, and the ledger is the product". An audit pass on 2026-11-25 probed
each claim against the running code rather than reading it, and found the contract
holds for crashes but not for **content**: three defects let bad data into the two
files the plugin owns, and one class of guard has no test that can fail.

Assumed load: one jcode process at a time, one `turn_end` per turn, two curator
branches possibly firing on the same turn, gateway reachable or not.

### Findings the probes proved

1. **A cue containing a double quote or a backslash corrupts diagnostics.**
   `log_diag` (`self-improve.sh:69-74`) builds JSON with `printf`, so any `"` or
   `\` in the interpolated value breaks the line. Matrix probe over 10 curator
   answers: 8 produced parseable diagnostics, and the two whose cue contained a
   quote or a backslash produced `BADJSON`. The ledger row for those same turns is
   fine, because that write goes through `json.dumps`. The same two characters also
   defeat the dedup grep (`:503`), which matches the literal `"cue": "<cue>"`, so
   the lesson is recorded twice.

2. **Concurrent curators both pass the dedup check.** Proven against the real
   plugin, not a simulation: one turn, gateway answering 0.9/0.0, produced two
   `curation-recorded` lines and two ledger rows with the same cue (`genome` and
   `skill`), because `run_skill_curator` and `run_genome_curator` are backgrounded
   (`:612-617`) and both pass the check before either appends. Appends themselves
   are safe (200 concurrent appends, 0 corrupt lines), so the fix is the check, not
   the write.

3. **Eight of thirteen diagnostic arms have no test that can fail.** Written:
   `cap`, `curation-duplicate`, `curation-incomplete`, `curation-nothing`,
   `curation-parse-failed`, `curation-recorded`, `curation-template`,
   `curator-failed`, `memory-failed`, `no-curation`, `router`, `router-failed`,
   `skip`. Asserted by the suite: `cap`, `curation-duplicate`, `curation-nothing`,
   `memory-failed`, `router-failed`, `skip` (6 of 13). The untested arms are exactly
   the "nothing happened" paths plus `curation-recorded`, which is the only arm that
   means the product worked.

4. **`diagnostics.jsonl` is unbounded.** The ledger is capped at
   `MAX_LEDGER_ENTRIES` (`:144-152`); diagnostics has no cap, no prune, no rotation.
   It is one line per turn per branch, forever.

5. **The suite's fake gateway answers a different shape than production for the
   memory share, and never asserts the model actually sent.** The suite sets
   `SELF_IMPROVE_ROUTER_MODEL`/`CURATOR_MODEL` but asserts nothing about them, so a
   regression that swaps the two models passes.

6. **`env` is tracked in git and duplicates `env.example`.** Probe: the two files'
   settings are identical; `env` is the live machine config (bank `omp`, port 8888)
   and is committed to the public repo. Two files that must change together, one of
   them machine-specific, in a public tree.

7. **The digest's sentence-boundary trim is not derived from the limit it protects.**
   `:190-198` cuts at `DIGEST_MAX_CHARS` (default 4000) and then trims to the last
   sentence only when the leftover exceeds a hardcoded 200 characters. The constant
   is unrelated to the configured limit, so lowering `DIGEST_MAX_CHARS` below ~200
   silently disables the trim, and a reader cannot tell from the code which value
   governs. Cosmetic, but it is a knob that does not do what its name says at every
   setting.

8. **Fixed ports in the suite** (18977/18978/18980) collide when two runs go at
   once, and the failure mode is a hang or a false pass, not an error.

### Not findings (checked, clean)

- Ledger append atomicity under concurrency: 200 parallel appends, 0 corrupt lines.
- `set -e` on the router path: fixed in `e6e401e`, covered by a regression case.
- `A && B || C` in the cap: the `rm` only runs when `tail` fails, which is the
  intent; shellcheck's SC2015 is a false positive here.
- Rate limiter per-session keying and the `unknown` fallback: correct, and the
  fallback collides only when `JCODE_HOOK_SESSION_ID` is unset, which the suite
  covers.

## Order of work

Each step is small enough to land and verify on its own, and each one is proved by
the gate in the same commit. Do them in this order, because the later steps read the
earlier ones' output.

1. **Quoting-safe diagnostics** (finding 1, half). Replace the `printf` in
   `log_diag` with a `python3 -c` call that takes the fields as argv and prints
   `json.dumps`. One writer, one place. Add a case whose cue contains `"` and `\`
   and assert every diagnostics line parses.
2. **Quoting-safe dedup** (finding 1, half). Replace the literal grep with a
   `python3 -c` reader that loads the ledger and tests `entry["cue"] == cue`. This
   also makes the check correct for a cue that spans a line. Same commit as step 1,
   because both are "the record is the product".
3. **One row per cue under concurrency** (finding 2). Wrap the check-and-append in
   `flock` on a lock file beside the ledger, so the second curator waits and then
   sees the first one's row. Add a case that runs the turn twice concurrently and
   asserts one row.
4. **Cap diagnostics** (finding 4). Reuse the ledger's cap block as a helper and
   call it for both files. Add a case that seeds past the cap and asserts the trim.
5. **Arm coverage** (finding 3). One case per untested arm, each driving the real
   entry point: `curation-incomplete`, `curation-parse-failed`, `curation-template`,
   `curator-failed`, `no-curation`, `router`, and `curation-recorded`.
6. **Assert the model on the wire** (finding 5). The fake gateway already records
   bodies; assert the router body names `small-stack` and the curator body names
   `large-stack`.
7. **Untrack `env`** (finding 6). `git rm --cached env`, add `env` to `.gitignore`,
   keep `env.example` as the only shipped copy, and note the move in the README.
8. **Derive the trim constant** (finding 7). Compute the threshold from
   `DIGEST_MAX_CHARS` instead of the literal 200.
9. **Free the ports** (finding 8). Bind port 0 in the suite and read the assigned
   port back, so two runs cannot collide.

## Gates / success criteria

(fixed before the run)

1. `bash -n`, `shellcheck -S warning`, and the suite all pass, and the suite has at
   least one case per diagnostic arm listed in finding 3.
2. A cue containing `"`, `\`, and a newline round-trips: recorded once, deduped on
   the second identical turn, and every diagnostics line parses as JSON.
3. Eight parallel runs with one cue produce exactly one ledger row.
4. `diagnostics.jsonl` is capped by the same mechanism as the ledger, and a test
   proves the cap holds.
5. The suite asserts the model string the plugin actually sent, for both the router
   and the curator.
6. `env` is untracked and gitignored; `env.example` is the only shipped copy; a fresh
   clone has no machine-specific settings.
7. Two suite runs in parallel both pass (no fixed-port collision).
8. The published clone passes the gate from a clean checkout.

## Cost

One focused pass over `self-improve.sh` (one writer, one check, one cap) plus the
suite. No new dependencies, no new files beyond the plan and the gitignore line.
Findings 1, 2 and 4 are the ones that change what the product contains; 3, 5, 6 and
7 are what make the next regression visible instead of silent.
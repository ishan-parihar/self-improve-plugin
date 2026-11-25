# Add the dream stage: consolidate memory as a session_end job

Dated-frozen: published `2026-11-25`. A plan that changes is a new plan (or declare it
in genome.toml `core.living_plans` and keep a 'Resume here' section).

## Mechanism

The capture stage (`self-improve.sh`, `turn_end`) writes one ledger row per turn and
shares the project fact to memory. It is a *producer*: fast, per-turn, loss-tolerant.

What is missing is the *consumer*. memory-wire ships a deterministic hygiene scan that
emits five candidate kinds — `merge`, `supersede`, `anchor`, `promote`, `split` — and
the write primitives to apply each verdict (`supersede`, `redact`, `delete`,
`bank_config`). Nothing currently reads those candidates, so the bank only grows.

Measured on the live `omp` bank at 4,291 rows: 16,854 supersede, 7,267 merge, 1,302
anchor, 252 split, 1 promote. 25,676 candidates, zero applied.

This plan adds `dream.sh`, a second entry point in the same repo, wired to
`session_end`. It reads a bounded slice of candidates, has a model judge each one, and
applies the verdicts. Same gate, same genome, same ledger discipline as capture.

### Why session_end and not turn_end

`hygiene_scan` is O(n²) over the whole bank: the cursor narrows the *delta* but the
detectors still need every row for context (`src/hygiene.rs:241` "we do not paginate
because the scan needs the full set"). At 4,288 rows that is 9.19M Jaccard comparisons
and ~7 minutes at 92% CPU. On a per-turn hook that is ruinous. On session end it is a
background job the agent never waits for.

### Why per-project banks are a prerequisite, not a nicety

The same O(n²) makes bank size the whole cost model. `omp` holds 4,291 of 6,366 rows
(67%) because the plugin pins `SELF_IMPROVE_MEMORY_BANK=omp`, overriding the
`owner/repo` derivation in `src/paths.rs`. `memory-wire doctor` names the result: a hook
in this repo resolves to `ishan-parihar-self-improve-plugin`, which holds nothing, while
the memories sit in `omp`. Splitting per project is what makes a dream run seconds
instead of minutes.

## Order of work

Each step lands and verifies on its own, and each is proved by the gate in the same
commit. The order matters: step 2 reads step 1's output, and step 4 is what makes step 3
cheap.

1. **Stop pinning the bank** (prerequisite). Drop `SELF_IMPROVE_MEMORY_BANK=omp` from
   `env` and `env.example`; let the `owner/repo` rung derive the bank. Add a case that
   asserts the derived bank name reaches the wire.
2. **Write `dream.sh`** (the stage). `session_end` entry point: read
   `memory-wire hygiene --bank <derived> --cursor <watermark>`, cap the slice, judge each
   candidate against the bank with one `large-stack` call, apply `supersede`/`redact`/
   `delete` for the verdicts it can act on, and record what it did. Exit 0 always, log a
   diagnostic per candidate, never touch the turn.
3. **Add dream cases to the suite** (the proof). A fake memory-wire sink and a fake
   gateway, so the whole path runs with no network: candidate parsing, the cap, each
   verdict branch, a dead server, and a malformed candidate line.
4. **Make the scan cheap** (the scale fix). Per-project banks from step 1 plus a
   persisted watermark, so a session end scans only what is new. Assert the watermark
   advances and a second run with no new rows is a no-op.
5. **Wire `session_end`** in `~/.jcode/config.toml` beside the existing `turn_end`, and
   record the wiring in `docs/CONFIGURATION.md`.
6. **Live verify and document** (the acceptance). One real session end against the real
   bank, then `docs/LEDGER.md` gains the dream section and `STATE.md` records it.

## Gates / success criteria

(fixed before the run)

1. `bash -n`, `shellcheck -S warning`, and the suite pass, with at least one case per
   dream branch (candidate kinds, cap, dead server, malformed line, watermark no-op).
2. A dream run against a bank with no new rows changes nothing and exits 0.
3. Every applied verdict is a real memory-wire call, asserted at the sink, not a log
   line.
4. The watermark advances only after a successful scan, so a failed run is retried, not
   skipped.
5. `dream.sh` never writes to the ledger and never blocks a turn: it is `session_end`
   only, and every failure path exits 0.
6. The gate runs both stages: `bash -n` on both scripts, `shellcheck` on both, one
   suite.
7. Two suite runs in parallel both pass (no fixed-port collision).

## Cost

One new script (~200 lines) and its cases, plus a one-line config change and the env
change. The expensive part is the model calls, which is why the slice is capped and the
priority order is supersede first (cheapest correct win: two rows, one contradicts the
other, the newer wins).

## Not in this plan

- Splitting `omp`'s existing 4,291 rows back to their projects. The `context` and
  `document_id` fields may carry enough provenance, but that is a migration with its own
  risk and belongs in its own plan.
- A nightly timer. `session_end` needs no new infra; a systemd timer can come later once
  the cursor logic is proven.
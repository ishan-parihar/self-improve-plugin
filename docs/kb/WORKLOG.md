# WORKLOG

Append-only. One entry per session that changed something. Never rewrite.
## 2026-11-24 — first public release

- Ported the dsh self-improve architecture to a jcode `turn_end` hook as a single
  shell script (`self-improve.sh`); state stays outside the repo at
  `~/.jcode/self-improve/`.
- Wrote the 10-case suite in `lib/test_self_improve.py`: all guards, the rate
  limiter, the ledger cap, spawn pruning, and the full fake-gateway path
  including dedup. Green from a clean clone.
- Fixed a live defect found while designing the memory-share path: the hardcoded
  `http://127.0.0.1:8888/retain` target was a dead port (memory-wire listens on
  18913/18914). Replaced with an opt-in `SELF_IMPROVE_MEMORY_URL`, default unset,
  so the integration has no failure mode until the operator enables it.
- Scaffolded the genome (core, records, integrity); `genome lint` clean.
- Published as https://github.com/ishan-parihar/self-improve-plugin (public, MIT,
  description, topics, FUNDING.yml, LICENSE).
- **2026-11-24T20:02** — Ported the dsh self-improve architecture to a jcode turn_end hook and published it as ishan-parihar/self-improve-plugin; 10-case suite green from a clean clone; replaced the dead memory-wire endpoint with an opt-in SELF_IMPROVE_MEMORY_URL

#!/usr/bin/env python3
"""Read the ledger's dream records back: `dream.sh --report`.

This is the reader for the `applied` field. A recorded field with no consumer is a
promise, not a contract, so the field needs something that reads it.

It is deliberately read-only: no hygiene pass, no ledger write, no claim. Nothing
here can change the state of the loop, which is what makes it safe to run at any
time.

Usage: SELF_IMPROVE_LEDGER=/path/to/ledger.jsonl python3 lib/report.py
"""
from __future__ import annotations

import json
import os
import sys


def load_dream_rows(path: str) -> list[dict]:
    """Only the dream rows, skipping anything that is not one.

    A truncated or foreign line is skipped rather than fatal: a report should
    describe what it can read, not refuse because one row is damaged. The row count
    is checked by the caller against the file's line count, so a lost row is still
    visible.
    """
    rows = []
    try:
        fh = open(path, encoding="utf-8")
    except FileNotFoundError:
        return rows
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("kind") == "dream":
                rows.append(entry)
    return rows


def by_bank(rows: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for e in rows:
        b = out.setdefault(
            e.get("bank") or "?",
            {"passes": 0, "candidates": 0, "by_kind": {}, "last": None, "outcomes": {}},
        )
        b["passes"] += 1
        b["last"] = e.get("ts")
        o = e.get("outcome") or "?"
        b["outcomes"][o] = b["outcomes"].get(o, 0) + 1
        try:
            detail = json.loads(e.get("detail") or "")
        except ValueError:
            detail = {}
        b["candidates"] += detail.get("total") or 0
        for k, v in (detail.get("by_kind") or {}).items():
            b["by_kind"][k] = b["by_kind"].get(k, 0) + v
    return out


def render(path: str, rows: list[dict]) -> str:
    if not rows:
        return f"no dream records in {path}"

    banks = by_bank(rows)
    applied = sum(1 for e in rows if e.get("applied"))
    lines = [
        f"dream report  ({path})",
        "  passes: %d   applied: %d   awaiting a decision: %d"
        % (len(rows), applied, len(rows) - applied),
    ]
    for bank in sorted(banks):
        b = banks[bank]
        kinds = ", ".join(
            "%s %d" % (k, b["by_kind"][k]) for k in sorted(b["by_kind"])
        )
        outs = ", ".join(
            "%s %d" % (k, b["outcomes"][k]) for k in sorted(b["outcomes"])
        )
        lines.append(
            "  %-40s passes=%-3d candidates=%-6d last=%s"
            % (bank, b["passes"], b["candidates"], b["last"])
        )
        lines.append("      candidates: %s" % (kinds or "none"))
        lines.append("      outcomes:   %s" % (outs or "none"))
    lines.append("")
    lines.append(
        "Nothing was applied. Candidates are a report, not an action: a wrong merge"
        " silently corrupts recall and there is no undo."
    )
    return "\n".join(lines)


def main() -> int:
    path = os.environ.get("LEDGER") or os.path.expanduser(
        "~/.jcode/self-improve/ledger.jsonl"
    )
    print(render(path, load_dream_rows(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

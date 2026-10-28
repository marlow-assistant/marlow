#!/usr/bin/env python3
"""Print the session context curve from usage.jsonl — one line per `claude -p`.

The "reading half" docs/tasks/usage-telemetry.md deliberately did not build.
Written 2026-08-23, when grepping marlow-server.log for "compact" showed only
the compaction lines and made the growth between them invisible. The growth was
never unlogged: record_invocation has written the three prefix counters for
every invocation since the telemetry task shipped. It was only unread.

    python3 scripts/context-curve.py                 # today, all sessions
    python3 scripts/context-curve.py --days 3
    python3 scripts/context-curve.py --session f46755dc

`prefix` is the same sum server/idle_compaction.py gates on, so a row here is
directly comparable to a "prefix >= N tokens" line in the server log.

**A row is NOT the context size.** `cache_read` is the CLI's total across the
invocation's internal agentic loop, so a row is roughly size x loop depth: a
one-call turn reads about the prefix, a ten-tool-call turn reads about ten
times it. Rows above the context window are normal and are the giveaway (this
file's first read showed 488,762). Compare rows of similar depth, treat the
SMALLEST reading in a stretch as the closest thing to the real size, and see
docs/tasks/idle-compaction.md for why the gate is deliberately left reading the
inflated number.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PREFIX_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens")

DEFAULT_PATH = Path(
    os.environ.get("MARLOW_TELEMETRY_PATH")
    or (os.environ.get("MARLOW_LOG_DIR", "~/.marlow/logs") + "/usage.jsonl")
).expanduser()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--days", type=int, default=1, help="how many days back, including today (default 1)")
    ap.add_argument("--session", help="only rows whose session id starts with this")
    args = ap.parse_args(argv)

    if not args.path.is_file():
        print(f"no telemetry at {args.path}", file=sys.stderr)
        return 1

    since = date.today() - timedelta(days=args.days - 1)
    previous: dict[str, int] = {}
    rows = 0

    for line in args.path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # A torn last line during a live write is normal, not a failure.
            continue
        ts = str(record.get("ts") or "")
        try:
            when = datetime.strptime(ts[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if when < since:
            continue
        session = record.get("session_id") or "-"
        if args.session and not session.startswith(args.session):
            continue

        # Sum defensively and treat a missing key as absent, not zero — same
        # reasoning as _sum_prefix_tokens: an error envelope reports nothing,
        # and printing 0 for it reads as a real measurement of an empty
        # context. That exact confusion cost a day on 2026-08-21.
        parts = [record.get(k) for k in PREFIX_KEYS]
        if all(p is None for p in parts):
            prefix_text, delta_text = "        -", ""
        else:
            prefix = sum(p or 0 for p in parts)
            delta = prefix - previous[session] if session in previous else None
            previous[session] = prefix
            prefix_text = f"{prefix:>9,}"
            delta_text = "" if delta is None else f"  {delta:>+9,}"

        flags = "".join(
            (
                "E" if record.get("is_error") else " ",
                "D" if record.get("had_permission_denial") else " ",
            )
        )
        print(f"{ts[:19]}  {str(record.get('source')):>10}  {session[:8]}  {flags}  {prefix_text}{delta_text}")
        rows += 1

    if not rows:
        print(f"no rows in the last {args.days} day(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

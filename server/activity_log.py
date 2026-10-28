"""The persisted activity log: one line per turn, what Marlow did
(docs/tasks/ux-lane.md §2a).

Deterministic by construction. Every field comes from something the harness
observed -- the stream's tool_use blocks, the permission round-trip, the
envelope's `permission_denials`, the identity commit -- and nothing from the
model's account of itself. A model-written summary would be a second call per
turn and a claim, and `dated-plans-go-stale.md` found two invented
self-diagnoses in Marlow's memory.

**No prompt, no reply, no error text.** The chat holds the first two, linked by
`turn_id`. `turn.error` is excluded because an InvokeError can quote the CLI's
stdout, which is reply content. Tool inputs reach this file only as the
allowlisted `target` in `invoke.ToolCall`.

**Where it lives is part of the design.** `settings.data_dir` is in
`boundaries.SECRET_PATHS`: hard-denied to the agent and hidden by the bwrap
tmpfs. Marlow can neither read nor rewrite its own record, which is the only
thing that makes "true by construction" true.

One file per UTC month, appended one record per `write()`. Retention deletes
whole months, so nothing is ever rewritten in place. A crash mid-write can at
worst leave a torn last line, which the reader skips.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from server.config import settings
from server.log import get_logger

if TYPE_CHECKING:
    from server.identity_git import TurnCommit
    from server.queue import Turn

logger = get_logger(__name__)

RECORD_VERSION = 1

# Months of history kept, the current one included (Mike, 2026-09-28).
RETENTION_MONTHS = 12

_MONTH_FILE = re.compile(r"^(\d{4})-(\d{2})\.jsonl$")


def activity_dir() -> Path:
    return settings.data_dir / "activity"


def _month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def build_record(
    turn: "Turn", *, merged: list[str], commit: "TurnCommit | None", has_output: bool = False
) -> dict[str, Any]:
    """The line for one finished turn. Pure: no I/O, safe on the event loop.

    `has_output` says a background row was written for this turn (below), so
    the client knows to offer the text on expand.

    `commit` is typed loosely at runtime on purpose: anything that is not a
    TurnCommit (a test double, a future None-returning path) records no files
    rather than failing the write.
    """
    paths = getattr(commit, "paths", None)
    sha = getattr(commit, "sha", None)
    denials = turn.result.permission_denials if turn.result else []
    record: dict[str, Any] = {
        "v": RECORD_VERSION,
        "turn_id": turn.turn_id,
        "source": turn.source,
        "label": turn.label,
        "merged": list(merged),
        "created_at": turn.created_at,
        "started_at": turn.started_at,
        "finished_at": turn.finished_at,
        "status": turn.status.value,
        "tools": [{"name": c.name, "target": c.target, "at": c.at} for c in turn.tool_calls],
        "tools_dropped": turn.tool_calls_dropped,
        "approvals": [dict(a) for a in turn.approvals],
        # Names only, the same posture invoke.py's denial warning holds: the
        # denial entry's own `tool_input` is exactly what must not persist.
        "denials": [
            d.get("tool_name") if isinstance(d, dict) and isinstance(d.get("tool_name"), str) else "?"
            for d in denials
        ],
        "files": list(paths) if isinstance(paths, tuple) else [],
        "commit": sha if isinstance(sha, str) else None,
        # MCP servers not connected when the turn started, from the CLI's own
        # init event (docs/tasks/connector-drops.md). None: no init was read.
        "mcp_not_connected": (
            list(turn.result.mcp_not_connected)
            if turn.result and turn.result.mcp_not_connected is not None
            else None
        ),
        # The model id the API answered with and the effort we passed
        # (invoke.InvocationResult). Raw facts: the clients word them.
        "model": turn.result.model if turn.result else None,
        "effort": turn.result.effort if turn.result else None,
    }
    # Additive, so RECORD_VERSION stays 1: absent means "not plumbing" and "no
    # output", which is also what every record written before them means
    # (docs/tasks/default-infrastructure.md "The activity record").
    if turn.tier is not None:
        record["tier"] = turn.tier
    if has_output:
        record["has_output"] = True
    return record


def append_record(record: dict[str, Any], *, now: float | None = None) -> None:
    """Append one record, then drop months past retention. Never raises.

    Same contract as `identity_git.commit_after_turn`: the reply is already
    the user's, and this runs in the single drain loop, so a full disk here is
    a missing log line and a logged error -- not a wedged queue.
    """
    _append(activity_dir(), record, now=now)


def _append(directory: Path, record: dict[str, Any], *, now: float | None = None) -> bool:
    """Whether the record landed. Never raises."""
    now = time.time() if now is None else now
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = time.gmtime(record.get("finished_at") or now)
        path = directory / f"{stamp.tm_year:04d}-{stamp.tm_mon:02d}.jsonl"
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            # One write() per record: with O_APPEND, a record lands whole or,
            # on a crash, as a torn tail the reader skips -- never interleaved.
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        logger.exception("activity log: failed to append to %s for turn %s", directory.name, record.get("turn_id"))
        return False
    _prune(directory, now)
    return True


def _prune(directory: Path, now: float) -> None:
    """Delete month files older than RETENTION_MONTHS. Never raises."""
    current = time.gmtime(now)
    oldest_kept = _month_index(current.tm_year, current.tm_mon) - (RETENTION_MONTHS - 1)
    try:
        names = os.listdir(directory)
    except OSError:
        logger.exception("activity log: could not list %s for retention", directory)
        return
    for name in names:
        match = _MONTH_FILE.match(name)
        if not match:
            continue
        if _month_index(int(match.group(1)), int(match.group(2))) >= oldest_kept:
            continue
        try:
            (directory / name).unlink()
            logger.info("activity log: dropped %s (past %d-month retention)", name, RETENTION_MONTHS)
        except OSError:
            logger.exception("activity log: could not drop %s", name)


# Page size bounds for `read_recent`. A month is a few hundred records, so
# the ceiling is about what one screen could ever want, not a performance knob.
DEFAULT_PAGE = 50
MAX_PAGE = 200


def read_recent(limit: int = DEFAULT_PAGE, before: float | None = None) -> list[dict[str, Any]]:
    """Newest-first records finished strictly before `before` (or all).

    Pages by timestamp rather than offset so a turn finishing while the phone
    scrolls cannot shift or repeat the page it is on. Reads month files newest
    first and stops at the first file that fills the page, so a request costs
    one or two files however long retention grows.

    Lines that do not parse -- a torn tail after a crash -- are skipped, not
    fatal: one bad line must not hide a month.
    """
    limit = max(1, min(int(limit), MAX_PAGE))
    directory = activity_dir()
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    months = sorted(
        (
            (_month_index(int(m.group(1)), int(m.group(2))), name)
            for name in names
            if (m := _MONTH_FILE.match(name))
        ),
        reverse=True,
    )
    if before is not None:
        cutoff = time.gmtime(before)
        newest_wanted = _month_index(cutoff.tm_year, cutoff.tm_mon)
        months = [(index, name) for index, name in months if index <= newest_wanted]

    found: list[dict[str, Any]] = []
    for _, name in months:
        records: list[dict[str, Any]] = []
        try:
            with open(directory / name, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    finished = record.get("finished_at")
                    if not isinstance(finished, (int, float)):
                        continue
                    if before is not None and not finished < before:
                        continue
                    records.append(record)
        except OSError:
            logger.exception("activity log: could not read %s", name)
            continue
        # Sorted, not just reversed: append order is finish order today, but
        # the page boundary is only sound if that is guaranteed here.
        records.sort(key=lambda r: r["finished_at"], reverse=True)
        found.extend(records)
        if len(found) >= limit:
            break
    return found[:limit]


# --- background output ---------------------------------------------------------
#
# The reply text of a cron turn that never reached the chat
# (docs/tasks/default-infrastructure.md "Seeing plumbing output"). The activity
# log above keeps no reply text by design, and a reply with no transcript row
# had nowhere else to go, so a job that replied `[silent]` left no trace of
# what it said. This is its own store rather than a field on the record so the
# list endpoint stays the same size and shape, and the text is fetched only
# when someone expands the row.
#
# Same directory posture as the log: under `data_dir`, so Marlow cannot rewrite
# what it reported. Same month files, append shape and retention.


def background_dir() -> Path:
    return settings.data_dir / "background"


def append_output(turn: "Turn", text: str, *, now: float | None = None) -> bool:
    """Record one undelivered reply; whether it landed. Never raises, like
    `append_record`: the record must not claim output that isn't there."""
    return _append(
        background_dir(),
        {"turn_id": turn.turn_id, "label": turn.label, "finished_at": turn.finished_at, "text": text},
        now=now,
    )


def read_output(turn_id: str) -> dict[str, Any] | None:
    """The background row for `turn_id`, or None. Newest month first.

    A linear scan: one row per undelivered cron reply is a few a night, so the
    whole retained store is a few thousand short lines. An index would be a
    second thing to keep true. Torn or foreign lines are skipped, as above.
    """
    directory = background_dir()
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("activity log: could not list %s", directory)
        return None
    months = sorted((name for name in names if _MONTH_FILE.match(name)), reverse=True)
    for name in months:
        try:
            with open(directory / name, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("turn_id") == turn_id and isinstance(row.get("text"), str):
                        return row
        except OSError:
            logger.exception("activity log: could not read background %s", name)
    return None

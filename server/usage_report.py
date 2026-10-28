"""The payload behind `GET /v1/usage` (docs/tasks/usage-visibility.md).

Facts only: fractions, epochs, counts. No display wording and no 90% line --
the phone owns those (a UX tweak must never need a box deploy).

Marlow's own activity comes from `usage.jsonl`, the same file telemetry writes,
read tolerantly (a bad line is skipped, a missing file is "no data"). Only the
live file is read, not its rotated siblings: 10 MB per file is months of turns
at this volume, far past the 7-day window, and a conversation's `started_at`
is overnight-recent.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from server import active_turn, auth_monitor
from server.config import settings
from server.idle_compaction import SOURCE as COMPACTION_SOURCE
from server.session_state import session_state
from server.usage_state import UsageState

# Window name -> length. Only windows named here get a `marlow` block: an
# unknown window's length is unknown, so its start can't be placed. The plan
# block passes every window through regardless.
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}

_SCHEDULED_SOURCES = frozenset({"cron", "system"})

# API price ratios -- input 1, cache write 1.25, cache read 0.1, output 5 --
# used purely as a RELATIVE proxy so that cache reads (cheap, and the bulk of
# every turn's tokens) don't drown out the work that actually costs. This is
# not a cost figure and not Marlow's share of the plan: the plan's own
# accounting is not public, which is why the design claims no percentage.
_W_INPUT = 1.0
_W_CACHE_CREATION = 1.25
_W_CACHE_READ = 0.1
_W_OUTPUT = 5.0


def _bucket_for(source: object) -> str | None:
    if source in active_turn.ATTENDED_SOURCES:
        return "chat"
    if source in _SCHEDULED_SOURCES:
        return "scheduled"
    if source == COMPACTION_SOURCE:
        return "compaction"
    return None  # unknown sources are counted nowhere rather than guessed at


def _count(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _weighted(record: dict) -> float:
    return (
        _count(record.get("input_tokens")) * _W_INPUT
        + _count(record.get("cache_creation_input_tokens")) * _W_CACHE_CREATION
        + _count(record.get("cache_read_input_tokens")) * _W_CACHE_READ
        + _count(record.get("output_tokens")) * _W_OUTPUT
    )


def _read_records() -> list[tuple[float, dict]] | None:
    """`(epoch, record)` per usable line, oldest first as written; None when
    the file can't be read at all."""
    try:
        text = settings.telemetry_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    records: list[tuple[float, dict]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            at = datetime.strptime(record["ts"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except (ValueError, TypeError, KeyError):
            continue
        if isinstance(record, dict):
            records.append((at, record))
    return records


def _window_start(name: str, plan_window: dict | None, now: float) -> float:
    length = WINDOW_SECONDS[name]
    resets_at = plan_window.get("resets_at") if plan_window else None
    # A reading whose window has already ended describes a dead window; the
    # live one's start is unknown, so fall back to the rolling span.
    if isinstance(resets_at, (int, float)) and resets_at > now:
        return resets_at - length
    return now - length


def _marlow(
    records: list[tuple[float, dict]], plan_windows: dict, now: float
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in WINDOW_SECONDS:
        start = _window_start(name, plan_windows.get(name), now)
        buckets = {b: {"turns": 0, "weighted_tokens": 0.0} for b in ("chat", "scheduled", "compaction")}
        for at, record in records:
            if not start <= at <= now:
                continue
            bucket = _bucket_for(record.get("source"))
            if bucket is None:
                continue
            buckets[bucket]["turns"] += 1
            buckets[bucket]["weighted_tokens"] += _weighted(record)
        for counts in buckets.values():
            counts["weighted_tokens"] = int(round(counts["weighted_tokens"]))
        out[name] = buckets
    return out


def _subscription_type() -> str | None:
    # get_status() raises TypeError on some corrupt credentials shapes (see
    # queue.WorkQueue._reauth_needed); the plan's name must not take the
    # whole usage report down with it.
    try:
        value = auth_monitor.get_status().subscription_type
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def build(state: UsageState, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    records = _read_records()

    plan = None
    plan_windows: dict = {}
    if state.plan is not None:
        plan_windows = state.plan.windows
        plan = {
            "observed_at": state.plan.observed_at,
            "status": state.plan.status,
            "windows": state.plan.windows,
        }

    conversation = None
    if state.conversation is not None:
        sid = state.conversation.session_id
        started_at = None
        compactions = 0
        for at, record in records or []:
            if record.get("session_id") != sid:
                continue
            if started_at is None or at < started_at:
                started_at = at
            # A failed attempt compacted nothing (a stale session fails every
            # retry -- idle-compaction.md), so it isn't a compaction.
            if record.get("source") == COMPACTION_SOURCE and record.get("is_error") is False:
                compactions += 1
        conversation = {
            "session_id": sid,
            "context_tokens": state.conversation.context_tokens,
            "observed_at": state.conversation.observed_at,
            "compaction_threshold_tokens": settings.idle_compaction_threshold_tokens,
            "started_at": started_at,
            "compactions": compactions,
            # True once the nightly consolidate has retired this session: the
            # next message starts a fresh one, so these numbers describe
            # yesterday. Readings stay chat-only, so this is the only way the
            # sheet learns of a rollover before Mike speaks.
            "retired": session_state.is_retired(sid),
        }

    return {
        # The raw `subscriptionType` from the CLI's login ("pro", "max"), so the
        # phone can name the plan. Read through auth_monitor, which already
        # parses that file for /health -- no second reader of the credentials.
        "subscription_type": _subscription_type(),
        "plan": plan,
        "conversation": conversation,
        "marlow": None if records is None else _marlow(records, plan_windows, now),
    }

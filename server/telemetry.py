"""Per-invocation token/cache telemetry (docs/tasks/usage-telemetry.md).

Every cache decision in this project so far rests on a synthetic clean-shell
spike (`spikes/cache-cadence.sh`, gate1-results §8b). That pins the *mechanism*
but says nothing about Marlow's real day: how often turns actually land inside
the cache window, and whether a miss was the cache expiring or the identity
block changing underneath it. This records enough to tell those apart.

**Numbers and hashes only — never prompt or result text.** `server/log.py`'s
redaction is field-targeted at secrets, not a general content filter, and the
identity/memory content flowing through `invoke.py` is exactly the private
material the two-location deploy model exists to protect. The identity block is
recorded as a length and a short digest, never its content. This is a hard
constraint, asserted directly in `tests/test_telemetry.py`.

Written as JSON-per-line to its own file (default `~/.marlow/logs/usage.jsonl`)
rather than into `marlow-server.log`, because the point is to parse it later.
The reading half is deliberately not built — see the task doc's "resist building
phase 2 speculatively".
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler

from server.config import settings

_LOGGER_NAME = "marlow.telemetry"

# Monotonic timestamp at which the previous invocation *finished*. The gap that
# matters for cache expiry is idle time between calls — from the end of one
# invocation to the start of the next — not start-to-start, which would fold a
# long invocation's own duration into the gap and make a prompt follow-up look
# like an idle period.
_last_finished_monotonic: float | None = None


def _reset_for_tests() -> None:
    """Test-only. Production never needs to clear the inter-invocation clock."""
    global _last_finished_monotonic
    _last_finished_monotonic = None


_HANDLER_MARKER = "_marlow_telemetry_handler"


def _get_writer() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    target = os.path.abspath(str(settings.telemetry_path))
    # Look for *our* handler specifically, not "any handler at all" — anything
    # else attached to this logger (pytest's capture handler, an operator's
    # debugging handler) would otherwise make us skip installing the file
    # handler and silently write nothing.
    #
    # And check *where* it points, not just that it exists. Handlers are cached
    # for the life of the process; settings are frozen in production, but a
    # test that repoints telemetry_path without tearing the handler down would
    # otherwise keep writing into the previous test's tmp_path and then assert
    # against an empty file of its own — a silently vacuous pass on the
    # privacy assertion, of all things.
    for handler in list(logger.handlers):
        if not getattr(handler, _HANDLER_MARKER, False):
            continue
        if getattr(handler, "baseFilename", None) == target:
            return logger
        handler.close()
        logger.removeHandler(handler)

    # Not settings.ensure_dirs() — telemetry_path is independently configurable
    # and may point outside log_dir.
    settings.telemetry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger.setLevel(logging.INFO)
    # No stream handler and no propagation: these lines are machine-readable
    # records, not operator output, and propagating would duplicate them into
    # marlow-server.log — the exact mixing this file exists to avoid.
    logger.propagate = False

    handler = RotatingFileHandler(
        settings.telemetry_path, maxBytes=10_000_000, backupCount=5
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _HANDLER_MARKER, True)
    logger.addHandler(handler)
    return logger


def identity_fingerprint(identity_context: str) -> tuple[int, str]:
    """(character count, short digest) for an injected identity block.

    The digest is what distinguishes a cold-start cache miss from a
    self-inflicted one: if the hash changed between two turns, the block itself
    moved and invalidated its own cache prefix. Truncated to 12 hex chars —
    ample to spot a change, and never reversible to content.
    """
    digest = hashlib.sha256(identity_context.encode("utf-8")).hexdigest()[:12]
    return len(identity_context), digest


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def record_invocation(
    *,
    usage: dict | None,
    source: str | None,
    turn_id: str | None,
    session_id: str | None,
    resume: bool,
    identity_chars: int,
    identity_hash: str,
    started_monotonic: float,
    duration_seconds: float,
    is_error: bool,
    subtype: str | None = None,
    had_permission_denial: bool = False,
    queue_wait_seconds: float | None = None,
    num_turns: int | None = None,
    first_output_seconds: float | None = None,
    first_text_seconds: float | None = None,
    context_tokens: int | None = None,
) -> dict:
    """Emit one telemetry record. Returns it, for tests and callers that log it.

    Never raises: telemetry failing must not take down an invocation that
    otherwise succeeded. A broken log file is a lost measurement, not an outage.
    """
    global _last_finished_monotonic

    gap = None if _last_finished_monotonic is None else started_monotonic - _last_finished_monotonic
    _last_finished_monotonic = started_monotonic + duration_seconds

    usage = usage if isinstance(usage, dict) else {}

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": source,
        "turn_id": turn_id,
        "session_id": session_id,
        "resume": resume,
        # None on the first invocation after a restart — that's a real absence
        # of information, not a zero, and analysis must not treat it as one.
        "gap_seconds": None if gap is None else round(gap, 3),
        # None for direct callers that do not pass through WorkQueue (the
        # queue is the only place where Turn.created_at exists).
        "queue_wait_seconds": (
            None if queue_wait_seconds is None else round(queue_wait_seconds, 3)
        ),
        "duration_seconds": round(duration_seconds, 3),
        "identity_chars": identity_chars,
        "identity_hash": identity_hash,
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        # The TRUE conversation size after this turn: the last top-level model
        # call's prefix, from the stream (docs/tasks/usage-visibility.md §3).
        # The four counters above are SUMMED across the invocation's internal
        # tool loop, so they run 1-4x the real size; this is the number a
        # context-size curve should be drawn from. None when the stream
        # carried no usable assistant usage (plain-json turns, compaction,
        # failures) -- an absence, not a zero.
        "context_tokens": context_tokens,
        # NOT `usage["thinking_tokens"]` -- no such key exists, and reading it
        # logged None on 200 of 200 rows from the day this field shipped until
        # 2026-08-22. Probed against CLI 2.1.235 on the box: thinking lives one
        # level down, in `output_tokens_details`. Kept defensive because the
        # sibling counters above are read with .get() for the same reason
        # invoke.py's docstring gives -- never assume a usage key is present.
        "thinking_tokens": (usage.get("output_tokens_details") or {}).get(
            "thinking_tokens"
        ),
        # Agent-loop depth for this invocation, straight off the result
        # envelope (docs/tasks/usage-telemetry.md asked for it three times and
        # guessed it was free plumbing; it is). None when the CLI reported no
        # envelope at all — an absence, not a zero.
        "num_turns": num_turns,
        # How long Mike waits before anything happens, as opposed to
        # duration_seconds, which is how long until the *last* word. Added
        # 2026-09-25: reply text already streams, so the 16.9s median turn is
        # not what he feels, and nothing measured what he does. Both count
        # from spawn, so queue_wait_seconds adds on top. first_output is the
        # CLI's own startup floor (first stdout line of any kind);
        # first_text is the first reply-text delta reaching the server, not
        # the phone -- the app's poll interval comes after it. None when the
        # turn did not stream (cron, or an attended turn with no text
        # consumer) or wrote no text at all -- an absence, not a zero.
        "first_output_seconds": _round_or_none(first_output_seconds),
        "first_text_seconds": _round_or_none(first_text_seconds),
        "is_error": is_error,
        "subtype": subtype,
        "had_permission_denial": had_permission_denial,
    }

    if settings.telemetry_enabled:
        try:
            _get_writer().info(json.dumps(record, separators=(",", ":"), sort_keys=True))
        except Exception:  # noqa: BLE001 — see docstring: never break an invocation
            logging.getLogger(__name__).warning("failed to write usage telemetry", exc_info=True)

    return record

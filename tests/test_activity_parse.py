"""`parse_activity_line` — the pure half of turn-activity streaming.

Every fixture here is a **real envelope shape**, taken from three probe runs
against CLI 2.1.235 on the box (docs/tasks/turn-activity-streaming.md, "The
mechanism"). That provenance is the point: the two bugs this feature already
tripped over -- telemetry's `thinking_tokens` and idle-compaction's
pre-compaction read -- were both hand-written fixtures encoding what the code
assumed rather than what the CLI emits, and both passed while production
recorded nothing. Do not simplify these into tidier invented shapes.
"""
from __future__ import annotations

import json

from server.invoke import ActivityEvent, parse_activity_line


def _line(obj) -> str:
    return json.dumps(obj) + "\n"


def _assistant(*blocks) -> str:
    """The observed assistant envelope, trimmed to the keys we read."""
    return _line({
        "type": "assistant",
        "parent_tool_use_id": None,
        "request_id": "req_1",
        "session_id": "s1",
        "timestamp": "2026-08-22T20:00:00Z",
        "uuid": "u1",
        "message": {
            "id": "msg_1",
            "model": "claude-sonnet-5",
            "role": "assistant",
            "type": "message",
            "stop_reason": None,
            "content": list(blocks),
            "usage": {"input_tokens": 2, "output_tokens": 53},
        },
    })


# --- the three lines that carry activity -------------------------------------

def test_tool_use_yields_tool_start_with_the_name():
    ev = parse_activity_line(
        _assistant({"type": "tool_use", "id": "tu1", "name": "Read",
                    "input": {"file_path": "/home/marlow/secrets.md"}}),
        now=100.0,
    )
    assert ev == ActivityEvent(kind="tool_start", tool="Read", at=100.0)


def test_tool_result_yields_tool_end_without_a_name():
    line = _line({
        "type": "user",
        "parent_tool_use_id": None,
        "session_id": "s1",
        "timestamp": "2026-08-22T20:00:01Z",
        "tool_result_meta": {},
        "tool_use_result": {},
        "uuid": "u2",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "alpha"}
        ]},
    })
    assert parse_activity_line(line, now=101.0) == ActivityEvent(
        kind="tool_end", tool=None, at=101.0
    )


def test_text_block_yields_text():
    ev = parse_activity_line(_assistant({"type": "text", "text": "the answer"}), now=102.0)
    assert ev == ActivityEvent(kind="text", tool=None, at=102.0)


# --- the privacy line ---------------------------------------------------------

def test_tool_input_never_reaches_the_event():
    """The single most important assertion in this file.

    tool_use.input carries file paths, file contents, search queries and email
    subjects. The event has no field for it and must never grow one -- see
    ActivityEvent's docstring and the task doc's §1.2.
    """
    ev = parse_activity_line(
        _assistant({"type": "tool_use", "id": "tu1", "name": "WebSearch",
                    "input": {"query": "divorce lawyer", "path": "/home/marlow/x"}}),
        now=100.0,
    )
    assert ev is not None
    assert ev.tool == "WebSearch"
    blob = repr(ev)
    assert "divorce" not in blob and "/home/marlow" not in blob
    assert set(vars(ev)) == {"kind", "tool", "at"}


# --- lines that are not activity ---------------------------------------------

def test_rate_limit_event_is_not_activity():
    """The FIRST line of every observed turn, and absent from the task doc's
    original type list."""
    assert parse_activity_line(_line({
        "type": "rate_limit_event",
        "session_id": "s1",
        "uuid": "u0",
        "rate_limit_info": {"status": "allowed", "resetsAt": 1787456400,
                            "isUsingOverage": False},
    })) is None


def test_system_init_is_not_activity():
    assert parse_activity_line(_line({
        "type": "system", "subtype": "init", "session_id": "s1",
        "model": "claude-sonnet-5", "tools": ["Read", "Bash"], "cwd": "/x",
    })) is None


def test_result_envelope_is_not_activity():
    assert parse_activity_line(_line({
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": "s1", "duration_ms": 4084, "usage": {},
    })) is None


# --- it must never raise, on anything ----------------------------------------

def test_malformed_and_hostile_lines_return_none_rather_than_raising():
    for bad in [
        "", "\n", "not json at all", "{truncated",
        '{"type":"assistant","message":',          # a torn line mid-write
        "[]", "null", '"a string"', "123",         # valid JSON, wrong shape
        _line({"type": "assistant"}),              # no message
        _line({"type": "assistant", "message": None}),
        _line({"type": "assistant", "message": {"content": "not a list"}}),
        _line({"type": "assistant", "message": {"content": [None, 7, "x"]}}),
        _line({"type": "assistant", "message": {"content": [{}]}}),
        _line({"type": "brand_new_future_type", "message": {"content": []}}),
    ]:
        assert parse_activity_line(bad) is None, bad


def test_tool_use_without_a_name_still_reports_a_tool_start():
    """Degrade to the generic label, don't drop the event -- the UI's fallback
    is "Using a tool…" and that is still better than silence."""
    ev = parse_activity_line(_assistant({"type": "tool_use", "id": "t"}), now=1.0)
    assert ev == ActivityEvent(kind="tool_start", tool=None, at=1.0)


def test_non_string_tool_name_is_dropped_not_forwarded():
    ev = parse_activity_line(
        _assistant({"type": "tool_use", "id": "t", "name": {"evil": "dict"}}), now=1.0
    )
    assert ev == ActivityEvent(kind="tool_start", tool=None, at=1.0)


# --- one event per line, and which one wins ----------------------------------

def test_tool_use_beats_text_in_a_mixed_message():
    """Turn.activity is one slot feeding one status line. When a message mixes
    narration and a tool call, the tool is the informative half and the leg
    that takes the time."""
    ev = parse_activity_line(
        _assistant({"type": "text", "text": "Let me check."},
                   {"type": "tool_use", "id": "t", "name": "Bash"}),
        now=5.0,
    )
    assert ev == ActivityEvent(kind="tool_start", tool="Bash", at=5.0)


def test_first_tool_wins_when_a_message_carries_two():
    ev = parse_activity_line(
        _assistant({"type": "tool_use", "id": "a", "name": "Read"},
                   {"type": "tool_use", "id": "b", "name": "Grep"}),
        now=5.0,
    )
    assert ev is not None and ev.tool == "Read"


def test_thinking_block_maps_to_thinking():
    """NOTE: never observed in three probe runs, including one prompted to
    think before using a tool (task doc, "thinking blocks have never been
    observed"). Parsed because the kind is in the contract and costs nothing.
    This asserts the mapping only -- do not read it as evidence the CLI emits
    these, and do not wire UI to it on the strength of this test."""
    ev = parse_activity_line(
        _assistant({"type": "thinking", "thinking": "hmm", "signature": "s"}), now=7.0
    )
    assert ev == ActivityEvent(kind="thinking", tool=None, at=7.0)


def test_events_are_frozen_so_a_stored_one_cannot_be_mutated():
    import dataclasses
    import pytest
    ev = parse_activity_line(_assistant({"type": "text", "text": "x"}), now=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.tool = "Read"  # type: ignore[misc]

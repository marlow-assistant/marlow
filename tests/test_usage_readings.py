"""Plan-usage and true-conversation-size capture from the stream
(docs/tasks/usage-visibility.md, server half).

The rate_limit_event line shape is the one observed on the box (CLI 2.1.284,
2026-10-04). The `assistant` line shape (top-level `parent_tool_use_id`,
`message.usage`) is the CLI's stream format; no fixture elsewhere in the repo
carries usage on an assistant line, so these are the first to pin it.
"""
import asyncio
import dataclasses
import json
import textwrap

import pytest

from conftest import write_executable
from server.config import settings as real_settings
from server.idle_compaction import IdleCompactionWatcher, _sum_prefix_tokens
from server.invoke import (
    InvocationResult,
    PlanUsage,
    parse_stream_model,
    parse_stream_readings,
    run_claude,
)
from server.queue import TurnStatus, WorkQueue

RATE_LIMIT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1791169200,
        "rateLimitType": "five_hour",
        "overageStatus": "allowed",
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.14, "resetsAt": 1791169200},
            "seven_day": {"utilization": 0.08, "resetsAt": 1791630000},
        },
    },
    "uuid": "u1",
    "session_id": "s1",
}
INIT = {"type": "system", "subtype": "init"}
RESULT = {"type": "result", "is_error": False, "subtype": "success", "result": "ok", "session_id": "s1"}


def _assistant(input_tokens=0, cache_read=0, cache_creation=0, *, parent=None, **extra):
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "x"}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "output_tokens": 9,
            },
        },
        "parent_tool_use_id": parent,
        **extra,
    }


def _stream(*events) -> str:
    return "\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n"


# --- rate_limit_event ----------------------------------------------------------


def test_a_well_formed_event_becomes_a_plan_reading():
    plan, _ = parse_stream_readings(_stream(INIT, RATE_LIMIT, RESULT), observed_at=1000.0)
    assert plan == PlanUsage(
        observed_at=1000.0,
        status="allowed",
        windows={
            "five_hour": {"utilization": 0.14, "resets_at": 1791169200},
            "seven_day": {"utilization": 0.08, "resets_at": 1791630000},
        },
    )


def test_the_event_is_found_wherever_it_sits_in_the_stream():
    # First on CLI 2.1.235, after system/init on 2.1.284, and the position is
    # not part of the contract.
    for events in (
        (RATE_LIMIT, INIT, RESULT),
        (INIT, RATE_LIMIT, RESULT),
        (INIT, _assistant(5), RATE_LIMIT, RESULT),
        (INIT, RESULT, RATE_LIMIT),
    ):
        plan, _ = parse_stream_readings(_stream(*events), observed_at=1.0)
        assert plan is not None and plan.windows["five_hour"]["utilization"] == 0.14


def test_unknown_window_names_pass_through():
    event = json.loads(json.dumps(RATE_LIMIT))
    event["rate_limit_info"]["unifiedWindows"]["seven_day_opus"] = {
        "utilization": 0.5, "resetsAt": 1791700000.0,
    }
    plan, _ = parse_stream_readings(_stream(event), observed_at=1.0)
    assert plan.windows["seven_day_opus"] == {"utilization": 0.5, "resets_at": 1791700000}


def test_a_non_allowed_status_is_passed_through():
    event = json.loads(json.dumps(RATE_LIMIT))
    event["rate_limit_info"]["status"] = "allowed_warning"
    plan, _ = parse_stream_readings(_stream(event), observed_at=1.0)
    assert plan.status == "allowed_warning"


def test_the_last_valid_event_wins():
    later = json.loads(json.dumps(RATE_LIMIT))
    later["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] = 0.2
    plan, _ = parse_stream_readings(_stream(RATE_LIMIT, later, "garbage"), observed_at=1.0)
    assert plan.windows["five_hour"]["utilization"] == 0.2


def _event_with(info) -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": info}


@pytest.mark.parametrize(
    "event",
    [
        {"type": "rate_limit_event"},
        _event_with(None),
        _event_with("allowed"),
        _event_with({"status": "allowed"}),  # no unifiedWindows
        _event_with({"status": "allowed", "unifiedWindows": None}),
        _event_with({"status": "allowed", "unifiedWindows": []}),
        _event_with({"status": "allowed", "unifiedWindows": {}}),
        _event_with({"unifiedWindows": {"five_hour": "high"}}),
        _event_with({"unifiedWindows": {"five_hour": {"utilization": "0.1", "resetsAt": 1}}}),
        _event_with({"unifiedWindows": {"five_hour": {"utilization": 0.1}}}),  # no reset time
        _event_with({"unifiedWindows": {"five_hour": {"resetsAt": 5}}}),
        _event_with({"unifiedWindows": {"five_hour": {"utilization": True, "resetsAt": 5}}}),
        _event_with({"unifiedWindows": {"five_hour": {"utilization": float("nan"), "resetsAt": 5}}}),
    ],
)
def test_a_malformed_event_is_no_reading_and_never_an_error(event):
    assert parse_stream_readings(_stream(event, RESULT), observed_at=1.0) == (None, None)


def test_one_bad_window_does_not_cost_the_good_one():
    event = json.loads(json.dumps(RATE_LIMIT))
    event["rate_limit_info"]["unifiedWindows"]["seven_day"] = {"utilization": None}
    plan, _ = parse_stream_readings(_stream(event), observed_at=1.0)
    assert list(plan.windows) == ["five_hour"]


@pytest.mark.parametrize("stdout", ["", "\n\n", "not json\n{truncated", "[1,2]\nnull\n42\n", _stream(INIT, RESULT)])
def test_no_event_or_junk_is_none(stdout):
    assert parse_stream_readings(stdout, observed_at=1.0) == (None, None)


def test_a_non_string_status_is_dropped_not_fatal():
    event = json.loads(json.dumps(RATE_LIMIT))
    event["rate_limit_info"]["status"] = 7
    plan, _ = parse_stream_readings(_stream(event), observed_at=1.0)
    assert plan.status is None


# --- context_tokens --------------------------------------------------------------


def test_the_last_top_level_assistant_message_wins():
    stdout = _stream(
        INIT,
        _assistant(input_tokens=3, cache_read=1000, cache_creation=50),
        {"type": "user", "message": {"content": []}},
        _assistant(input_tokens=4, cache_read=2000, cache_creation=100),
        RESULT,
    )
    assert parse_stream_readings(stdout, observed_at=1.0)[1] == 4 + 2000 + 100


def test_subagent_messages_are_excluded():
    stdout = _stream(
        _assistant(input_tokens=1, cache_read=500),
        _assistant(input_tokens=1, cache_read=90_000, parent="toolu_abc"),
        _assistant(input_tokens=1, cache_read=80_000, isSidechain=True),
        RESULT,
    )
    assert parse_stream_readings(stdout, observed_at=1.0)[1] == 501


def test_no_assistant_message_is_none():
    assert parse_stream_readings(_stream(RATE_LIMIT, RESULT), observed_at=1.0)[1] is None


def test_a_zero_or_missing_usage_does_not_replace_a_real_reading():
    no_usage = {"type": "assistant", "message": {"content": []}, "parent_tool_use_id": None}
    stdout = _stream(_assistant(2, 700), _assistant(0, 0, 0), no_usage, RESULT)
    assert parse_stream_readings(stdout, observed_at=1.0)[1] == 702


def test_a_repeated_message_with_partial_messages_is_harmless():
    stdout = _stream(_assistant(2, 700), _assistant(2, 700), RESULT)
    assert parse_stream_readings(stdout, observed_at=1.0)[1] == 702


# --- which model answered ----------------------------------------------------


def _answered_by(model, **extra):
    event = _assistant(1, 100)
    event["message"]["model"] = model
    event.update(extra)
    return event


def test_the_last_top_level_model_is_the_one_that_answered():
    stdout = _stream(INIT, _answered_by("claude-sonnet-5-5"), _answered_by("claude-opus-5-5"), RESULT)
    assert parse_stream_model(stdout) == "claude-opus-5-5"


def test_a_subagent_model_is_not_the_turn_model():
    stdout = _stream(
        _answered_by("claude-sonnet-5-5"),
        _answered_by("claude-haiku-5-5", parent_tool_use_id="toolu_abc"),
        _answered_by("claude-haiku-5-5", isSidechain=True),
        RESULT,
    )
    assert parse_stream_model(stdout) == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    "bad",
    ["<synthetic>", "", "Claude Sonnet", "claude-sonnet-5-5\nInjected line", "x" * 65, 5, None, ["claude"]],
)
def test_anything_not_shaped_like_a_model_id_is_dropped(bad):
    stdout = _stream(_answered_by("claude-sonnet-5-5"), _answered_by(bad), RESULT)
    assert parse_stream_model(stdout) == "claude-sonnet-5-5"


@pytest.mark.parametrize("stdout", ["", "garbage\n", _stream(INIT, RESULT), _stream(_assistant(1, 100))])
def test_no_model_is_none(stdout):
    assert parse_stream_model(stdout) is None


# --- through run_claude --------------------------------------------------------


def _fake_cli(tmp_path, monkeypatch, events, *, exit_code=0):
    body = f"""\
        #!/usr/bin/env python3
        import json, sys
        if "stream-json" in sys.argv[1:]:
            for e in {events!r}:
                print(json.dumps(e), flush=True)
        else:
            print(json.dumps({RESULT!r}))
        sys.exit({exit_code})
    """
    path = tmp_path / "claude"
    write_executable(path, textwrap.dedent(body))
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(path))
    )


def test_run_claude_surfaces_both_readings_on_a_streamed_turn(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, [INIT, RATE_LIMIT, _assistant(3, 1000, 20), RESULT])
    result = asyncio.run(run_claude("hi", source="cron", on_tool_call=lambda c: None))
    assert result.context_tokens == 1023
    assert result.plan_usage.windows["five_hour"]["utilization"] == 0.14
    assert result.plan_usage.observed_at > 0


def test_run_claude_reports_the_answering_model_and_the_effort_it_passed(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, [INIT, _answered_by("claude-sonnet-5-5"), RESULT])
    result = asyncio.run(run_claude("hi", source="cron", effort="high", on_tool_call=lambda c: None))
    assert result.model == "claude-sonnet-5-5"
    assert result.effort == "high"


def test_a_failed_exit_still_reports_model_and_effort(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, [INIT, _answered_by("claude-sonnet-5-5"), RESULT], exit_code=1)
    result = asyncio.run(run_claude("hi", source="cron", on_tool_call=lambda c: None))
    assert result.is_error
    assert (result.model, result.effort) == ("claude-sonnet-5-5", "medium")


def test_a_plain_json_turn_has_no_readings(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, [INIT, RATE_LIMIT, _assistant(3, 1000, 20), RESULT])
    result = asyncio.run(run_claude("hi", source="cron"))
    assert result.context_tokens is None and result.plan_usage is None
    assert result.model is None
    assert result.effort == "medium", "the flag was passed either way"


def test_a_failed_exit_still_carries_the_reading(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, [RATE_LIMIT, RESULT], exit_code=1)
    result = asyncio.run(run_claude("hi", source="cron", on_tool_call=lambda c: None))
    assert result.is_error
    assert result.plan_usage is not None


# --- the compaction gate runs on the true size ----------------------------------


def _gate_reading_after_a_chat_turn(monkeypatch, *, usage, context_tokens):
    """Run one chat turn through a real queue and return what the compaction
    gate recorded as the session's size."""
    # A fresh watcher swapped in for the queue's, so the singleton the rest of
    # the suite (and the app) uses is never touched.
    watcher = IdleCompactionWatcher()
    monkeypatch.setattr("server.queue.idle_compaction_watcher", watcher)

    async def _fake_run_claude(prompt, **kwargs):
        return InvocationResult(
            exit_code=0, is_error=False, subtype="success", result_text="ok",
            session_id="s-gate", usage=usage, context_tokens=context_tokens,
        )

    monkeypatch.setattr("server.queue.run_claude", _fake_run_claude)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        deadline = asyncio.get_event_loop().time() + 5
        while wq.get_turn(turn_id).status not in (TurnStatus.DONE, TurnStatus.ERROR):
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0.02)
        # The attended bookkeeping runs after the status is stamped.
        while watcher._last_attended_session_id != "s-gate":
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0.02)
        await wq.stop()

    asyncio.run(_body())
    return watcher._last_attended_prefix_tokens


_SUMMED = {"input_tokens": 10, "cache_read_input_tokens": 300_000, "cache_creation_input_tokens": 0}


def test_the_compaction_gate_runs_on_the_true_size(monkeypatch):
    """2026-10-05: the summed counters read a 38K session at ~190K and it was
    compacted for nothing (idle_compaction._prefix_reading)."""
    assert _gate_reading_after_a_chat_turn(monkeypatch, usage=_SUMMED, context_tokens=77_777) == 77_777


def test_without_a_true_size_the_gate_falls_back_to_the_summed_counters(monkeypatch):
    # A stream-format change must degrade to over-compacting, never to never compacting.
    reading = _gate_reading_after_a_chat_turn(monkeypatch, usage=_SUMMED, context_tokens=None)
    assert reading == _sum_prefix_tokens(_SUMMED) == 300_010

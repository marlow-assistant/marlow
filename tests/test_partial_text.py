"""Streaming the reply text itself (docs/tasks/streaming-reply-text.md).

Three layers, because they fail differently:

- `PartialTextTracker` — pure, and the place the privacy rule lives. A delta on
  a `tool_use` block's index is tool *input* being serialised, and must never
  reach the phone as reply text.
- the invocation — that `--include-partial-messages` is paid for only when
  someone is consuming it, and that adding it did not disturb the invariant
  everything downstream rests on: the last stream-json line is still the result
  envelope.
- reassembly — that a turn which wrote, called a tool, then wrote again is
  delivered whole rather than as its last paragraph, and that the envelope
  still wins wherever that cannot be proved (`_whole_reply`).
- the queue — that the accumulated text is bounded, cleared with the status
  line, and that a coalesced turn never inherits the previous reply's opening.
"""
import asyncio
import dataclasses
import json
import textwrap

import pytest

from conftest import write_executable
from server.config import settings as real_settings
from server.invoke import (
    ActivityEvent,
    PartialTextTracker,
    _final_envelope,
    _whole_reply,
    run_claude,
)
from server.queue import (
    MAX_PARTIAL_TEXT_CHARS,
    MAX_TURN_ACTIVITIES,
    Turn,
    TurnStatus,
    WorkQueue,
    _clear_progress,
    _record_activity,
    _record_text,
)


def _evt(inner: dict, *, parent: str | None = None) -> str:
    return json.dumps(
        {
            "type": "stream_event",
            "event": inner,
            "session_id": "s1",
            "parent_tool_use_id": parent,
            "uuid": "u1",
        }
    )


def _start(index: int, block_type: str = "text") -> str:
    return _evt({"type": "content_block_start", "index": index,
                 "content_block": {"type": block_type}})


def _delta(index: int, text: str, delta_type: str = "text_delta") -> str:
    return _evt({"type": "content_block_delta", "index": index,
                 "delta": {"type": delta_type, "text": text}})


# --- the tracker --------------------------------------------------------------


def test_deltas_on_an_open_text_block_accumulate():
    t = PartialTextTracker()
    assert t.feed(_start(0)) is None
    assert "".join(x for x in (t.feed(_delta(0, "he")), t.feed(_delta(0, "llo"))) if x) == "hello"


def test_a_delta_with_no_opening_block_is_dropped():
    """Fail closed. Without a `content_block_start` there is nothing saying this
    index is text rather than a tool's serialised input."""
    assert PartialTextTracker().feed(_delta(0, "hello")) is None


def test_text_on_a_tool_block_never_becomes_reply_text():
    """The privacy rule, not a nicety: a delta arriving on a tool_use block is
    the tool's INPUT — file paths, file contents, search queries, email
    subjects — which is exactly what ActivityEvent's name-only rule keeps out
    of what the phone is shown."""
    t = PartialTextTracker()
    t.feed(_start(0, "tool_use"))
    assert t.feed(_delta(0, "/etc/passwd")) is None
    assert t.feed(_delta(0, "{}", "input_json_delta")) is None


def test_a_tool_block_reusing_a_text_index_closes_it():
    """Indexes are reused across blocks, so opening a tool_use on an index must
    retire whatever text block used to live there."""
    t = PartialTextTracker()
    t.feed(_start(1))
    assert t.feed(_delta(1, "visible")) == "visible"
    t.feed(_start(1, "tool_use"))
    assert t.feed(_delta(1, "/etc/passwd")) is None


def test_content_block_stop_closes_the_block():
    t = PartialTextTracker()
    t.feed(_start(0))
    assert t.feed(_delta(0, "a")) == "a"
    t.feed(_evt({"type": "content_block_stop", "index": 0}))
    assert t.feed(_delta(0, "b")) is None


def test_thinking_deltas_are_not_reply_text():
    """Thinking is narrated by the status line, not written into the bubble."""
    t = PartialTextTracker()
    t.feed(_start(0))
    assert t.feed(_delta(0, "hmm", "thinking_delta")) is None


def test_sub_agent_text_is_dropped():
    """A non-null parent_tool_use_id means a Task tool's own inner turn. That
    text is not in `result` either; fusing it into the reply would show the
    user a conversation they did not have."""
    t = PartialTextTracker()
    t.feed(_evt({"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text"}}, parent="tool-1"))
    assert t.feed(_evt({"type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": "inner"}},
                       parent="tool-1")) is None


@pytest.mark.parametrize("line", [
    "", "not json", "[]", "null", '{"type":"assistant","message":{"content":[]}}',
    '{"type":"stream_event"}',
    '{"type":"stream_event","event":{"type":"content_block_delta"}}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":"0",'
    '"delta":{"type":"text_delta","text":"x"}}}',
    '{"type":"stream_event","event":{"type":"content_block_start","index":0}}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":null}}',
    '{"type":"result","is_error":false}',
])
def test_hostile_and_unfamiliar_lines_return_none_rather_than_raising(line):
    """Same contract as parse_activity_line: this runs inside the stdout loop,
    where an exception would kill a turn that is otherwise succeeding."""
    t = PartialTextTracker()
    t.feed(_start(0))
    assert t.feed(line) is None


# --- the invocation -----------------------------------------------------------

_PARTIAL_CLI = '''
    import json, sys, time
    argv = sys.argv[1:]

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    envelope = {
        "type": "result",
        "is_error": False,
        "subtype": "success",
        "result": "\\x00".join(argv),
        "session_id": "stream-session",
        "permission_denials": [],
        "num_turns": 3,
    }

    if "--include-partial-messages" in argv:
        emit({"type": "stream_event", "parent_tool_use_id": None,
              "event": {"type": "content_block_start", "index": 0,
                        "content_block": {"type": "text"}}})
        for piece in ["the ", "answer"]:
            emit({"type": "stream_event", "parent_tool_use_id": None,
                  "event": {"type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": piece}}})
            time.sleep(0.05)
    if "stream-json" in argv:
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "the answer"}]}})
        emit(envelope)
    else:
        print(json.dumps(envelope))
'''


def _cli(tmp_path, monkeypatch, body=_PARTIAL_CLI):
    path = tmp_path / "claude"
    write_executable(path, "#!/usr/bin/env python3\n" + textwrap.dedent(body))
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(path))
    )


def _run(**kwargs):
    return asyncio.run(run_claude("hello", **kwargs))


def test_the_flag_is_only_paid_for_when_someone_consumes_it(tmp_path, monkeypatch):
    """It multiplies stdout volume by the number of deltas. An attended turn
    that only wants the status line should not pay that."""
    _cli(tmp_path, monkeypatch)
    result = _run(source="chat", on_activity=lambda e: None)
    assert "--include-partial-messages" not in result.result_text.split("\x00")


@pytest.mark.parametrize("source", ["cron", "compaction", None, "future-thing"])
def test_unattended_sources_never_get_the_flag(tmp_path, monkeypatch, source):
    """Same fail-closed gate as streaming itself: nobody is watching."""
    _cli(tmp_path, monkeypatch)
    result = _run(source=source, on_activity=lambda e: None, on_text=lambda t: None)
    argv = result.result_text.split("\x00")
    assert "--include-partial-messages" not in argv
    assert argv[argv.index("--output-format") + 1] == "json"


def test_an_attended_turn_with_a_text_callback_streams_text(tmp_path, monkeypatch):
    _cli(tmp_path, monkeypatch)
    chunks: list[str] = []
    result = _run(source="chat", on_activity=lambda e: None, on_text=chunks.append)
    argv = result.result_text.split("\x00")
    assert "--include-partial-messages" in argv
    # Ahead of the `--` separator, like every other flag: past it, it would be
    # prompt text rather than a flag.
    assert argv.index("--include-partial-messages") < argv.index("--")
    assert "".join(chunks) == "the answer"


def test_the_last_line_is_still_the_result_envelope(tmp_path, monkeypatch):
    """Load-bearing, and exactly the kind of thing a CLI upgrade breaks
    silently: everything downstream of _invoke_once reads the final line as the
    same payload `--output-format json` returns as its whole body."""
    _cli(tmp_path, monkeypatch)
    result = _run(source="chat", on_activity=lambda e: None, on_text=lambda t: None)
    lines = [line for line in result.raw_stdout.splitlines() if line.strip()]
    assert json.loads(lines[-1])["type"] == "result"
    assert _final_envelope(result.raw_stdout) == json.loads(lines[-1])
    assert result.session_id == "stream-session"
    assert result.is_error is False


def test_a_raising_text_callback_cannot_fail_the_turn(tmp_path, monkeypatch):
    """A half-rendered reply is a nicety; the turn under it is the product."""
    _cli(tmp_path, monkeypatch)

    def _boom(_chunk):
        raise RuntimeError("client blew up")

    result = _run(source="chat", on_activity=lambda e: None, on_text=_boom)
    assert result.is_error is False
    assert result.session_id == "stream-session"


def test_num_turns_reaches_telemetry(tmp_path, monkeypatch):
    """Free win found while probing: loop depth was in the envelope all along
    (docs/tasks/usage-telemetry.md)."""
    _cli(tmp_path, monkeypatch)
    records: list[dict] = []
    monkeypatch.setattr("server.invoke.record_invocation",
                        lambda **kw: records.append(kw) or kw)
    _run(source="chat")
    assert records[-1]["num_turns"] == 3


# --- reassembly ---------------------------------------------------------------
#
# The bug this section exists for: the CLI's `result` is the LAST assistant
# message's text, so an interleaved turn's opening is not in it. Observed on the
# box 2026-09-04 — 751 characters of answer, an Edit, then "Updated. Enjoy
# Twillingate.", and only the last 27 survived to the phone.


def _stop(index: int) -> str:
    return _evt({"type": "content_block_stop", "index": index})


def _interleaved() -> PartialTextTracker:
    """text -> tool -> text, all on index 0, the way the CLI reuses indexes."""
    t = PartialTextTracker()
    t.feed(_start(0))
    t.feed(_delta(0, "That works — it fixes the problem I flagged."))
    t.feed(_stop(0))
    t.feed(_start(0, "tool_use"))
    t.feed(_delta(0, "/home/marlow/trip.md", "input_json_delta"))
    t.feed(_stop(0))
    t.feed(_start(0))
    t.feed(_delta(0, "Updated. Enjoy Twillingate."))
    t.feed(_stop(0))
    return t


def test_the_tracker_keeps_every_text_block_in_order():
    assert _interleaved().blocks() == [
        "That works — it fixes the problem I flagged.",
        "Updated. Enjoy Twillingate.",
    ]


def test_a_block_still_open_when_the_stream_ends_is_not_lost():
    """A killed or truncated stream must not silently drop the paragraph that
    was being written."""
    t = PartialTextTracker()
    t.feed(_start(0))
    t.feed(_delta(0, "half a th"))
    assert t.blocks() == ["half a th"]


def test_tool_input_is_not_a_block():
    """The privacy rule again, at the other end: `blocks()` is rendered into the
    bubble, so a serialised file path must not reach it either."""
    assert "/home/marlow/trip.md" not in "".join(_interleaved().blocks())


def test_the_head_is_restored_and_the_blocks_do_not_fuse():
    """Bare concatenation is the "I'll check your calendar.You have three
    meetings" garble; the final render has no tool chip to place, so the join
    is a paragraph break."""
    assert _whole_reply("Updated. Enjoy Twillingate.", _interleaved()) == (
        "That works — it fixes the problem I flagged.\n\nUpdated. Enjoy Twillingate."
    )


def test_a_single_block_turn_is_returned_untouched():
    """89% of turns. The envelope already is the whole reply."""
    t = PartialTextTracker()
    t.feed(_start(0))
    t.feed(_delta(0, "the answer"))
    assert _whole_reply("the answer", t) == "the answer"


def test_the_envelope_wins_when_the_last_block_does_not_match_it():
    """The fail-closed guard, and the reason this survives a CLI change: if
    `result` ever stops meaning "the final message", the equality stops holding
    and the envelope is returned unchanged rather than something invented."""
    assert _whole_reply("something else entirely", _interleaved()) == "something else entirely"


def test_an_unstreamed_turn_is_returned_untouched():
    """Cron: no tracker, nothing to reassemble from."""
    assert _whole_reply("cron reply", None) == "cron reply"
    assert _whole_reply(None, _interleaved()) is None


_INTERLEAVED_CLI = '''
    import json, sys

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    streaming = "stream-json" in sys.argv[1:]

    def stream(inner):
        if streaming:
            emit({"type": "stream_event", "parent_tool_use_id": None, "event": inner})

    def message(obj):
        if streaming:
            emit(obj)

    stream({"type": "content_block_start", "index": 0,
            "content_block": {"type": "text"}})
    stream({"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "The long half."}})
    stream({"type": "content_block_stop", "index": 0})
    message({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit"}]}})
    stream({"type": "content_block_start", "index": 0,
            "content_block": {"type": "text"}})
    stream({"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "Updated."}})
    stream({"type": "content_block_stop", "index": 0})
    emit({"type": "result", "is_error": False, "subtype": "success",
          "result": "Updated.", "session_id": "stream-session",
          "permission_denials": []})

'''


def test_an_interleaved_turn_reaches_the_client_whole(tmp_path, monkeypatch):
    """End to end, through the real stdout loop: what the queue stores as the
    turn's reply is both halves, not the envelope's last paragraph."""
    _cli(tmp_path, monkeypatch, body=_INTERLEAVED_CLI)
    chunks: list[str] = []
    result = _run(source="chat", on_activity=lambda e: None, on_text=chunks.append)
    assert "".join(chunks) == "The long half.Updated."
    assert result.result_text == "The long half.\n\nUpdated."


def test_the_same_turn_unwatched_still_gets_the_envelope(tmp_path, monkeypatch):
    """Unattended turns keep the envelope. Not a live loss: cron shares the
    mechanism but not the shape — 0 of 75 cron turns on the box produced more
    than one text block, against 33 of 331 from the phone (the table in
    docs/tasks/streaming-reply-text.md). Pinned so that if a cron turn ever
    does interleave, changing this is a decision and not a surprise."""
    _cli(tmp_path, monkeypatch, body=_INTERLEAVED_CLI)
    result = _run(source="cron", on_activity=lambda e: None, on_text=lambda t: None)
    assert result.result_text == "Updated."


# --- the queue ----------------------------------------------------------------


def _turn() -> Turn:
    return Turn(turn_id="t1", prompt="x", session_id=None, resume=False,
                source="chat", client_msg_id=None)


def test_text_accumulates_as_a_growing_string():
    turn = _turn()
    record = _record_text(turn)
    record("hel")
    record("lo")
    assert turn.partial_text == "hello"


def test_partial_text_is_capped_dropping_the_tail():
    """The opposite choice from `activities`, and deliberately so: dropping the
    oldest here would silently delete the BEGINNING of the answer."""
    turn = _turn()
    record = _record_text(turn)
    for _ in range(20):
        record("x" * 10_000)
    assert len(turn.partial_text) == MAX_PARTIAL_TEXT_CHARS
    assert turn.partial_text.startswith("x")


def test_a_tool_call_marks_its_offset_in_the_reply():
    """The interleaved 11%: without the offset the client renders "I'll check
    your calendar.You have three meetings" — two thoughts fused into one
    sentence."""
    turn = _turn()
    text, activity = _record_text(turn), _record_activity(turn)
    text("I'll check your calendar.")
    activity(ActivityEvent(kind="tool_start", tool="Calendar", at=1.0))
    text("You have three meetings.")
    assert turn.partial_text_marks == [len("I'll check your calendar.")]


def test_marks_do_not_grow_without_bound():
    turn = _turn()
    text, activity = _record_text(turn), _record_activity(turn)
    for i in range(MAX_TURN_ACTIVITIES + 10):
        text("x")
        activity(ActivityEvent(kind="tool_start", tool=f"T{i}", at=float(i)))
    assert len(turn.partial_text_marks) == MAX_TURN_ACTIVITIES


def test_back_to_back_tool_calls_mark_one_offset_once():
    turn = _turn()
    activity = _record_activity(turn)
    for i in range(3):
        activity(ActivityEvent(kind="tool_start", tool=f"T{i}", at=float(i)))
    assert turn.partial_text_marks == [0]


def test_clearing_progress_takes_the_text_with_the_status_line():
    turn = _turn()
    _record_text(turn)("half an answer")
    _record_activity(turn)(ActivityEvent(kind="tool_start", tool="Read", at=1.0))
    _clear_progress(turn)
    assert (turn.activities, turn.partial_text, turn.partial_text_marks) == ([], "", [])


def test_a_finished_turn_carries_no_partial_text(tmp_path, monkeypatch):
    """`result_text` is authoritative from the moment the turn is terminal, and
    a partial left behind is a client's chance to render the approximation
    instead."""
    _cli(tmp_path, monkeypatch)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            turn = wq.get_turn(turn_id)
            if turn.status in (TurnStatus.DONE, TurnStatus.ERROR):
                break
            await asyncio.sleep(0.02)
        await wq.stop()
        return wq.get_turn(turn_id)

    turn = asyncio.run(_body())
    assert turn.status == TurnStatus.DONE
    assert turn.partial_text == ""
    assert turn.partial_text_marks == []
    assert turn.result.result_text is not None


_SLOW_TAIL_CLI = _PARTIAL_CLI.replace(
    '''    if "stream-json" in argv:''',
    '''    time.sleep(0.5)
    if "stream-json" in argv:''',
)


def test_the_text_is_readable_while_the_turn_is_still_running(tmp_path, monkeypatch):
    """The whole feature, in one assertion. Measured on the box: 3.2s to first
    text against 13.1s for the reply in one lump at the end. A server that only
    exposed the text once the envelope landed would pass every other test here
    and deliver exactly the silence this removes."""
    _cli(tmp_path, monkeypatch, _SLOW_TAIL_CLI)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        deadline = asyncio.get_event_loop().time() + 5.0
        seen = ""
        while asyncio.get_event_loop().time() < deadline:
            turn = wq.get_turn(turn_id)
            if turn.status in (TurnStatus.DONE, TurnStatus.ERROR):
                break
            if turn.partial_text:
                seen = turn.partial_text
                break
            await asyncio.sleep(0.02)
        while wq.get_turn(turn_id).status not in (TurnStatus.DONE, TurnStatus.ERROR):
            await asyncio.sleep(0.02)
        await wq.stop()
        return seen

    seen = asyncio.run(_body())
    # A PREFIX, not the whole reply: this test routinely catches the stream
    # mid-flight ("the " of "the answer"), which is the property being
    # asserted. Requiring the full string would only pass by racing to the end.
    assert seen and "the answer".startswith(seen)


def test_the_serialized_turn_carries_the_text_and_its_marks():
    from server.app import _serialize_turn

    turn = _turn()
    assert _serialize_turn(turn)["partial_text"] == ""
    _record_text(turn)("half an ")
    _record_activity(turn)(ActivityEvent(kind="tool_start", tool="Read", at=1.0))
    _record_text(turn)("answer")

    body = _serialize_turn(turn)
    assert body["partial_text"] == "half an answer"
    assert body["partial_text_marks"] == [8]
    # A copy, not the live list: the poll handler must not hand a client a
    # reference the read loop keeps mutating underneath it.
    assert body["partial_text_marks"] is not turn.partial_text_marks


# --- time to first word (telemetry, 2026-09-25) --------------------------------

# A gap either side of the text, so the three instants telemetry distinguishes
# -- CLI up, first word, last word -- are far enough apart to tell apart. A
# timing recorded at the wrong place (at spawn, or when the envelope lands)
# collapses two of them and fails the ordering below.
_TIMED_CLI = '''
    import json, sys, time
    argv = sys.argv[1:]

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    envelope = {"type": "result", "is_error": False, "subtype": "success",
                "result": "done", "session_id": "s", "permission_denials": []}

    if "stream-json" in argv:
        emit({"type": "system", "subtype": "init", "tools": []})
        time.sleep(0.3)
        if "--include-partial-messages" in argv:
            emit({"type": "stream_event", "parent_tool_use_id": None,
                  "event": {"type": "content_block_start", "index": 0,
                            "content_block": {"type": "text"}}})
            emit({"type": "stream_event", "parent_tool_use_id": None,
                  "event": {"type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": "done"}}})
        time.sleep(0.3)
        emit(envelope)
    else:
        print(json.dumps(envelope))
'''


@pytest.fixture
def recorded(monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr("server.invoke.record_invocation", lambda **kw: records.append(kw))
    return records


def test_first_word_is_timed_between_startup_and_the_last_word(tmp_path, monkeypatch, recorded):
    _cli(tmp_path, monkeypatch, body=_TIMED_CLI)
    _run(source="chat", on_activity=lambda e: None, on_text=lambda t: None)

    (rec,) = recorded
    first_output, first_text, total = (
        rec["first_output_seconds"], rec["first_text_seconds"], rec["duration_seconds"]
    )
    # Relative, not absolute: interpreter startup under a loaded full-suite
    # run pushed first_output past a fixed 0.3s bound once. The gaps the fake
    # CLI sleeps are what the three instants must be separated by.
    assert first_text - first_output >= 0.25
    assert total - first_text >= 0.25


def test_no_text_consumer_times_startup_but_not_a_first_word(tmp_path, monkeypatch, recorded):
    _cli(tmp_path, monkeypatch, body=_TIMED_CLI)
    _run(source="chat", on_activity=lambda e: None)

    (rec,) = recorded
    assert rec["first_output_seconds"] is not None
    assert rec["first_text_seconds"] is None


def test_unattended_turns_record_no_timings(tmp_path, monkeypatch, recorded):
    """Cron reads one JSON blob at exit; there is no stream to time, and a
    number here would be the whole turn masquerading as a first word."""
    _cli(tmp_path, monkeypatch, body=_TIMED_CLI)
    _run(source="cron", on_activity=lambda e: None, on_text=lambda t: None)

    (rec,) = recorded
    assert rec["first_output_seconds"] is None
    assert rec["first_text_seconds"] is None

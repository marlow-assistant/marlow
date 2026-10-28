"""The stream-json read loop in server/invoke.py (phase 1.1).

`tests/test_activity_parse.py` covers the line parser against real envelope
shapes. This file covers the thing around it: when streaming is turned on at
all, that events actually arrive *during* the turn rather than in a burst at
the end, and that every property the non-streaming path already had --
identical InvocationResult, stderr captured, timeout, error paths -- survives
the format change.

The fake CLI below emits the six-line sequence observed on the box (CLI
2.1.235, three runs, see docs/tasks/turn-activity-streaming.md): a
rate_limit_event, a system init, an assistant tool_use, a user tool_result, an
assistant text block, then result. It flushes and sleeps between lines because
the single most important assertion here is a *timing* one -- a CLI that
buffered its whole stream and flushed at exit would satisfy every
shape-based test in this file while delivering exactly the ten seconds of
silence the feature exists to remove.
"""
import asyncio
import dataclasses
import json
import os
import textwrap
import time

import pytest

from server.config import settings as real_settings
from server.invoke import ActivityEvent, InvokeError, _final_envelope, run_claude


def _with_claude_bin(monkeypatch, path: str) -> None:
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=path)
    )


def _fake_cli(tmp_path, body: str, name: str = "claude"):
    path = tmp_path / name
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return path


# Emits the observed six-line stream when asked for stream-json, and the plain
# single-object envelope otherwise -- so one binary can serve both formats and
# a test can assert on which one the server actually chose.
_STREAMING_CLI = '''
    import json, sys, time
    argv = sys.argv[1:]
    gap = float(__import__("os").environ.get("FAKE_LINE_GAP", "0"))

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()
        time.sleep(gap)

    envelope = {
        "type": "result",
        "is_error": False,
        "subtype": "success",
        "result": "\\x00".join(argv),
        "session_id": "stream-session",
        "permission_denials": [],
        "usage": {"input_tokens": 11, "output_tokens": 22},
    }

    if "stream-json" in argv:
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "ok"}})
        emit({"type": "system", "subtype": "init", "tools": ["Read"]})
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/passwd"}}]}})
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1"}]}})
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "the answer"}]}})
        emit(envelope)
    else:
        print(json.dumps(envelope))
'''


def _run(prompt="hello", **kwargs):
    return asyncio.run(run_claude(prompt, **kwargs))


# --- when streaming is on at all ----------------------------------------------


@pytest.mark.parametrize("source", ["cron", "system", "compaction", None, "future-thing"])
def test_unattended_sources_never_stream_even_with_a_callback(tmp_path, monkeypatch, source):
    """The gate is active_turn.ATTENDED_SOURCES, and it fails closed.

    Nobody is watching a cron turn, and --output-format is the one argument in
    _invoke_once that changes what the CLI prints. An unclassified source
    ("future-thing" here) must land on the format that has run in production
    since gate #1, not on the new one.
    """
    _with_claude_bin(monkeypatch, str(_fake_cli(tmp_path, _STREAMING_CLI)))

    events: list[ActivityEvent] = []
    result = _run(source=source, on_activity=events.append)

    argv = result.result_text.split("\x00")
    assert "stream-json" not in argv
    assert "--verbose" not in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert events == []


def test_an_attended_turn_without_a_callback_also_stays_on_plain_json(tmp_path, monkeypatch):
    """Streaming costs a different output format; don't pay it for nothing.

    Every existing caller passes no callback, so this is what keeps this change
    a no-op for all of them.
    """
    _with_claude_bin(monkeypatch, str(_fake_cli(tmp_path, _STREAMING_CLI)))

    result = _run(source="chat")

    argv = result.result_text.split("\x00")
    assert "stream-json" not in argv
    assert argv[argv.index("--output-format") + 1] == "json"


def test_attended_turn_with_a_callback_streams(tmp_path, monkeypatch):
    _with_claude_bin(monkeypatch, str(_fake_cli(tmp_path, _STREAMING_CLI)))

    events: list[ActivityEvent] = []
    result = _run(source="chat", on_activity=events.append)

    argv = result.result_text.split("\x00")
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    # Not decoration: the CLI refuses stream-json in print mode without it.
    assert "--verbose" in argv
    # And --output-format is still ahead of the `--` separator, like every
    # other flag -- past it, it would be prompt text instead of a flag.
    assert argv.index("--output-format") < argv.index("--")


# --- the load-bearing one: it arrives DURING the turn --------------------------


def test_events_arrive_before_the_turn_finishes(tmp_path, monkeypatch):
    """A buffered-then-flushed stream would pass every other test in this file.

    With a 0.15s gap per line, the tool_use event is four lines ahead of the
    result, so genuine streaming delivers it a comfortable margin before
    run_claude returns. If the read loop ever regresses to communicate(), every
    event lands within milliseconds of the return and this fails.
    """
    monkeypatch.setenv("FAKE_LINE_GAP", "0.15")
    _with_claude_bin(monkeypatch, str(_fake_cli(tmp_path, _STREAMING_CLI)))

    seen: list[tuple[ActivityEvent, float]] = []
    _run(source="chat", on_activity=lambda e: seen.append((e, time.monotonic())))
    returned_at = time.monotonic()

    kinds = [e.kind for e, _ in seen]
    assert kinds == ["tool_start", "tool_end", "text"]
    first_at = seen[0][1]
    assert returned_at - first_at > 0.3, (
        f"first event only {returned_at - first_at:.3f}s before the result — "
        "that is a buffered flush, not a stream"
    )
    # Monotonically ordered, as the UI will assume when it replaces a label.
    assert [at for _, at in seen] == sorted(at for _, at in seen)


def test_the_tool_name_reaches_the_ui_and_its_input_does_not(tmp_path, monkeypatch):
    """§1.2: the tool NAME and nothing else. The fake plants a file path in
    `input` precisely so this can assert it never comes out the other end."""
    _with_claude_bin(monkeypatch, str(_fake_cli(tmp_path, _STREAMING_CLI)))

    events: list[ActivityEvent] = []
    _run(source="chat", on_activity=events.append)

    assert events[0].tool == "Read"
    assert not any("/etc/passwd" in repr(e) for e in events)


# --- the envelope survives the format change ----------------------------------


def test_the_result_is_identical_whether_or_not_it_streamed(tmp_path, monkeypatch):
    """Everything downstream of invoke.py reads InvocationResult, so the format
    switch is only safe if the parsed result is the same object shape. argv
    differs by construction (that's the flag), so it is compared separately."""
    _with_claude_bin(monkeypatch, str(_fake_cli(tmp_path, _STREAMING_CLI)))

    plain = _run(source="chat")
    streamed = _run(source="chat", on_activity=lambda e: None)

    for field in ("exit_code", "is_error", "subtype", "session_id", "permission_denials"):
        assert getattr(plain, field) == getattr(streamed, field), field
    assert plain.usage == streamed.usage == {"input_tokens": 11, "output_tokens": 22}
    assert streamed.session_id == "stream-session"
    # raw_stdout keeps the WHOLE stream, not just the envelope — it is what
    # every existing error message quotes.
    assert streamed.raw_stdout.count("\n") == 6


def test_a_result_line_larger_than_the_readline_limit_survives(tmp_path, monkeypatch):
    """asyncio's StreamReader.readline() raises past 64KiB, and the result line
    carries the entire reply. The obvious implementation would therefore lose
    exactly the long answers most worth waiting for."""
    big = "x" * 200_000
    _with_claude_bin(
        monkeypatch,
        str(
            _fake_cli(
                tmp_path,
                f'''
                import json, sys
                sys.stdout.write(json.dumps({{"type": "assistant", "message": {{
                    "content": [{{"type": "tool_use", "name": "Read"}}]}}}}) + "\\n")
                sys.stdout.write(json.dumps({{
                    "type": "result", "is_error": False, "subtype": "success",
                    "result": {big!r}, "session_id": "s1", "permission_denials": [],
                }}) + "\\n")
                ''',
            )
        ),
    )

    events: list[ActivityEvent] = []
    result = _run(source="chat", on_activity=events.append)

    assert result.result_text == big
    assert [e.kind for e in events] == ["tool_start"]


def test_large_stderr_does_not_deadlock_the_stream(tmp_path, monkeypatch):
    """stderr is a pipe with its own buffer. Reading stdout to EOF *first* and
    stderr after would hang forever on a CLI that filled it — the deadlock
    communicate() exists to avoid, and which the streaming path has to avoid
    on its own."""
    _with_claude_bin(
        monkeypatch,
        str(
            _fake_cli(
                tmp_path,
                '''
                import json, sys
                sys.stderr.write("n" * 300_000)
                sys.stderr.flush()
                sys.stdout.write(json.dumps({"type": "result", "is_error": False,
                    "subtype": "success", "result": "ok", "session_id": "s1",
                    "permission_denials": []}) + "\\n")
                ''',
            )
        ),
    )

    result = asyncio.run(
        asyncio.wait_for(
            run_claude("hi", source="chat", on_activity=lambda e: None), timeout=20
        )
    )

    assert result.result_text == "ok"
    assert len(result.raw_stderr) == 300_000


def test_a_stream_with_no_result_line_is_an_invoke_error(tmp_path, monkeypatch):
    """Exit 0 with no envelope is not a successful turn. It has to reach the
    InvokeError path so queue.py surfaces an ERROR turn and telemetry records
    the invoke_error subtype, rather than a turn that silently says nothing."""
    _with_claude_bin(
        monkeypatch,
        str(
            _fake_cli(
                tmp_path,
                '''
                import json, sys
                sys.stdout.write(json.dumps({"type": "system", "subtype": "init"}) + "\\n")
                ''',
            )
        ),
    )

    with pytest.raises(InvokeError, match="no result line"):
        _run(source="chat", on_activity=lambda e: None)


def test_a_nonzero_exit_still_short_circuits_before_envelope_parsing(tmp_path, monkeypatch):
    """The pre-existing contract: a non-zero exit is the CLI itself failing, and
    is reported from stderr without looking for an envelope at all."""
    _with_claude_bin(
        monkeypatch,
        str(
            _fake_cli(
                tmp_path,
                '''
                import sys
                sys.stderr.write("boom\\n")
                sys.exit(3)
                ''',
            )
        ),
    )

    result = _run(source="chat", on_activity=lambda e: None)

    assert result.exit_code == 3
    assert result.is_error is True
    assert "boom" in result.raw_stderr


def test_a_raising_callback_cannot_fail_the_turn(tmp_path, monkeypatch):
    """A status label is a nicety; the answer under it is the product. The UI
    side of this is the least-reviewed code in the chain, so the boundary is
    here rather than trusting it."""
    _with_claude_bin(monkeypatch, str(_fake_cli(tmp_path, _STREAMING_CLI)))

    def _explode(event):
        raise RuntimeError("callback is broken")

    result = _run(source="chat", on_activity=_explode)

    assert result.is_error is False
    assert result.session_id == "stream-session"


def test_a_hostile_stdout_line_cannot_fail_the_turn(tmp_path, monkeypatch):
    """stdout is shared with whatever else the CLI decides to print. A stray
    log line, or a crafted one echoed back from a prompt, must cost a status
    update at most — never the turn."""
    _with_claude_bin(
        monkeypatch,
        str(
            _fake_cli(
                tmp_path,
                '''
                import json, sys
                sys.stdout.write("not json at all\\n")
                sys.stdout.write("[1, 2, 3]\\n")
                sys.stdout.write("{\\"type\\": \\"result\\", \\"truncated\\"\\n")
                sys.stdout.write(json.dumps({"type": "result", "is_error": False,
                    "subtype": "success", "result": "survived", "session_id": "s1",
                    "permission_denials": []}) + "\\n")
                ''',
            )
        ),
    )

    events: list[ActivityEvent] = []
    result = _run(source="chat", on_activity=events.append)

    assert result.result_text == "survived"
    assert events == []


# --- _final_envelope ----------------------------------------------------------


def test_final_envelope_takes_the_last_result_line():
    stdout = "\n".join(
        [
            json.dumps({"type": "result", "result": "first"}),
            json.dumps({"type": "assistant", "message": {"content": []}}),
            json.dumps({"type": "result", "result": "last"}),
        ]
    )
    assert _final_envelope(stdout)["result"] == "last"


def test_final_envelope_ignores_trailing_noise_after_the_envelope():
    stdout = "\n".join(
        [json.dumps({"type": "result", "result": "ok"}), "", "goodbye", "[]"]
    )
    assert _final_envelope(stdout)["result"] == "ok"


def test_final_envelope_returns_none_when_there_is_no_result_line():
    assert _final_envelope("") is None
    assert _final_envelope('{"type": "system"}') is None
    assert _final_envelope("plain text") is None


# --- failure paths that must not leave a CLI running --------------------------


def test_the_no_result_line_error_names_the_tail_without_quoting_it(tmp_path, monkeypatch):
    """Where the stream broke, never what it said. The last line of a turn that
    died mid-stream is typically a tool_result -- an email or a file the job
    had just read -- and this message goes to the server log and to
    turn.error. It used to quote the tail's last 500 chars (/code-review
    medium, 2026-09-28). A cron turn, because cron reaching stream-json is what
    made that tail routinely hold mail."""
    _with_claude_bin(
        monkeypatch,
        str(
            _fake_cli(
                tmp_path,
                '''
                import json, sys
                print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {}}))
                print(json.dumps({"type": "system", "subtype": "init",
                                  "tools": ["Read"] * 400}))
                print(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "memory/x.md"}}]}}))
                print(json.dumps({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": "PRIVATE-EMAIL-BODY"}]}}))
                sys.stdout.write('{"type": "assistant", "message": {"content": "TRUNCATED-TEXT')
                ''',
            )
        ),
    )

    with pytest.raises(InvokeError) as excinfo:
        _run(source="cron", on_tool_call=lambda call: None)

    message = str(excinfo.value)
    assert "PRIVATE-EMAIL-BODY" not in message
    assert "TRUNCATED-TEXT" not in message
    assert "memory/x.md" not in message
    assert "system/init, assistant, user, <unparseable>" in message


def test_a_failing_read_loop_kills_the_cli_rather_than_orphaning_it(tmp_path, monkeypatch):
    """asyncio.gather propagates the first exception immediately and does NOT
    cancel its siblings — so without an explicit kill, a read loop that raises
    leaves `claude -p` running while the queue starts the next turn. That is
    the concurrent invocation the single work queue exists to prevent, and two
    live CLIs race each other's .credentials.json refresh write-back.

    The fake writes its pid and then sleeps far longer than this test, so
    "still running afterwards" is observable rather than inferred.
    """
    pidfile = tmp_path / "pid"
    _with_claude_bin(
        monkeypatch,
        str(
            _fake_cli(
                tmp_path,
                f'''
                import os, sys, time
                open({str(pidfile)!r}, "w").write(str(os.getpid()))
                time.sleep(60)
                ''',
            )
        ),
    )

    async def _explode(stream, handle):
        # Wait for the child to be up, so the kill has something to kill.
        for _ in range(200):
            if pidfile.exists():
                break
            await asyncio.sleep(0.01)
        raise RuntimeError("stdout pipe fell over")

    monkeypatch.setattr("server.invoke._read_stdout_lines", _explode)

    with pytest.raises(RuntimeError, match="stdout pipe fell over"):
        _run(source="chat", on_activity=lambda e: None)

    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        # Signal 0 probes for existence. The process is killed AND reaped by
        # the time the exception surfaces, so this must not find it.
        os.kill(pid, 0)


def test_a_callback_that_raises_basexception_still_kills_the_cli(tmp_path, monkeypatch):
    """_activity_line_handler catches Exception, which by design does not cover
    BaseException. That is the realistic route into the path above, so it gets
    its own test rather than resting on the injected failure."""
    pidfile = tmp_path / "pid"
    _with_claude_bin(
        monkeypatch,
        str(
            _fake_cli(
                tmp_path,
                f'''
                import json, os, sys, time
                open({str(pidfile)!r}, "w").write(str(os.getpid()))
                print(json.dumps({{"type": "assistant", "message": {{"content": [
                    {{"type": "tool_use", "name": "Read"}}]}}}}), flush=True)
                time.sleep(60)
                ''',
            )
        ),
    )

    def _explode(event):
        raise KeyboardInterrupt("not an Exception subclass")

    with pytest.raises(KeyboardInterrupt):
        _run(source="chat", on_activity=_explode)

    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# --- a second result envelope cannot clear the first --------------------------
#
# The threat this defends is narrow and the boundary is elsewhere (the
# PreToolUse hook, the bwrap namespace). What makes it worth three lines of
# code is that `permission_denials` is the project's stated denial detector --
# a denied tool still reports exit=0/is_error=false -- so clearing it is the
# one forgery that would be completely silent. `result` is not defended,
# deliberately: a prompt-injected model already writes the reply.


def _two_envelopes(first: dict, second: dict) -> str:
    base = {"type": "result", "subtype": "success", "session_id": "s1"}
    return "\n".join(json.dumps({**base, **e}) for e in (first, second))


def test_a_later_envelope_cannot_clear_a_reported_denial():
    stdout = _two_envelopes(
        {"result": "a", "is_error": False, "permission_denials": [{"tool_name": "Bash"}]},
        {"result": "b", "is_error": False, "permission_denials": []},
    )

    payload = _final_envelope(stdout)

    assert payload["permission_denials"] == [{"tool_name": "Bash"}]
    # Content still comes from the last envelope — only the security-relevant
    # fields are merged.
    assert payload["result"] == "b"


def test_denials_are_unioned_across_every_envelope():
    stdout = _two_envelopes(
        {"permission_denials": [{"tool_name": "Bash"}]},
        {"permission_denials": [{"tool_name": "Read"}]},
    )

    denied = [d["tool_name"] for d in _final_envelope(stdout)["permission_denials"]]

    assert sorted(denied) == ["Bash", "Read"]


def test_a_later_envelope_cannot_clear_is_error():
    stdout = _two_envelopes({"is_error": True}, {"is_error": False})

    assert _final_envelope(stdout)["is_error"] is True


def test_a_single_envelope_is_returned_untouched():
    """The 99.99% path. Merging must not perturb it — in particular it must not
    invent a `permission_denials` key on an envelope that had none."""
    stdout = json.dumps({"type": "result", "result": "ok", "session_id": "s1"})

    payload = _final_envelope(stdout)

    assert payload == {"type": "result", "result": "ok", "session_id": "s1"}


def test_new_line_types_after_the_envelope_are_tolerated():
    """The whole reason multiple envelopes are merged rather than rejected: no
    position rule, so a CLI that grows a trailing line does not break every
    attended turn while cron keeps working."""
    stdout = "\n".join(
        [
            json.dumps({"type": "result", "result": "ok", "session_id": "s1"}),
            json.dumps({"type": "telemetry_flush", "note": "invented future line"}),
            json.dumps({"type": "shutdown"}),
        ]
    )

    assert _final_envelope(stdout)["result"] == "ok"


def test_the_last_envelope_still_names_the_session():
    stdout = _two_envelopes({"session_id": "old"}, {"session_id": "new"})

    assert _final_envelope(stdout)["session_id"] == "new"


def test_an_appended_envelope_cannot_hide_a_denial_end_to_end(tmp_path, monkeypatch):
    """The parser tests above are the unit; this is the path a real stream
    takes, so the merge is proven where InvocationResult is actually built."""
    _with_claude_bin(
        monkeypatch,
        str(
            _fake_cli(
                tmp_path,
                '''
                import json, sys
                print(json.dumps({"type": "result", "is_error": False,
                    "subtype": "success", "result": "denied one", "session_id": "s1",
                    "permission_denials": [{"tool_name": "Bash"}]}), flush=True)
                print(json.dumps({"type": "result", "is_error": False,
                    "subtype": "success", "result": "all clear", "session_id": "s1",
                    "permission_denials": []}), flush=True)
                ''',
            )
        ),
    )

    result = _run(source="chat", on_activity=lambda e: None)

    assert result.had_permission_denial is True
    assert [d["tool_name"] for d in result.permission_denials] == ["Bash"]

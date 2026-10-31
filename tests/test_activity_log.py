"""The persisted activity log (docs/tasks/ux-lane.md §2a).

Four layers, each with its own failure worth pinning:
- the per-tool target allowlist -- the privacy decision; a leak here outlives
  the turn on disk and crosses the API;
- the stream collector -- a cron turn must reach stream-json and report its
  tools, and must still not pick up anything live;
- the writer -- month files, retention, never raising into the drain loop;
- the queue wiring -- a record that is built correctly but never written, or
  written without the files the commit staged, would pass every unit test.
"""
import asyncio
import dataclasses
import json
import os
import stat
import time

import pytest

from server import active_turn, activity_log
from server.config import settings as real_settings
from server.identity_git import TurnCommit
from server.invoke import InvocationResult, ToolCall, _tool_target, parse_tool_calls, run_claude
from server.queue import MAX_LOGGED_TOOL_CALLS, Turn, TurnStatus, WorkQueue, _record_tool_call
from tests.test_activity_stream import _STREAMING_CLI, _fake_cli, _with_claude_bin

WD = str(real_settings.working_dir)


# --- the target allowlist ------------------------------------------------------


def test_a_file_in_the_working_tree_is_recorded_relative():
    assert _tool_target("Read", {"file_path": f"{WD}/memory/travel.md"}) == "memory/travel.md"
    assert _tool_target("Edit", {"file_path": "memory/travel.md"}) == "memory/travel.md"
    assert _tool_target("NotebookEdit", {"notebook_path": f"{WD}/n.ipynb"}) == "n.ipynb"


def test_a_file_outside_the_tree_is_recorded_absolute():
    assert _tool_target("Read", {"file_path": "/tmp/x.txt"}) == "/tmp/x.txt"


@pytest.mark.parametrize(
    "path",
    [
        f"{WD}/.env",  # SECRET_GLOBS
        f"{WD}/sub/.env.local",
        str(real_settings.data_dir / "device_tokens.json"),  # SECRET_PATHS
        str(real_settings.credentials_path),
        "~/.ssh/id_ed25519",
        f"{WD}/.claude/settings.json",  # AGENT_CONFIG_PATHS
        f"{WD}/.git/config",
        ".env",  # relative, resolved against the working dir
    ],
    # IDs without the sandbox path: conftest's mkdtemp names it per process,
    # so under xdist every worker collected differently named tests and the
    # run refused to start (2026-10-31).
    ids=lambda p: p.replace(WD, "<wd>"),
)
def test_secret_and_agent_config_paths_record_no_target(path):
    """The name still shows ("Read"); where it read does not."""
    assert _tool_target("Read", {"file_path": path}) is None


def test_web_search_keeps_the_query_collapsed_and_clipped():
    assert _tool_target("WebSearch", {"query": "flights\n  to   YYC"}) == "flights to YYC"
    long = _tool_target("WebSearch", {"query": "q" * 500})
    assert len(long) == 120 and long.endswith("…")


def test_web_fetch_keeps_only_the_host():
    """A URL's path and query string can carry tokens."""
    target = _tool_target("WebFetch", {"url": "https://example.com/reset?token=s3cret"})
    assert target == "example.com"


@pytest.mark.parametrize(
    "name, tool_input",
    [
        ("Bash", {"command": "curl -H 'Authorization: Bearer s3cret' x"}),
        ("Grep", {"pattern": "s3cret", "path": WD}),
        ("Glob", {"pattern": "**/*.md"}),
        ("mcp__claude_ai_Gmail__search_threads", {"query": "subject:s3cret"}),
        ("SomeFutureTool", {"file_path": f"{WD}/memory/x.md"}),
    ],
)
def test_everything_not_allowlisted_is_name_only(name, tool_input):
    assert _tool_target(name, tool_input) is None


@pytest.mark.parametrize("bad", [None, "str", [], {"file_path": 5}, {"file_path": ""}])
def test_malformed_input_is_no_target_not_an_error(bad):
    assert _tool_target("Read", bad) is None


# --- the line parser -----------------------------------------------------------


def _assistant(*blocks) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


def test_every_tool_use_block_on_a_line_is_returned():
    """Parallel calls arrive as several blocks in one message. The status-line
    parser keeps one; the log must keep all of them."""
    line = _assistant(
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "memory/a.md"}},
        {"type": "tool_use", "id": "b", "name": "WebSearch", "input": {"query": "q"}},
    )
    calls = parse_tool_calls(line, now=1.0)
    assert calls == [
        ("a", ToolCall("Read", "memory/a.md", 1.0)),
        ("b", ToolCall("WebSearch", "q", 1.0)),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not json",
        "[1,2]",
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}),
        json.dumps({"type": "assistant", "message": {"content": "x"}}),
        _assistant({"type": "tool_use", "name": 7}),
    ],
)
def test_unfamiliar_lines_yield_nothing(line):
    assert parse_tool_calls(line) == []


# --- invocation: cron reaches stream-json, and nothing live --------------------

_TWO_TOOLS_CLI = '''
    import json, sys
    argv = sys.argv[1:]
    envelope = {"type": "result", "is_error": False, "subtype": "success",
                "result": "\\x00".join(argv), "session_id": "s", "permission_denials": []}
    if "stream-json" in argv:
        read = {"type": "tool_use", "id": "t1", "name": "Read",
                "input": {"file_path": "memory/travel.md"}}
        # The same block twice, as a CLI re-emitting a message would: logged once.
        for _ in range(2):
            print(json.dumps({"type": "assistant", "message": {"content": [read]}}))
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "echo s3cret"}}]}}))
        print(json.dumps(envelope))
    else:
        print(json.dumps(envelope))
'''


def _argv(result) -> list[str]:
    return result.result_text.split("\x00")


def test_a_cron_turn_collecting_tool_calls_streams_without_partial_messages(tmp_path, monkeypatch):
    _with_claude_bin(monkeypatch, str(_fake_cli(tmp_path, _TWO_TOOLS_CLI)))
    calls, events, text = [], [], []
    result = asyncio.run(
        run_claude(
            "hello",
            source="cron",
            on_activity=events.append,
            on_text=text.append,
            on_tool_call=calls.append,
        )
    )
    argv = _argv(result)
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    # Nobody is watching: no deltas paid for, and nothing live delivered.
    assert "--include-partial-messages" not in argv
    assert events == [] and text == []
    assert [(c.name, c.target) for c in calls] == [("Read", "memory/travel.md"), ("Bash", None)]
    assert not any("s3cret" in repr(c) for c in calls)


def test_an_attended_turn_still_gets_everything_live(tmp_path, monkeypatch):
    _with_claude_bin(monkeypatch, str(_fake_cli(tmp_path, _STREAMING_CLI)))
    calls, events = [], []
    result = asyncio.run(
        run_claude(
            "hello", source="chat", on_activity=events.append,
            on_text=lambda t: None, on_tool_call=calls.append,
        )
    )
    assert "--include-partial-messages" in _argv(result)
    assert [e.kind for e in events] == ["tool_start", "tool_end", "text"]
    assert [c.name for c in calls] == ["Read"]


def test_a_raising_tool_call_callback_cannot_fail_the_turn(tmp_path, monkeypatch):
    _with_claude_bin(monkeypatch, str(_fake_cli(tmp_path, _TWO_TOOLS_CLI)))

    def _boom(call):
        raise RuntimeError("callback bug")

    result = asyncio.run(run_claude("hello", source="cron", on_tool_call=_boom))
    assert result.is_error is False


# --- the queue's collector -----------------------------------------------------


def _turn(**kwargs) -> Turn:
    return Turn(turn_id="t1", prompt="x", session_id=None, resume=False,
                source="cron", client_msg_id=None, **kwargs)


def test_the_collector_keeps_the_first_calls_and_counts_the_rest():
    turn = _turn()
    record = _record_tool_call(turn)
    for i in range(MAX_LOGGED_TOOL_CALLS + 7):
        record(ToolCall("Read", f"f{i}", float(i)))
    assert len(turn.tool_calls) == MAX_LOGGED_TOOL_CALLS
    assert turn.tool_calls[0].target == "f0"
    assert turn.tool_calls_dropped == 7


# --- the record ----------------------------------------------------------------


def test_the_record_carries_no_prompt_reply_or_error_text():
    turn = _turn(label="morning")
    turn.prompt = "PROMPT-TEXT"
    turn.status = TurnStatus.ERROR
    turn.error = "claude -p stream produced no result line; tail: REPLY-TEXT"
    turn.result = InvocationResult(
        exit_code=0, is_error=True, subtype="success", result_text="REPLY-TEXT",
        session_id="s",
        permission_denials=[{"tool_name": "Bash", "tool_input": {"command": "cat SECRET-INPUT"}}],
    )
    record = activity_log.build_record(turn, merged=[], commit=None)
    blob = json.dumps(record)
    for leaked in ("PROMPT-TEXT", "REPLY-TEXT", "SECRET-INPUT"):
        assert leaked not in blob
    assert record["denials"] == ["Bash"]
    assert record["label"] == "morning"
    assert record["status"] == "error"


def test_the_record_carries_the_model_and_effort_that_ran():
    turn = _turn(label="consolidate")
    turn.result = InvocationResult(
        exit_code=0, is_error=False, subtype="success", result_text="ok", session_id="s",
        model="claude-sonnet-5-5", effort="high",
    )
    record = activity_log.build_record(turn, merged=[], commit=None)
    assert (record["model"], record["effort"]) == ("claude-sonnet-5-5", "high")


def test_a_turn_with_no_result_records_no_model_or_effort():
    record = activity_log.build_record(_turn(), merged=[], commit=None)
    assert record["model"] is None and record["effort"] is None


def test_the_record_names_the_committed_files():
    record = activity_log.build_record(
        _turn(), merged=["t0"], commit=TurnCommit("abc1234", ("memory/travel.md",))
    )
    assert record["files"] == ["memory/travel.md"]
    assert record["commit"] == "abc1234"
    assert record["merged"] == ["t0"]


def test_tier_and_output_are_absent_unless_set():
    """Additive fields: absent is what every older record means, so an
    ordinary turn must not start writing `"tier": null`."""
    record = activity_log.build_record(_turn(label="morning"), merged=[], commit=None)
    assert "tier" not in record and "has_output" not in record

    record = activity_log.build_record(
        _turn(label="ideas", tier="plumbing"), merged=[], commit=None, has_output=True
    )
    assert record["tier"] == "plumbing" and record["has_output"] is True
    assert record["v"] == 1


# --- the writer ----------------------------------------------------------------


def _use_data_dir(monkeypatch, path):
    monkeypatch.setattr(
        "server.activity_log.settings", dataclasses.replace(real_settings, data_dir=path)
    )


def _utc(year, month, day=15) -> float:
    return time.mktime((year, month, day, 12, 0, 0, 0, 0, 0)) - time.timezone


def test_records_append_to_a_private_month_file(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    when = _utc(2026, 9)
    activity_log.append_record({"turn_id": "a", "finished_at": when}, now=when)
    activity_log.append_record({"turn_id": "b", "finished_at": when}, now=when)

    path = tmp_path / "activity" / "2026-09.jsonl"
    assert [json.loads(line)["turn_id"] for line in path.read_text().splitlines()] == ["a", "b"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_retention_keeps_twelve_months_including_the_current_one(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    directory = tmp_path / "activity"
    directory.mkdir()
    for name in ("2025-09.jsonl", "2025-10.jsonl", "2026-08.jsonl", "notes.txt"):
        (directory / name).write_text("{}\n")

    now = _utc(2026, 9)
    activity_log.append_record({"turn_id": "x", "finished_at": now}, now=now)

    assert sorted(os.listdir(directory)) == [
        "2025-10.jsonl", "2026-08.jsonl", "2026-09.jsonl", "notes.txt",
    ]


def test_a_write_failure_is_logged_not_raised(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    _use_data_dir(monkeypatch, blocker)
    activity_log.append_record({"turn_id": "x"})  # must not raise


def test_background_output_round_trips_from_a_private_month_file(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    aug, sep = _utc(2026, 8), _utc(2026, 9)
    for turn_id, when, text in (("old", aug, "August"), ("new", sep, "September")):
        turn = _turn(label="ideas")
        turn.turn_id, turn.finished_at = turn_id, when
        assert activity_log.append_output(turn, text, now=sep) is True

    path = tmp_path / "background" / "2026-09.jsonl"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    with open(path, "a") as handle:
        handle.write('{"turn_id": "torn\n[1]\n{"turn_id": "notext"}\n')

    assert activity_log.read_output("old") == {
        "turn_id": "old", "label": "ideas", "finished_at": aug, "text": "August",
    }
    assert activity_log.read_output("new")["text"] == "September"
    assert activity_log.read_output("notext") is None
    assert activity_log.read_output("missing") is None


def test_background_output_keeps_twelve_months(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    directory = tmp_path / "background"
    directory.mkdir()
    (directory / "2025-09.jsonl").write_text("{}\n")
    now = _utc(2026, 9)
    turn = _turn()
    turn.finished_at = now
    activity_log.append_output(turn, "x", now=now)
    assert sorted(os.listdir(directory)) == ["2026-09.jsonl"]


def test_a_failed_background_write_says_so(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    _use_data_dir(monkeypatch, blocker)
    assert activity_log.append_output(_turn(), "x") is False
    assert activity_log.read_output("t1") is None


# --- the queue wiring ----------------------------------------------------------


def _read_records(data_dir) -> list[dict]:
    directory = data_dir / "activity"
    if not directory.exists():
        return []
    return [
        json.loads(line)
        for name in sorted(os.listdir(directory))
        for line in (directory / name).read_text().splitlines()
    ]


def _run_queue(monkeypatch, tmp_path, enqueue_kwargs, fake_run_claude):
    _use_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("server.queue.run_claude", fake_run_claude)
    monkeypatch.setattr(
        "server.queue.commit_after_turn",
        lambda turn_id, source: TurnCommit("abc1234", ("memory/travel.md",)),
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", **enqueue_kwargs)
        deadline = time.monotonic() + 5
        while not _read_records(tmp_path) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await wq.stop()
        return turn_id

    return asyncio.run(_body())


def test_a_cron_turn_is_written_with_its_tools_and_files(tmp_path, monkeypatch):
    async def _fake(prompt, *, on_tool_call=None, **kwargs):
        assert on_tool_call is not None, "cron turns must collect tool calls"
        on_tool_call(ToolCall("Read", "memory/travel.md", 1.0))
        return InvocationResult(exit_code=0, is_error=False, subtype="success",
                                result_text="brief", session_id="s")

    turn_id = _run_queue(monkeypatch, tmp_path, {"source": "cron", "label": "morning"}, _fake)

    [record] = _read_records(tmp_path)
    assert record["turn_id"] == turn_id
    assert record["label"] == "morning"
    assert record["tools"][0]["target"] == "memory/travel.md"
    assert record["files"] == ["memory/travel.md"]
    assert record["started_at"] >= record["created_at"]
    assert record["finished_at"] >= record["started_at"]


def test_a_compaction_turn_is_never_logged_and_never_streams(tmp_path, monkeypatch):
    seen = {}

    async def _fake(prompt, *, on_tool_call=None, **kwargs):
        seen["on_tool_call"] = on_tool_call
        return InvocationResult(exit_code=0, is_error=False, subtype="success",
                                result_text="", session_id="s")

    _use_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("server.queue.run_claude", _fake)
    written = []
    monkeypatch.setattr("server.queue.activity_log.append_record", written.append)
    committed = []
    monkeypatch.setattr("server.queue.commit_after_turn",
                        lambda turn_id, source: committed.append(turn_id))

    async def _body():
        wq = WorkQueue()
        wq.start()
        wq.enqueue("/compact", source="compaction", raw_prompt=True)
        deadline = time.monotonic() + 5
        while not committed and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await wq.stop()

    asyncio.run(_body())
    assert committed, "the drain loop never reached the commit"
    assert seen["on_tool_call"] is None
    assert written == []


def test_an_approval_lands_on_the_running_turn(tmp_path, monkeypatch):
    wq_ref = {}

    async def _fake(prompt, **kwargs):
        wq_ref["wq"].note_permission_decision("Edit", "allow")
        return InvocationResult(exit_code=0, is_error=False, subtype="success",
                                result_text="ok", session_id="s")

    _use_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("server.queue.run_claude", _fake)
    monkeypatch.setattr("server.queue.commit_after_turn", lambda turn_id, source: None)

    async def _body():
        wq = WorkQueue()
        wq_ref["wq"] = wq
        wq.start()
        # Nothing is running yet: recorded nowhere, and no error.
        wq.note_permission_decision("Read", "allow")
        wq.enqueue("hi", source="chat")
        deadline = time.monotonic() + 5
        while not _read_records(tmp_path) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await wq.stop()

    asyncio.run(_body())
    [record] = _read_records(tmp_path)
    assert record["approvals"] == [{"tool": "Edit", "decision": "allow"}]
    assert record["files"] == [] and record["commit"] is None


def test_a_failing_log_write_does_not_stop_the_queue(tmp_path, monkeypatch):
    def _boom(record):
        raise OSError("disk full")

    async def _fake(prompt, **kwargs):
        return InvocationResult(exit_code=0, is_error=False, subtype="success",
                                result_text="ok", session_id="s")

    monkeypatch.setattr("server.queue.run_claude", _fake)
    monkeypatch.setattr("server.queue.commit_after_turn", lambda turn_id, source: None)
    monkeypatch.setattr("server.queue.activity_log.append_record", _boom)

    async def _body():
        wq = WorkQueue()
        wq.start()
        ids = [wq.enqueue("a", source="cron"), wq.enqueue("b", source="cron")]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(wq.get_turn(i).status == TurnStatus.DONE for i in ids):
                break
            await asyncio.sleep(0.02)
        statuses = [wq.get_turn(i).status for i in ids]
        await wq.stop()
        return statuses

    assert asyncio.run(_body()) == [TurnStatus.DONE, TurnStatus.DONE]


# --- the reader and GET /v1/activity -------------------------------------------


def _seed(tmp_path, month: str, finished: list[float], extra_lines: list[str] = ()):
    directory = tmp_path / "activity"
    directory.mkdir(exist_ok=True)
    lines = [json.dumps({"turn_id": f"t{f:g}", "finished_at": f}) for f in finished]
    (directory / f"{month}.jsonl").write_text("\n".join([*lines, *extra_lines]) + "\n")


def test_reading_pages_newest_first_across_months(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    aug, sep = _utc(2026, 8), _utc(2026, 9)
    _seed(tmp_path, "2026-08", [aug, aug + 1, aug + 2])
    _seed(tmp_path, "2026-09", [sep, sep + 1])

    first = activity_log.read_recent(limit=3)
    assert [r["finished_at"] for r in first] == [sep + 1, sep, aug + 2]

    second = activity_log.read_recent(limit=3, before=first[-1]["finished_at"])
    assert [r["finished_at"] for r in second] == [aug + 1, aug]


def test_a_torn_or_foreign_line_is_skipped_not_fatal(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    sep = _utc(2026, 9)
    _seed(tmp_path, "2026-09", [sep], extra_lines=['{"turn_id": "torn', "[1]", '{"no_time": 1}'])
    assert [r["finished_at"] for r in activity_log.read_recent()] == [sep]


def test_a_legacy_ios_record_reads_back_and_is_a_chat_source(tmp_path, monkeypatch):
    """Records written before the `ios` -> `chat` rename keep `source: "ios"` for
    their whole 12-month retention. They must still read back, unchanged (the
    server never rewrites stored data), and be recognised as chat turns."""
    _use_data_dir(monkeypatch, tmp_path)
    sep = _utc(2026, 9)
    directory = tmp_path / "activity"
    directory.mkdir()
    lines = [
        json.dumps({"turn_id": "old", "finished_at": sep, "source": "ios"}),
        json.dumps({"turn_id": "new", "finished_at": sep + 1, "source": "chat"}),
    ]
    (directory / "2026-09.jsonl").write_text("\n".join(lines) + "\n")

    records = activity_log.read_recent()
    assert [(r["turn_id"], r["source"]) for r in records] == [("new", "chat"), ("old", "ios")]
    assert all(r["source"] in active_turn.CHAT_SOURCES for r in records)


def test_no_log_yet_is_an_empty_page(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    assert activity_log.read_recent() == []


def test_the_page_size_is_clamped(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    sep = _utc(2026, 9)
    _seed(tmp_path, "2026-09", [sep + i for i in range(activity_log.MAX_PAGE + 5)])
    assert len(activity_log.read_recent(limit=10_000)) == activity_log.MAX_PAGE
    assert len(activity_log.read_recent(limit=0)) == 1


def test_the_endpoint_requires_a_device_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server.app import public_app

    _use_data_dir(monkeypatch, tmp_path)
    with TestClient(public_app) as client:
        assert client.get("/v1/activity").status_code == 401
        bad = {"Authorization": "Bearer not-a-token"}
        assert client.get("/v1/activity", headers=bad).status_code == 401


def test_the_endpoint_pages_and_signals_the_end(tmp_path, monkeypatch, paired_device_token):
    from fastapi.testclient import TestClient

    from server.app import public_app

    _use_data_dir(monkeypatch, tmp_path)
    sep = _utc(2026, 9)
    _seed(tmp_path, "2026-09", [sep, sep + 1, sep + 2])
    headers = {"Authorization": f"Bearer {paired_device_token}"}
    with TestClient(public_app) as client:
        page = client.get("/v1/activity", params={"limit": 2}, headers=headers).json()
        assert [e["finished_at"] for e in page["entries"]] == [sep + 2, sep + 1]
        assert page["next_before"] == sep + 1

        rest = client.get(
            "/v1/activity", params={"limit": 2, "before": page["next_before"]}, headers=headers
        ).json()
        assert [e["finished_at"] for e in rest["entries"]] == [sep]
        assert rest["next_before"] is None

        for bad in ({"limit": 0}, {"limit": 201}, {"before": "nan"}, {"before": "inf"}):
            assert client.get("/v1/activity", params=bad, headers=headers).status_code == 422


# --- background output: the queue wiring and GET /v1/activity/{id}/output ------


def _reply(text):
    async def _fake(prompt, **kwargs):
        return InvocationResult(exit_code=0, is_error=False, subtype="success",
                                result_text=text, session_id="s")
    return _fake


def _read_background(data_dir) -> list[dict]:
    directory = data_dir / "background"
    if not directory.exists():
        return []
    return [
        json.loads(line)
        for name in sorted(os.listdir(directory))
        for line in (directory / name).read_text().splitlines()
    ]


def test_a_silent_cron_reply_is_kept_and_the_record_points_at_it(tmp_path, monkeypatch):
    turn_id = _run_queue(
        monkeypatch, tmp_path, {"source": "cron", "label": "ideas", "tier": "plumbing"}, _reply("[silent]")
    )
    [record] = _read_records(tmp_path)
    assert record["tier"] == "plumbing" and record["has_output"] is True
    [row] = _read_background(tmp_path)
    assert (row["turn_id"], row["label"], row["text"]) == (turn_id, "ideas", "[silent]")
    assert row["finished_at"] == record["finished_at"]


@pytest.mark.parametrize(
    "kwargs, text",
    [
        # Delivered: the text is in the chat, linked by turn_id.
        ({"source": "cron", "label": "morning"}, "Your brief."),
        # Silent with nothing to say.
        ({"source": "cron", "label": "email-flash"}, "  "),
        # A chat turn is never background, whatever it replies.
        ({"source": "chat"}, "[silent]"),
    ],
)
def test_no_background_row_unless_a_cron_reply_went_undelivered(tmp_path, monkeypatch, kwargs, text):
    _run_queue(monkeypatch, tmp_path, kwargs, _reply(text))
    [record] = _read_records(tmp_path)
    assert "has_output" not in record
    assert _read_background(tmp_path) == []


def _delivery(monkeypatch):
    """What reached the chat and the phone, by turn id."""
    rows, pushes = [], []
    monkeypatch.setattr("server.queue.transcript.append_assistant", lambda **kw: rows.append(kw["turn_id"]))
    monkeypatch.setattr("server.queue.push_broadcast_nowait", lambda payload: pushes.append(payload.ref))
    return rows, pushes


def test_a_finished_plumbing_turn_is_never_delivered_whatever_it_says(tmp_path, monkeypatch):
    """The tier decides delivery, not the reply text
    (docs/tasks/default-infrastructure.md "Replies are never pushed")."""
    rows, pushes = _delivery(monkeypatch)
    turn_id = _run_queue(
        monkeypatch, tmp_path, {"source": "cron", "label": "consolidate", "tier": "plumbing"},
        _reply("Moved Sep 9 into the trip log."),
    )
    assert rows == [] and pushes == []
    [row] = _read_background(tmp_path)
    assert (row["turn_id"], row["text"]) == (turn_id, "Moved Sep 9 into the trip log.")
    [record] = _read_records(tmp_path)
    assert record["has_output"] is True


def test_a_failed_plumbing_turn_still_reaches_the_phone(tmp_path, monkeypatch):
    rows, pushes = _delivery(monkeypatch)

    async def _fake(prompt, **kwargs):
        return InvocationResult(exit_code=1, is_error=True, subtype="error",
                                result_text="", session_id="s")

    turn_id = _run_queue(monkeypatch, tmp_path, {"source": "cron", "label": "consolidate", "tier": "plumbing"}, _fake)
    assert rows == [turn_id] and pushes == [turn_id]


def test_an_ordinary_cron_reply_is_still_delivered(tmp_path, monkeypatch):
    rows, pushes = _delivery(monkeypatch)
    turn_id = _run_queue(monkeypatch, tmp_path, {"source": "cron", "label": "morning"}, _reply("Your brief."))
    assert rows == [turn_id] and pushes == [turn_id]


def test_an_errored_cron_turn_keeps_no_output(tmp_path, monkeypatch):
    async def _fake(prompt, **kwargs):
        return InvocationResult(exit_code=1, is_error=True, subtype="error",
                                result_text="[silent]", session_id="s")

    _run_queue(monkeypatch, tmp_path, {"source": "cron", "label": "ideas", "tier": "plumbing"}, _fake)
    [record] = _read_records(tmp_path)
    assert record["status"] == "error" and "has_output" not in record
    assert _read_background(tmp_path) == []


def test_the_output_endpoint_serves_a_kept_reply_to_a_paired_device(tmp_path, monkeypatch, paired_device_token):
    from fastapi.testclient import TestClient

    from server.app import public_app

    _use_data_dir(monkeypatch, tmp_path)
    turn = _turn(label="ideas")
    turn.turn_id, turn.finished_at = "kept", _utc(2026, 9)
    activity_log.append_output(turn, "Added one idea.")
    with TestClient(public_app) as client:
        assert client.get("/v1/activity/kept/output").status_code == 401
        bad = {"Authorization": "Bearer not-a-token"}
        assert client.get("/v1/activity/kept/output", headers=bad).status_code == 401

        headers = {"Authorization": f"Bearer {paired_device_token}"}
        assert client.get("/v1/activity/kept/output", headers=headers).json() == {
            "turn_id": "kept", "label": "ideas", "finished_at": turn.finished_at, "text": "Added one idea.",
        }
        assert client.get("/v1/activity/other/output", headers=headers).status_code == 404

"""The connector startup race (docs/tasks/connector-drops.md): cron turns make
the CLI wait for MCP, and every streamed turn records what was not connected."""
import asyncio
import dataclasses
import json
import textwrap

import pytest

from server import activity_log
from server.config import settings as real_settings
from server.invoke import InvocationResult, _scrubbed_env, parse_init_mcp_status, run_claude
from server.queue import Turn

WAIT_VAR = "MCP_CONNECTION_NONBLOCKING"


def _init(servers) -> str:
    return json.dumps({"type": "system", "subtype": "init", "tools": [], "mcp_servers": servers})


# --- the env ------------------------------------------------------------------


def test_only_a_waiting_turn_gets_the_variable(monkeypatch):
    monkeypatch.delenv(WAIT_VAR, raising=False)
    assert WAIT_VAR not in _scrubbed_env()
    assert _scrubbed_env(wait_for_mcp=True)[WAIT_VAR] == "false"


@pytest.mark.parametrize("inherited", ["false", "true", "0"])
def test_an_inherited_value_never_decides(monkeypatch, inherited):
    # A service env that set it would otherwise make chat wait (or cron not).
    monkeypatch.setenv(WAIT_VAR, inherited)
    assert WAIT_VAR not in _scrubbed_env()
    assert _scrubbed_env(wait_for_mcp=True)[WAIT_VAR] == "false"


def test_waiting_does_not_loosen_the_scrub(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    env = _scrubbed_env(wait_for_mcp=True)
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ANTHROPIC_API_KEY"):
        assert name not in env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


# --- reading init -------------------------------------------------------------


def test_names_the_servers_that_were_not_connected():
    stdout = "\n".join([
        json.dumps({"type": "rate_limit_event"}),
        _init([
            {"name": "claude.ai Gmail", "status": "pending"},
            {"name": "weather", "status": "connected"},
            {"name": "claude.ai Google Calendar", "status": "failed"},
        ]),
    ])
    assert parse_init_mcp_status(stdout) == ["claude.ai Gmail", "claude.ai Google Calendar"]


def test_all_connected_is_an_empty_list_not_none():
    assert parse_init_mcp_status(_init([{"name": "weather", "status": "connected"}])) == []


@pytest.mark.parametrize("stdout", [
    "",
    json.dumps({"type": "result", "subtype": "success"}),
    '{"type": "system", "subtype": "init", "mcp_servers": [',
    json.dumps({"type": "system", "subtype": "init"}),
    json.dumps({"type": "system", "subtype": "init", "mcp_servers": "nope"}),
    json.dumps(["init"]),
])
def test_no_readable_init_is_none(stdout):
    assert parse_init_mcp_status(stdout) is None


def test_only_the_first_init_counts():
    # The first request went out with the first one; a later one says nothing
    # about what the turn started with.
    stdout = "\n".join([
        _init([{"name": "claude.ai Gmail", "status": "pending"}]),
        _init([{"name": "claude.ai Gmail", "status": "connected"}]),
    ])
    assert parse_init_mcp_status(stdout) == ["claude.ai Gmail"]


def test_odd_entries_are_bounded_and_never_raise():
    stdout = _init(
        ["junk", None, {"status": "pending"}, {"name": 7, "status": "pending"}]
        + [{"name": "x" * 500, "status": "pending"}] * 100
    )
    names = parse_init_mcp_status(stdout)
    assert names[:2] == ["?", "?"]
    assert len(names) == 30
    assert all(len(n) <= 80 for n in names)


# --- end to end through run_claude ---------------------------------------------

# Puts what it was given for the variable into the reply, and emits a stream
# whose init has Gmail pending.
_CLI = '''
    import json, os, sys
    argv = sys.argv[1:]
    envelope = {"type": "result", "is_error": False, "subtype": "success",
                "result": os.environ.get("MCP_CONNECTION_NONBLOCKING", "<unset>"),
                "session_id": "s", "permission_denials": []}
    if "stream-json" in argv:
        print(json.dumps({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [
            {"name": "claude.ai Gmail", "status": "pending"},
            {"name": "weather", "status": "connected"}]}))
    print(json.dumps(envelope))
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(_CLI))
    path.chmod(0o755)
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(path))
    )
    monkeypatch.delenv(WAIT_VAR, raising=False)


def test_a_cron_turn_waits_and_records_the_pending_connector(fake_cli):
    result = asyncio.run(run_claude("hi", source="cron", on_tool_call=lambda call: None))
    assert result.result_text == "false"
    assert result.mcp_not_connected == ["claude.ai Gmail"]


@pytest.mark.parametrize("source", ["chat", "ios", "compaction", "system", None])
def test_no_other_source_waits(fake_cli, source):
    result = asyncio.run(run_claude("hi", source=source, on_tool_call=lambda call: None))
    assert result.result_text == "<unset>"


def test_a_chat_turn_still_records_what_was_pending(fake_cli):
    # Chat is the comparison group: it keeps the default, and is measured too.
    result = asyncio.run(run_claude("hi", source="chat", on_tool_call=lambda call: None))
    assert result.mcp_not_connected == ["claude.ai Gmail"]


def test_a_failed_exit_still_records_what_was_pending(tmp_path, monkeypatch):
    # A blind turn that then errors is exactly one worth seeing in the log.
    path = tmp_path / "claude"
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent('''
        import json, sys
        print(json.dumps({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [
            {"name": "claude.ai Gmail", "status": "pending"}]}))
        sys.exit(1)
    '''))
    path.chmod(0o755)
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(path))
    )
    result = asyncio.run(run_claude("hi", source="cron", on_tool_call=lambda call: None))
    assert result.is_error
    assert result.mcp_not_connected == ["claude.ai Gmail"]


def test_a_plain_json_turn_records_nothing(fake_cli):
    result = asyncio.run(run_claude("hi", source="cron"))
    assert result.mcp_not_connected is None


# --- the activity log -----------------------------------------------------------


def _turn(result) -> Turn:
    turn = Turn(turn_id="t1", prompt="x", session_id=None, resume=False,
                source="cron", client_msg_id=None)
    turn.result = result
    return turn


@pytest.mark.parametrize("value", [None, [], ["claude.ai Gmail"]])
def test_the_record_carries_the_init_status(value):
    result = InvocationResult(exit_code=0, is_error=False, subtype="success",
                              result_text="", session_id="s", mcp_not_connected=value)
    record = activity_log.build_record(_turn(result), merged=[], commit=None)
    assert record["mcp_not_connected"] == value


def test_a_turn_without_a_result_records_none():
    assert activity_log.build_record(_turn(None), merged=[], commit=None)["mcp_not_connected"] is None

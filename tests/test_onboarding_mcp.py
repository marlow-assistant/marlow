"""server/onboarding_mcp.py (docs/tasks/onboarding-v2.md, "Harness design").

The server is stateless: the step list arrives on argv, and what each call
returns is the next step's text. The transport test runs the real file in a
subprocess with `-I`, as invoke.py does.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from server import onboarding_mcp as o

SCRIPT = Path(__file__).resolve().parent.parent / "server" / "onboarding_mcp.py"

STEPS = [
    {"id": "one-thing", "text": "Set up one thing while they watch."},
    {"id": "tour", "text": "Show them where it lives."},
    {"id": "wrap-up", "text": "Up to two ideas, then USER.md."},
]


def _call(arguments, steps=STEPS):
    reply = o.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "step_done", "arguments": arguments}}, steps)
    return reply["result"]["isError"], reply["result"]["content"][0]["text"]


# --- results ----------------------------------------------------------------------

def test_a_finished_step_hands_over_the_next_one():
    is_error, text = _call({"step": "one-thing", "outcome": "done", "note": "a reminder"})
    assert not is_error
    assert text == "Next, onboarding step tour: Show them where it lives."


def test_a_skipped_step_hands_over_the_next_one_too():
    assert _call({"step": "tour", "outcome": "skipped"}) == (
        False, "Next, onboarding step wrap-up: Up to two ideas, then USER.md.")


def test_the_last_step_ends_onboarding():
    is_error, text = _call({"step": "wrap-up", "outcome": "done"})
    assert not is_error
    assert text == "Onboarding is finished. Close in a line; no new list of things to try."


def test_a_step_that_is_not_in_the_list_is_pointed_back_to_the_current_one():
    # A step already passed, or one that never existed: the first entry is
    # where the user is.
    for step in ("name", "tour-", "ONE-THING"):
        assert _call({"step": step, "outcome": "done"}) == (
            False, "That isn't the current onboarding step; the current one is one-thing.")


def test_a_later_step_in_the_list_answers_from_its_own_position():
    # The list runs from the current step to the last, so a later id is in it
    # and the answer follows that id. Whether it counts is the Marlow server's call.
    assert _call({"step": "tour", "outcome": "done"})[1].startswith("Next, onboarding step wrap-up")


def test_an_empty_list_means_onboarding_is_already_finished():
    assert _call({"step": "tour", "outcome": "done"}, steps=[]) == (False, "Onboarding is already finished.")


@pytest.mark.parametrize("arguments, fragment", [
    ({"outcome": "done"}, "step"),
    ({"step": "", "outcome": "done"}, "step"),
    ({"step": 3, "outcome": "done"}, "step"),
    ({"step": "tour"}, "outcome"),
    ({"step": "tour", "outcome": "finished"}, "outcome"),
    ({"step": "tour", "outcome": "done", "note": 5}, "note"),
    ({"step": "tour", "outcome": "done", "extra": 1}, "extra"),
    ({}, "step"),
])
def test_malformed_arguments_are_a_tool_error_naming_the_problem(arguments, fragment):
    is_error, text = _call(arguments)
    assert is_error is True
    assert fragment in text and len(text) < 200


def test_arguments_that_are_not_an_object_are_a_tool_error_not_a_crash():
    reply = o.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "step_done", "arguments": "tour"}}, STEPS)
    assert reply["result"]["isError"] is True


def test_an_unknown_tool_is_a_protocol_error():
    reply = o.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "nope", "arguments": {}}}, STEPS)
    assert reply["error"]["code"] == -32602


# --- protocol ---------------------------------------------------------------------

def test_tools_list_has_the_one_tool_with_a_strict_schema():
    reply = o.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, STEPS)
    (tool,) = reply["result"]["tools"]
    assert tool["name"] == "step_done"
    assert "same reply" in tool["description"]
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["step", "outcome"]
    assert schema["properties"]["step"]["type"] == "string"
    assert schema["properties"]["outcome"]["type"] == "string"
    assert schema["properties"]["outcome"]["enum"] == ["done", "skipped"]
    assert schema["properties"]["note"]["type"] == "string"
    assert set(schema["properties"]) == {"step", "outcome", "note"}


def test_initialize_echoes_a_known_protocol_version_and_offers_tools():
    reply = o.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-03-26"}}, STEPS)
    assert reply["result"]["protocolVersion"] == "2025-03-26"
    assert reply["result"]["capabilities"] == {"tools": {}}
    assert reply["result"]["serverInfo"]["name"] == "onboarding"


def test_unknown_protocol_version_gets_ours():
    reply = o.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "1999"}}, STEPS)
    assert reply["result"]["protocolVersion"] == o.PROTOCOL_VERSIONS[0]


def test_a_notification_gets_no_reply():
    assert o.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, STEPS) is None
    assert o.handle(["not", "a", "dict"], STEPS) is None


def test_ping_and_an_unknown_method():
    assert o.handle({"jsonrpc": "2.0", "id": 5, "method": "ping"}, STEPS)["result"] == {}
    reply = o.handle({"jsonrpc": "2.0", "id": 6, "method": "resources/list"}, STEPS)
    assert reply["error"]["code"] == -32601


def test_a_bug_in_the_handler_costs_one_answer_not_the_server(monkeypatch):
    monkeypatch.setattr(o, "step_done", lambda *_a: 1 / 0)
    is_error, text = _call({"step": "tour", "outcome": "done"})
    assert is_error and "ZeroDivisionError" in text


# --- --steps ----------------------------------------------------------------------

def test_steps_parse_from_json():
    assert o.parse_steps(["--steps", json.dumps(STEPS)]) == STEPS


@pytest.mark.parametrize("argv", [
    [],
    ["--steps"],
    ["--steps", "not json"],
    ["--steps", '{"id": "a", "text": "b"}'],
    ["--steps", '[{"id": "a"}]'],
    ["--steps", '[{"id": "a", "text": 3}]'],
    ["--steps", '[{"id": "", "text": "b"}]'],
    ["--steps", '["a"]'],
    ["--steps", json.dumps(STEPS[:1] + [{"id": 4, "text": "x"}])],
])
def test_missing_or_invalid_steps_are_an_empty_list(argv):
    assert o.parse_steps(argv) == []


def test_steps_with_a_log_flag_after_them_still_parse():
    assert o.parse_steps(["--steps", json.dumps(STEPS), "--log", "/x/y.log"]) == STEPS


def test_a_bad_steps_argument_is_logged_when_there_is_a_log(tmp_path):
    log_file = tmp_path / "onboarding.log"
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--steps", "{", "--log", str(log_file)],
        input="", capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    assert "--steps" in log_file.read_text()


# --- a real pipe ------------------------------------------------------------------

def _converse(argv, *messages):
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *argv],
        input="\n".join(messages) + "\n", capture_output=True, text=True, timeout=20,
    )
    assert done.returncode == 0, done.stderr
    return [json.loads(line) for line in done.stdout.splitlines()]


def test_stdio_round_trip_over_a_real_pipe():
    replies = _converse(
        ["--steps", json.dumps(STEPS)],
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),  # no reply
        "this is not json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "step_done", "arguments": {"step": "one-thing", "outcome": "done"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "step_done", "arguments": {"step": "one-thing"}}}),
    )
    assert [r.get("id") for r in replies] == [1, None, 2, 3, 4]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[1]["error"]["code"] == -32700
    assert [t["name"] for t in replies[2]["result"]["tools"]] == ["step_done"]
    assert replies[3]["result"]["content"][0]["text"] == "Next, onboarding step tour: Show them where it lives."
    assert replies[4]["result"]["isError"] is True


def test_stdio_with_no_steps_argument_starts_and_says_onboarding_is_finished():
    replies = _converse(
        [],
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "step_done", "arguments": {"step": "name", "outcome": "done"}}}),
    )
    assert replies[0]["result"]["content"][0]["text"] == "Onboarding is already finished."


def test_the_file_imports_nothing_of_ours():
    # Run as `python -I` from a directory the agent writes: no package path.
    source = SCRIPT.read_text()
    assert "from server" not in source and "import server" not in source


# --- policy lockstep ----------------------------------------------------------

def test_the_tool_is_vetted_allowed_and_named_as_the_server_reads_it():
    """The hook vets it (no phone prompt mid-setup), settings allow it, and
    invoke/queue look for the same name in the stream. It takes no path and
    reaches nothing, so it is vetted like the weather tools."""
    from server import boundaries
    from server.invoke import ONBOARDING_TOOL

    name = f"mcp__onboarding__{o.TOOL['name']}"
    assert name == ONBOARDING_TOOL
    vetted = {t for t in boundaries.VETTED_PATHLESS_TOOLS if t.startswith("mcp__onboarding__")}
    assert vetted == {name}
    policy = json.loads((Path(__file__).resolve().parent.parent / "deploy" / "marlow-settings.json").read_text())
    assert name in policy["permissions"]["allow"]


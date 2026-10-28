"""hooks/pretooluse.py (plan §4). Two layers tested:
  - boundary_deny_reason: pure logic, no server needed.
  - the full script as a real subprocess against a real running server —
    this is what actually exercises the HTTP contract the CLI's PreToolUse
    hook depends on, not just the Python functions underneath it.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from hooks.pretooluse import boundary_deny_reason
from server.app import internal_app
from server.config import settings as base_settings
from server.log import DENY_LOG_NAME
from server import active_turn
from server.permissions import permission_registry

# A socket path nobody serves: the hook's call fails and it must deny.
_NOTHING_LISTENING = "/tmp/marlow-test-nothing-listening.sock"

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- boundary_deny_reason: pure logic ---------------------------------------

def test_flags_secret_path_reads():
    reason = boundary_deny_reason("Read", {"file_path": str(base_settings.credentials_path)})
    assert reason is not None and "credential or private key" in reason


def test_flags_rm_rf_bash_commands():
    reason = boundary_deny_reason("Bash", {"command": "rm -rf /tmp/x"})
    assert reason is not None and "bulk delete" in reason


def test_ordinary_bash_command_not_flagged():
    assert boundary_deny_reason("Bash", {"command": "ls -la"}) is None


def test_flags_git_writes_as_harness_owned():
    """The deny has to be reachable through the hook, not just correct in
    bash_policy — an `allow` in settings.json is inert unless the hook agrees,
    and the reverse is what matters here."""
    reason = boundary_deny_reason("Bash", {"command": "git commit -m 'note'"})
    assert reason is not None and "made automatically" in reason


def test_git_reads_are_not_flagged():
    """Marlow reviewing the history of its own memory is the reason `git` is on
    the vetted-command allowlist at all; the deny must not take that away."""
    assert boundary_deny_reason("Bash", {"command": "git log --oneline -5"}) is None
    assert boundary_deny_reason("Bash", {"command": "git diff HEAD~1"}) is None


@pytest.mark.parametrize("tool", ["Edit", "Write"])
@pytest.mark.parametrize("name", ["CLAUDE.md", "FIRST-RUN.md"])
def test_product_owned_rules_cannot_be_edited(tool, name):
    """update.sh replaces these on every deploy (docs/tasks/box-upgrades.md),
    so an edit is lost silently -- and the CLAUDE.md one is the agent rewriting
    its own rules. The reason names the file the edit belongs in."""
    reason = boundary_deny_reason(tool, {"file_path": str(base_settings.working_dir / name)})
    assert reason is not None and "PREFERENCES.md" in reason


def test_product_owned_rules_deny_survives_dot_dot():
    disguised = base_settings.working_dir / "memory" / ".." / "CLAUDE.md"
    assert boundary_deny_reason("Edit", {"file_path": str(disguised)}) is not None


def test_product_owned_rules_deny_a_relative_path():
    """The CLI resolves a relative path against cwd, which is the working dir."""
    assert boundary_deny_reason("Edit", {"file_path": "CLAUDE.md"}) is not None


def test_product_owned_rules_stay_readable():
    """The split from AGENT_CONFIG_PATHS: CLAUDE.md is the agent's own
    instructions and the app shows it. A read deny would hide both."""
    assert boundary_deny_reason("Read", {"file_path": str(base_settings.working_dir / "CLAUDE.md")}) is None
    assert boundary_deny_reason("Grep", {"path": str(base_settings.working_dir / "CLAUDE.md")}) is None


def test_preferences_are_not_hard_denied():
    """The user's half: an edit asks the phone, like SOUL.md and USER.md."""
    path = str(base_settings.working_dir / "PREFERENCES.md")
    assert boundary_deny_reason("Edit", {"file_path": path}) is None


def test_ordinary_read_not_flagged():
    assert boundary_deny_reason("Read", {"file_path": str(base_settings.working_dir / "notes.md")}) is None


# --- step 2: hard denies get logged -----------------------------------------

def test_hard_deny_is_logged(tmp_path):
    """Today's gap: a hard deny is decided inside this process and returns
    without ever POSTing anywhere, so it left no server-side trace at all --
    the only denial that got logged was the unattended branch in server/app.py.
    This is the evidence stream the promotion rule in
    docs/tasks/marlow-harness-affordances.md needs."""
    out = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}, "session_id": "s"},
        env_overrides={"MARLOW_LOG_DIR": str(tmp_path)},
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    log_file = tmp_path / DENY_LOG_NAME
    assert log_file.exists(), "hard deny should have logged to the denial log"
    contents = log_file.read_text()
    assert "denied/hard Bash" in contents
    assert "rm -rf /tmp/x" in contents


def test_a_server_that_never_answers_is_logged_as_its_own_class(tmp_path):
    """The one denial class that cannot be logged server-side, because the
    server is the thing that failed. Before this it was invisible everywhere:
    an outage denied every approval-needing tool and left no record that the
    permission system -- rather than Mike -- was the thing saying no.

    Port 1 is unbindable, so request_decision's except branch fires exactly as
    it would against a wedged or stopped server. The command must be one the
    vetted-allow fast path does NOT clear, or the hook answers locally and
    never makes the HTTP call this test is about."""
    _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "curl https://example.com"}, "session_id": "s"},
        env_overrides={"MARLOW_LOG_DIR": str(tmp_path), "MARLOW_INTERNAL_SOCKET": _NOTHING_LISTENING},
    )
    contents = (tmp_path / DENY_LOG_NAME).read_text()
    assert "denied/no-answer Bash: curl https://example.com" in contents
    # Not misfiled as a decision Mike made -- that is the whole point of the
    # class split (server/deny_detail.py).
    assert "denied/declined" not in contents


def test_hard_deny_does_not_write_the_servers_rotating_log(tmp_path):
    """The denial log is a separate file deliberately, and this is the half that
    would regress silently if someone "tidied" it back onto get_logger().

    server/log.py's shared handler is a RotatingFileHandler, which is not
    multi-process safe; the server holds it open for its whole life while this
    hook is a fresh process per tool call, so a rollover reached by both can
    leave the server appending to an unlinked inode. Writing denials anywhere
    near that handler is the bug -- see server/log.get_deny_logger.
    """
    _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}, "session_id": "s"},
        env_overrides={"MARLOW_LOG_DIR": str(tmp_path)},
    )
    assert not (tmp_path / "marlow-server.log").exists(), (
        "a hard deny must not touch the server's rotating log file"
    )


def test_hard_deny_logging_failure_does_not_crash_the_hook(tmp_path):
    """The hook's contract is "always exits 0, always emits exactly one
    decision" -- a logging failure must not become a second crash path. Point
    MARLOW_LOG_DIR at a plain file so settings.ensure_dirs()'s mkdir() raises;
    the hook must still deny for the real reason, not "hook error"."""
    not_a_dir = tmp_path / "not-a-directory"
    not_a_dir.write_text("x")
    out = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}, "session_id": "s"},
        env_overrides={"MARLOW_LOG_DIR": str(not_a_dir)},
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "hook error" not in reason
    assert "never allowed" in reason


# --- real subprocess, no server needed (boundary check short-circuits) -----

def _run_hook(payload: dict, env_overrides: dict | None = None) -> dict:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), **(env_overrides or {})}
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "hooks" / "pretooluse.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_hook_denies_boundary_violation_without_needing_a_server():
    out = _run_hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}, "session_id": "s"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    # Regression test: this string reaches the model, and the 2026-08-04 lesson
    # (see hooks/pretooluse.py) is that naming an internal module here reads
    # as spoofed tool output rather than an explanation.
    assert "boundaries.py" not in reason
    assert "never allowed" in reason


def test_hook_fails_closed_on_unparseable_stdin_instead_of_crashing():
    # The hook's own contract is "always exits 0" — a crash here would make
    # Claude treat the hook itself as broken instead of getting a decision.
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "hooks" / "pretooluse.py")],
        input="not json at all",
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_fails_closed_when_server_unreachable():
    # Nothing listening on this socket -> the hook's HTTP call errors -> deny.
    # The command must be one the vetted-allow fast path does NOT clear, or the
    # hook answers locally and never makes the HTTP call this test is about.
    out = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "curl https://example.com"}, "session_id": "s"},
        env_overrides={"MARLOW_INTERNAL_SOCKET": _NOTHING_LISTENING},
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_vetted_command_is_allowed_without_reaching_the_server():
    """The fast path's whole point. Pointing at a dead socket proves it decides
    locally: if it POSTed, the unreachable-server path above would deny."""
    out = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git status"}, "session_id": "s"},
        env_overrides={"MARLOW_INTERNAL_SOCKET": _NOTHING_LISTENING},
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


@pytest.mark.parametrize("tool_name,tool_input", [
    ("WebSearch", {"query": "alberta politics this week"}),
    ("WebFetch", {"url": "https://example.com/x", "prompt": "summarise"}),
    ("ToolSearch", {"query": "select:WebSearch"}),
    ("mcp__claude_ai_Google_Calendar__list_events", {"calendar_id": "primary"}),
])
def test_pathless_tools_are_allowed_without_reaching_the_server(tool_name, tool_input):
    """The third fast path, proven the same way as the Bash one: a dead socket
    means a POST would deny, so an allow can only have been decided locally.

    This is what unblocks the morning job. Unattended these did not merely
    prompt -- there is no phone in cron, so each call burned the 55s window and
    failed closed (found on the box 2026-08-06, with all four sitting in
    `permissions.allow`).
    """
    out = _run_hook(
        {"tool_name": tool_name, "tool_input": tool_input, "session_id": "s"},
        env_overrides={"MARLOW_INTERNAL_SOCKET": _NOTHING_LISTENING},
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_a_pathless_tool_does_not_outrank_the_hard_denies():
    """Ordering, which is the whole safety argument for a fast path. A vetted
    tool name must not carry a secret path past boundaries.py -- the deny runs
    first and the membership test never gets asked."""
    out = _run_hook(
        {
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://x/", "file_path": "~/.ssh/id_ed25519"},
            "session_id": "s",
        },
        env_overrides={"MARLOW_INTERNAL_SOCKET": _NOTHING_LISTENING},
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "credential or private key" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_secret_path_in_a_bash_argument_is_denied():
    """/security-review 2026-08-05: `git diff --no-index` read ~/.ssh straight
    past a settings.json that denies Read(~/.ssh/**), because the hook only
    checked the structured file_path keys and never the command's own arguments.
    """
    out = _run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git diff --no-index ~/.ssh/id_ed25519 /dev/null"},
            "session_id": "s",
        },
        env_overrides={"MARLOW_INTERNAL_SOCKET": _NOTHING_LISTENING},
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "credential or private key" in out["hookSpecificOutput"]["permissionDecisionReason"]


# --- real subprocess against a real running internal_app -------------------

@pytest.fixture
def running_internal_server():
    sock = str(Path(tempfile.mkdtemp(prefix="mt-", dir="/tmp")) / "internal.sock")
    config = uvicorn.Config(internal_app, uds=sock, log_level="warning")
    server = uvicorn.Server(config)
    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    assert server.started, "internal_app test server never came up"

    # Both tests below exercise the phone round-trip, which as of 2026-08-07
    # only happens for an attended turn -- an unattended one is denied outright
    # without ever reaching the registry (server/active_turn.py). In production
    # that state is implied: the hook can only run from inside a live
    # invocation, and run_claude publishes the source around it. Here there is
    # no invocation, so the fixture has to say it. The server runs in a thread
    # of THIS process, so the module global is the same object it reads.
    with active_turn.running("chat"):
        yield sock, loop

    loop.call_soon_threadsafe(setattr, server, "should_exit", True)
    thread.join(timeout=5)


def test_hook_fails_closed_on_an_internal_error():
    """The "always exits 0" contract, for every failure and not just bad stdin.
    `~nosuchuser` raised out of expanduser() inside the secret-path scan, so the
    hook died with exit 1 having examined nothing -- and Claude treats a
    non-zero hook exit as a non-blocking error, i.e. it may run the tool.
    """
    out = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "cat ~nosuchuser/x"}, "session_id": "s"},
        env_overrides={"MARLOW_INTERNAL_SOCKET": _NOTHING_LISTENING},
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_full_round_trip_allow(running_internal_server):
    sock, loop = running_internal_server
    env = {"MARLOW_INTERNAL_SOCKET": sock}

    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "hooks" / "pretooluse.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(REPO_ROOT), **env},
    )
    # Not a vetted command -- see test_hook_fails_closed_when_server_unreachable.
    proc.stdin.write(json.dumps({"tool_name": "Bash", "tool_input": {"command": "curl https://example.com"}, "session_id": "s1"}))
    proc.stdin.close()

    deadline = time.time() + 5
    req = None
    while time.time() < deadline:
        pending = permission_registry.pending()
        if pending:
            req = pending[0]
            break
        time.sleep(0.02)
    assert req is not None, "hook's request never reached the server"
    assert req.tool == "Bash"

    # answer() resolves an asyncio.Future owned by the server thread's loop —
    # must run on that loop, not this (the test) thread.
    loop.call_soon_threadsafe(permission_registry.answer, req.request_id, "allow")

    # Not communicate(): we already wrote+closed stdin ourselves above (so the
    # hook's json.load(stdin) sees EOF right away instead of blocking for
    # more input); communicate() re-touches self.stdin regardless of any
    # `input=` arg and raises ValueError on the already-closed file.
    proc.wait(timeout=5)
    stdout, stderr = proc.stdout.read(), proc.stderr.read()
    assert proc.returncode == 0, stderr
    out = json.loads(stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    # Regression test: this string reaches the model as the reason a tool
    # call succeeded/failed. It used to be "phone round-trip via
    # /internal/permission-request" — engineering jargon that reads like
    # injected/spoofed tool output rather than an explanation, and Marlow
    # flagged it as suspicious instead of just noting the denial (found
    # 2026-08-03 testing this branch on a real device).
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "Approved by the user via the phone approval prompt."


def test_hook_full_round_trip_explicit_deny(running_internal_server):
    sock, loop = running_internal_server
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "hooks" / "pretooluse.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "MARLOW_INTERNAL_SOCKET": sock},
    )
    # `Task` rather than WebFetch: WebFetch is vetted locally now (Mike's
    # 2026-08-07 call) and would never reach the server, which is what this
    # test needs it to do. Task is on no allowlist in either settings file.
    proc.stdin.write(json.dumps({"tool_name": "Task", "tool_input": {"prompt": "go"}, "session_id": "s2"}))
    proc.stdin.close()

    deadline = time.time() + 5
    req = None
    while time.time() < deadline:
        pending = [r for r in permission_registry.pending() if r.tool == "Task"]
        if pending:
            req = pending[0]
            break
        time.sleep(0.02)
    assert req is not None

    loop.call_soon_threadsafe(permission_registry.answer, req.request_id, "deny")

    proc.wait(timeout=5)
    stdout, stderr = proc.stdout.read(), proc.stderr.read()
    assert proc.returncode == 0, stderr
    out = json.loads(stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == "Not approved: the user did not approve this tool use via the phone approval prompt."
    assert "internal/permission-request" not in reason, "must not surface our own endpoint path to the model"


def test_an_unattended_denial_does_not_claim_mike_declined(running_internal_server):
    """The reason string reaches the MODEL, and from there Mike's morning digest.

    Every denial used to emit "the user did not approve this tool use via the
    phone approval prompt". For a scheduled run that is a false statement about
    Mike delivered to Mike: the server now denies unattended turns outright, so
    no prompt is ever raised and nobody declines anything. Marlow would have
    reported that Mike turned down an action he was never asked about.

    Same class as the 2026-08-04 fix, where a reason string worded as engineering
    jargon read to the model as spoofed tool output rather than an explanation.
    """
    sock, _loop = running_internal_server
    # Nested inside the fixture's "chat" -- the inner source wins for the
    # duration, which is also what makes this a live check of `running`'s
    # save/restore.
    with active_turn.running("cron"):
        out = _run_hook(
            {
                "tool_name": "mcp__claude_ai_Gmail__send_message",
                "tool_input": {"to": "stranger@example.com"},
                "session_id": "cron-session",
            },
            env_overrides={"MARLOW_INTERNAL_SOCKET": sock},
        )

    decision = out["hookSpecificOutput"]["permissionDecision"]
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert decision == "deny"
    assert "did not approve" not in reason, (
        f"claims Mike declined a prompt that was never raised: {reason!r}"
    )
    assert "nobody" in reason.lower(), reason
    # The reason is written for the model, so it must not leak our internals --
    # the same guard the attended path already carries.
    assert "internal/permission-request" not in reason
    assert "active_turn" not in reason


def test_an_attended_denial_still_says_the_user_did_not_approve(running_internal_server):
    """The other side of the branch: a real prompt that Mike declines (or lets
    time out) must keep its original wording. The unattended case is the
    exception, not a replacement."""
    sock, loop = running_internal_server

    def _deny_when_it_shows_up():
        deadline = time.time() + 5
        while time.time() < deadline:
            pending = permission_registry.pending()
            if pending:
                loop.call_soon_threadsafe(
                    permission_registry.answer, pending[0].request_id, "deny"
                )
                return
            time.sleep(0.02)
        raise AssertionError("permission request never reached the server")

    answerer = threading.Thread(target=_deny_when_it_shows_up, daemon=True)
    answerer.start()
    out = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "curl https://example.com"}, "session_id": "s1"},
        env_overrides={"MARLOW_INTERNAL_SOCKET": sock},
    )
    answerer.join(timeout=5)

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "did not approve" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_similar_grant_allow_says_no_prompt_was_shown(running_internal_server, monkeypatch):
    """"Allow similar for this reply" clears later calls with nobody looking.
    The reason must not claim Mike approved THIS call via a prompt -- the same
    honesty rule as the unattended string above, pointed the other way."""
    from server import app as app_module
    from server.permissions import SimilarGrants

    sock, _loop = running_internal_server
    grants = SimilarGrants()
    grants.grant("t1", ("Bash", "find"))
    monkeypatch.setattr(app_module, "similar_grants", grants)
    monkeypatch.setattr(app_module.work_queue, "running_turn_id", lambda: "t1")
    out = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "find / -name x"}, "session_id": "s"},
        env_overrides={"MARLOW_INTERNAL_SOCKET": sock},
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "calls like this for the rest of this reply" in reason
    assert "prompt" not in reason


def test_a_command_naming_the_admin_socket_is_denied() -> None:
    """Behind the bwrap tmpfs, which is the real boundary: with confinement off
    this is all that stops one approved call minting itself a device token."""
    from server.config import settings

    for sock in (str(settings.admin_socket_path), "~/.marlow/admin/admin.sock"):
        cmd = f"curl --unix-socket {sock} -X POST http://localhost/internal/admin/invite"
        assert boundary_deny_reason("Bash", {"command": cmd}) is not None

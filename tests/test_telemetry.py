"""Tests for per-invocation usage telemetry (docs/tasks/usage-telemetry.md).

The load-bearing one is `test_telemetry_line_contains_no_prompt_or_result_text`:
logging is a classic quiet-leak path and this repo has an explicit
no-personal-content-ever posture. It asserts against distinctive sentinels
rather than eyeballing the format, so a future field that starts carrying
content fails the suite instead of shipping.
"""
import asyncio
import dataclasses
import importlib
import json
import os
import pathlib
import textwrap

import pytest

from conftest import write_executable
import server.config
import server.telemetry as telemetry_module
from server.config import settings as real_settings
from server.invoke import INVOKE_ERROR_SUBTYPE, InvokeError, run_claude
from server.telemetry import identity_fingerprint, record_invocation

PROMPT_SENTINEL = "ZZQPROMPTSENTINELQZZ-mikes-private-question"
RESULT_SENTINEL = "ZZQRESULTSENTINELQZZ-marlows-private-answer"
IDENTITY_SENTINEL = "ZZQIDENTITYSENTINELQZZ-mikes-private-memory"


@pytest.fixture(autouse=True)
def _reset_telemetry_clock():
    telemetry_module._reset_for_tests()
    yield
    telemetry_module._reset_for_tests()


@pytest.fixture
def telemetry_file(tmp_path, monkeypatch):
    """Points telemetry at a temp file and clears the cached logger handler."""
    import logging

    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(
        "server.telemetry.settings",
        dataclasses.replace(real_settings, telemetry_path=path, telemetry_enabled=True),
    )
    logger = logging.getLogger("marlow.telemetry")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    yield path
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _base_kwargs(**overrides):
    kwargs = dict(
        usage={"input_tokens": 10, "output_tokens": 20},
        source="chat",
        turn_id="t-1",
        session_id="s-1",
        resume=False,
        identity_chars=100,
        identity_hash="abc123def456",
        started_monotonic=1000.0,
        duration_seconds=2.0,
        is_error=False,
    )
    kwargs.update(overrides)
    return kwargs


# --- the privacy constraint -------------------------------------------------


def test_telemetry_line_contains_no_prompt_or_result_text(tmp_path, monkeypatch, telemetry_file):
    """End-to-end: a real invocation whose prompt, result, and identity are all
    distinctive sentinels must produce a telemetry line containing none of them."""
    import logging

    monkeypatch.setenv("MARLOW_SOUL_PATH", str(tmp_path / "SOUL.md"))
    (tmp_path / "SOUL.md").write_text(IDENTITY_SENTINEL)
    (tmp_path / "USER.md").write_text("user file")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            print(json.dumps({{
                "is_error": False,
                "subtype": "success",
                "result": {RESULT_SENTINEL!r},
                "session_id": "s1",
                "permission_denials": [],
                "usage": {{"input_tokens": 5, "output_tokens": 7,
                           "cache_read_input_tokens": 58000,
                           "cache_creation_input_tokens": 0}},
            }}))
            """
        ))

    patched = dataclasses.replace(
        real_settings,
        claude_bin=str(fake_bin),
        soul_path=tmp_path / "SOUL.md",
        user_path=tmp_path / "USER.md",
        memory_dir=memory_dir,
    )
    monkeypatch.setattr("server.invoke.settings", patched)
    monkeypatch.setattr("server.identity.settings", patched, raising=False)

    result = asyncio.run(run_claude(PROMPT_SENTINEL, source="chat", turn_id="t-9"))
    assert result.result_text == RESULT_SENTINEL

    logging.getLogger("marlow.telemetry").handlers[0].flush()
    raw = telemetry_file.read_text()

    assert PROMPT_SENTINEL not in raw
    assert RESULT_SENTINEL not in raw
    assert IDENTITY_SENTINEL not in raw

    line = _lines(telemetry_file)[0]
    # The numbers we actually wanted did survive.
    assert line["cache_read_input_tokens"] == 58000
    assert line["input_tokens"] == 5
    assert line["source"] == "chat"
    assert line["turn_id"] == "t-9"


def test_identity_fingerprint_is_a_length_and_a_short_digest():
    chars, digest = identity_fingerprint(IDENTITY_SENTINEL)
    assert chars == len(IDENTITY_SENTINEL)
    assert len(digest) == 12
    assert IDENTITY_SENTINEL not in digest
    # Same input, same digest — it must be stable enough to compare across turns.
    assert identity_fingerprint(IDENTITY_SENTINEL)[1] == digest
    assert identity_fingerprint(IDENTITY_SENTINEL + "!")[1] != digest


# --- usage parsing off the envelope -----------------------------------------


def _fake_claude_returning(tmp_path, monkeypatch, envelope_extra: str):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            print(json.dumps({{
                "is_error": False,
                "subtype": "success",
                "result": "ok",
                "session_id": "s1",
                "permission_denials": [],
                {envelope_extra}
            }}))
            """
        ))
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(fake_bin))
    )


def test_usage_survives_onto_invocation_result(tmp_path, monkeypatch, telemetry_file):
    _fake_claude_returning(
        tmp_path, monkeypatch, '"usage": {"input_tokens": 11, "cache_read_input_tokens": 22},'
    )
    result = asyncio.run(run_claude("hello"))
    assert result.usage["input_tokens"] == 11
    assert result.usage["cache_read_input_tokens"] == 22


def test_phase0_fields_are_recorded_from_usage_and_queue(tmp_path, monkeypatch, telemetry_file):
    # The nesting is the point. This test previously fed a flat
    # `usage.thinking_tokens`, which the CLI has never emitted -- so it passed
    # for as long as the field existed while production logged None on every
    # single row. The fixture must quote the real envelope, not the shape the
    # code happened to expect. Verified against CLI 2.1.235 on the box.
    _fake_claude_returning(
        tmp_path,
        monkeypatch,
        '"usage": {"input_tokens": 11, "output_tokens_details": {"thinking_tokens": 42}},',
    )

    asyncio.run(run_claude("hello", queue_wait_seconds=7.8912))

    line = _lines(telemetry_file)[0]
    assert line["thinking_tokens"] == 42
    assert line["queue_wait_seconds"] == 7.891


def test_flat_thinking_tokens_is_ignored(tmp_path, monkeypatch, telemetry_file):
    """A top-level `usage.thinking_tokens` must NOT be read.

    Guards the regression directly: if someone "fixes" this back to a flat
    lookup, or adds a fallback to it, this goes red. There is no such key in
    the real envelope, so honouring one would only ever re-enable the silent
    None -- and a fallback would make the bug invisible again the moment a
    future CLI emitted a flat zero.
    """
    _fake_claude_returning(
        tmp_path, monkeypatch, '"usage": {"input_tokens": 11, "thinking_tokens": 42},'
    )

    asyncio.run(run_claude("hello"))

    assert _lines(telemetry_file)[0]["thinking_tokens"] is None


def test_thinking_tokens_absent_when_details_missing(tmp_path, monkeypatch, telemetry_file):
    """No `output_tokens_details` at all -> None, not a crash.

    invoke.py's docstring warns never to assume a usage key is present; this is
    that warning applied one level deeper, where the .get() chain could raise.
    """
    _fake_claude_returning(tmp_path, monkeypatch, '"usage": {"input_tokens": 11},')

    asyncio.run(run_claude("hello"))

    assert _lines(telemetry_file)[0]["thinking_tokens"] is None


def test_phase0_queue_wait_is_absent_for_direct_invocations(tmp_path, monkeypatch, telemetry_file):
    _fake_claude_returning(
        tmp_path, monkeypatch, '"usage": {"output_tokens_details": {"thinking_tokens": 42}},'
    )

    asyncio.run(run_claude("hello"))

    assert _lines(telemetry_file)[0]["queue_wait_seconds"] is None


def test_missing_usage_yields_empty_dict_not_crash(tmp_path, monkeypatch, telemetry_file):
    # An older CLI, or an error envelope, simply has no `usage` key.
    _fake_claude_returning(tmp_path, monkeypatch, "")
    result = asyncio.run(run_claude("hello"))
    assert result.usage == {}
    line = _lines(telemetry_file)[0]
    assert line["input_tokens"] is None


def test_malformed_usage_is_tolerated(tmp_path, monkeypatch, telemetry_file):
    # A non-dict `usage` must not become an AttributeError deep in telemetry.
    _fake_claude_returning(tmp_path, monkeypatch, '"usage": "not-a-dict",')
    result = asyncio.run(run_claude("hello"))
    assert result.usage == {}
    assert _lines(telemetry_file)[0]["output_tokens"] is None


def test_nonzero_exit_still_records_a_line(tmp_path, monkeypatch, telemetry_file):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, "#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n")
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(fake_bin))
    )

    result = asyncio.run(run_claude("hello"))

    assert result.exit_code == 3
    line = _lines(telemetry_file)[0]
    assert line["is_error"] is True
    assert line["input_tokens"] is None
    assert line["duration_seconds"] >= 0


# --- the inter-invocation gap -----------------------------------------------


def test_first_invocation_has_no_gap(telemetry_file):
    record = record_invocation(**_base_kwargs())
    assert record["gap_seconds"] is None, "a missing predecessor is not a zero gap"


def test_gap_measures_idle_time_between_invocations(telemetry_file):
    # First call runs 1000.0 -> 1002.0.
    record_invocation(**_base_kwargs(started_monotonic=1000.0, duration_seconds=2.0))
    # Second starts at 1010.0, so the *idle* window is 8s, not 10s start-to-start.
    second = record_invocation(**_base_kwargs(started_monotonic=1010.0, duration_seconds=1.0))
    assert second["gap_seconds"] == 8.0

    third = record_invocation(**_base_kwargs(started_monotonic=1015.0, duration_seconds=1.0))
    assert third["gap_seconds"] == 4.0  # 1015.0 - (1010.0 + 1.0)


def test_gap_is_none_again_after_a_restart(telemetry_file):
    record_invocation(**_base_kwargs())
    telemetry_module._reset_for_tests()  # stands in for a process restart
    assert record_invocation(**_base_kwargs())["gap_seconds"] is None


# --- writer behaviour -------------------------------------------------------


def test_disabled_telemetry_writes_nothing_but_still_returns_the_record(tmp_path, monkeypatch):
    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(
        "server.telemetry.settings",
        dataclasses.replace(real_settings, telemetry_path=path, telemetry_enabled=False),
    )
    record = record_invocation(**_base_kwargs())
    assert record["input_tokens"] == 10
    assert not path.exists()


def test_writer_failure_does_not_raise(monkeypatch, telemetry_file):
    # A broken log file is a lost measurement, never a failed invocation.
    monkeypatch.setattr(
        "server.telemetry._get_writer", lambda: (_ for _ in ()).throw(OSError("disk full"))
    )
    record = record_invocation(**_base_kwargs())
    assert record["turn_id"] == "t-1"


def test_records_are_one_json_object_per_line(telemetry_file):
    for i in range(3):
        record_invocation(**_base_kwargs(turn_id=f"t-{i}", started_monotonic=1000.0 + i * 10))
    lines = telemetry_file.read_text().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["turn_id"] for line in lines] == ["t-0", "t-1", "t-2"]


def test_telemetry_does_not_leak_into_the_main_server_log(telemetry_file):
    import logging

    logger = logging.getLogger("marlow.telemetry")
    record_invocation(**_base_kwargs())

    assert logger.propagate is False, "would duplicate machine records into marlow-server.log"
    # Exactly one handler of ours, and no stream handler — these records are
    # machine-readable data, not operator output. (Other handlers may be
    # present under pytest's log capture; only ours is asserted on.)
    ours = [h for h in logger.handlers if getattr(h, "_marlow_telemetry_handler", False)]
    assert len(ours) == 1
    assert ours[0].baseFilename == str(telemetry_file)


@pytest.fixture
def reloaded_config():
    """Reload server.config against the current env, then put the module back.

    The restoring reload has to run with the *original* environment, so this
    fixture snapshots and restores the vars itself rather than leaning on
    monkeypatch. monkeypatch is torn down after any fixture that requests it —
    and a reload at the end of the test body runs earlier still — so either
    would re-read the patched values and leave the module-global
    `server.config.settings` pointing at the probe directory for every later
    test in the session.
    """
    watched = ("MARLOW_LOG_DIR", "MARLOW_TELEMETRY_PATH")
    saved = {key: os.environ.get(key) for key in watched}

    yield lambda: importlib.reload(server.config).Settings()

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    importlib.reload(server.config)


def test_telemetry_path_defaults_inside_log_dir(monkeypatch, reloaded_config):
    """Regression: telemetry_path once hardcoded ~/.marlow/logs, so it ignored
    MARLOW_LOG_DIR and the suite wrote to the real home directory."""
    monkeypatch.setenv("MARLOW_LOG_DIR", "/tmp/marlow-log-dir-probe")
    monkeypatch.delenv("MARLOW_TELEMETRY_PATH", raising=False)

    fresh = reloaded_config()

    assert fresh.telemetry_path == fresh.log_dir / "usage.jsonl"


def test_explicit_telemetry_path_overrides_log_dir(monkeypatch, reloaded_config):
    monkeypatch.setenv("MARLOW_LOG_DIR", "/tmp/marlow-log-dir-probe")
    monkeypatch.setenv("MARLOW_TELEMETRY_PATH", "/tmp/elsewhere/usage.jsonl")

    fresh = reloaded_config()

    assert fresh.telemetry_path == pathlib.Path("/tmp/elsewhere/usage.jsonl")


def test_config_reload_reproduces_the_sandbox_settings(reloaded_config):
    """The invariant `reloaded_config`'s teardown depends on.

    A reload rebinds the module-global `server.config.settings` for the rest of
    the session, so the restoring reload only works because conftest puts the
    sandbox paths in `os.environ` rather than patching the settings object. If
    that ever changes, every reload in this file starts leaking the real
    ~/.marlow paths into later tests — fail here instead, where it's obvious.
    """
    original = server.config.settings.log_dir
    importlib.reload(server.config)
    assert server.config.settings.log_dir == original


# --- every exit path records --------------------------------------------


def test_timeout_still_advances_the_gap_clock(tmp_path, monkeypatch, telemetry_file):
    """The bug this guards: an invocation that raises InvokeError used to skip
    telemetry entirely, so the *next* turn's gap_seconds silently absorbed it —
    a 20-minute timeout turned a prompt follow-up into an apparent 20-minute
    idle period, corrupting the one measurement this file exists to take."""
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(fake_bin))
    )
    monkeypatch.setattr("server.invoke._SUBPROCESS_TIMEOUT_SECONDS", 0.2)

    with pytest.raises(InvokeError, match="exceeded"):
        asyncio.run(run_claude("hello"))

    line = _lines(telemetry_file)[0]
    assert line["is_error"] is True
    assert line["subtype"] == INVOKE_ERROR_SUBTYPE
    assert line["gap_seconds"] is None  # it was the first invocation
    assert line["input_tokens"] is None

    # The clock advanced, so the *next* invocation reports its own idle time
    # rather than the timeout's 0.2s.
    next_start = telemetry_module._last_finished_monotonic + 5.0
    second = record_invocation(**_base_kwargs(started_monotonic=next_start))
    assert second["gap_seconds"] == pytest.approx(5.0)


def test_unparseable_stdout_still_records(tmp_path, monkeypatch, telemetry_file):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, "#!/usr/bin/env python3\nprint('not json at all')\n")
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(fake_bin))
    )

    with pytest.raises(InvokeError, match="non-JSON"):
        asyncio.run(run_claude("hello"))

    line = _lines(telemetry_file)[0]
    assert line["subtype"] == INVOKE_ERROR_SUBTYPE
    assert line["is_error"] is True


def test_successful_invocation_records_exactly_once(tmp_path, monkeypatch, telemetry_file):
    """The finally-block guard must not double-record a path that already did."""
    _fake_claude_returning(tmp_path, monkeypatch, '"usage": {"input_tokens": 1},')
    asyncio.run(run_claude("hello"))
    assert len(_lines(telemetry_file)) == 1


def test_writer_follows_a_repointed_telemetry_path(tmp_path, monkeypatch, telemetry_file):
    """A cached handler from an earlier path would keep writing there, so a
    caller that repoints telemetry_path would assert against an empty file."""
    record_invocation(**_base_kwargs(turn_id="first"))
    assert [line["turn_id"] for line in _lines(telemetry_file)] == ["first"]

    moved = tmp_path / "moved" / "usage.jsonl"
    monkeypatch.setattr(
        "server.telemetry.settings",
        dataclasses.replace(real_settings, telemetry_path=moved, telemetry_enabled=True),
    )
    record_invocation(**_base_kwargs(turn_id="second"))

    assert [line["turn_id"] for line in _lines(moved)] == ["second"]
    assert [line["turn_id"] for line in _lines(telemetry_file)] == ["first"]


def test_context_tokens_is_recorded_beside_the_summed_counters(telemetry_file):
    # The summed counters and the true size are different quantities and both
    # are kept (docs/tasks/usage-visibility.md §3).
    record = record_invocation(**_base_kwargs(context_tokens=61_234))
    assert record["context_tokens"] == 61_234
    assert record["input_tokens"] == 10
    import logging

    logging.getLogger("marlow.telemetry").handlers[0].flush()
    assert _lines(telemetry_file)[0]["context_tokens"] == 61_234


def test_context_tokens_is_null_when_unknown(telemetry_file):
    assert record_invocation(**_base_kwargs())["context_tokens"] is None


def test_run_claude_records_the_streams_context_tokens(tmp_path, monkeypatch, telemetry_file):
    import logging

    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            print(json.dumps({"type": "assistant", "parent_tool_use_id": None,
                "message": {"content": [], "usage": {"input_tokens": 5,
                    "cache_read_input_tokens": 900, "cache_creation_input_tokens": 95}}}))
            print(json.dumps({"type": "result", "is_error": False, "subtype": "success",
                "result": "ok", "session_id": "s1", "permission_denials": [],
                "usage": {"input_tokens": 5, "cache_read_input_tokens": 3000}}))
            """
        ))
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(fake_bin))
    )
    asyncio.run(run_claude("hello", source="cron", on_tool_call=lambda c: None))
    logging.getLogger("marlow.telemetry").handlers[0].flush()
    line = _lines(telemetry_file)[0]
    assert line["context_tokens"] == 1000
    assert line["cache_read_input_tokens"] == 3000  # the summed counter is untouched

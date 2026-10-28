"""config.py is the one place paths/secrets-from-env are resolved; conftest
points every MARLOW_* var at the temp sandbox, so the singleton should reflect
exactly that. Also covers the working-dir / credentials separation the
two-location deploy relies on."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from server.config import Settings, settings


def test_singleton_reflects_sandbox_env() -> None:
    assert settings.working_dir == Path(os.environ["MARLOW_WORKING_DIR"])
    assert settings.credentials_path == Path(os.environ["MARLOW_CREDENTIALS_PATH"])
    assert settings.claude_bin == os.environ["MARLOW_CLAUDE_BIN"]


def test_working_dir_and_credentials_are_independent(monkeypatch) -> None:
    # The whole point of the split: the identity/working dir, the code location,
    # and the credentials path are separately overridable — moving one must not
    # move another.
    monkeypatch.setenv("MARLOW_WORKING_DIR", "/opt/brain")
    monkeypatch.setenv("MARLOW_CREDENTIALS_PATH", "/etc/creds/token.json")
    s = Settings()
    assert s.working_dir == Path("/opt/brain")
    assert s.credentials_path == Path("/etc/creds/token.json")
    assert s.credentials_path.parent != s.working_dir


def test_server_repo_is_independently_overridable(monkeypatch) -> None:
    monkeypatch.setenv("MARLOW_SERVER_REPO", "/opt/marlow/software")
    monkeypatch.setenv("MARLOW_WORKING_DIR", "/opt/marlow/identity")
    s = Settings()
    assert s.server_repo == Path("/opt/marlow/software")
    assert s.working_dir == Path("/opt/marlow/identity")


def test_model_and_effort_default_to_the_pinned_values(monkeypatch) -> None:
    # docs/tasks/model-and-effort-config.md piece 1: Sonnet (alias, not a
    # version string), pinned so the CLI's own default is never silently
    # inherited. Medium since 2026-10-02, matching the web and iOS defaults.
    monkeypatch.delenv("MARLOW_MODEL", raising=False)
    monkeypatch.delenv("MARLOW_EFFORT", raising=False)
    s = Settings()
    assert s.model == "sonnet"
    assert s.effort == "medium"


def test_model_and_effort_fall_back_when_env_is_set_but_empty(monkeypatch) -> None:
    # `Environment=MARLOW_EFFORT=` in a systemd unit reads as "" from
    # os.environ, not as absent. Without the `or` guard that reaches argv as
    # `--effort=` and fails every turn from every source.
    monkeypatch.setenv("MARLOW_MODEL", "")
    monkeypatch.setenv("MARLOW_EFFORT", "")
    monkeypatch.setenv("MARLOW_CRON_EFFORT", "")
    s = Settings()
    assert s.model == "sonnet"
    assert s.effort == "medium"
    assert s.cron_effort == "medium"


def test_cron_effort_defaults_to_medium(monkeypatch) -> None:
    # docs/tasks/model-and-effort-config.md piece 3.
    monkeypatch.delenv("MARLOW_CRON_EFFORT", raising=False)
    assert Settings().cron_effort == "medium"


def test_model_and_effort_env_override(monkeypatch) -> None:
    monkeypatch.setenv("MARLOW_MODEL", "opus")
    monkeypatch.setenv("MARLOW_EFFORT", "high")
    s = Settings()
    assert s.model == "opus"
    assert s.effort == "high"


def test_derived_state_paths_live_under_data_dir() -> None:
    assert settings.device_tokens_path.parent == settings.data_dir
    assert settings.pairing_challenges_path.parent == settings.data_dir


def test_tilde_expansion(monkeypatch) -> None:
    monkeypatch.setenv("MARLOW_WORKING_DIR", "~/somewhere")
    s = Settings()
    assert "~" not in str(s.working_dir)
    assert s.working_dir == Path.home() / "somewhere"


def test_tls_defaults(monkeypatch) -> None:
    monkeypatch.delenv("MARLOW_TLS_CERT", raising=False)
    monkeypatch.delenv("MARLOW_TLS_KEY", raising=False)
    monkeypatch.delenv("MARLOW_ALLOW_PLAINTEXT", raising=False)
    s = Settings()
    assert s.tls_cert_path == Path.home() / ".marlow/tls/marlow.crt"
    assert s.tls_key_path == Path.home() / ".marlow/tls/marlow.key"
    assert s.allow_plaintext is False


def test_allow_plaintext_requires_exact_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("MARLOW_ALLOW_PLAINTEXT", "1")
    assert Settings().allow_plaintext is True
    monkeypatch.setenv("MARLOW_ALLOW_PLAINTEXT", "true")
    assert Settings().allow_plaintext is False


def test_ensure_dirs_is_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MARLOW_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("MARLOW_LOG_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("MARLOW_ATTACHMENTS_DIR", str(tmp_path / "a"))
    s = Settings()
    s.ensure_dirs()
    assert s.data_dir.is_dir() and s.log_dir.is_dir() and s.attachments_dir.is_dir()
    # 0o700 — state and logs may hold tokens/redaction-adjacent data.
    assert (s.data_dir.stat().st_mode & 0o777) == 0o700
    assert (s.attachments_dir.stat().st_mode & 0o777) == 0o700


def test_attachments_dir_defaults_alongside_apns_not_under_data_dir(monkeypatch) -> None:
    """docs/tasks/ios-image-input.md's trap #1: the attachments dir must not
    live under data_dir, which boundaries.SECRET_PATHS hard-denies for Read and
    invoke.py hides behind a bwrap tmpfs -- either one alone would make the CLI
    unable to see an uploaded image, in a way that looks like a permission bug
    rather than a wrong path."""
    monkeypatch.delenv("MARLOW_ATTACHMENTS_DIR", raising=False)
    s = Settings()
    assert s.attachments_dir == Path.home() / ".marlow/attachments"
    assert s.data_dir not in s.attachments_dir.parents
    assert s.attachments_dir != s.data_dir


def test_apns_environment_defaults_to_sandbox(monkeypatch) -> None:
    monkeypatch.delenv("MARLOW_APNS_ENVIRONMENT", raising=False)
    assert Settings().apns_environment == "sandbox"


def test_apns_environment_accepts_production(monkeypatch) -> None:
    monkeypatch.setenv("MARLOW_APNS_ENVIRONMENT", "production")
    assert Settings().apns_environment == "production"


def test_apns_environment_typo_fails_loud_instead_of_silently_meaning_production(monkeypatch) -> None:
    # push.py does `apns_environment == "sandbox"` — anything else silently
    # means production. A typo/case mismatch must fail at Settings()
    # construction, not route real traffic to the wrong APNs environment.
    monkeypatch.setenv("MARLOW_APNS_ENVIRONMENT", "Sandbox")
    with pytest.raises(ValueError):
        Settings()


@pytest.mark.parametrize("bad", ["relative/entrypoint.py", "/opt/x y.py", "/opt/x;id.py", "/opt/x\n* * * * * id"])
def test_cron_entrypoint_must_be_a_plain_absolute_path(monkeypatch, bad) -> None:
    """It is written into a crontab command line."""
    monkeypatch.setenv("MARLOW_CRON_ENTRYPOINT", bad)
    with pytest.raises(ValueError, match="MARLOW_CRON_ENTRYPOINT"):
        Settings()

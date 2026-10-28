"""/health is what the watchdog and off-host dead-man's switch read. Its shape
must stay stable, and it must reflect auth state honestly."""
from __future__ import annotations

import importlib
from unittest.mock import patch

from server import health
from server.config import settings
from server.identity_git import GIT_HARDENING_FLAGS

EXPECTED_KEYS = {
    "status",
    "auth_ok",
    "reauth_prewarn",
    "queue_depth",
    "queue_paused",
    "oldest_pending_permission_age",
    "git_branch",
    "git_commit",
    "model",
    "effort",
}


def test_shape_is_stable() -> None:
    h = health.get_health()
    assert set(h) == EXPECTED_KEYS
    assert isinstance(h["queue_depth"], int)
    assert h["queue_paused"] is False  # not paused by default
    assert h["oldest_pending_permission_age"] is None  # no open requests by default
    assert isinstance(h["git_branch"], str)
    assert isinstance(h["git_commit"], str)
    assert h["model"] == settings.model
    assert h["effort"] == settings.effort


def test_reports_ok_with_valid_far_future_credentials() -> None:
    # conftest's fake credential has a year-2286 refreshTokenExpiresAt.
    h = health.get_health()
    assert h["status"] == "ok"
    assert h["auth_ok"] is True
    assert h["reauth_prewarn"] is False


def test_build_identity_is_captured_at_import() -> None:
    with patch.object(health.subprocess, "run") as git_run:
        first = health.get_health()
        second = health.get_health()

    git_run.assert_not_called()
    assert first["git_branch"] == second["git_branch"]
    assert first["git_commit"] == second["git_commit"]


def test_git_hardening_flags_are_passed() -> None:
    """`rev-parse` does not need them; the next arg someone adds might.

    Pinned rather than commented because the failure is silent: an unhardened
    index-refreshing git call runs a config-supplied command inside this
    process and reports nothing unusual.
    """
    with patch.object(health.subprocess, "run") as git_run:
        importlib.reload(health)

    assert git_run.call_args_list, "expected git to be invoked at import"
    for call in git_run.call_args_list:
        argv = call.args[0]
        for flag in GIT_HARDENING_FLAGS:
            assert flag in argv, f"{flag} missing from {argv}"

    importlib.reload(health)


def test_unexpected_git_error_falls_back_rather_than_killing_import() -> None:
    """The catch is broad on purpose -- see _git_value's docstring.

    A raise here escapes `import server.health`, so the server never binds and
    there is no /health left to report why. ValueError stands in for the real
    one: text=True decodes with the locale encoding, so output git considers
    fine can raise UnicodeDecodeError.
    """
    try:
        with patch.object(
            health.subprocess, "run", side_effect=ValueError("undecodable")
        ):
            reloaded = importlib.reload(health)

        assert reloaded.get_health()["git_commit"] == "unknown"
    finally:
        importlib.reload(health)


def test_git_failure_falls_back_to_unknown() -> None:
    try:
        with patch.object(health.subprocess, "run", side_effect=OSError("no git")):
            reloaded = importlib.reload(health)

        result = reloaded.get_health()
        assert result["git_branch"] == "unknown"
        assert result["git_commit"] == "unknown"
    finally:
        # Leave the imported singleton reflecting the real checkout for every
        # test that runs after this one, independent of test ordering.
        importlib.reload(health)

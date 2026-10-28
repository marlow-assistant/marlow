"""/health status surface (plan §3.1/§6) — used by watchdog + dead-man's switch."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from server import auth_monitor
from server.config import settings
from server.identity_git import GIT_HARDENING_FLAGS
from server.permissions import permission_registry
from server.queue import work_queue

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GIT_TIMEOUT_SECONDS = 5


def _git_value(*args: str) -> str:
    """One git query, reduced to a string that is never an exception.

    Carries GIT_HARDENING_FLAGS even though the only calls below are
    `rev-parse`, which refreshes no index and runs no hooks, so today the flags
    neutralise nothing. They are here so the invariant holds for whatever gets
    asked next: the obvious follow-on for a build-identity endpoint is "is the
    checkout dirty", and `describe --dirty` / `status` DO refresh the index,
    which is what makes a `core.fsmonitor` in .git/config execute a command --
    inside this process, at import, before anything else runs. See
    server/identity_git.py:84 for the family of keys and why they matter here.
    Add args freely; do not drop the flags.

    The except is deliberately broad because this runs at IMPORT (below), not
    per request. A narrow catch means an unforeseen failure propagates out of
    `import server.health` and the server never binds its port -- no /health to
    report the problem, the watchdog restart-looping every two minutes, and the
    dead-man's switch firing. "unknown" is always the better answer than a
    process that will not start; there is no exception worth escalating here.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *GIT_HARDENING_FLAGS, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return "unknown"
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# Frozen at import so /health identifies the code loaded by this process, not a
# newer commit that may have landed in the checkout without a successful restart.
_BUILD_IDENTITY = {
    "git_branch": _git_value("rev-parse", "--abbrev-ref", "HEAD"),
    "git_commit": _git_value("rev-parse", "--short", "HEAD"),
}


def get_health() -> dict[str, Any]:
    auth_status = auth_monitor.get_status()

    if not auth_status.credentials_found:
        status = "error"
    elif auth_status.reauth_needed:
        status = "reauth_needed"
    else:
        status = "ok"

    return {
        "status": status,
        "auth_ok": auth_status.auth_ok,
        "reauth_prewarn": auth_status.reauth_prewarn,
        "queue_depth": work_queue.depth,
        "queue_paused": work_queue.paused,
        "oldest_pending_permission_age": permission_registry.oldest_pending_age(),
        "model": settings.model,
        "effort": settings.effort,
        **_BUILD_IDENTITY,
    }

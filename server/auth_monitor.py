"""Token-expiry detection + pre-warn (plan §6/§3.7).

Reads the confirmed gate #1 credential shape directly — no guessing at a
schema, this is what gate1-results §6 observed on disk:

    {"claudeAiOauth": {"accessToken": ..., "refreshToken": ...,
                        "expiresAt": <epoch-ms>, "refreshTokenExpiresAt": <epoch-ms>,
                        "subscriptionType": "pro", "scopes": [...]}}

`refreshTokenExpiresAt` is the one that matters for reauth planning — it read
as anchored, not rolling, in gate #1 (~9-day window). `expiresAt` (access
token, ~8h) self-refreshes headlessly and needs no action.

Open item (gate1-results §6): the exact failure signature of an *expired*
refresh token hasn't been observed yet. This module's `reauth_needed` is
derived from the timestamp alone, not from an observed failure — treat it as
the authoritative signal; invoke.py's `_looks_like_auth_failure` is a
secondary, unverified heuristic on top.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from server.config import settings
from server.log import get_logger
from server.push import PushPayload, PushType
from server.push import broadcast_nowait as push_broadcast_nowait

logger = get_logger(__name__)


class AuthPhase(str, Enum):
    OK = "ok"
    PREWARN = "prewarn"
    NEEDED = "needed"


@dataclass
class AuthStatus:
    credentials_found: bool
    access_token_expires_at: datetime | None
    refresh_token_expires_at: datetime | None
    subscription_type: str | None

    @property
    def reauth_needed(self) -> bool:
        if self.refresh_token_expires_at is None:
            return not self.credentials_found  # no file at all = definitely needs auth
        return datetime.now(timezone.utc) >= self.refresh_token_expires_at

    @property
    def reauth_prewarn(self) -> bool:
        if self.refresh_token_expires_at is None:
            return False
        window = timedelta(hours=settings.auth_prewarn_hours)
        return datetime.now(timezone.utc) + window >= self.refresh_token_expires_at

    @property
    def auth_ok(self) -> bool:
        return self.credentials_found and not self.reauth_needed

    @property
    def phase(self) -> AuthPhase:
        """Collapses the three booleans above into the one value both the log
        throttle and the notifier key off. Order matters: `reauth_prewarn` is
        also true once the deadline has passed, so NEEDED must win."""
        if self.reauth_needed:
            return AuthPhase.NEEDED
        if self.reauth_prewarn:
            return AuthPhase.PREWARN
        return AuthPhase.OK


def _epoch_ms_to_dt(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _no_credentials(cause: str, reason: str) -> AuthStatus:
    status = AuthStatus(
        credentials_found=False,
        access_token_expires_at=None,
        refresh_token_expires_at=None,
        subscription_type=None,
    )
    _log_phase_transition(status, cause=cause, detail=reason)
    return status


def get_status() -> AuthStatus:
    if not settings.credentials_path.exists():
        return _no_credentials("missing", f"no credentials file at {settings.credentials_path}")

    try:
        raw = json.loads(settings.credentials_path.read_text())
        oauth = raw["claudeAiOauth"]
    except (json.JSONDecodeError, KeyError, OSError) as e:
        return _no_credentials("unparseable", f"failed to parse credentials file: {e}")

    status = AuthStatus(
        credentials_found=True,
        access_token_expires_at=_epoch_ms_to_dt(oauth.get("expiresAt")),
        refresh_token_expires_at=_epoch_ms_to_dt(oauth.get("refreshTokenExpiresAt")),
        subscription_type=oauth.get("subscriptionType"),
    )
    _log_phase_transition(status)
    return status


# Last phase `get_status()` logged, so a steady state logs once instead of once
# per call. get_status() is on the /health path, which the phone polls every
# few seconds while the app is open — the un-throttled version put 638 lines
# into an 8-hour window against 87 real ones (88% of the day's log), which is
# how a *later* investigation of nightly rollover ended up with an unusable
# log. Same reasoning as queue.py's pause heartbeat, which already does this.
# Keyed on (phase, cause), not phase alone: three materially different
# failures share phase NEEDED — an expired token, a missing credentials file,
# and an unparseable one. Keying on phase alone meant that once any of them
# had logged, a switch to a *different* cause was silently swallowed, leaving
# the log blaming the old one. That is a misdiagnosis in the log this throttle
# exists to make trustworthy.
#
# `cause` is a fixed word, deliberately not the message: the unparseable
# message embeds the exception text, and a file caught mid-write decodes to a
# different error each poll ("line 1 column 5" then "column 9"...), which
# would key every poll differently and let the flood straight back in.
_last_logged_key: tuple[AuthPhase, str | None] | None = None


def _log_phase_transition(
    status: AuthStatus, cause: str | None = None, detail: str | None = None
) -> None:
    global _last_logged_key
    phase = status.phase
    key = (phase, cause)
    if key == _last_logged_key:
        return
    _last_logged_key = key
    if detail is not None:
        # Missing or unparseable credentials file. Throttled like the rest —
        # /health reads this file on every poll, so an un-gated line here is
        # the same 600-lines-a-day problem in a worse mood.
        logger.warning("%s — interactive reauth needed", detail)
    elif phase is AuthPhase.NEEDED:
        logger.warning(
            "refresh token expired at %s — interactive reauth needed", status.refresh_token_expires_at
        )
    elif phase is AuthPhase.PREWARN:
        # warning, not info: this is the one auth line that wants a human. It
        # sat at info while the window was 24h from lapsing, which is below
        # the level anyone greps for.
        logger.warning(
            "refresh token expires %s — within pre-warn window", status.refresh_token_expires_at
        )
    else:
        logger.info("auth ok, refresh token expires %s", status.refresh_token_expires_at)


def reset_log_state() -> None:
    """Test seam — the throttle above is module state, and a test asserting on
    a transition must not inherit the phase an earlier test left behind."""
    global _last_logged_key
    _last_logged_key = None


# --- proactive notifier ------------------------------------------------------

class AuthWatcher:
    """Tells Mike his session is about to lapse *while he can still do
    something about it*.

    Before this, the only reauth notification in the system was queue.py's,
    fired when the work queue pauses — which is after expiry, and only if a
    turn happens to be waiting. A token that lapsed overnight with an empty
    queue produced no push at all, and the pre-warn window produced nothing
    but a log line nobody reads. This watcher runs on its own clock so the
    warning does not depend on traffic or on the phone having the app open.

    Deliberately not coordinated with queue.py's push: at worst an actual
    expiry sends one extra notification, which is the cheap side of the
    trade against a shared-state handshake between two pollers.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._notified_phase: AuthPhase | None = None
        self._notified_at: datetime | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            # Never let a bad poll kill the loop — a watcher that dies silently
            # is worse than no watcher, because it looks like everything is
            # fine right up until the morning the token lapses.
            try:
                self.check(get_status())
            except Exception:
                logger.exception("auth watch poll failed; continuing")
            await asyncio.sleep(settings.auth_watch_interval_seconds)

    def check(self, status: AuthStatus, now: datetime | None = None) -> bool:
        """Decide and (if warranted) send. Returns whether a push was sent, and
        takes `now` so the repeat cadence is testable without sleeping."""
        now = now or datetime.now(timezone.utc)
        phase = status.phase
        if phase is AuthPhase.OK:
            # Reauth happened (or the token rolled). Clear, so the next slide
            # into the window notifies again rather than being swallowed by
            # state left over from the last one.
            self._notified_phase = None
            self._notified_at = None
            return False

        if phase != self._notified_phase:
            reason = "entered"
        elif self._notified_at is not None and now - self._notified_at >= timedelta(
            hours=settings.auth_notify_repeat_hours
        ):
            # One push at the top of a 24h window is one swipe away from being
            # missed entirely. Repeat, slowly.
            reason = "still in"
        else:
            return False

        self._notified_phase = phase
        self._notified_at = now
        push_type = PushType.REAUTH_NEEDED if phase is AuthPhase.NEEDED else PushType.REAUTH_PREWARN
        logger.warning(
            "auth watch: %s %s phase (refresh token expires %s) — notifying paired devices",
            reason,
            phase.value,
            status.refresh_token_expires_at,
        )
        push_broadcast_nowait(PushPayload(type=push_type, ref=phase.value))
        return True


auth_watcher = AuthWatcher()

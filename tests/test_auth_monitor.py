import json
import time
from types import SimpleNamespace

from server import auth_monitor


def _fake_settings(tmp_path, prewarn_hours=24):
    return SimpleNamespace(
        credentials_path=tmp_path / "credentials.json",
        auth_prewarn_hours=prewarn_hours,
    )


def _write_creds(path, *, expires_at_ms, refresh_expires_at_ms):
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "x",
                    "refreshToken": "y",
                    "expiresAt": expires_at_ms,
                    "refreshTokenExpiresAt": refresh_expires_at_ms,
                    "subscriptionType": "pro",
                    "scopes": [],
                }
            }
        )
    )


def test_missing_credentials_file_means_reauth_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_monitor, "settings", _fake_settings(tmp_path))
    status = auth_monitor.get_status()
    assert not status.credentials_found
    assert status.reauth_needed
    assert not status.auth_ok


def test_far_future_expiry_is_healthy(tmp_path, monkeypatch):
    fake = _fake_settings(tmp_path)
    monkeypatch.setattr(auth_monitor, "settings", fake)
    now_ms = int(time.time() * 1000)
    _write_creds(fake.credentials_path, expires_at_ms=now_ms + 8 * 3600 * 1000, refresh_expires_at_ms=now_ms + 9 * 86400 * 1000)
    status = auth_monitor.get_status()
    assert status.auth_ok
    assert not status.reauth_needed
    assert not status.reauth_prewarn


def test_refresh_token_already_past_means_reauth_needed(tmp_path, monkeypatch):
    fake = _fake_settings(tmp_path)
    monkeypatch.setattr(auth_monitor, "settings", fake)
    now_ms = int(time.time() * 1000)
    _write_creds(fake.credentials_path, expires_at_ms=now_ms - 3600 * 1000, refresh_expires_at_ms=now_ms - 1000)
    status = auth_monitor.get_status()
    assert status.reauth_needed
    assert not status.auth_ok


def test_refresh_token_within_prewarn_window(tmp_path, monkeypatch):
    fake = _fake_settings(tmp_path, prewarn_hours=24)
    monkeypatch.setattr(auth_monitor, "settings", fake)
    now_ms = int(time.time() * 1000)
    # expires in 12h — inside the 24h prewarn window, not yet expired
    _write_creds(fake.credentials_path, expires_at_ms=now_ms + 3600 * 1000, refresh_expires_at_ms=now_ms + 12 * 3600 * 1000)
    status = auth_monitor.get_status()
    assert not status.reauth_needed
    assert status.reauth_prewarn
    assert status.auth_ok  # prewarn is a heads-up, not a failure


def test_corrupt_credentials_file_treated_as_not_found(tmp_path, monkeypatch):
    fake = _fake_settings(tmp_path)
    monkeypatch.setattr(auth_monitor, "settings", fake)
    fake.credentials_path.write_text("{not valid json")
    status = auth_monitor.get_status()
    assert not status.credentials_found
    assert status.reauth_needed


# --- log throttle ------------------------------------------------------------
#
# /health reads credentials on every poll and the phone polls it every few
# seconds. Un-throttled, the pre-warn line put 638 of one day's 1353 log lines
# into an 8-hour window — 88% noise, which made a later grep for nightly
# rollover useless. These pin the throttle to transitions.

import logging  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402

from server.auth_monitor import AuthPhase, AuthStatus, AuthWatcher  # noqa: E402
from server.push import PushType  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_log_state():
    auth_monitor.reset_log_state()
    yield
    auth_monitor.reset_log_state()


def _prewarn_settings(tmp_path, **kw):
    fake = _fake_settings(tmp_path)
    fake.auth_watch_interval_seconds = 900.0
    fake.auth_notify_repeat_hours = kw.get("repeat_hours", 6.0)
    return fake


def test_prewarn_logs_once_across_many_polls(tmp_path, monkeypatch, caplog):
    fake = _fake_settings(tmp_path)
    monkeypatch.setattr(auth_monitor, "settings", fake)
    now_ms = int(time.time() * 1000)
    _write_creds(
        fake.credentials_path,
        expires_at_ms=now_ms + 3600 * 1000,
        refresh_expires_at_ms=now_ms + 6 * 3600 * 1000,
    )
    with caplog.at_level(logging.INFO, logger="server.auth_monitor"):
        for _ in range(50):
            assert auth_monitor.get_status().reauth_prewarn

    prewarn_lines = [r for r in caplog.records if "pre-warn window" in r.getMessage()]
    assert len(prewarn_lines) == 1
    # ...and at a level someone actually greps for.
    assert prewarn_lines[0].levelno == logging.WARNING


def test_missing_credentials_logs_once_across_many_polls(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(auth_monitor, "settings", _fake_settings(tmp_path))
    with caplog.at_level(logging.INFO, logger="server.auth_monitor"):
        for _ in range(50):
            assert not auth_monitor.get_status().credentials_found
    assert len([r for r in caplog.records if "no credentials file" in r.getMessage()]) == 1


def test_log_throttle_reopens_on_phase_change(tmp_path, monkeypatch, caplog):
    fake = _fake_settings(tmp_path)
    monkeypatch.setattr(auth_monitor, "settings", fake)
    now_ms = int(time.time() * 1000)
    _write_creds(
        fake.credentials_path,
        expires_at_ms=now_ms + 3600 * 1000,
        refresh_expires_at_ms=now_ms + 9 * 86400 * 1000,
    )
    with caplog.at_level(logging.INFO, logger="server.auth_monitor"):
        auth_monitor.get_status()  # ok
        auth_monitor.get_status()  # ok, throttled
        _write_creds(
            fake.credentials_path,
            expires_at_ms=now_ms + 3600 * 1000,
            refresh_expires_at_ms=now_ms + 6 * 3600 * 1000,
        )
        auth_monitor.get_status()  # -> prewarn, logs
        _write_creds(
            fake.credentials_path,
            expires_at_ms=now_ms - 1,
            refresh_expires_at_ms=now_ms - 1,
        )
        auth_monitor.get_status()  # -> expired, logs

    messages = [r.getMessage() for r in caplog.records]
    assert sum("pre-warn window" in m for m in messages) == 1
    assert sum("interactive reauth needed" in m for m in messages) == 1


# --- watcher -----------------------------------------------------------------


def _status(phase: AuthPhase) -> AuthStatus:
    now = datetime.now(timezone.utc)
    if phase is AuthPhase.OK:
        expires = now + timedelta(days=9)
    elif phase is AuthPhase.PREWARN:
        expires = now + timedelta(hours=6)
    else:
        expires = now - timedelta(hours=1)
    return AuthStatus(
        credentials_found=True,
        access_token_expires_at=now + timedelta(hours=8),
        refresh_token_expires_at=expires,
        subscription_type="pro",
    )


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_monitor, "settings", _prewarn_settings(tmp_path))
    sent = []
    monkeypatch.setattr(auth_monitor, "push_broadcast_nowait", sent.append)
    w = AuthWatcher()
    w.sent = sent  # type: ignore[attr-defined]
    return w


def test_watcher_notifies_on_entering_prewarn(watcher):
    assert watcher.check(_status(AuthPhase.PREWARN)) is True
    assert [p.type for p in watcher.sent] == [PushType.REAUTH_PREWARN]


def test_watcher_stays_quiet_while_healthy(watcher):
    for _ in range(10):
        assert watcher.check(_status(AuthPhase.OK)) is False
    assert watcher.sent == []


def test_watcher_does_not_repeat_within_the_interval(watcher):
    now = datetime.now(timezone.utc)
    assert watcher.check(_status(AuthPhase.PREWARN), now=now) is True
    for minutes in (1, 30, 120, 359):
        assert watcher.check(_status(AuthPhase.PREWARN), now=now + timedelta(minutes=minutes)) is False
    assert len(watcher.sent) == 1


def test_watcher_repeats_after_the_interval(watcher):
    now = datetime.now(timezone.utc)
    watcher.check(_status(AuthPhase.PREWARN), now=now)
    assert watcher.check(_status(AuthPhase.PREWARN), now=now + timedelta(hours=6)) is True
    assert watcher.check(_status(AuthPhase.PREWARN), now=now + timedelta(hours=7)) is False
    assert watcher.check(_status(AuthPhase.PREWARN), now=now + timedelta(hours=12)) is True
    assert len(watcher.sent) == 3


def test_watcher_escalates_prewarn_to_needed_immediately(watcher):
    now = datetime.now(timezone.utc)
    watcher.check(_status(AuthPhase.PREWARN), now=now)
    # Crossing the deadline is a new fact, not a repeat — it must not wait out
    # the repeat interval.
    assert watcher.check(_status(AuthPhase.NEEDED), now=now + timedelta(minutes=1)) is True
    assert [p.type for p in watcher.sent] == [PushType.REAUTH_PREWARN, PushType.REAUTH_NEEDED]


def test_watcher_rearms_after_reauth(watcher):
    now = datetime.now(timezone.utc)
    watcher.check(_status(AuthPhase.PREWARN), now=now)
    watcher.check(_status(AuthPhase.OK), now=now + timedelta(minutes=5))
    # A fresh 9-day token later slides back into the window: that is a new
    # warning, not a duplicate of the one from before the reauth.
    assert watcher.check(_status(AuthPhase.PREWARN), now=now + timedelta(minutes=10)) is True
    assert len(watcher.sent) == 2


def test_watcher_notifies_for_a_missing_credentials_file(watcher, tmp_path):
    gone = AuthStatus(
        credentials_found=False,
        access_token_expires_at=None,
        refresh_token_expires_at=None,
        subscription_type=None,
    )
    assert gone.phase is AuthPhase.NEEDED
    assert watcher.check(gone) is True
    assert [p.type for p in watcher.sent] == [PushType.REAUTH_NEEDED]


def test_watch_loop_survives_a_raising_poll(watcher, monkeypatch):
    """A watcher that dies on one bad read looks exactly like a healthy one
    until the morning the token lapses unannounced."""
    import asyncio

    calls = {"n": 0}

    def _poll():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return _status(AuthPhase.PREWARN)

    monkeypatch.setattr(auth_monitor, "get_status", _poll)
    monkeypatch.setattr(auth_monitor.settings, "auth_watch_interval_seconds", 0.01)

    async def _body():
        watcher.start()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if watcher.sent:
                break
        await watcher.stop()

    asyncio.run(_body())
    assert calls["n"] >= 2
    assert [p.type for p in watcher.sent] == [PushType.REAUTH_PREWARN]


def test_stop_is_safe_when_never_started(watcher):
    import asyncio

    asyncio.run(watcher.stop())


def test_log_throttle_distinguishes_causes_within_the_needed_phase(tmp_path, monkeypatch, caplog):
    """An expired token, a missing file and an unparseable file all sit in
    phase NEEDED. Keying the throttle on phase alone meant the log kept
    blaming whichever one happened to be logged first."""
    fake = _fake_settings(tmp_path)
    monkeypatch.setattr(auth_monitor, "settings", fake)
    now_ms = int(time.time() * 1000)

    with caplog.at_level(logging.INFO, logger="server.auth_monitor"):
        _write_creds(fake.credentials_path, expires_at_ms=now_ms - 1, refresh_expires_at_ms=now_ms - 1)
        auth_monitor.get_status()  # expired token
        # Reauth attempted, but the credential write left the file corrupt.
        fake.credentials_path.write_text("{ not json")
        auth_monitor.get_status()
        auth_monitor.get_status()  # ...and still throttled per cause
        fake.credentials_path.unlink()
        auth_monitor.get_status()

    messages = [r.getMessage() for r in caplog.records]
    assert sum("refresh token expired" in m for m in messages) == 1
    assert sum("failed to parse" in m for m in messages) == 1
    assert sum("no credentials file" in m for m in messages) == 1


def test_a_file_corrupt_in_a_different_way_each_poll_does_not_reopen_the_flood(
    tmp_path, monkeypatch, caplog
):
    """The parse-error message embeds the exception text, and a file caught
    mid-write decodes to a different error each time. The throttle keys on a
    fixed cause word so that cannot key every poll differently."""
    fake = _fake_settings(tmp_path)
    monkeypatch.setattr(auth_monitor, "settings", fake)

    with caplog.at_level(logging.INFO, logger="server.auth_monitor"):
        for i in range(50):
            fake.credentials_path.write_text('{"claudeAiOauth"' + " " * i)
            auth_monitor.get_status()

    assert len([r for r in caplog.records if "failed to parse" in r.getMessage()]) == 1

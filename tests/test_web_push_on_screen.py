"""A web push to an Apple endpoint is held, then dropped, while the web app is
on screen (docs/tasks/web-push-on-screen.md): iOS Safari must show every push,
so one the open page would announce itself flashes a blank banner instead."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import server.push as push_module
import server.webpush as webpush_module
from server.app import public_app
from server.pairing import pairing_store
from server.presence import presence
from server.push import PushPayload, PushType, dispatch_web
from tests.test_webpush import _Sender, _auth, _register, _no_leaked_subscriptions, key_dir  # noqa: F401

APPLE = "https://web.push.apple.com/QGx-abc"
FCM = "https://fcm.googleapis.com/fcm/send/abc123"
HOLD = 0.3


@pytest.fixture
def sender(monkeypatch, key_dir):  # noqa: F811
    s = _Sender()
    monkeypatch.setattr(webpush_module, "_webpush_send", s)
    return s


@pytest.fixture(autouse=True)
def _short_hold(monkeypatch):
    monkeypatch.setattr(push_module, "ON_SCREEN_HOLD_SECONDS", HOLD)
    presence.forget_all()
    yield
    presence.forget_all()


def _turn_ready():
    return PushPayload(type=PushType.TURN_READY, ref="turn-1")


async def _dispatch_while(token, payload, *, mark_after=None):
    """Dispatch; mark the device on screen `mark_after` seconds in, if given.
    Returns how long dispatch took."""
    start = time.monotonic()
    task = asyncio.create_task(dispatch_web(token, payload))
    if mark_after is not None:
        await asyncio.sleep(mark_after)
        presence.mark_on_screen(token)
    await task
    return time.monotonic() - start


def test_a_page_on_screen_gets_no_push(paired_device_token, sender):
    """Watching a reply: the client's next poll lands inside the hold."""
    _register(paired_device_token, APPLE)
    presence.mark_on_screen(paired_device_token)
    asyncio.run(_dispatch_while(paired_device_token, _turn_ready(), mark_after=0.05))
    assert sender.calls == []


def test_a_phone_that_just_went_to_sleep_still_gets_it(paired_device_token, sender):
    """On screen a moment ago, nothing since: the push is sent, at the end of
    the hold, not lost."""
    _register(paired_device_token, APPLE)
    presence.mark_on_screen(paired_device_token)
    took = asyncio.run(_dispatch_while(paired_device_token, _turn_ready()))
    assert len(sender.calls) == 1
    assert took >= HOLD


def test_a_device_not_seen_lately_is_sent_at_once(paired_device_token, sender):
    _register(paired_device_token, APPLE)
    took = asyncio.run(_dispatch_while(paired_device_token, _turn_ready()))
    assert len(sender.calls) == 1
    assert took < HOLD


def test_seen_long_ago_counts_as_not_seen(paired_device_token, sender, monkeypatch):
    _register(paired_device_token, APPLE)
    presence.mark_on_screen(paired_device_token)
    monkeypatch.setattr(push_module, "ON_SCREEN_RECENT_SECONDS", 0.0)
    took = asyncio.run(_dispatch_while(paired_device_token, _turn_ready()))
    assert len(sender.calls) == 1
    assert took < HOLD


def test_notify_is_held_like_a_reply(paired_device_token, sender):
    _register(paired_device_token, APPLE)
    presence.mark_on_screen(paired_device_token)
    payload = PushPayload(type=PushType.NOTIFY, ref="r", alert_body="Call the dentist")
    asyncio.run(_dispatch_while(paired_device_token, payload, mark_after=0.05))
    assert sender.calls == []


@pytest.mark.parametrize("payload", [
    PushPayload(type=PushType.PERMISSION_REQUEST, ref="p1", alert_title="Edit", alert_body="+ x"),
    PushPayload(type=PushType.REAUTH_NEEDED, ref="reauth"),
    PushPayload(type=PushType.REAUTH_PREWARN, ref="prewarn"),
], ids=["permission", "reauth", "prewarn"])
def test_pushes_a_page_doesnt_announce_itself_are_never_held(paired_device_token, sender, payload):
    _register(paired_device_token, APPLE)
    presence.mark_on_screen(paired_device_token)
    took = asyncio.run(_dispatch_while(paired_device_token, payload, mark_after=0.05))
    assert len(sender.calls) == 1
    assert took < HOLD


def test_chrome_and_firefox_are_never_held(paired_device_token, sender):
    """Their service worker may show nothing for a focused page, and sees the
    window itself."""
    _register(paired_device_token, FCM)
    presence.mark_on_screen(paired_device_token)
    took = asyncio.run(_dispatch_while(paired_device_token, _turn_ready(), mark_after=0.05))
    assert len(sender.calls) == 1
    assert took < HOLD


def test_another_device_on_screen_does_not_hold_this_one(pair_device, sender):
    phone, laptop = pair_device(), pair_device()
    _register(phone, APPLE)
    presence.mark_on_screen(phone)
    presence.mark_on_screen(laptop)

    async def run():
        start = time.monotonic()
        task = asyncio.create_task(dispatch_web(phone, _turn_ready()))
        await asyncio.sleep(0.05)
        presence.mark_on_screen(laptop)
        await task
        return time.monotonic() - start

    took = asyncio.run(run())
    assert len(sender.calls) == 1
    assert took >= HOLD


# --- the signal ------------------------------------------------------------------

def test_only_a_request_marked_on_screen_counts(paired_device_token):
    """The service worker's own fetch of /v1/turn, and a background tab's
    polling, carry no header and must not look like a page on screen."""
    with TestClient(public_app) as client:
        client.get("/health", headers=_auth(paired_device_token))
        assert not presence.seen_within(paired_device_token, 60)
        client.get("/health", headers={**_auth(paired_device_token), "X-Marlow-Foreground": "1"})
        assert presence.seen_within(paired_device_token, 60)


def test_an_unauthenticated_request_marks_nothing(paired_device_token):
    with TestClient(public_app) as client:
        resp = client.get("/health", headers={"Authorization": "Bearer nope", "X-Marlow-Foreground": "1"})
    assert resp.status_code == 401
    assert not presence.seen_within("nope", 60)
    assert not presence.seen_within(paired_device_token, 60)


def test_pairing_store_tokens_are_untouched(paired_device_token):
    """Presence is memory only: nothing about it is written to the pairing file."""
    before = pairing_store.all_device_tokens()
    presence.mark_on_screen(paired_device_token)
    assert pairing_store.all_device_tokens() == before

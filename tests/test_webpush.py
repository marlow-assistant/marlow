import asyncio
import base64
import json
import os
import stat
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from pywebpush import WebPushException

import server.push as push_module
import server.webpush as webpush_module
from server.app import public_app
from server.config import settings as real_settings
from server.pairing import pairing_store
from server.push import PushPayload, PushType, broadcast, dispatch_web

GOOD_ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _keys() -> dict:
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    return {"p256dh": _b64(point), "auth": _b64(os.urandom(16))}


def _sub(endpoint: str = GOOD_ENDPOINT) -> dict:
    return {"endpoint": endpoint, "keys": _keys()}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(webpush_module, "settings", replace(real_settings, data_dir=tmp_path))
    monkeypatch.setattr(webpush_module, "_vapid_cache", None)
    yield tmp_path
    monkeypatch.setattr(webpush_module, "_vapid_cache", None)


@pytest.fixture(autouse=True)
def _no_leaked_subscriptions():
    """pairing_store is a process-wide singleton and device tokens outlive a
    test, so a subscription left behind here would make a *later* file's
    broadcast attempt a real send to a push service."""

    def _wipe():
        for token in pairing_store.all_device_tokens():
            pairing_store.clear_webpush_subscription(token)

    _wipe()
    yield
    _wipe()


class _Sender:
    """Stands in for pywebpush.webpush: records calls, optionally fails."""

    def __init__(self, status=None):
        self.status = status
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.status is not None:
            raise WebPushException("boom", response=SimpleNamespace(status_code=self.status))
        return SimpleNamespace(status_code=201)

    @property
    def payloads(self):
        return [json.loads(c["data"]) for c in self.calls]


@pytest.fixture
def sender(monkeypatch, key_dir):
    s = _Sender()
    monkeypatch.setattr(webpush_module, "_webpush_send", s)
    return s


def _register(token, endpoint=GOOD_ENDPOINT):
    sub = webpush_module.validate_subscription(_sub(endpoint))
    assert sub is not None
    pairing_store.set_webpush_subscription(token, sub)


# --- VAPID key ----------------------------------------------------------------

def test_key_is_created_with_mode_0600_and_is_stable(key_dir):
    path = key_dir / "vapid_private.pem"
    assert not path.exists()
    first = webpush_module.public_key_b64url()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert webpush_module.public_key_b64url() == first
    # A fresh process: drop the cache, reload from disk.
    webpush_module._vapid_cache = None
    assert webpush_module.public_key_b64url() == first
    assert [p.name for p in key_dir.iterdir() if p.name != "vapid_private.pem"] == []


def test_key_is_never_logged(key_dir, caplog):
    caplog.set_level("DEBUG")
    webpush_module.public_key_b64url()
    pem = (key_dir / "vapid_private.pem").read_text()
    body = "".join(line for line in pem.splitlines() if "-----" not in line)
    assert body not in caplog.text


def test_key_endpoint_requires_auth_and_returns_uncompressed_point(paired_device_token):
    with TestClient(public_app) as client:
        assert client.get("/v1/webpush/key").status_code == 401
        assert client.get("/v1/webpush/key", headers=_auth("nope")).status_code == 401
        resp = client.get("/v1/webpush/key", headers=_auth(paired_device_token))
    assert resp.status_code == 200
    key = resp.json()["public_key"]
    assert "=" not in key
    raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    assert len(raw) == 65 and raw[0] == 0x04


# --- subscription endpoint -----------------------------------------------------

def test_subscription_requires_auth():
    with TestClient(public_app) as client:
        assert client.post("/v1/device/webpush-subscription", json=_sub()).status_code == 401
        resp = client.post("/v1/device/webpush-subscription", json=_sub(), headers=_auth("nope"))
        assert resp.status_code == 401


def test_subscription_is_stored_and_reregistering_replaces(paired_device_token):
    with TestClient(public_app) as client:
        first = _sub()
        resp = client.post("/v1/device/webpush-subscription", json=first, headers=_auth(paired_device_token))
        assert resp.status_code == 200 and resp.json() == {"ok": True}
        stored = pairing_store.webpush_subscription_for(paired_device_token)
        assert stored["endpoint"] == GOOD_ENDPOINT
        assert stored["p256dh"] == first["keys"]["p256dh"] and stored["auth"] == first["keys"]["auth"]
        # Idempotent: same body again.
        client.post("/v1/device/webpush-subscription", json=first, headers=_auth(paired_device_token))
        assert pairing_store.webpush_subscription_for(paired_device_token) == stored
        # Replaced: one subscription per device.
        second = _sub("https://updates.push.services.mozilla.com/wpush/v2/zzz")
        client.post("/v1/device/webpush-subscription", json=second, headers=_auth(paired_device_token))
    assert pairing_store.webpush_subscription_for(paired_device_token)["endpoint"] == second["endpoint"]


def test_clear_subscription_and_unknown_token_are_harmless(paired_device_token):
    _register(paired_device_token)
    pairing_store.clear_webpush_subscription(paired_device_token)
    assert pairing_store.webpush_subscription_for(paired_device_token) is None
    pairing_store.clear_webpush_subscription(paired_device_token)
    pairing_store.set_webpush_subscription("no-such-token", {"endpoint": GOOD_ENDPOINT})
    assert pairing_store.webpush_subscription_for("no-such-token") is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/abc",
        "https://example.com/push",
        "https://evilfcm.googleapis.com.attacker.com/x",
        "https://fcm.googleapis.com.evil.com/x",
        "https://evilfcm.googleapis.com/x",
        "https://user:pw@fcm.googleapis.com/x",
        "https://fcm.googleapis.com@evil.com/x",
        "https://fcm.googleapis.com:8443/x",
        "https://evil.com\\@fcm.googleapis.com/x",
        "https://127.0.0.1/x",
        "ftp://fcm.googleapis.com/x",
        "//fcm.googleapis.com/x",
        "fcm.googleapis.com/x",
        "",
        "https://fcm.googleapis.com/" + "a" * 2100,
    ],
)
def test_bad_endpoints_are_rejected_with_400(paired_device_token, endpoint):
    with TestClient(public_app) as client:
        resp = client.post(
            "/v1/device/webpush-subscription", json=_sub(endpoint), headers=_auth(paired_device_token)
        )
    assert resp.status_code == 400
    assert pairing_store.webpush_subscription_for(paired_device_token) is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/x",
        "https://updates.push.services.mozilla.com/wpush/v2/x",
        "https://web.push.apple.com/x",
        "https://FCM.googleapis.com:443/x",
        "https://eu.web.push.apple.com/x",
    ],
)
def test_real_push_service_endpoints_are_accepted(endpoint):
    assert webpush_module.endpoint_is_allowed(endpoint)


def _mutate(**keys):
    sub = _sub()
    sub["keys"].update(keys)
    return sub


@pytest.mark.parametrize(
    "body",
    [
        {"endpoint": GOOD_ENDPOINT},
        {"endpoint": GOOD_ENDPOINT, "keys": {}},
        {"endpoint": GOOD_ENDPOINT, "keys": {"p256dh": _keys()["p256dh"]}},
        {"endpoint": GOOD_ENDPOINT, "keys": {"auth": _keys()["auth"]}},
        {"keys": _keys()},
        {"endpoint": GOOD_ENDPOINT, "keys": "nope"},
        _mutate(p256dh=""),
        _mutate(auth=""),
        _mutate(p256dh="not base64!!"),
        _mutate(auth="a" * 1000),
        _mutate(p256dh=_b64(b"short")),
        _mutate(auth=_b64(b"short")),
        _mutate(p256dh=_b64(b"\x02" + b"\0" * 64)),
        {"endpoint": 5, "keys": _keys()},
    ],
)
def test_bad_keys_and_shapes_are_rejected_with_400(paired_device_token, body):
    with TestClient(public_app) as client:
        resp = client.post("/v1/device/webpush-subscription", json=body, headers=_auth(paired_device_token))
    assert resp.status_code == 400


# --- sending -------------------------------------------------------------------

def test_turn_ready_payload_carries_no_text(paired_device_token, sender):
    _register(paired_device_token)
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.TURN_READY, ref="turn-1")))
    assert sender.payloads == [{"type": "turn_ready", "ref": "turn-1"}]
    call = sender.calls[0]
    assert call["headers"] == {"Urgency": "high"}
    assert call["timeout"] == webpush_module._SEND_TIMEOUT_SECONDS
    assert call["vapid_claims"]["sub"].startswith("mailto:")
    assert call["ttl"] == 24 * 60 * 60
    assert call["subscription_info"]["endpoint"] == GOOD_ENDPOINT


def test_notify_carries_alert_body_and_clamps_it(paired_device_token, sender):
    _register(paired_device_token)
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.NOTIFY, ref="r", alert_body="Call mum")))
    big = "x" * 10_000
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.NOTIFY, ref="r", alert_body=big)))
    assert sender.payloads[0] == {"type": "notify", "ref": "r", "alert_body": "Call mum"}
    assert len(sender.payloads[1]["alert_body"].encode()) <= push_module._MAX_ALERT_BODY_BYTES + 3


def test_permission_request_ttl_is_the_hook_window(paired_device_token, sender):
    _register(paired_device_token)
    asyncio.run(dispatch_web(
        paired_device_token,
        PushPayload(type=PushType.PERMISSION_REQUEST, ref="p1", alert_title="Edit USER.md", alert_body="+ x"),
    ))
    assert sender.calls[0]["ttl"] == int(real_settings.t_hook_seconds)
    assert sender.calls[0]["headers"] == {"Urgency": "high"}
    assert sender.payloads[0] == {
        "type": "permission_request", "ref": "p1", "alert_title": "Edit USER.md", "alert_body": "+ x"
    }


def test_reauth_push_is_opaque_and_normal_urgency(paired_device_token, sender):
    _register(paired_device_token)
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.REAUTH_NEEDED, ref="")))
    assert sender.payloads == [{"type": "reauth_needed", "ref": ""}]
    assert sender.calls[0]["headers"] == {"Urgency": "normal"}


def test_no_subscription_means_no_send(paired_device_token, sender):
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.TURN_READY, ref="t")))
    assert sender.calls == []


@pytest.mark.parametrize("status", [404, 410])
def test_gone_subscription_is_pruned(paired_device_token, monkeypatch, key_dir, status):
    _register(paired_device_token)
    monkeypatch.setattr(webpush_module, "_webpush_send", _Sender(status=status))
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.TURN_READY, ref="t")))
    assert pairing_store.webpush_subscription_for(paired_device_token) is None


def test_a_410_does_not_prune_a_subscription_registered_while_the_send_was_in_flight(
    paired_device_token, monkeypatch, key_dir
):
    """The browser rotated its subscription: the app's launch re-registered a
    new endpoint while a send to the old one sat in its thread. The old
    endpoint's 410 must clear only the old one."""
    _register(paired_device_token)
    fresh = webpush_module.validate_subscription(
        _sub("https://fcm.googleapis.com/fcm/send/fresh-endpoint")
    )

    class _RotatesMidSend(_Sender):
        def __call__(self, **kwargs):
            pairing_store.set_webpush_subscription(paired_device_token, fresh)
            return super().__call__(**kwargs)

    monkeypatch.setattr(webpush_module, "_webpush_send", _RotatesMidSend(status=410))
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.TURN_READY, ref="t")))
    assert pairing_store.webpush_subscription_for(paired_device_token) == fresh


def test_server_error_does_not_prune(paired_device_token, monkeypatch, key_dir, caplog):
    _register(paired_device_token)
    monkeypatch.setattr(webpush_module, "_webpush_send", _Sender(status=500))
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.TURN_READY, ref="t")))
    assert pairing_store.webpush_subscription_for(paired_device_token) is not None
    assert GOOD_ENDPOINT not in caplog.text


@pytest.mark.parametrize("subject, endpoint, hinted", [
    ("mailto:marlow@localhost", "https://web.push.apple.com/QGx", True),
    ("mailto:owner@example.com", "https://web.push.apple.com/QGx", False),
    ("mailto:marlow@localhost", GOOD_ENDPOINT, False),
], ids=["apple, placeholder", "apple, real address", "fcm, placeholder"])
def test_a_403_under_the_placeholder_contact_says_how_to_fix_it(
    paired_device_token, monkeypatch, key_dir, caplog, subject, endpoint, hinted
):
    """Apple's service refused every Safari push with the placeholder contact
    (Mike's box, 2026-10-25), and the bare "status=403" took an investigation
    to read. A 403 with a real address set, or from a service that accepts
    the placeholder, is some other fault: no hint."""
    monkeypatch.setattr(webpush_module, "settings", replace(webpush_module.settings, webpush_subject=subject))
    _register(paired_device_token, endpoint)
    monkeypatch.setattr(webpush_module, "_webpush_send", _Sender(status=403))
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.TURN_READY, ref="t")))
    assert "status=403" in caplog.text
    assert ("--email" in caplog.text) is hinted
    assert pairing_store.webpush_subscription_for(paired_device_token) is not None


def test_unexpected_exception_never_raises(paired_device_token, monkeypatch, key_dir):
    _register(paired_device_token)

    def explode(**_):
        raise RuntimeError("network down")

    monkeypatch.setattr(webpush_module, "_webpush_send", explode)
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.TURN_READY, ref="t")))
    assert pairing_store.webpush_subscription_for(paired_device_token) is not None


def test_hung_send_is_bounded(paired_device_token, monkeypatch, key_dir):
    import time

    _register(paired_device_token)
    monkeypatch.setattr(webpush_module, "_SEND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(webpush_module, "_webpush_send", lambda **_: time.sleep(0.3))
    start = time.monotonic()
    asyncio.run(dispatch_web(paired_device_token, PushPayload(type=PushType.TURN_READY, ref="t")))
    assert time.monotonic() - start < 2


def test_broadcast_reaches_both_an_apns_device_and_a_web_device(pair_device, sender, monkeypatch):
    apns_device, web_device = pair_device("iPhone"), pair_device("Browser")
    pairing_store.set_apns_token(apns_device, "apns-tok")
    _register(web_device)
    apns_calls = []

    async def fake_dispatch(device_token, payload):
        apns_calls.append(device_token)

    monkeypatch.setattr(push_module, "dispatch", fake_dispatch)
    asyncio.run(broadcast(PushPayload(type=PushType.TURN_READY, ref="t")))
    assert apns_device in apns_calls
    assert len(sender.calls) == 1  # only the browser device has a subscription
    assert sender.payloads == [{"type": "turn_ready", "ref": "t"}]


# --- redirects ------------------------------------------------------------------

def test_a_redirecting_push_service_is_not_followed(key_dir):
    """The endpoint is pinned to the push services, but `requests` follows a
    3xx by default: a service that redirected would have the POST, VAPID
    header and all, sent wherever it pointed. Drives the real pywebpush send
    against two loopback servers (never a real push service): the first
    answers 307 to the second, which must never be hit."""
    import http.server
    import threading

    hits: list[str] = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(201)
            self.end_headers()

        def log_message(self, *args):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)

    class Redirector(Target):
        def do_POST(self):  # noqa: N802
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
            self.end_headers()

    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    for srv in (target, redirector):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        sub = webpush_module.validate_subscription(_sub())
        sub["endpoint"] = f"http://127.0.0.1:{redirector.server_port}/push"
        with pytest.raises(Exception):
            webpush_module._send_blocking(sub, json.dumps({"type": "turn_ready", "ref": "t"}), 60, "high")
        assert hits == []
    finally:
        target.shutdown()
        redirector.shutdown()

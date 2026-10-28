import asyncio
from dataclasses import replace

import pytest

import server.push as push_module
from server.config import settings as real_settings
from server.pairing import pairing_store
from server.push import PushPayload, PushType, broadcast, broadcast_nowait, dispatch


class _FakeResult:
    def __init__(self, status="200", description=None):
        self.status = status
        self.description = description

    @property
    def is_successful(self):
        return self.status == "200"


class _FakeClient:
    def __init__(self, result=None, raise_exc=None):
        self.result = result or _FakeResult()
        self.raise_exc = raise_exc
        self.sent = []

    async def send_notification(self, request):
        self.sent.append(request)
        if self.raise_exc:
            raise self.raise_exc
        return self.result


@pytest.fixture(autouse=True)
def _reset_push_client_cache(monkeypatch):
    # _client/_client_checked are module-level lazy-cache state; reset around
    # every test so one test's monkeypatched settings/client don't leak into
    # the next.
    monkeypatch.setattr(push_module, "_client", None)
    monkeypatch.setattr(push_module, "_client_checked", False)
    yield
    monkeypatch.setattr(push_module, "_client", None)
    monkeypatch.setattr(push_module, "_client_checked", False)


def test_dispatch_noops_without_apns_config_and_does_not_raise(pair_device):
    # MARLOW_APNS_* is unset in the test sandbox (conftest.py) — this is the
    # local-dev/CI shape, and dispatch must not refuse to run.
    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())


def test_dispatch_noops_when_device_has_no_apns_token_registered(monkeypatch, pair_device):
    fake_client = _FakeClient()
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)

    token = pair_device("iPhone")  # deliberately never registered an apns_token

    async def _body():
        await dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())
    assert fake_client.sent == []


def test_permission_push_carries_only_its_summary_and_review_deny(monkeypatch, pair_device):
    """Since 2026-10-02 a permission push says what Marlow wants to do (Mike's
    call; push.py's docstring). What it carries is exactly the title and body it
    was handed -- never tool_input -- plus the request id and the Review/Deny
    category. No Allow from the lock screen: Mike always reviews in the app."""
    fake_client = _FakeClient()
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(
            token,
            PushPayload(
                type=PushType.PERMISSION_REQUEST, ref="req-123", alert_title="Edit USER.md", alert_body="+ Likes unicorns"
            ),
        )

    asyncio.run(_body())

    assert len(fake_client.sent) == 1
    message = fake_client.sent[0].message
    assert set(message.keys()) == {"aps", "type", "ref", "request_id"}
    assert message["type"] == "permission_request"
    assert message["ref"] == message["request_id"] == "req-123"
    assert message["aps"]["alert"] == {"title": "Edit USER.md", "body": "+ Likes unicorns"}
    assert message["aps"]["category"] == "PERMISSION_REQUEST_TRUNCATED"
    assert set(message["aps"].keys()) == {
        "alert", "content-available", "interruption-level", "category", "sound"
    }


def test_permission_push_requires_text():
    """Without text it would fall back to the generic line it replaced."""
    import pytest

    with pytest.raises(ValueError):
        PushPayload(type=PushType.PERMISSION_REQUEST, ref="r")
    with pytest.raises(ValueError):
        PushPayload(type=PushType.TURN_READY, ref="r", alert_title="x")


def test_dispatch_sets_expiration_and_interruption_level_for_permission_requests(monkeypatch, pair_device):
    """§5: a stale approval prompt delivered after the 55s window is worse
    than none (dead buttons), and a Focus mode silencing it is a guaranteed
    timeout — both closed via real APNs headers, not app logic."""
    from dataclasses import replace as dc_replace

    from server.config import settings as real_settings

    monkeypatch.setattr(push_module, "settings", dc_replace(real_settings, t_hook_seconds=55))
    fake_client = _FakeClient()
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.PERMISSION_REQUEST, ref="req-1", alert_body="b"))

    asyncio.run(_body())

    sent = fake_client.sent[0]
    assert sent.message["aps"]["interruption-level"] == "time-sensitive"
    assert sent.time_to_live == 55


def test_dispatch_leaves_non_permission_pushes_without_the_permission_headers(monkeypatch, pair_device):
    fake_client = _FakeClient()
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())

    sent = fake_client.sent[0]
    assert "interruption-level" not in sent.message["aps"]
    assert sent.time_to_live is None


def test_turn_ready_push_sets_binary_unread_badge_and_receive_sound(monkeypatch, pair_device):
    fake_client = _FakeClient()
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())

    aps = fake_client.sent[0].message["aps"]
    assert aps["badge"] == 1
    assert aps["sound"] == "default"


def test_notify_push_carries_reminder_body_and_turn_ref(monkeypatch, pair_device):
    fake_client = _FakeClient()
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(
            token,
            PushPayload(type=PushType.NOTIFY, ref="turn-reminder", alert_body="Go to Costco"),
        )

    asyncio.run(_body())

    message = fake_client.sent[0].message
    assert message["type"] == "notify"
    assert message["ref"] == "turn-reminder"
    assert message["aps"] == {
        "alert": "Go to Costco",
        "content-available": 1,
        "badge": 1,
        "sound": "default",
    }


def test_notify_push_requires_body_and_other_types_cannot_carry_one():
    with pytest.raises(ValueError):
        PushPayload(type=PushType.NOTIFY, ref="turn-reminder")
    with pytest.raises(ValueError):
        PushPayload(type=PushType.TURN_READY, ref="turn-1", alert_body="private text")


def test_non_message_pushes_do_not_mark_the_chat_unread(monkeypatch, pair_device):
    fake_client = _FakeClient()
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.REAUTH_NEEDED, ref="reauth"))

    asyncio.run(_body())

    aps = fake_client.sent[0].message["aps"]
    assert "badge" not in aps
    assert "sound" not in aps


def test_dispatch_swallows_a_raising_apns_client(monkeypatch, pair_device):
    fake_client = _FakeClient(raise_exc=RuntimeError("connection reset"))
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())  # must not raise


def test_dispatch_410_gone_clears_stored_apns_token(monkeypatch, pair_device):
    fake_client = _FakeClient(result=_FakeResult(status="410", description="Unregistered"))
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())
    assert pairing_store.apns_token_for(token) is None


def test_dispatch_swallows_a_raising_clear_apns_token(monkeypatch, pair_device):
    # A 410 response updating our own bookkeeping (pairing_store.clear_apns_token
    # -> disk write) must not be able to escape dispatch()'s "never raises"
    # contract any more than a raising APNs client can — an OSError here would
    # otherwise propagate through broadcast()'s asyncio.gather and kill
    # queue.py's drain loop or 500 the permission-request endpoint.
    fake_client = _FakeClient(result=_FakeResult(status="410", description="Unregistered"))
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)
    monkeypatch.setattr(
        pairing_store, "clear_apns_token", lambda device_token: (_ for _ in ()).throw(OSError("disk full"))
    )

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())  # must not raise


def test_dispatch_successful_delivery_leaves_token_in_place(monkeypatch, pair_device):
    fake_client = _FakeClient(result=_FakeResult(status="200"))
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())
    assert pairing_store.apns_token_for(token) == "fake-apns-token"


def test_broadcast_sends_to_every_currently_paired_device(monkeypatch, pair_device):
    token_a = pair_device("iPhone")
    token_b = pair_device("iPad")
    pairing_store.set_apns_token(token_a, "fake-apns-token-a")
    pairing_store.set_apns_token(token_b, "fake-apns-token-b")

    dispatched_to = []

    async def _fake_dispatch(device_token, payload):
        dispatched_to.append(device_token)

    monkeypatch.setattr(push_module, "dispatch", _fake_dispatch)

    async def _body():
        await broadcast(PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())
    assert set(dispatched_to) >= {token_a, token_b}  # other tests may have paired more devices


def test_broadcast_runs_dispatches_concurrently_not_sequentially(monkeypatch, pair_device):
    token_a = pair_device("iPhone")
    token_b = pair_device("iPad")
    pairing_store.set_apns_token(token_a, "fake-apns-token-a")
    pairing_store.set_apns_token(token_b, "fake-apns-token-b")

    started_together = asyncio.Event()
    concurrent_count = {"peak": 0, "current": 0}

    async def _slow_dispatch(device_token, payload):
        concurrent_count["current"] += 1
        concurrent_count["peak"] = max(concurrent_count["peak"], concurrent_count["current"])
        if concurrent_count["current"] >= 2:
            started_together.set()
        await asyncio.wait_for(started_together.wait(), timeout=1.0)
        concurrent_count["current"] -= 1

    monkeypatch.setattr(push_module, "dispatch", _slow_dispatch)

    async def _body():
        await broadcast(PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())
    assert concurrent_count["peak"] >= 2  # would deadlock/time out if run sequentially


def test_dispatch_times_out_instead_of_hanging_forever(monkeypatch, pair_device):
    # aioapns has no timeout of its own anywhere in its call chain. Without
    # dispatch()'s own asyncio.wait_for, a stalled APNs connection would hang
    # this coroutine forever — and since broadcast() is awaited directly in
    # queue.py's drain loop / app.py's permission round-trip (via
    # broadcast_nowait's background task), that hang has to end somewhere.
    monkeypatch.setattr(push_module, "_DISPATCH_TIMEOUT_SECONDS", 0.05)

    class _HangingClient:
        async def send_notification(self, request):
            await asyncio.sleep(10)  # never returns within the test's lifetime

    monkeypatch.setattr(push_module, "_get_client", lambda: _HangingClient())

    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await asyncio.wait_for(
            dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1")), timeout=1.0
        )

    asyncio.run(_body())  # would itself time out (and fail the test) if dispatch didn't bound the hang


def test_broadcast_never_raises_even_if_all_device_tokens_itself_raises(monkeypatch):
    # broadcast()'s "never raises" contract (matching dispatch()'s own) has to
    # cover all_device_tokens() too, not just the gather — a caller-visible
    # exception here is exactly what would kill queue.py's drain loop.
    monkeypatch.setattr(
        pairing_store, "all_device_tokens", lambda: (_ for _ in ()).throw(RuntimeError("store corrupt"))
    )

    async def _body():
        await broadcast(PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())  # must not raise


def test_broadcast_nowait_schedules_without_blocking_the_caller(monkeypatch, pair_device):
    # pairing_store is a shared singleton across the test session (other
    # tests' paired devices persist), so this asserts membership, not
    # exact equality — same pattern as test_broadcast_sends_to_every_
    # currently_paired_device above.
    release = asyncio.Event()
    dispatched = []

    async def _slow_dispatch(device_token, payload):
        await release.wait()
        dispatched.append(device_token)

    monkeypatch.setattr(push_module, "dispatch", _slow_dispatch)
    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        broadcast_nowait(PushPayload(type=PushType.TURN_READY, ref="turn-1"))
        # broadcast_nowait must return immediately — the dispatch above is
        # still blocked on `release`, so if this returned only after
        # broadcast() finished, dispatched would already be non-empty here.
        assert dispatched == []
        release.set()
        await asyncio.sleep(0.05)  # let the background task actually run

    asyncio.run(_body())
    assert token in dispatched


def test_get_client_constructs_when_fully_configured(monkeypatch, tmp_path):
    # aioapns.APNs() needs a running event loop (it calls
    # asyncio.get_event_loop() internally) — realistic here, since _get_client
    # is only ever reached from inside the already-awaited dispatch().
    key_path = tmp_path / "AuthKey.p8"
    key_path.write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")
    monkeypatch.setattr(
        push_module,
        "settings",
        replace(
            real_settings,
            apns_key_path=key_path,
            apns_key_id="KEYID123",
            apns_team_id="TEAMID123",
            apns_topic="com.mikemason.Marlow",
            apns_environment="sandbox",
        ),
    )

    async def _body():
        return push_module._get_client()

    assert asyncio.run(_body()) is not None


def test_get_client_reads_key_file_contents_not_the_path(monkeypatch, tmp_path):
    # Regression test: aioapns hands `key=` straight to PyJWT's
    # jwt.encode(key=..., algorithm="ES256"), which needs real PEM key
    # material — a file path string there fails to sign at send time. A real
    # EC key + an actual JWT signature is the only way to catch that a path
    # slipped through instead of file contents (constructing APNs() alone
    # doesn't touch the key; signing happens lazily on first send).
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    key_path = tmp_path / "AuthKey.p8"
    key_path.write_text(pem)

    monkeypatch.setattr(
        push_module,
        "settings",
        replace(
            real_settings,
            apns_key_path=key_path,
            apns_key_id="KEYID123",
            apns_team_id="TEAMID123",
            apns_topic="com.mikemason.Marlow",
            apns_environment="sandbox",
        ),
    )

    async def _body():
        return push_module._get_client()

    client = asyncio.run(_body())
    assert client is not None
    assert client.pool.key == pem  # file contents, not the path string

    from aioapns.connection import JWTAuthorizationHeaderProvider

    header = JWTAuthorizationHeaderProvider(
        key=client.pool.key, key_id=client.pool.key_id, team_id=client.pool.team_id
    ).get_header()  # actually signs a JWT — fails with a path string, not PEM
    assert header.startswith("bearer ")


def test_get_client_noops_when_only_partially_configured(monkeypatch, tmp_path):
    key_path = tmp_path / "AuthKey.p8"
    key_path.write_text("fake")
    monkeypatch.setattr(
        push_module,
        "settings",
        replace(real_settings, apns_key_path=key_path, apns_key_id="KEYID123", apns_team_id=None, apns_topic=None),
    )
    assert push_module._get_client() is None


def test_auth_pushes_say_what_they_want_on_the_lock_screen(monkeypatch, pair_device):
    """The generic body is a wake-and-fetch nudge; for the two auth types it is
    the whole notification, since there is nothing to fetch and no personal
    content involved. A vague body here is a swipe-away, which is how a reauth
    deadline gets missed."""
    fake_client = _FakeClient()
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)
    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.REAUTH_PREWARN, ref="prewarn"))
        await dispatch(token, PushPayload(type=PushType.REAUTH_NEEDED, ref="needed"))
        await dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1"))

    asyncio.run(_body())

    prewarn, needed, turn = (s.message for s in fake_client.sent)
    assert "expires soon" in prewarn["aps"]["alert"]
    assert "has expired" in needed["aps"]["alert"]
    assert turn["aps"]["alert"] == "Marlow has something for you."
    # Still opaque — the bodies describe our own session state, never content.
    for message in (prewarn, needed):
        assert set(message.keys()) == {"aps", "type", "ref"}
        assert set(message["aps"].keys()) == {"alert", "content-available"}


def test_only_turn_ready_is_mutable_content(monkeypatch, pair_device):
    """`mutable-content` is what lets the notification service extension swap
    the generic body for the real reply, and it is scoped to `turn_ready` on
    purpose — see the comment at the flag in push.py. Asserted on every type,
    not just the one, because the failure that matters is the flag SPREADING:
    on a permission request it would hand a 55-second decision window to an
    extension that can stall on the network."""
    fake_client = _FakeClient()
    monkeypatch.setattr(push_module, "_get_client", lambda: fake_client)
    token = pair_device("iPhone")
    pairing_store.set_apns_token(token, "fake-apns-token")

    async def _body():
        await dispatch(token, PushPayload(type=PushType.TURN_READY, ref="turn-1"))
        await dispatch(token, PushPayload(type=PushType.NOTIFY, ref="sched-1", alert_body="Costco at 4"))
        await dispatch(token, PushPayload(type=PushType.PERMISSION_REQUEST, ref="req-1", alert_body="b"))
        await dispatch(token, PushPayload(type=PushType.REAUTH_NEEDED, ref="needed"))

    asyncio.run(_body())

    turn, notify, permission, reauth = (s.message for s in fake_client.sent)
    assert turn["aps"]["mutable-content"] == 1
    for message in (notify, permission, reauth):
        assert "mutable-content" not in message["aps"]

    # The extension needs the turn ID to fetch `GET /v1/turn/{ref}`, and `ref`
    # is the only place it can come from. Still no content in the payload.
    assert set(turn.keys()) == {"aps", "type", "ref"}
    assert turn["ref"] == "turn-1"


def test_a_long_notify_body_is_truncated_rather_than_dropped_by_apns() -> None:
    """APNs rejects a >4KB payload outright, and for a reminder a push that
    never arrives is the worst outcome available."""
    from server.push import _MAX_ALERT_BODY_BYTES, _clamp_alert_body

    short = "Go to Costco"
    assert _clamp_alert_body(short) is short

    clamped = _clamp_alert_body("x" * (_MAX_ALERT_BODY_BYTES * 2))
    assert len(clamped.encode("utf-8")) <= _MAX_ALERT_BODY_BYTES + len("…".encode())
    assert clamped.endswith("…")


def test_truncation_never_splits_a_multibyte_character() -> None:
    from server.push import _MAX_ALERT_BODY_BYTES, _clamp_alert_body

    # 3-byte characters do not divide the byte budget evenly, so a naive slice
    # would leave a partial character that fails JSON encoding downstream.
    clamped = _clamp_alert_body("あ" * _MAX_ALERT_BODY_BYTES)
    clamped.encode("utf-8").decode("utf-8")
    assert "�" not in clamped

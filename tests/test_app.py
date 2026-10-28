import asyncio
import base64
import inspect
import logging
import os
import subprocess
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

import server.permissions as permissions_module
from server import active_turn, transcript
from server import app as app_module
from server.app import MessageRequest, _prepare_message, admin_app, internal_app, public_app
from server.config import settings
from server.log import DENY_LOG_NAME
from server.pairing import pairing_store
from server.permissions import permission_registry
from server.push import PushPayload, PushType
from server.queue import work_queue


def _make_self_signed_cert(tmp_path):
    cert = tmp_path / "marlow.crt"
    key = tmp_path / "marlow.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-subj", "/CN=test",
        ],
        check=True, capture_output=True,
    )
    return cert, key


def test_startup_fails_loud_when_identity_missing(tmp_path, monkeypatch):
    # Code-review finding: build_identity_context() was only ever called on
    # the first turn, so a broken identity dir let the server start and pass
    # /health while every subsequent invocation silently errored. The lifespan
    # hook now loads identity at startup so this fails before the server ever
    # accepts a connection.
    monkeypatch.setattr(
        app_module, "settings", replace(settings, soul_path=tmp_path / "missing-soul.md")
    )

    with pytest.raises(Exception):
        with TestClient(public_app):
            pass


def test_health_with_a_token(paired_device_token):
    with TestClient(public_app) as client:
        resp = client.get("/health", headers={"Authorization": f"Bearer {paired_device_token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["auth_ok"] is True
        assert body["oldest_pending_permission_age"] is None  # gate #4


def test_internal_health_matches_public_health():
    with TestClient(internal_app) as internal_client:
        resp = internal_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


def test_message_without_token_is_rejected():
    with TestClient(public_app) as client:
        resp = client.post("/v1/message", json={"text": "hi"})
        assert resp.status_code == 401


def test_a_message_turn_is_attended(paired_device_token):
    """FAIL-CLOSED guard for the `ios` -> `chat` rename. `ATTENDED_SOURCES` is an
    allowlist: if `/v1/message` wrote a source that is not in it, every message
    would run unattended -- no live streaming, and every tool request denied
    without asking. The source the endpoint really writes must be in the set."""
    headers = {"Authorization": f"Bearer {paired_device_token}"}
    with TestClient(public_app) as client:
        resp = client.post("/v1/message", json={"text": "hello", "client_msg_id": "attended-1"}, headers=headers)
        assert resp.status_code == 202
        turn_id = resp.json()["turn_id"]
        source = client.get(f"/v1/turn/{turn_id}", headers=headers).json()["source"]
    assert source == "chat"
    assert source in active_turn.ATTENDED_SOURCES
    with active_turn.running(source):
        assert not active_turn.is_unattended()


def test_full_pairing_and_message_round_trip():
    with TestClient(public_app) as client, TestClient(admin_app) as admin_client:
        invite_resp = admin_client.post("/internal/admin/invite", json={"device_name": "e2e-device"})
        assert invite_resp.status_code == 200
        invite = invite_resp.json()["invite"]

        pair_resp = client.post("/v1/pair", json={"device_name": "e2e-device", "invite": invite})
        assert pair_resp.status_code == 200
        token = pair_resp.json()["device_token"]
        headers = {"Authorization": f"Bearer {token}"}

        msg_resp = client.post("/v1/message", json={"text": "hello", "client_msg_id": "e2e-1"}, headers=headers)
        assert msg_resp.status_code == 202
        turn_id = msg_resp.json()["turn_id"]

        # queue drains fast against the fake claude binary, but poll briefly
        # rather than assume a single tick is enough.
        deadline = time.time() + 5
        turn_body = None
        while time.time() < deadline:
            turn_body = client.get(f"/v1/turn/{turn_id}", headers=headers).json()
            if turn_body["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert turn_body["status"] == "done"
        assert turn_body["result_text"] == "fake response"

        # resend with the same client_msg_id -> same turn_id, no duplicate work
        resend_resp = client.post("/v1/message", json={"text": "hello again", "client_msg_id": "e2e-1"}, headers=headers)
        assert resend_resp.json()["turn_id"] == turn_id

        # The transcript holds the reply, and reading it consumes nothing: a
        # second client (or a second read) finds it again.
        for _ in range(2):
            rows = client.get("/v1/transcript?after=0", headers=headers).json()["rows"]
            # Not the first-run greeting: this box is fresh, so pairing posted one.
            assert [
                r["turn_id"] for r in rows if r["role"] == "assistant" and r["source"] != "system"
            ] == [turn_id]


def test_wrong_invite_rejected_with_the_same_answer_as_a_used_one():
    with TestClient(admin_app) as admin_client, TestClient(public_app) as client:
        invite = admin_client.post("/internal/admin/invite", json={"device_name": "d2"}).json()["invite"]
        wrong = client.post("/v1/pair", json={"device_name": "d2", "invite": "not-an-invite"})
        assert wrong.status_code == 400
        assert client.post("/v1/pair", json={"device_name": "d2", "invite": invite}).status_code == 200
        used = client.post("/v1/pair", json={"device_name": "d2", "invite": invite})
        assert used.status_code == 400
        assert used.json() == wrong.json()


def test_unpair_revokes_the_calling_token_only():
    with TestClient(internal_app) as internal_client, TestClient(public_app) as client:
        a, a_token = _paired_headers(client, internal_client, "unpair-a")
        b, _ = _paired_headers(client, internal_client, "unpair-b")
        assert client.post("/v1/device/unpair", headers=a).json() == {"ok": True}
        assert not pairing_store.is_valid_token(a_token)
        assert client.get("/v1/preferences", headers=a).status_code == 401
        assert client.get("/v1/preferences", headers=b).status_code == 200


def test_unpair_needs_a_token():
    with TestClient(public_app) as client:
        assert client.post("/v1/device/unpair").status_code == 401


def test_public_health_needs_a_token_and_the_socket_one_does_not():
    with TestClient(internal_app) as internal_client, TestClient(public_app) as client:
        assert client.get("/health").status_code == 401
        headers, _ = _paired_headers(client, internal_client, "health-dev")
        assert client.get("/health", headers=headers).status_code == 200
        assert internal_client.get("/health").status_code == 200


def test_admin_can_list_and_revoke_devices_by_fingerprint():
    with TestClient(admin_app) as admin_client, TestClient(public_app) as client:
        headers, token = _paired_headers(client, None, "lost-phone-test")
        listing = admin_client.get("/internal/admin/devices").json()["devices"]
        assert token not in str(listing)
        fp = next(d["fingerprint"] for d in listing if d["device_name"] == "lost-phone-test")
        assert admin_client.post("/internal/admin/devices/revoke", json={"fingerprint": fp}).json() == {"revoked": True}
        assert client.get("/v1/preferences", headers=headers).status_code == 401


# The internal socket is reachable from inside a turn's bwrap namespace (the
# hook must connect), so nothing there may mint, list or revoke device tokens.
# A turn that could mint an invite could pair itself and answer its own
# permission prompts (funnel-ingress review, 2026-10-24).
@pytest.mark.parametrize("method,path", [
    ("POST", "/internal/admin/invite"),
    ("GET", "/internal/admin/devices"),
    ("POST", "/internal/admin/devices/revoke"),
])
def test_the_internal_socket_cannot_mint_list_or_revoke_tokens(method, path):
    with TestClient(internal_app) as internal_client:
        resp = internal_client.request(method, path, json={"device_name": "x", "fingerprint": "00000000"})
    assert resp.status_code in (404, 405)



def test_answer_permission_without_token_is_rejected():
    with TestClient(public_app) as client:
        resp = client.post("/v1/permission/does-not-matter", json={"decision": "allow"})
        assert resp.status_code == 401


def test_answer_permission_rejects_bad_decision_value(paired_device_token):
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post("/v1/permission/does-not-matter", json={"decision": "maybe"}, headers=headers)
        assert resp.status_code == 400


def test_register_apns_token_requires_a_valid_device_token():
    with TestClient(public_app) as client:
        resp = client.post("/v1/device/apns-token", json={"apns_token": "fake-apns-token"})
        assert resp.status_code == 401


def test_register_apns_token_stores_it(paired_device_token):
    from server.pairing import pairing_store

    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post("/v1/device/apns-token", json={"apns_token": "fake-apns-token"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert pairing_store.apns_token_for(paired_device_token) == "fake-apns-token"


def test_register_apns_token_is_idempotent_and_supports_rotation(paired_device_token):
    from server.pairing import pairing_store

    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        client.post("/v1/device/apns-token", json={"apns_token": "first-token"}, headers=headers)
        resp = client.post("/v1/device/apns-token", json={"apns_token": "rotated-token"}, headers=headers)
        assert resp.status_code == 200
        assert pairing_store.apns_token_for(paired_device_token) == "rotated-token"


def test_permission_round_trip_allow():
    """The full flow (plan §4 steps 1-5): hook-equivalent POSTs to the internal
    endpoint and blocks; meanwhile the paired device polls /v1/permissions, finds
    the open request, and answers it. Runs both sides as concurrent tasks on
    one event loop rather than real threads/sockets, since permission_registry's
    asyncio.Future isn't safe to resolve from a different thread's loop."""

    async def _body():
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=internal_app), base_url="http://internal") as internal_client,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://public") as client,
        ):
            invite = app_module.pairing_store.issue_invite("perm-test-device")
            token = (await client.post(
                "/v1/pair", json={"device_name": "perm-test-device", "invite": invite}
            )).json()["device_token"]
            headers = {"Authorization": f"Bearer {token}"}

            async def _answer_once_visible():
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    pending = (await client.get("/v1/permissions", headers=headers)).json()["permission_requests"]
                    if pending:
                        request_id = pending[0]["request_id"]
                        assert pending[0]["tool"] == "Bash"
                        assert pending[0]["expires_at"] > pending[0]["created_at"], (
                            "client renders its countdown against this instead of a hardcoded duration"
                        )
                        answer_resp = await client.post(
                            f"/v1/permission/{request_id}", json={"decision": "allow"}, headers=headers
                        )
                        assert answer_resp.json() == {"accepted": True}
                        return
                raise AssertionError("permission request never appeared in /v1/permissions")

            # The round-trip only exists for an attended turn, and in production
            # the hook can only fire from inside a live invocation -- so say so
            # rather than relying on the module's default (server/active_turn.py
            # fails closed when nothing is running, which would deny this).
            with active_turn.running("chat"):
                internal_call = internal_client.post(
                    "/internal/permission-request",
                    json={"tool": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
                )
                internal_resp, _ = await asyncio.gather(internal_call, _answer_once_visible())
            assert internal_resp.json() == {"decision": "allow"}

    asyncio.run(_body())


# --- every denial class lands in the one denial log -------------------------
#
# The promotion rule in docs/tasks/marlow-harness-affordances.md reads exactly
# one file, so these tests are about the CLASS MARKER as much as the line: the
# classes are not interchangeable evidence and merging them without a marker
# would be worse than the split file it replaced (server/deny_detail.py).


def _denial_log() -> str:
    """conftest.py points MARLOW_LOG_DIR at the sandbox, so this is the real
    file the real handler wrote -- not a caplog capture, which could not see it
    anyway (the denial logger sets propagate=False on purpose)."""
    path = settings.log_dir / DENY_LOG_NAME
    return path.read_text() if path.exists() else ""


def test_a_declined_permission_is_logged():
    """Mike pressing Deny is the loudest signal the system can produce -- a
    deliberate answer about this exact tool use -- and before this it was
    logged NOWHERE. It is the case the promotion rule was written for: a repeat
    is a standing preference and belongs in the identity block."""

    async def _body():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:

            async def _deny_once_visible():
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    pending = permission_registry.pending()
                    if pending:
                        permission_registry.answer(pending[0].request_id, "deny")
                        return
                raise AssertionError("permission request never appeared")

            with active_turn.running("chat"):
                call = internal_client.post(
                    "/internal/permission-request",
                    json={
                        "tool": "Bash",
                        "tool_input": {"command": "curl https://declined.example"},
                        "session_id": "s-declined",
                    },
                )
                resp, _ = await asyncio.gather(call, _deny_once_visible())
            assert resp.json() == {"decision": "deny"}

    asyncio.run(_body())
    assert "denied/declined Bash: curl https://declined.example" in _denial_log()


def test_a_timed_out_permission_is_logged_as_a_different_class(monkeypatch):
    """Deliberately NOT folded in with `declined`. A timeout is a phone in a
    pocket, no signal, or asleep -- it says something about reachability, not
    about the tool. Folding the two together would let the weak signal quietly
    manufacture preferences, so the marker has to survive."""
    monkeypatch.setattr(permissions_module, "settings", replace(settings, t_hook_seconds=0.05))

    async def _body():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            with active_turn.running("chat"):
                resp = await internal_client.post(
                    "/internal/permission-request",
                    json={
                        "tool": "Bash",
                        "tool_input": {"command": "curl https://timedout.example"},
                        "session_id": None,
                    },
                )
            assert resp.json() == {"decision": "deny"}

    asyncio.run(_body())
    contents = _denial_log()
    assert "denied/timed-out Bash: curl https://timedout.example" in contents
    assert "denied/declined Bash: curl https://timedout.example" not in contents


def test_an_allowed_permission_is_not_logged_as_a_denial():
    """Guards the `decision != "allow"` condition: logging every answered
    request would drown the one file the promotion rule cats."""

    async def _body():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:

            async def _allow_once_visible():
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    pending = permission_registry.pending()
                    if pending:
                        permission_registry.answer(pending[0].request_id, "allow")
                        return
                raise AssertionError("permission request never appeared")

            with active_turn.running("chat"):
                call = internal_client.post(
                    "/internal/permission-request",
                    json={
                        "tool": "Bash",
                        "tool_input": {"command": "curl https://allowed.example"},
                        "session_id": "s-allowed",
                    },
                )
                resp, _ = await asyncio.gather(call, _allow_once_visible())
            assert resp.json() == {"decision": "allow"}

    asyncio.run(_body())
    assert "allowed.example" not in _denial_log()


def test_get_permissions_without_token_is_rejected():
    with TestClient(public_app) as client:
        resp = client.get("/v1/permissions")
        assert resp.status_code == 401


def test_pending_endpoint_is_gone(paired_device_token):
    """`/v1/pending` handed each finished turn out once, globally, so a second
    client never saw what the first had taken. The transcript replaced it and
    it was removed (docs/tasks/server-transcript.md step 3, 2026-10-01).
    Permission requests are on /v1/permissions and reauth on /v1/health."""
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        assert client.get("/v1/pending", headers=headers).status_code == 404


def test_get_permissions_returns_pending_requests():
    async def _body():
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=internal_app), base_url="http://internal") as internal_client,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://public") as client,
        ):
            invite = app_module.pairing_store.issue_invite("perm-list-device")
            token = (await client.post(
                "/v1/pair", json={"device_name": "perm-list-device", "invite": invite}
            )).json()["device_token"]
            headers = {"Authorization": f"Bearer {token}"}

            async def _find_and_allow():
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    body = (await client.get("/v1/permissions", headers=headers)).json()
                    pending = body["permission_requests"]
                    if pending:
                        assert pending[0]["tool"] == "Bash"
                        await client.post(
                            f"/v1/permission/{pending[0]['request_id']}",
                            json={"decision": "allow"},
                            headers=headers,
                        )
                        return
                raise AssertionError("permission request never appeared in /v1/permissions")

            with active_turn.running("chat"):
                internal_call = internal_client.post(
                    "/internal/permission-request",
                    json={"tool": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
                )
                internal_resp, _ = await asyncio.gather(internal_call, _find_and_allow())
            assert internal_resp.json() == {"decision": "allow"}

    asyncio.run(_body())


def test_permission_request_timeout_denies_and_posts_system_message(monkeypatch):
    monkeypatch.setattr(permissions_module, "settings", replace(settings, t_hook_seconds=0.05))

    async def _body():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            with active_turn.running("chat"):
                resp = await internal_client.post(
                    "/internal/permission-request",
                    json={"tool": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}, "session_id": None},
                )
            assert resp.json() == {"decision": "deny"}

    asyncio.run(_body())

    rows = transcript.read_after(0)["rows"]
    assert any(
        r["source"] == "system" and "couldn't reach you in time" in r["text"] for r in rows
    )


def _assert_denied_without_reaching_the_phone(source: str, tool: str, monkeypatch) -> None:
    """Deny, instantly, with no push and no registry entry.

    All four assertions are load-bearing together, and "deny" alone is the one
    that isn't: the fail-closed timeout path ALSO returns deny, just 55 seconds
    later and after pushing to a sleeping phone. That is precisely the
    behaviour step D exists to remove, so a test that only checked the verdict
    would pass against the unfixed code. Verified by reverting the guard: this
    fails, and takes 55s to do it.

    T_hook is left at its real value for the same reason -- shortening it would
    hide the difference between the two paths.
    """
    pushed: list = []
    monkeypatch.setattr(app_module, "push_broadcast_nowait", lambda payload: pushed.append(payload))

    async def _body():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            with active_turn.running(source):
                started = time.monotonic()
                resp = await internal_client.post(
                    "/internal/permission-request",
                    json={"tool": tool, "tool_input": {}, "session_id": "unattended-session"},
                )
                elapsed = time.monotonic() - started

            assert resp.json() == {
                "decision": "deny",
                # The reason code is what lets the hook tell this apart from a
                # timeout or a real refusal, so it must not become optional:
                # without it the hook tells the model Mike declined a prompt
                # that was never raised.
                "reason": active_turn.UNATTENDED_DENY_REASON,
            }
            assert elapsed < 1, (
                f"denied via the {settings.t_hook_seconds}s timeout path, not instantly"
            )

    asyncio.run(_body())

    assert pushed == [], "an unattended deny must not push to a sleeping phone"
    assert permissions_module.permission_registry.pending() == [], (
        "no request should have been registered at all"
    )


def test_a_cron_turn_is_denied_without_ringing_the_phone(monkeypatch):
    """Step D, the whole of it (2026-08-07).

    The tool here is the case that motivates it: an inbox scan running
    unsupervised at 08:00 has just read text written by strangers, and a
    half-awake Allow on the send prompt it raises is a real failure mode.
    """
    _assert_denied_without_reaching_the_phone(
        "cron", "mcp__claude_ai_Gmail__send_message", monkeypatch
    )


def test_an_unknown_source_is_treated_as_unattended(monkeypatch):
    """Fail-closed for a caller that forgets to thread `source` -- a new one
    (a web client, a CLI admin path) should get loud denials rather than
    silently ringing a phone from a context with nobody behind it."""
    _assert_denied_without_reaching_the_phone("some-future-source", "Bash", monkeypatch)


def test_an_unattended_deny_logs_the_bash_command_it_denied(monkeypatch, caplog):
    """A job that trips the same tool every morning has to be diagnosable.

    The tool name alone doesn't say which step of a job prompt to fix, and the
    unattended path raises no permission request, so there is nowhere else the
    command survives -- docs/tasks/scheduled-work.md's "treat a recurring
    timeout as the signal" needs the signal to name the call.
    """
    monkeypatch.setattr(app_module, "push_broadcast_nowait", lambda payload: None)

    async def _body():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            with active_turn.running("cron"):
                await internal_client.post(
                    "/internal/permission-request",
                    json={
                        "tool": "Bash",
                        "tool_input": {"command": "curl https://example.com/feed"},
                        "session_id": "unattended-session",
                    },
                )

    with caplog.at_level(logging.WARNING):
        asyncio.run(_body())

    assert "curl https://example.com/feed" in caplog.text
    # And into the denial log as well, so the promotion rule reads one file.
    # The duplication is deliberate and unique to this class: marlow-server.log
    # is the operational narrative, and "why did the 08:00 job produce nothing"
    # is a question answered by reading it in sequence. An attended deny needs
    # no narrative line -- Mike was holding the phone at the time.
    assert "denied/unattended Bash: curl https://example.com/feed" in _denial_log()


def test_a_denied_calls_detail_never_carries_file_content():
    """Only Bash's command is logged, and only in part.

    Edit and Write carry the file's new *content* in tool_input, and this line
    lands in journalctl on a box whose working dir is Mike's personal identity
    repo -- so the detail is an allowlist of one field, not a dump.
    """
    assert app_module._denied_call_detail("Bash", {"command": "ls memory"}) == ": ls memory"

    long_command = "ls " + "a" * 500
    detail = app_module._denied_call_detail("Bash", {"command": long_command})
    assert detail.endswith("...")
    assert len(detail) < len(long_command)

    for tool, tool_input in (
        ("Edit", {"file_path": "SOUL.md", "new_string": "a private paragraph"}),
        ("Write", {"file_path": "memory/x.md", "content": "a private paragraph"}),
        ("mcp__claude_ai_Gmail__send_message", {"body": "a private paragraph"}),
        # A Bash call with nothing usable in the field still logs cleanly.
        ("Bash", {}),
        ("Bash", {"command": ""}),
        ("Bash", {"command": ["ls"]}),
    ):
        assert app_module._denied_call_detail(tool, tool_input) == ""


def test_public_listener_uses_tls_when_cert_present(tmp_path, monkeypatch):
    cert, key = _make_self_signed_cert(tmp_path)
    monkeypatch.setattr(
        app_module, "settings", replace(settings, tls_cert_path=cert, tls_key_path=key)
    )
    config = app_module.build_public_uvicorn_config()
    assert config.ssl_certfile == str(cert)
    assert config.ssl_keyfile == str(key)


def test_public_listener_refuses_to_start_without_cert_or_optout(tmp_path, monkeypatch):
    missing_cert = tmp_path / "missing.crt"
    missing_key = tmp_path / "missing.key"
    monkeypatch.setattr(
        app_module,
        "settings",
        replace(settings, tls_cert_path=missing_cert, tls_key_path=missing_key, allow_plaintext=False),
    )
    with pytest.raises(RuntimeError) as exc_info:
        app_module.build_public_uvicorn_config()
    assert str(missing_cert) in str(exc_info.value)
    assert str(missing_key) in str(exc_info.value)


def test_public_listener_allows_plaintext_with_explicit_optout(tmp_path, monkeypatch):
    missing_cert = tmp_path / "missing.crt"
    missing_key = tmp_path / "missing.key"
    monkeypatch.setattr(
        app_module,
        "settings",
        replace(settings, tls_cert_path=missing_cert, tls_key_path=missing_key, allow_plaintext=True),
    )
    config = app_module.build_public_uvicorn_config()
    assert config.ssl_certfile is None
    assert config.ssl_keyfile is None


@pytest.mark.parametrize("allow_plaintext", [True, False])
def test_internal_listener_never_gets_tls(tmp_path, monkeypatch, allow_plaintext):
    # The internal (loopback) listener has no TLS settings at all, regardless
    # of what the public listener's cert/plaintext configuration looks like.
    cert, key = _make_self_signed_cert(tmp_path)
    monkeypatch.setattr(
        app_module,
        "settings",
        replace(settings, tls_cert_path=cert, tls_key_path=key, allow_plaintext=allow_plaintext),
    )
    config = app_module.build_internal_uvicorn_config()
    assert config.ssl_certfile is None
    assert config.ssl_keyfile is None


def test_cron_message_enqueues_and_returns_turn_id(monkeypatch):
    # A fresh WorkQueue on the test's own event loop — the module-level
    # work_queue singleton is bound to whichever TestClient portal starts it
    # first, and cross-loop asyncio.Queue access (as would happen posting via
    # internal_app's own portal) delivers real but badly delayed results.
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)

    async def _body():
        wq.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            resp = await internal_client.post(
                "/internal/cron/message",
                json={"job": "morning-scan", "text": "do the scan"},
            )
            assert resp.status_code == 202
            turn_id = resp.json()["turn_id"]
            turn = await _run_to_completion(wq, turn_id)
            assert turn.source == "cron"
            assert turn.status.value == "done"
            # The activity log's name for it (docs/tasks/ux-lane.md §2a).
            assert turn.label == "morning-scan"

            resp = await internal_client.post(
                "/internal/cron/message",
                json={"entry": "dentist-reminder", "text": "remind Mike"},
            )
            entry_turn = await _run_to_completion(wq, resp.json()["turn_id"])
            assert entry_turn.label == "dentist-reminder"
        await wq.stop()

    asyncio.run(_body())


async def _run_to_completion(wq, turn_id: str, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        turn = wq.get_turn(turn_id)
        if turn.status.value in ("done", "error"):
            return turn
        await asyncio.sleep(0.02)
    raise AssertionError(f"turn {turn_id} did not finish within {timeout}s")


def test_main_starts_queue_before_gathering_servers(monkeypatch, tmp_path):
    # Code-review finding: internal_app has no lifespan of its own, so nothing
    # previously guaranteed work_queue.start() had run before it could start
    # serving /internal/cron/message — main() now starts the queue itself
    # before racing the two uvicorn servers via asyncio.gather. Verify that
    # ordering directly rather than the (idempotent, so easy to satisfy by
    # accident) end state.
    calls: list[str] = []

    monkeypatch.setattr(
        app_module, "settings", replace(settings, allow_plaintext=True, public_bind_host="127.0.0.1")
    )
    monkeypatch.setattr(app_module, "build_identity_context", lambda settings: calls.append("identity"))

    class _FakeQueue:
        def start(self):
            calls.append("queue_start")

    monkeypatch.setattr(app_module, "work_queue", _FakeQueue())

    class _FakeServer:
        def __init__(self, config):
            pass

        async def serve(self):
            calls.append("serve")

    monkeypatch.setattr(app_module.uvicorn, "Server", _FakeServer)

    async def fake_gather(*aws):
        calls.append("gather")
        for aw in aws:
            await aw

    monkeypatch.setattr(app_module.asyncio, "gather", fake_gather)

    asyncio.run(app_module.main())

    assert calls.index("identity") < calls.index("gather")
    assert calls.index("queue_start") < calls.index("gather")


def test_cron_message_route_absent_from_public_app():
    # Security property, not a detail: nothing off-box (public/tailnet) may
    # schedule work. Assert 404, not just "no route defined for it here".
    with TestClient(public_app) as client:
        resp = client.post(
            "/internal/cron/message", json={"job": "morning-scan", "text": "do the scan"}
        )
        assert resp.status_code == 404


def test_cron_notify_creates_synthetic_turn_and_pushes_its_body(monkeypatch):
    class _FakeQueue:
        def turn_id_for_client_msg_id(self, client_msg_id):
            return None

        def post_system_message(self, text, *, client_msg_id=None):
            assert text == "Go to Costco"
            assert client_msg_id == "costco-2099-08-28-1400"
            return "turn-reminder"

    pushes = []
    monkeypatch.setattr(app_module, "work_queue", _FakeQueue())
    monkeypatch.setattr(app_module, "push_broadcast_nowait", pushes.append)

    with TestClient(internal_app) as client:
        response = client.post(
            "/internal/cron/notify",
            json={
                "entry": "costco",
                "text": "Go to Costco",
                "client_msg_id": "costco-2099-08-28-1400",
            },
        )

    assert response.status_code == 202
    assert response.json() == {"turn_id": "turn-reminder"}
    assert pushes == [
        PushPayload(type=PushType.NOTIFY, ref="turn-reminder", alert_body="Go to Costco")
    ]


def test_cron_notify_retry_does_not_repeat_turn_or_push(monkeypatch):
    from server.queue import WorkQueue

    wq = WorkQueue()
    pushes = []
    monkeypatch.setattr(app_module, "work_queue", wq)
    monkeypatch.setattr(app_module, "push_broadcast_nowait", pushes.append)
    body = {
        "entry": "costco",
        "text": "Go to Costco",
        "client_msg_id": "costco-2099-08-28-1400",
    }

    with TestClient(internal_app) as client:
        first = client.post("/internal/cron/notify", json=body)
        second = client.post("/internal/cron/notify", json=body)

    assert second.json() == first.json()
    assert len(wq._turns) == 1
    assert len(pushes) == 1


def test_cron_notify_route_absent_from_public_app():
    with TestClient(public_app) as client:
        response = client.post(
            "/internal/cron/notify",
            json={"entry": "x", "text": "x", "client_msg_id": "x-2099-01-01-0000"},
        )
    assert response.status_code == 404


def test_cron_message_dedupes_by_client_msg_id(monkeypatch):
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)

    async def _body():
        wq.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            first = await internal_client.post(
                "/internal/cron/message",
                json={"job": "morning-scan", "text": "do the scan", "client_msg_id": "morning-scan-2026-07-29"},
            )
            second = await internal_client.post(
                "/internal/cron/message",
                json={"job": "morning-scan", "text": "do the scan again", "client_msg_id": "morning-scan-2026-07-29"},
            )
            assert first.status_code == 202
            assert second.status_code == 202
            assert first.json()["turn_id"] == second.json()["turn_id"]
        await wq.stop()

    asyncio.run(_body())


def test_attaching_to_a_session_does_not_by_itself_retire_it(monkeypatch):
    """Retirement is `RETIRING_JOBS`, not a side effect of attachment.

    The two sets hold the same one job today, so this asserts the *wiring*
    rather than a difference you can observe from the outside: a job added to
    ATTACHED_SESSION_JOBS for context alone must not silently start rolling
    Mike's session. Piece 3's proactive-review job is the concrete case
    (docs/tasks/nightly-consolidation.md) — attaching it is the obvious thing
    to do, and inheriting retirement would roll the session mid-week and pay a
    cold identity-block cache write for it.
    """
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)
    monkeypatch.setattr(app_module, "ATTACHED_SESSION_JOBS", frozenset({"consolidate", "review"}))
    monkeypatch.setattr(app_module, "RETIRING_JOBS", frozenset({"consolidate"}))

    async def _body():
        wq.start()
        await _run_to_completion(wq, wq.enqueue("earlier today", source="chat", in_conversation=True))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            resp = await internal_client.post(
                "/internal/cron/message", json={"job": "review", "text": "how did the week go"}
            )
            assert resp.status_code == 202
            turn = wq.get_turn(resp.json()["turn_id"])
            assert turn.resume is True, "still attached, so it sees the day"
            assert turn.retire_session_on_success is False, "but it does not end it"
        await wq.stop()

    asyncio.run(_body())


def _schedules_store(tmp_path, monkeypatch, entries, jobs=()):
    """Point the cron endpoint's effort lookup at a store of these entries."""
    import json

    from server import schedules

    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(exist_ok=True)
    for job in jobs:
        (cron_dir / f"{job}.md").write_text("prompt\n")
    path = cron_dir / "schedules.json"
    path.write_text(json.dumps({"timezone": "America/Edmonton", "entries": entries}))
    monkeypatch.setattr(schedules, "SCHEDULES_PATH", path)
    assert schedules.load(path) is not None, "the fixture store itself must validate"


def _cron_turn_effort(monkeypatch, body):
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)
    result = {}

    async def _body():
        wq.start()
        # consolidate attaches, so it needs a conversation to attach to.
        await _run_to_completion(wq, wq.enqueue("earlier today", source="chat", in_conversation=True))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            resp = await internal_client.post("/internal/cron/message", json={**body, "text": "go"})
            assert resp.status_code == 202
            result["effort"] = wq.get_turn(resp.json()["turn_id"]).effort
        await wq.stop()

    asyncio.run(_body())
    return result["effort"]


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ("ideas", "high"), ("consolidate", "high"), ("morning", "high"), ("week-ahead", "high"),
        # Mike's own jobs left the code set (2026-10-19); their entries carry it now.
        ("weekly-digest", None), ("alberta-weekly", None),
        ("email-flash", None), ("physio-nudge", None),
    ],
)
def test_with_no_store_a_job_gets_its_code_default(tmp_path, monkeypatch, job, expected):
    """`None` leaves invoke.py to apply `settings.cron_effort`."""
    from server import schedules

    monkeypatch.setattr(schedules, "SCHEDULES_PATH", tmp_path / "absent.json")
    assert _cron_turn_effort(monkeypatch, {"job": job}) == expected


def test_a_jobs_entry_sets_its_effort(tmp_path, monkeypatch):
    _schedules_store(
        tmp_path, monkeypatch,
        [{"id": "digest", "cron": "0 18 * * 0", "job": "weekly-digest", "effort": "high"}],
        jobs=("weekly-digest",),
    )
    assert _cron_turn_effort(monkeypatch, {"job": "weekly-digest"}) == "high"


def test_a_jobs_entry_without_an_effort_falls_back_to_cron_effort(tmp_path, monkeypatch):
    _schedules_store(
        tmp_path, monkeypatch,
        [{"id": "scan", "cron": "0 11 * * *", "job": "inbox-scan"}],
        jobs=("inbox-scan",),
    )
    assert _cron_turn_effort(monkeypatch, {"job": "inbox-scan"}) is None


def test_an_entry_may_override_a_product_briefs_default(tmp_path, monkeypatch):
    """`morning` and `week-ahead` default to high but are the user's to change
    ("make the morning brief quicker"); nothing shares their cache."""
    _schedules_store(
        tmp_path, monkeypatch,
        [{"id": "morning", "cron": "0 8 * * *", "job": "morning", "effort": "low"}],
        jobs=("morning",),
    )
    assert _cron_turn_effort(monkeypatch, {"job": "morning"}) == "low"


@pytest.mark.parametrize("job", ["ideas", "consolidate"])
def test_ideas_and_consolidate_ignore_an_entrys_effort(tmp_path, monkeypatch, job):
    """They share the prompt cache inside the hour; see LOCKED_EFFORT_JOBS.
    No entry can name them any more (the harness renders them), so this pins
    the level they run at with none."""
    _schedules_store(tmp_path, monkeypatch, [], jobs=(job,))
    assert _cron_turn_effort(monkeypatch, {"job": job}) == "high"


def test_two_entries_for_one_job_take_the_higher_effort(tmp_path, monkeypatch):
    _schedules_store(
        tmp_path, monkeypatch,
        [
            {"id": "digest", "cron": "0 18 * * 0", "job": "digest", "effort": "medium"},
            {"id": "digest-once", "at": "2099-01-01T09:00", "job": "digest", "effort": "high"},
        ],
        jobs=("digest",),
    )
    assert _cron_turn_effort(monkeypatch, {"job": "digest"}) == "high"


def test_a_prompt_entry_sets_its_effort(tmp_path, monkeypatch):
    _schedules_store(
        tmp_path, monkeypatch,
        [{"id": "rebates", "cron": "0 21 * * 0", "prompt": "Research heat pump rebates.", "effort": "high"}],
    )
    assert _cron_turn_effort(monkeypatch, {"entry": "rebates"}) == "high"


def test_a_rejected_store_falls_back_to_code_defaults(tmp_path, monkeypatch):
    from server import schedules

    path = tmp_path / "schedules.json"
    path.write_text("{not json")
    monkeypatch.setattr(schedules, "SCHEDULES_PATH", path)
    assert _cron_turn_effort(monkeypatch, {"job": "morning"}) == "high"
    assert _cron_turn_effort(monkeypatch, {"job": "email-flash"}) is None


def test_every_high_effort_job_is_a_job_the_repo_ships():
    """The match is on the job name a box's schedules send, so a typo or a
    rename here would quietly drop that job back to medium on every box that
    upgrades. Each name must be one the identity template schedules. A user's own job belongs on its entry, not here (Mike,
    2026-10-19), so a migration's job no longer counts as shipped."""
    import json
    from pathlib import Path

    from server.app import HIGH_EFFORT_JOBS, LOCKED_EFFORT_JOBS

    root = Path(__file__).resolve().parent.parent
    shipped: set[str] = set()
    for store in (root / "identity/cron/schedules.json",):
        shipped |= {e["job"] for e in json.loads(store.read_text())["entries"] if "job" in e}
    # The harness's own jobs, which no store names (default-infrastructure.md).
    from server.schedules import PLUMBING_JOBS

    shipped |= PLUMBING_JOBS
    assert HIGH_EFFORT_JOBS <= shipped, HIGH_EFFORT_JOBS - shipped
    assert LOCKED_EFFORT_JOBS <= HIGH_EFFORT_JOBS


def test_cron_entry_never_gets_a_job_effort(tmp_path, monkeypatch):
    """A schedules entry is model-named: one called "consolidate" must not pick
    up the job's effort (the same rule that keeps it off the attach path). It
    gets its own entry's effort, or none."""
    _schedules_store(
        tmp_path, monkeypatch,
        [
            {"id": "consolidate", "cron": "0 21 * * 0", "prompt": "Tidy up."},
            {"id": "morning", "cron": "0 9 * * 0", "prompt": "Hello.", "effort": "low"},
        ],
    )
    assert _cron_turn_effort(monkeypatch, {"entry": "consolidate"}) is None
    assert _cron_turn_effort(monkeypatch, {"entry": "morning"}) == "low"


def test_cron_message_ordinary_job_runs_in_the_conversation(monkeypatch):
    # Since 2026-09-25 every job runs in the conversation, resolved when it
    # runs (tests/test_queue.py covers the resume itself). What sets
    # consolidate apart is binding the session at enqueue, refusing a fresh
    # fallback, and retiring -- none of which an ordinary job does.
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)

    async def _body():
        wq.start()
        # Seed a prior attended (phone) turn the way a real iOS send would.
        chat_turn_id = wq.enqueue("earlier today", source="chat", in_conversation=True)
        await _run_to_completion(wq, chat_turn_id)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            resp = await internal_client.post(
                "/internal/cron/message", json={"job": "morning", "text": "do the scan"}
            )
            assert resp.status_code == 202
            turn_id = resp.json()["turn_id"]
            turn = await _run_to_completion(wq, turn_id)
            assert turn.in_conversation is True
            assert turn.session_id is None
            assert turn.require_session is False
            assert turn.retire_session_on_success is False
        await wq.stop()

    asyncio.run(_body())


def test_cron_message_attached_job_resumes_prior_chat_session(monkeypatch):
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)

    async def _body():
        wq.start()
        chat_turn_id = wq.enqueue("earlier today", source="chat", in_conversation=True)
        chat_turn = await _run_to_completion(wq, chat_turn_id)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            resp = await internal_client.post(
                "/internal/cron/message", json={"job": "consolidate", "text": "review today"}
            )
            assert resp.status_code == 202
            turn_id = resp.json()["turn_id"]
            turn = await _run_to_completion(wq, turn_id)
            assert turn.session_id == chat_turn.result.session_id
            assert turn.resume is True
            # Must never fall back to a fresh session on a dead resume — see
            # test_require_session_turn_errors_instead_of_running_blind_on_dead_session
            # (tests/test_queue.py) for the failure mode this closes off.
            assert turn.require_session is True
        await wq.stop()

    asyncio.run(_body())


def test_cron_message_attached_job_skips_when_there_is_no_conversation(monkeypatch):
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)

    async def _body():
        wq.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            resp = await internal_client.post(
                "/internal/cron/message", json={"job": "consolidate", "text": "review today"}
            )
            assert resp.status_code == 202
            assert resp.json() == {"status": "skipped", "reason": "no conversation session"}
        await wq.stop()

    asyncio.run(_body())


def test_a_session_hint_from_an_old_app_is_ignored(monkeypatch):
    # The phone no longer picks the session (2026-09-25), but a build from
    # before that still sends `session_hint`. It must validate, and must not
    # steer the turn -- the message runs in the conversation.
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)

    async def _body():
        wq.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client, httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public_app), base_url="http://public"
        ) as client:
            invite = app_module.pairing_store.issue_invite("rollover-device")
            device_token = (await client.post(
                "/v1/pair", json={"device_name": "rollover-device", "invite": invite}
            )).json()["device_token"]
            headers = {"Authorization": f"Bearer {device_token}"}

            resp = await client.post(
                "/v1/message",
                json={"text": "hi", "session_hint": "stale-session"},
                headers=headers,
            )
            assert resp.status_code == 202
            turn_id = resp.json()["turn_id"]
            turn = wq.get_turn(turn_id)
            assert turn.in_conversation is True
            assert turn.session_id is None
            assert turn.resume is False
            await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())


def _paired_headers(
    client, internal_client, name: str = "tz-device"
) -> tuple[dict[str, str], str]:
    invite = app_module.pairing_store.issue_invite(name)
    device_token = client.post("/v1/pair", json={"device_name": name, "invite": invite}).json()["device_token"]
    return {"Authorization": f"Bearer {device_token}"}, device_token


@pytest.fixture
def isolated_pairing_state():
    """Restore the shared pairing store after tests that write a timezone.

    `turn_context.build_turn_context()` reads `latest_timezone()` globally, and
    this module sorts before tests/test_invoke.py — so without this, every later
    invocation test silently builds the "the user's timezone is Europe/Lisbon"
    branch instead of the fallback. Nothing asserts on that today, which is
    exactly why it would surface later as a test that passes alone and fails in
    the suite.
    """
    from server.pairing import pairing_store

    snapshot = {token: dict(entry) for token, entry in pairing_store._tokens.items()}
    yield
    pairing_store._tokens.clear()
    pairing_store._tokens.update(snapshot)


def test_message_records_the_devices_timezone(isolated_pairing_state):
    """Carried on the message rather than a register-once call: Mike travels,
    so a zone registered at pairing time is wrong the first time he flies."""
    from server.pairing import pairing_store

    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        headers, device_token = _paired_headers(client, internal_client)
        resp = client.post(
            "/v1/message",
            json={"text": "hi", "client_msg_id": "tz-1", "timezone": "Europe/Lisbon"},
            headers=headers,
        )
        assert resp.status_code == 202
        assert pairing_store.timezone_for(device_token) == "Europe/Lisbon"


def test_a_bad_timezone_is_dropped_without_failing_the_message(isolated_pairing_state):
    """Refusing to deliver Mike's actual message over a malformed zone string
    would be a far worse failure than a briefing in the wrong hour."""
    from server.pairing import pairing_store

    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        headers, device_token = _paired_headers(client, internal_client, "bad-tz-device")
        resp = client.post(
            "/v1/message",
            json={"text": "hi", "client_msg_id": "tz-2", "timezone": "Mars/Olympus"},
            headers=headers,
        )
        assert resp.status_code == 202
        assert pairing_store.timezone_for(device_token) is None


def test_a_message_without_a_timezone_still_works():
    """An older client that predates this field must keep working."""
    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        headers, _ = _paired_headers(client, internal_client, "old-client")
        resp = client.post(
            "/v1/message", json={"text": "hi", "client_msg_id": "tz-3"}, headers=headers
        )
        assert resp.status_code == 202


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_prepare_message_accepts_allowed_effort(effort):
    prepared = _prepare_message(MessageRequest(text="hi", effort=effort), "unused-token")
    assert prepared == (None, (), effort)


@pytest.mark.parametrize(
    "effort",
    ["--settings=x", "low --model=opus", "", "LOW", "x" * 4096],
    ids=["settings-flag", "embedded-flag", "empty", "wrong-case", "oversized"],
)
def test_prepare_message_drops_and_warns_for_bad_effort(effort, caplog):
    with caplog.at_level(logging.WARNING, logger="server.app"):
        prepared = _prepare_message(MessageRequest(text="hi", effort=effort), "unused-token")

    assert prepared == (None, (), None)
    assert "ignoring unsupported effort" in caplog.text


def test_bad_effort_does_not_fail_the_message():
    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        headers, _ = _paired_headers(client, internal_client, "bad-effort-device")
        resp = client.post(
            "/v1/message",
            json={"text": "hi", "client_msg_id": "effort-bad", "effort": "LOW"},
            headers=headers,
        )

    assert resp.status_code == 202


# --- image attachments (docs/tasks/ios-image-input.md) ----------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_message_with_image_round_trips_and_cleans_up_after(paired_device_token):
    """Acceptance #1/#3/#4: reply lands, permission_denials is empty (the fake
    CLI never denies, but this pins the field's presence/shape end to end),
    and the attachment directory is gone once the turn is terminal."""
    from server.attachments import turn_attachment_dir

    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post(
            "/v1/message",
            json={
                "text": "what's in this?",
                "client_msg_id": "img-1",
                "image_b64": _b64(b"fake jpeg bytes"),
                "image_mime": "image/jpeg",
            },
            headers=headers,
        )
        assert resp.status_code == 202
        turn_id = resp.json()["turn_id"]

        deadline = time.time() + 5
        turn_body = None
        while time.time() < deadline:
            turn_body = client.get(f"/v1/turn/{turn_id}", headers=headers).json()
            if turn_body["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert turn_body["status"] == "done"
        assert turn_body["had_permission_denial"] is False

        assert not os.path.exists(turn_attachment_dir(turn_id))


def test_message_with_image_that_fails_to_write_is_a_clean_500(paired_device_token, monkeypatch):
    """`/code-review medium` finding: write_attachment used to be called with
    no try/except, so an OSError (disk full, permissions) surfaced as an
    unhandled 500 and left the partial <turn_id>/ dir for the 24h orphan
    sweep. Now it's a clean 500 and the dir is cleaned up immediately."""
    import server.app as app_module
    from server.attachments import turn_attachment_dir

    captured_turn_ids: list[str] = []

    def _boom(turn_id, images):
        captured_turn_ids.append(turn_id)
        raise OSError("disk full")

    monkeypatch.setattr(app_module, "write_attachments", _boom)

    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post(
            "/v1/message",
            json={
                "text": "what's in this?",
                "image_b64": _b64(b"fake jpeg bytes"),
                "image_mime": "image/jpeg",
            },
            headers=headers,
        )
        assert resp.status_code == 500
        assert captured_turn_ids
        assert not os.path.exists(turn_attachment_dir(captured_turn_ids[0]))


def test_message_with_image_resent_same_client_msg_id_is_idempotent(paired_device_token):
    """Acceptance #6: an auto-retry over a flaky link lands the same image
    once, under one client_msg_id, with no duplicate turn."""
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        payload = {
            "text": "what's in this?",
            "client_msg_id": "img-retry-1",
            "image_b64": _b64(b"fake jpeg bytes"),
            "image_mime": "image/jpeg",
        }
        first = client.post("/v1/message", json=payload, headers=headers)
        second = client.post("/v1/message", json=payload, headers=headers)
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["turn_id"] == second.json()["turn_id"]


def test_message_with_oversized_image_is_rejected(paired_device_token):
    """Acceptance #7: >6MB decoded is a hard 413, not a silently-dropped image."""
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        oversized = b"x" * (6 * 1024 * 1024 + 1)
        resp = client.post(
            "/v1/message",
            json={"text": "hi", "image_b64": _b64(oversized), "image_mime": "image/jpeg"},
            headers=headers,
        )
        assert resp.status_code == 413


def test_message_with_non_jpeg_mime_is_rejected(paired_device_token):
    """Acceptance #7: any MIME that is not image/jpeg is a hard 400 — the phone
    transcodes HEIC/PNG before sending, so this is a client bug, not a format
    the server tries to accommodate."""
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post(
            "/v1/message",
            json={"text": "hi", "image_b64": _b64(b"x"), "image_mime": "image/png"},
            headers=headers,
        )
        assert resp.status_code == 400


def test_message_with_only_one_image_field_is_rejected(paired_device_token):
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        only_b64 = client.post(
            "/v1/message", json={"text": "hi", "image_b64": _b64(b"x")}, headers=headers
        )
        assert only_b64.status_code == 400

        only_mime = client.post(
            "/v1/message", json={"text": "hi", "image_mime": "image/jpeg"}, headers=headers
        )
        assert only_mime.status_code == 400


def test_message_with_invalid_base64_is_rejected(paired_device_token):
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post(
            "/v1/message",
            json={"text": "hi", "image_b64": "not-valid-base64!!!", "image_mime": "image/jpeg"},
            headers=headers,
        )
        assert resp.status_code == 400


def test_message_with_empty_image_b64_is_rejected(paired_device_token):
    """`/code-review medium` finding: an empty string passes the None-identity
    pairing check (it isn't None) and b64decode("") returns b"" with no error,
    which would otherwise sail past the >6MB size check (0 is never > 6MB)
    into a 0-byte file on disk — the "reply about an image Marlow never saw"
    failure the hard-fail design explicitly exists to refuse."""
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post(
            "/v1/message",
            json={"text": "hi", "image_b64": "", "image_mime": "image/jpeg"},
            headers=headers,
        )
        assert resp.status_code == 400


# --- several images per message (Mike, 2026-10-23) ---------------------------


def _images(*blobs: bytes) -> list[dict[str, str]]:
    return [{"b64": _b64(b), "mime": "image/jpeg"} for b in blobs]


def _attachment_dirs() -> list[str]:
    from server.config import settings

    root = str(settings.attachments_dir)
    return sorted(os.listdir(root)) if os.path.isdir(root) else []


def test_message_with_several_images_stores_them_in_order_and_records_them(paired_device_token):
    from server import transcript
    from server.attachments import turn_attachment_dir

    real_dir = turn_attachment_dir

    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post(
            "/v1/message",
            json={"text": "compare", "client_msg_id": "multi-1", "images": _images(b"one", b"two", b"three")},
            headers=headers,
        )
        assert resp.status_code == 202
        turn_id = resp.json()["turn_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            body = client.get(f"/v1/turn/{turn_id}", headers=headers).json()
            if body["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert body["status"] == "done"
        assert not os.path.exists(real_dir(turn_id))
        rows = client.get("/v1/transcript", headers=headers).json()["rows"]
        mine = [r for r in rows if r.get("client_msg_id") == "multi-1"][0]
        assert len(mine["attachments"]) == 3
        for att, expected in zip(mine["attachments"], (b"one", b"two", b"three")):
            assert transcript.attachment_path(att["id"]).read_bytes() == expected


def test_multi_image_files_are_numbered_and_handed_to_the_queue(paired_device_token, monkeypatch):
    import server.app as app_module

    captured: dict = {}
    real = app_module.write_attachments

    def _spy(turn_id, images):
        paths = real(turn_id, images)
        captured["paths"] = paths
        captured["bytes"] = [open(p, "rb").read() for p in paths]
        return paths

    monkeypatch.setattr(app_module, "write_attachments", _spy)
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post(
            "/v1/message", json={"text": "x", "images": _images(b"a", b"b")}, headers=headers
        )
        assert resp.status_code == 202
    assert [os.path.basename(p) for p in captured["paths"]] == ["0.jpg", "1.jpg"]
    assert captured["bytes"] == [b"a", b"b"]


def test_one_bad_image_rejects_the_whole_message_and_writes_nothing(paired_device_token):
    before = _attachment_dirs()
    bad = _images(b"ok1", b"ok2")
    bad.insert(1, {"b64": _b64(b"x"), "mime": "image/png"})
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        for payload in (
            bad,
            _images(b"ok") + [{"b64": "not-valid-base64!!!", "mime": "image/jpeg"}],
            _images(b"ok") + [{"b64": "", "mime": "image/jpeg"}],
            _images(b"ok") + [{"b64": _b64(b"x")}],
            _images(b"ok") + ["nope"],
        ):
            resp = client.post(
                "/v1/message", json={"text": "hi", "client_msg_id": "bad-multi", "images": payload}, headers=headers
            )
            assert resp.status_code in (400, 422), resp.text
    assert _attachment_dirs() == before


def test_a_bad_image_error_names_which_one(paired_device_token):
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post(
            "/v1/message",
            json={"text": "hi", "images": _images(b"a", b"b") + [{"b64": "!!", "mime": "image/jpeg"}]},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "images[2]" in resp.json()["detail"]


def test_too_many_images_is_a_400_and_the_cap_itself_is_accepted(paired_device_token):
    from server.app import _MAX_IMAGES

    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        over = client.post(
            "/v1/message", json={"text": "hi", "images": _images(*[b"x"] * (_MAX_IMAGES + 1))}, headers=headers
        )
        assert over.status_code == 400
        at_cap = client.post(
            "/v1/message", json={"text": "hi", "images": _images(*[b"x"] * _MAX_IMAGES)}, headers=headers
        )
        assert at_cap.status_code == 202
        empty = client.post("/v1/message", json={"text": "hi", "images": []}, headers=headers)
        assert empty.status_code == 400


def test_images_over_the_total_decoded_limit_are_a_413_though_each_is_under_6mb(paired_device_token):
    big = b"x" * (5 * 1024 * 1024)
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post(
            "/v1/message", json={"text": "hi", "images": _images(big, big, big)}, headers=headers
        )
        assert resp.status_code == 413


def test_images_together_with_the_single_image_pair_is_a_400(paired_device_token):
    with TestClient(public_app) as client:
        headers = {"Authorization": f"Bearer {paired_device_token}"}
        resp = client.post(
            "/v1/message",
            json={"text": "hi", "images": _images(b"a"), "image_b64": _b64(b"b"), "image_mime": "image/jpeg"},
            headers=headers,
        )
        assert resp.status_code == 400


def test_a_message_without_an_image_still_works():
    """An older client that predates these fields must keep working."""
    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        headers, _ = _paired_headers(client, internal_client, "no-image-client")
        resp = client.post(
            "/v1/message", json={"text": "hi", "client_msg_id": "no-img-1"}, headers=headers
        )
        assert resp.status_code == 202


# --- turn activity in the poll payload (turn-activity-streaming.md phase 1.3) --


def test_serialize_turn_carries_activity_and_never_the_tool_input():
    """The wire shape the phone reads. `activity` is additive: the shipped iOS
    `Turn` declares explicit CodingKeys and decodes with a plain JSONDecoder,
    so a client that has never heard of this key ignores it rather than failing
    to decode — which is what makes it safe to deploy the server first."""
    from server.app import _serialize_turn
    from server.invoke import ActivityEvent
    from server.queue import Turn, TurnStatus

    turn = Turn(
        turn_id="t-1", prompt="hello", session_id="s1", resume=False,
        source="chat", client_msg_id=None, status=TurnStatus.RUNNING,
    )

    # Nothing yet: an empty list, not a missing key — the client should not
    # have to tell "no activity" apart from "old server".
    assert _serialize_turn(turn)["activities"] == []

    turn.activities = [
        ActivityEvent(kind="tool_start", tool="Read", at=1234.5),
        ActivityEvent(kind="tool_end", tool=None, at=1234.7),
    ]
    body = _serialize_turn(turn)

    # Cumulative and in order: one poll every 2s has to be able to see a tool
    # call that only lasted 160ms, which is the whole reason this is a list.
    assert body["activities"] == [
        {"kind": "tool_start", "tool": "Read", "at": 1234.5},
        {"kind": "tool_end", "tool": None, "at": 1234.7},
    ]
    # §1.2 again, at the last boundary before the network: three keys, and
    # none of them is tool_input.
    assert all(set(a) == {"kind", "tool", "at"} for a in body["activities"])
    # And every pre-existing key is still exactly where it was.
    assert body["turn_id"] == "t-1"
    assert body["status"] == "running"
    assert body["session_id"] == "s1"


def test_serialize_merged_turn_names_its_survivor():
    from server.app import _serialize_turn
    from server.queue import Turn, TurnStatus

    turn = Turn(
        turn_id="absorbed", prompt="more", session_id="s1", resume=True,
        source="chat", client_msg_id="m1", status=TurnStatus.MERGED,
        merged_into="survivor",
    )

    body = _serialize_turn(turn)
    assert body["status"] == "merged"
    assert body["merged_into"] == "survivor"
    assert body["result_text"] is None


def test_routes_that_mutate_queue_state_are_async():
    """The routes that MUTATE `WorkQueue` state must be coroutine functions, so
    that mutation happens on the loop that owns the queue.

    Asserted on the wiring rather than through a request because the failure is
    a thread race with no deterministic trigger. A sync `def` here puts the
    handler on Starlette's threadpool, where `enqueue()` now raises outright
    ("must run on its owning event loop"). The removed `/v1/pending` was the
    other case: its unguarded iteration of `_turns` raced `_evict_old_turns`/
    `enqueue` and could raise "dictionary changed size during iteration" as a
    500 (`/code-review high` findings, 2026-08-27). Any new route that iterates
    queue state belongs in this list. Dropping `async` from any of these
    reads as a harmless cleanup and silently reopens that.

    `get_turn` is deliberately absent: it only does a single `dict.get`, which
    completes without a bytecode boundary for another thread to interleave on.
    """
    for handler in (app_module.post_message, app_module.cron_message):
        assert inspect.iscoroutinefunction(handler), (
            f"{handler.__name__} must stay async -- it mutates WorkQueue state"
        )


def test_an_inline_schedule_entry_cannot_select_job_behaviour(monkeypatch):
    """A schedules.json id is chosen by Marlow; a job name is approved by Mike.

    Before this split the entrypoint sent the entry id in `job`, so an entry
    called "consolidate" — a plausible name for a reminder, and the name of a
    real job on the box — would have resumed the phone's live session to run
    model-authored unattended text and then retired it (review 2026-08-30).
    Since 2026-09-25 an entry runs in the conversation like any job; what it
    must still never do is take consolidate's path and END it.
    """
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)

    async def _body():
        wq.start()
        await _run_to_completion(wq, wq.enqueue("earlier today", source="chat", in_conversation=True))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            resp = await internal_client.post(
                "/internal/cron/message",
                json={"entry": "consolidate", "text": "research heat pump rebates"},
            )
            assert resp.status_code == 202
            turn = wq.get_turn(resp.json()["turn_id"])
            assert turn.source == "cron"
            assert turn.in_conversation is True
            assert turn.retire_session_on_success is False, "an entry must never end the conversation"
            assert turn.require_session is False
            assert turn.session_id is None
        await wq.stop()

    asyncio.run(_body())


def test_cron_message_requires_exactly_one_of_job_or_entry(monkeypatch):
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)

    async def _body():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            neither = await internal_client.post("/internal/cron/message", json={"text": "hi"})
            both = await internal_client.post(
                "/internal/cron/message", json={"job": "morning", "entry": "x", "text": "hi"}
            )
            assert neither.status_code == 422
            assert both.status_code == 422

    asyncio.run(_body())


# --- first-run greeting ----------------------------------------------------


def _greetings() -> list[dict]:
    # By text, not source: startup answers an unanswered user row with its own
    # system notice ("interrupted by a restart"), which is not a greeting.
    return [r for r in transcript.read_after(0)["rows"] if r["text"] == app_module.FIRST_RUN_GREETING]


@pytest.fixture
def no_paired_devices(isolated_pairing_state):
    """The greeting needs the new device to be the only one, and the token store
    is process-wide, so earlier tests' devices would otherwise count."""
    from server.pairing import pairing_store

    pairing_store._tokens.clear()


def test_first_pairing_on_a_fresh_box_opens_with_a_greeting(no_paired_devices):
    """A new user otherwise opens the app to an empty chat with nothing to
    answer. Once, not per device: the second device pairs into a conversation
    that already exists."""
    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        _paired_headers(client, internal_client, name="first")
        assert len(_greetings()) == 1
        _paired_headers(client, internal_client, name="second")
        assert len(_greetings()) == 1


def test_a_briefing_before_pairing_does_not_cost_the_greeting(no_paired_devices):
    """The starter's 08:00 briefing runs whether anyone has paired or not. A
    friend who installs in the evening and pairs the next day must still be
    greeted (/code-review medium, 2026-10-02)."""
    transcript.append_assistant(turn_id="t-brief", text="## Morning Briefing", source="cron", status="done")
    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        _paired_headers(client, internal_client, name="late")
    assert len(_greetings()) == 1


def test_a_lost_token_file_never_greets_a_known_user(no_paired_devices):
    """device_tokens.json lost or corrupt reads as no devices, so re-pairing
    Mike's phone looks like a first pairing. His messages say otherwise."""
    transcript.append_user(turn_id="t-old", text="hello", source="ios", client_msg_id=None)
    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        _paired_headers(client, internal_client, name="repair")
    assert _greetings() == []


def test_a_quiet_box_past_retention_never_greets_a_new_device(no_paired_devices):
    """Retention can delete every message from a box that went quiet for a
    month. The phone already paired there says it isn't new."""
    from server.pairing import pairing_store

    pairing_store._tokens["existing"] = {"device_name": "phone", "paired_at": 1.0}
    transcript.append_user(turn_id="t-old", text="hello", source="ios", client_msg_id=None, now=1.0)
    assert transcript.prune(now=1.0 + transcript.RETENTION_SECONDS + 1) == 1
    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        _paired_headers(client, internal_client, name="laptop")
    assert _greetings() == []


def test_only_a_plumbing_job_dispatch_is_tagged_plumbing(monkeypatch):
    """`tier` comes from the `job` field alone (docs/tasks/default-
    infrastructure.md). An entry the model names "consolidate" is the user's
    own job, the same split that keeps it off the attach-and-retire path."""
    from server.queue import WorkQueue

    wq = WorkQueue()
    monkeypatch.setattr(app_module, "work_queue", wq)

    async def _body():
        wq.start()
        tiers = {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://internal"
        ) as internal_client:
            for field, name in (
                ("job", "ideas"), ("job", "morning"), ("entry", "consolidate"), ("entry", "ideas"),
                # The check-in is harness-timed but delivered: never plumbing.
                ("job", "email-flash"),
            ):
                resp = await internal_client.post("/internal/cron/message", json={field: name, "text": "x"})
                assert resp.status_code == 202
                tiers[(field, name)] = wq.get_turn(resp.json()["turn_id"]).tier
        await wq.stop()
        return tiers

    assert asyncio.run(_body()) == {
        ("job", "ideas"): "plumbing",
        ("job", "morning"): None,
        ("entry", "consolidate"): None,
        ("entry", "ideas"): None,
        ("job", "email-flash"): None,
    }

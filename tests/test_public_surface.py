"""What an unauthenticated caller on the public socket can reach, and how much
they can make the server read (docs/tasks/funnel-ingress.md, steps 3 and 5).

A Funnel box answers to the internet, so these pin the surface rather than
any one route: a route added later without auth fails the first test.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from server import app as app_module
from server.app import public_app, require_device_token

# Routes that are allowed to answer without a device token. Adding to this
# list is a decision about what the internet can reach: say why.
UNAUTHENTICATED = {
    # Redeems a single-use 256-bit invite; every failure is the same 400.
    ("POST", "/v1/pair"),
}


def _depends_on_token(route: APIRoute) -> bool:
    def walk(dep) -> bool:
        return dep.call is require_device_token or any(walk(d) for d in dep.dependencies)

    return walk(route.dependant)


def test_every_public_route_needs_the_token_unless_allowlisted():
    unguarded = set()
    for route in public_app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if (method, route.path) in UNAUTHENTICATED:
                continue
            if not _depends_on_token(route):
                unguarded.add((method, route.path))
    assert not unguarded, f"public routes without require_device_token: {sorted(unguarded)}"


def test_only_api_routes_and_static_fallback_exist_on_the_public_app():
    # Anything that is not an APIRoute (a Mount, a raw Route) would sit outside
    # the check above. The client is served by a 404 handler, not a route.
    others = [r for r in public_app.routes if not isinstance(r, APIRoute)]
    assert not others, others


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_no_schema_or_docs_pages_on_the_public_app(path):
    with TestClient(public_app) as client:
        resp = client.get(path)
    # A built client answers unknown GETs with its app shell, so not "is a 404":
    # the point is that no schema or Swagger page is served.
    assert "openapi" not in resp.text.lower() and "swagger" not in resp.text.lower()
    assert resp.headers["content-type"].split(";")[0] != "application/json" or resp.status_code == 404


def test_allowlisted_routes_exist():
    paths = {(m, r.path) for r in public_app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert UNAUTHENTICATED <= paths, "the allowlist names a route that is gone"


# --- body cap ----------------------------------------------------------------

def test_the_cap_covers_a_full_six_image_message():
    # 12 MiB decoded, as base64, plus the JSON around it.
    assert app_module._MAX_BODY_BYTES > app_module._MAX_TOTAL_IMAGE_BYTES * 4 // 3
    assert app_module._MAX_SMALL_BODY_BYTES < app_module._MAX_BODY_BYTES


def test_oversized_declared_length_is_refused_before_the_route_runs(paired_device_token):
    with TestClient(public_app) as client:
        resp = client.post(
            "/v1/message", content=b"x" * (app_module._MAX_BODY_BYTES + 1),
            headers={"content-type": "application/json", "authorization": f"Bearer {paired_device_token}"},
        )
    # 413, not the 422 a malformed body would otherwise give: nothing ran.
    assert resp.status_code == 413


def test_the_big_cap_is_only_for_a_valid_token(paired_device_token):
    """FastAPI reads a body before it checks the token, so a stranger must not
    get the six-image allowance: 1 MiB is refused for them and not for a device."""
    body = b"x" * (1024 * 1024)
    ctype = {"content-type": "application/json"}
    with TestClient(public_app) as client:
        assert client.post("/v1/message", content=body, headers=ctype).status_code == 413
        assert client.post(
            "/v1/message", content=body, headers={**ctype, "authorization": "Bearer not-a-token"}
        ).status_code == 413
        ok = client.post(
            "/v1/message", content=body, headers={**ctype, "authorization": f"Bearer {paired_device_token}"}
        )
    assert ok.status_code != 413


def test_other_routes_get_the_small_cap_even_with_a_token(paired_device_token):
    with TestClient(public_app) as client:
        resp = client.post(
            "/v1/preferences", content=b"x" * (app_module._MAX_SMALL_BODY_BYTES + 1),
            headers={"content-type": "application/json", "authorization": f"Bearer {paired_device_token}"},
        )
    assert resp.status_code == 413


def test_pairing_has_its_own_tiny_cap():
    with TestClient(public_app) as client:
        resp = client.post(
            "/v1/pair", json={"invite": "x" * (app_module._MAX_PAIR_BODY_BYTES + 1), "device_name": "d"}
        )
    assert resp.status_code == 413


def test_a_chunked_body_is_stopped_at_the_cap_without_a_content_length():
    async def body():
        # An async generator makes httpx send chunked: no Content-Length.
        async def chunks():
            for _ in range(app_module._MAX_PAIR_BODY_BYTES // 1024 + 4):
                yield b"x" * 1024

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public_app), base_url="http://public"
        ) as client:
            return await client.post("/v1/pair", content=chunks(), headers={"content-type": "application/json"})

    resp = asyncio.run(body())
    assert resp.status_code == 413


def test_a_body_under_the_cap_passes_through():
    with TestClient(public_app) as client:
        resp = client.post("/v1/pair", json={"invite": "nope", "device_name": "d"})
    assert resp.status_code == 400


# --- uvicorn -----------------------------------------------------------------

def test_the_public_listener_is_bounded(monkeypatch):
    monkeypatch.setenv("MARLOW_ALLOW_PLAINTEXT", "1")
    from dataclasses import replace

    monkeypatch.setattr(app_module, "settings", replace(app_module.settings, allow_plaintext=True))
    cfg = app_module.build_public_uvicorn_config()
    assert cfg.limit_concurrency == app_module.PUBLIC_LIMIT_CONCURRENCY
    assert cfg.timeout_keep_alive == app_module.PUBLIC_KEEP_ALIVE_SECONDS
    assert cfg.timeout_keep_alive <= 10
    from server.request_deadline import RequestDeadlineProtocol

    assert cfg.http is RequestDeadlineProtocol


# --- slots a stranger can hold ------------------------------------------------
# uvicorn counts a connection against limit_concurrency from the moment it
# opens and times nothing until the first response, so without a deadline a
# socket that never finishes its headers holds its slot forever: 64 of them
# locked the owner out with 503s (review, 2026-10-24).

async def _answer(scope, receive, send):
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"ok"})


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sockets_that_never_finish_their_headers_do_not_lock_out_the_owner(monkeypatch):
    import uvicorn
    from dataclasses import replace

    from server.request_deadline import RequestDeadlineProtocol

    slots = 6
    monkeypatch.setattr(app_module, "PUBLIC_LIMIT_CONCURRENCY", slots)
    monkeypatch.setattr(RequestDeadlineProtocol, "header_seconds", 0.3)
    monkeypatch.setattr(
        app_module, "settings",
        replace(app_module.settings, allow_plaintext=True, public_bind_host="127.0.0.1", public_port=_free_port()),
    )
    # The production listener config; only the app behind it is a stub.
    cfg = app_module.build_public_uvicorn_config()
    cfg.app, cfg.lifespan, cfg.log_level = _answer, "off", "error"

    async def run():
        server = uvicorn.Server(cfg)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        try:
            held = []
            for i in range(slots):
                r, w = await asyncio.open_connection(cfg.host, cfg.port)
                if i % 2:  # half send nothing, half stall mid-headers
                    w.write(b"GET / HTTP/1.1\r\nHost: x\r\n")
                    await w.drain()
                held.append((r, w))
            await asyncio.sleep(1.0)
            # The stalled sockets were closed by the server, not left open.
            for r, _ in held:
                assert await asyncio.wait_for(r.read(), 1) == b""
            r, w = await asyncio.open_connection(cfg.host, cfg.port)
            w.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            await w.drain()
            status = await asyncio.wait_for(r.readline(), 2)
            w.close()
            return status
        finally:
            server.should_exit = True
            await task

    assert asyncio.run(run()).startswith(b"HTTP/1.1 200")


def test_a_kept_alive_socket_gets_the_header_deadline_again(monkeypatch):
    """After one response, the next request's headers are under the same
    deadline; the keep-alive timer alone stops once a byte arrives."""
    import uvicorn
    from dataclasses import replace

    from server.request_deadline import RequestDeadlineProtocol

    monkeypatch.setattr(RequestDeadlineProtocol, "header_seconds", 0.3)
    monkeypatch.setattr(app_module, "PUBLIC_KEEP_ALIVE_SECONDS", 30)
    monkeypatch.setattr(
        app_module, "settings",
        replace(app_module.settings, allow_plaintext=True, public_bind_host="127.0.0.1", public_port=_free_port()),
    )
    cfg = app_module.build_public_uvicorn_config()
    cfg.app, cfg.lifespan, cfg.log_level = _answer, "off", "error"

    async def run():
        server = uvicorn.Server(cfg)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        try:
            r, w = await asyncio.open_connection(cfg.host, cfg.port)
            w.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            await w.drain()
            assert (await asyncio.wait_for(r.readuntil(b"ok"), 2)).startswith(b"HTTP/1.1 200")
            w.write(b"GET / HTTP/1.1\r\nHost: x\r\n")  # never finished
            await w.drain()
            return await asyncio.wait_for(r.read(), 2)
        finally:
            server.should_exit = True
            await task

    assert asyncio.run(run()) == b""


def _slow_body_status(app, headers: dict[str, str], gap: float) -> int:
    async def go():
        async def drip():
            for _ in range(4):
                await asyncio.sleep(gap)
                yield b"x"

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            return (await client.post("/v1/pair", content=drip(), headers=headers)).status_code

    return asyncio.run(go())


def _echo_app(**limits):
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    from server.body_limit import BodyLimitMiddleware

    async def read(request):
        return Response(str(len(await request.body())))

    return BodyLimitMiddleware(
        Starlette(routes=[Route("/v1/pair", read, methods=["POST"])]),
        default_cap=1024, is_authorized=lambda h: h == "Bearer good", **limits,
    )


def test_a_dripped_body_without_a_token_is_cut_off():
    assert _slow_body_status(_echo_app(unauth_body_seconds=0.2), {}, gap=0.1) == 408


def test_a_dripped_body_with_a_valid_token_is_not_timed():
    app = _echo_app(unauth_body_seconds=0.2)
    assert _slow_body_status(app, {"authorization": "Bearer good"}, gap=0.1) == 200
    assert _slow_body_status(app, {"authorization": "Bearer bad"}, gap=0.1) == 408


def test_the_public_app_times_unauthenticated_bodies():
    from server.body_limit import BodyLimitMiddleware

    [mw] = [m for m in public_app.user_middleware if m.cls is BodyLimitMiddleware]
    assert mw.kwargs["unauth_body_seconds"] == app_module._UNAUTH_BODY_SECONDS <= 30


def test_a_pipelined_request_is_not_cut_off_by_the_header_deadline(monkeypatch):
    """Two requests in one write: the second's headers are already parsed when
    the first response completes, so it must not inherit a fresh deadline."""
    import uvicorn
    from dataclasses import replace

    from server.request_deadline import RequestDeadlineProtocol

    monkeypatch.setattr(RequestDeadlineProtocol, "header_seconds", 0.2)
    monkeypatch.setattr(
        app_module, "settings",
        replace(app_module.settings, allow_plaintext=True, public_bind_host="127.0.0.1", public_port=_free_port()),
    )
    calls = 0

    async def slow_second(scope, receive, send):
        nonlocal calls
        calls += 1
        if calls == 2:
            await asyncio.sleep(0.6)
        await _answer(scope, receive, send)

    cfg = app_module.build_public_uvicorn_config()
    cfg.app, cfg.lifespan, cfg.log_level = slow_second, "off", "error"

    async def run():
        server = uvicorn.Server(cfg)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        try:
            r, w = await asyncio.open_connection(cfg.host, cfg.port)
            w.write(b"GET /a HTTP/1.1\r\nHost: x\r\n\r\nGET /b HTTP/1.1\r\nHost: x\r\n\r\n")
            await w.drain()
            first = await asyncio.wait_for(r.readuntil(b"ok"), 2)
            second = await asyncio.wait_for(r.readuntil(b"ok"), 2)
            w.close()
            return first, second
        finally:
            server.should_exit = True
            await task

    first, second = asyncio.run(run())
    assert first.startswith(b"HTTP/1.1 200") and b"HTTP/1.1 200" in second

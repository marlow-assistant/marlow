"""Request-size cap, in front of every route (docs/tasks/funnel-ingress.md, step 3).

`_decode_image()` checks size only after the JSON is parsed and the base64
decoded, which is too late for a socket the internet can reach. This refuses
first: on `Content-Length` when the client declares one, and by counting bytes
as they arrive when it doesn't (chunked bodies). Nothing here knows about
tokens, with one exception: the few routes that legitimately take a large body
(a six-image message, an audio segment) get their big cap only when the request
carries a valid device token. FastAPI reads a body before it resolves the auth
dependency, so without this a caller with no token could make the server buffer
the largest legitimate body on every connection. Everyone else gets the small
default.

Raw-TCP Funnel hands uvicorn a loopback peer, so there is no client IP and
no per-client budget; the cap is global by necessity.

Size is half of it. A body dripped a byte at a time holds a connection slot
for as long as it takes, so a request without a valid token also gets
`unauth_body_seconds` to deliver its whole body (review of the funnel-ingress
branch, 2026-10-24; server/request_deadline.py covers the headers). A token
holder uploading six photos on a bad link is not held to it.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyTooLarge(BaseException):
    # Not an Exception: FastAPI's body parsing catches Exception and answers
    # 400, which would hide the real reason and let the handler carry on.
    pass


class BodyTimeout(BaseException):
    # BaseException for the same reason as BodyTooLarge.
    pass


class BodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        default_cap: int,
        path_caps: dict[str, int] | None = None,
        authed_caps: dict[str, int] | None = None,
        is_authorized: Callable[[str | None], bool] = lambda _header: False,
        unauth_body_seconds: float | None = None,
    ) -> None:
        self.app = app
        self.default_cap = default_cap
        self.path_caps = path_caps or {}
        self.authed_caps = authed_caps or {}
        self.is_authorized = is_authorized
        self.unauth_body_seconds = unauth_body_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        declared = None
        authorization = None
        for name, value in scope["headers"]:
            if name == b"content-length" and declared is None:
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
            elif name == b"authorization" and authorization is None:
                authorization = value.decode("latin-1")
        authorized = self.is_authorized(authorization)
        cap = self.path_caps.get(path, self.default_cap)
        if path in self.authed_caps and authorized:
            cap = self.authed_caps[path]
        if declared is not None and declared > cap:
            await self._refuse(send)
            return

        seen = 0
        started = False
        body_done = False
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time() + self.unauth_body_seconds
            if self.unauth_body_seconds is not None and not authorized
            else None
        )

        async def counted_receive() -> Message:
            nonlocal seen, body_done
            if deadline is not None and not body_done:
                try:
                    message = await asyncio.wait_for(receive(), max(deadline - loop.time(), 0))
                except TimeoutError:
                    raise BodyTimeout from None
            else:
                message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > cap:
                    raise BodyTooLarge
                if not message.get("more_body", False):
                    body_done = True
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracking_send)
        except BodyTooLarge:
            # A handler that had already begun answering can't be taken back;
            # the connection just ends.
            if not started:
                await self._refuse(send)
        except BodyTimeout:
            if not started:
                await self._refuse(send, 408, b'{"detail":"request body too slow"}')

    @staticmethod
    async def _refuse(send: Send, status: int = 413, body: bytes = b'{"detail":"request body too large"}') -> None:
        # Connection: close, so uvicorn drops the socket rather than waiting
        # for the rest of a body nobody will read.
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

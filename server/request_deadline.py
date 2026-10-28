"""A deadline for a request's headers on the public listener (review of the
funnel-ingress branch, 2026-10-24).

uvicorn counts a connection against `limit_concurrency` from the moment it
opens, and starts no timer until its first response is done: the keep-alive
timeout only runs *between* requests. So a socket that never sends a complete
request line and headers holds its slot forever. With the cap at 64 that was
a lockout for anyone who could open 64 TLS connections, measured against a
throwaway uvicorn with the production settings: the 65th request got 503. On a
Funnel box every peer is loopback, so no per-client limit can tell the owner
from the stranger; what is left is making a held slot expire.

Each connection gets `HEADER_SECONDS` to deliver complete headers, counted from
when it opens (after the TLS handshake) and again after each response. A slow
*body* is the body-limit middleware's to bound (server/body_limit.py), because
only it knows whether the request carries a valid token.
"""
from __future__ import annotations

import asyncio

from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

HEADER_SECONDS = 10.0


class RequestDeadlineProtocol(HttpToolsProtocol):
    header_seconds = HEADER_SECONDS

    _header_deadline: asyncio.TimerHandle | None = None

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        self._arm_header_deadline()

    def on_headers_complete(self) -> None:
        self._cancel_header_deadline()
        super().on_headers_complete()

    def on_response_complete(self) -> None:
        # Read before super(), which pops the next pipelined request and starts
        # it: that one already has its headers and may run past the deadline.
        # Anything else is the next request on a kept-alive socket, which gets
        # the same deadline as the first.
        next_is_pipelined = bool(self.pipeline)
        super().on_response_complete()
        if not self.transport.is_closing() and not next_is_pipelined:
            self._arm_header_deadline()

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_header_deadline()
        super().connection_lost(exc)

    def _arm_header_deadline(self) -> None:
        self._cancel_header_deadline()
        self._header_deadline = self.loop.call_later(self.header_seconds, self._header_deadline_expired)

    def _cancel_header_deadline(self) -> None:
        if self._header_deadline is not None:
            self._header_deadline.cancel()
            self._header_deadline = None

    def _header_deadline_expired(self) -> None:
        self._header_deadline = None
        if not self.transport.is_closing():
            self.transport.close()

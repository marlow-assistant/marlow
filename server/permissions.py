"""Permission round-trip registry (plan §4; mechanics: gate1-results §2-4).

request_id -> asyncio.Future[str] ("allow"|"deny"). A request is resolved
exactly once — either by an answer via POST /v1/permission/{request_id}, or
by wait_for_decision's own timeout (fail-closed "deny"). Once resolved, any
later answer for the same request_id is discarded rather than replayed
(design §10.4) — Mike answering after the hook already denied and moved on
must not retroactively flip anything.

Futures are created inside create(), which only ever runs from a request
handler already executing on the server's single event loop — unlike
WorkQueue's asyncio.Queue, there's no import-time binding hazard here to
work around.

answer() is called from POST /v1/permission/{request_id} — a plain `def`
handler, which Starlette runs in a threadpool worker thread, not the loop
thread that owns the Future. Resolving the Future from that thread requires
call_soon_threadsafe rather than a direct set_result (asyncio.Future isn't
thread-safe to mutate off its owning loop's thread — a direct set_result from
a foreign thread can sit unseen until the loop next wakes for an unrelated
reason, which for an idle server means the *whole point* of a fast phone
answer — beating the timeout — silently doesn't happen). _resolve() below
always schedules via call_soon_threadsafe so this holds regardless of which
thread calls it.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from server.config import settings
from server.log import get_logger

logger = get_logger(__name__)


@dataclass
class PermissionRequest:
    request_id: str
    tool: str
    tool_input: dict[str, Any]
    session_id: str | None
    # The reply that asked, so "Allow similar for this reply" grants for that
    # reply and no other -- even if Mike answers after it has finished.
    turn_id: str | None = None
    # approval_summary.summarize(), computed once at creation so the push and
    # every poll describe the same thing (a Write's Create/Replace title reads
    # the filesystem).
    summary: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    decision: str | None = None
    timed_out: bool = False

    @property
    def resolved(self) -> bool:
        return self.decision is not None


class PermissionRegistry:
    def __init__(self) -> None:
        self._requests: dict[str, PermissionRequest] = {}
        self._futures: dict[str, asyncio.Future[str]] = {}

    def create(
        self,
        tool: str,
        tool_input: dict[str, Any],
        session_id: str | None,
        turn_id: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> PermissionRequest:
        request_id = str(uuid.uuid4())
        req = PermissionRequest(
            request_id=request_id,
            tool=tool,
            tool_input=tool_input,
            session_id=session_id,
            turn_id=turn_id,
            summary=summary,
        )
        self._requests[request_id] = req
        self._futures[request_id] = asyncio.get_running_loop().create_future()
        logger.info("permission request %s created for tool=%r", request_id, tool)
        return req

    async def wait_for_decision(self, request_id: str, timeout: float | None = None) -> str:
        """Blocks until answered or timed out; always returns 'allow' or
        'deny'. On timeout, resolves the request itself as 'deny' (fail-
        closed) and marks it timed_out so the caller knows to post the
        "couldn't reach you, skipped it" notice — an explicit deny answer
        gets no such notice, only a timeout does."""
        timeout = settings.t_hook_seconds if timeout is None else timeout
        future = self._futures[request_id]
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            req = self._requests[request_id]
            req.timed_out = True
            self._resolve(request_id, "deny")
            logger.warning("permission request %s timed out after %ss — denied", request_id, timeout)
            return "deny"

    def get(self, request_id: str) -> PermissionRequest | None:
        return self._requests.get(request_id)

    def answer(self, request_id: str, decision: str) -> bool:
        """Called from POST /v1/permission/{request_id} — may run on a
        threadpool worker thread (see module docstring), so this must not
        assume it's on the loop thread. Returns False (no-op) if the request
        is unknown or already resolved."""
        req = self._requests.get(request_id)
        if req is None or req.resolved:
            return False
        self._resolve(request_id, decision)
        return True

    def _resolve(self, request_id: str, decision: str) -> None:
        req = self._requests.get(request_id)
        if req is None or req.resolved:
            return
        req.decision = decision
        # Drop both entries now rather than retaining one per historical
        # request forever (systemd Restart=always means weeks/months of
        # uptime) — callers that already hold the PermissionRequest object
        # (e.g. app.py's `req` local) keep reading it fine after this, since
        # it's the same object, just no longer indexed here.
        self._requests.pop(request_id, None)
        future = self._futures.pop(request_id, None)
        if future is not None and not future.done():
            future.get_loop().call_soon_threadsafe(future.set_result, decision)

    def pending(self) -> list[PermissionRequest]:
        return list(self._requests.values())

    def oldest_pending_age(self) -> float | None:
        open_requests = self.pending()
        if not open_requests:
            return None
        return time.time() - min(r.created_at for r in open_requests)


class SimilarGrants:
    """"Allow similar for this reply" (Mike, 2026-10-02).

    One reply's grants at a time, keyed by server/approval_summary.similar_key.
    Holding only the current turn's set is what makes the scope structural
    rather than remembered: a grant for an earlier reply cannot match because
    it is gone the moment a different turn grants or asks. Cron never reaches
    here -- app.py denies unattended turns before looking.
    """

    def __init__(self) -> None:
        self._turn_id: str | None = None
        self._keys: set[tuple[str, ...]] = set()

    def grant(self, turn_id: str, key: tuple[str, ...]) -> None:
        if turn_id != self._turn_id:
            self._turn_id, self._keys = turn_id, set()
        self._keys.add(key)
        logger.info("similar grant for turn %s: %r", turn_id, key)

    def covers(self, turn_id: str | None, key: tuple[str, ...] | None) -> bool:
        return turn_id is not None and key is not None and turn_id == self._turn_id and key in self._keys


permission_registry = PermissionRegistry()
similar_grants = SimilarGrants()

"""Which paired web clients are on screen right now, for push.dispatch_web
(docs/tasks/web-push-on-screen.md).

The signal is a request carrying `X-Marlow-Foreground: 1`, which the web
client adds only while its page is visible and focused. Plain recency would
not do: a background tab keeps polling, and the service worker's own fetch
of /v1/turn is authenticated too, while the page is hidden.

Memory only. After a restart nothing is known, so every push is sent: the
failure is a banner too many, never one missed.
"""
from __future__ import annotations

import asyncio
import time


class Presence:
    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._waiters: dict[str, set[asyncio.Event]] = {}

    def mark_on_screen(self, device_token: str) -> None:
        """Called on the event loop (an async FastAPI dependency), the same
        loop the waiting dispatch runs on, so setting its event is safe."""
        self._last[device_token] = time.monotonic()
        for event in self._waiters.get(device_token, ()):
            event.set()

    def seen_within(self, device_token: str, seconds: float) -> bool:
        last = self._last.get(device_token)
        return last is not None and time.monotonic() - last <= seconds

    async def seen_again_within(self, device_token: str, seconds: float) -> bool:
        """True if the device sends an on-screen request in the next
        `seconds`; a request from before this call doesn't count."""
        event = asyncio.Event()
        waiters = self._waiters.setdefault(device_token, set())
        waiters.add(event)
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            waiters.discard(event)
            if not waiters:
                self._waiters.pop(device_token, None)

    def forget_all(self) -> None:
        """Test support."""
        self._last.clear()


presence = Presence()

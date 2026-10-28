"""Real APNs dispatch (plan §10.3, docs/tasks/apns-push-delivery.md).

Payloads are wake-and-fetch only — opaque {type, ref}, never actual content
(Gmail/calendar/message text never transits Apple's infrastructure). Two
exceptions carry text. A ``notify`` reminder, whose body is the lock-screen
product. And a ``permission_request``, which since 2026-10-02 says what Marlow
wants to do (server/approval_summary.push_text: the server's line and Marlow's
own words or the first line of an edit) -- Mike's call, on the grounds that a
one-line description of a tool call is unlikely to be a privacy problem and
that composing it on the phone was a moving part with a 55s deadline. Job and
prompt replies remain opaque. The app fetches real
content over the tailnet; see docs/tasks/ios-minimal-client.md and
docs/tasks/schedules-and-reminders.md.

Sends a visible alert with a fixed, content-free body plus
`content-available: 1` rather than a silent-only push — a `content-available`
-only push is throttled at iOS's discretion and unreliable in Low Power Mode,
unacceptable for a 07:00 nudge whose entire job is to arrive.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from server.config import settings
from server import webpush
from server.log import get_logger
from server.pairing import pairing_store
from server.presence import presence

logger = get_logger(__name__)

_GENERIC_ALERT_BODY = "Marlow has something for you."

_GONE_STATUS = "410"

# aioapns has no timeout anywhere in its own call chain — an APNs connection
# that stalls past the TLS handshake would otherwise hang send_notification()
# forever. dispatch()'s "never raises" contract only covers exceptions; this
# covers the hang case, which matters more here than a typical HTTP client
# because callers (queue.py's drain loop, app.py's permission round-trip) are
# themselves latency-sensitive.
_DISPATCH_TIMEOUT_SECONDS = 10

# APNs rejects a payload over 4KB outright, so an over-long reminder would not
# arrive at all -- and the lock screen IS the product for a `notify`, so a
# silent non-delivery is the worst available outcome. Truncate instead, with
# room left for the rest of the aps dict, and warn so the model writing
# 4KB reminders is discoverable. Nothing tells the model about this limit:
# a reminder that long is a bug in the reminder, not in the cap.
_MAX_ALERT_BODY_BYTES = 3500


def _clamp_alert_body(body: str) -> str:
    encoded = body.encode("utf-8")
    if len(encoded) <= _MAX_ALERT_BODY_BYTES:
        return body
    # Cut on a byte budget, then drop the partial character `errors="ignore"`
    # leaves behind -- a lone surrogate would fail JSON encoding downstream.
    clamped = encoded[:_MAX_ALERT_BODY_BYTES].decode("utf-8", errors="ignore")
    logger.warning(
        "alert body truncated from %d to %d bytes for APNs (limit %d)",
        len(encoded),
        len(clamped.encode("utf-8")),
        _MAX_ALERT_BODY_BYTES,
    )
    return clamped + "\u2026"


class PushType(str, Enum):
    TURN_READY = "turn_ready"
    NOTIFY = "notify"
    PERMISSION_REQUEST = "permission_request"
    REAUTH_NEEDED = "reauth_needed"
    # Sent while the session still works but is inside the pre-warn window.
    # A distinct type from REAUTH_NEEDED because the phone treats them
    # differently: this one is "go reauth at your convenience", the other
    # is "everything is already stopped".
    REAUTH_PREWARN = "reauth_prewarn"


# The auth types are the one exception to the content-free rule in the module
# docstring, and they aren't really an exception: no Gmail/calendar/message
# text is involved, only the state of our own session. A lock-screen line that
# says what it wants is the whole point of these two — the generic body is a
# wake-and-fetch nudge, and "Marlow has something for you" is exactly what
# someone swipes away without opening, which is how a reauth deadline gets
# missed.
#
# Keyed on the PushType members themselves, not their wire strings: the wire
# value is also the `type` field the iOS client switches on, so it is a
# plausible thing to rename, and a stale string literal here would miss the
# lookup and silently serve the generic body again — no exception, no log,
# just the vague line these two exist to replace.
_ALERT_BODIES = {
    PushType.REAUTH_PREWARN: "Marlow's Claude session expires soon — sign in again when you get a chance.",
    PushType.REAUTH_NEEDED: "Marlow's Claude session has expired. Sign in again to restart it.",
}


# The only category a permission push uses: Review opens the app, Deny answers
# from the lock screen. No Allow -- Mike always reviews the real request in the
# app (2026-10-02). The identifier is registered by the iOS client.
PERMISSION_CATEGORY = "PERMISSION_REQUEST_TRUNCATED"


@dataclass
class PushPayload:
    type: PushType
    ref: str
    alert_body: str | None = None
    alert_title: str | None = None

    def __post_init__(self) -> None:
        if self.type in (PushType.NOTIFY, PushType.PERMISSION_REQUEST):
            # A permission push without text would fall back to the generic
            # line, which is exactly what it was changed to stop saying.
            if not isinstance(self.alert_body, str) or not self.alert_body.strip():
                raise ValueError(f"{self.type.value} pushes require a non-empty alert body")
        elif self.alert_body is not None:
            raise ValueError("only notify and permission_request pushes may carry an alert body")
        if self.alert_title is not None and self.type != PushType.PERMISSION_REQUEST:
            raise ValueError("only permission_request pushes may carry an alert title")


# Lazily constructed on first real dispatch attempt, not at import time —
# import time is before config is necessarily final in tests. _client_checked
# distinguishes "not looked at yet" from "looked, and config is absent/broken"
# so repeated dispatch() calls with no APNs configured don't re-log every time
# — kept as two plain vars rather than packing three states into one
# (None/False/instance), which is what tests reaching into `_client` directly
# would otherwise have to know about.
_client = None
_client_checked = False


def _apns_configured() -> bool:
    return bool(
        settings.apns_key_path and settings.apns_key_id and settings.apns_team_id and settings.apns_topic
    )


def _get_client():
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    if not _apns_configured():
        logger.info("APNs not configured (MARLOW_APNS_* unset); push dispatch will no-op")
        return None
    try:
        from aioapns import APNs

        # aioapns hands `key` straight to PyJWT's jwt.encode(key=...), which
        # needs the actual PEM key material for ES256 — not a file path. Read
        # the .p8 contents here rather than passing the path through.
        key_pem = settings.apns_key_path.read_text()
        _client = APNs(
            key=key_pem,
            key_id=settings.apns_key_id,
            team_id=settings.apns_team_id,
            topic=settings.apns_topic,
            use_sandbox=settings.apns_environment == "sandbox",
        )
    except Exception:
        logger.exception("failed to construct APNs client; push dispatch will no-op")
        return None
    return _client


async def dispatch(device_token: str, payload: PushPayload) -> None:
    """Never raises into its callers — a dead APNs connection must not fail
    a permission round-trip or kill the queue's drain loop (same reasoning as
    queue.py's _reauth_needed() fail-open: a notification failing is bad, a
    wedged queue is worse)."""
    apns_token = pairing_store.apns_token_for(device_token)
    if not apns_token:
        logger.info("no apns_token registered for device=%s...; skipping push", device_token[:8])
        return

    client = _get_client()
    if client is None:
        return

    try:
        from aioapns import NotificationRequest

        # Do not put NOTIFY in _ALERT_BODIES: that map is deliberately static,
        # while a reminder's supplied text is the one content-bearing APNs
        # exception. PushPayload.__post_init__ makes a missing body fail at the
        # call site instead of quietly degrading to the generic line.
        body = (
            _clamp_alert_body(payload.alert_body or "")
            if payload.type in (PushType.NOTIFY, PushType.PERMISSION_REQUEST)
            else _ALERT_BODIES.get(payload.type, _GENERIC_ALERT_BODY)
        )
        alert: str | dict = body
        if payload.alert_title:
            alert = {"title": _clamp_alert_body(payload.alert_title), "body": body}
        aps: dict = {"alert": alert, "content-available": 1}
        message_extra: dict = {}
        request_kwargs: dict = {}
        if payload.type in (PushType.TURN_READY, PushType.NOTIFY):
            # A binary unread indicator, deliberately not an invented count:
            # the server knows that a turn became ready, but it has no per-
            # device read state. APNs applies this even when iOS has suspended
            # or terminated the app; ChatView clears it when the transcript is
            # opened. The default sound is the background receive sound — the
            # foreground path supplies its own lighter chat sound because
            # APNsRegistrar suppresses remote-notification presentation while
            # the app is active.
            aps["badge"] = 1
            aps["sound"] = "default"
        if payload.type == PushType.TURN_READY:
            # Lets a UNNotificationServiceExtension replace the generic body
            # with the real reply, fetched over the tailnet — the content
            # itself still never transits Apple's infrastructure, only this
            # permission to rewrite locally does
            # (docs/tasks/ios-notification-open-and-content-swap.md Half B).
            #
            # TURN_READY only, deliberately. NOTIFY already carries its own
            # body and has nothing to fetch; PERMISSION_REQUEST builds a
            # second local notification with Allow/Deny inside a 55s deadline,
            # and handing that path to an extension that may stall on the
            # network would spend the decision window before the buttons
            # appear.
            #
            # Harmless on a phone with no extension installed: iOS displays the
            # original alert. So this can deploy ahead of any iOS build, which
            # is what the phase-1 spike needs.
            aps["mutable-content"] = 1
        if payload.type == PushType.PERMISSION_REQUEST:
            # docs/tasks/ios-permission-round-trip-ui.md §5: a store-and-forward
            # delivery arriving after the human's decision window has closed
            # produces a notification whose buttons are already dead, and a
            # Focus mode silencing a 55s prompt is a guaranteed timeout — both
            # closed with real APNs semantics rather than app-side cleverness.
            # `interruption-level` also needs the
            # com.apple.developer.usernotifications.time-sensitive entitlement
            # on the client for iOS to actually honor it.
            aps["interruption-level"] = "time-sensitive"
            request_kwargs["time_to_live"] = int(settings.t_hook_seconds)
            aps["category"] = PERMISSION_CATEGORY
            aps["sound"] = "default"
            # The key the app's notification-action handler reads. `ref` holds
            # the same id; this keeps the handler unchanged.
            message_extra["request_id"] = payload.ref
        request = NotificationRequest(
            device_token=apns_token,
            message={"aps": aps, "type": payload.type.value, "ref": payload.ref, **message_extra},
            **request_kwargs,
        )
        result = await asyncio.wait_for(client.send_notification(request), timeout=_DISPATCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            "APNs send timed out after %ss; not delivered (device=%s...)",
            _DISPATCH_TIMEOUT_SECONDS,
            device_token[:8],
        )
        return
    except Exception:
        logger.exception("APNs send failed; not delivered (device=%s...)", device_token[:8])
        return

    try:
        if result.status == _GONE_STATUS:
            logger.info(
                "APNs reports Unregistered (410) for device=%s...; clearing stored apns_token",
                device_token[:8],
            )
            # Offloaded: _save_tokens() is a blocking file write, and this
            # runs inside a coroutine that broadcast() gathers concurrently
            # with every other paired device's dispatch() — a synchronous
            # write here would stall the whole event loop, and everyone
            # else's push, for its duration.
            await asyncio.to_thread(pairing_store.clear_apns_token, device_token)
        elif not result.is_successful:
            logger.warning(
                "APNs push not delivered: status=%s description=%s", result.status, result.description
            )
    except Exception:
        # Same "never raises" contract as the send above — updating our own
        # bookkeeping after a delivered response must not be able to wedge
        # queue.py's drain loop or fail a permission round-trip.
        logger.exception("failed to update pairing store after APNs response (device=%s...)", device_token[:8])


# docs/tasks/web-push-on-screen.md. iOS Safari must show every push, so one
# the open page would announce itself flashed a blank "Marlow has something for
# you." over the reply being watched (Mike, 2026-10-25). An on-screen web
# client asks for something at least every 15 s (its health loop), every 2 s
# while it watches a reply. RECENT: not seen in that long, it isn't on screen.
# HOLD: the health loop plus a second, long enough to hear from any page still
# up. A phone that locked just before the push sends nothing more, and gets it
# at most HOLD late; a hold, not a skip, is what keeps that push.
ON_SCREEN_RECENT_SECONDS = 20.0
ON_SCREEN_HOLD_SECONDS = 16.0

# What a focused page announces itself (planFor in
# web/src/lib/pushNotification.ts). A permission prompt is not held: up to
# 16 s out of its 55 s window, for a phone just locked, costs more than a
# flash. The reauth pushes show whether focused or not.
_ANNOUNCED_BY_AN_OPEN_PAGE = (PushType.TURN_READY, PushType.NOTIFY)


async def _web_page_on_screen(device_token: str, endpoint: str, push_type: PushType) -> bool:
    """Apple's endpoint only: Chrome and Firefox may show nothing for a focused
    page, and their service worker sees the window itself. APNs never comes
    here: the native app presents nothing in the foreground and iOS doesn't
    require it to."""
    if push_type not in _ANNOUNCED_BY_AN_OPEN_PAGE or not webpush.is_apple(endpoint):
        return False
    if not presence.seen_within(device_token, ON_SCREEN_RECENT_SECONDS):
        return False
    return await presence.seen_again_within(device_token, ON_SCREEN_HOLD_SECONDS)


async def dispatch_web(device_token: str, payload: PushPayload) -> None:
    """Web Push counterpart of dispatch(): same types, same content rules,
    never raises. Builds the payload here (not in webpush.py) so every
    content decision stays next to the APNs one."""
    try:
        subscription = pairing_store.webpush_subscription_for(device_token)
        if not subscription:
            return
        if await _web_page_on_screen(device_token, subscription["endpoint"], payload.type):
            logger.info("web push skipped: the page is on screen (device=%s...)", device_token[:8])
            return
        data: dict = {"type": payload.type.value, "ref": payload.ref}
        if payload.type in (PushType.NOTIFY, PushType.PERMISSION_REQUEST):
            # The types that carry text, as on APNs. turn_ready stays opaque:
            # the service worker fetches /v1/turn/{ref} itself.
            data["alert_body"] = _clamp_alert_body(payload.alert_body or "")
        if payload.alert_title:
            data["alert_title"] = _clamp_alert_body(payload.alert_title)
        await webpush.send(
            device_token,
            data,
            permission_request=payload.type == PushType.PERMISSION_REQUEST,
            high_urgency=payload.type
            in (PushType.PERMISSION_REQUEST, PushType.TURN_READY, PushType.NOTIFY),
        )
    except Exception:
        logger.exception("web push dispatch failed unexpectedly (device=%s...)", device_token[:8])


async def broadcast(payload: PushPayload) -> None:
    """Push to every currently-paired device (plan §4 step 3: "the paired
    device(s)", plural). Concurrent, not sequential — dispatch() never raises
    (see its own docstring), so there's no error-handling reason to wait on
    one device's APNs round-trip before starting the next, and the three
    callers of this (a permission request, a reauth wake, a turn-ready wake)
    all want the fastest possible fan-out, not the slowest.

    Never raises, matching dispatch()'s own contract — this wraps
    all_device_tokens() too, not just the gather, since a caller-visible
    exception from here is exactly as unacceptable as one from dispatch()
    itself (a wedged work-queue drain loop, or a failed permission round-trip
    over a notification failure)."""
    try:
        # Web Push rides the same fan-out: one device may hold an APNs token,
        # a browser subscription, or both, and each send is a no-op when its
        # half is absent.
        await asyncio.gather(
            *(
                send(device_token, payload)
                for device_token in pairing_store.all_device_tokens()
                for send in (dispatch, dispatch_web)
            )
        )
    except Exception:
        logger.exception("push broadcast failed unexpectedly")


_background_broadcasts: set[asyncio.Task] = set()


def broadcast_nowait(payload: PushPayload) -> None:
    """Fire-and-forget wrapper for callers on a latency-sensitive path that
    must not block on APNs delivery finishing: the permission round-trip
    (app.py) sits in front of hooks/pretooluse.py's own T_hook-based timeout,
    and the work queue (queue.py) serializes every `claude -p` call behind a
    single drain loop. broadcast() is bounded and never raises, but even a
    bounded wait is latency neither path can afford to spend on notification
    delivery rather than the actual request it's there to unblock.

    Keeps a strong reference to the task until it finishes — asyncio doesn't
    guarantee a fire-and-forget task survives without one."""
    task = asyncio.create_task(broadcast(payload))
    _background_broadcasts.add(task)
    task.add_done_callback(_background_broadcasts.discard)

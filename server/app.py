"""HTTP app + route wiring (plan §2/§3).

Three separate ASGI apps, bound to three different places:
  - `public_app` — the tailnet-facing client API (§3.1). Bound to the
    tailscale interface only (config.py refuses to default to 0.0.0.0).
  - `internal_app` — on an owner-only Unix socket (§3.3; config.py,
    `internal_socket_path`). The hook's permission requests and cron turns.
    A turn can reach it: the hook runs inside the bwrap namespace.
  - `admin_app` — on a second owner-only socket that a turn cannot see
    (config.py, `admin_socket_path`). Pairing invites, the device list,
    revocation: these have to live on the running server process (see
    pairing.py's module docstring for why a standalone script can't do this).

The permission-round-trip endpoints (`/v1/permission/{request_id}`,
`/internal/permission-request`) are backed by permissions.py's Future-based
registry (gate #4, plan §4).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import os
import stat
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from server import active_turn, activity_log, changes, deny_detail, health, ideas, onboarding, schedules, transcribe, transcript, transparency, usage_report, web_static, webpush
from server.auth_monitor import auth_watcher
from server.body_limit import BodyLimitMiddleware
from server.request_deadline import RequestDeadlineProtocol
from server.attachments import cleanup_attachment, write_attachments
from server.config import settings
from server.identity import build_identity_context
from server.idle_compaction import SOURCE as COMPACTION_SOURCE
from server.idle_compaction import idle_compaction_watcher
from server.location import Location, location_store
from server.location import validate as validate_location
from server.log import get_logger
from server.pairing import PairingError, pairing_store
from server.presence import presence
from server.preferences import EFFORTS, MAX_CHECKIN_EVERY_HOURS, MAX_FACE_ID, DayError, preferences_store
from server import approval_summary
from server.active_turn import SIMILAR_ALLOW_REASON
from server.permissions import PermissionRequest, permission_registry, similar_grants
from server.push import PushPayload, PushType, broadcast_nowait as push_broadcast_nowait
from server.queue import TurnStatus, work_queue
from server.transcribe import transcriber
from server.turn_context import is_valid_timezone
from server.usage_state import usage_state

logger = get_logger(__name__)


# --- shared auth dependency -------------------------------------------------

async def require_device_token(
    authorization: str | None = Header(default=None),
    x_marlow_foreground: str | None = Header(default=None),
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not pairing_store.is_valid_token(token):
        raise HTTPException(status_code=401, detail="invalid device token")
    # The web client's "my page is on screen" (server/presence.py). Async, so
    # this runs on the event loop the held pushes wait on.
    if x_marlow_foreground == "1":
        presence.mark_on_screen(token)
    return token


def _serialize_permission_request(req: PermissionRequest) -> dict[str, Any]:
    return {
        "request_id": req.request_id,
        "tool": req.tool,
        "tool_input": req.tool_input,
        # What the pane says, beside the raw input rather than instead of it
        # (server/approval_summary.py has the trust layering).
        "summary": req.summary or approval_summary.summarize(req.tool, req.tool_input),
        "created_at": req.created_at,
        # The client renders a countdown against this rather than keeping its
        # own copy of t_hook_seconds — one source of truth for when the hook
        # actually gives up (docs/tasks/ios-permission-round-trip-ui.md §4).
        "expires_at": req.created_at + settings.t_hook_seconds,
    }


def _pending_permission_payloads() -> list[dict[str, Any]]:
    return [_serialize_permission_request(r) for r in permission_registry.pending()]


def _serialize_turn(turn) -> dict[str, Any]:
    return {
        "turn_id": turn.turn_id,
        "status": turn.status.value,
        "source": turn.source,
        "result_text": turn.result.result_text if turn.result else None,
        "session_id": turn.result.session_id if turn.result else turn.session_id,
        "had_permission_denial": turn.result.had_permission_denial if turn.result else False,
        "error": turn.error,
        "created_at": turn.created_at,
        "merged_into": turn.merged_into,
        # Additive, and safe to deploy ahead of any client that reads it: the
        # iOS `Turn` declares explicit CodingKeys and decodes with a plain
        # JSONDecoder, so a key it has never heard of is ignored rather than
        # being a decode failure. Empty on every turn that is not an attended
        # one mid-flight — see `queue.Turn.activities`.
        #
        # A list, not the single object this was until 2026-08-22: the client
        # polls every 2s and a local file read takes ~0.16s, so one slot could
        # not show a short tool call at all. Cumulative for the turn, so one
        # poll sees everything since the last one and can replay it.
        "activities": [_serialize_activity(a) for a in turn.activities],
        # The reply as it is being written (docs/tasks/streaming-reply-text.md).
        # Cumulative and whole every time -- the client renders it if it grew,
        # with no cursor to keep and nothing to resync after a missed poll.
        # Additive and client-optional for the same CodingKeys reason as above.
        #
        # Empty once the turn is terminal, and that is deliberate: `result_text`
        # above is authoritative from that moment, and a client must render it
        # rather than the approximation it accumulated.
        "partial_text": turn.partial_text,
        "partial_text_marks": list(turn.partial_text_marks),
    }


def _serialize_activity(activity) -> dict[str, Any]:
    """The tool NAME and nothing else, by construction — ActivityEvent has no
    field carrying tool input, and this asks for the three it does have rather
    than reflecting over whatever it might grow
    (docs/tasks/turn-activity-streaming.md §1.2)."""
    return {"kind": activity.kind, "tool": activity.tool, "at": activity.at}


# --- public (tailnet) app ----------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Fail loud at startup, not on the first turn — a missing/empty SOUL.md or
    # USER.md must not let the server come up looking healthy and then error
    # on every single invocation (docs/tasks/loader-identity-injection.md).
    build_identity_context(settings)
    work_queue.start()
    auth_watcher.start()
    idle_compaction_watcher.start()
    # In the background, on the transcription worker. If loading holds the GIL
    # the loop pauses for it (2.4 s on the slowest box measured), which
    # update.sh's 10 s health wait covers.
    transcriber.preload_if_used()
    yield
    transcriber.shutdown()
    await idle_compaction_watcher.stop()
    await auth_watcher.stop()
    await work_queue.stop()


# No /docs, /redoc or /openapi.json: they are unauthenticated, and on a Funnel
# box they would hand the whole API schema to anyone who finds the name.
public_app = FastAPI(
    title="marlow-public", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None
)


class PairRequest(BaseModel):
    invite: str
    device_name: str


class MessageRequest(BaseModel):
    text: str
    # No `session_hint` since 2026-09-25: the server owns the one conversation
    # (server/session_state.py), so which session a message lands in is not
    # the device's to say. An older app build still sends one; pydantic drops
    # unknown fields, so it validates and is ignored.
    client_msg_id: str | None = None
    # Optional for backward compatibility. Validated and reduced to one of
    # three server-owned constants in _prepare_message before it reaches the
    # queue (docs/tasks/effort-toggle-server-half.md).
    effort: str | None = None
    # IANA name, e.g. "America/Edmonton". Optional so an older client keeps
    # working; carried on the message rather than a dedicated endpoint because
    # every turn is already a report of where the phone is, and Mike travels —
    # a register-once call would go stale the first time he flies.
    timezone: str | None = None
    # Images, inline (docs/tasks/ios-image-input.md — inline base64 beats a
    # separate upload endpoint: one round trip, one idempotency key, no
    # orphaned-upload problem). `images` is the multi-image form
    # ([{"b64", "mime"}], at most _MAX_IMAGES); `image_b64`/`image_mime` is the
    # original single-image pair, still accepted so an older client keeps
    # working. All optional; the pair is both-or-neither and the two forms are
    # exclusive. Checked in _decode_images rather than here, and `images` is a
    # loose list of dicts rather than a model, so a malformed entry reaches the
    # client as a 400 with a specific message, not a pydantic 422.
    image_b64: str | None = None
    image_mime: str | None = None
    images: list[dict[str, Any]] | None = None
    # Where the device is, at the precision the user picked
    # (docs/tasks/message-location.md). A loose dict, not a nested model, on
    # purpose: a nested model turns a malformed location into a 422 that
    # refuses the whole message, and this field is warn-and-drop like
    # `timezone`. Absent clears what the server holds for this device.
    location: dict[str, Any] | None = None
    # Set when the message is a "Do it" tap on an idea
    # (docs/tasks/ideas-panel.md). Warn-and-drop like `timezone`: an idea that
    # is no longer open must not cost Mike the message.
    idea_id: str | None = None
    # True when the user spoke this in voice mode and will hear the reply read
    # aloud (docs/tasks/voice-mode-hands-free.md). A property of this message,
    # not of the device: a spoken and a typed message can share the queue.
    # `Any`, read as `is True`: warn-and-drop like the fields above, so a
    # malformed value reads as typed instead of 422ing the message.
    spoken: Any = None


def _parse_location(raw: dict[str, Any] | None) -> Location | None:
    """The device's report, or None for absent or malformed. Never raises."""
    if raw is None:
        return None

    def number(key: str) -> float | None:
        value = raw.get(key)
        # bool is an int subclass; `true` is not a latitude.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            return float(value)
        except OverflowError:
            # A 400-digit JSON integer parses as a Python int that float()
            # refuses, and this function must never cost the message.
            return None

    latitude, longitude = number("latitude"), number("longitude")
    place = raw.get("place")
    parsed = (
        validate_location(
            latitude, longitude, number("accuracy_m"), place if isinstance(place, str) else None
        )
        if latitude is not None and longitude is not None
        else None
    )
    if parsed is None:
        # Coordinates only, never `place`: it is free text and this is a log.
        # Bounded like the effort preview: a hostile value must not turn one
        # warning into kilobytes of log.
        logger.warning(
            "ignoring malformed location from device (latitude=%.80r, longitude=%.80r)",
            raw.get("latitude"), raw.get("longitude"),
        )
    return parsed


# Unlike the timezone field's warn-and-drop (a client bug there costs a
# briefing in the wrong hour), a bad image is a hard fail: silently dropping
# it and sending the caption alone would produce a reply about an image
# Marlow never saw, which is worse than an error.
_MAX_IMAGE_BYTES = 6 * 1024 * 1024
# Multi-image messages (Mike, 2026-10-23): at most 6 images, and a ceiling on
# their decoded total. Like the 6MB this is a bound against a hostile or buggy
# client, not what the app sends (~300KB each after the on-device downscale).
_MAX_IMAGES = 6
_MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024

# The largest legitimate body is a full six-image message: the decoded ceiling
# as base64 (4/3), plus room for the caption and the rest of the JSON. Derived
# from the limits above so they can't drift apart. Refused before any route
# runs (server/body_limit.py), and only with a valid device token: every other
# request, and these two without one, gets the small default. Pairing is the
# route a stranger can reach, so it gets a tiny cap.
_MAX_BODY_BYTES = _MAX_TOTAL_IMAGE_BYTES * 4 // 3 + 256 * 1024
_MAX_SMALL_BODY_BYTES = 64 * 1024
_MAX_PAIR_BODY_BYTES = 4 * 1024
# Without a valid token, the whole body inside this, or 408. At most 64 KiB, so
# ten seconds is generous on any link a phone pairs over.
_UNAUTH_BODY_SECONDS = 10.0
# Audio segments are capped again by the route itself (transcribe.MAX_SEGMENT_BYTES).
_MAX_AUDIO_BODY_BYTES = transcribe.MAX_SEGMENT_BYTES + 64 * 1024


def _bearer_is_valid(authorization: str | None) -> bool:
    return bool(
        authorization
        and authorization.startswith("Bearer ")
        and pairing_store.is_valid_token(authorization.removeprefix("Bearer ").strip())
    )


public_app.add_middleware(
    BodyLimitMiddleware,
    default_cap=_MAX_SMALL_BODY_BYTES,
    path_caps={"/v1/pair": _MAX_PAIR_BODY_BYTES},
    authed_caps={"/v1/message": _MAX_BODY_BYTES, "/v1/transcribe": _MAX_AUDIO_BODY_BYTES},
    is_authorized=_bearer_is_valid,
    unauth_body_seconds=_UNAUTH_BODY_SECONDS,
)
_ALLOWED_IMAGE_MIME = "image/jpeg"
_ALLOWED_CLIENT_EFFORTS = ("low", "medium", "high")


def _decode_image(image_b64: str, image_mime: str, label: str = "image_b64") -> bytes:
    """`label` names the offending field in an error: `image_b64` for the
    legacy pair, `images[i]` for the list, so a client can tell which of up to
    six is bad."""
    if image_mime != _ALLOWED_IMAGE_MIME:
        raise HTTPException(status_code=400, detail=f"unsupported image MIME type: {image_mime!r}")
    try:
        data = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"{label} is not valid base64") from e
    if not data:
        # An empty string passes both the caller's None-identity pairing check
        # and b64decode (which happily returns b"" for ""), and would
        # otherwise sail past the size check below (0 is never > 6MB) into a
        # 0-byte file on disk — the exact "reply about an image Marlow never
        # saw" failure this function exists to refuse.
        raise HTTPException(status_code=400, detail=f"{label} decoded to zero bytes")
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} exceeds the 6MB decoded limit")
    return data


def _decode_images(body: MessageRequest) -> list[bytes]:
    """Every image on the request, decoded and validated, or an HTTPException.

    All-or-nothing (contract CMP-4): one bad image rejects the whole message,
    and nothing has been written to disk by the time this returns or raises, so
    the caption is never sent with some images missing. Both wire forms are
    folded into one list here; sending both is a 400 rather than a guess about
    which one the client meant."""
    # Both present or both absent — checked before either is used, so a
    # malformed pair never gets partway through decoding.
    if (body.image_b64 is None) != (body.image_mime is None):
        raise HTTPException(
            status_code=400, detail="image_b64 and image_mime must both be present or both absent"
        )
    if body.images is not None and body.image_b64 is not None:
        raise HTTPException(status_code=400, detail="send either images or image_b64/image_mime, not both")
    if body.images is None:
        if body.image_b64 is None or body.image_mime is None:
            return []
        return [_decode_image(body.image_b64, body.image_mime)]

    if not body.images:
        raise HTTPException(status_code=400, detail="images must not be empty")
    if len(body.images) > _MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"at most {_MAX_IMAGES} images per message")
    decoded: list[bytes] = []
    for i, item in enumerate(body.images):
        b64, mime = item.get("b64"), item.get("mime")
        if not isinstance(b64, str) or not isinstance(mime, str):
            raise HTTPException(status_code=400, detail=f"images[{i}] needs string b64 and mime")
        decoded.append(_decode_image(b64, mime, f"images[{i}]"))
        # Running total, so a hostile body stops costing memory at the image
        # that crosses the line rather than after all six are decoded.
        if sum(len(d) for d in decoded) > _MAX_TOTAL_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="images exceed the 12MB decoded total limit")
    return decoded


# The first thing a new box ever says. The user would otherwise open the app to
# an empty chat with nothing to answer (friends-alpha dry run, 2026-10-02). It
# is a canned system turn, not a model call, so the model never saw it, and the
# starter identity's FIRST-RUN.md tells it the greeting went out. Keep the two
# in step: FIRST-RUN.md describes what this asks, without quoting it. No name:
# the one typed at install may be the wrong one, which is half of why we ask.
# One question only: the first version also asked what they wanted help with,
# the user answered just the name, and the model never came back to it (Mike's
# VM run, 2026-10-02). FIRST-RUN.md leads the rest one step at a time.
FIRST_RUN_GREETING = (
    "Hi, I'm Marlow. You can reach me here any time, and I remember what "
    "matters from one day to the next. Let's get you set up. It takes a "
    "few minutes, and I'll show you what I can do as we go. First, what should "
    "I call you?"
)


@public_app.post("/v1/pair")
async def pair(body: PairRequest) -> dict[str, str]:
    # The store writes its token file, so off the loop. The greeting stays ON
    # it: post_system_message mutates queue-owned state, which belongs to the
    # loop (see post_message), and with no await between the check and the post
    # two devices pairing at once can't both greet.
    try:
        device_token = await asyncio.to_thread(pairing_store.redeem, body.invite, body.device_name)
    except PairingError as e:
        # One body for every failure, whatever the cause: this route is
        # reachable from the internet on a Funnel box.
        raise HTTPException(status_code=400, detail="pairing failed") from e
    # Both halves, because each alone misfires on Mike's box. "Only device": a
    # lost or corrupt device_tokens.json reads as empty, so re-pairing his phone
    # would greet him as a stranger. "No user message": retention empties the
    # transcript of a box that went quiet for a month. Needing both makes a
    # misfire need both at once. Neither half asks whether the conversation is
    # empty, because a scheduled job may have replied before anyone paired.
    if len(pairing_store.all_device_tokens()) == 1 and transcript.awaiting_first_contact(
        FIRST_RUN_GREETING
    ):
        logger.info("first pairing on a fresh box: posting the greeting")
        work_queue.post_system_message(FIRST_RUN_GREETING)
    return {"device_token": device_token}


@public_app.post("/v1/message", status_code=202)
async def post_message(
    body: MessageRequest, token: str = Depends(require_device_token)
) -> dict[str, str]:
    # This handler itself must stay on the server's event loop: enqueue()
    # mutates all queue-owned state there as one operation. Image decode/write
    # and pairing-store persistence are blocking, so they go to a thread rather
    # than onto the loop alongside the queue mutation.
    #
    # NOT the same pool the old sync route used, and deliberately said out
    # loud: a sync `def` route runs on Starlette's private anyio threadpool
    # (CapacityLimiter 40), while asyncio.to_thread uses the loop's DEFAULT
    # executor -- min(32, cpu_count + 4), so 6 threads on a 2-core box -- which
    # queue.py already shares for sweep_orphaned_attachments and for _drain's
    # awaited commit_after_turn. A burst of 6MB image sends can therefore
    # contend with the drain loop in a way the old arrangement could not
    # (`/code-review high` finding, 2026-08-27). Acceptable at one phone; if a
    # second attended client ever exists, give this its own bounded executor
    # rather than widening the default one.
    attachment_turn_id, attachment_paths, effort = await asyncio.to_thread(
        _prepare_message, body, token
    )

    # The user's bubble (docs/tasks/server-transcript.md), written BEFORE the
    # turn is queued and so before the client can have its turn_id: a client
    # that sees the row must find the turn it names. That needs the id now, so
    # it is minted for every message, not only image ones (enqueue's own
    # minting would come too late).
    #
    # Unless the queue already has a live turn for this client_msg_id: then
    # enqueue will hand THAT id back and discard ours, so the row must carry it.
    # Otherwise a first send whose transcript write failed would be retried
    # into a row stamped with an id the client never receives. `reusable_turn_id`
    # is the same check enqueue uses, so the two cannot disagree.
    reusable = work_queue.reusable_turn_id(body.client_msg_id) if body.client_msg_id else None
    new_turn_id = reusable or attachment_turn_id or str(uuid.uuid4())
    # Looked up before the row is written, because the bubble quotes it. Only
    # the prompt carries the idea line; the row keeps the text Mike typed or
    # tapped, plus the quote.
    idea = await asyncio.to_thread(_open_idea, body.idea_id) if body.idea_id else None
    row_turn_id = await asyncio.to_thread(
        _record_user_message, body, new_turn_id, attachment_paths, idea
    )

    turn_id = work_queue.enqueue(
        f"{ideas.accepted_context(idea)}\n\n{body.text}" if idea else body.text,
        in_conversation=True,
        source="chat",
        client_msg_id=body.client_msg_id,
        turn_id=new_turn_id,
        attachment_paths=attachment_paths,
        effort=effort,
        spoken=body.spoken is True,
        # A retry whose row already existed under another turn_id (the first
        # turn ERRORed, or the queue's 600s window forgot it): the row is not
        # rewritten, so this turn's reply names it. Ignored by enqueue if the
        # resend dedups, which returns the first turn unchanged.
        answers_turn_ids=[row_turn_id] if row_turn_id and row_turn_id != new_turn_id else [],
    )
    if idea is not None:
        # After enqueue, not before: a send that fails must leave the idea
        # open, or the retry would run without it and the card would be gone.
        await asyncio.to_thread(ideas.accept, idea.id)
    return {"turn_id": turn_id}


def _open_idea(idea_id: str) -> ideas.Idea | None:
    """The idea a "Do it" tap names, or None when it is not open. A resend of
    the same message finds it already accepted and gets None. Inside the
    queue's dedup window that is harmless, because the resend returns the first
    turn, whose prompt carried the line. Past it, the retry runs on the
    bubble's "Do it: <title>" alone, which still names the idea."""
    idea = ideas.find_open(idea_id)
    if idea is None:
        logger.warning("ignoring Do it on an idea that is not open (%.60r)", idea_id)
    return idea


def _record_user_message(
    body: MessageRequest, turn_id: str, attachment_paths: tuple[str, ...], idea: ideas.Idea | None = None
) -> str | None:
    """Write the transcript's user row, and copy the image beside it. Runs in a
    worker thread: it is SQLite and file I/O. Returns the turn_id the row
    carries -- `turn_id` for a new row, the first send's for a dedup -- or None
    if the transcript could not be written.

    The image is copied from the attachment `_prepare_message` already wrote
    rather than re-decoded, and BEFORE the turn is enqueued, so the copy
    cannot lose a race with the drain's `cleanup_attachment`. `append_user` never raises: a transcript that cannot be written must
    not cost Mike the message he sent."""
    row = transcript.append_user(
        turn_id=turn_id,
        text=body.text,
        source="chat",
        client_msg_id=body.client_msg_id,
        attachments=[(path, _ALLOWED_IMAGE_MIME) for path in attachment_paths],
        quote={"idea_id": idea.id, "emoji": idea.emoji, "title": idea.title} if idea else None,
    )
    return row["turn_id"] if row else None


def _prepare_message(
    body: MessageRequest, token: str
) -> tuple[str | None, str | None, str | None]:
    """Blocking validation/persistence performed off the server event loop."""
    effort: str | None = None
    if body.effort is not None:
        # Return the matched tuple member, not the request string. invoke.py
        # repeats this closed-set match at the argv boundary so a future caller
        # cannot accidentally bypass the HTTP validation.
        effort = next(
            (allowed for allowed in _ALLOWED_CLIENT_EFFORTS if body.effort == allowed),
            None,
        )
        if effort is None:
            # Warn-and-drop like timezone: an app bug should not cost Mike the
            # message he actually sent. Bound the preview so a hostile 4KB
            # value cannot turn one warning into 4KB of log noise.
            preview = (
                body.effort if len(body.effort) <= 80 else body.effort[:77] + "..."
            )
            logger.warning(
                "ignoring unsupported effort %r from device; using server default",
                preview,
            )

    if body.timezone is not None:
        # Warn-and-drop rather than 400: a client sending a bad zone string is
        # a client bug, and refusing to deliver Mike's actual message over it
        # would be a much worse failure than a briefing in the wrong hour.
        if is_valid_timezone(body.timezone):
            pairing_store.set_timezone(token, body.timezone)
        else:
            logger.warning("ignoring unresolvable timezone %r from device", body.timezone)

    # Every message reports, including "nothing": absent clears this device's
    # entry, which is how Off takes effect at once.
    location_store.report(token, _parse_location(body.location))

    # Decode and validate EVERY image before writing any of them.
    images = _decode_images(body)

    attachment_turn_id: str | None = None
    attachment_paths: tuple[str, ...] = ()
    if images:
        # Minted here, not left to enqueue(), because the files have to exist at
        # these paths BEFORE the turn is queued (docs/tasks/ios-image-input.md
        # §2) — the worker could otherwise reach the turn before the write
        # lands. This helper is already in a worker thread; only the completed
        # paths are handed back to the event-loop-owned enqueue operation.
        attachment_turn_id = str(uuid.uuid4())
        try:
            attachment_paths = write_attachments(attachment_turn_id, images)
        except OSError as e:
            # Nothing was enqueued yet, so cleanup_attachment's usual trigger
            # (the drain loop, on a terminal turn) never runs for whatever
            # os.makedirs already created — remove it here instead of leaving
            # it for the 24h orphan sweep (`/code-review medium` finding).
            cleanup_attachment(attachment_turn_id)
            logger.exception("failed to write attachment for turn %s", attachment_turn_id)
            raise HTTPException(status_code=500, detail="failed to store attachment") from e

    return attachment_turn_id, attachment_paths, effort


@public_app.get("/v1/ideas")
def get_ideas(token: str = Depends(require_device_token)) -> dict[str, Any]:
    return {"ideas": [idea.to_client() for idea in ideas.open_ideas()]}


class DismissIdeaRequest(BaseModel):
    reason: str | None = None


@public_app.post("/v1/ideas/{idea_id}/dismiss")
def dismiss_idea(
    idea_id: str, body: DismissIdeaRequest, token: str = Depends(require_device_token)
) -> dict[str, bool]:
    if not ideas.dismiss(idea_id, body.reason):
        raise HTTPException(status_code=404, detail="no open idea with that id")
    return {"ok": True}


def _preferences() -> dict[str, Any]:
    day_start, day_end = preferences_store.day()
    return {
        "face": preferences_store.face(),
        "effort": preferences_store.effort(),
        # Never null, unlike the two above: the harness runs on these whether
        # or not anyone picked them, so the client shows what is in force.
        "day_start": day_start,
        "day_end": day_end,
        # 0 is off. Never null, for the reason the day isn't.
        "checkin_every_hours": preferences_store.checkin_every_hours(),
    }


@public_app.get("/v1/preferences")
def get_preferences(token: str = Depends(require_device_token)) -> dict[str, Any]:
    """Choices every client shares (server/preferences.py). Face and effort
    are null until picked somewhere; a client then keeps its own default."""
    return _preferences()


class PreferencesRequest(BaseModel):
    """Any of them: a client sends what it has pending. Null is "not sent",
    not "clear": nothing is ever unpicked."""

    face: int | None = Field(None, ge=1, le=MAX_FACE_ID, strict=True)
    effort: Literal[EFFORTS] | None = None  # type: ignore[valid-type]
    # "HH:MM", local to the schedule store's zone. Checked as a pair against
    # what is stored (preferences.validate_day).
    day_start: str | None = Field(None, max_length=5)
    day_end: str | None = Field(None, max_length=5)
    checkin_every_hours: int | None = Field(None, ge=0, le=MAX_CHECKIN_EVERY_HOURS, strict=True)


@public_app.post("/v1/preferences")
def set_preferences(body: PreferencesRequest, token: str = Depends(require_device_token)) -> dict[str, Any]:
    if (
        body.face is None and body.effort is None and body.day_start is None
        and body.day_end is None and body.checkin_every_hours is None
    ):
        # A client bug to surface, not a no-op to accept.
        raise HTTPException(status_code=422, detail="nothing to set")
    before = (preferences_store.day(), preferences_store.checkin_every_hours())
    try:
        preferences_store.update(
            face=body.face,
            effort=body.effort,
            day_start=body.day_start,
            day_end=body.day_end,
            checkin_every_hours=body.checkin_every_hours,
        )
    except DayError as e:
        # A code, not a sentence: the clients word it.
        raise HTTPException(status_code=422, detail=e.code) from e
    except OSError as e:
        logger.error("could not persist preferences: %s", e)
        raise HTTPException(status_code=500, detail="failed to store preferences") from e
    if (preferences_store.day(), preferences_store.checkin_every_hours()) != before:
        # The overnight jobs and the check-ins are timed from these, so
        # re-render the crontab now rather than at the next turn's sync --
        # which may be after tonight's tidy-up has fired at the old time.
        # Never raises.
        schedules.sync_after_turn()
    return _preferences()


@public_app.get("/v1/transcribe")
def get_transcribe_status(token: str = Depends(require_device_token)) -> dict[str, Any]:
    """Whether this box can turn speech into text (server/transcribe.py). The
    web client asks once, and shows its mic and voice mode only when it can.
    Never loads the model."""
    return {"available": transcriber.available()}


@public_app.post("/v1/transcribe")
async def post_transcribe(
    request: Request,
    utterance_id: str = Query(..., pattern=transcribe.UTTERANCE_ID_RE.pattern),
    seq: int = Query(..., ge=0, lt=transcribe.MAX_SEGMENTS),
    final: bool = Query(...),
    content_type: str | None = Header(default=None),
    token: str = Depends(require_device_token),
) -> dict[str, Any]:
    """One segment of an utterance, as the raw recorded file (the body is the
    audio, not JSON: no base64 inflation, and no multipart parser). Segments
    are taken in order, `seq` from 0; the one with `final=true` answers with
    the whole utterance's text in `transcript`. Repeating a segment already
    transcribed returns the same answer, so a lost response is safe to retry.
    """
    media = (content_type or "").split(";", 1)[0].strip().lower()
    container = transcribe.ALLOWED_FORMATS.get(media)
    if container is None:
        raise HTTPException(status_code=415, detail="send audio/webm or audio/ogg (Opus)")
    # Read with a running count rather than `await request.body()`, so a body
    # over the cap is refused at the cap instead of after it has all arrived.
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > transcribe.MAX_SEGMENT_BYTES:
            raise HTTPException(status_code=413, detail="segment too large")
    if not data:
        raise HTTPException(status_code=400, detail="empty segment")
    try:
        result = await transcriber.segment(
            token=token, utterance_id=utterance_id, seq=seq, final=final,
            data=bytes(data), container=container,
        )
    except transcribe.TranscribeError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e
    except Exception as e:
        # The model failed to load or crashed on this input. Logged here with
        # the trace; the client hears only that it failed.
        logger.exception("transcription failed")
        raise HTTPException(status_code=500, detail="transcription failed") from e
    return {
        "seq": result.seq, "text": result.text, "final": result.final, "transcript": result.transcript,
        "server_seconds": result.server_seconds,
    }


@public_app.get("/v1/turn/{turn_id}")
def get_turn(turn_id: str, token: str = Depends(require_device_token)) -> dict[str, Any]:
    turn = work_queue.get_turn(turn_id)
    # A compaction turn_id is never handed to any client (it's never returned
    # from /v1/message, never pushed, never in the transcript — see
    # server/queue.py's delivery guards), so this is defense in depth, not a
    # reachable path today: docs/tasks/idle-compaction.md asks for every
    # delivery surface to be audited, this one included. 404, not a
    # source-revealing error, so a guessed id learns nothing.
    if turn is None or turn.source == COMPACTION_SOURCE:
        raise HTTPException(status_code=404, detail="unknown turn_id")
    return _serialize_turn(turn)


@public_app.get("/v1/activity")
def get_activity(
    limit: int = Query(activity_log.DEFAULT_PAGE, ge=1, le=activity_log.MAX_PAGE),
    before: float | None = Query(None, allow_inf_nan=False),
    token: str = Depends(require_device_token),
) -> dict[str, Any]:
    """The persisted activity log, newest first (docs/tasks/ux-lane.md §2).

    A plain `def`, so Starlette runs the file reads in its threadpool rather
    than on the loop the drain task shares. Records go out as written: the
    writer is the privacy boundary (no prompt, reply or error text; targets
    only from invoke.py's allowlist), so there is nothing to strip here that
    was not already kept off disk.

    `next_before` is set only when the page came back full, so the client's
    stop condition is a null rather than an empty extra request.
    """
    entries = activity_log.read_recent(limit, before)
    next_before = entries[-1]["finished_at"] if len(entries) == limit else None
    return {"entries": entries, "next_before": next_before}


@public_app.get("/v1/activity/{turn_id}/output")
def get_activity_output(turn_id: str, token: str = Depends(require_device_token)) -> dict[str, Any]:
    """What a cron turn replied when the reply reached no chat row
    (docs/tasks/default-infrastructure.md "Seeing plumbing output"). Fetched
    when the row is expanded, so `/v1/activity` keeps no reply text.

    The text is the model's own reply, the class of data the transcript
    already serves this same token. `turn_id` is only ever compared, never a
    path. 404 when the turn has none, whether it never existed or was
    delivered to the chat instead. Plain `def` for the threadpool, like above.
    """
    row = activity_log.read_output(turn_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no background output for this turn")
    return {
        "turn_id": row["turn_id"],
        "label": row.get("label"),
        "finished_at": row.get("finished_at"),
        "text": row["text"],
    }


@public_app.get("/v1/usage")
def get_usage(token: str = Depends(require_device_token)) -> dict[str, Any]:
    """Plan windows, the conversation's true size and Marlow's own activity
    (docs/tasks/usage-visibility.md). Facts only -- wording and the 90% line
    belong to the phone. A plain `def`, so the usage.jsonl read runs in the
    threadpool like `/v1/activity`'s. Every block is null until there is
    something to say; a restart empties the in-memory readings."""
    return usage_report.build(usage_state)


@public_app.get("/v1/changes")
def get_changes(token: str = Depends(require_device_token)) -> dict[str, Any]:
    """The box's half of What's new (docs/tasks/whats-new.md): only `server`
    entries, newest first. The client merges its own bundled `app` entries and
    decides what is unseen; nothing here tracks who has read what.

    `onboarding_complete` is False on a fresh install until first-run
    onboarding ends; clients show no card before then and count what they
    would have shown as seen.

    Read from the server's onboarding record (server/onboarding.py,
    docs/tasks/onboarding-v2.md). It replaced a search of USER.md for the
    starter's "Not yet known" placeholder, which the phrase written anywhere
    else in USER.md would have held back for good. Fails open."""
    return {
        "changes": changes.server_entries(),
        "onboarding_complete": onboarding.is_complete(
            settings.data_dir / "onboarding.json", settings.user_path
        ),
    }


@public_app.get("/v1/transcript")
async def get_transcript(
    after: int | None = Query(None, ge=0),
    before: int | None = Query(None, ge=0),
    limit: int = Query(transcript.DEFAULT_LIMIT),
    token: str = Depends(require_device_token),
) -> dict[str, Any]:
    """The server-owned conversation (docs/tasks/server-transcript.md).

    `after=<seq>` is catch-up, ascending. `before=<seq>` is scrollback,
    descending. Neither is the newest page, descending. Both at once is
    ambiguous, so it is a 422 rather than a guess. Cursor reads with no side
    effects, so retrying and two devices reading are both safe.

    `limit` is clamped to `transcript.MAX_LIMIT`, not rejected. Blocking
    SQLite, so it goes to a thread, off the loop the drain shares.
    """
    if after is not None and before is not None:
        raise HTTPException(status_code=422, detail="at most one of after and before")
    if after is not None:
        return await asyncio.to_thread(transcript.read_after, after, limit)
    return await asyncio.to_thread(transcript.read_before, before, limit)


@public_app.get("/v1/transcript/attachment/{attachment_id}")
async def get_transcript_attachment(
    attachment_id: str, token: str = Depends(require_device_token)
) -> FileResponse:
    """One file a user row carries; `attachment_id` is an entry's `id` in the
    row's `attachments`.

    The type served is the one the server recorded when it wrote the row. Two
    headers make that stick for a type that is not an image: `Content-
    Disposition: attachment`, so a browser downloads it instead of rendering
    it, and `nosniff`, so it cannot be re-typed from its bytes. Images are the
    only thing a client should show inline.

    Anything that is not a canonical uuid, and any id with no row, is the same
    404 -- before any path is built from it -- so a guess learns nothing."""
    found = await asyncio.to_thread(transcript.get_attachment, attachment_id)
    if found is None:
        raise HTTPException(status_code=404, detail="unknown attachment")
    path, mime = found
    # FileResponse, so a large file streams rather than being read whole into
    # memory; it raises at send time (a 500) if the file is gone, so a row
    # whose file vanished is turned into the 404 here first.
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(status_code=404, detail="unknown attachment")
    headers = {"X-Content-Type-Options": "nosniff"}
    if not mime.startswith("image/"):
        headers["Content-Disposition"] = "attachment"
    return FileResponse(path, media_type=mime, headers=headers)


@public_app.get("/v1/identity")
def get_identity(token: str = Depends(require_device_token)) -> dict[str, Any]:
    """The files Marlow lives by and its schedules (docs/tasks/ux-lane.md §3a).
    A plain `def` for the same threadpool reason as `/v1/activity`."""
    return transparency.identity_index()


@public_app.get("/v1/identity/file")
def get_identity_file(
    id: str = Query(...),
    token: str = Depends(require_device_token),
) -> dict[str, Any]:
    """One indexed file's text. `id` is looked up in the index, never turned
    into a path; unknown and not-allowed are the same 404 (§3a)."""
    body = transparency.read_file(id)
    if body is None:
        raise HTTPException(status_code=404, detail="unknown file")
    return body


@public_app.get("/v1/permissions")
def get_permissions(token: str = Depends(require_device_token)) -> dict[str, Any]:
    """Side-effect-free (docs/tasks/ios-permission-round-trip-ui.md §3): the
    client polls this every 2s while a turn is in flight. It was the companion
    to `/v1/pending`, which also handed out finished turns once, globally;
    that endpoint went in 2026-10-01 once the transcript replaced it
    (docs/tasks/server-transcript.md step 3)."""
    return {"permission_requests": _pending_permission_payloads()}


class AnswerPermissionRequest(BaseModel):
    decision: str  # "allow" | "deny" | "allow_similar"


@public_app.post("/v1/permission/{request_id}")
async def answer_permission(
    request_id: str, body: AnswerPermissionRequest, token: str = Depends(require_device_token)
) -> dict[str, bool]:
    if body.decision not in ("allow", "deny", "allow_similar"):
        raise HTTPException(status_code=400, detail="decision must be 'allow', 'deny' or 'allow_similar'")
    if body.decision == "allow_similar":
        req = permission_registry.get(request_id)
        if req is None:
            return {"accepted": False}  # too late or unknown -- see below
        key = approval_summary.similar_key(req.tool, req.tool_input)
        if key is None or req.turn_id is None:
            # The pane only offers the button when summary.similar is set, so
            # this is a stale or hand-made client. Refuse loudly rather than
            # quietly downgrading to a plain allow it did not ask for.
            raise HTTPException(status_code=400, detail="this request has no 'similar' scope")
        accepted = permission_registry.answer(request_id, "allow")
        if accepted:
            # After the answer and with no await between: answer() only
            # SCHEDULES the hook's wake-up on this loop, so the grant is in
            # place before the CLI can send its next call. Granting only on an
            # accepted answer keeps a tap that lost the race from widening
            # anything.
            similar_grants.grant(req.turn_id, key)
            # And the siblings already waiting: parallel tool calls each raise
            # their own request, so three greps in one breath are three cards.
            # The grant only meets requests that arrive after it; these arrived
            # before, and leaving them up is the repetition the button removes.
            for sibling in permission_registry.pending():
                if sibling.turn_id == req.turn_id and \
                        approval_summary.similar_key(sibling.tool, sibling.tool_input) == key:
                    permission_registry.answer(sibling.request_id, "allow")
        return {"accepted": accepted}
    # Single-use (design §10.4): False here just means "too late" or "unknown
    # id" — not an error worth surfacing to the device, the round-trip already
    # resolved one way or another.
    accepted = permission_registry.answer(request_id, body.decision)
    return {"accepted": accepted}


class ApnsTokenRequest(BaseModel):
    apns_token: str


@public_app.post("/v1/device/apns-token")
def register_apns_token(body: ApnsTokenRequest, token: str = Depends(require_device_token)) -> dict[str, bool]:
    """The app calls this on every launch, not just at pairing — APNs tokens
    rotate (reinstall, restore, OS update), so idempotently re-registering is
    the only way to catch it."""
    pairing_store.set_apns_token(token, body.apns_token)
    return {"ok": True}


@public_app.get("/v1/webpush/key")
def get_webpush_key(token: str = Depends(require_device_token)) -> dict[str, str]:
    return {"public_key": webpush.public_key_b64url()}


@public_app.post("/v1/device/webpush-subscription")
def register_webpush_subscription(
    body: dict[str, Any], token: str = Depends(require_device_token)
) -> dict[str, bool]:
    """Idempotent, like the APNs token: the browser re-registers on launch.
    Validation lives in webpush.py (the endpoint is a URL the server will
    POST to, so it is pinned to the push services). A bad body is a 400, not
    pydantic's 422, matching the image validation above."""
    subscription = webpush.validate_subscription(body)
    if subscription is None:
        raise HTTPException(status_code=400, detail="invalid web push subscription")
    pairing_store.set_webpush_subscription(token, subscription)
    return {"ok": True}


@public_app.post("/v1/device/unpair")
def unpair_device(token: str = Depends(require_device_token)) -> dict[str, bool]:
    """Revokes the calling token, and its push registrations with it. Both
    clients call this before clearing local state; without it "Unpair" only
    forgot the token on the phone and the box kept honouring it."""
    return {"ok": pairing_store.revoke(token)}


# Behind the token since funnel-ingress.md step 4: the body names the Claude
# auth state, queue depth, model and git commit, which a public socket must
# not hand to strangers. The on-host watchdog uses internal_app's copy.
@public_app.get("/health")
def get_health(token: str = Depends(require_device_token)) -> dict[str, Any]:
    return health.get_health()


# Last in the public section, and installed as a 404 fallback (see
# web_static.py), so nothing above can be shadowed by the client.
web_static.install(public_app, lambda: settings.web_dist_dir, lambda: preferences_store.face())


# --- admin (Unix socket) app ------------------------------------------------

# Its own socket, which a turn can't see (config.py, `admin_socket_path`). The
# internal socket is reachable from inside the bwrap namespace, so nothing that
# mints or revokes a device token may live there. Paths keep their old
# /internal/admin prefix so the deploy scripts only change which socket.
admin_app = FastAPI(title="marlow-admin", docs_url=None, redoc_url=None, openapi_url=None)


class IssueInviteRequest(BaseModel):
    device_name: str


@admin_app.post("/internal/admin/invite")
def issue_invite(body: IssueInviteRequest) -> dict[str, str]:
    return {"invite": pairing_store.issue_invite(body.device_name)}


@admin_app.get("/internal/admin/devices")
def list_devices() -> dict[str, Any]:
    return {"devices": pairing_store.list_devices()}


class RevokeDeviceRequest(BaseModel):
    fingerprint: str


@admin_app.post("/internal/admin/devices/revoke")
def revoke_device(body: RevokeDeviceRequest) -> dict[str, bool]:
    return {"revoked": pairing_store.revoke_by_fingerprint(body.fingerprint)}


# --- internal (Unix socket) app ---------------------------------------------

internal_app = FastAPI(title="marlow-internal")


class PermissionRequestBody(BaseModel):
    tool: str
    tool_input: dict[str, Any] = {}
    session_id: str | None = None


# Moved to server/deny_detail.py so hooks/pretooluse.py's hard-deny logging
# (step 2 of docs/tasks/marlow-harness-affordances.md) can reuse the exact
# same content discipline without importing this module's FastAPI app. Kept
# under their original names here so this stays a no-op for every caller
# (including tests/test_app.py, which references both by these names).
_DENY_LOG_COMMAND_CHARS = deny_detail.DENY_LOG_COMMAND_CHARS
_denied_call_detail = deny_detail.denied_call_detail


@internal_app.post("/internal/permission-request")
async def permission_request(body: PermissionRequestBody) -> dict[str, str]:
    """Called by hooks/pretooluse.py (plan §4 steps 2-6). Long-polls (blocks)
    up to settings.t_hook_seconds, then returns the decision. On timeout,
    posts a "couldn't reach you, skipped it" system message into chat so
    nothing hangs and nothing happens silently."""
    # Unattended turns never reach the phone (2026-08-07). A cron job runs at
    # 08:00 with nobody awake, so the round-trip has exactly one possible
    # ending: 55 seconds of the job's wall-clock burned, a push notification
    # fired at a sleeping Mike, and then the same fail-closed deny. Worse, a
    # half-awake Allow on a Gmail-send prompt raised by an inbox scan is a real
    # failure mode -- that scan just read text written by strangers.
    #
    # This is the whole of the cron permission policy. It is deliberately not
    # `--settings deploy/marlow-settings.cron.json`, which the runbook used to
    # call for: that file differs from the attended one only by `ask` on two
    # tools, so it never addressed the 55s-and-a-push path at all. Denying here
    # covers every tool that isn't vetted locally, which is the never-prompt set
    # the cron file only described.
    #
    # The `reason` field is what lets the hook say something true to the model.
    # A bare "deny" is indistinguishable from a timeout or a real refusal, and
    # the hook's one denial string claims Mike declined a prompt -- which would
    # put "you didn't approve this" into a digest about a prompt that was never
    # raised.
    if active_turn.is_unattended():
        logger.warning(
            "denying %s without a prompt: unattended turn (source=%r)%s",
            body.tool,
            active_turn.current_source(),
            _denied_call_detail(body.tool, body.tool_input),
        )
        # Also into the denial log, so the promotion rule reads one file. The
        # line above stays in marlow-server.log on purpose -- that log is the
        # operational narrative, and "why did the 08:00 job produce nothing" is
        # a question answered by reading it in sequence. The duplication is
        # deliberate and applies only to this class; an attended deny needs no
        # narrative line because Mike was holding the phone at the time.
        deny_detail.log_denial(
            deny_detail.UNATTENDED,
            body.tool,
            body.tool_input,
            note=f"source={active_turn.current_source()!r}",
        )
        return {"decision": "deny", "reason": active_turn.UNATTENDED_DENY_REASON}

    turn_id = work_queue.running_turn_id()
    # "Allow similar for this reply": checked only here, after the unattended
    # deny above, so a grant can never be exercised by cron.
    if similar_grants.covers(turn_id, approval_summary.similar_key(body.tool, body.tool_input)):
        logger.info("allowing %s without a prompt: similar grant for turn %s", body.tool, turn_id)
        work_queue.note_permission_decision(body.tool, "allow", via="similar")
        return {"decision": "allow", "reason": SIMILAR_ALLOW_REASON}

    summary = approval_summary.summarize(body.tool, body.tool_input)
    req = permission_registry.create(
        body.tool, body.tool_input, body.session_id, turn_id=turn_id, summary=summary
    )
    # Fire-and-forget (server/push.py): starting wait_for_decision's timeout
    # clock must not wait on the push finishing first — hooks/pretooluse.py
    # bounds its own wait to settings.t_hook_seconds + 5 on the assumption
    # that's ~how long this whole endpoint takes; a slow APNs round-trip ahead
    # of the wait would eat into that margin and cause a premature fail-closed
    # deny before the server even starts waiting for a decision.
    alert_title, alert_body = approval_summary.push_text(summary)
    push_broadcast_nowait(
        PushPayload(
            type=PushType.PERMISSION_REQUEST,
            ref=req.request_id,
            alert_title=alert_title,
            alert_body=alert_body,
        )
    )

    decision = await permission_registry.wait_for_decision(req.request_id)
    # For the activity log (docs/tasks/ux-lane.md §2a). A timeout is its own
    # outcome, not a "deny" -- Mike never answered, which is the distinction
    # deny_detail draws below for the same reason.
    work_queue.note_permission_decision(req.tool, "timeout" if req.timed_out else decision)
    if req.timed_out:
        work_queue.post_system_message(
            f"I wanted to use {req.tool} but couldn't reach you in time — skipped it."
        )
    # Attended denials were logged NOWHERE before this -- the loudest signal the
    # system can produce (Mike deliberately answering "no" to this exact tool
    # use) left no trace at all. The two are split because they are not the
    # same evidence: `declined` is an answer and a repeat is a standing
    # preference worth putting in the identity block; `timed-out` is a phone in
    # a pocket and a repeat says something about reachability, not about the
    # tool. Folding them together would let the second quietly manufacture
    # preferences out of bad signal. See server/deny_detail.py.
    if decision != "allow":
        deny_detail.log_denial(
            deny_detail.TIMED_OUT if req.timed_out else deny_detail.DECLINED,
            req.tool,
            req.tool_input,
        )
    return {"decision": decision}


class CronMessageBody(BaseModel):
    # Exactly one of these two, and the distinction is a boundary, not
    # bookkeeping. `job` is a Mike-approved cron/<job>.md name and SELECTS
    # BEHAVIOUR below (ATTACHED_SESSION_JOBS / RETIRING_JOBS). `entry` is a
    # schedules.json id, which Marlow chooses for itself -- an entry called
    # "consolidate" would otherwise have retired the live conversation to
    # run model-authored text (review 2026-08-30). Keeping them in separate
    # fields makes that structurally impossible rather than relying on anyone
    # remembering to check a flag. (Both now RUN in the conversation, since
    # 2026-09-25; what an entry can still never do is end it.)
    job: str | None = None
    entry: str | None = None
    text: str
    client_msg_id: str | None = None


class CronNotifyBody(BaseModel):
    entry: str
    text: str
    client_msg_id: str


# Jobs that must run in the conversation or not at all -- the day's review is
# worthless in a session that has no day in it
# (docs/tasks/nightly-consolidation.md piece 2). Every other job and entry
# also runs in the conversation since 2026-09-25, but falls back to a fresh
# one when there is none; these skip instead, and bind the session at enqueue
# rather than at run time so the review and the retirement name the same id.
# (This set once also kept `morning` OUT of the conversation. Consolidation
# retiring the session at 01:00 now does that job: the 08:00 brief starts
# the new day's session unless consolidation failed, in which case running
# on in yesterday's conversation is the lesser evil.)
ATTACHED_SESSION_JOBS: frozenset[str] = frozenset({"consolidate"})

# Jobs whose successful completion ENDS the conversation they attached to, so
# the phone's next message starts a fresh session
# (docs/tasks/nightly-consolidation.md piece 1). A separate set from
# ATTACHED_SESSION_JOBS even though it holds the same one job today, because
# they answer different questions: "does this job need the day's context?" and
# "is this job the last word on the day?". Keying retirement off attachment
# would make the second answer a side effect of the first -- piece 3's planned
# proactive-review job is the concrete case, since the obvious reason to
# attach it is context, and inheriting retirement would silently roll Mike's
# session mid-week and pay a cold identity-block cache write for it (review,
# 2026-08-16). A job may attach without retiring; a job that retires without
# attaching would be retiring a session it never saw, which is why the enqueue
# below takes the intersection rather than this set alone.
RETIRING_JOBS: frozenset[str] = frozenset({"consolidate"})

# Harness upkeep the user doesn't own (schedules.PLUMBING_JOBS,
# docs/tasks/default-infrastructure.md tier 1) is tagged `tier="plumbing"`,
# which files it under "background jobs" and keeps its reply off the chat
# (queue.py). Keyed off `job` only, for the reason CronMessageBody gives: an
# entry the model names "consolidate" is the user's job, not plumbing.
PLUMBING_JOBS = schedules.PLUMBING_JOBS

# Product jobs that run at `high` effort by default instead of
# `settings.cron_effort` (Mike, 2026-10-18/19). Each is synthesis nobody is
# waiting on: `ideas` and `consolidate` write memory or ideas that outlive the
# day; `morning` and `week-ahead` are the long reads Marlow writes. Thinking is
# a rounding error against a day's cache reads, so the only cost is time
# nobody feels. Opus was considered and not taken without a trial
# (docs/tasks/model-and-effort-config.md "Per-job effort").
#
# Only jobs the repo ships belong here. A user's own job, and a product job
# they want run differently, gets its effort from its schedules.json entry,
# which Marlow chooses when it writes the entry (`_cron_effort` below). This
# set once also named Mike's own `weekly-digest` and `alberta-weekly`; they
# moved to their entries (deploy/migrations/0006-entry-effort.py).
HIGH_EFFORT_JOBS: frozenset[str] = frozenset({"ideas", "consolidate", "morning", "week-ahead"})

# Jobs whose entry can NOT change their effort. `ideas` (00:30) and
# `consolidate` (01:00) stay on the same level on purpose: one resumes the
# conversation and the other attaches inside the prompt cache's hour, and an
# effort change between them would re-write part of a cache the second could
# have read. Neither is a store entry any more (the renderer times both from
# "Your day"), and an entry's effort never reaches them.
LOCKED_EFFORT_JOBS: frozenset[str] = frozenset({"ideas", "consolidate"})


def _cron_effort(*, job: str | None = None, entry: str | None = None) -> str | None:
    """The effort for a cron turn, or None for `settings.cron_effort`.

    Read from the validated store at enqueue, so the root-owned entrypoint
    copy never has to carry it. The store is agent-writable, so everything
    here is bounded by the validator: a value is one of EFFORTS or absent,
    and invoke.py re-checks it. The worst an entry can do is make a turn
    slower (or a product brief shallower, at the user's own request).

    An `--entry` dispatch is looked up by entry id ONLY. The id is
    model-chosen, so an entry named "consolidate" must not inherit that job's
    default -- the same rule that keeps it off the attach path. A job
    dispatch is looked up by job name; if several entries run the same job
    (a recurring one and a one-off), the highest effort among them wins,
    because whichever entry fired, it is the same prompt.
    """
    if job in LOCKED_EFFORT_JOBS:
        return "high"
    store = schedules.load(schedules.SCHEDULES_PATH)
    entries = store.entries if store is not None else ()
    if entry is not None:
        found = next((e for e in entries if e.id == entry), None)
        return found.effort if found is not None else None
    chosen = [e.effort for e in entries if e.job == job and e.effort is not None]
    if chosen:
        return max(chosen, key=EFFORTS.index)
    return "high" if job in HIGH_EFFORT_JOBS else None


@internal_app.post("/internal/cron/notify", status_code=202)
async def cron_notify(body: CronNotifyBody) -> dict[str, str]:
    """Create a pre-written turn and put its reminder text on the lock screen.

    Loopback-only, like the model-backed cron endpoint below. The turn must
    exist before the push is sent: its id is the push ref used by pending drain
    and tap-to-refresh, so a bare notification would open to nothing.
    """
    logger.info("cron reminder %r posting a synthetic turn", body.entry)
    existing = work_queue.turn_id_for_client_msg_id(body.client_msg_id)
    if existing is not None:
        return {"turn_id": existing}
    turn_id = work_queue.post_system_message(body.text, client_msg_id=body.client_msg_id)
    push_broadcast_nowait(
        PushPayload(type=PushType.NOTIFY, ref=turn_id, alert_body=body.text)
    )
    return {"turn_id": turn_id}


@internal_app.post("/internal/cron/message", status_code=202)
async def cron_message(body: CronMessageBody) -> dict[str, str]:
    """Called by cron/entrypoint.py. Socket trust: no device token, because
    reaching the internal socket already means you are this instance's user. Never exposed on public_app —
    nothing off-box schedules work (docs/tasks/scheduled-work.md)."""
    if (body.job is None) == (body.entry is None):
        raise HTTPException(
            status_code=422, detail="exactly one of job or entry is required"
        )
    # Resolved first: it is the handler's only await, and placed between the
    # session lookup and the enqueue below it would let the conversation move
    # under a consolidate that had already bound it.
    effort = await asyncio.to_thread(_cron_effort, job=body.job, entry=body.entry)
    session_id = None
    resume = False
    if body.entry is not None:
        # An inline schedules.json prompt. Model-authored text under a
        # model-chosen id: runs in the conversation like any job, but never
        # takes the attach-and-retire path -- see CronMessageBody.
        logger.info("cron entry %r enqueuing a turn", body.entry)
        return {
            "turn_id": work_queue.enqueue(
                body.text,
                source="cron",
                client_msg_id=body.client_msg_id,
                in_conversation=True,
                label=body.entry,
                effort=effort,
            )
        }
    if body.job in ATTACHED_SESSION_JOBS:
        session_id = work_queue.conversation_session_id()
        if session_id is None:
            # Skip rather than run blind. Reaching here now means no phone
            # turn has *ever* completed, or the state file was lost — the
            # lookup reads persisted state since 2026-08-16, so it is no
            # longer the routine "server restarted today" case it used to be
            # (that case is what made this skip fire on 2026-08-15 and let a
            # session run five days; see server/session_state.py). A fresh
            # session asked to "review today" is the one failure mode worse
            # than a no-op — a
            # plausible invented day, written unattended into memory/. This
            # must stay visible (INFO, naming the job and reason) so a week
            # of silent skips is discoverable rather than looking like a
            # working job with nothing to say.
            logger.info(
                "cron job %r skipped: no conversation session to attach to",
                body.job,
            )
            return {"status": "skipped", "reason": "no conversation session"}
        resume = True
    logger.info("cron job %r enqueuing a turn", body.job)
    turn_id = work_queue.enqueue(
        body.text,
        source="cron",
        client_msg_id=body.client_msg_id,
        session_id=session_id,
        resume=resume,
        # If the attended session dies between this check and the turn
        # actually running, invoke.py must not fall back to a fresh session —
        # that fresh-session output is exactly the "invented day, written
        # unattended into memory/" hazard the skip-if-no-session check above
        # exists to prevent. A dead session here becomes a visible ERROR turn.
        require_session=resume,
        # `resume and ...`, not `body.job in RETIRING_JOBS` alone: a job that
        # never attached has no session of the phone's to retire, and the
        # skip-if-no-attended-session branch above returns before this line
        # anyway. Retirement itself is decided in server/queue.py's `_drain`
        # (status DONE, not ERROR) — this only says which jobs are eligible.
        retire_session_on_success=resume and body.job in RETIRING_JOBS,
        # Every other job runs in the conversation, resolved at run time.
        in_conversation=not resume,
        label=body.job,
        tier="plumbing" if body.job in PLUMBING_JOBS else None,
        effort=effort,
    )
    return {"turn_id": turn_id}


@internal_app.get("/health")
def internal_get_health() -> dict[str, Any]:
    # Same handler as the public /health. Duplicated here (on the socket) so
    # the on-host watchdog (deploy/watchdog.sh) doesn't depend on tailscale
    # being up to detect a wedge — if tailscale itself is the problem, the
    # public endpoint would be unreachable even though the process is fine.
    return health.get_health()


# --- entrypoint: run both ASGI apps concurrently ----------------------------

def _resolve_public_tls_kwargs() -> dict[str, str]:
    """Cert+key present and readable -> serve HTTPS. Otherwise
    MARLOW_ALLOW_PLAINTEXT=1 -> serve HTTP with a loud warning. Otherwise refuse
    to start. A missing cert must never silently downgrade to plaintext — this
    server approves tool calls for a shell-capable agent (see the "Decided"
    section of docs/marlow-v2-START.md)."""
    # Known edge case, not guarded: this checks existence/readability, not
    # that the files hold valid parseable cert/key material. A truncated file
    # (e.g. an interrupted `tailscale cert` run) would pass this check and
    # fail later inside uvicorn/OpenSSL with a generic error instead of the
    # clear RuntimeError below.
    cert, key = settings.tls_cert_path, settings.tls_key_path
    if cert.is_file() and key.is_file() and os.access(cert, os.R_OK) and os.access(key, os.R_OK):
        return {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    if settings.allow_plaintext:
        logger.warning(
            "MARLOW_ALLOW_PLAINTEXT=1: serving the public (tailnet) listener over "
            "PLAIN HTTP. This is a local-dev-only escape hatch — never set it on "
            "a deployed box."
        )
        return {}
    raise RuntimeError(
        f"TLS cert/key missing or unreadable (cert={cert}, key={key}) and "
        "MARLOW_ALLOW_PLAINTEXT is not set. Refusing to start the public listener "
        "in plaintext. Run `tailscale cert` (deploy/README.md §8b) to issue them, "
        "or set MARLOW_ALLOW_PLAINTEXT=1 for local dev only."
    )


# Below the unit's default 1024 open files, with room for the server's own;
# well above what a few clients' polls and uploads use.
PUBLIC_LIMIT_CONCURRENCY = 256
PUBLIC_KEEP_ALIVE_SECONDS = 5


def build_public_uvicorn_config() -> uvicorn.Config:
    return uvicorn.Config(
        public_app,
        host=settings.public_bind_host,
        port=settings.public_port,
        log_config=None,
        # Bounds on what an unauthenticated socket can hold open (funnel-
        # ingress.md step 3). Global, not per client: behind raw-TCP Funnel
        # every peer is loopback, so the cap is also a lockout lever. The
        # header deadline is what stops a socket holding a slot by saying
        # nothing (server/request_deadline.py): taking every slot then needs a
        # sustained stream of new TLS connections, a flood, not 64 idle ones.
        # The keep-alive is short so idle sockets don't accumulate. Header
        # size limits stay at httptools' defaults.
        http=RequestDeadlineProtocol,
        limit_concurrency=PUBLIC_LIMIT_CONCURRENCY,
        timeout_keep_alive=PUBLIC_KEEP_ALIVE_SECONDS,
        **_resolve_public_tls_kwargs(),
    )


def prepare_internal_socket_dir() -> None:
    _prepare_socket_dir(settings.internal_socket_path.parent)


def prepare_admin_socket_dir() -> None:
    """As the internal one, and refused if the two directories overlap: the
    admin one is a tmpfs inside the bwrap namespace and the internal one must
    stay reachable there, so sharing (or nesting) either hides the hook's
    socket or exposes the admin one to a turn."""
    admin_dir = os.path.realpath(settings.admin_socket_path.parent)
    run_dir = os.path.realpath(settings.internal_socket_path.parent)
    if admin_dir == run_dir or run_dir.startswith(admin_dir + os.sep) or admin_dir.startswith(run_dir + os.sep):
        raise RuntimeError(
            f"admin socket directory {admin_dir} overlaps the internal socket directory {run_dir}; "
            "MARLOW_ADMIN_SOCKET must live in a directory of its own"
        )
    _prepare_socket_dir(settings.admin_socket_path.parent)


def _prepare_socket_dir(run_dir) -> None:
    """Make a socket's directory owner-only, or raise RuntimeError.

    The directory's mode is the whole access check: uvicorn makes the socket
    itself 0666 (config.py, `internal_socket_path`, says why a socket at all).
    An existing directory with looser bits is tightened rather than refused,
    so a box only ever has to get this right once. A symlink or a directory
    owned by someone else is refused: then someone else chooses who can
    connect.
    """
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(run_dir)
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"socket directory {run_dir} is not a directory")
    if st.st_uid != os.getuid():
        raise RuntimeError(f"socket directory {run_dir} is not owned by this user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(run_dir, 0o700)


def build_internal_uvicorn_config() -> uvicorn.Config:
    # Plaintext by design: a Unix socket never leaves the box, and TLS would
    # only cost the hook a handshake on every tool call.
    return uvicorn.Config(
        internal_app, uds=str(settings.internal_socket_path), log_config=None
    )


def build_admin_uvicorn_config() -> uvicorn.Config:
    return uvicorn.Config(admin_app, uds=str(settings.admin_socket_path), log_config=None)


async def main() -> None:
    # Start the queue (and fail loud on broken identity) before either
    # listener can accept a connection. public_app's own lifespan does this
    # too — both calls are idempotent — but that runs inside
    # asyncio.gather below, racing internal_app's own (lifespan-less)
    # startup with no ordering guarantee between them. internal_app serving
    # /internal/cron/message before work_queue.start() has run would hit
    # WorkQueue.enqueue()'s "must run before enqueue()" RuntimeError as an
    # unhandled 500 instead of a clean failure.
    build_identity_context(settings)
    work_queue.start()

    prepare_internal_socket_dir()
    prepare_admin_socket_dir()
    public_config = build_public_uvicorn_config()
    internal_config = build_internal_uvicorn_config()
    admin_config = build_admin_uvicorn_config()
    if settings.public_bind_host == "127.0.0.1" and settings.funnel:
        # Intended: Tailscale Funnel forwards to loopback (deploy/install.sh
        # --funnel). Said at info so the fallback warning below stays a
        # warning only where loopback is a mistake.
        logger.info("public listener on 127.0.0.1 (MARLOW_FUNNEL=1): reachable through Tailscale Funnel")
    elif settings.public_bind_host == "127.0.0.1":
        logger.warning(
            "public_bind_host fell back to 127.0.0.1 — tailscale IP not detected. "
            "The tailnet-facing API is NOT reachable from the phone until this is fixed "
            "(no-public-ingress is intentional; this fallback is a fail-safe, not a fix)."
        )
    await asyncio.gather(
        uvicorn.Server(public_config).serve(),
        uvicorn.Server(internal_config).serve(),
        uvicorn.Server(admin_config).serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())

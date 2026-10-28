"""Web Push delivery for the browser client (docs/tasks/portable-web-client.md).

The server half of the portable web client: a per-box VAPID keypair, the
validation a browser-supplied subscription must pass before it is stored, and
the send. APNs stays in push.py, which builds the payload and calls `send()`
here; this module never imports push.py (push.py imports it).

Web Push encrypts payloads end to end (RFC 8291), so the push service cannot
read what we send. The content rules are still push.py's: opaque {type, ref},
plus a body only for `notify`.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import tempfile

import requests
import threading
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02
from pywebpush import WebPushException
from pywebpush import webpush as _webpush_send

from server.config import WEBPUSH_PLACEHOLDER_SUBJECT, settings
from server.log import get_logger
from server.pairing import pairing_store

logger = get_logger(__name__)

# One list, used for subscription validation. boundaries.EGRESS_ALLOWLIST
# carries the same three hosts for the sandbox; this is the narrower question
# of what a *client-supplied* URL may point at.
ALLOWED_PUSH_HOSTS: tuple[str, ...] = (
    "fcm.googleapis.com",  # Chrome, Edge, Android
    "updates.push.services.mozilla.com",  # Firefox
    "web.push.apple.com",  # Safari
)

MAX_ENDPOINT_CHARS = 2048
# Browsers hand out a 65-byte uncompressed P-256 point and a 16-byte secret.
_P256DH_BYTES = 65
_AUTH_BYTES = 16
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Same reasoning as push._DISPATCH_TIMEOUT_SECONDS: bound the hang case.
# pywebpush is blocking `requests`, so the timeout is passed down AND the
# await is bounded (the thread itself cannot be cancelled).
_SEND_TIMEOUT_SECONDS = 10

_DEFAULT_TTL_SECONDS = 24 * 60 * 60

_GONE_STATUSES = (404, 410)


# --- VAPID key --------------------------------------------------------------

_key_lock = threading.Lock()
_vapid_cache: tuple[str, Vapid02] | None = None


def _write_private_key(path, vapid: Vapid02) -> None:
    pem = vapid.private_pem()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Temp file + replace so a crash mid-write never leaves a truncated key
    # that the next start would load as "the key" (or fail on forever);
    # mkstemp creates it 0600, so the key is never visible with wider mode.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".vapid-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def get_vapid() -> Vapid02:
    """The box's VAPID keypair, generated on first use. Never logged."""
    global _vapid_cache
    path = settings.vapid_private_key_path
    with _key_lock:
        if _vapid_cache is not None and _vapid_cache[0] == str(path):
            return _vapid_cache[1]
        if path.exists():
            vapid = Vapid02.from_pem(path.read_bytes())
        else:
            vapid = Vapid02()
            vapid.generate_keys()
            _write_private_key(path, vapid)
            logger.info("generated a new VAPID keypair for Web Push")
        _vapid_cache = (str(path), vapid)
        return vapid


def public_key_b64url() -> str:
    """Uncompressed P-256 point, base64url without padding — the form
    `PushManager.subscribe({applicationServerKey})` accepts."""
    raw = get_vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# --- subscription validation -------------------------------------------------

def _b64url_decode(value: object) -> bytes | None:
    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    value = value.rstrip("=")
    if not _B64URL_RE.match(value):
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None


def endpoint_is_allowed(endpoint: object) -> bool:
    """A device token must not be able to turn the server into a request
    forwarder: the endpoint is a URL we will POST to, so it is pinned to the
    known push services — https only, no userinfo, default port, and a host
    that is one of them or a subdomain (exact or `.`-suffix match, so
    `fcm.googleapis.com.evil.com` and `evilfcm.googleapis.com` both fail)."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT_CHARS:
        return False
    if any(ord(c) < 0x21 or ord(c) == 0x7F or c == "\\" for c in endpoint):
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return False
    if port not in (None, 443):
        return False
    host = (parts.hostname or "").lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_PUSH_HOSTS)


def validate_subscription(body: object) -> dict | None:
    """The normalized {endpoint, p256dh, auth} to store, or None if invalid."""
    if not isinstance(body, dict):
        return None
    endpoint = body.get("endpoint")
    keys = body.get("keys")
    if not endpoint_is_allowed(endpoint) or not isinstance(keys, dict):
        return None
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    p256dh_raw, auth_raw = _b64url_decode(p256dh), _b64url_decode(auth)
    if p256dh_raw is None or len(p256dh_raw) != _P256DH_BYTES or p256dh_raw[0] != 0x04:
        return None
    if auth_raw is None or len(auth_raw) != _AUTH_BYTES:
        return None
    return {"endpoint": endpoint, "p256dh": p256dh.rstrip("="), "auth": auth.rstrip("=")}


# --- sending -----------------------------------------------------------------

def is_apple(endpoint: str) -> bool:
    host = (urlsplit(endpoint).hostname or "").lower()
    return host == "web.push.apple.com" or host.endswith(".web.push.apple.com")


def _no_redirect_session() -> requests.Session:
    """The endpoint was validated against ALLOWED_PUSH_HOSTS, but `requests`
    follows redirects by default, so a push service answering 3xx would send
    the POST (VAPID header and all) to wherever it pointed. With no redirects
    allowed, a 3xx raises instead and is logged as an undelivered push."""
    session = requests.Session()
    session.max_redirects = 0
    return session


def _send_blocking(subscription: dict, data: str, ttl: int, urgency: str):
    return _webpush_send(
        subscription_info={
            "endpoint": subscription["endpoint"],
            "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
        },
        data=data,
        vapid_private_key=get_vapid(),
        # Fresh dict per send: py_vapid fills in aud/exp on the dict it is given.
        vapid_claims={"sub": settings.webpush_subject},
        ttl=ttl,
        headers={"Urgency": urgency},
        timeout=_SEND_TIMEOUT_SECONDS,
        requests_session=_no_redirect_session(),
    )


async def send(device_token: str, data: dict, *, permission_request: bool = False, high_urgency: bool = True) -> None:
    """Never raises — same contract as push.dispatch. `data` is the finished
    JSON payload (built in push.py, where the content rules live).

    A stale permission prompt is worse than none, so its TTL is the hook
    window; everything else may wait a day for a sleeping laptop."""
    subscription = pairing_store.webpush_subscription_for(device_token)
    if not subscription:
        return
    ttl = int(settings.t_hook_seconds) if permission_request else _DEFAULT_TTL_SECONDS
    urgency = "high" if high_urgency else "normal"
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_send_blocking, subscription, json.dumps(data), ttl, urgency),
            timeout=_SEND_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "web push send timed out after %ss; not delivered (device=%s...)",
            _SEND_TIMEOUT_SECONDS,
            device_token[:8],
        )
        return
    except WebPushException as exc:
        status = exc.status_code
        if status in _GONE_STATUSES:
            logger.info(
                "push service reports subscription gone (%s) for device=%s...; clearing it",
                status,
                device_token[:8],
            )
            try:
                # Offloaded for the same reason as push.dispatch's APNs clear:
                # _save_tokens() is a blocking file write inside a coroutine
                # that broadcast() gathers with every other device's send.
                await asyncio.to_thread(
                    pairing_store.clear_webpush_subscription, device_token, subscription["endpoint"]
                )
            except Exception:
                logger.exception("failed to clear web push subscription (device=%s...)", device_token[:8])
        elif (
            status == 403
            and settings.webpush_subject == WEBPUSH_PLACEHOLDER_SUBJECT
            and is_apple(subscription["endpoint"])
        ):
            # The 403 that hid Safari's notifications on Mike's box
            # (2026-10-25): say what fixes it, not just that it failed.
            logger.warning(
                "web push not delivered: status=403 (device=%s...); this box has no push contact "
                "address, which Apple's service requires. Set MARLOW_WEBPUSH_SUBJECT=mailto:<your "
                "address>, or rerun deploy/install.sh with --email",
                device_token[:8],
            )
        else:
            # Not the exception text: it embeds the response body and may name
            # the endpoint.
            logger.warning("web push not delivered: status=%s (device=%s...)", status, device_token[:8])
        return
    except Exception as exc:
        logger.warning(
            "web push send failed: %s (device=%s...)", type(exc).__name__, device_token[:8]
        )

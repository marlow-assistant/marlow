"""Device pairing + token store (plan §3.1/§10.1).

First-device bootstrap (plan §8, resolved during gate #3): an admin
out-of-band invite. Mike issues one by calling the internal endpoint
`POST /internal/admin/invite {device_name}` (app.py) over SSH, on the
instance's owner-only socket — deploy/pair-device.sh does it — which prints
a QR of `https://<box>:8443/#pair=<invite>` and the same link as text. The app
sends the invite to `POST /v1/pair`; whoever can reach the internal socket
(i.e. Mike, SSH'd into the box as the instance's user) is the source of trust —
no existing paired device is required, which is what makes it work for the
*first* device. This has to be an endpoint on the running server, not a
standalone script: the invite lives in this module's in-memory PairingStore
singleton, so a separate process issuing one would never share it with the
process the app actually talks to.

The invite is 256 random bits, single use, ten minutes, held only as a hash.
That is what lets `/v1/pair` sit on the internet (docs/tasks/funnel-ingress.md):
nothing to guess, so no attempt budget (a global one would let a stranger lock
pairing), and every failure is the same error so there is no oracle.

Persistence is flat JSON under settings.data_dir, which boundaries.py already
lists as a secret path (device tokens are bearer credentials).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time

from server.config import settings
from server.log import get_logger

logger = get_logger(__name__)

_INVITE_TTL_SECONDS = 10 * 60


def fingerprint(device_token: str) -> str:
    """Short, loggable name for a token. Never the token: it is a bearer
    credential, and eight hex digits of its hash are enough to tell devices
    apart in a list."""
    return hashlib.sha256(device_token.encode()).hexdigest()[:8]


def _clean_name(name: str | None) -> str:
    """The device name arrives from an unauthenticated body and is printed by
    deploy/devices.sh into an admin's terminal: no control characters (escape
    sequences, newlines that forge a row), and the same 64 the clients allow."""
    cleaned = "".join(c for c in (name or "") if c.isprintable())
    return cleaned.strip()[:64]


def _invite_key(invite: str) -> str:
    return hashlib.sha256(invite.encode()).hexdigest()


class PairingError(Exception):
    pass


class PairingStore:
    """In-memory invites (short-lived, fine to lose on restart) + a
    persisted device-token table (must survive restarts)."""

    def __init__(self) -> None:
        # hash of the invite -> {"device_name", "created_at"}
        self._invites: dict[str, dict] = {}
        # Reentrant: redeem/revoke hold it across _save_tokens, which takes it too.
        # Every writer of the token table holds it, so a save never serializes a
        # dict another thread is changing.
        self._lock = threading.RLock()
        self._tokens: dict[str, dict] = self._load_tokens()

    def _load_tokens(self) -> dict[str, dict]:
        path = settings.device_tokens_path
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            logger.error("device_tokens.json is corrupt — refusing to load, starting empty")
            return {}

    def _save_tokens(self) -> None:
        with self._lock:
            settings.ensure_dirs()
            path = settings.device_tokens_path
            path.write_text(json.dumps(self._tokens, indent=2))
            path.chmod(0o600)

    # --- admin side: run over SSH, not exposed over HTTP -------------------

    def issue_invite(self, device_name: str) -> str:
        invite = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            # Sweep as we go: nothing else would ever drop an unredeemed one.
            self._invites = {
                k: v for k, v in self._invites.items() if now - v["created_at"] <= _INVITE_TTL_SECONDS
            }
            self._invites[_invite_key(invite)] = {"device_name": device_name, "created_at": now}
            outstanding = len(self._invites)
        # Counts only, never the invite.
        logger.info("pairing invite issued for device_name=%r (%d outstanding)", device_name, outstanding)
        return invite

    # --- app side: POST /v1/pair --------------------------------------------

    def redeem(self, invite: str, device_name: str) -> str:
        """Invite -> device token. One error for every failure (unknown,
        expired, already used), so a caller learns nothing about which."""
        # Popped before the token is written, under the lock: two requests
        # racing one invite cannot both succeed, and a failed write cannot
        # leave a live invite behind to retry into a second token.
        with self._lock:
            entry = self._invites.pop(_invite_key(invite), None)
            if entry is None or time.time() - entry["created_at"] > _INVITE_TTL_SECONDS:
                logger.warning("pairing failed (%d invites outstanding)", len(self._invites))
                raise PairingError("pairing failed")
            device_token = secrets.token_urlsafe(32)
            name = _clean_name(device_name) or entry["device_name"]
            self._tokens[device_token] = {"device_name": name, "paired_at": time.time()}
            try:
                self._save_tokens()
            except OSError:
                # Not left half-paired: a token that works until the next
                # restart and then vanishes is worse than a clean failure. The
                # invite is spent either way; the caller needs a new one.
                del self._tokens[device_token]
                raise
        logger.info("device paired: %r (token %s)", name, fingerprint(device_token))
        return device_token

    # --- revocation ---------------------------------------------------------

    def revoke(self, device_token: str) -> bool:
        """Remove a token and with it its APNs / web-push registration (they
        live on the entry). True if it existed."""
        with self._lock:
            entry = self._tokens.pop(device_token, None)
            if entry is None:
                return False
            self._save_tokens()
        logger.info("device revoked: %r (token %s)", entry.get("device_name"), fingerprint(device_token))
        return True

    def list_devices(self) -> list[dict]:
        return [
            {
                "device_name": e.get("device_name"),
                "paired_at": e.get("paired_at"),
                "fingerprint": fingerprint(t),
            }
            for t, e in self._tokens.items()
        ]

    def revoke_by_fingerprint(self, fp: str) -> bool:
        """Revokes the one device whose fingerprint matches. Zero or several
        matches revoke nothing: eight hex digits can collide, and revoking
        the wrong phone is worse than asking for a longer prefix."""
        matches = [t for t in list(self._tokens) if fingerprint(t) == fp]
        if len(matches) != 1:
            return False
        return self.revoke(matches[0])

    def _entry(self, device_token: str) -> dict | None:
        return self._tokens.get(device_token)

    def is_valid_token(self, device_token: str) -> bool:
        return device_token in self._tokens

    def device_name_for(self, device_token: str) -> str | None:
        entry = self._entry(device_token)
        return entry["device_name"] if entry else None

    def set_apns_token(self, device_token: str, apns_token: str) -> None:
        """Updatable after pairing, not pair-time-only — APNs tokens rotate
        on reinstall, restore, and OS update. A no-op for an unknown
        device_token rather than raising: the route calling this already
        validated the token via require_device_token, so this can only
        diverge on a bug elsewhere, and a silent no-op is safer than a 500
        for what is otherwise a harmless registration call."""
        with self._lock:
            entry = self._entry(device_token)
            if entry is None:
                return
            entry["apns_token"] = apns_token
            self._save_tokens()

    def apns_token_for(self, device_token: str) -> str | None:
        entry = self._entry(device_token)
        return entry.get("apns_token") if entry else None

    def clear_apns_token(self, device_token: str) -> None:
        """Called on APNs' 410 Unregistered — otherwise a reinstalled app
        leaves a permanently-failing token on file."""
        with self._lock:
            entry = self._entry(device_token)
            if entry is not None and "apns_token" in entry:
                del entry["apns_token"]
                self._save_tokens()

    def set_webpush_subscription(self, device_token: str, subscription: dict) -> None:
        """One browser subscription per device token; re-registering replaces
        it (a browser re-subscribes after the user clears site data, and the
        old endpoint is then dead). Stores only endpoint + keys, which the
        caller has already validated. Same no-op-on-unknown-token contract as
        `set_apns_token`."""
        with self._lock:
            entry = self._entry(device_token)
            if entry is None:
                return
            if entry.get("webpush") == subscription:
                return  # the client re-registers on every launch; skip the write
            entry["webpush"] = subscription
            self._save_tokens()

    def webpush_subscription_for(self, device_token: str) -> dict | None:
        entry = self._entry(device_token)
        return entry.get("webpush") if entry else None

    def clear_webpush_subscription(self, device_token: str, endpoint: str | None = None) -> None:
        """Called when the push service answers 404/410 — the subscription is
        permanently gone, and retrying it on every push is just noise.

        `endpoint` is the one the failed send used. A send takes up to 10s in
        a thread, and the browser may have re-registered a NEW endpoint in that
        window (a rotated subscription); clearing by device alone would delete
        the fresh one and leave the device without push until its next launch
        (review, 2026-10-01). With an endpoint, only that one is cleared."""
        with self._lock:
            entry = self._entry(device_token)
            if entry is None or "webpush" not in entry:
                return
            if endpoint is not None and entry["webpush"].get("endpoint") != endpoint:
                return
            del entry["webpush"]
            self._save_tokens()

    def set_timezone(self, device_token: str, timezone: str) -> None:
        """Record where Mike's phone says it is (docs/tasks/scheduled-work.md).

        Stamped with a report time because `latest_timezone()` resolves a
        disagreement between devices by recency, and because a paired-but-
        dormant device (the simulator, an old phone) must not be able to
        out-vote the handset actually in Mike's pocket just by having been
        paired later.

        Same no-op-on-unknown-token contract as `set_apns_token`.
        """
        with self._lock:
            entry = self._entry(device_token)
            if entry is None:
                return
            if entry.get("timezone") == timezone:
                return  # unchanged; skip the disk write on every single turn
            entry["timezone"] = timezone
            entry["timezone_reported_at"] = time.time()
            self._save_tokens()
            logger.info("device timezone now %s", timezone)

    def timezone_for(self, device_token: str) -> str | None:
        entry = self._entry(device_token)
        return entry.get("timezone") if entry else None

    def latest_timezone(self) -> str | None:
        """The most recently reported timezone across all paired devices.

        Cron turns have no device in context at all — nobody sent them — so
        "most recent report wins" is the only signal available there, and it is
        the right one: if Mike's phone said Lisbon last night, his 08:00
        briefing is a Lisbon 08:00.
        """
        best_at = -1.0
        best: str | None = None
        for entry in self._tokens.values():
            zone = entry.get("timezone")
            if not zone:
                continue
            reported_at = entry.get("timezone_reported_at", 0.0)
            if reported_at > best_at:
                best_at, best = reported_at, zone
        return best

    def all_device_tokens(self) -> list[str]:
        """Every currently-paired device token — who to push a permission-
        request wake to (plan §4 step 3: "the paired device(s)", plural)."""
        return list(self._tokens.keys())


pairing_store = PairingStore()

"""Uploaded image attachments (docs/tasks/ios-image-input.md).

Storage layout: `<settings.attachments_dir>/<turn_id>/<n>.jpg`, n counting up
from 0 in the order the client attached them (up to server/app.py's
_MAX_IMAGES). server/app.py writes the file
BEFORE enqueueing the turn, so it exists by the time the worker can reach it;
server/queue.py's drain loop removes it once the turn reaches a terminal
state, unconditional on status — an ERRORed turn leaves the same bytes behind
as a DONE one.

Deliberately NOT under `settings.data_dir`: that directory is in
`boundaries.SECRET_PATHS` (hard-denied for Read, and hidden behind a bwrap
tmpfs by invoke.py), so a file placed there would be structurally unreadable
by the CLI — a "file not found" that survives every permission fix. See
`boundaries.ATTACHMENT_ROOT` for the read-allow this directory gets instead.

MIME/size validation is an HTTP-level policy decision and lives in
server/app.py where the wire request is decoded; this module only knows the
path, the write, and the cleanup.
"""
from __future__ import annotations

import os
import shutil
import time

from server.config import settings
from server.log import get_logger

logger = get_logger(__name__)

# A restart drops in-flight turns (server/queue.py's own KNOWN GAP comments on
# the reauth-pause path document the same thing for a different reason), so a
# turn's attachment directory can be orphaned with no in-memory Turn left to
# ever call cleanup_attachment() for it. Swept once at queue startup rather
# than relied on to never happen.
_ORPHAN_MAX_AGE_SECONDS = 24 * 3600


def turn_attachment_dir(turn_id: str) -> str:
    return os.path.join(str(settings.attachments_dir), turn_id)


def write_attachments(turn_id: str, images: list[bytes]) -> tuple[str, ...]:
    """Writes the decoded images as 0.jpg, 1.jpg, ... and returns their paths,
    in order.

    Caller's job to validate size/MIME first (server/app.py) — this only knows
    how to place bytes on disk, 0700 like every other private state directory
    on this box. One directory per turn, so cleanup and the orphan sweep stay
    per-turn however many images it holds.
    """
    turn_dir = turn_attachment_dir(turn_id)
    os.makedirs(turn_dir, mode=0o700, exist_ok=True)
    paths: list[str] = []
    for i, data in enumerate(images):
        path = os.path.join(turn_dir, f"{i}.jpg")
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)
    return tuple(paths)


def cleanup_attachment(turn_id: str) -> None:
    """Removes the turn's attachment directory, if any.

    Never raises — mirrors identity_git.commit_after_turn's contract, and
    server/queue.py wraps the call anyway rather than resting the drain loop
    on that promise alone (the lesson from that module: an exception escaping
    here would kill the only consumer task in the process).
    """
    turn_dir = turn_attachment_dir(turn_id)
    try:
        shutil.rmtree(turn_dir)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("failed to clean up attachment dir %s", turn_dir)


def sweep_orphaned_attachments(now: float | None = None) -> int:
    """Removes attachment dirs older than 24h. Returns the count removed.

    What an in-flight turn's directory becomes if the server restarts before
    the drain loop's own cleanup runs — the in-memory Turn (and with it, the
    only thing that would have called cleanup_attachment) is gone the moment
    the process is, so nothing left in `_turns` ever cleans up a directory
    written just before the restart.
    """
    root = str(settings.attachments_dir)
    if not os.path.isdir(root):
        return 0
    now = now if now is not None else time.time()
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age <= _ORPHAN_MAX_AGE_SECONDS:
            continue
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError:
            logger.exception("failed to sweep orphaned attachment dir %s", path)
    if removed:
        logger.info("swept %d orphaned attachment dir(s) older than 24h", removed)
    return removed

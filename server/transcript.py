"""The server-owned transcript: an append-only SQLite store, one row per
bubble (docs/tasks/server-transcript.md).

The conversation used to be durable only on the phone, and `/v1/pending` (removed
2026-10-01) handed each unsolicited turn out once, globally, so a second client
could never see what the first had consumed. Here the server is the only writer and every
client keeps its own cursor (the highest `seq` it has seen), so there is no
per-device delivery state to race on.

**Rows are immutable once written.** There is no update path in this module on
purpose: no edits, no deletes from clients, and the only removal is `prune`.

**Attachments are their own table** (`transcript_attachments`), not a column on
the row, because a message will carry several files and not only pictures
(Mike, 2026-10-01). `POST /v1/message` is still single-image today, which is
simply one attachment at idx 0. The file lives at
`data_dir/transcript-attachments/<attachment id>` with NO extension: the stored
`mime` is the only thing that says what it is, so a name can never talk a
client or a browser into a different type than the one the server recorded.

**Where it lives is part of the design**, same as `activity_log`:
`settings.data_dir` is in `boundaries.SECRET_PATHS`, hard-denied to the agent
and hidden by the bwrap tmpfs. The transcript is a client surface; the agent
has its own sessions. Attachments are copied under `data_dir` for the same
reason -- outside `ATTACHMENT_ROOT`, so the agent's read-allow never covers
history.

**Writes never raise.** The reply is already the user's by the time `_drain`
writes its row, so a full disk is a missing row and a logged error, not a
wedged queue -- `activity_log.append_record`'s contract. Reads do raise: they
run in an endpoint, where a 500 is the honest answer.

**A connection per call**, not one shared handle. Callers are the event loop
(`post_system_message`), the default executor (`to_thread` from the endpoints
and `_drain`) and the retention task, and a `sqlite3` connection may not cross
threads by default. Opening one costs ~100us against a local file, which at
one phone's message rate is nothing, and it removes any lock to get wrong.
WAL lets the readers run while a write is in flight; `timeout` is the
busy-wait for the rare writer-vs-writer overlap (a prune against a message).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence

from server.config import settings
from server.log import get_logger

logger = get_logger(__name__)

# 30 days, Mike's call (2026-10-01). Clients apply the same cutoff to their
# cache, so this is a number two codebases agree on.
RETENTION_SECONDS = 30 * 24 * 3600

# Page bounds. A reply is a few KB, so 500 rows is a few MB at worst: about
# what one catch-up could ever want, not a performance knob. `clamp_limit`
# clamps rather than rejects so an old or hand-typed request still gets a page.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# How long a writer waits on another writer's lock before giving up (and, for a
# write, logging and swallowing).
_BUSY_TIMEOUT_SECONDS = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT    NOT NULL UNIQUE,
    role          TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    source        TEXT    NOT NULL,
    turn_id       TEXT    NOT NULL,
    client_msg_id TEXT,
    text          TEXT    NOT NULL,
    status        TEXT    CHECK (status IS NULL OR status IN ('done', 'error')),
    -- Assistant rows: JSON array of the user-row turn_ids this reply resolves
    -- (see `append_assistant`). '[]' everywhere else.
    answers       TEXT    NOT NULL DEFAULT '[]',
    created_at    REAL    NOT NULL,
    -- User rows from a "Do it" tap: JSON {idea_id, emoji, title}, the idea the
    -- bubble quotes (docs/tasks/ideas-panel.md). NULL everywhere else.
    quote         TEXT
);
-- The idempotent-retry key. Partial, so assistant rows (no client_msg_id) never
-- collide with one another.
--
-- PERMANENT (for the retention window), unlike the queue's client_msg_id dedup,
-- which is a 600s window (queue.CLIENT_MSG_ID_WINDOW_SECONDS) that exists so a
-- legitimately new message reusing an id is not swallowed. The two answer
-- different questions. Client ids are uuids, so a repeat is a resend, never a
-- new message; and the transcript records that a message was SENT once, not
-- that it RAN once. A retry after the queue forgot, or after an ERROR, makes a
-- new turn but must not make a second bubble -- its reply names the original
-- row through `answers` instead.
CREATE UNIQUE INDEX IF NOT EXISTS transcript_client_msg_id
    ON transcript (client_msg_id) WHERE client_msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS transcript_created_at ON transcript (created_at);

CREATE TABLE IF NOT EXISTS transcript_attachments (
    id         TEXT    PRIMARY KEY,
    row_id     TEXT    NOT NULL REFERENCES transcript (id),
    idx        INTEGER NOT NULL,
    mime       TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_attachments_row ON transcript_attachments (row_id, idx);
"""

_COLUMNS = "seq, id, role, source, turn_id, client_msg_id, text, status, answers, created_at, quote"

# Rows deleted per transaction by `prune`.
PRUNE_BATCH = 200

# What a user row with no reply gets when the server restarted under it.
INTERRUPTED_TEXT = "This message was interrupted by a server restart. Send it again?"


def db_path() -> Path:
    return settings.data_dir / "transcript.db"


def attachments_dir() -> Path:
    return settings.data_dir / "transcript-attachments"


def attachment_path(attachment_id: str) -> Path | None:
    """The file for an attachment id, or None if it is not a canonical uuid.

    The id comes off the URL. It is parsed and re-rendered, never spliced into
    a path as received, so nothing but 32 hex digits and four hyphens can reach
    the filesystem -- no separators, no `..`, no NUL.
    """
    try:
        canonical = str(uuid.UUID(attachment_id))
    except ValueError:
        return None
    if canonical != attachment_id:
        return None
    return attachments_dir() / canonical


# Db paths this process has already set up (mkdir, WAL, schema). Once per path,
# not per call: `executescript` takes the write lock and commits first, so doing
# it on every connection put a lock acquisition in front of every READ too. Keyed
# by path rather than a bare flag so a test (or anything else) pointing at a
# fresh path still initialises it. A path is only recorded once its setup
# succeeded, so a failure is retried on the next call, not remembered.
_initialised: set[str] = set()
_init_lock = threading.Lock()


def _initialise(path: Path) -> None:
    key = str(path)
    if key in _initialised:
        return
    with _init_lock:
        if key in _initialised:
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECONDS)) as conn:
            # WAL is a property of the file once set; the schema is IF NOT EXISTS.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # Added 2026-10-12 to a table that already exists on deployed
            # boxes, where CREATE IF NOT EXISTS changes nothing.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(transcript)")}
            if "quote" not in columns:
                conn.execute("ALTER TABLE transcript ADD COLUMN quote TEXT")
        _initialised.add(key)


def _connect() -> sqlite3.Connection:
    path = db_path()
    _initialise(path)
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn


def _attach(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    """Row dicts with `attachments: [{id, mime}]` in idx order, [] for none.
    One query for the page, not one per row."""
    by_row: dict[str, list[dict[str, str]]] = {}
    ids = [r["id"] for r in rows]
    if ids:
        marks = ",".join("?" * len(ids))
        for att in conn.execute(
            f"SELECT id, row_id, mime FROM transcript_attachments"
            f" WHERE row_id IN ({marks}) ORDER BY row_id, idx",
            ids,
        ):
            by_row.setdefault(att["row_id"], []).append({"id": att["id"], "mime": att["mime"]})
    out = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        item["answers"] = json.loads(item["answers"])
        item["quote"] = json.loads(item["quote"]) if item["quote"] else None
        item["attachments"] = by_row.get(row["id"], [])
        out.append(item)
    return out


def _fetch_one(conn: sqlite3.Connection, where: str, param: Any) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM transcript WHERE {where} = ?", (param,)).fetchone()
    return _attach(conn, [row])[0] if row is not None else None


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


# --- writes -------------------------------------------------------------------


def append_user(
    *,
    turn_id: str,
    text: str,
    source: str,
    client_msg_id: str | None,
    attachments: Sequence[tuple[str, str]] = (),
    quote: dict[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Write the user's bubble and return its row. Never raises (None on
    failure).

    Idempotent on `client_msg_id`: a retried send finds the existing row and
    returns THAT, writing nothing, the same way the queue's idempotency index
    hands back the cached turn_id. Coalesced (later MERGED) messages are
    separate sends and each get their own row.

    `attachments` is `(source file path, mime)` pairs, in display order. Each
    is copied to `transcript-attachments/<new id>` BEFORE the row is inserted,
    so a row never lists a file that failed to land; the cost is stray files if
    the insert then loses an idempotency race, which are unlinked below. A file
    that cannot be copied is skipped and logged and the message still goes in:
    a bubble without its picture beats a lost message. Blocking (file copies),
    so callers run this in a thread.
    """
    now = time.time() if now is None else now
    row_id = str(uuid.uuid4())
    copied: list[tuple[str, str]] = []  # (attachment id, mime), landed on disk

    def _discard_copies() -> None:
        for att_id, _ in copied:
            path = attachment_path(att_id)
            if path is not None:
                path.unlink(missing_ok=True)
        copied.clear()

    try:
        with closing(_connect()) as conn:
            if client_msg_id is not None:
                existing = _fetch_one(conn, "client_msg_id", client_msg_id)
                if existing is not None:
                    return existing
            for src, mime in attachments:
                att_id = str(uuid.uuid4())
                try:
                    dest = attachment_path(att_id)
                    assert dest is not None  # a fresh uuid4 is always canonical
                    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(src, dest)
                    copied.append((att_id, mime))
                except OSError:
                    logger.exception("transcript: could not copy attachment for turn %s", turn_id)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO transcript"
                        " (id, role, source, turn_id, client_msg_id, text, status, created_at, quote)"
                        " VALUES (?, 'user', ?, ?, ?, ?, NULL, ?, ?)",
                        (row_id, source, turn_id, client_msg_id, text, now,
                         json.dumps(quote, ensure_ascii=False) if quote else None),
                    )
                    conn.executemany(
                        "INSERT INTO transcript_attachments (id, row_id, idx, mime, created_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        [(att_id, row_id, idx, mime, now) for idx, (att_id, mime) in enumerate(copied)],
                    )
            except sqlite3.IntegrityError:
                # Lost a race with the same client_msg_id between the SELECT
                # and the INSERT (two concurrent retries). The unique index is
                # the real guard; this just returns the winner's row.
                _discard_copies()
                return _fetch_one(conn, "client_msg_id", client_msg_id) if client_msg_id else None
            return _fetch_one(conn, "id", row_id)
    except Exception:
        logger.exception("transcript: failed to append user row for turn %s", turn_id)
        _discard_copies()
        return None


def append_assistant(
    *,
    turn_id: str,
    text: str,
    source: str,
    status: str,
    answers: Sequence[str] = (),
    now: float | None = None,
) -> dict[str, Any] | None:
    """Write a reply (or an error, or a system notice) and return its row.
    Never raises (None on failure).

    `status` is `done` or `error`. `text` is the reply, the error text, or ""
    for an error the server has no text for (the client shows its generic one
    -- see `Turn.error` in server/queue.py).

    `answers` is the turn_ids of the USER rows this reply resolves, which is
    not the same thing as this row's own `turn_id`: a coalesced reply answers
    every message merged into it, and a retry's reply answers the original row
    rather than its own fresh turn. [] for a reply to nothing a user said
    (cron, system). The client contract is that a user row is in flight exactly
    until some assistant row lists its turn_id here.
    """
    now = time.time() if now is None else now
    try:
        with closing(_connect()) as conn:
            row_id = str(uuid.uuid4())
            with conn:
                conn.execute(
                    "INSERT INTO transcript"
                    " (id, role, source, turn_id, client_msg_id, text, status, answers, created_at)"
                    " VALUES (?, 'assistant', ?, ?, NULL, ?, ?, ?, ?)",
                    (row_id, source, turn_id, text, status, json.dumps(list(answers)), now),
                )
            return _fetch_one(conn, "id", row_id)
    except Exception:
        logger.exception("transcript: failed to append assistant row for turn %s", turn_id)
        return None


def mark_interrupted(started_before: float, now: float | None = None) -> int:
    """Answer every user row the server lost in a restart. Returns how many.
    Never raises.

    `_turns` is in memory, so a user row with no reply at startup names a turn
    that no longer exists and will never be answered; left alone, a client
    would show it as in flight forever. Each gets one `status=error`,
    `source=system` row saying so, answering it.

    `started_before` is the moment this process started, and only rows older
    than it count: the server is already taking messages while this runs off
    the loop, and a message sent a moment ago is live, not lost. Only rows
    inside the retention window are considered -- older ones are about to be
    pruned. Idempotent: an answered row is skipped, so a second start finds
    nothing new.
    """
    now = time.time() if now is None else now
    try:
        with closing(_connect()) as conn:
            orphans = conn.execute(
                "SELECT turn_id FROM transcript u"
                " WHERE u.role = 'user' AND u.created_at < ? AND u.created_at >= ?"
                " AND NOT EXISTS ("
                "   SELECT 1 FROM transcript a, json_each(a.answers) j"
                "   WHERE a.role = 'assistant' AND j.value = u.turn_id)"
                " ORDER BY u.seq",
                (started_before, now - RETENTION_SECONDS),
            ).fetchall()
            with conn:
                conn.executemany(
                    "INSERT INTO transcript"
                    " (id, role, source, turn_id, client_msg_id, text, status, answers, created_at)"
                    " VALUES (?, 'assistant', 'system', ?, NULL, ?, 'error', ?, ?)",
                    [
                        (str(uuid.uuid4()), r["turn_id"], INTERRUPTED_TEXT, json.dumps([r["turn_id"]]), now)
                        for r in orphans
                    ],
                )
        if orphans:
            logger.warning("transcript: %d message(s) interrupted by a restart marked as errors", len(orphans))
        return len(orphans)
    except Exception:
        logger.exception("transcript: could not mark interrupted turns")
        return 0


# --- reads --------------------------------------------------------------------


def _latest_seq(conn: sqlite3.Connection) -> int:
    # sqlite_sequence, not MAX(seq): after a prune empties the table MAX is
    # NULL, and a client holding cursor 57 would see `latest_seq` 0 and wonder
    # whether the server had been reset. AUTOINCREMENT never reuses a number.
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'transcript'").fetchone()
    return int(row[0]) if row else 0


def awaiting_first_contact(greeting: str) -> bool:
    """True while the user has never typed a message and `greeting` hasn't been
    posted.

    Not "the table is empty": the starter's morning briefing runs whether or not
    anyone has paired, so a box installed in the evening and paired the next
    day already holds a cron reply (/code-review medium, 2026-10-02). Only the
    user writes `role = 'user'` rows. Retention can delete those from a box that
    went quiet for RETENTION_SECONDS, which is why the caller also requires this
    to be the only paired device. Fails closed: if the read raises, say "no",
    because a missed greeting costs less than greeting a known user as a
    stranger."""
    try:
        with closing(_connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM transcript WHERE role = 'user' OR text = ? LIMIT 1",
                (greeting,),
            ).fetchone()
            return row is None
    except Exception:
        logger.exception("could not read the transcript for first contact")
        return False


def _page(conn: sqlite3.Connection, rows: list[sqlite3.Row], limit: int) -> dict[str, Any]:
    return {
        "rows": _attach(conn, rows[:limit]),
        "latest_seq": _latest_seq(conn),
        "has_more": len(rows) > limit,
    }


def read_after(after: int, limit: int | None = None) -> dict[str, Any]:
    """Rows with `seq > after`, ascending: catch-up. `has_more` means the page
    stopped at `limit` and there is more past its last row."""
    limit = clamp_limit(limit)
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM transcript WHERE seq > ? ORDER BY seq ASC LIMIT ?",
            (after, limit + 1),
        ).fetchall()
        return _page(conn, rows, limit)


def read_before(before: int | None, limit: int | None = None) -> dict[str, Any]:
    """Rows with `seq < before`, descending: scrollback. `before=None` is the
    newest page. `has_more` means older rows exist past the page's last."""
    limit = clamp_limit(limit)
    with closing(_connect()) as conn:
        if before is None:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM transcript ORDER BY seq DESC LIMIT ?", (limit + 1,)
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM transcript WHERE seq < ? ORDER BY seq DESC LIMIT ?",
                (before, limit + 1),
            ).fetchall()
        return _page(conn, rows, limit)


def get_attachment(attachment_id: str) -> tuple[Path, str] | None:
    """`(file, stored mime)` for an attachment id, or None if the id is not a
    canonical uuid or names no attachment. The mime is the server's own record
    from when the row was written, never anything the file's name or bytes
    claim."""
    path = attachment_path(attachment_id)
    if path is None:
        return None
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT mime FROM transcript_attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
    return (path, row["mime"]) if row is not None else None


# --- retention ----------------------------------------------------------------


def prune(now: float | None = None) -> int:
    """Delete rows older than RETENTION_SECONDS, with their attachment rows and
    files. Returns the number of rows removed. Never raises.

    In batches of PRUNE_BATCH rows, one short transaction each. A single
    DELETE of a long-neglected table would hold the write lock for as long as it
    took, and `WorkQueue.post_system_message` writes synchronously ON the event
    loop: a lock held for seconds would freeze the whole server for seconds. A
    batch holds it for milliseconds, and the lock is free between batches. That
    bound is what makes the synchronous write safe.

    Per batch, attachment rows go before their message rows (one transaction),
    and files are unlinked only after it commits: a crash between leaves an
    orphan file nothing can reach (the endpoint looks the id up in the table,
    and the id is gone), not a row whose attachment 404s.
    """
    now = time.time() if now is None else now
    cutoff = now - RETENTION_SECONDS
    removed = 0
    try:
        with closing(_connect()) as conn:
            while True:
                ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM transcript WHERE created_at < ? ORDER BY seq LIMIT ?",
                        (cutoff, PRUNE_BATCH),
                    )
                ]
                if not ids:
                    break
                marks = ",".join("?" * len(ids))
                attachment_ids = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT id FROM transcript_attachments WHERE row_id IN ({marks})", ids
                    )
                ]
                with conn:
                    conn.execute(f"DELETE FROM transcript_attachments WHERE row_id IN ({marks})", ids)
                    conn.execute(f"DELETE FROM transcript WHERE id IN ({marks})", ids)
                removed += len(ids)
                for att_id in attachment_ids:
                    path = attachment_path(att_id)
                    if path is None:
                        continue
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        logger.exception("transcript: could not remove pruned attachment %s", path)
                if len(ids) < PRUNE_BATCH:
                    break
    except Exception:
        logger.exception("transcript: retention prune failed")
    if removed:
        logger.info("transcript: pruned %d row(s) older than %d days", removed, RETENTION_SECONDS // 86400)
    return removed

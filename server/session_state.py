"""Session facts that must outlive the server process.

Both halves of nightly consolidation (docs/tasks/nightly-consolidation.md
pieces 1 and 2) hinge on the server remembering something about a session
across a gap of hours:

- **which session is THE conversation**, so the 01:00 consolidate job can
  resume it rather than reviewing a blind fresh session (piece 2, which skips
  the job entirely when it can't find one). Since 2026-09-25 this is also what
  every phone message and cron job resumes: there is one conversation, owned
  here, not a phone session plus a fresh session per job (see
  `note_conversation`);
- **which sessions are retired**, so the phone's next message after a
  successful consolidation is routed to a fresh session (piece 1).

Both started life in `WorkQueue`'s in-memory dicts, on the reasoning that a
missed rollover self-corrects at the next night's run. The box disproved the
premise on 2026-08-15:

    23:55:01  cron job 'consolidate' skipped: no attended (ios) session to attach to

The last phone turn that day was 05:42 and the server restarted at 05:50, so
by 23:55 the queue's `_turns` held no phone turn and the attach lookup found
nothing. Nothing self-corrected — the phone kept the same session id (it is
persisted on the *device*), so the conversation simply ran on. It reached 94
turns over five days and ~4.8 MB of transcript, at which point a single reply
was reading millions of cached tokens.

That is the shape of the failure worth designing against: `Restart=always`,
plus deploys that land in the evening, means a restart between the day's last
phone message and 01:00 is ordinary rather than exceptional. And it fails
*silently and asymmetrically* — the log line reads like a decision the server
made, while the phone-side session that should have been capped grows without
limit. A restart on the other side of 01:00 loses the retirement instead, and
rollover quietly never fires.

Deliberately not a general persistence layer. Turn retention
(docs/tasks/turn-retention.md) is a different problem, and this file should
not grow into it: two facts, flat JSON, no schema.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from server.config import settings
from server.log import get_logger

logger = get_logger(__name__)

# One new entry per calendar day (one consolidate run), so this is years of
# headroom. Capped anyway because the file is read at every startup and there
# is no other thing that ever removes an id: an uncapped list is a slow leak
# with no upper bound, and the only ids that matter are recent ones — a
# session retired last spring is not one the phone can still be holding.
_MAX_RETIRED = 200


class SessionState:
    """Loaded once at construction, written through on every change.

    Every method is failure-tolerant by design. This is bookkeeping in service
    of a nightly job; an unreadable or unwritable state file must never be the
    reason a turn doesn't get answered. A load failure starts empty (which
    degrades to exactly the old in-memory behaviour), and a save failure is
    logged and swallowed.

    `_load` catches `Exception` rather than an enumerated tuple, which is the
    unusual call and so worth justifying: the singleton at the bottom of this
    file is constructed at *import*, so anything escaping `_load` is not a
    caught startup error but an ImportError on `server.queue` -- the server
    does not come up at all, and the handler that would have explained why is
    the one being skipped. The blast radius of an over-broad catch here is one
    night's rollover; the blast radius of a missed exception type is the whole
    server. An enumerated tuple already missed `TypeError` once (review,
    2026-08-16).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings.session_state_path
        self._conversation: str | None = None
        self._retired: list[str] = []
        self._load()

    def reload(self) -> None:
        """Drop in-memory state and re-read the file.

        Mutates in place rather than returning a new instance because callers
        hold a bound reference to the singleton (`from server.session_state
        import session_state`), so rebinding the module attribute would leave
        them pointing at the old object. Used by the test suite to isolate
        cases, and usable over SSH after hand-editing the file to force or
        undo a rollover without bouncing the server.
        """
        self._conversation, self._retired = None, []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            # Tolerate a hand-edited file: this is the one piece of state Mike
            # might reasonably poke at over SSH to force or undo a rollover.
            # Every value is type-checked rather than trusted -- an earlier
            # version iterated `retired_sessions` on faith, and `{"retired_
            # sessions": 5}` raised TypeError straight out of the constructor
            # below (review, 2026-08-16).
            # `last_attended_session_id` is the pre-2026-09-25 key for the same
            # fact. Read as a fallback so the first start after that deploy
            # carries the live conversation over instead of starting fresh.
            conversation = data.get("conversation_session_id", data.get("last_attended_session_id"))
            self._conversation = conversation if isinstance(conversation, str) else None
            retired = data.get("retired_sessions")
            if isinstance(retired, list):
                self._retired = [s for s in retired if isinstance(s, str)][-_MAX_RETIRED:]
        except Exception as e:
            logger.error(
                "session_state at %s is unreadable (%s) — starting empty. "
                "Tonight's consolidate run may skip; it recovers as soon as a "
                "phone turn completes.",
                self._path,
                e,
            )
            self._conversation, self._retired = None, []

    def _save(self) -> None:
        """Write via a temp file + atomic replace.

        A torn write here is worse than no write: a truncated JSON file reads
        as corrupt at the next startup and drops *both* facts, so the crash
        window is the thing to design out rather than to log about.
        """
        payload = {
            "conversation_session_id": self._conversation,
            "retired_sessions": self._retired,
        }
        tmp = self._path.with_suffix(".json.tmp")
        try:
            settings.ensure_dirs()
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.chmod(0o600)
            os.replace(tmp, self._path)
        except OSError as e:
            logger.error("could not persist session_state to %s: %s", self._path, e)
            # Not `unlink(missing_ok=True)` alone: that only forgives
            # FileNotFoundError, and a parent that is not a directory raises
            # NotADirectoryError here, out of the very handler that promises a
            # save failure never propagates (found 2026-10-01, root-run suite).
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    # --- the conversation ----------------------------------------------------

    def note_conversation(self, session_id: str) -> None:
        """Record the session a completed conversation turn ran in.

        Until 2026-09-25 only phone turns recorded here, the phone held its own
        session id, and every cron job except consolidate ran in a fresh
        session. Mike's report was the cost: a job blind to what he had said an
        hour earlier, and chat caught flat-footed by a message a job had just
        sent him -- both read as "Marlow is dumb". The separation's one safety
        argument (untrusted content a job reads unattended, e.g. email, stays
        out of the conversation) was weighed and accepted: chat already reads
        the same mail when asked. Forking the conversation per job is parked
        in docs/tasks/one-conversation.md until clutter is shown to hurt.

        Ordered by *completion*: one work queue, strictly serialized, and only
        a completed turn has a trustworthy session id to record.
        """
        if session_id and session_id != self._conversation:
            self._conversation = session_id
            self._save()

    @property
    def conversation_session_id(self) -> str | None:
        return self._conversation

    # --- retirement (piece 1's rollover) -----------------------------------

    def is_retired(self, session_id: str) -> bool:
        return session_id in self._retired

    def retire(self, session_id: str) -> bool:
        """Mark a session retired. Returns True if this was a new retirement,
        so the caller owns the log line and it stays at one per event."""
        if not session_id or session_id in self._retired:
            return False
        self._retired.append(session_id)
        del self._retired[:-_MAX_RETIRED]
        # Retiring the conversation ends it: the next turn of any kind -- the
        # morning brief, usually -- starts the new day's session fresh.
        if self._conversation == session_id:
            self._conversation = None
        self._save()
        return True


session_state = SessionState()

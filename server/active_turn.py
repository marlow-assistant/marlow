"""Which turn is executing right now, for the code that can't be handed it.

Exists for one question: **is there a human awake behind this invocation?**

The permission round-trip needs the answer and has no way to derive it. The
chain is server -> `claude -p` -> PreToolUse hook -> back to the server over
loopback, and the hook's payload carries only `tool_name`/`tool_input`/
`session_id` (that schema is the CLI's, not ours). `session_id` can't stand in
for the answer either: a cron job enqueues with `session_id=None, resume=False`
(server/app.py's `cron_message`), so its id is minted by the CLI mid-run and the
server has never seen it before the hook quotes it back.

A module global is exact here rather than sloppy, and the reason is structural:
**one work queue serializes every `claude -p` call** (a hard constraint, see
CLAUDE.md), so at most one invocation is ever in flight. A `ContextVar` would be
the reflex and would be wrong — `queue._drain` and the `/internal/
permission-request` handler run as *different* asyncio tasks, and a value set in
one task is invisible in the other, which is precisely the pair that has to
agree.

Unknown sources are treated as unattended. That is the fail-closed direction: a
new caller that forgets to thread `source` gets instant denials on tools that
need a prompt (loud, safe, and obvious the first time anyone tries it) rather
than silently ringing a phone from a context that has no one to ring.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

# Turns a person sent from a paired device (the phone or the web client), as
# opposed to "cron", "system" and the compaction source. `POST /v1/message`
# writes "chat". "ios" is what it wrote before the 2026-10 rename, when the
# phone was the only client; it stays in the set for good because stored
# records (transcript rows kept 30 days, activity-log records) still carry it
# and every reader of those must treat the two identically. Nothing writes "ios"
# any more. Every "is this a chat turn" check in the server goes through this
# set rather than a literal, so the two spellings cannot drift apart.
CHAT_SOURCES: frozenset[str] = frozenset({"chat", "ios"})

# Sources with a human and a paired device on the other end. Everything else --
# "cron", "system", None, and anything added later -- is unattended.
#
# FAIL-CLOSED ALLOWLIST: whatever `/v1/message` writes MUST be in here, in the
# same commit. A source missing from this set is unattended, so the turn gets no
# live streaming and every tool request is denied without asking. The rename
# from "ios" to "chat" is exactly this hazard; tests/test_app.py pins it.
#
# Note that `WorkQueue.enqueue` requires its caller to name a source rather than
# defaulting to one. It used to default to "ios" (now "chat"), which quietly
# inverted the fail-closed property this module advertises: a new caller that
# omitted the argument -- the likeliest way to get it wrong -- became ATTENDED
# and could ring Mike's phone from a 3am job (review, 2026-08-07).
ATTENDED_SOURCES: frozenset[str] = CHAT_SOURCES

# Returned to the hook alongside an unattended `deny` so it can tell this apart
# from a timeout or a real refusal. The hook owns the model-facing wording; this
# is only the machine-readable half. Without it the hook has one string for
# every denial -- "the user did not approve this tool use via the phone approval
# prompt" -- which is a false claim about Mike when no prompt was ever raised
# (review, 2026-08-07; same class as the 2026-08-04 deny-reason wording fix).
UNATTENDED_DENY_REASON = "unattended"

# The reason code for an allow that came from a similar grant ("Allow similar
# for this reply"), so the hook can tell the model no prompt was shown for THIS
# call. Here rather than in server/permissions.py for the hook's sake: importing
# that module pulls in server.log's rotating handler, which the hook must never
# open (tests/test_pretooluse.py).
SIMILAR_ALLOW_REASON = "similar"

_current_source: str | None = None


@contextmanager
def running(source: str | None) -> Iterator[None]:
    """Mark `source` as the in-flight invocation for the duration of the block.

    Saves and restores rather than clearing, so a nested call (there shouldn't
    be one, but the queue is not the only possible caller) can't leave the
    module claiming nothing is running while an outer invocation still is.
    """
    global _current_source
    previous = _current_source
    _current_source = source
    try:
        yield
    finally:
        _current_source = previous


def current_source() -> str | None:
    return _current_source


def is_unattended() -> bool:
    """True when nothing useful would come of asking the phone.

    Note this is also True when *no* invocation is in flight. That state is
    unreachable for a real hook call -- the hook only runs inside one -- and
    denying is the right answer for a permission request that arrives from
    nowhere.
    """
    return _current_source not in ATTENDED_SOURCES

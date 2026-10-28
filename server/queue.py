"""THE work queue. Single asyncio task drains it; only one `claude -p`
invocation is ever in flight (plan §3.2) — protects the git-push races and the
`.credentials.json` refresh write-back (gate1-results §6). All invocation
sources — iOS, cron, anything — enqueue here rather than calling invoke.py
directly.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from server import active_turn, activity_log, auth_monitor, transcript
from server.attachments import cleanup_attachment, sweep_orphaned_attachments
from server.config import settings
from server.identity_git import commit_after_turn
from server.idle_compaction import SOURCE as COMPACTION_SOURCE
from server.idle_compaction import idle_compaction_watcher
from server.invoke import ActivityEvent, InvocationResult, InvokeError, ToolCall, run_claude
from server.log import get_logger
from server.push import PushPayload, PushType
from server.push import broadcast_nowait as push_broadcast_nowait
from server.schedules import sync_after_turn
from server.session_state import session_state
from server.usage_state import usage_state

logger = get_logger(__name__)

# `_turns` retention cap (docs/tasks/turn-retention.md). A round, generous
# number chosen over a time window: simple, and this is a resource-leak fix,
# not a design for how long history should live -- see that doc for the
# "properly persistent" direction this deliberately punts on.
MAX_RETAINED_TURNS = 256

# A terminal turn is never evicted before it has been terminal for at least
# this long (review, 2026-08-19) -- nothing records that a client has
# fetched a turn (GET /v1/turn/{id} is a read, and the transcript holds the
# durable copy), so there is no "claimed" signal to gate on; the grace window
# is what gives a polling client time to see the terminal state. (A
# `delivered` flag set by /v1/pending used to exist and was rejected as that
# signal for the same reason: most turns never set it.) This is a floor, not a
# retention promise -- comfortably longer than any normal poll gap, short
# enough that it never meaningfully delays capping runaway growth.
EVICTION_GRACE_SECONDS = 300.0

# _client_msg_index's idempotency window is independent of _turns' retention
# cap on purpose (docs/tasks/turn-retention.md) -- it exists for network
# retry of the SAME send, which happens within seconds, not for remembering
# a client_msg_id for as long as its turn happens to survive the count cap.
# Tying the two together would mean a quiet day (fewer than MAX_RETAINED_TURNS
# turns since) leaves old client_msg_ids matchable for days, so a legitimately
# new message that happens to reuse an id would be silently swallowed and
# answered with a stale reply instead.
CLIENT_MSG_ID_WINDOW_SECONDS = 600.0

# Bound one coalesced train of thought so an abnormal backlog cannot become one
# enormous invocation (docs/tasks/coalesce-queued-turns.md §4).
MAX_COALESCED_TURNS = 5

# How often the transcript's 30-day retention runs (docs/tasks/server-transcript.md).
# Daily is plenty: retention is measured in days, and the cutoff moves with the
# clock whether or not a pass has run.
TRANSCRIPT_PRUNE_INTERVAL_SECONDS = 24 * 3600

# A scheduled run that has nothing to say replies with exactly this (or with
# nothing at all), and the server writes no transcript row and sends no push.
# Without it every cron reply was delivered, which is what held the mid-day
# inbox check to twice a day: each run that found nothing still buzzed the
# phone with "nothing high-priority" (2026-10-02). It applies only to a DONE
# cron turn whose WHOLE reply is the token. A reply that merely mentions it is
# delivered as written, and so is an error -- so when the convention misfires
# Mike gets a message he didn't need, never silence where something was due.
# The identity names the token (identity/CLAUDE.md "Scheduled jobs"); keep the
# two in step.
SILENT_REPLY_TOKEN = "[silent]"


def is_silent_reply(text: str | None) -> bool:
    # Backticks tolerated: the token reads like code, and a model quoting it
    # as `[silent]` plainly means the same thing.
    return (text or "").strip().strip("`").strip().lower() in ("", SILENT_REPLY_TOKEN)


class TurnStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    # Folded into `Turn.merged_into`; has no invocation result of its own.
    MERGED = "merged"
    # A turn cancelled before it started running (docs/tasks/idle-compaction.md
    # — an attended message cancels a still-QUEUED compaction). Never applied
    # to a turn already RUNNING; that one is left to finish, see
    # `_cancel_queued_compactions`'s docstring for why.
    CANCELLED = "cancelled"


@dataclass
class Turn:
    turn_id: str
    prompt: str
    session_id: str | None
    resume: bool
    source: str  # "chat" | "cron" | ...  ("ios" survives only in stored records)
    client_msg_id: str | None
    status: TurnStatus = TurnStatus.PENDING
    result: InvocationResult | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    # Set once, when `_drain` gives this turn a terminal status. None means
    # still PENDING/RUNNING. `_evict_old_turns` uses this, not `created_at`,
    # for the eviction-grace floor -- what matters is how long a *finished*
    # turn has been sitting there unclaimed, not how long ago it was sent.
    finished_at: float | None = None
    # True for a job that must never fall back to a fresh session (the
    # dead-session self-heal invoke.py otherwise does for `resume`) — set by
    # the nightly consolidation attach (docs/tasks/nightly-consolidation.md
    # piece 2), whose whole safety argument depends on this turn never running
    # in a session with no memory of the day it's asked to review.
    require_session: bool = False
    # Paths written by server/app.py before this turn was enqueued
    # (docs/tasks/ios-image-input.md); up to _MAX_IMAGES, in attach order.
    attachment_paths: tuple[str, ...] = ()
    # Validated per-turn phone preference; None means the configured default.
    # See docs/tasks/effort-toggle-server-half.md.
    effort: str | None = None
    # The user spoke this message and will hear the reply read aloud
    # (docs/tasks/voice-mode-hands-free.md); invoke.py tells Marlow so.
    spoken: bool = False
    # True for the job whose successful completion should retire the session
    # it just resumed (docs/tasks/nightly-consolidation.md piece 1, "Design
    # settled 2026-08-16"). Set by server/app.py's cron_message from the same
    # ATTACHED_SESSION_JOBS membership that decides `resume` — deliberately
    # the same condition, not a coincidence: the job that attaches to the
    # phone's session is, by construction, the last turn of that
    # conversation, so its success is exactly the rollover trigger. Never
    # true for source="chat" turns; a phone message never retires its own
    # session.
    retire_session_on_success: bool = False
    # True for a turn that runs in THE conversation (server/session_state.py,
    # `note_conversation`): phone messages and every cron job except
    # consolidate, which names its session explicitly. Resolved in `_drain`,
    # at run time, not here -- a turn queued behind one that rolls the
    # session (consolidation, a dead-session retry) must land where that turn
    # left the conversation, not where it was when this one was sent.
    in_conversation: bool = False
    # True once an attended message has made this queued turn moot
    # (docs/tasks/idle-compaction.md). Only ever set on a PENDING turn — see
    # `_cancel_queued_compactions`. `_drain` checks this before doing
    # anything else and skips straight to CANCELLED without invoking.
    cancelled: bool = False
    # Threaded to invoke.py's run_claude(raw_prompt=...) — see that
    # docstring. Default False everywhere; only the compaction path
    # (server/idle_compaction.py) sets this True.
    raw_prompt: bool = False
    # Everything this turn has been observed doing, oldest first, for the
    # phone's status line (docs/tasks/turn-activity-streaming.md).
    #
    # This was a single latest-wins slot until 2026-08-22 and that was wrong,
    # for a reason measurement made obvious: the client polls every 2s while a
    # local file read takes ~0.16s, so "what is it doing right now" structurally
    # cannot show a short tool call. It would be sampled about 8% of the time.
    # invoke.py was already emitting every event -- the loss was entirely here,
    # three lines before it reached the client. Accumulating costs a list per
    # running turn and lets one poll see everything since the last one.
    #
    # Emptied the moment the turn reaches a terminal status: a turn that is
    # DONE is not still reading a file, and a stale label outliving its turn is
    # the one failure mode that would make this worse than the silence it
    # replaces.
    #
    # Only ever appended to on attended turns; invoke.py declines to stream for
    # anything else, so cron and compaction turns leave this empty throughout.
    activities: list[ActivityEvent] = field(default_factory=list)
    # The reply as it is being written, oldest character first
    # (docs/tasks/streaming-reply-text.md). Empty except on an attended turn
    # mid-flight, same as `activities`.
    #
    # A SEPARATE field, deliberately not an entry in the list above, because
    # the two have opposite failure tolerances. `activities` is capped with
    # FIFO eviction, which is right for status labels -- losing an old
    # "Reading a file..." costs nothing -- and catastrophic here, where
    # dropping the oldest entry silently deletes the BEGINNING of the answer.
    # Do not unify them.
    #
    # Always an approximation: assembled from deltas that can be capped or
    # arrive after a dropped poll. `result.result_text` is authoritative and is
    # what the client renders once the turn is terminal -- and since 2026-09-04
    # it is reassembled from these same blocks when the turn interleaved text
    # and tools, because the CLI's envelope carries only the last one
    # (invoke.py `_whole_reply`). This field feeds the live render; that one
    # settles it.
    partial_text: str = ""
    # Character offsets into `partial_text` at which a tool call started, so
    # the 11% of turns that interleave text and tools do not render as two
    # thoughts fused into one sentence ("I'll check your calendar.You have
    # three meetings"). The client draws its existing tool chip at each offset.
    partial_text_marks: list[int] = field(default_factory=list)
    # Set only on a MERGED turn: the final turn in the coalesced run owns the
    # invocation result and is where the client renders the reply.
    merged_into: str | None = None
    # What the activity log calls this turn (docs/tasks/ux-lane.md §2a): the
    # cron job or schedules entry id. None for the phone, whose turns need no
    # name. Display only -- nothing may branch on it; `source` and the flags
    # above carry every behavioural decision.
    label: str | None = None
    # "plumbing" for a harness job the user doesn't own (consolidate, ideas),
    # else None (docs/tasks/default-infrastructure.md). Set only from the
    # `job` field of a cron dispatch, never from a schedules entry id, which
    # the model chooses -- the same split `CronMessageBody` keeps. Today the
    # activity record carries it for the client's "background jobs" toggle.
    tier: str | None = None
    # When `_drain` handed this turn to the CLI. `created_at` is when it was
    # sent, and the gap between them is queue wait, not work.
    started_at: float | None = None
    # Every tool call, for the persisted log -- NOT `activities`, which is
    # attended-only, FIFO-capped and emptied at the terminal status. This one
    # is filled on every recorded turn, cron included, keeps the FIRST
    # MAX_LOGGED_TOOL_CALLS, and survives to the log write. Targets come only
    # from invoke.py's allowlist.
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_calls_dropped: int = 0
    # Outcomes of the permission round-trip while this turn ran: a human
    # answer, or a timeout. Unattended turns never ask (app.py denies them
    # up front), so theirs show only as `permission_denials`.
    approvals: list[dict[str, str]] = field(default_factory=list)
    # The transcript user-row turn_ids this turn's reply answers when that is
    # NOT just its own (docs/tasks/server-transcript.md). Set by POST
    # /v1/message when the transcript already held a row for this client_msg_id
    # under a different turn_id: a retry after an ERROR, or after the queue's
    # 600s dedup window forgot. The row is not rewritten, so the new turn's
    # reply has to name it. Empty for every other turn, which answers its own id.
    answers_turn_ids: list[str] = field(default_factory=list)


# Every Turn field must be classified here before it can participate in
# coalescing. This is deliberately exhaustive rather than just naming today's
# disqualifying flags: a future per-turn contract must fail closed until its
# author decides whether it is metadata, a match key, or an eligibility flag
# (docs/tasks/coalesce-queued-turns.md §4).
_COALESCING_CLASSIFIED_TURN_FIELDS = frozenset(
    {
        # Invocation payload / record identity; safe to differ within a run.
        "turn_id",
        "prompt",
        "client_msg_id",
        # Must match the head; enforced by the collection loop.
        "session_id",
        "resume",
        "effort",
        "spoken",
        # Eligibility contract; enforced by _can_coalesce below.
        "source",
        "require_session",
        "attachment_paths",
        "retire_session_on_success",
        "cancelled",
        # Must match the head; enforced by the collection loop.
        "in_conversation",
        "raw_prompt",
        # Queue-owned lifecycle metadata. Candidates are pending, so these are
        # at their defaults until the run settles them.
        "status",
        "result",
        "error",
        "created_at",
        "finished_at",
        "activities",
        "partial_text",
        "partial_text_marks",
        "merged_into",
        # Activity-log metadata (ux-lane.md §2a). `label` is display-only and
        # only cron sets it, and cron never coalesces; the rest is settled by
        # the run, like the lifecycle fields above. `tier` the same: only a
        # cron job dispatch sets it.
        "label",
        "tier",
        "started_at",
        "tool_calls",
        "tool_calls_dropped",
        "approvals",
        # Transcript metadata: which user rows the reply answers. Every
        # coalesced turn's own value is carried into the survivor's row.
        "answers_turn_ids",
    }
)


# Cap on `Turn.activities`. Generous -- a turn making more than this many tool
# calls is already far past what any status line can usefully narrate, and the
# client is told to skip ahead rather than drain a long backlog. Oldest are
# dropped, so what survives is the recent activity, which is the part still
# worth showing.
MAX_TURN_ACTIVITIES = 64


# Cap on `Turn.partial_text`. Turn eviction already bounds `_turns` at 256, but
# nothing bounds the growth of a SINGLE entry and a runaway turn must not be
# able to. Far past any reply Marlow has written; `result_text` is unaffected
# either way (docs/tasks/turn-retention.md).
MAX_PARTIAL_TEXT_CHARS = 100_000


def _record_activity(turn: Turn):
    """Callback that appends each ActivityEvent to `turn`, oldest first.

    A named function rather than a lambda in the call below so it can carry the
    cap and say why the list exists at all: see `Turn.activities`.
    """

    def _append(event: ActivityEvent) -> None:
        if event.kind == "tool_start":
            # Where in the reply-so-far this tool call happened. Recorded here
            # rather than in invoke.py because this is the only place that
            # knows how much text has accumulated; both callbacks are driven
            # from the same stdout line loop, in order, so the offset is exact.
            mark = len(turn.partial_text)
            if not turn.partial_text_marks or turn.partial_text_marks[-1] != mark:
                turn.partial_text_marks.append(mark)
                if len(turn.partial_text_marks) > MAX_TURN_ACTIVITIES:
                    del turn.partial_text_marks[0]
        turn.activities.append(event)
        if len(turn.activities) > MAX_TURN_ACTIVITIES:
            del turn.activities[0 : len(turn.activities) - MAX_TURN_ACTIVITIES]

    return _append


# Cap on `Turn.tool_calls`. Keeps the FIRST calls, the opposite of
# `activities`: a log reads from the start of the turn, and a runaway turn is
# already told apart by `tool_calls_dropped`.
MAX_LOGGED_TOOL_CALLS = 100

# Cap on `Turn.approvals`. Each one is a phone prompt; far past any real turn.
MAX_LOGGED_APPROVALS = 32


def _record_tool_call(turn: Turn):
    """Callback that appends each ToolCall to `turn`, up to the cap."""

    def _append(call: ToolCall) -> None:
        if len(turn.tool_calls) >= MAX_LOGGED_TOOL_CALLS:
            turn.tool_calls_dropped += 1
            return
        turn.tool_calls.append(call)

    return _append


def _record_text(turn: Turn):
    """Callback that accumulates reply-text deltas onto `turn`.

    A growing string, not a window: the client re-reads the whole thing each
    poll and renders it if it got longer, so there is no cursor, no dedupe and
    nothing to resync after a missed poll. Truncation at the cap drops the
    TAIL, keeping the answer's beginning readable -- the opposite choice from
    `activities`, for the reason in `Turn.partial_text`.
    """

    def _append(chunk: str) -> None:
        if len(turn.partial_text) >= MAX_PARTIAL_TEXT_CHARS:
            return
        turn.partial_text = (turn.partial_text + chunk)[:MAX_PARTIAL_TEXT_CHARS]

    return _append


def _clear_progress(turn: Turn) -> None:
    """Drop everything that only described a turn mid-flight.

    One function because these must always be cleared together: a status label
    outliving its turn is a stale line under a finished answer, and a
    `partial_text` outliving its turn is a previous reply's half-written text
    showing under a fresh (coalesced or re-run) placeholder.
    """
    turn.activities.clear()
    turn.partial_text = ""
    turn.partial_text_marks.clear()


class WorkQueue:
    def __init__(self) -> None:
        # Not constructed here: asyncio.Queue binds to whatever event loop
        # first touches it. Built at __init__ time (module import, before any
        # loop is running) it would bind to the wrong loop the moment a
        # second server lifecycle starts in the same process — e.g. every
        # test that spins up its own event loop via TestClient. start()
        # always runs inside the loop that will actually serve requests, so
        # the queue is (re)created there instead.
        self._queue: asyncio.Queue[str] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._turns: dict[str, Turn] = {}
        # client_msg_id -> (turn_id, inserted_at). The timestamp is what makes
        # this an idempotency window (CLIENT_MSG_ID_WINDOW_SECONDS) rather
        # than a cache that lives as long as its turn survives `_turns`'
        # eviction -- see that constant's comment.
        self._client_msg_index: dict[str, tuple[str, float]] = {}
        # Retirement and the attended-session lookup both live in
        # server/session_state.py, on disk — NOT in `_turns` beside
        # `_client_msg_index` above. That was the original design, on the
        # reasoning that a missed rollover self-corrects at the next night's
        # run. The box disproved it on 2026-08-15: a restart at 05:50 wiped
        # the day's phone turns, 23:55's consolidate logged "no attended (ios)
        # session to attach to", and nothing self-corrected — the phone holds
        # its session id on the *device*, so the conversation ran on to 94
        # turns across five days. See that module's docstring for the full
        # reasoning; the short version is that with `Restart=always` plus
        # evening deploys, a restart between the day's last message and 01:00
        # is ordinary, and both halves fail silently when it happens.
        self._consumer_task: asyncio.Task | None = None
        # Startup schedule reconciliation is offloaded because `crontab -l`
        # and `crontab <file>` are blocking subprocesses. The drain loop waits
        # for it before processing the first turn, so startup sync cannot race
        # the same sync that runs after that turn.
        self._schedule_sync_task: asyncio.Task | None = None
        # Referenced only to keep it alive — asyncio warns a Task with no
        # remaining reference can be GC'd mid-run. Never awaited; the sweep
        # is fire-and-forget.
        self._sweep_task: asyncio.Task | None = None
        # Transcript retention: once at start, then daily. A loop, so unlike
        # the one-shot sweep above it is cancelled in stop().
        self._retention_task: asyncio.Task | None = None
        # Set while the drain loop is holding a turn because reauth_needed
        # (plan §6a) — surfaced alongside queue_depth so a paused queue is
        # observable rather than looking like a wedged/slow drain.
        self._paused = False
        # `asyncio.Queue` has no un-get. When coalescing pops the first
        # ineligible turn, keep it here so the next loop iteration consumes it
        # before awaiting a later queue item and preserves FIFO exactly.
        self._carry: str | None = None

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._consumer_task is not None:
            if self._loop is not loop:
                raise RuntimeError("WorkQueue is already running on a different event loop")
            return
        self._loop = loop
        self._queue = asyncio.Queue()
        self._carry = None
        self._schedule_sync_task = asyncio.create_task(asyncio.to_thread(sync_after_turn))
        self._consumer_task = asyncio.create_task(self._drain())
        logger.info("work queue consumer started")
        # Once per process start, not per turn: a restart is the only way
        # an attachment dir becomes orphaned (see attachments.py), so
        # sweeping here is the natural place — and cheap enough to not
        # need its own periodic schedule. Dispatched to a thread rather
        # than called inline: this runs before uvicorn.serve() is reached
        # (`/code-review medium` finding), and the sweep's os.listdir/
        # getmtime/rmtree pass over settings.attachments_dir would
        # otherwise delay server readiness by however long that scan
        # takes. Nothing downstream depends on it finishing before the
        # server starts accepting requests.
        self._sweep_task = asyncio.create_task(asyncio.to_thread(sweep_orphaned_attachments))
        # A separate task from the sweep, though the transcript doc
        # (docs/tasks/server-transcript.md) says "the same task": the sweep is
        # a one-shot (a restart is the only way a dir is orphaned, see above)
        # and tests await it to completion, so turning it into the daily loop
        # would either hang them or put the attachment sweep on a schedule it
        # was deliberately not given. Same shape otherwise: started here,
        # blocking work in a thread.
        #
        # `time.time()` here, not inside the loop: the interrupted-turn pass it
        # begins with must tell a row from BEFORE this process from one sent
        # while it runs.
        self._retention_task = asyncio.create_task(self._retention_loop(time.time()))

    async def stop(self) -> None:
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None
            self._queue = None
            self._loop = None
            self._carry = None
        if self._retention_task is not None:
            self._retention_task.cancel()
            try:
                await self._retention_task
            except asyncio.CancelledError:
                pass
            self._retention_task = None
        if self._schedule_sync_task is not None:
            try:
                await self._schedule_sync_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("startup schedule sync failed during shutdown")
            self._schedule_sync_task = None

    async def _retention_loop(self, started_at: float) -> None:
        """First, answer the user rows a restart orphaned; then prune the
        transcript now and once a day. Neither `transcript` call raises; the
        guards are for the same reason as the others in this file -- one bad
        pass must not end the daily ones.

        `_turns` is in memory, so any user row older than `started_at` with no
        reply names a turn that died with the previous process
        (`transcript.mark_interrupted`). Off the loop, alongside the sweep,
        and not awaited by anything: the server takes messages while it runs.
        """
        try:
            await asyncio.to_thread(transcript.mark_interrupted, started_at)
        except Exception:
            logger.exception("transcript interrupted-turn pass failed")
        while True:
            try:
                await asyncio.to_thread(transcript.prune)
            except Exception:
                logger.exception("transcript retention pass failed")
            await asyncio.sleep(TRANSCRIPT_PRUNE_INTERVAL_SECONDS)

    def reusable_turn_id(self, client_msg_id: str) -> str | None:
        """The live turn this client_msg_id already maps to, or None. THE
        idempotency condition: `enqueue` returns this id instead of making a
        turn, and POST /v1/message asks it first so the transcript row it is
        about to write carries the id the client will actually be given, not one
        minted and then discarded.

        Live means inside CLIENT_MSG_ID_WINDOW_SECONDS and not ERROR. An ERROR
        is excepted: the dedupe exists so a resend doesn't run the message
        twice, and an errored turn never answered it. Handing it back made the
        phone's retry button replay the failure instantly without running
        `claude -p` (2026-09-28, an API 522). A fresh turn takes over the index
        entry in `enqueue`; the errored one stays in `_turns` as a record. iOS
        never files a `chat` turn it did not place, so the abandoned one cannot
        come back as a stray bubble (ChatStore.ingestUnsolicited).
        """
        cached = self._client_msg_index.get(client_msg_id)
        if cached and time.time() - cached[1] < CLIENT_MSG_ID_WINDOW_SECONDS:
            cached_turn = self._turns.get(cached[0])
            if cached_turn is None or cached_turn.status != TurnStatus.ERROR:
                return cached[0]
        return None

    def enqueue(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        resume: bool = False,
        source: str,
        client_msg_id: str | None = None,
        require_session: bool = False,
        turn_id: str | None = None,
        attachment_paths: tuple[str, ...] = (),
        effort: str | None = None,
        spoken: bool = False,
        retire_session_on_success: bool = False,
        raw_prompt: bool = False,
        in_conversation: bool = False,
        label: str | None = None,
        tier: str | None = None,
        answers_turn_ids: list[str] | None = None,
    ) -> str:
        """Returns a turn_id. Resending the same client_msg_id returns the
        existing turn_id instead of enqueuing a duplicate (§3.1 idempotency).

        Must run on the event loop captured by start(). This method mutates the
        queue, turn table, dedup index, retention state and pending compactions
        as one synchronous operation; scheduling only Queue.put_nowait from a
        worker thread would leave every other mutation racing the drain task.
        HTTP handlers that call this are therefore async handlers, even though
        their blocking preparation work is dispatched to a thread.

        `turn_id`, if given, is used verbatim instead of minting a fresh uuid4.
        Exists for server/app.py's image path: the attachment file has to be
        written to `<attachments_dir>/<turn_id>/<n>.jpg` BEFORE this call, so the
        id has to be known ahead of it rather than generated inside. A resend
        that hits the client_msg_id dedup below ignores this turn_id (and the
        attachment_paths that go with it) entirely — the file just-written for
        it becomes an orphan, cleaned up by the 24h sweep rather than by this
        call, which matches the accepted cost in the build spec's "one to
        challenge" section (wasted transfer, not a duplicate turn).

        `source` is REQUIRED and deliberately has no default. It used to default
        to "ios" (the attended source, now "chat"), which silently inverted the fail-closed property of
        server/active_turn.py: a caller that omitted it -- the likeliest way to
        get it wrong -- produced an ATTENDED turn, so a new unattended trigger
        (an internal timer, a webhook) could raise a permission prompt on Mike's
        phone at 3am while every test stayed green. Omission is now a TypeError
        at import-adjacent call sites rather than a decision nobody made
        (review, 2026-08-07). Anything not in `active_turn.ATTENDED_SOURCES` is
        treated as having nobody to ask.
        """
        if self._queue is None or self._loop is None:
            raise RuntimeError("WorkQueue.start() must run before enqueue()")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError as e:
            raise RuntimeError("WorkQueue.enqueue() must run on its owning event loop") from e
        if current_loop is not self._loop:
            raise RuntimeError("WorkQueue.enqueue() must run on its owning event loop")
        if client_msg_id:
            reusable = self.reusable_turn_id(client_msg_id)
            if reusable is not None:
                return reusable

        turn_id = turn_id or str(uuid.uuid4())
        turn = Turn(
            turn_id=turn_id,
            prompt=prompt,
            session_id=session_id,
            resume=resume,
            source=source,
            client_msg_id=client_msg_id,
            require_session=require_session,
            attachment_paths=attachment_paths,
            effort=effort,
            spoken=spoken,
            retire_session_on_success=retire_session_on_success,
            raw_prompt=raw_prompt,
            in_conversation=in_conversation,
            label=label,
            tier=tier,
            answers_turn_ids=list(answers_turn_ids or []),
        )
        self._turns[turn_id] = turn
        if client_msg_id:
            self._client_msg_index[client_msg_id] = (turn_id, turn.created_at)
        self._queue.put_nowait(turn_id)

        # docs/tasks/idle-compaction.md: an attended message is reason enough
        # to skip a compaction that hasn't started running yet — Mike waiting
        # on a reply must never queue behind maintenance nobody asked for.
        # Here, in enqueue() itself, rather than at one call site remembering
        # to do it: every attended path (iOS today, anything attended later)
        # gets the cancel for free.
        if source in active_turn.ATTENDED_SOURCES:
            self._cancel_queued_compactions()

        return turn_id

    def _cancel_queued_compactions(self) -> None:
        """Mark any still-PENDING compaction turn cancelled so `_drain` skips
        it instead of running it. `asyncio.Queue` has no way to remove a
        queued item, hence marking rather than removing.

        Only PENDING turns are touched — a compaction already RUNNING is left
        alone. Mike's call (2026-08-18), settling the design doc's open
        question: killing the subprocess mid-compaction is only safe if
        compaction commits atomically, and the probe showed it does not — the
        CLI's transcript is append-only, so a killed compaction can't be told
        apart from one that silently corrupted the session. Letting it finish
        costs a bounded wait (the compaction call itself); killing it risks
        the session.
        """
        for turn in self._turns.values():
            if turn.source == COMPACTION_SOURCE and turn.status == TurnStatus.PENDING and not turn.cancelled:
                turn.cancelled = True
                logger.info(
                    "cancelled queued compaction turn %s: an attended message arrived", turn.turn_id
                )

    def _evict_old_turns(self) -> None:
        """Bound `_turns`' otherwise-unbounded growth (docs/tasks/turn-retention.md)
        with a simple count cap. `enqueue()` is the only inserter, so dict
        insertion order tracks `created_at` order and the oldest turns are
        always at the front. Only terminal turns are evicted -- PENDING/RUNNING
        ones are skipped even if old, so a turn held on reauth (the KNOWN GAP
        in `_drain`) or still queued behind a burst is never at risk.

        Also skips a terminal turn still inside EVICTION_GRACE_SECONDS of
        finishing (review, 2026-08-19) -- no client-fetched signal exists to
        gate on here (see that constant's comment), so the grace window is
        what stands between a burst of turns and one getting evicted before
        any client -- fast-polling sender or a reconnecting one -- has had a
        chance to fetch it.
        """
        if len(self._turns) <= MAX_RETAINED_TURNS:
            return
        now = time.time()
        for turn_id, turn in list(self._turns.items()):
            if len(self._turns) <= MAX_RETAINED_TURNS:
                break
            if turn.status in (TurnStatus.PENDING, TurnStatus.RUNNING):
                continue
            if turn.finished_at is not None and now - turn.finished_at < EVICTION_GRACE_SECONDS:
                continue
            del self._turns[turn_id]
            if turn.client_msg_id and self._client_msg_index.get(turn.client_msg_id, (None, None))[0] == turn_id:
                del self._client_msg_index[turn.client_msg_id]

    def _prune_client_msg_index(self) -> None:
        """Age out `_client_msg_index` entries past CLIENT_MSG_ID_WINDOW_SECONDS.

        Independent of `_evict_old_turns` on purpose: the idempotency window
        is about how long ago a *send* happened, not whether its turn has
        been evicted yet, and the two caps use different units for exactly
        the reason CLIENT_MSG_ID_WINDOW_SECONDS' comment gives. `enqueue()`
        already treats a stale entry as a miss (the freshness check there),
        so this is bookkeeping to keep the dict from growing forever -- not
        load-bearing for correctness.
        """
        cutoff = time.time() - CLIENT_MSG_ID_WINDOW_SECONDS
        stale = [cid for cid, (_, inserted_at) in self._client_msg_index.items() if inserted_at < cutoff]
        for cid in stale:
            del self._client_msg_index[cid]

    def has_pending_turn(self, source: str) -> bool:
        """True if a turn of this source is still queued or currently
        running (and not cancelled). Used by the idle-compaction watcher so
        its poll doesn't pile a second compaction on top of one already
        outstanding."""
        return any(
            t.source == source and not t.cancelled and t.status in (TurnStatus.PENDING, TurnStatus.RUNNING)
            for t in self._turns.values()
        )

    def get_turn(self, turn_id: str) -> Turn | None:
        return self._turns.get(turn_id)

    def running_turn_id(self) -> str | None:
        """The RUNNING turn's id: one queue serializes every invocation, so it
        is the turn a permission request came from (see below)."""
        for turn in self._turns.values():
            if turn.status == TurnStatus.RUNNING:
                return turn.turn_id
        return None

    def note_permission_decision(self, tool: str, decision: str, via: str | None = None) -> None:
        """Record a permission round-trip outcome on the running turn, for the
        activity log. The hook's request carries no turn id, but one queue
        serializes every invocation, so the RUNNING turn is the one that asked.
        No running turn (a request outliving its turn) records nothing."""
        for turn in self._turns.values():
            if turn.status == TurnStatus.RUNNING:
                if len(turn.approvals) < MAX_LOGGED_APPROVALS:
                    entry = {"tool": tool, "decision": decision}
                    if via:
                        # "similar": allowed by a grant, no prompt raised.
                        # Still "allow", so clients that count approvals by
                        # decision keep counting it.
                        entry["via"] = via
                    turn.approvals.append(entry)
                return

    def turn_id_for_client_msg_id(self, client_msg_id: str) -> str | None:
        """Return a still-live idempotency match without mutating its window."""
        cached = self._client_msg_index.get(client_msg_id)
        if cached and time.time() - cached[1] < CLIENT_MSG_ID_WINDOW_SECONDS:
            return cached[0]
        return None

    def conversation_session_id(self) -> str | None:
        """The session the conversation is in -- what consolidate (piece 2,
        docs/tasks/nightly-consolidation.md) resumes to review the day, and
        what every `in_conversation` turn resumes in `_drain`.

        Only DONE turns with a resolved result.session_id count: an ERROR'd
        or still-PENDING/RUNNING turn has no trustworthy id. `_drain` records
        each one as it completes.

        Read from disk rather than by scanning `_turns`, which is what this
        did until 2026-08-16 — that scan returned None after any restart, so
        the consolidate job skipped whenever the server had bounced since the
        day's last phone message. See server/session_state.py.
        """
        return session_state.conversation_session_id

    def is_session_retired(self, session_id: str) -> bool:
        """Has this session been rolled over by a successful consolidation
        turn? Checked by `_drain` before honouring an attended (`source ==
        "chat"`) turn's session hint (piece 1). Public so app.py/tests can
        assert on it without reaching into the private set."""
        return session_state.is_retired(session_id)

    def _retire_session(self, session_id: str) -> None:
        """Mark a session retired. Only called from `_drain`, and only after
        the turn that resumed it has actually completed successfully — see
        the call site for the fail-safe conditions (status DONE, not ERROR;
        `retire_session_on_success` set at enqueue time). Retirement takes
        effect lazily: nothing here touches any other Turn or forces a
        rollover immediately. The next attended send that names this session
        is what triggers the fresh-session routing, in `_drain` below.

        Persisted, so a restart between 01:00 and Mike's first message of the
        morning does not quietly lose the rollover."""
        if session_state.retire(session_id):
            logger.info("session %s retired: its consolidation turn completed successfully", session_id)

    def post_system_message(self, text: str, *, client_msg_id: str | None = None) -> str:
        """Injects an already-DONE turn directly, bypassing invoke.py/`claude
        -p` entirely — for server-generated notices the assistant never said,
        e.g. gate #4's "couldn't reach you in time, skipped it" (plan §4 step
        6). Doesn't require start() — it never touches the asyncio.Queue."""
        if client_msg_id:
            cached_turn_id = self.turn_id_for_client_msg_id(client_msg_id)
            if cached_turn_id is not None:
                return cached_turn_id

        turn_id = str(uuid.uuid4())
        now = time.time()
        turn = Turn(
            turn_id=turn_id,
            prompt=text,
            session_id=None,
            resume=False,
            source="system",
            client_msg_id=client_msg_id,
            status=TurnStatus.DONE,
            result=InvocationResult(
                exit_code=0, is_error=False, subtype="system", result_text=text, session_id=None
            ),
            created_at=now,
            # Created already-terminal, never passes through _drain -- set
            # this directly so _evict_old_turns' grace-period floor (EVICTION_GRACE_SECONDS)
            # applies to it too, same as any other DONE turn.
            finished_at=now,
        )
        self._turns[turn_id] = turn
        if client_msg_id:
            self._client_msg_index[client_msg_id] = (turn_id, now)
        # The notice is a bubble like any reply, and a push-woken client must
        # find it already there (the caller, cron_notify, pushes after this
        # returns). Synchronous on purpose: this method has no await and "does
        # not require start()", and one WAL insert is well under a millisecond.
        # That holds only because the one long writer, retention, deletes in
        # small batches (`transcript.prune`): a single big DELETE would hold the
        # write lock for seconds, and this is ON the event loop, so the server
        # would stall behind it. Keep prune batched if you touch either.
        # Never raises, so it cannot cost the caller its turn_id.
        transcript.append_assistant(
            turn_id=turn_id, text=text, source="system", status="done", now=now
        )
        return turn_id

    @property
    def depth(self) -> int:
        """Turns outstanding, including work held outside asyncio.Queue.

        The reauth-held head and the coalescing carry slot have both already
        been popped, so qsize() alone under-reports them — and `/health` reporting
        `queue_depth: 0, queue_paused: true` with real work waiting is exactly
        the wrong thing to show a monitor."""
        if self._queue is None:
            return 0
        return self._queue.qsize() + (1 if self._paused else 0) + (1 if self._carry else 0)

    @property
    def paused(self) -> bool:
        """True while the drain loop is holding the head-of-line turn for
        reauth_needed (plan §6a) — draining a whole night of crons against a
        dead token would produce a pile of ERROR turns that read as genuine
        refusals rather than an auth problem."""
        return self._paused

    @staticmethod
    def _reauth_needed() -> bool:
        """`auth_monitor.get_status()` guards the failures it anticipated
        (`JSONDecodeError`, `KeyError`, `OSError`) but not every shape a
        corrupt `.credentials.json` can take: a string `expiresAt` reaches
        `value / 1000` and a top-level JSON array reaches
        `raw["claudeAiOauth"]` — both `TypeError`. Uncaught, that propagates
        out of the drain loop and kills the consumer task, after which every
        turn sits PENDING forever with no ERROR and `queue_paused: false`, so
        /health looks healthy. A silent permanent wedge is far worse than the
        problem this gate exists to solve.

        Fails **open** on an unexpected error: treat auth as usable and let
        the invocation fail loudly on its own. Corrupt credentials will make
        `claude -p` fail anyway, producing a visible ERROR turn — the
        pre-existing behaviour. Failing closed would pause the queue forever
        on what may be a parsing bug, which is the wedge again by another
        route.
        """
        try:
            return auth_monitor.get_status().reauth_needed
        except Exception:
            logger.exception(
                "auth status check failed; treating auth as usable and "
                "letting the invocation surface the error"
            )
            return False

    async def _wait_for_auth(self) -> None:
        """Blocks until auth_monitor no longer reports reauth_needed, polling
        on a fixed interval rather than busy-looping. This runs *before* the
        turn is popped any further — the still-queued turns behind it simply
        sit in the asyncio.Queue untouched, so FIFO order falls out for free
        without needing to re-enqueue/reorder anything.

        A cheaper alternative would be an asyncio.Event set by auth_monitor
        the moment credentials refresh, but auth_monitor only reads a file on
        demand today — there's no watcher to set such an event from, and
        adding one is out of scope here. Polling get_status() (a small local
        file read + JSON parse) every auth_recheck_interval_seconds is cheap
        enough and simple enough for that gap.
        """
        self._paused = True
        # Log the transition, then stay quiet. An overnight expiry holds for
        # hours; warning on every poll would put ~1000 identical lines in the
        # log before Mike is even awake to read the first one.
        logger.warning(
            "queue paused: reauth_needed, holding head-of-line turn "
            "(rechecking every %ss)",
            settings.auth_recheck_interval_seconds,
        )
        # Fire once on entering the pause, not on every recheck poll — an
        # overnight expiry would otherwise produce ~1000 identical pushes by
        # the time Mike wakes up (same reasoning as the log heartbeat below).
        # Fire-and-forget (server/push.py) — this must not delay entering the
        # recheck-poll loop below on a slow/stalled APNs round-trip.
        push_broadcast_nowait(PushPayload(type=PushType.REAUTH_NEEDED, ref="reauth"))

        held_seconds = 0.0
        # An overnight expiry holds for hours; the heartbeat must be rare
        # enough to read. `held_seconds % 3600` alone isn't a once-per-hour
        # gate — with a recheck interval above 3600 the predicate is true on
        # every poll, inverting the throttle into exactly the log spam the
        # comment above promises to avoid.
        heartbeat_every = max(3600.0, float(settings.auth_recheck_interval_seconds))
        next_heartbeat = heartbeat_every
        resumed = False
        try:
            while self._reauth_needed():
                await asyncio.sleep(settings.auth_recheck_interval_seconds)
                held_seconds += settings.auth_recheck_interval_seconds
                if held_seconds >= next_heartbeat:
                    logger.warning(
                        "queue still paused for reauth after %.1fh (%s turns waiting)",
                        held_seconds / 3600,
                        self.depth,
                    )
                    next_heartbeat += heartbeat_every
            resumed = True
        finally:
            self._paused = False
            if resumed:
                logger.info("queue resuming after %.0fs paused for reauth", held_seconds)
            else:
                # Cancellation (shutdown) unwinds through here too. Logging
                # "resuming" would tell a post-mortem reader that auth
                # recovered at this timestamp when it did not.
                logger.info(
                    "queue pause abandoned after %.0fs without reauth (shutting down)",
                    held_seconds,
                )

    @staticmethod
    def _can_coalesce(turn: Turn) -> bool:
        """Fail-closed allowlist for turns safe to share one invocation."""
        if frozenset(Turn.__dataclass_fields__) != _COALESCING_CLASSIFIED_TURN_FIELDS:
            return False
        return (
            turn.source in active_turn.CHAT_SOURCES
            and not turn.attachment_paths
            and not turn.cancelled
            and not turn.require_session
            and not turn.retire_session_on_success
            and not turn.raw_prompt
        )

    async def _drain(self) -> None:
        if self._schedule_sync_task is not None:
            try:
                # stop() cancels this consumer. Shield the separately-owned
                # sync task so cancellation does not leave its to_thread work
                # running untracked while a later start launches another.
                await asyncio.shield(self._schedule_sync_task)
            except Exception:
                # sync_after_turn itself promises never to raise. Keep the
                # only consumer alive even if that contract regresses.
                logger.exception("startup schedule sync failed; continuing with the previous crontab")
        while True:
            if self._carry is not None:
                turn_id = self._carry
                self._carry = None
            else:
                turn_id = await self._queue.get()
            turn = self._turns[turn_id]

            if turn.cancelled:
                # docs/tasks/idle-compaction.md: an attended message arrived
                # while this turn (always a still-PENDING compaction — see
                # `_cancel_queued_compactions`) was sitting in the queue.
                # Skip it entirely: no invocation, no push, no attachment/
                # identity-commit bookkeeping, since none of that ran.
                turn.status = TurnStatus.CANCELLED
                turn.finished_at = time.time()
                # A no-op today — this branch `continue`s before invoking, so
                # nothing ever appended. Here so that "terminal implies no
                # activity" is true of every terminal path by inspection,
                # rather than true of two of them and coincidental in the
                # third.
                _clear_progress(turn)
                logger.info("turn %s cancelled before running (source=%s)", turn_id, turn.source)
                self._queue.task_done()
                self._evict_old_turns()
                self._prune_client_msg_index()
                continue

            if self._reauth_needed():
                # Hold the turn pending — do NOT invoke and do NOT mark it
                # ERROR. An errored turn here is exactly the misleading
                # outcome plan §6 warns about: it would read as a genuine
                # refusal rather than a dead token. Status stays PENDING.
                #
                # KNOWN GAP: this turn has already been popped off the
                # asyncio.Queue, so if the process stops while held, it is
                # orphaned — start() builds a fresh queue and never re-enqueues
                # it, leaving a PENDING turn in _turns that looks live but will
                # never run. A restart already drops in-memory queue state
                # generally (see deploy/README.md §8b on the cert-renewal
                # restart window), so this is the existing shape rather than a
                # new one — but pausing is exactly when a restart is most
                # likely, so it's more reachable here. Fix belongs with turn
                # retention/persistence, not with this auth gate
                # (docs/tasks/turn-retention.md — still open there).
                #
                # KNOWN GAP: nothing bounds accumulation while held. enqueue()
                # keeps accepting into an unbounded asyncio.Queue, so a 9-hour
                # expiry means every overnight cron and iOS message runs
                # back-to-back the moment Mike reauths — a burst of hours-late
                # replies to prompts whose context has moved on. Holding beats
                # a pile of misleading ERROR turns, but a staleness/age-out
                # policy is the real answer and it belongs with retention
                # (docs/tasks/turn-retention.md), which is where turn age
                # becomes a first-class concept. `_evict_old_turns()`'s count
                # cap doesn't touch this — it only evicts terminal turns.
                await self._wait_for_auth()

            run = [turn]
            if self._can_coalesce(turn):
                while len(run) < MAX_COALESCED_TURNS:
                    try:
                        candidate_id = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    candidate = self._turns[candidate_id]
                    if not (
                        self._can_coalesce(candidate)
                        and candidate.session_id == turn.session_id
                        and candidate.resume == turn.resume
                        and candidate.in_conversation == turn.in_conversation
                        and candidate.effort == turn.effort
                        # One note covers the whole merged prompt, so a typed
                        # message must not be read as spoken, or vice versa.
                        and candidate.spoken == turn.spoken
                    ):
                        self._carry = candidate_id
                        break
                    run.append(candidate)

            # The last placeholder sits at the bottom of the transcript, so it
            # owns the one result. Earlier turns become terminal immediately;
            # they have no invocation, push, telemetry, or attachment cleanup.
            head_created_at = run[0].created_at
            turn = run[-1]
            turn_id = turn.turn_id
            merged_at = time.time()
            for absorbed in run[:-1]:
                absorbed.status = TurnStatus.MERGED
                absorbed.merged_into = turn_id
                absorbed.finished_at = merged_at
                _clear_progress(absorbed)
                self._queue.task_done()
            if len(run) > 1:
                # The only trace a merge leaves. Without it, one reply to three
                # messages is indistinguishable in the journal from two turns
                # lost -- which is exactly the question asked after the first
                # burst on the phone (review, 2026-08-23).
                logger.info(
                    "coalesced %d turns into %s: %s",
                    len(run),
                    turn_id,
                    ", ".join(item.turn_id for item in run[:-1]),
                )

            turn.status = TurnStatus.RUNNING
            turn.started_at = time.time()

            # The conversation is looked up now, at run time. Piece 1's
            # rollover (docs/tasks/nightly-consolidation.md) needs nothing
            # more than this: a successful consolidation clears it
            # (session_state.retire), so whatever runs next -- the morning
            # brief, usually -- starts the new day fresh and becomes the
            # conversation, and the phone's next message resumes that.
            invoke_session_id, invoke_resume = turn.session_id, turn.resume
            if turn.in_conversation:
                invoke_session_id = self.conversation_session_id()
                invoke_resume = invoke_session_id is not None

            try:
                # Wall-clock deliberately matches Turn.created_at. This is a
                # queue stage, not an invocation stage: invoke.py measures
                # the latter with monotonic time for duration/gap telemetry.
                # Measured from the FIRST turn in a coalesced run, not the
                # survivor: the survivor is the newest and waited the least,
                # and wait time is the metric coalescing exists to improve --
                # so taking it from the survivor would flatter coalescing on
                # its own scoreboard (review, 2026-08-23).
                queue_wait_seconds = max(0.0, time.time() - head_created_at)
                result = await run_claude(
                    "\n\n".join(item.prompt for item in run),
                    session_id=invoke_session_id,
                    resume=invoke_resume,
                    source=turn.source,
                    turn_id=turn.turn_id,
                    queue_wait_seconds=queue_wait_seconds,
                    require_session=turn.require_session,
                    attachment_paths=turn.attachment_paths,
                    effort=turn.effort,
                    spoken=turn.spoken,
                    raw_prompt=turn.raw_prompt,
                    # Latest-wins into the single slot above. Runs in this same
                    # task between awaits on the CLI's stdout, so no locking:
                    # the poll handler that reads it is a different task, but
                    # an attribute assignment is atomic and a reader that
                    # catches the previous value simply shows a label one step
                    # behind for a few hundred milliseconds.
                    on_activity=_record_activity(turn),
                    on_text=_record_text(turn),
                    # Every turn the log records gets this, cron included --
                    # it is what moves an unwatched turn onto stream-json.
                    # Compaction gets none: it is maintenance, not activity,
                    # and stays on the format its probe was run against.
                    on_tool_call=(
                        _record_tool_call(turn) if turn.source != COMPACTION_SOURCE else None
                    ),
                )
                turn.result = result
                turn.status = TurnStatus.ERROR if result.is_error else TurnStatus.DONE
                if result.is_error:
                    # Usually None, leaving the client's generic text. Built
                    # from a fixed table in invoke.py, never from CLI output.
                    turn.error = result.error_reason
                # Recorded here, on completion, because that is the first moment
                # there is a trustworthy session id — and to disk, because the
                # gap between this line and the next turn (the 01:00
                # consolidate, the 08:00 brief) routinely contains a restart.
                # A dead-session self-heal lands in a new id; this is where
                # the conversation follows it.
                if turn.in_conversation and turn.status == TurnStatus.DONE and result.session_id:
                    session_state.note_conversation(result.session_id)
                # Fails safe by construction: ERROR'd or require_session-refused
                # turns never reach here with a usable result.session_id equal
                # to the session that was actually reviewed, and a turn whose
                # job was skipped (no attended session to attach to,
                # server/app.py's cron_message) was never enqueued at all, so
                # there is no Turn to reach this line. Never roll away a day
                # that was not consolidated (docs/tasks/nightly-consolidation.md
                # piece 1, "Design settled 2026-08-16").
                #
                # Retire BOTH ids, not just the one the call landed on.
                # `result.session_id` is normally identical to the one asked
                # for (measured: 5/5 resumed cron turns, 95/95 ios), but a
                # resumed turn CAN land in a new session. Retiring only the
                # landed id would leave the conversation pointing at the other
                # one, rollover would silently never fire, and the log line
                # would still read like success. idle_compaction also reads
                # the retired set, so the asked-for id belongs in it too.
                if turn.retire_session_on_success and turn.status == TurnStatus.DONE:
                    for session_id in (turn.session_id, result.session_id):
                        if session_id:
                            self._retire_session(session_id)
            except InvokeError as e:
                logger.error("invocation failed for turn %s: %s", turn_id, e)
                turn.error = str(e)
                turn.status = TurnStatus.ERROR
            except Exception:
                logger.exception("unexpected error draining turn %s", turn_id)
                turn.error = "internal error"
                turn.status = TurnStatus.ERROR
            finally:
                # `turn.status` is settled by every path above (try's success
                # line or either except clause), so this is a single place to
                # stamp completion rather than repeating it in three branches.
                turn.finished_at = time.time()
                # Same three branches, same reason: DONE and both ERROR paths
                # must all leave the status line empty. In the `finally` rather
                # than the success branch precisely because the ERROR paths are
                # the ones where a stale "Using a tool…" would otherwise sit
                # under an error message forever.
                _clear_progress(turn)
                self._queue.task_done()

            # Idle-compaction bookkeeping, deliberately placed AFTER the
            # try/except rather than inside the success branch, and keyed off
            # `turn.status`/`turn.result` (both settled by now) rather than a
            # local `result` that the exception paths never bind.
            #
            # Every terminal outcome is reported, ERROR included. The watcher's
            # own module comment has the full reasoning; the short version is
            # that all three of its inputs — the idle clock, the prefix, and
            # the hysteresis baseline — are reasons to compact, so hanging them
            # off `status == DONE` made a *failed* turn leave the watcher more
            # eager rather than less (review, 2026-08-18, findings 1-3).
            #
            # A CANCELLED compaction never reaches here: the cancel check at
            # the top of the loop `continue`s before invoking. That is correct
            # — it never ran, so it must not count as an attempt.
            result_session_id = turn.result.session_id if turn.result else None
            if turn.source in active_turn.CHAT_SOURCES:
                idle_compaction_watcher.note_attended_turn(
                    result_session_id,
                    turn.result.usage if turn.result else None,
                    context_tokens=turn.result.context_tokens if turn.result else None,
                )
            elif turn.source == COMPACTION_SOURCE:
                # `or turn.session_id` so a failure with no parsed result still
                # names the session it was trying to compact — that id is the
                # whole point of the call, and without it the attempt goes
                # unrecorded and the retry loop comes straight back.
                idle_compaction_watcher.note_compaction_attempted(
                    result_session_id or turn.session_id,
                    succeeded=turn.status == TurnStatus.DONE,
                    usage=turn.result.usage if turn.result else None,
                    turn_id=turn.turn_id,
                    error=turn.error,
                )

            # Usage readings for GET /v1/usage (docs/tasks/usage-visibility.md).
            # Beside the compaction bookkeeping and keyed off the same settled
            # `turn.result`, so a failed turn that still carried a reading
            # counts. Display-only: nothing from here reaches the watcher above.
            # Guarded because an exception escaping kills the one consumer task.
            try:
                usage_state.note_finished_turn(turn.source, turn.result)
            except Exception:
                logger.exception("usage state update failed for turn %s", turn_id)

            # The transcript row (docs/tasks/server-transcript.md), BEFORE the
            # push below: a push-woken client asks the server for rows after
            # its cursor, and must find the reply already there. Only `turn`,
            # the last of a coalesced run -- the earlier ones are MERGED and
            # the reply is theirs through `merged_into`. A CANCELLED compaction
            # never reaches here, and a ran one writes nothing, the same
            # exclusion as the push. In a thread because it is
            # file I/O; awaited, so the ordering above holds. Never raises
            # (a full disk is a missing row, not a wedged queue); guarded
            # anyway for the reason the commit below is.
            #
            # A cron run that replied SILENT_REPLY_TOKEN is not delivered at
            # all: no row, no push. Logged so that a job which has gone quiet
            # can be told apart from one that stopped firing.
            #
            # Nor is a plumbing run that finished, whatever it said: the tier
            # decides delivery, not the reply text, and housekeeping shouldn't
            # announce itself at 1am (docs/tasks/default-infrastructure.md).
            # Its text goes to the background store below, for the Activity
            # screen. An ERROR still pushes: a failed tidy means yesterday's
            # conversation carries into today, the one thing the user does
            # need to hear.
            silent = (
                turn.source == "cron"
                and turn.status == TurnStatus.DONE
                and (
                    turn.tier == "plumbing"
                    or is_silent_reply(turn.result.result_text if turn.result else None)
                )
            )
            if silent:
                logger.info("cron turn %s (%s) not delivered (silent or plumbing)", turn_id, turn.label)
            if turn.source != COMPACTION_SOURCE and not silent:
                try:
                    # ERROR carries `turn.error`, which is None for a CLI
                    # failure with no fixed reason; "" leaves the client's
                    # generic text. Never `result_text` there: on an error
                    # envelope that is CLI output, not a reply.
                    if turn.status == TurnStatus.DONE:
                        reply_text = (turn.result.result_text if turn.result else None) or ""
                    else:
                        reply_text = turn.error or ""
                    # Which user rows this reply resolves: the survivor's own, every
                    # turn merged into it, and any original row a retry's turn
                    # stands in for (`Turn.answers_turn_ids`). Only attended
                    # turns have user rows; a cron or system reply answers
                    # nothing a user said.
                    answers: list[str] = []
                    if turn.source in active_turn.CHAT_SOURCES:
                        for item in run:
                            for answered in item.answers_turn_ids or [item.turn_id]:
                                if answered not in answers:
                                    answers.append(answered)
                    await asyncio.to_thread(
                        transcript.append_assistant,
                        turn_id=turn_id,
                        text=reply_text,
                        source=turn.source,
                        status=turn.status.value,
                        answers=answers,
                        now=turn.finished_at,
                    )
                except Exception:
                    logger.exception("transcript write failed for turn %s", turn_id)

            # Fire-and-forget (server/push.py) — this is the single serialized
            # work queue (module docstring); a slow/stalled APNs round-trip
            # here must not delay picking up the next queued turn.
            #
            # Never for a compaction turn: it is unattended by construction,
            # Mike never asked for it, and the probe found its `result` is
            # empty — a push here would be a notification for nothing
            # (docs/tasks/idle-compaction.md). Nor for a silent cron run.
            if turn.source != COMPACTION_SOURCE and not silent:
                push_broadcast_nowait(PushPayload(type=PushType.TURN_READY, ref=turn_id))

            # Unconditional on turn.status, deliberately (docs/tasks/ios-image-input.md
            # acceptance #4): an ERRORed turn leaves the same bytes on disk as a
            # DONE one, and both must not linger. cleanup_attachment() never
            # raises, but guarded anyway rather than resting the only consumer
            # task in the process on that promise (same reasoning as the
            # identity-commit guard below).
            if turn.attachment_paths:
                try:
                    cleanup_attachment(turn_id)
                except Exception:
                    logger.exception("attachment cleanup failed for turn %s", turn_id)

            # The schedule store is model-written during this turn. Validate
            # and install it before identity git so pruning performed by a
            # fired one-off is captured in this same turn's commit. Like the
            # commit below, it is blocking harness I/O and stays off the event
            # loop; failure leaves the previous crontab installed and never
            # kills the queue.
            try:
                await asyncio.to_thread(sync_after_turn)
            except Exception:
                logger.exception("schedule sync failed for turn %s", turn_id)

            # Harness-owned identity git (server/identity_git.py). Deliberately
            # OUTSIDE the try/except above and unconditional on turn status: an
            # ERRORed turn can still have written files before it failed, and
            # those writes are exactly what wants recovering. Off the event loop
            # because it shells out to git.
            #
            # AFTER the broadcast, not before: the reply is already the user's
            # once the turn finishes, and commit_after_turn is up to five git
            # subprocesses at 30s each. In front of the push, a stalled git (disk
            # or NFS) would hold back the phone notification for a reply that is
            # already sitting complete in _turns. Durability is the slower,
            # lower-priority half of this pair.
            #
            # Guarded even though commit_after_turn documents that it never
            # raises: an exception escaping here kills the consumer task, and
            # THIS loop is the only one — every later turn would then sit PENDING
            # forever with no error to show for it. Not resting a whole queue on
            # another module keeping a promise.
            commit = None
            try:
                commit = await asyncio.to_thread(commit_after_turn, turn_id, turn.source)
            except Exception:
                logger.exception("identity git commit failed for turn %s", turn_id)

            # The activity log (docs/tasks/ux-lane.md §2a). After the commit
            # because the record names the files it staged. Compaction is
            # maintenance nobody asked for and is left out, same as it is left
            # out of the push and the transcript. append_record never raises; guarded anyway
            # for the reason the commit is.
            #
            # A cron reply that reached no transcript row (`silent` above) is
            # kept first in the background store, which the record then points
            # at, so the user can still read what the job said on the Activity
            # screen (docs/tasks/default-infrastructure.md "Seeing plumbing
            # output"). DONE only: an ERROR's text is CLI output, never a reply.
            if turn.source != COMPACTION_SOURCE:
                has_output = False
                if silent:
                    text = (turn.result.result_text if turn.result else None) or ""
                    if text.strip():
                        try:
                            has_output = await asyncio.to_thread(activity_log.append_output, turn, text)
                        except Exception:
                            logger.exception("background output write failed for turn %s", turn_id)
                try:
                    record = activity_log.build_record(
                        turn,
                        merged=[item.turn_id for item in run[:-1]],
                        commit=commit,
                        has_output=has_output,
                    )
                    await asyncio.to_thread(activity_log.append_record, record)
                except Exception:
                    logger.exception("activity log write failed for turn %s", turn_id)

            self._evict_old_turns()
            self._prune_client_msg_index()


work_queue = WorkQueue()

"""Idle-triggered `/compact` watcher (docs/tasks/idle-compaction.md).

Mike's shape: context climbs above a threshold, then he goes quiet, then a
few minutes later the harness compacts — so the compaction cost (tens of
seconds, a full-context read) is paid when nobody is waiting for a reply,
instead of mid-turn the way the CLI's own auto-compaction would pay it.

Structural template is `server/auth_monitor.py`'s `AuthWatcher`: a background
asyncio task, exception-safe poll loop, injectable `now` for testability. The
differences from that watcher are what this module is actually about:

- It decides to *enqueue a turn* (a `/compact` against the work queue) rather
  than just to log/push.
- Eligibility is three independent gates, all required (idle, above
  threshold, hysteresis) — see `_eligible`. Measured against Mike's real
  usage (the doc's `usage.jsonl` table), idle alone fires roughly eight times
  a day at his gap distribution; it is *permission* to compact, not a reason
  to.

State here is in-memory only, unlike `session_state.py`. That module needed
disk persistence because its facts have to survive an overnight restart
between the day's last message and the 01:00 consolidate cron. Idle-
compaction's state (the running prefix estimate, the hysteresis baseline) has
no equivalent hours-long gap to survive — a restart mid-day just means the
watcher re-learns the current prefix from the next attended turn, which is
cheap and not a correctness problem the way a skipped rollover was.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from server.config import settings
from server.log import get_logger

logger = get_logger(__name__)

# Distinct `source` for a compaction turn, deliberately NOT in
# `active_turn.ATTENDED_SOURCES` (server/queue.py imports this and confirms
# it at the enqueue call site). Compaction turns fire while Mike is away by
# construction — the whole point is paying the cost when nobody is waiting —
# so a permission prompt reaching the phone for one would be a 3am ring for a
# turn nobody asked for. Being unattended also keeps it off
# `session_state.note_conversation` and out of retirement (server/queue.py only
# calls those for conversation turns; compaction names its session explicitly).
SOURCE = "compaction"

# The probe (docs/tasks/idle-compaction.md, "Probe results") confirmed bare
# `/compact` as the ENTIRE prompt is what triggers real compaction in
# headless mode — nothing before it, nothing after. That is why this turn is
# always enqueued with raw_prompt=True (server/invoke.py): prepend_turn_context
# would put its block in FRONT of this string, and the attachment line has
# the same problem.
COMPACT_PROMPT = "/compact"

# usage keys invoke.py's docstring warns never to assume are present — this
# is the "sum defensively" reading of that warning.
_PREFIX_USAGE_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens")


def _sum_prefix_tokens(usage: dict | None) -> int | None:
    """Best-effort prefix size from the CLI's own counters.

    Returns None (not 0) when nothing usable was reported — an error
    envelope, an older CLI, a turn with no usage block at all — so a caller
    can tell "no data yet" apart from "a real turn measured a small prefix"
    instead of treating the former as evidence the context is empty.
    """
    if not isinstance(usage, dict) or not usage:
        return None
    total = 0
    found = False
    for key in _PREFIX_USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
            found = True
    return total if found else None


def _prefix_reading(usage: dict | None, context_tokens: int | None) -> int | None:
    """The size the gates run on: the true size when the turn reported one,
    else the summed counters.

    Until 2026-10-05 the gates ran on the summed counters alone, which read
    size x loop depth (see note_attended_turn), and that upward bias was kept
    on purpose: with no true number available, it was the only way to be sure
    a large session was never read as small ("Read 2026-08-23" in
    docs/tasks/idle-compaction.md). Once invoke.py measured the true size
    (usage-visibility, 2026-10-05) the bias stopped protecting anything and only
    cost: on 2026-10-05 a fresh 38,406-token session made 5 calls in one turn,
    read ~190K, and was compacted five minutes later -- below the 50K floor,
    reclaiming nothing, and hiding how a longer verbatim conversation would
    actually cost.

    The fallback keeps the old protective behaviour for a turn with no true
    reading (a CLI that renames the stream fields, a plain-json turn), so a
    parsing regression degrades to over-compacting rather than to never
    compacting.
    """
    if context_tokens is not None and not isinstance(context_tokens, bool) and context_tokens > 0:
        return int(context_tokens)
    return _sum_prefix_tokens(usage)


@dataclass
class _SessionHysteresis:
    # The prefix size established as "as small as compaction can make this
    # session," as of its most recent compaction. None until a compaction has
    # happened and a baseline has been established.
    baseline: int | None = None
    # True from the moment a compaction of this session completes until the
    # next attended turn completes. The compaction call's own `usage` reflects
    # the PRE-compaction read (the probe confirmed this), so the true
    # post-compaction prefix is unknowable until an attended turn actually
    # runs against the compacted session and reports what it saw. While this
    # is True, no further compaction of this session is eligible — firing
    # again before the baseline is known would be compacting a session whose
    # current size is unmeasured, exactly the "repeated cost, no benefit"
    # failure hysteresis exists to prevent.
    awaiting_baseline: bool = False
    # The pre-compaction size, retained until the next attended turn reports
    # the post-compaction prefix so the operator can see the observed
    # reduction. Sourced from the last attended turn when the compaction
    # call's own counters are absent or zero, which 2026-08-21 showed is the
    # normal case, not the exceptional one — see note_compaction_attempted.
    # None for a failed attempt, or when neither source has a usable number;
    # neither case supports a reduction claim.
    pre_compaction_tokens: int | None = None


class IdleCompactionWatcher:
    """Owns the running idea of "how big is the phone's session, and when did
    it last hear from Mike" and turns that into an occasional `/compact`
    enqueue.

    `work_queue` is passed into every method rather than imported at module
    scope, both for testability (tests construct their own `WorkQueue()`
    instances, per the existing convention in tests/test_queue.py) and to
    keep this module free of a module-level import of server.queue — that
    import runs the other direction (server/queue.py imports this module for
    the SOURCE constant and the two `note_*` hooks below), and a module-level
    import back here would be a cycle. `_run`'s import of the singleton is
    the one place production wiring actually needs it, and it happens lazily
    inside the loop.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        # Updated only by note_attended_turn_done, i.e. only by a completed
        # chat turn (active_turn.CHAT_SOURCES). A cron turn must not touch any of this three
        # — resetting the idle clock off cron activity would suppress
        # compaction exactly when the overnight gap made it cheapest (doc).
        self._last_attended_turn_at: float | None = None
        self._last_attended_session_id: str | None = None
        self._last_attended_prefix_tokens: int | None = None
        self._hysteresis: dict[str, _SessionHysteresis] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            # Say so on startup. Without this, "the watcher is running and has
            # declined every poll" and "the watcher never started" look
            # identical from outside — the module is otherwise silent until it
            # actually compacts something, and on 2026-08-19 that cost a while
            # to tell apart. Logging the gates too, because the kill switch and
            # the threshold are the two things you want to know first.
            logger.info(
                "idle-compaction watcher started (enabled=%s, idle=%ss, threshold=%d tokens)",
                settings.idle_compaction_enabled,
                settings.idle_compaction_idle_seconds,
                settings.idle_compaction_threshold_tokens,
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        # Lazy, deliberately — see the class docstring on why this can't be a
        # module-level import.
        from server.queue import work_queue

        while True:
            # Same reasoning as AuthWatcher._run: a watcher that dies silently
            # on one bad poll is worse than no watcher — it looks fine right
            # up until the day nothing ever compacts.
            try:
                self.maybe_enqueue(work_queue)
            except Exception:
                logger.exception("idle-compaction poll failed; continuing")
            await asyncio.sleep(settings.idle_compaction_poll_interval_seconds)

    # --- bookkeeping hooks, called from queue._drain on every TERMINAL turn ---
    #
    # Both hooks fire on failure as well as success, which is the single
    # property the whole module's safety rests on (review, 2026-08-18 —
    # findings 1-3 were three symptoms of one root cause). An earlier revision
    # hung all three pieces of state off the `status == DONE` branch, so a
    # failed turn left the watcher holding a *confident and wrong* picture
    # rather than an uncertain one: it still believed Mike was idle, still
    # believed the prefix was large, and still believed the session had never
    # been compacted. Every one of those is a reason to act, so failure made
    # the watcher MORE eager rather than less.
    #
    # The rule that replaces it: an outcome the watcher cannot interpret must
    # move it toward "I don't know" (which declines to compact), never leave
    # it on a stale belief that says compact.

    def note_attended_turn(
        self,
        session_id: str | None,
        usage: dict | None,
        *,
        context_tokens: int | None = None,
        now: float | None = None,
    ) -> None:
        """Called for EVERY terminal attended (`active_turn.CHAT_SOURCES`) turn — DONE
        or ERROR, session id or not.

        The idle clock is reset unconditionally, before any of the early
        returns below. It answers "is Mike at his phone", and a turn that
        errored answers that just as well as one that succeeded: he is right
        there, most likely about to retry. Gating the reset on success meant a
        failed message left the clock reading from his last *successful* turn,
        so a compaction could fire while he was actively typing (finding 3).

        Also the ONLY place a hysteresis baseline gets established — see
        `_SessionHysteresis.awaiting_baseline`.

        `context_tokens` is the turn's TRUE size (invoke.py: the last top-level
        model call's prefix) and is what the gates run on. `usage`, the
        envelope's summed counters, is only the fallback for a turn whose
        stream carried no usable assistant usage — see `_prefix_reading`.
        """
        now = now if now is not None else time.monotonic()
        self._last_attended_turn_at = now

        if not session_id:
            return
        prefix = _prefix_reading(usage, context_tokens)

        self._last_attended_session_id = session_id
        # Unconditional, INCLUDING when prefix is None. The session id and the
        # prefix are one fact together — "session S is this big" — so updating
        # the id while keeping the previous session's number silently pairs a
        # new session with an old size. After a nightly rollover that reads as
        # a brand-new session already at 400K, and fires a compaction that
        # reclaims nothing (finding 2; the probe recorded exactly this waste as
        # preTokens: 657). Clearing to None costs one skipped cycle, because
        # `_eligible` declines on "no prefix data recorded yet".
        self._last_attended_prefix_tokens = prefix

        hyst = self._hysteresis.setdefault(session_id, _SessionHysteresis())
        # ZERO IS NOT A MEASUREMENT, and everything below deals in baselines,
        # so normalize it away here. `_sum_prefix_tokens` sets `found` on key
        # PRESENCE rather than on a nonzero value, so usage that carries the
        # prefix keys all zeroed returns 0 rather than None — not a hypothetical
        # (2026-08-21 in production), which is why note_compaction_attempted
        # already normalizes the identical case with the identical reasoning.
        #
        # Letting a 0 through here is worse than letting it through there. It
        # would record "this session compacts down to nothing" as the floor,
        # and _eligible's growth gate reads the baseline literally: with
        # baseline 0 it asks only for `idle_compaction_growth_tokens` of
        # apparent growth, so every idle stretch past the floor would compact,
        # for the rest of the session. `prefix` itself stays as measured — a 0
        # there makes _eligible decline on "below floor", which is the correct
        # response to not knowing.
        baseline_prefix = prefix or None
        # A LATER, SMALLER reading lowers the baseline. The baseline means "as
        # small as compaction can make this session", and nothing that happens
        # between compactions makes a session genuinely smaller *by more than
        # compaction already did* — so the smallest reading seen since the last
        # compaction is the best estimate of that floor, and anything larger
        # was measuring something else.
        #
        # Note what is NOT claimed: that only this module can shrink a session.
        # The CLI's own auto-compaction is still live — the 2026-08-16 decision
        # declined to OVERRIDE its threshold (docs/tasks/idle-compaction.md),
        # not to disable it, and invoke.py forces only
        # CLAUDE_CODE_DISABLE_AUTO_MEMORY. So a drop can be a real CLI-side
        # compaction rather than an inflated earlier reading. Taking the
        # minimum is the right response either way, which is why this does not
        # try to tell them apart; the log line below is careful not to assert a
        # cause it cannot know.
        #
        # What it is measuring is loop depth. `_sum_prefix_tokens` sums
        # `cache_read`, which the CLI reports as a total across the
        # invocation's internal agentic loop, NOT as one call's prefix
        # (docs/tasks/usage-telemetry.md, read 2026-08-10: individual turns
        # recorded 206K/264K/344K against a 200K window). So a reading is
        # roughly size × depth: always >= the true size, never below.
        #
        # That bias is harmless everywhere else in this module and dangerous
        # exactly here. Everywhere else it makes the watcher compact when it
        # need not have, which costs one invocation while Mike is idle. Here it
        # gets FROZEN into the growth gate: one deep turn right after a
        # compaction recorded a baseline of 142,028 on 2026-08-23, and the gate
        # then wanted 142,028 + `idle_compaction_growth_tokens` before it would
        # compact again — suppressing compaction for the rest of that session,
        # which is the expensive cold start this whole module exists to
        # prevent. It went unnoticed only because a restart wiped the map.
        #
        # Taking the minimum is robust to the inflation instead of trying to
        # correct it, and it errs toward compacting, which is the cheap
        # direction. It still matters after 2026-10-05, when the gates moved
        # to the true size (`_prefix_reading`): a turn that falls back to the
        # summed counters is inflated exactly as above.
        if (
            not hyst.awaiting_baseline
            and baseline_prefix is not None
            and hyst.baseline is not None
            and baseline_prefix < hyst.baseline
        ):
            logger.info(
                "idle-compaction: lowering hysteresis baseline for session %s "
                "from %d to %d tokens (a smaller reading: the earlier one was "
                "loop-inflated, or the CLI compacted on its own)",
                session_id,
                hyst.baseline,
                baseline_prefix,
            )
            hyst.baseline = baseline_prefix
        # Leaving `awaiting_baseline` set when the reading was unusable is the
        # module's standing rule that an outcome it cannot interpret must move
        # it toward "I don't know" — the next attended turn measures it instead.
        if hyst.awaiting_baseline and baseline_prefix is not None:
            hyst.baseline = baseline_prefix
            hyst.awaiting_baseline = False
            pre_compaction_tokens = hyst.pre_compaction_tokens
            hyst.pre_compaction_tokens = None
            if pre_compaction_tokens is not None and pre_compaction_tokens > 0:
                reduction = pre_compaction_tokens - prefix
                reduction_percent = reduction / pre_compaction_tokens * 100
                if reduction >= 0:
                    logger.info(
                        "idle-compaction: post-compaction prefix for session %s observed at "
                        "%d tokens (reduced by %d tokens, %.1f%%, from %d)",
                        session_id,
                        prefix,
                        reduction,
                        reduction_percent,
                        pre_compaction_tokens,
                    )
                else:
                    logger.info(
                        "idle-compaction: post-compaction prefix for session %s observed at "
                        "%d tokens (grew by %d tokens, %.1f%%, from %d)",
                        session_id,
                        prefix,
                        -reduction,
                        -reduction_percent,
                        pre_compaction_tokens,
                    )
            else:
                logger.info(
                    "idle-compaction: hysteresis baseline for session %s established at %d tokens",
                    session_id,
                    prefix,
                )

    def note_compaction_attempted(
        self,
        session_id: str | None,
        *,
        succeeded: bool,
        usage: dict | None,
        turn_id: str,
        error: str | None = None,
    ) -> None:
        """Called for EVERY terminal compaction turn, succeeded or failed.

        Success: whatever `usage` reports here, it is not a POST-compaction
        size — the probe found it reflects the pre-compaction read, and
        2026-08-21 found it can be all zeros instead. Either way there is
        nothing usable to record as the new size, so mark the session as
        awaiting a baseline and let the next attended turn measure it. The
        block below picks the best available *pre*-compaction number so the
        reduction can be reported once that baseline lands.

        Failure: the same marking, deliberately, and it is what stops a failed
        compaction from retrying forever (finding 1). Nothing about a failed
        attempt changes the gates — the idle clock, the prefix and the
        hysteresis baseline are all exactly as they were — so a hook that
        fired only on success left the watcher eligible again on the very next
        poll. With `require_session=True` disabling invoke.py's dead-session
        self-heal, a stale session id fails *permanently* rather than
        recovering, so that was not a transient: an idle overnight session
        would issue a full-context `claude -p` every poll interval until
        morning, each one billed.

        Marking on failure turns that into a bounded retry with a natural
        backoff: nothing happens until the next attended turn sets a baseline,
        and then the growth gate requires the session to grow by
        `idle_compaction_growth_tokens` before trying again. A genuinely
        transient failure costs a delayed compaction; a permanent one costs
        one attempt per that much growth instead of one every 30 seconds.
        """
        if not session_id:
            return
        hyst = self._hysteresis.setdefault(session_id, _SessionHysteresis())
        hyst.baseline = None
        hyst.awaiting_baseline = True
        pre_compaction_tokens = _sum_prefix_tokens(usage) if succeeded else None
        measured_from = "compaction call"
        # The compaction call's own usage is *supposed* to carry the
        # PRE-compaction read (the probe finding this design rests on). In
        # production on 2026-08-21 it did not: the prefix keys came back
        # present and all ZERO, so _sum_prefix_tokens returned 0 rather than
        # None, and the reduction log in note_attended_turn — which guards on
        # `> 0` — fell through and printed nothing. The compaction itself had
        # worked (142967 -> 33770 tokens, 76%); only the evidence for it was
        # lost, which is the entire point of this instrumentation.
        #
        # `_last_attended_prefix_tokens` is the better source regardless: it
        # was measured by a real attended turn, it is what `_eligible` gated
        # on, and it is the number the enqueue line already printed. Guard it
        # on the session id — pairing one session's size with another's is the
        # exact mistake note_attended_turn's unconditional-clear comment
        # exists to prevent, and it would fabricate a reduction here.
        if (
            succeeded
            and not pre_compaction_tokens
            and session_id == self._last_attended_session_id
            and self._last_attended_prefix_tokens
        ):
            pre_compaction_tokens = self._last_attended_prefix_tokens
            measured_from = "last attended turn"
        # Normalize a surviving 0 to None: "prefix unavailable" is true, while
        # "prefix=0 tokens" reads as a measurement and is the line that sent
        # Mike looking for a reduction that was never going to print.
        if not pre_compaction_tokens:
            pre_compaction_tokens = None
        hyst.pre_compaction_tokens = pre_compaction_tokens
        if succeeded:
            if pre_compaction_tokens is None:
                logger.info(
                    "idle-compaction: succeeded for session %s (turn %s, pre-compaction "
                    "prefix unavailable); awaiting post-compaction measurement",
                    session_id,
                    turn_id,
                )
            else:
                logger.info(
                    "idle-compaction: succeeded for session %s (turn %s, pre-compaction "
                    "prefix=%d tokens via %s); awaiting post-compaction measurement",
                    session_id,
                    turn_id,
                    pre_compaction_tokens,
                    measured_from,
                )
        else:
            logger.warning(
                "idle-compaction: failed for session %s (turn %s, error=%s); "
                "awaiting hysteresis baseline before retry",
                session_id,
                turn_id,
                error or "unknown error",
            )

    # --- gating ---------------------------------------------------------------

    def _eligible(self, work_queue, *, now: float) -> tuple[bool, str]:
        """Returns (eligible, session_id_or_reason): on True the second
        element is the session id to compact; on False it's a short reason
        string, useful for logging and for tests asserting on why a poll
        declined.
        """
        if not settings.idle_compaction_enabled:
            return False, "disabled"

        session_id = self._last_attended_session_id
        if session_id is None:
            return False, "no attended session"

        # Never fire while the queue is paused for reauth_needed — a
        # compaction enqueued during an auth pause would sit at the head of
        # the queue in front of Mike's next real message the moment he
        # reauths.
        if work_queue.paused:
            return False, "queue paused for reauth"

        if work_queue.is_session_retired(session_id):
            return False, "session retired"

        if self._last_attended_turn_at is None:
            return False, "no attended turn recorded yet"
        idle_for = now - self._last_attended_turn_at
        if idle_for < settings.idle_compaction_idle_seconds:
            return False, "not idle long enough"

        prefix = self._last_attended_prefix_tokens
        if prefix is None:
            return False, "no prefix data recorded yet"
        if prefix < settings.idle_compaction_floor_tokens:
            return False, "below floor"
        if prefix < settings.idle_compaction_threshold_tokens:
            return False, "below threshold"

        hyst = self._hysteresis.get(session_id, _SessionHysteresis())
        if hyst.awaiting_baseline:
            return False, "awaiting hysteresis baseline"
        if hyst.baseline is not None and prefix < hyst.baseline + settings.idle_compaction_growth_tokens:
            return False, "hysteresis growth not met"

        return True, session_id

    def maybe_enqueue(self, work_queue, *, now: float | None = None) -> str | None:
        """Run the gates and, if all pass, enqueue exactly one `/compact`
        turn. Returns the turn_id, or None if nothing was enqueued.
        """
        now = now if now is not None else time.monotonic()
        eligible, info = self._eligible(work_queue, now=now)
        if not eligible:
            return None
        session_id = info

        # Don't pile a second compaction on top of one already outstanding —
        # the eligibility gates above are re-evaluated every poll interval,
        # and a compaction call can run for tens of seconds.
        if work_queue.has_pending_turn(SOURCE):
            return None

        turn_id = work_queue.enqueue(
            COMPACT_PROMPT,
            session_id=session_id,
            resume=True,
            source=SOURCE,
            # This job's entire safety argument is "never run in a fresh,
            # contextless session" — a /compact issued into a session with no
            # history to summarize is pointless at best. require_session=True
            # turns off invoke.py's dead-session self-heal, so a session that
            # has died since we last heard from it errors visibly instead of
            # silently spinning up a stray session to run /compact in.
            require_session=True,
            raw_prompt=True,
        )
        logger.info(
            "idle-compaction: enqueued /compact for session %s (turn %s, prefix >= %d tokens)",
            session_id,
            turn_id,
            self._last_attended_prefix_tokens or 0,
        )
        return turn_id


idle_compaction_watcher = IdleCompactionWatcher()

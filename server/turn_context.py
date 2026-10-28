"""The small, deliberately-volatile context block prepended to each turn.

Deliberately NOT part of server/identity.py. That module's output is the
`--append-system-prompt` cache prefix, and its docstring is explicit that
changing it "invalidates the block and all conversation history behind it" —
so a clock in there would invalidate a ~58K block on every single invocation
and undo the whole point of cache-friendly-identity-injection.md. This string
rides on the *prompt*, which is positional and last, where per-turn variation
costs nothing.

What goes here is only what nothing else supplies:

- **Not the date.** The CLI already injects `Today's date is YYYY-MM-DD` into
  its own system prompt (confirmed on the box at CLI 2.1.223, 2026-08-07).
- **Time of day and timezone.** Neither appears anywhere in the CLI's
  environment block, and the timezone is the one the harness is uniquely able
  to answer: the box is pinned to America/Edmonton and would keep saying so
  while Mike is in Lisbon. Only his phone knows where he is, so the phone
  reports it and this reads it back.

Third, and for the same reason: whether the schedule store still agrees with
where he is. Marlow can see his zone on every turn but cannot see
`cron/schedules.json` without spending a tool call reading it, so it can only
notice a stale store on turns where it happens to be looking — which are the
turns that least need the reminder. The harness holds both halves already, so
it says the sentence, and only when the two disagree.

Fourth, also only the phone's to know: where they are, when they have turned
that on (`server/location.py`, docs/tasks/message-location.md).

The date the CLI injects is the *box's* date, which is a day wrong whenever
Mike's zone has crossed midnight and the box's hasn't (or vice versa). So when
a phone-reported zone exists this block states his full local date as well —
the CLI's line stays visible, and the two disagreeing is the point rather than
a bug: this one names whose date it is.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from server import active_turn, ideas
from server.config import settings
from server.location import MAX_AGE_SECONDS, location_store
from server.log import get_logger
from server.preferences import preferences_store
from server.pairing import pairing_store

logger = get_logger(__name__)

_FORMAT = "%A %Y-%m-%d %H:%M %Z"

# Mike's number (2026-08-07): below this, "time passed" isn't worth saying.
_GAP_THRESHOLD_SECONDS = 50 * 60

# When each conversation last ran, keyed by session id. Module state, not
# persisted: the work queue serializes every invocation through this process,
# so it is accurate while the server is up, and a restart merely costs the
# first turn of each session its gap sentence.
#
# Keyed rather than global because "the previous message" is a claim about ONE
# conversation. A global counter mixed cron in with iOS: cron jobs then ran in
# a fresh CLI session with no history at all, so a global gap both asserted a
# previous message that never existed in that session AND reset the clock,
# swallowing the gap on Mike's next real message. Found in review, 2026-08-07.
# Jobs share the conversation since 2026-09-25, but the keying still holds
# for the turns that do start fresh (the morning after a rollover).
_last_turn_at: dict[str, float] = {}

# Bounds the dict against a long-lived process accumulating dead sessions.
_SESSION_TTL_SECONDS = 7 * 24 * 3600


def _reset_gap_for_tests() -> None:
    _last_turn_at.clear()


def _round_half_up(value: float) -> int:
    """`round()` is banker's rounding: round(2.5) is 2 and round(3.5) is 4, so
    consecutive half-hours describe themselves inconsistently. This is a
    quantity a human reads, so round the way a human expects."""
    return int(value + 0.5)


def _describe_gap(seconds: float) -> str:
    """Human units. The point is a correct sense of elapsed time with no
    arithmetic, so the boundaries are chosen not to overstate: the hours branch
    starts at a full 2 hours, because `round(1.5) -> 2` used to report a
    90-minute gap as "2 hours" — a third too much, in the bucket immediately
    above the 50-minute threshold where most gaps land (review, 2026-08-07).
    """
    hours = seconds / 3600
    if hours >= 48:
        return f"{_round_half_up(hours / 24)} days"
    if hours >= 2:
        return f"{_round_half_up(hours)} hours"
    return f"{_round_half_up(seconds / 60)} minutes"


def _gap_sentence(session_id: str | None, resume: bool, now_monotonic: float) -> str:
    """"3 hours since the previous message", when that is worth saying.

    Mike's observation: a running conversation gives Marlow no sense that time
    has passed between turns, so it answers a 9am question at 6pm as though no
    time had passed. Because every turn carries a timestamp, the history
    technically already contains this — but it is arithmetic across turns, and
    the whole lesson of loader-identity-injection.md is that work the model
    *could* do and isn't required to do loses to answer-momentum. Stating it
    outright is a handful of tokens and needs no inference.

    Deliberately only past a threshold: on a back-and-forth exchange the gap is
    noise, and a line that appears every single turn stops being read.

    Silent for any turn that is not resuming an existing session, and for any
    turn with no session yet — a fresh session (resume=False) has no previous
    message in it to have elapsed since, whatever the source. Same for the
    first turn of a new conversation.

    This used to key off `source == "cron"`, on the premise that a cron job
    always starts a fresh session — true when this was written, but the
    nightly consolidation job (docs/tasks/nightly-consolidation.md piece 2)
    broke the premise by resuming a real prior session while still carrying
    `source="cron"`, which silently suppressed the sentence on exactly the
    turn most likely to need it (a multi-hour gap since the day's last
    message). `resume` is what the CLI invocation actually keys its own
    session-freshness decision on (server/invoke.py), so this now asks the
    same question the invocation already answered instead of re-deriving a
    weaker proxy from `source` (review finding, corrected 2026-08-16).

    Mutates `_last_turn_at`, so the caller must only invoke it once it is
    committed to using the result: a consumed gap cannot be recovered.
    """
    if not resume or not session_id:
        return ""

    if len(_last_turn_at) > 1:
        for stale in [
            sid for sid, at in _last_turn_at.items()
            if now_monotonic - at > _SESSION_TTL_SECONDS
        ]:
            del _last_turn_at[stale]

    previous = _last_turn_at.get(session_id)
    _last_turn_at[session_id] = now_monotonic
    if previous is None:
        return ""
    elapsed = now_monotonic - previous
    if elapsed < _GAP_THRESHOLD_SECONDS:
        return ""
    return f" {_describe_gap(elapsed)} have passed since the previous message."


def is_valid_timezone(name: str) -> bool:
    """Is this a resolvable IANA zone name?

    The value arrives from a paired device, so it is not arbitrary attacker
    input, but it does end up inside the prompt — and a typo would otherwise be
    stored once and quietly mis-state Mike's local time on every turn until
    someone noticed.
    """
    if not name or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


# Every sentence here says "the user", never a name. The name used to come
# from MARLOW_USER_NAME in the install's systemd drop-in, a second copy of a
# fact the assistant owns in USER.md. Onboarding asks what they want to be
# called and writes USER.md, which the agent can edit and the drop-in it
# cannot, so the two split on the first nickname
# (docs/tasks/install-review-leftovers.md item 1, closed 2026-10-02). The model
# reads USER.md every turn and knows who "the user" is. Never a pronoun guessed
# from a name either: "they" carries the rest of each sentence.


def _schedule_zone() -> str | None:
    """The timezone `cron/schedules.json` is written in, or None.

    Read straight from the file rather than through `server.schedules.load()`:
    that module imports `is_valid_timezone` from this one, so importing it back
    would be a cycle, and a full validation pass is the wrong price to pay on
    every turn for a hint. This IS only a hint — a store too broken to parse is
    `load()`'s job to reject loudly, and this stays quiet about it rather than
    reporting the same failure twice a turn.

    The path comes from `settings`, which is the ONE definition both this and
    `schedules.SCHEDULES_PATH` resolve — see that property for why re-spelling
    it here would be a silent failure rather than a duplicate.
    """
    try:
        raw = json.loads(settings.schedules_path.read_text())
        zone = raw.get("timezone")
    except Exception:
        return None
    return zone if isinstance(zone, str) and is_valid_timezone(zone) else None


def _rejection_sentence() -> str:
    """Tell Marlow its schedule store is being refused, if it is.

    The store is the ONE piece of harness state Marlow both owns and cannot
    observe the effect of. `schedules.sync_after_turn` treats a refused store
    exactly like an absent one -- it returns, leaving the previous crontab in
    place -- so a bad edit produces a successful `Write`, an ERROR in the
    journal, and no other signal anywhere. Found 2026-09-06: Marlow scheduled a
    one-off job, the store was refused on every turn for half an hour, and
    nothing would have fired. It had no way to know; the file it wrote is
    exactly the file it reads back.

    So this is the declarative-schedules failure (2026-08-30) in its other
    direction. That one was a
    capability the harness gained and never announced; this is a capability the
    harness silently withdrew. Both are the harness knowing something the model
    needs and not saying it.

    Imported inside the function because server.schedules imports
    is_valid_timezone from this module -- the same circularity that makes
    _schedule_zone read the file by hand. This one needs the real validator, not
    a re-derivation of it, so a deferred import is the honest way round.
    """
    try:
        from server import schedules

        reason = schedules.rejection_reason(settings.schedules_path)
    except Exception:
        return ""
    if not reason:
        return ""
    # Says what is broken, what it costs, and what a repair looks like. Both of
    # the latter two are load-bearing and were learned by watching this fail.
    #
    # The cost clause: without it a model may reasonably read this as advisory
    # and finish its turn, leaving the crontab frozen.
    #
    # The remedy clause: dry-run 2026-09-06, handing Marlow this sentence and
    # nothing else. It diagnosed and repaired the store unaided -- but chose
    # `cron: "0 9 9 9 *"` plus a note to itself to delete the entry after it
    # fired, which is a yearless line that fires again next September and a
    # prune no turn-scoped agent ever performs (the tendency identity/CLAUDE.md
    # already records about memory files). So the sentence names the trap.
    #
    # It deliberately does NOT enumerate which payloads `at` accepts. It did
    # briefly, saying "notify or prompt" -- correct for about an hour, until
    # `at`+`job` shipped in the same change. A warning that lists a schema is a
    # second copy of the validator, and this one was already stale before it
    # deployed. Name the SHAPE that prunes; the validator names the rest.
    return (
        f" WARNING: cron/schedules.json is currently INVALID and the server is"
        f" refusing it -- {reason}. No schedule change has taken effect since it"
        f" broke, and the installed crontab is frozen at its last good version,"
        f" so anything scheduled after that point will NOT fire. Fix the store"
        f" before doing anything else with schedules. A one-off wants `at`, which"
        f" prunes itself once it fires; do NOT reach for a dated `cron`"
        f" expression instead -- cron has no year field, so it fires again next"
        f" year and nothing will remember to remove it."
    )


def _ideas_rejection_sentence() -> str:
    """Tell Marlow its ideas store is being refused, if it is.

    Same failure shape as `_rejection_sentence`: the write succeeds, the
    server reads the store as empty, and the only symptom is a blank ideas
    panel on the phone that nobody connects to the file."""
    try:
        reason = ideas.rejection_reason()
    except Exception:
        return ""
    if not reason:
        return ""
    # The one ERROR for a refused store: the phone's reads are quiet.
    logger.error("ideas store rejected: %s", reason)
    return (
        f" WARNING: ideas/ideas.json is currently INVALID and the server is"
        f" refusing it -- {reason}. The phone shows no ideas until it is fixed."
    )


def _describe_offset(seconds: float) -> str:
    minutes = int(abs(seconds)) // 60
    hours, remainder = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if remainder:
        parts.append(f"{remainder} minute" + ("s" if remainder != 1 else ""))
    return " ".join(parts)


def _drift_sentence(
    moment: datetime, local: datetime, local_zone: str, attended: bool
) -> str:
    """"His store still says X while he is in Y" — plus the cost and the repair.

    Compares OFFSETS at this moment, not zone names. America/Edmonton and
    America/Denver are different strings and identical schedules; saying
    anything there would be a nag with no correction to make. What matters is
    whether a time written in the store lands at that time on Mike's clock.

    Only recurring entries drift. One-offs are pinned to an instant when the
    store is synced (server/schedules.pin_one_offs), so a zone change cannot
    move them — deliberately, and the sentence says so by naming only jobs.

    **Why this reads the way it does.** From 2026-08-31 to 2026-09-10 it was a
    bare statement of fact: "his store is set to X, so jobs fire N earlier."
    True, cheap, and completely inert — Mike's timezone changed on Sep 8,
    the sentence appeared on every turn for days, and Marlow never once offered
    to fix it (docs/tasks/dated-plans-go-stale.md). A status note competes with
    whatever Mike actually asked about, and loses.

    `_rejection_sentence` below is the same module's worked example of a notice
    that does land: fact, then **cost**, then **what a repair looks like**, then
    **the trap**. Handed that sentence and nothing else, Marlow diagnosed and
    repaired a broken store unaided. This one now has the same four parts.

    The trap named here is the wrong repair, and it is the obvious one: editing
    each entry's time by the offset. That double-applies — the times in the
    store are already written in Mike's terms, so the renderer would shift
    times that were never wrong, and the next zone change breaks it again in a
    way no single field can undo. The store's `timezone` is the one field that
    means "whose clock these are written on"; it is the only thing to change.

    And it asks rather than acts, because when Mike's jobs run is his call. A
    silent retime is a side effect he did not request, on a surface whose whole
    failure history is things moving without him seeing it.

    **Which is exactly why the ask is gated on `attended`** (/code-review
    medium, 2026-09-10, caught before this shipped). The old bare-fact version
    was inert enough to sit harmlessly in a cron prompt; an imperative is not.
    Every invocation gets this block — the Sep 8 physio-nudge transcript carries
    the drift clause verbatim — so an ungated "OFFER to retime the store, ask
    first" lands in a bedtime nudge whose reply goes straight to the lock
    screen, in a job that tells itself "nobody is awake to approve anything."
    Asking a sleeping man to confirm a schedule change is worse than saying
    nothing, and it is the same class of misfire this whole task doc is about.

    Unattended turns keep the fact and the cost, which `consolidate` can act on
    by writing memory, and lose only the half that needs a person in front of
    it. `active_turn.ATTENDED_SOURCES` is the same gate `invoke.py` uses to
    decide streaming, deliberately: "is there a person watching this reply" is
    one question and should have one answer.
    """
    store_zone = _schedule_zone()
    if store_zone is None:
        return ""
    store_offset = moment.astimezone(ZoneInfo(store_zone)).utcoffset()
    local_offset = local.utcoffset()
    if store_offset is None or local_offset is None:
        return ""
    delta = (local_offset - store_offset).total_seconds()
    if delta == 0:
        return ""
    direction = "later" if delta > 0 else "earlier"
    # Says which jobs by describing them, not by listing them. Naming "the
    # bedtime nudge, the morning brief" read better and was a second, never-
    # updated copy of the store's contents -- the exact failure
    # `_rejection_sentence` refuses to repeat with the validator's schema, in a
    # change whose whole subject is hardcoded content outliving what it
    # described (/code-review medium, 2026-09-10).
    fact = (
        " The user's schedule store (cron/schedules.json)"
        f" still says {store_zone} while the user is in {local_zone}, so EVERY"
        f" recurring entry in it is firing {_describe_offset(delta)} {direction}"
        f" than the time written against it. This does not correct itself and"
        f" they cannot see it from the phone."
    )
    if not attended:
        return fact
    return fact + (
        " OFFER to retime the store. The repair is the store's single"
        " `timezone` field: do NOT edit the individual entry times, which are"
        " already written in their terms and would then be wrong twice over. Ask"
        " first — when their jobs run is their call, not yours."
    )


def _format_coordinate(value: float) -> str:
    # Four places is ~11 m, finer than any phone fix. `g` drops the trailing
    # zeros an Approximate (2-place) report would otherwise grow.
    return f"{round(value, 4):g}"


def _location_sentence(now_epoch: float) -> str:
    """Where their device last put them, or "" when there is no report under
    36 hours old. The first call after a restart reads the rounded copy from
    data_dir (server/location.py); it sits with the other reads before the
    gap is consumed all the same."""
    latest = location_store.latest()
    if latest is None:
        return ""
    where, reported_at = latest
    age = max(0.0, now_epoch - reported_at)
    # location.MAX_AGE_SECONDS (36 h) carries last night's report into the
    # morning jobs; the reasoning is there, with the restore that shares it.
    if age > MAX_AGE_SECONDS:
        return ""
    coords = f"{_format_coordinate(where.latitude)}, {_format_coordinate(where.longitude)}"
    if where.accuracy_m is not None:
        radius = (
            f"{round(where.accuracy_m)} m"
            if where.accuracy_m < 1000
            else f"{where.accuracy_m / 1000:.0f} km"
        )
        coords += f", ±{radius}"
    place = f"near {where.place} ({coords})" if where.place else f"at {coords}"
    if age < 2 * 60:
        when = "just now"
    elif age < 60 * 60:
        when = f"{int(age // 60)} min ago"
    else:
        when = f"{int(age // 3600)} h ago"
    return f" Their device put them {place}, reported {when}."


# Prepended to a message spoken in voice mode (docs/tasks/voice-mode-hands-free.md,
# Mike 2026-10-20: "be cautious of longer answers"). Every word of the reply
# is read out in full, so length is the user's time, and markdown is noise:
# the phone strips it, but a table stripped is still a wall of numbers. Per
# message, not per session, so Marlow returns to normal the turn after.
#
# This is the only description of voice mode Marlow gets (Mike, 2026-10-21).
# A copy in identity/CLAUDE.md drifted from it, and both lost to the general
# "few sentences" rules in PREFERENCES.md and SOUL.md: a spoken "how much
# snow on Friday?" got four sentences. Hence "override", for this message
# only. Spelling and symbols for the voice are the phone's job.
#
# The opening sentence is unconditional ("always start with"), not "if you'll
# use a tool": the phone speaks it while the tools run. As a conditional it
# was skipped in 3 of 4 live tool turns (a Grep or a Write first, 10-15 s of
# silence), and after a ToolSearch; in fresh `claude -p` runs on Mike's box,
# 7 of 9 conditional vs 9 of 9 unconditional led with a sentence
# (2026-10-21).
SPOKEN_TURN_NOTE = (
    "[Spoken: the user said this in voice mode and will hear your whole reply "
    "read aloud, and silence until you write something. For this message "
    "only, this overrides your usual length and style. Always start with one "
    "short sentence, before any tool call (ToolSearch included): the answer "
    "if you have it, otherwise what you're about to do (\"Checking your "
    "notes.\", \"I'll start a fun list with those.\"). Answer only what they "
    "asked, in a sentence or two, with round numbers (\"about 4 "
    "centimetres\", not \"3.9\"). Add anything else only if it changes what "
    "they'd do; they'll ask for more. Plain sentences only: no tables, lists, "
    "headings, code or links.]"
)


def _day_sentence() -> str:
    """ "Your day" and the check-in interval (docs/tasks/default-
    infrastructure.md). Marlow can't change
    it, so this is here for the turn where they ask to move the overnight jobs
    or the tidy-up: the identity says to point them at Settings, and this says
    what Settings currently holds. Never raises (`day()` doesn't)."""
    start, end = preferences_store.day()
    every = preferences_store.checkin_every_hours()
    checkins = f"every {every}h" if every else "off"
    return f" Their day in Settings: {start}-{end}; check-ins {checkins}."


def build_turn_context(
    *,
    resume: bool,
    session_id: str | None = None,
    source: str | None = None,
    now: datetime | None = None,
) -> str:
    """The one-line block, or "" when there is nothing worth saying.

    `source` is NOT what decides the elapsed-time sentence below; `resume` is
    (see `_gap_sentence`'s docstring for why that changed). It does decide one
    thing, added 2026-09-10: whether the drift notice asks Mike to do something,
    since an instruction to ask him is worthless on a turn he is not present
    for. That is the only question `source` answers here — if you find yourself
    reaching for it a third time, check first whether `resume` is the honest
    predicate, because it was the last two times.

    `resume` has no default on purpose, unlike every other argument here. It
    is the only one whose omission fails *silently and unrecoverably*: a
    forgotten `resume` reads as `False`, suppresses the gap sentence, and
    still consumes the stored timestamp, so the gap is gone rather than
    deferred. Every caller knows whether it is resuming; make it say so.

    Returns a bare string rather than raising on any failure: this is a
    convenience for the model, and a broken clock must never be the reason a
    turn doesn't run.
    """
    try:
        moment = now or datetime.now().astimezone()
        # Every fallible call happens BEFORE the gap is consumed. `_gap_sentence`
        # advances the session's timestamp as a side effect, so reading the
        # pairing store first means a transient read failure can no longer
        # swallow a six-hour gap on its way to the `except` below (review,
        # 2026-08-07). Nothing after this line can raise.
        zone = pairing_store.latest_timezone()
        local = moment.astimezone(ZoneInfo(zone)) if zone and is_valid_timezone(zone) else None
        # Also before the gap is consumed, for the same reason: this one reads
        # a file, and a file read that raises must not be able to swallow a
        # six-hour gap.
        # `zone` is not None whenever `local` is not None (it is what built it),
        # but the narrowing is not visible to a reader of this line alone, so it
        # is spelled out rather than asserted — the sentence names the zone back
        # to Marlow and a bare `str(local.tzinfo)` would be a second, weaker
        # spelling of a name we already hold.
        drift = (
            _drift_sentence(
                moment, local, zone, source in active_turn.ATTENDED_SOURCES
            )
            if local is not None and zone is not None
            else ""
        )
        # Same placement rule as drift: before the gap is consumed, because
        # it reads a file and a read that raises must not swallow the gap.
        rejected = _rejection_sentence() + _ideas_rejection_sentence()
        where = _location_sentence(time.time() if now is None else now.timestamp()) + _day_sentence()

        gap = _gap_sentence(session_id, resume, time.monotonic())

        if local is not None:
            return (
                f"[Context: the user's timezone is {zone} — their local time is "
                f"{local.strftime(_FORMAT)}. Any date given elsewhere in this "
                f"prompt is the server's; this one is theirs.{where}{drift}{rejected}{gap}]"
            )

        return (
            f"[Context: server local time is {moment.strftime(_FORMAT)}. The user's "
            f"phone has not reported a timezone, so assume they are in this one.{where}{rejected}{gap}]"
        )
    except Exception:
        logger.exception("could not build turn context; continuing without it")
        return ""


def prepend_turn_context(
    prompt: str,
    *,
    resume: bool,
    session_id: str | None = None,
    source: str | None = None,
) -> str:
    """Prompt with the context block in front, or unchanged if there is none.

    `resume` is required here for the same reason as in `build_turn_context`."""
    context = build_turn_context(session_id=session_id, source=source, resume=resume)
    return f"{context}\n\n{prompt}" if context else prompt

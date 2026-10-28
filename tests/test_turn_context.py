"""server/turn_context.py — the per-turn clock block.

The two things worth guarding are both structural rather than cosmetic: the
block must never migrate into the cache prefix, and the timezone must come from
the phone rather than the box, because the box is pinned to America/Edmonton
and Mike travels.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone as dt_timezone

import pytest

from server import turn_context
from server.config import settings
from server.turn_context import (
    _GAP_THRESHOLD_SECONDS,
    build_turn_context,
    is_valid_timezone,
    prepend_turn_context,
)


@pytest.fixture(autouse=True)
def _clean_gap_state():
    turn_context._reset_gap_for_tests()
    yield
    turn_context._reset_gap_for_tests()


@pytest.fixture
def no_devices(monkeypatch):
    monkeypatch.setattr(turn_context.pairing_store, "latest_timezone", lambda: None)


@pytest.fixture
def phone_in(monkeypatch):
    def _set(zone: str | None):
        monkeypatch.setattr(turn_context.pairing_store, "latest_timezone", lambda: zone)
    return _set


# --- timezone validation ----------------------------------------------------

@pytest.mark.parametrize("name", ["America/Edmonton", "Europe/Lisbon", "UTC"])
def test_real_zones_validate(name):
    assert is_valid_timezone(name)


@pytest.mark.parametrize("name", ["", "Mars/Olympus", "MDT", "../../etc/passwd", "A" * 200])
def test_junk_zones_are_rejected(name):
    """A bad zone is stored once and then silently mis-states Mike's local time
    on every turn until somebody notices, so it is rejected at the door rather
    than at render time. `MDT` is here deliberately: it is a real abbreviation
    and not a zone, which is the mistake a client author would actually make.
    """
    assert not is_valid_timezone(name)


# --- what the block says ----------------------------------------------------

def test_uses_the_phones_timezone_not_the_servers(phone_in):
    """The whole reason this exists. The box is pinned to America/Edmonton and
    would keep saying so while Mike is in Lisbon; only the phone knows."""
    phone_in("Europe/Lisbon")
    # 03:30 UTC — still "yesterday" in Edmonton, already today in Lisbon.
    moment = datetime(2026, 8, 8, 3, 30, tzinfo=dt_timezone.utc)
    block = build_turn_context(resume=False, now=moment)
    assert "Europe/Lisbon" in block
    assert "04:30" in block
    assert "Saturday 2026-08-08" in block


def test_falls_back_to_server_time_and_says_so(no_devices):
    """No phone has reported yet (or ever will, for a cron-only deployment).
    Saying "assume they are in this one" beats asserting a zone we don't have.
    """
    block = build_turn_context(resume=False)
    assert "has not reported a timezone" in block
    assert "server local time" in block


def test_the_block_never_names_the_user(phone_in, monkeypatch):
    """USER.md is the name's only home. The block once read MARLOW_USER_NAME from
    the install's drop-in, which onboarding cannot edit, so a nickname recorded
    in USER.md left the block saying the install-time name every turn. An older
    install's drop-in may still set it; it must not leak back in."""
    monkeypatch.setenv("MARLOW_USER_NAME", "Priya")
    from server.config import Settings

    monkeypatch.setattr(turn_context, "settings", Settings())
    phone_in("Europe/Lisbon")
    block = build_turn_context(resume=False)
    assert "the user's timezone is Europe/Lisbon" in block
    assert "Priya" not in block


def test_an_unlocated_block_says_the_user(no_devices):
    block = build_turn_context(resume=False)
    assert ". The user's phone has not reported a timezone" in block, "capitalised: it opens a sentence"
    assert "Mike" not in block


def test_an_unresolvable_stored_zone_falls_back_instead_of_raising(phone_in):
    """Defence for a zone that validated when stored and doesn't now — a tzdata
    package change can retire a name. Losing the clock is acceptable; failing
    the turn is not."""
    phone_in("Mars/Olympus")
    block = build_turn_context(resume=False)
    assert "has not reported a timezone" in block


def test_a_broken_pairing_store_does_not_break_the_turn(monkeypatch):
    """This is a convenience for the model. It must never be why a message
    doesn't get answered."""
    def boom():
        raise OSError("pairing file is gone")
    monkeypatch.setattr(turn_context.pairing_store, "latest_timezone", boom)
    assert build_turn_context(resume=False) == ""
    assert prepend_turn_context("what's up?", resume=False) == "what's up?"


# --- elapsed time (Mike's addition, 2026-08-07) -----------------------------

def _fake_clock(monkeypatch, *times):
    clock = iter(times)
    monkeypatch.setattr(turn_context.time, "monotonic", lambda: next(clock))


def test_no_gap_sentence_on_the_first_turn(no_devices):
    assert "have passed" not in build_turn_context(session_id="s1", source="chat", resume=True)


def test_no_gap_sentence_within_the_threshold(no_devices, monkeypatch):
    """A back-and-forth exchange shouldn't carry it — a line that appears every
    turn stops being read."""
    _fake_clock(monkeypatch, 1000.0, 1000.0 + _GAP_THRESHOLD_SECONDS - 1)
    build_turn_context(session_id="s1", source="chat", resume=True)
    assert "have passed" not in build_turn_context(session_id="s1", source="chat", resume=True)


def test_gap_sentence_past_the_threshold(no_devices, monkeypatch):
    _fake_clock(monkeypatch, 1000.0, 1000.0 + 3 * 3600)
    build_turn_context(session_id="s1", source="chat", resume=True)
    block = build_turn_context(session_id="s1", source="chat", resume=True)
    assert "3 hours have passed since the previous message." in block


def test_no_gap_sentence_without_resume(no_devices, monkeypatch):
    """`resume=False` means this turn is not continuing anything, whatever the
    source — a fresh session has no previous message to have elapsed since.
    This is the property the old `source == "cron"` check used to stand in
    for, imperfectly (see the resumed-cron test below)."""
    _fake_clock(monkeypatch, 1000.0, 1000.0 + 3 * 3600)
    build_turn_context(session_id="s1", source="chat", resume=True)
    assert "have passed" not in build_turn_context(session_id="s1", source="chat", resume=False)


def test_resumed_cron_turn_gets_the_gap_sentence(no_devices, monkeypatch):
    """The bug this fix closes (docs/tasks/nightly-consolidation.md, open
    finding #1): the nightly consolidation job resumes a real attended
    session but still carries `source="cron"`. The old `source == "cron"`
    suppression silently swallowed the sentence on exactly the turn most
    likely to need it — hours past the day's last message. The sentence must
    follow session freshness (`resume`), not the source label.
    """
    _fake_clock(monkeypatch, 1000.0, 1000.0 + 3 * 3600)
    build_turn_context(session_id="s1", source="chat", resume=True)
    block = build_turn_context(session_id="s1", source="cron", resume=True)
    assert "3 hours have passed since the previous message." in block


@pytest.mark.parametrize("elapsed,expected", [
    (55 * 60, "55 minutes"),
    # 90 minutes read as "2 hours" until 2026-08-07: round(1.5) is 2, a third
    # too much, in the bucket right above the 50-minute threshold.
    (90 * 60, "90 minutes"),
    (2 * 3600, "2 hours"),
    # Banker's rounding made these inconsistent -- round(2.5) is 2 but
    # round(3.5) is 4, so 2.5h and 2h described themselves identically.
    (int(2.5 * 3600), "3 hours"),
    (int(3.5 * 3600), "4 hours"),
    (3 * 3600, "3 hours"),
    (5 * 86400, "5 days"),
])
def test_gap_is_described_in_human_units(no_devices, monkeypatch, elapsed, expected):
    _fake_clock(monkeypatch, 1000.0, 1000.0 + elapsed)
    build_turn_context(session_id="s1", source="chat", resume=True)
    assert expected in build_turn_context(session_id="s1", source="chat", resume=True)


# --- the review finding: the gap is per-conversation ------------------------

def test_cron_never_claims_a_previous_message(no_devices, monkeypatch):
    """A cron turn that does not resume anything (`session_id=None,
    resume=False` in app.cron_message for every job outside
    ATTACHED_SESSION_JOBS) has no previous message by construction. Telling it
    "10 hours have passed since the previous message" is a false claim about
    its own history, not a harmless approximation.
    """
    _fake_clock(monkeypatch, 1000.0, 1000.0 + 10 * 3600)
    build_turn_context(session_id="s1", source="chat", resume=True)
    assert "have passed" not in build_turn_context(session_id=None, source="cron", resume=False)


def test_a_cron_run_does_not_swallow_the_next_real_gap(no_devices, monkeypatch):
    """The half of the bug that loses information rather than inventing it.

    Mike chats at 22:00; the morning job fires at 08:00; he messages at 08:30.
    With one global timestamp the cron run reset the clock, so his 10.5-hour
    gap measured as 30 minutes and was silently dropped -- exactly the case the
    feature exists for.
    """
    _fake_clock(monkeypatch, 0.0, 10 * 3600, 10.5 * 3600)
    build_turn_context(session_id="s1", source="chat", resume=True)         # 22:00, Mike
    build_turn_context(session_id=None, source="cron", resume=False)       # 08:00, morning job
    block = build_turn_context(session_id="s1", source="chat", resume=True)  # 08:30, Mike
    assert "have passed" in block


def test_two_conversations_do_not_share_a_gap(no_devices, monkeypatch):
    """"Since the previous message" is a claim about ONE conversation."""
    _fake_clock(monkeypatch, 1000.0, 1000.0 + 6 * 3600)
    build_turn_context(session_id="s1", source="chat", resume=True)
    assert "have passed" not in build_turn_context(session_id="s2", source="chat", resume=True)


def test_a_turn_with_no_session_yet_has_nothing_to_compare_to(no_devices, monkeypatch):
    _fake_clock(monkeypatch, 1000.0, 1000.0 + 6 * 3600)
    build_turn_context(session_id=None, source="chat", resume=False)
    assert "have passed" not in build_turn_context(session_id=None, source="chat", resume=False)


def test_a_failed_lookup_does_not_consume_the_gap(monkeypatch):
    """`_gap_sentence` advances the session's timestamp as a side effect, so it
    must run only after everything fallible has succeeded. Otherwise a
    transient pairing-store read error silently eats a six-hour gap, and the
    turn after it measures from the failed invocation.
    """
    _fake_clock(monkeypatch, 1000.0, 1000.0 + 6 * 3600)
    monkeypatch.setattr(turn_context.pairing_store, "latest_timezone", lambda: None)
    build_turn_context(session_id="s1", source="chat", resume=True)

    def boom():
        raise OSError("pairing file is gone")
    monkeypatch.setattr(turn_context.pairing_store, "latest_timezone", boom)
    assert build_turn_context(session_id="s1", source="chat", resume=True) == ""

    # The gap survived the failure: the next good turn still reports it.
    _fake_clock(monkeypatch, 1000.0 + 6 * 3600)
    monkeypatch.setattr(turn_context.pairing_store, "latest_timezone", lambda: None)
    assert "6 hours have passed" in build_turn_context(session_id="s1", source="chat", resume=True)


# --- placement --------------------------------------------------------------

def test_context_goes_in_front_of_the_prompt(no_devices):
    out = prepend_turn_context("what's on my calendar?", resume=False)
    assert out.endswith("what's on my calendar?")
    assert out.startswith("[Context:")


def test_the_block_is_not_in_the_identity_cache_prefix():
    """The mistake this module exists to prevent, asserted rather than
    commented. server/identity.py's output is the --append-system-prompt cache
    prefix; anything time-varying in it invalidates a ~58K block and every turn
    of history behind it, on every single invocation.

    If someone ever "tidies up" by composing the clock into the identity block,
    this fails.
    """
    from server.identity import compose_identity_context

    composed = compose_identity_context("soul", "user", [])
    assert "[Context:" not in composed
    # Byte-stability is the actual property; two calls a moment apart must not
    # differ, which a clock inside would guarantee they do.
    assert composed == compose_identity_context("soul", "user", [])


# --- schedule-store drift ---------------------------------------------------
#
# Marlow sees Mike's zone every turn but not the schedule store, which is a
# file. The harness holds both, so it says the sentence — and only when the two
# actually disagree about when a job lands.

@pytest.fixture
def store_zone(monkeypatch, tmp_path):
    def _set(zone: str | None):
        # A REAL Settings with one field replaced (it is frozen, so swap the
        # module's reference), the same shape test_cron_entrypoint uses. A stub
        # carrying a hand-written schedules_path would pass while proving
        # nothing about where the renderer actually writes.
        monkeypatch.setattr(
            turn_context, "settings", replace(settings, working_dir=tmp_path)
        )
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(exist_ok=True)
        if zone is None:
            return
        (cron_dir / "schedules.json").write_text(
            json.dumps({"timezone": zone, "entries": []})
        )
    return _set


def test_a_stale_store_is_named_with_the_size_and_direction_of_the_drift(
    phone_in, store_zone
):
    """The case that prompted this: Mike flies to Newfoundland, the store still
    says Alberta, and a job written as 07:45 fires at 11:15 his time."""
    phone_in("America/St_Johns")
    store_zone("America/Edmonton")
    out = build_turn_context(resume=False, source="chat")
    assert "still says America/Edmonton" in out
    assert "while the user is in America/St_Johns" in out
    assert "firing 3 hours 30 minutes later than the time written against it" in out


def test_the_drift_notice_names_no_individual_jobs(phone_in, store_zone):
    """It describes which entries drift; it does not list them. A list here is a
    second copy of the store's contents, kept in the server, updated by nobody —
    Marlow renames and removes its own jobs. `_rejection_sentence` refuses the
    same thing with the validator's schema, having watched that copy go stale
    before it deployed (/code-review medium, 2026-09-10).
    """
    phone_in("America/Halifax")
    store_zone("America/St_Johns")
    out = build_turn_context(resume=False, source="chat")
    assert "EVERY recurring entry" in out
    for job in ("bedtime nudge", "morning brief", "physio", "consolidate"):
        assert job not in out


def test_drift_the_other_way_says_earlier(phone_in, store_zone):
    phone_in("America/Edmonton")
    store_zone("America/St_Johns")
    assert "firing 3 hours 30 minutes earlier" in build_turn_context(resume=False)


def test_the_drift_notice_carries_a_cost_a_repair_and_the_wrong_repair(
    phone_in, store_zone
):
    """For ten days this sentence was a bare statement of fact, and Marlow never
    acted on one of them (docs/tasks/dated-plans-go-stale.md). The four parts
    below are what `_rejection_sentence` has and this used to lack; they are the
    entire content of the fix, so they are pinned rather than left to prose.

    Pinned as behaviour, not wording: each assertion is the part that would
    change what Marlow does if it went missing. Rephrasing the sentence should
    keep all four and may fail this test — that is the point of it. Dropping one
    silently returns the notice to being ambient, which is the state that
    produced no offer in ten days of saying it every single turn.
    """
    phone_in("America/Halifax")
    store_zone("America/St_Johns")
    out = build_turn_context(resume=False, source="chat")
    # The cost: without it a model may reasonably read this as advisory.
    assert "does not correct itself" in out
    assert "cannot see it from the phone" in out
    # The ask, which is the thing that was missing entirely.
    assert "OFFER to retime the store" in out
    # The repair, named precisely enough to carry out.
    assert "single `timezone` field" in out
    # The wrong repair, which is the obvious one and breaks again on the next
    # move: the entry times are already written in his terms.
    assert "do NOT edit the individual entry times" in out
    # Ask, don't act. When his jobs run is his call.
    assert "Ask first" in out


def test_an_unattended_turn_is_told_the_drift_but_not_told_to_ask(
    phone_in, store_zone
):
    """Nobody is awake on a cron turn, and its reply goes to the lock screen.

    The bare-fact version this replaced was inert enough to sit harmlessly in
    the physio-nudge prompt (it did, verbatim, on 2026-09-08). An imperative is
    not: ungated, "OFFER to retime the store... Ask first" would land in a
    bedtime stretch reminder that no one can answer. The fact and the cost stay
    — consolidate can act on those by writing memory — and only the half
    needing a person in front of it is dropped.
    """
    phone_in("America/Halifax")
    store_zone("America/St_Johns")
    out = build_turn_context(resume=False, source="cron")
    assert "still says America/St_Johns" in out
    assert "does not correct itself" in out
    assert "OFFER" not in out
    assert "Ask first" not in out


def test_an_unknown_source_is_treated_as_unattended(phone_in, store_zone):
    """The gate is membership of ATTENDED_SOURCES, not absence of "cron" — so a
    source nobody has thought of yet, and the `None` default, both fail closed
    into the quieter sentence rather than into an ask nobody will read."""
    phone_in("America/Halifax")
    store_zone("America/St_Johns")
    assert "OFFER" not in build_turn_context(resume=False)
    assert "OFFER" not in build_turn_context(resume=False, source="something-new")


def test_equal_offsets_say_nothing_even_when_the_names_differ(phone_in, store_zone):
    """A nag with no correction to make is worse than silence. Denver and
    Edmonton are different strings and identical schedules."""
    phone_in("America/Denver")
    store_zone("America/Edmonton")
    assert "schedule store" not in build_turn_context(resume=False)


def test_the_same_zone_says_nothing(phone_in, store_zone):
    phone_in("America/Edmonton")
    store_zone("America/Edmonton")
    assert "schedule store" not in build_turn_context(resume=False)


def test_no_store_says_nothing(phone_in, store_zone):
    """A box with no schedules is the ordinary state, not a problem."""
    phone_in("America/St_Johns")
    store_zone(None)
    assert "schedule store" not in build_turn_context(resume=False)


def test_a_broken_store_is_silent_here_and_leaves_the_clock_intact(
    phone_in, store_zone, tmp_path
):
    """load() rejects an invalid store loudly; this is a hint and must not
    report the same failure a second time every turn — nor lose Mike's local
    time trying to."""
    phone_in("America/St_Johns")
    store_zone("America/Edmonton")
    (tmp_path / "cron" / "schedules.json").write_text("{not json")
    out = build_turn_context(resume=False)
    assert "schedule store" not in out
    assert "the user's timezone is America/St_Johns" in out


def test_a_broken_store_cannot_swallow_the_gap(phone_in, store_zone, tmp_path, monkeypatch):
    """The ordering invariant this module already guards for the pairing read:
    every fallible call happens before _gap_sentence consumes the timestamp."""
    phone_in("America/St_Johns")
    store_zone("America/Edmonton")
    (tmp_path / "cron" / "schedules.json").write_text("{not json")
    _fake_clock(monkeypatch, 1000.0, 1000.0 + 6 * 3600)
    build_turn_context(session_id="s1", resume=True)
    assert "6 hours have passed" in build_turn_context(session_id="s1", resume=True)


def test_no_phone_zone_means_no_claim_about_drift(no_devices, store_zone):
    """Without a reported zone the harness does not know he moved, and the
    fallback block already tells him to assume the server's zone."""
    store_zone("America/St_Johns")
    assert "schedule store" not in build_turn_context(resume=False)


def test_the_store_path_has_exactly_one_definition():
    """turn_context cannot import server.schedules (that module imports
    is_valid_timezone from here), so the store path is the kind of constant
    that gets re-spelled per reader. Re-spelled, a later move fails silently:
    the renderer writes the new location, this reads the old one, finds
    nothing, and the drift notice switches off with every test still green.
    Both must resolve settings.schedules_path (/code-review medium, 2026-08-31).
    """
    from server import schedules

    assert schedules.SCHEDULES_PATH == settings.schedules_path
    assert settings.schedules_path == settings.working_dir / "cron" / "schedules.json"


# --- a schedule store the server is refusing --------------------------------
#
# The store is the one piece of harness state Marlow both owns and cannot
# observe the effect of: sync_after_turn treats a refused store exactly like an
# absent one, so a bad edit yields a successful Write and no other signal.


@pytest.fixture
def broken_store(monkeypatch, tmp_path):
    def _write(store: dict):
        monkeypatch.setattr(
            turn_context, "settings", replace(settings, working_dir=tmp_path)
        )
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(exist_ok=True)
        # Every `job` entry requires its prompt file to exist, so the store
        # cannot be exercised without them -- a fixture missing one fails on
        # the WRONG entry and the test passes for the wrong reason.
        for job in (
            "consolidate", "morning", "alberta-weekly",
            "weekly-digest", "physio-nudge", "cabot-trail-check",
        ):
            (cron_dir / f"{job}.md").write_text(f"the {job} prompt\n")
        (cron_dir / "schedules.json").write_text(json.dumps(store))
    return _write


# Verbatim from the box, 2026-09-06 (one reminder's text made generic for
# publication) — the store Marlow wrote at 16:04 and the
# server refused on every turn afterwards. Kept whole rather than minimised:
# the five valid entries above the bad one are the point, since the rejection
# is all-or-nothing and they were frozen too.
#
# It VALIDATES now. `at`+`job` shipped in the same change that added this
# warning, so the store that motivated the feature is no longer an example of
# it — see test_the_store_that_broke_is_accepted_now. The warning tests below
# use a different fault.
#
# One edit to "verbatim": its `consolidate` entry is gone. Since 2026-10-21 the
# harness runs that job itself and an entry naming it is refused
# (docs/tasks/default-infrastructure.md).
_THE_STORE_THAT_BROKE = {
    "timezone": "America/St_Johns",
    "entries": [
        {"id": "morning", "cron": "0 8 * * *", "job": "morning"},
        {"id": "alberta-weekly", "cron": "3 8 * * 1", "job": "alberta-weekly"},
        {"id": "weekly-digest", "cron": "0 18 * * 0", "job": "weekly-digest"},
        {"id": "physio-nudge", "cron": "57 20 * * *", "job": "physio-nudge"},
        {
            "id": "trip-flights",
            "at": "2026-09-07T14:30-0230",
            "notify": "Book the flights.",
        },
        {"id": "cabot-trail-check", "at": "2026-09-09T09:00", "job": "cabot-trail-check"},
    ],
}


def _with_a_bad_entry(store: dict) -> dict:
    """The historical store, with its last entry given a fault that survives.

    Deliberately a DIFFERENT fault from the original one: `at`+`job` is legal
    now. Two payloads on one entry is refused by a rule that has been stable
    since the store existed, so this test is about the reporting rather than
    about any particular validation rule.
    """
    entries = [dict(entry) for entry in store["entries"]]
    entries[-1] = {
        "id": "cabot-trail-check",
        "at": "2026-09-09T09:00",
        "job": "cabot-trail-check",
        "notify": "and also this",
    }
    return {**store, "entries": entries}


def test_the_store_that_broke_is_accepted_now(broken_store):
    """The regression this whole change exists for: Marlow's one-off job."""
    from server import schedules

    broken_store(_THE_STORE_THAT_BROKE)
    assert schedules.load(turn_context.settings.schedules_path) is not None


def test_a_refused_store_is_reported_to_the_model(phone_in, broken_store):
    phone_in("America/St_Johns")
    broken_store(_with_a_bad_entry(_THE_STORE_THAT_BROKE))
    out = build_turn_context(resume=False)
    assert "cron/schedules.json is currently INVALID" in out
    # The reason must identify WHICH entry. The message did not until
    # 2026-09-06, so even reading the journal you could not tell.
    assert "'cabot-trail-check'" in out
    assert "exactly one of notify, prompt, or job" in out
    # And it must say what the breakage COSTS, or it reads as advisory.
    assert "will NOT fire" in out
    # And it must name the REMEDY. Handed only the rule on 2026-09-06, Marlow
    # repaired the store with a dated `cron` expression plus a note to itself
    # to delete the entry later -- valid, and a yearless line that fires again
    # next September with no turn ever pruning it.
    assert "prunes itself" in out
    assert "no year" in out
    # But NOT a restatement of the schema. That went stale within the hour
    # once `at`+`job` shipped; the validator is the one copy of the rules.
    assert "notify` or `prompt`" not in out


def test_a_valid_store_says_nothing(phone_in, broken_store):
    """This block is read every turn; a healthy box must pay nothing for it."""
    phone_in("America/St_Johns")
    broken_store(
        {
            "timezone": "America/St_Johns",
            "entries": [{"id": "morning", "cron": "0 8 * * *", "notify": "up"}],
        }
    )
    assert "INVALID" not in build_turn_context(resume=False)


def test_an_absent_store_says_nothing(phone_in, store_zone):
    """A box with no schedules is the ordinary state, not a fault."""
    phone_in("America/St_Johns")
    store_zone(None)
    assert "INVALID" not in build_turn_context(resume=False)


def test_the_context_says_what_your_day_is_set_to(phone_in, store_zone, tmp_path, monkeypatch):
    """Marlow can't change "Your day", so it needs to see it to point the user
    at Settings (docs/tasks/default-infrastructure.md)."""
    from server.preferences import PreferencesStore

    store = PreferencesStore(tmp_path / "preferences.json")
    monkeypatch.setattr(turn_context, "preferences_store", store)
    phone_in("America/St_Johns")
    store_zone(None)
    assert "Their day in Settings: 08:00-23:00; check-ins every 3h." in build_turn_context(resume=False)
    store.update(day_start="07:30", day_end="01:00", checkin_every_hours=0)
    assert "Their day in Settings: 07:30-01:00; check-ins off." in build_turn_context(resume=False)


def test_a_failure_to_check_costs_the_warning_and_nothing_else(
    phone_in, broken_store, monkeypatch
):
    """Degrade to silence, never to a missing context block.

    `_rejection_sentence` swallows its own failures rather than relying on
    build_turn_context's outer catch, because that catch returns "" for the
    WHOLE block -- losing Mike's timezone and the elapsed-time gap to a
    transient read error on a file that is only advisory.
    """
    phone_in("America/St_Johns")
    broken_store(_with_a_bad_entry(_THE_STORE_THAT_BROKE))

    def boom(_path):
        raise OSError("store unreadable")

    from server import schedules

    monkeypatch.setattr(schedules, "rejection_reason", boom)
    out = build_turn_context(resume=False)
    assert "INVALID" not in out
    assert "the user's timezone is America/St_Johns" in out


def test_the_reason_is_read_from_the_real_validator(broken_store):
    """Not a re-derivation of the rules: schedules.load must agree that this
    store is refused, or the two can drift and the warning becomes fiction."""
    from server import schedules

    broken_store(_with_a_bad_entry(_THE_STORE_THAT_BROKE))
    path = turn_context.settings.schedules_path
    assert schedules.load(path) is None
    assert schedules.rejection_reason(path)

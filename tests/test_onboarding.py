"""The onboarding state machine (docs/tasks/onboarding-v2.md, "Harness design").

Everything is driven through tmp files: the record, USER.md and the step file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import onboarding
from server.onboarding import STEPS, State

IDS = [s.id for s in STEPS]
PLACEHOLDER_USER = "# User\n\nNot yet known: who they are.\n"
FILLED_USER = "# User\n\nLives in a small town and likes mornings.\n"


@pytest.fixture
def paths(tmp_path):
    record = tmp_path / "data" / "onboarding.json"
    record.parent.mkdir()
    user = tmp_path / "USER.md"
    user.write_text(PLACEHOLDER_USER, encoding="utf-8")
    return record, user


def _go(paths, calls=(), commit_paths=()):
    record, user = paths
    return onboarding.after_turn(record, user, list(calls), tuple(commit_paths))


def _fresh(paths) -> State:
    record, user = paths
    return onboarding.load(record, user)


def _advance_to(paths, step_id):
    """Skip forward to `step_id` so a test can start from the middle."""
    _fresh(paths)
    while onboarding.load(*paths).step != step_id:
        cur = onboarding.load(*paths).step
        _go(paths, [(cur, "skipped")])


def test_marker_is_the_starters_and_the_shipped_steps_parse():
    """The starter USER.md opens "Everything else" with the marker, and the
    shipped step file matches STEPS, so a fresh box starts and can finish."""
    identity = Path(__file__).resolve().parent.parent / "identity"
    starter = (identity / "USER.md").read_text().split("## Everything else", 1)[1]
    assert starter.strip().startswith(onboarding.MARKER)
    texts = onboarding.load_steps(identity / "FIRST-RUN.md")
    assert list(texts) == [s.id for s in onboarding.STEPS]
    assert onboarding.MARKER in texts["wrap-up"], "the wrap-up says what ends it"
    assert all("step_done" in texts[s.id] for s in onboarding.STEPS)


def test_steps_table():
    assert [(s.id, s.cap, s.check) for s in STEPS] == [
        ("name", 3, "none"),
        ("stay-on-top", 6, "none"),
        ("one-thing", 4, "one-thing"),
        ("tour", 3, "none"),
        ("runs-alone", 4, "none"),
        ("wrap-up", 4, "user-md"),
    ]


# --- load ------------------------------------------------------------------


def test_fresh_install_starts_at_name_and_writes_record(paths):
    record, _ = paths
    s = _fresh(paths)
    assert (s.step, s.turns, s.finished, s.ended) == ("name", 0, False, None)
    assert json.loads(record.read_text())["step"] == "name"


def test_existing_box_without_marker_is_finished(paths):
    record, user = paths
    user.write_text(FILLED_USER, encoding="utf-8")
    s = _fresh(paths)
    assert s.finished and s.ended == "existing" and s.step is None
    assert json.loads(record.read_text())["finished"] is True
    assert onboarding.step_line(s, {i: "t" for i in IDS}) is None
    assert onboarding.remaining_steps(s, {i: "t" for i in IDS}) == []


def test_missing_user_md_is_existing(paths):
    record, user = paths
    user.unlink()
    s = _fresh(paths)
    assert s.finished and s.ended == "existing"
    assert record.exists()


def test_after_turn_on_finished_leaves_it_alone(paths):
    record, user = paths
    user.write_text(FILLED_USER, encoding="utf-8")
    _fresh(paths)
    before = record.read_text()
    mtime = record.stat().st_mtime_ns
    s = _go(paths, [("name", "done")])
    assert s.finished and s.ended == "existing"
    assert record.read_text() == before
    assert record.stat().st_mtime_ns == mtime


def test_corrupt_record_is_finished_and_untouched(paths):
    record, _ = paths
    record.write_text("{not json", encoding="utf-8")
    s = _fresh(paths)
    assert s.finished and s.ended == "existing"
    assert record.read_text() == "{not json"
    s = _go(paths, [("name", "done")])
    assert s.finished
    assert record.read_text() == "{not json"


def test_record_roundtrips(paths):
    _fresh(paths)
    _go(paths, [("name", "done")])
    s = onboarding.load(*paths)
    assert s.step == "stay-on-top" and s.outcomes == {"name": "done"} and s.turns == 0


# --- after_turn ------------------------------------------------------------


def test_done_on_unchecked_step_advances_and_resets_turns(paths):
    _fresh(paths)
    _go(paths)  # a plain turn on name
    assert onboarding.load(*paths).turns == 1
    s = _go(paths, [("name", "done")])
    assert s.step == "stay-on-top" and s.turns == 0
    assert s.outcomes == {"name": "done"}
    assert s.rejected is None and s.capped is None


def test_plain_turn_counts(paths):
    _fresh(paths)
    s = _go(paths)
    assert s.step == "name" and s.turns == 1


def test_one_thing_rejected_without_commit(paths):
    _advance_to(paths, "one-thing")
    s = _go(paths, [("one-thing", "done")], commit_paths=())
    assert s.step == "one-thing" and s.rejected == "one-thing"
    line = onboarding.step_line(s, {i: "text" for i in IDS})
    assert "Not done yet" in line
    # and the rejection clears on the next turn
    s = _go(paths)
    assert s.rejected is None


@pytest.mark.parametrize(
    "path,ok",
    [
        ("cron/schedules.json", True),
        ("memory/x.md", True),
        ("ideas/ideas.json", False),
        ("memoryx/y.md", False),
    ],
)
def test_one_thing_check_paths(paths, path, ok):
    _advance_to(paths, "one-thing")
    s = _go(paths, [("one-thing", "done")], commit_paths=(path,))
    if ok:
        assert s.step == "tour" and s.outcomes["one-thing"] == "done"
    else:
        assert s.step == "one-thing" and s.rejected == "one-thing"


def test_skipped_passes_a_failing_check(paths):
    _advance_to(paths, "one-thing")
    s = _go(paths, [("one-thing", "skipped")])
    assert s.step == "tour" and s.outcomes["one-thing"] == "skipped"


def test_two_steps_in_one_turn(paths):
    _fresh(paths)
    s = _go(paths, [("name", "done"), ("stay-on-top", "done")])
    assert s.step == "one-thing"
    assert s.outcomes == {"name": "done", "stay-on-top": "done"}
    assert s.turns == 0


def test_rejection_stops_later_calls(paths):
    _advance_to(paths, "one-thing")
    s = _go(paths, [("one-thing", "done"), ("one-thing", "skipped")])
    assert s.step == "one-thing" and s.rejected == "one-thing"


def test_call_for_other_step_ignored(paths):
    _fresh(paths)
    s = _go(paths, [("tour", "done"), ("nonsense", "done")])
    assert s.step == "name" and s.outcomes == {} and s.turns == 1


def test_unknown_outcome_ignored(paths):
    _fresh(paths)
    s = _go(paths, [("name", "maybe")])
    assert s.step == "name" and s.outcomes == {}


def test_cap_advances_on_nth_turn(paths):
    _fresh(paths)
    assert _go(paths).step == "name"
    assert _go(paths).step == "name"
    s = _go(paths)  # third turn of name, cap 3
    assert s.step == "stay-on-top" and s.turns == 0
    assert s.outcomes == {"name": "capped"} and s.capped == "name"
    line = onboarding.step_line(s, {i: f"text-{i}" for i in IDS})
    assert "Moving on from name" in line
    s = _go(paths)
    assert s.capped is None and s.turns == 1
    assert "Moving on" not in onboarding.step_line(s, {i: "t" for i in IDS})


def test_advance_on_cap_turn_is_not_capped(paths):
    _fresh(paths)
    _go(paths)
    _go(paths)
    s = _go(paths, [("name", "done")])
    assert s.outcomes == {"name": "done"} and s.capped is None


def test_cap_clears_rejected(paths):
    _advance_to(paths, "one-thing")
    for _ in range(3):
        _go(paths)
    s = _go(paths, [("one-thing", "done")])  # 4th turn: rejected AND at cap
    # A rejection is not an advance, so the cap still moves on, and the
    # moving-on sentence replaces the now-stale "not done yet".
    assert s.step == "tour" and s.capped == "one-thing"
    assert s.rejected is None


def test_wrap_up_rejected_while_marker_present(paths):
    _advance_to(paths, "wrap-up")
    s = _go(paths, [("wrap-up", "done")])
    assert s.step == "wrap-up" and s.rejected == "wrap-up"
    assert "Not yet known" in onboarding.step_line(s, {i: "t" for i in IDS})


def test_wrap_up_done_finishes(paths):
    record, user = paths
    _advance_to(paths, "wrap-up")
    user.write_text(FILLED_USER, encoding="utf-8")
    s = _go(paths, [("wrap-up", "done")])
    assert s.finished and s.step is None and s.ended == "done"
    assert s.outcomes["wrap-up"] == "done"
    assert onboarding.is_complete(record, user)
    assert onboarding.step_line(s, {i: "t" for i in IDS}) is None


def test_wrap_up_skipped_finishes_done(paths):
    _advance_to(paths, "wrap-up")
    s = _go(paths, [("wrap-up", "skipped")])
    assert s.finished and s.ended == "done"


def test_wrap_up_capped_finishes_capped(paths):
    _advance_to(paths, "wrap-up")
    for _ in range(4):
        s = _go(paths)
    assert s.finished and s.ended == "capped" and s.outcomes["wrap-up"] == "capped"


def test_user_md_unreadable_fails_check(paths):
    _, user = paths
    _advance_to(paths, "wrap-up")
    user.unlink()
    s = _go(paths, [("wrap-up", "done")])
    assert s.rejected == "wrap-up" and not s.finished


def test_is_complete(paths):
    assert onboarding.is_complete(*paths) is False
    _go(paths, [("name", "done")])
    assert onboarding.is_complete(*paths) is False


def test_is_complete_fails_open(monkeypatch, paths):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(onboarding, "load", boom)
    assert onboarding.is_complete(*paths) is True


def test_atomic_write_leaves_no_temp_files(paths):
    record, _ = paths
    _fresh(paths)
    _go(paths, [("name", "done")])
    _go(paths)
    assert [p.name for p in record.parent.iterdir()] == ["onboarding.json"]


# --- step_line / remaining_steps -------------------------------------------


def test_step_line_shape(paths):
    s = _fresh(paths)
    texts = {i: f"instructions for {i}" for i in IDS}
    line = onboarding.step_line(s, texts)
    assert line.startswith("Onboarding step 1 of 6 (name): instructions for name")
    assert "step_done" in line
    assert "Back to setup:" in line
    assert "#" not in line


def test_step_line_mentions_missing_for_each_check(paths):
    texts = {i: "t" for i in IDS}
    s = State(step="one-thing", rejected="one-thing")
    assert "nothing has been saved to the schedule or memory" in onboarding.step_line(s, texts)
    s = State(step="wrap-up", rejected="wrap-up")
    assert 'USER.md still has the "Not yet known" paragraph' in onboarding.step_line(s, texts)


def test_remaining_steps_from_the_middle(paths):
    _advance_to(paths, "tour")
    s = onboarding.load(*paths)
    texts = {i: f"text-{i}" for i in IDS}
    assert onboarding.remaining_steps(s, texts) == [
        {"id": "tour", "text": "text-tour"},
        {"id": "runs-alone", "text": "text-runs-alone"},
        {"id": "wrap-up", "text": "text-wrap-up"},
    ]


# --- load_steps ------------------------------------------------------------


def _write_steps(tmp_path, ids, header="Author notes, ignored.\n\n"):
    f = tmp_path / "FIRST-RUN.md"
    body = header + "".join(f"## {i}\n\nDo the {i} thing.\nSecond line.\n\n" for i in ids)
    f.write_text(body, encoding="utf-8")
    return f


def test_load_steps_happy_path(tmp_path):
    out = onboarding.load_steps(_write_steps(tmp_path, IDS))
    assert list(out) == IDS
    assert out["name"] == "Do the name thing.\nSecond line."


def test_load_steps_ignores_header(tmp_path):
    out = onboarding.load_steps(_write_steps(tmp_path, IDS, header="# Title\n\nWords with ## inside.\n"))
    assert "Title" not in out["name"] and list(out) == IDS


@pytest.mark.parametrize(
    "ids",
    [
        IDS[:-1],
        IDS + ["extra"],
        [IDS[1], IDS[0]] + IDS[2:],
        ["name"] + ["bogus"] + IDS[2:],
        [],
    ],
)
def test_load_steps_wrong_ids_raise(tmp_path, ids):
    with pytest.raises(ValueError):
        onboarding.load_steps(_write_steps(tmp_path, ids))


def test_one_thing_counts_a_save_from_an_earlier_turn(paths):
    """The reminder saved in one turn and the claim made in the next: the
    model confirms, the user says thanks, then it calls step_done. The check
    is about what onboarding has saved, not what the claiming turn did."""
    _advance_to(paths, "one-thing")
    _go(paths, [], ["cron/schedules.json"])

    state = _go(paths, [("one-thing", "done")], [])

    assert state.step == "tour" and state.rejected is None


def test_a_save_during_stay_on_top_counts_for_one_thing(paths):
    """A model that jumps ahead and saves the reminder while still asking
    what to stay on top of has still saved it."""
    _advance_to(paths, "stay-on-top")
    _go(paths, [("stay-on-top", "done")], ["memory/dentist.md"])

    state = _go(paths, [("one-thing", "done")], [])

    assert state.step == "tour"

"""Where a new user is in onboarding (docs/tasks/onboarding-v2.md, "Harness design").

The model knows when a step is finished; what it lost, the first time a real
user went through, was *where it was* (a time-zone question ended onboarding).
So the server keeps the place: one small JSON record, advanced after each chat
turn from the onboarding tool calls the turn made, and only when the step's
check on disk agrees. A turn cap per step moves a stuck conversation on.

This module is the state machine and nothing else. Every path is a parameter;
it imports no invoke, queue or config (only the logger), so the whole thing is testable with tmp
files. The caller decides which turns count (chat only, never cron or system).

Failure direction: anything that goes wrong reads as "onboarding is over". A
broken record or an unexpected exception must never restart onboarding for a
user who has already been through it, or show a step line to one who has not
asked for it.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from server.log import get_logger

logger = get_logger(__name__)

# The placeholder the starter USER.md ships with: its presence starts
# onboarding on a fresh box, and its absence is the wrap-up step's check.
# identity/USER.md, identity/CLAUDE.md and FIRST-RUN.md's wrap-up all name it
# (tests/test_onboarding.py and tests/test_deploy.py hold them to it).
MARKER = "Not yet known"

CHECK_NONE = "none"
CHECK_ONE_THING = "one-thing"
CHECK_USER_MD = "user-md"

OUTCOME_DONE = "done"
OUTCOME_SKIPPED = "skipped"
OUTCOME_CAPPED = "capped"


@dataclass(frozen=True)
class Step:
    id: str
    cap: int  # chat turns on this step before the server moves on
    check: str  # what must be true on disk for a "done" claim


# The caps are a first guess, to tune from real runs.
STEPS: tuple[Step, ...] = (
    Step("name", 3, CHECK_NONE),
    Step("stay-on-top", 6, CHECK_NONE),
    Step("one-thing", 4, CHECK_ONE_THING),
    Step("tour", 3, CHECK_NONE),
    Step("runs-alone", 4, CHECK_NONE),
    Step("wrap-up", 4, CHECK_USER_MD),
)
_BY_ID = {s.id: s for s in STEPS}

# Appended to every step line: the rules that used to open and close
# FIRST-RUN.md, and that apply to every step alike. The detour rule is the one
# the first outside user's run needed (a time-zone question ended setup,
# 2026-10-17); the approval one is because a new user has never seen a prompt.
# The first sentence is the old script's purpose: without it each step is done
# to the letter and the whole reads as a tour.
STEP_RULES = (
    "This setup decides whether they keep using you: the aim is that they leave it with "
    "real things you are already keeping on top of for them, not a tour. "
    "You lead setup: end each message with exactly one concrete question or offer, never "
    'two and never a loose one ("what do you need?", "want a few things to try?"), and '
    "keep messages short. If they ask for something else, help "
    "with that first, then go straight back to this step and say so (\"Back to setup: …\"). "
    "If anything asks their phone before the wrap-up (opening a message, say), that is "
    "their first approval: say in a line first that their phone will ask, what you're "
    "asking to do, and why. When this step is finished, or they wave it off, call "
    "step_done with this step's id and carry on with what it returns, in the same reply."
)

_MISSING = {
    CHECK_ONE_THING: "Not done yet: nothing has been saved to the schedule or memory.",
    CHECK_USER_MD: 'Not done yet: USER.md still has the "Not yet known" paragraph.',
}


@dataclass
class State:
    step: str | None = None  # current step id; None once finished
    turns: int = 0  # chat turns spent on the current step
    outcomes: dict[str, str] = field(default_factory=dict)  # step id -> done|skipped|capped
    finished: bool = False
    ended: str | None = None  # done | capped | existing
    rejected: str | None = None  # current step, if its claim failed the check last turn
    capped: str | None = None  # step the cap moved past last turn
    # Any onboarding turn's commit touched the schedule or memory. Sticky, so
    # a reminder saved in one turn and claimed in the next still counts.
    saved: bool = False


def _finished_state(ended: str) -> State:
    return State(step=None, finished=True, ended=ended)


def _write(record_path: Path, state: State) -> None:
    """Atomic: a crash mid-write must leave the old record, not half of a new
    one (which would read as corrupt, hence as finished)."""
    record_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=record_path.parent, prefix=".onboarding-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=2)
        os.replace(tmp, record_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse(raw: Any) -> State:
    if not isinstance(raw, dict):
        raise ValueError("record is not an object")
    step = raw.get("step")
    outcomes = raw.get("outcomes", {})
    finished = raw.get("finished")
    if not isinstance(finished, bool):
        raise ValueError("finished must be a bool")
    if finished != (step is None) or (step is not None and step not in _BY_ID):
        raise ValueError(f"inconsistent step {step!r}")
    turns = raw.get("turns", 0)
    if not isinstance(turns, int) or isinstance(turns, bool) or turns < 0:
        raise ValueError("turns must be a non-negative int")
    if not isinstance(outcomes, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in outcomes.items()
    ):
        raise ValueError("outcomes must map str to str")
    optional = {}
    for key in ("ended", "rejected", "capped"):
        v = raw.get(key)
        if v is not None and not isinstance(v, str):
            raise ValueError(f"{key} must be a string or null")
        optional[key] = v
    saved = raw.get("saved", False)
    if not isinstance(saved, bool):
        raise ValueError("saved must be a bool")
    return State(
        step=step, turns=turns, outcomes=dict(outcomes), finished=finished, saved=saved, **optional
    )


def _user_md_has_marker(user_path: Path) -> bool | None:
    """True/False, or None when USER.md can't be read."""
    try:
        return MARKER in user_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def load(record_path: Path, user_path: Path, *, persist: bool = True) -> State:
    """The current state, creating the record on first use.

    `persist=False` for a reader off the work queue (`is_complete`, from a
    request thread): only the queue's own reads may write, or a stale fresh
    state could land over a step after_turn just saved.

    A record that exists but won't parse is a finished onboarding, not a fresh
    one, and is left on disk for a human to look at."""
    try:
        text = record_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logger.warning("onboarding record unreadable, treating onboarding as finished: %s", e)
        return _finished_state("existing")
    else:
        try:
            return _parse(json.loads(text))
        except ValueError as e:
            logger.warning("onboarding record corrupt, treating onboarding as finished: %s", e)
            return _finished_state("existing")

    # No record. USER.md still holding the placeholder means onboarding is due;
    # anything else is a box that onboarded before the record existed.
    if _user_md_has_marker(user_path):
        state = State(step=STEPS[0].id)
    else:
        state = _finished_state("existing")
    if persist:
        _write(record_path, state)
    return state


def _saves_something(commit_paths: tuple[str, ...]) -> bool:
    return any(p == "cron/schedules.json" or p.startswith("memory/") for p in commit_paths)


def _check_passes(check: str, state: State, user_path: Path) -> bool:
    if check == CHECK_NONE:
        return True
    if check == CHECK_ONE_THING:
        return state.saved
    if check == CHECK_USER_MD:
        return _user_md_has_marker(user_path) is False
    return False


def _advance(state: State, outcome: str) -> None:
    assert state.step is not None
    state.outcomes[state.step] = outcome
    state.turns = 0
    index = [s.id for s in STEPS].index(state.step)
    if index + 1 < len(STEPS):
        state.step = STEPS[index + 1].id
        return
    state.step = None
    state.finished = True
    state.ended = OUTCOME_CAPPED if outcome == OUTCOME_CAPPED else OUTCOME_DONE


def after_turn(
    record_path: Path,
    user_path: Path,
    calls: list[tuple[str, str]],
    commit_paths: tuple[str, ...],
) -> State:
    """Advance after one CHAT turn. `calls` are (step id, outcome) from the
    turn's onboarding tool calls in order; `commit_paths` are the repo-relative
    paths the turn's commit touched."""
    state = load(record_path, user_path)
    if state.finished:
        return state
    state.rejected = None
    state.capped = None
    state.turns += 1
    state.saved = state.saved or _saves_something(commit_paths)
    advanced = False

    for step_id, outcome in calls:
        if state.step is None or step_id != state.step:
            continue
        if outcome == OUTCOME_SKIPPED:
            _advance(state, OUTCOME_SKIPPED)
            advanced = True
        elif outcome == OUTCOME_DONE:
            if _check_passes(_BY_ID[state.step].check, state, user_path):
                _advance(state, OUTCOME_DONE)
                advanced = True
            else:
                state.rejected = state.step
                break

    if not state.finished and not advanced and state.turns >= _BY_ID[state.step].cap:
        state.capped = state.step
        state.rejected = None
        _advance(state, OUTCOME_CAPPED)

    _write(record_path, state)
    return state


def is_complete(record_path: Path, user_path: Path) -> bool:
    """Fails open: if the answer can't be had, nothing is held back on it."""
    try:
        return load(record_path, user_path, persist=False).finished
    except Exception:
        logger.exception("onboarding state unavailable, treating onboarding as complete")
        return True


def step_line(state: State, texts: dict[str, str]) -> str | None:
    """The per-turn line naming the current step; None when finished."""
    if state.finished or state.step is None:
        return None
    ids = [s.id for s in STEPS]
    parts = [
        f"Onboarding step {ids.index(state.step) + 1} of {len(ids)} ({state.step}): {texts[state.step]}"
    ]
    if state.capped:
        parts.append(f"Moving on from {state.capped}: they can come back to it any time.")
    if state.rejected and state.rejected in _BY_ID:
        missing = _MISSING.get(_BY_ID[state.rejected].check)
        if missing:
            parts.append(missing)
    parts.append(STEP_RULES)
    return "\n".join(parts)


def remaining_steps(state: State, texts: dict[str, str]) -> list[dict[str, str]]:
    """The current step and every later one, for the onboarding tool."""
    if state.finished or state.step is None:
        return []
    ids = [s.id for s in STEPS]
    return [{"id": i, "text": texts[i]} for i in ids[ids.index(state.step):]]


def load_steps(path: Path) -> dict[str, str]:
    """Parse the step file into {id: text}. Raises ValueError unless the
    `## <id>` sections are exactly STEPS' ids, in order. Anything before the
    first `## ` heading is an author header and ignored."""
    sections: list[tuple[str, list[str]]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    got = [sid for sid, _ in sections]
    want = [s.id for s in STEPS]
    if got != want:
        raise ValueError(f"{path}: step sections {got} do not match {want}")
    return {sid: "\n".join(body).strip() for sid, body in sections}

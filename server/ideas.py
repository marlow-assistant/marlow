"""Marlow's ideas: offers it could act on, and Mike's taps on them
(docs/tasks/ideas-panel.md).

Two files with one writer each, so a tap can never race a turn:

- `ideas/ideas.json` is written by the model and only read here. It is
  validated whole, like `cron/schedules.json`: one bad entry refuses the store,
  and a refused store reads as empty and is reported to the model through the
  turn context.
- `ideas/ideas-responses.json` is written only by this module: accepted or
  dismissed, keyed by idea id.

An idea is *open* when it is not retired and has no response. Only open ideas
reach the phone.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.config import settings
from server.log import get_logger

logger = get_logger(__name__)

# Room for the job's ten open ideas, its twenty retired, and slack for a day
# of chat-side additions. A store past this is a job that stopped pruning.
MAX_ENTRIES = 60
SECTIONS = ("now", "standing")
_ID_RE = re.compile(r"[a-z0-9-]{1,40}\Z")
_DATE_FORMAT = "%Y-%m-%d"
# Generous over what the job's grammar asks for (a 60-word plan); these exist
# so a runaway write cannot put a novel on the phone, not to police style.
_MAX_CHARS = {"emoji": 16, "title": 200, "plan": 1200, "category": 40, "source": 300}
_REQUIRED = ("id", "section", "emoji", "title", "plan", "category", "added")
_ALLOWED = frozenset(_REQUIRED) | {"source", "until", "spotlight", "retired"}

MAX_REASON_CHARS = 500

# `[` and `]` delimit the context block an accepted idea is quoted into, and a
# newline would let quoted text start a line of its own.
_UNSAFE_INLINE = re.compile(r"[\x00-\x1f\x7f-\x9f\[\]]")


class IdeaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Idea:
    id: str
    section: str
    emoji: str
    title: str
    plan: str
    category: str
    added: str
    source: str | None = None
    until: str | None = None
    spotlight: bool = False
    retired: bool = False

    def to_client(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "section": self.section,
            "emoji": self.emoji,
            "title": self.title,
            "plan": self.plan,
            "category": self.category,
            "source": self.source,
            "until": self.until,
            "added": self.added,
            "spotlight": self.spotlight,
        }


def _text(raw: dict[str, Any], key: str, idea_id: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IdeaValidationError(f"idea {idea_id!r}: {key} must be a non-empty string")
    limit = _MAX_CHARS.get(key)
    if limit is not None and len(value) > limit:
        raise IdeaValidationError(f"idea {idea_id!r}: {key} is over {limit} characters")
    return value


def _date(raw: dict[str, Any], key: str, idea_id: str) -> str:
    value = raw.get(key)
    try:
        if not isinstance(value, str):
            raise ValueError
        datetime.strptime(value, _DATE_FORMAT)
    except ValueError:
        raise IdeaValidationError(f"idea {idea_id!r}: {key} must be a YYYY-MM-DD date") from None
    return value


def _parse_idea(raw: Any) -> Idea:
    if not isinstance(raw, dict):
        raise IdeaValidationError("each idea must be an object")
    idea_id = raw.get("id")
    if not isinstance(idea_id, str) or not _ID_RE.fullmatch(idea_id):
        raise IdeaValidationError(f"invalid idea id {idea_id!r} (want [a-z0-9-], up to 40)")
    unknown = set(raw) - _ALLOWED
    if unknown:
        raise IdeaValidationError(f"idea {idea_id!r} has unsupported keys: {sorted(unknown)!r}")
    missing = [key for key in _REQUIRED if key not in raw]
    if missing:
        raise IdeaValidationError(f"idea {idea_id!r} is missing {missing!r}")

    section = raw["section"]
    if section not in SECTIONS:
        raise IdeaValidationError(f"idea {idea_id!r}: section must be one of {SECTIONS!r}")

    source = _text(raw, "source", idea_id) if raw.get("source") is not None else None
    # Grounding is the defence against plausible filler: a "for you now" idea
    # that cannot name what it is about is the failure the trial exists to
    # catch, so the store refuses it rather than trusting the prompt.
    if section == "now" and source is None:
        raise IdeaValidationError(f"idea {idea_id!r}: a 'now' idea must name its source")

    spotlight = raw.get("spotlight", False)
    if not isinstance(spotlight, bool):
        raise IdeaValidationError(f"idea {idea_id!r}: spotlight must be true or false")

    retired = raw.get("retired")
    if retired is not None:
        if not isinstance(retired, dict) or set(retired) != {"on", "why"}:
            raise IdeaValidationError(f"idea {idea_id!r}: retired must be {{on, why}}")
        _date(retired, "on", idea_id)
        if not isinstance(retired["why"], str) or not retired["why"].strip():
            raise IdeaValidationError(f"idea {idea_id!r}: retired.why must be a non-empty string")

    return Idea(
        id=idea_id,
        section=section,
        emoji=_text(raw, "emoji", idea_id),
        title=_text(raw, "title", idea_id),
        plan=_text(raw, "plan", idea_id),
        category=_text(raw, "category", idea_id),
        added=_date(raw, "added", idea_id),
        source=source,
        until=_date(raw, "until", idea_id) if raw.get("until") is not None else None,
        spotlight=spotlight,
        retired=retired is not None,
    )


def _parse_store(raw: Any) -> tuple[Idea, ...]:
    if not isinstance(raw, dict) or set(raw) != {"ideas"}:
        raise IdeaValidationError('store must be exactly {"ideas": [...]}')
    if not isinstance(raw["ideas"], list):
        raise IdeaValidationError("ideas must be a list")
    if len(raw["ideas"]) > MAX_ENTRIES:
        raise IdeaValidationError(f"idea cap is {MAX_ENTRIES}; prune retired ideas")
    ideas = tuple(_parse_idea(entry) for entry in raw["ideas"])
    ids = [idea.id for idea in ideas]
    if len(set(ids)) != len(ids):
        raise IdeaValidationError("idea ids must be unique")
    # Live ideas only: a retired one that kept its flag is history, and
    # refusing the whole store over it would blank the panel for a slip that
    # changes nothing on screen.
    spotlit = [idea.id for idea in ideas if idea.spotlight and not idea.retired]
    if len(spotlit) > 1:
        raise IdeaValidationError(f"at most one idea may be spotlit; found {spotlit!r}")
    return ideas


def load(
    path: str | os.PathLike[str] | None = None, *, quiet: bool = False
) -> tuple[Idea, ...] | None:
    """The validated store, or None when absent or refused. Never raises.

    Absent is quiet (a box that has never run the job). Refused is an ERROR,
    except on `quiet` reads: the phone reads the store on every foreground,
    and one bad write would otherwise log the same ERROR all day. The turn
    context reports it once a turn instead, and tells the model."""
    store_path = Path(path) if path is not None else settings.ideas_path
    if not store_path.exists():
        return None
    try:
        return _parse_store(json.loads(store_path.read_text(encoding="utf-8")))
    except Exception as exc:
        if not quiet:
            logger.error("ideas store rejected at %s: %s", store_path, exc)
        return None


def rejection_reason(path: str | os.PathLike[str] | None = None) -> str | None:
    """Why the store is refused, or None if it is fine or absent."""
    store_path = Path(path) if path is not None else settings.ideas_path
    try:
        if not store_path.exists():
            return None
        _parse_store(json.loads(store_path.read_text(encoding="utf-8")))
    except Exception as exc:
        return str(exc)
    return None


class ResponseStore:
    """Mike's taps, one record per idea id. The only writer of its file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path_override = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        # Resolved per call, not captured at import: tests (and a deploy that
        # moves the working dir) swap `settings`, and a captured path would
        # quietly keep writing the old one.
        return self._path_override or settings.ideas_responses_path

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            # Corrupt means the history is lost, not that taps stop working:
            # the next write starts it again.
            logger.exception("ideas responses unreadable at %s; treating as empty", self.path)
            return {}
        responses = raw.get("responses") if isinstance(raw, dict) else None
        return responses if isinstance(responses, dict) else {}

    def _write(self, responses: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: the model reads this file, and a half-written one
        # would read as no responses at all, re-offering everything declined.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ideas-responses-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"responses": responses}, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return self._read()

    def record(self, idea_id: str, status: str, reason: str | None = None) -> None:
        with self._lock:
            responses = self._read()
            responses[idea_id] = {
                "status": status,
                "reason": reason,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._write(responses)


response_store = ResponseStore()


def open_ideas() -> list[Idea]:
    """Live ideas with no response yet, in store order."""
    ideas = load(quiet=True)
    if not ideas:
        return []
    answered = response_store.all()
    return [idea for idea in ideas if not idea.retired and idea.id not in answered]


def find_open(idea_id: str) -> Idea | None:
    return next((idea for idea in open_ideas() if idea.id == idea_id), None)


def clean_reason(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = " ".join(raw.split())[:MAX_REASON_CHARS]
    return text or None


def dismiss(idea_id: str, reason: str | None) -> bool:
    """Record a dismissal. True on success, False when the id is not open and
    not already dismissed. Repeatable, so "Not now" is one tap and the reason
    can follow in a second call."""
    previous = response_store.all().get(idea_id)
    if previous is None:
        if find_open(idea_id) is None:
            return False
    elif previous.get("status") != "dismissed":
        return False
    response_store.record(idea_id, "dismissed", clean_reason(reason))
    return True


def accept(idea_id: str) -> None:
    """Record an acceptance. Called only once the turn is queued, so a send
    that fails leaves the card open for the retry."""
    response_store.record(idea_id, "accepted")


def _inline(text: str) -> str:
    return " ".join(_UNSAFE_INLINE.sub(" ", text).split())


def accepted_context(idea: Idea) -> str:
    """The line that tells the turn which idea Mike tapped "Do it" on.

    The plan is the model's own earlier words, but they were written from
    email and web pages, so they are framed as what was offered, never as an
    instruction from Mike; his only instruction is the tap."""
    return (
        f"[Idea: the user tapped Do it on your idea `{idea.id}`,"
        f" \"{_inline(idea.title)}\". The plan you offered: {_inline(idea.plan)}"
        f" The server has recorded the acceptance; do not edit ideas/ideas-responses.json.]"
    )


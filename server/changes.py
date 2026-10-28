"""What changed, for the user (docs/tasks/whats-new.md).

`changes.json` at the repo root is the one list. Each entry says which half it
describes: `server` entries are served from here, so they appear only once the
box runs that code; `app` entries are bundled into each client at build time,
so a client-only install brings its own notes without a box deploy. An app
built from a newer `main` than its box must not announce server behaviour the
box isn't running yet, which is why the box serves only its own half.

Facts only: the category is a key, and its label is the client's wording.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from server.log import get_logger

logger = get_logger(__name__)

CHANGES_PATH = Path(__file__).resolve().parent.parent / "changes.json"

PARTS = frozenset({"server", "app"})
# The policy's categories, in the order the doc lists them. The first five are
# the always-tell ones.
CATEGORIES = frozenset({"access", "unattended", "data", "personality", "removed", "feature", "fix"})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEYS = {"id", "date", "part", "category", "text"}


class ChangesError(ValueError):
    pass


def parse(raw: Any) -> list[dict[str, Any]]:
    """The validated entries, newest first. Strict: tests run this over the
    real file, so a malformed entry fails the suite rather than the box."""
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise ChangesError("changes.json needs an 'entries' list")
    seen: set[int] = set()
    entries: list[dict[str, Any]] = []
    for entry in raw["entries"]:
        if not isinstance(entry, dict) or set(entry) != _KEYS:
            raise ChangesError(f"entry must have exactly {sorted(_KEYS)}: {entry!r}")
        entry_id = entry["id"]
        # bool is an int in Python; an id of `true` is a typo, not entry 1.
        if not isinstance(entry_id, int) or isinstance(entry_id, bool) or entry_id < 1:
            raise ChangesError(f"id must be a positive integer: {entry_id!r}")
        if entry_id in seen:
            raise ChangesError(f"duplicate id {entry_id}")
        seen.add(entry_id)
        if not isinstance(entry["date"], str) or not _DATE.match(entry["date"]):
            raise ChangesError(f"entry {entry_id}: date must be YYYY-MM-DD")
        if entry["part"] not in PARTS:
            raise ChangesError(f"entry {entry_id}: part must be one of {sorted(PARTS)}")
        if entry["category"] not in CATEGORIES:
            raise ChangesError(f"entry {entry_id}: unknown category {entry['category']!r}")
        if not isinstance(entry["text"], str) or not entry["text"].strip():
            raise ChangesError(f"entry {entry_id}: text must be non-empty")
        entries.append(dict(entry))
    return sorted(entries, key=lambda e: e["id"], reverse=True)


def load(path: Path = CHANGES_PATH) -> list[dict[str, Any]]:
    """Never raises: a broken changelog must not stop the server starting.
    It costs the user a card, which the suite's check on the real file is
    there to prevent."""
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        logger.error("changes.json unreadable, serving no changes: %s", e)
        return []


# Frozen at import, like /health's build identity: the list describes the code
# this process loaded, not a checkout that moved on without a restart.
_ENTRIES = load()


def server_entries() -> list[dict[str, Any]]:
    return [dict(e) for e in _ENTRIES if e["part"] == "server"]


# The marker identity/CLAUDE.md keys first-run onboarding on: FIRST-RUN.md ends
# by replacing it. A stopgap, not a design: see the loose end on
# `get_changes` in app.py. The clients have no onboarding state of their own, so the
# box reports it and they hold the update card back until it is gone.
_ONBOARDING_MARKER = "Not yet known"


def onboarding_complete(user_path: Path) -> bool:
    """False only while USER.md still carries the starter's marker. Fails open
    (True): an unreadable file costs a card shown early, never one withheld
    forever."""
    try:
        return _ONBOARDING_MARKER not in user_path.read_text(encoding="utf-8")
    except OSError:
        return True

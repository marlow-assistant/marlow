"""Composes Marlow's hand-authored identity into injectable context.

docs/tasks/loader-identity-injection.md: recall was flaky on v1 because "read
SOUL/USER/memory" was a discretionary model choice that lost to answer-
momentum. The fix is to inject the bounded, almost-always-relevant identity
(SOUL + USER + memory) into every `claude -p` call instead of asking the model
to go fetch it. CLAUDE.md is deliberately excluded here — a clean-shell spike
confirmed `claude -p` auto-loads a cwd CLAUDE.md on its own, so injecting it
again would double it up in context.

A missing SOUL.md/USER.md is a real bug (a Marlow without its SOUL is not a
degraded-but-fine state), so those two are fail-loud. An empty/missing
memory/ is a warning, not fatal — a fresh identity has no memories yet.

`build_identity_context()` always reads fresh off disk. Callers wanting the
per-session freeze (docs/tasks/cache-friendly-identity-injection.md) should
use `get_identity_context()` instead — server/invoke.py does.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from server.log import get_logger

logger = get_logger(__name__)

# docs/tasks/cache-friendly-identity-injection.md: freeze the composed string
# for the life of a session so a mid-session memory write doesn't invalidate
# `--append-system-prompt`'s cache block (and everything after it, incl.
# history) on every subsequent turn. Keyed by session_id; in-memory only — a
# server restart is itself effectively a new session boundary, so losing this
# on restart is acceptable (build_identity_context's caller falls back to a
# fresh read, gate1-results §8 confirms a miss costs one turn, not ongoing).
_session_cache: dict[str, tuple[float, str]] = {}


def _reset_session_cache_for_tests() -> None:
    """Test-only. Production code never needs to clear this."""
    _session_cache.clear()


def _prune_expired(now: float, max_age: float) -> None:
    """Bounds _session_cache's growth. A session that's cached once and never
    resumed again would otherwise sit forever — this sweeps every entry older
    than max_age whenever a cache miss happens elsewhere, so the dict stays
    roughly the size of "sessions touched in the last max_age window" instead
    of growing for the life of the process."""
    expired = [sid for sid, (cached_at, _) in _session_cache.items() if now - cached_at > max_age]
    for sid in expired:
        del _session_cache[sid]

# inject_all puts every memory/ file's full text in the block; index_only puts
# one line per entry and lets Marlow Read the bodies it needs. index_only exists
# because at 156KB the inject_all block measured 58,244 tokens — ~29% of a 200K
# window on every turn (gate1-results §8b). Caching makes that cheap but not
# small, and attention dilution doesn't care about cache state.
# Spec + the byte-stability constraint both modes must satisfy:
# docs/tasks/memory-index-split.md.
MEMORY_MODE_INJECT_ALL = "inject_all"
MEMORY_MODE_INDEX_ONLY = "index_only"
SUPPORTED_MEMORY_MODES = (MEMORY_MODE_INJECT_ALL, MEMORY_MODE_INDEX_ONLY)

# Index lines are bounded so one long-winded memory can't quietly re-inflate the
# block this mode exists to shrink.
_MAX_DESCRIPTION_CHARS = 200

# Enough to reach a description in any sanely-formatted memory file. index_only
# must not become an O(all memory) disk read per invocation, which is what
# reading every body in full would make it.
_DESCRIPTION_SCAN_BYTES = 4096


class IdentityLoadError(Exception):
    """Raised when a required identity file is missing or unreadable."""


def _read_required(path: Path, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise IdentityLoadError(
            f"{label} is required but could not be read at {path}: {e}"
        ) from e
    if not text.strip():
        raise IdentityLoadError(f"{label} at {path} is empty")
    return text


def _decode_prefix(raw: bytes) -> str:
    """Decodes a byte prefix that may have been cut mid-codepoint.

    Trims up to three trailing bytes to recover a split multi-byte character;
    anything still undecodable after that is a genuinely bad file, and the
    UnicodeDecodeError propagates to the caller's skip-and-warn.
    """
    for trim in range(4):
        candidate = raw[: len(raw) - trim] if trim else raw
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8")  # re-raises, with the original bytes


def _extract_description(text: str) -> str:
    """One stable line describing a memory entry, for the index.

    Prefers a frontmatter `description:` field. Falls back to the first
    non-heading line of the body so that un-migrated memory files still get a
    usable index line rather than vanishing from it — the fallback is lower
    quality, not a failure, and `scripts/memory_index_lint.py` reports which
    files are relying on it.

    Whitespace is collapsed and the result truncated: an index line must stay
    one line and stay bounded, whatever the file does.
    """
    lines = text.splitlines()
    body_start = 0

    if lines and lines[0].strip() == "---":
        # An opening fence was seen, so line 0 is never a description — skip it
        # even if no closing fence turns up. Two ways that happens: the file
        # opens with a horizontal rule rather than frontmatter, or (in
        # index_only, which reads a bounded prefix) the block runs past the
        # scan window. Without this the fallback returns the literal "---",
        # which is worse than no description at all: the lint reports it as
        # present and Marlow cannot route on it.
        body_start = 1
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body_start = i + 1
                break
            # Top-level scalar only: a nested `description:` under `metadata:`
            # is a different field and must not be picked up.
            if line.startswith("description:"):
                described = line[len("description:") :].strip().strip("\"'")
                if described:
                    return _clamp(described)

    for line in lines[body_start:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not _is_horizontal_rule(stripped):
            return _clamp(stripped)

    return ""


# `---`, `----`, `***`, `___` … a markdown horizontal rule is never a
# description. Skipping only the exact opening `---` fence would still let a
# four-dash rule through as the index line.
_HRULE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")


def _is_horizontal_rule(stripped: str) -> bool:
    return bool(_HRULE.match(stripped.replace(" ", "")))


def _clamp(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_DESCRIPTION_CHARS:
        return collapsed
    return collapsed[: _MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"


def _read_memory_entries(memory_dir: Path, mode: str) -> list[tuple[str, str]]:
    if mode not in SUPPORTED_MEMORY_MODES:
        raise NotImplementedError(
            f"memory_mode={mode!r} is not implemented yet "
            f"(supported: {SUPPORTED_MEMORY_MODES}) — see "
            "docs/tasks/loader-identity-injection.md"
        )

    if not memory_dir.is_dir():
        logger.warning("memory dir %s does not exist — injecting no memory", memory_dir)
        return []

    entries: list[tuple[str, str]] = []
    for entry_path in sorted(p for p in memory_dir.iterdir() if p.is_file()):
        try:
            if mode == MEMORY_MODE_INDEX_ONLY:
                with entry_path.open("rb") as fh:
                    text = _decode_prefix(fh.read(_DESCRIPTION_SCAN_BYTES))
            else:
                text = entry_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # One bad file (stray binary, .DS_Store, wrong encoding) must not
            # take down every future invocation — skip and warn instead of
            # raising, matching the "empty/missing memory is a warning" intent.
            logger.warning("skipping unreadable memory entry %s: %s", entry_path, e)
            continue
        if not text.strip():
            continue
        if mode == MEMORY_MODE_INDEX_ONLY:
            entries.append((entry_path.name, _extract_description(text)))
        else:
            entries.append((entry_path.name, text))

    if not entries:
        logger.warning("memory dir %s has no entries — injecting no memory", memory_dir)

    return entries


# The index has to say out loud that the bodies exist and are worth fetching.
# The v1 lesson (docs/tasks/loader-identity-injection.md) is that a *discretionary*
# read loses to answer-momentum, and an index that merely lists filenames is
# exactly such a read. Naming the expectation is the cheapest available
# counterweight; whether it holds is what the first days of real use will say.
def _index_preamble(memory_label: str) -> str:
    return (
        "These are your memories. Only the descriptions are here — the full text "
        f"of each is in `{memory_label}`. When a line below is relevant to what "
        "you have been asked, read that file before answering rather than "
        "working from the description alone."
    )


def memory_label(memory_dir: Path, working_dir: Path) -> str:
    """How the index should refer to the memory directory.

    The preamble used to hardcode `memory/`, but MARLOW_MEMORY_DIR is
    independently configurable while `invoke.py` runs the CLI with
    cwd=working_dir. If the two were ever set apart on the box, the index would
    name a path that doesn't exist and every memory body would be silently
    unreachable — no error anywhere, because the preamble is static text.

    Pure path math, no I/O, so it stays byte-stable for a given deployment.
    """
    try:
        return str(memory_dir.relative_to(working_dir)) + "/"
    except ValueError:
        # Configured outside the working tree — an absolute path is the only
        # thing that would actually resolve from the CLI's cwd.
        return str(memory_dir)


def compose_identity_context(
    soul: str,
    user: str,
    memory_entries: list[tuple[str, str]],
    memory_mode: str = MEMORY_MODE_INJECT_ALL,
    memory_label: str = "memory/",
) -> str:
    """Pure composition — no file I/O, so it's unit-testable without a filesystem.

    Fixed section order: SOUL, then USER, then MEMORY entries (sorted by
    filename, the order _read_memory_entries already produces). The second
    element of each entry is its full text under inject_all and its one-line
    description under index_only.

    The rendering is a stable interface, not an implementation detail: it sits
    at the front of the cache prefix, so changing it invalidates the block and
    all conversation history behind it (gate1-results §8a). Change it
    deliberately or not at all.
    """
    sections = [
        f"# SOUL\n\n{soul.strip()}",
        f"# USER\n\n{user.strip()}",
    ]
    if memory_entries:
        if memory_mode == MEMORY_MODE_INDEX_ONLY:
            index_lines = "\n".join(
                f"- `{name}` — {description}" if description else f"- `{name}`"
                for name, description in memory_entries
            )
            sections.append(f"# MEMORY\n\n{_index_preamble(memory_label)}\n\n{index_lines}")
        else:
            memory_sections = "\n\n".join(
                f"## {name}\n\n{text.strip()}" for name, text in memory_entries
            )
            sections.append(f"# MEMORY\n\n{memory_sections}")

    return "\n\n".join(sections)


def build_identity_context(settings) -> str:
    """Orchestrator: reads the configured identity files off disk and composes
    them. Raises IdentityLoadError if SOUL.md/USER.md are missing or empty.
    """
    soul = _read_required(settings.soul_path, "SOUL.md")
    user = _read_required(settings.user_path, "USER.md")
    memory_entries = _read_memory_entries(settings.memory_dir, settings.memory_mode)
    return compose_identity_context(
        soul,
        user,
        memory_entries,
        settings.memory_mode,
        memory_label(settings.memory_dir, settings.working_dir),
    )


def get_identity_context(
    settings,
    *,
    session_id: str | None,
    resume: bool,
) -> str:
    """Session-aware wrapper around build_identity_context — this is what
    callers (server/invoke.py) should use.

    resume=False (first turn of a new conversation, or the `--continue`-hint
    case where session_id isn't a known id to key on) always reads fresh.
    resume=True with a session_id reuses the string frozen at that session's
    first call, unless it has aged past
    settings.identity_freeze_max_age_seconds — then it's treated as a normal
    cache miss and recomputed/re-stored.
    """
    if not (resume and session_id):
        return build_identity_context(settings)

    now = time.monotonic()
    cached = _session_cache.get(session_id)
    if cached is not None:
        cached_at, text = cached
        if now - cached_at <= settings.identity_freeze_max_age_seconds:
            return text

    text = build_identity_context(settings)
    _prune_expired(now, settings.identity_freeze_max_age_seconds)
    _session_cache[session_id] = (now, text)
    return text

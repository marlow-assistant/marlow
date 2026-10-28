#!/usr/bin/env python3
"""Previews the index_only memory block for a real memory/ directory.

Run this on the deploy box *before* switching MARLOW_MEMORY_MODE to
index_only. It prints the exact index Marlow would be given, flags entries
falling back to their first body line instead of a frontmatter
`description:`, and reports the size change against inject_all.

Read-only. To backfill missing `description:` frontmatter, use
`scripts/memory_write_descriptions.py`, which authors them via the API — then
come back here to check the result.

    python3 scripts/memory_index_lint.py /home/marlow/marlow-identity/memory

Exit codes: 0 only when there is at least one entry and *every* entry has a
frontmatter `description:`; 1 if any entry is missing one, whether it indexes
as a bare filename (no usable description at all) or falls back to its first
body line; 2 for a usage error — the directory doesn't exist, or it exists but
yielded no readable entries at all, which means nothing was checked rather than
everything passed.

The fallback is deliberately a failure here even though it is not a failure at
load time. A first-body-line index reads as a real description while being
whatever that file happened to open with, so it is the one outcome that looks
migrated without being migrated. Failing the lint is what makes this tool a
gate on the mode flip rather than a report you can skim past. The full report
still prints either way, so a mid-migration preview costs nothing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.identity import (  # noqa: E402
    _DESCRIPTION_SCAN_BYTES,
    MEMORY_MODE_INDEX_ONLY,
    MEMORY_MODE_INJECT_ALL,
    _decode_prefix,
    _read_memory_entries,
    compose_identity_context,
)


def _scanned_prefix(path: Path) -> str:
    """The same bounded prefix the index itself is built from.

    Reading the whole file here would make the lint report on text the loader
    never sees: a `description:` sitting past the scan window would be reported
    as present while the real index line fell back to the first body line. That
    mismatch is exactly what this tool exists to catch before the mode flip.
    """
    try:
        with path.open("rb") as fh:
            return _decode_prefix(fh.read(_DESCRIPTION_SCAN_BYTES))
    except (OSError, UnicodeDecodeError):
        return ""


def _plural(n: int, singular: str, plural: str) -> str:
    """This report is the migration's only feedback loop and gets read while
    Mike is mid-task on the box — "1 entries fall back to their first body
    line" is the kind of wrong that makes a tool feel unfinished."""
    return singular if n == 1 else plural


def _has_frontmatter_description(path: Path) -> bool:
    lines = _scanned_prefix(path).splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if line.startswith("description:"):
            return bool(line[len("description:") :].strip().strip("\"'"))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("memory_dir", type=Path)
    args = parser.parse_args()

    if not args.memory_dir.is_dir():
        print(f"error: {args.memory_dir} is not a directory", file=sys.stderr)
        return 2

    index_entries = _read_memory_entries(args.memory_dir, MEMORY_MODE_INDEX_ONLY)
    full_entries = _read_memory_entries(args.memory_dir, MEMORY_MODE_INJECT_ALL)

    if not index_entries:
        # Not "everything passed" — nothing was checked. This is a gate on the
        # mode flip, and passing on zero evidence is worse than not running at
        # all. The dir exists but yielded no entries: almost always a path one
        # level off, an unpopulated memory/, or files `_read_memory_entries`
        # skipped (binary, non-UTF-8, .DS_Store, whitespace-only) — each of
        # which warns to stderr, where the old green verdict then contradicted
        # it. Exit 2 rather than 1, matching the missing-directory case: this
        # is "you pointed me somewhere wrong", not "your memory needs
        # migrating".
        print(
            f"error: {args.memory_dir} has no readable memory entries — nothing to "
            "check. Wrong path, or every file was skipped (see warnings above).",
            file=sys.stderr,
        )
        return 2

    fallbacks: list[str] = []
    missing: list[str] = []
    for name, description in index_entries:
        if not description:
            missing.append(name)
        elif not _has_frontmatter_description(args.memory_dir / name):
            fallbacks.append(name)

    index_block = compose_identity_context("", "", index_entries, MEMORY_MODE_INDEX_ONLY)
    full_block = compose_identity_context("", "", full_entries, MEMORY_MODE_INJECT_ALL)

    print("=== index Marlow would receive ===")
    print(index_block)
    print()
    print("=== report ===")
    print(f"entries:            {len(index_entries)}")
    print(f"index block:        {len(index_block):,} chars")
    print(f"inject_all block:   {len(full_block):,} chars")
    if full_block:
        shrink = 100 * (1 - len(index_block) / len(full_block))
        print(f"reduction:          {shrink:.1f}%")

    if fallbacks:
        n = len(fallbacks)
        print()
        print(f"{n} {_plural(n, 'entry falls', 'entries fall')} back to "
              f"{_plural(n, 'its', 'their')} first body line "
              f"(add a frontmatter `description:` to {_plural(n, 'it', 'these')}):")
        # Print the line the index actually got, not a re-derivation.
        described = dict(index_entries)
        for name in fallbacks:
            print(f"  - {name} — {described[name]}")

    if missing:
        n = len(missing)
        print()
        print(f"{n} {_plural(n, 'entry has', 'entries have')} NO usable description — "
              f"{_plural(n, 'it indexes', 'they index')} as a bare filename:")
        for name in missing:
            print(f"  - {name}")

    if fallbacks or missing:
        bad = len(fallbacks) + len(missing)
        total = len(index_entries)
        print()
        print(f"FAIL: {bad} of {total} {_plural(total, 'entry', 'entries')} "
              f"{_plural(bad, 'lacks', 'lack')} a frontmatter `description:`. Run "
              "scripts/memory_write_descriptions.py, then re-run this.")
        return 1

    n = len(index_entries)
    print()
    print(f"OK: {_plural(n, 'the 1 entry has', f'all {n} entries have')} "
          "a frontmatter `description:`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

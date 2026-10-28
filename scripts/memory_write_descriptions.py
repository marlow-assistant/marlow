#!/usr/bin/env python3
"""Authors frontmatter `description:` lines for memory files, using the API.

The index_only memory mode (docs/tasks/memory-index-split.md) routes on one
line per memory entry. Files without a frontmatter `description:` fall back to
their first body line, which is usually a worse index line than a written one.
This backfills them.

    export ANTHROPIC_API_KEY=...
    # 1. see what it would write, without touching anything:
    python3 scripts/memory_write_descriptions.py MEMORY_DIR
    # 2. apply, after reading the proposals:
    python3 scripts/memory_write_descriptions.py MEMORY_DIR --write --i-understand-this-sends-memory-content-to-the-api

**This sends memory file content to the Anthropic API.** That is the whole
mechanism — there is no local model here. The content is personal, so the flag
above is required and there is no way to skip it. Nothing is written to this
repo: the script reads and writes only under MEMORY_DIR, which lives on the
deploy box in the private identity repo.

Uses the API, not `claude -p`. That does not conflict with the CLI-only rule in
CLAUDE.md, which exists so *the server's runtime path* bills against the Pro
subscription. This is a one-shot offline migration billed to API credits, run
by hand, and nothing under server/ imports it. Keep it that way.

Idempotent: files that already have a frontmatter `description:` are skipped,
so a re-run after adding new memories only touches the new ones.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.identity import _clamp  # noqa: E402

MODEL = "claude-haiku-4-5"

# Enough to judge what a memory is about. Bounds cost and keeps a long memory
# from crowding the request.
MAX_INPUT_CHARS = 6000

SYSTEM = """\
You write one-line index descriptions for an AI assistant's memory files.

The assistant sees only these lines by default — it reads a memory's full text
only when a line looks relevant. So the line's whole job is to let the
assistant decide "is this file worth opening for what I've just been asked?"

Write in the assistant's own register: plain, specific, no marketing. Describe
what the memory is *about* and what it would settle, not what kind of document
it is. State the substance — a line that names the conclusion is far more
useful than one that names the topic.

Good:   macOS CI job burns 10x quota minutes vs wall minutes; 100% quota stops all CI
Good:   /code-review and /security-review are blocked for model-invocation at any level
Bad:    A note about CI minutes
Bad:    This memory file contains important information about code review

One line. No trailing period. No leading "Memory about" or "This file".\
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "One-line index description, under 200 characters.",
        }
    },
    "required": ["description"],
    "additionalProperties": False,
}


def _split_frontmatter(text: str) -> tuple[list[str], int]:
    """Returns (frontmatter lines excluding the fences, index of closing fence).

    Returns ([], -1) when the file has no frontmatter block.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return [], -1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[1:i], i
    return [], -1


def _has_description(text: str) -> bool:
    front, _ = _split_frontmatter(text)
    return any(
        line.startswith("description:") and line[len("description:") :].strip().strip("\"'")
        for line in front
    )


def _insert_description(text: str, description: str) -> str:
    """Adds `description:` to the file's frontmatter, creating a block if needed."""
    # A description containing a colon or quote would break naive YAML; quote
    # and escape so the written file stays parseable.
    value = '"' + description.replace("\\", "\\\\").replace('"', '\\"') + '"'
    entry = f"description: {value}"

    lines = text.splitlines()
    _, close_idx = _split_frontmatter(text)
    if close_idx == -1:
        return "---\n" + entry + "\n---\n\n" + text.lstrip("\n")

    # After `name:` if present, else at the top of the block — keeps the shape
    # consistent with the memory files that already have frontmatter.
    insert_at = 1
    for i, line in enumerate(lines[1:close_idx], start=1):
        if line.startswith("name:"):
            insert_at = i + 1
            break
    lines.insert(insert_at, entry)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _describe(client, name: str, body: str) -> str:
    message = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": f"Memory file `{name}`:\n\n<memory>\n{body}\n</memory>",
            }
        ],
    )
    if message.stop_reason == "refusal":
        raise RuntimeError(f"model declined to describe {name}")

    import json

    text = next(b.text for b in message.content if b.type == "text")
    return _clamp(json.loads(text)["description"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("memory_dir", type=Path)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually modify the files. Without it, proposals are printed only.",
    )
    parser.add_argument(
        "--i-understand-this-sends-memory-content-to-the-api",
        dest="consented",
        action="store_true",
        help="Required with --write. Acknowledges the content leaves the box.",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Process at most N files (0 = all)."
    )
    args = parser.parse_args()

    if not args.memory_dir.is_dir():
        print(f"error: {args.memory_dir} is not a directory", file=sys.stderr)
        return 2
    if args.write and not args.consented:
        print(
            "error: --write requires "
            "--i-understand-this-sends-memory-content-to-the-api",
            file=sys.stderr,
        )
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2

    try:
        import anthropic
    except ImportError:
        print("error: pip install anthropic", file=sys.stderr)
        return 2

    client = anthropic.Anthropic()

    todo = []
    for path in sorted(p for p in args.memory_dir.iterdir() if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"skip {path.name}: unreadable ({e})")
            continue
        if not text.strip():
            print(f"skip {path.name}: empty")
            continue
        if _has_description(text):
            print(f"skip {path.name}: already has a description")
            continue
        todo.append((path, text))

    if args.limit:
        todo = todo[: args.limit]

    if not todo:
        print("nothing to do — every entry already has a description")
        return 0

    print(f"\n{len(todo)} file(s) need a description. Model: {MODEL}\n")
    failures = 0
    for path, text in todo:
        try:
            description = _describe(client, path.name, text[:MAX_INPUT_CHARS])
        except anthropic.RateLimitError as e:
            print(f"FAIL {path.name}: rate limited ({e}) — re-run to continue")
            failures += 1
            continue
        except Exception as e:  # noqa: BLE001
            # Deliberately broad. A truncated response (stop_reason=max_tokens
            # cutting the schema JSON mid-string) raises JSONDecodeError, a
            # content-less response raises StopIteration, an off-schema one
            # raises KeyError — none of which are API errors, and any of which
            # would otherwise abort the whole run partway through the list and
            # break this script's "re-run to retry just those" contract.
            print(f"FAIL {path.name}: {type(e).__name__}: {e}")
            failures += 1
            continue

        print(f"{path.name}\n    {description}")
        if args.write:
            path.write_text(_insert_description(text, description), encoding="utf-8")

    print()
    if args.write:
        print(f"wrote {len(todo) - failures} description(s). Review with:")
        print(f"    git -C {args.memory_dir.parent} diff")
    else:
        print("dry run — nothing written. Re-run with --write to apply.")
    if failures:
        print(f"{failures} file(s) failed; re-run to retry just those.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

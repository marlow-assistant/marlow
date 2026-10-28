"""Give an existing box's morning brief a weather section (docs/tasks/weather-mcp.md).

cron/morning.md is seeded as a STARTER and is the user's after that (update.sh,
SHIPPED_JOBS), so the seed's new Weather section only reaches new boxes. This
adds it once to the rest. It edits a file the agent owns, so it changes as
little as possible and never twice:

- Skipped for good if the prompt already mentions weather or a forecast. That
  is a box where the user (or Marlow) has already said something about it,
  including a fresh seed, which is what keeps this a no-op on new installs.
- Skipped for good if there is no cron/morning.md: the user removed the brief.
- Otherwise the section goes in just before the closing `---` rule that the
  template's prompt ends with, or at the end when there is none. Unnumbered,
  because a user's numbering is theirs and renumbering it is not this file's
  business.

Not deferred on a box without PREFERENCES.md, unlike 0003: the section names
the tool itself, and the tools reach every box through invoke.py whatever its
CLAUDE.md says.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

EXIT_DONE = 0

SECTION = """## Weather
Today where they are, from the weather tools (`forecast`), one or two lines,
plus anything notable in the next couple of days. Skip if there's no location
and no home town in memory.
"""

_ALREADY = re.compile(r"weather|forecast", re.IGNORECASE)


def with_weather(text: str) -> str:
    lines = text.splitlines(keepends=True)
    rule = max((i for i, line in enumerate(lines) if line.strip() == "---"), default=None)
    if rule is None:
        return text.rstrip("\n") + "\n\n" + SECTION
    head = "".join(lines[:rule]).rstrip("\n")
    return head + "\n\n" + SECTION + "\n" + "".join(lines[rule:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-dir", required=True, type=Path)
    # Part of the runner's interface; unused, as in 0001.
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args(argv)

    prompt = args.working_dir / "cron" / "morning.md"
    if not prompt.is_file():
        print("no cron/morning.md; nothing to add to")
        return EXIT_DONE
    text = prompt.read_text()
    if _ALREADY.search(text):
        print("morning brief already mentions weather; left alone")
        return EXIT_DONE

    # Temp file + replace, as for the schedule store: a torn write would cost
    # the user their whole brief, not just this section.
    fd, tmp = tempfile.mkstemp(dir=prompt.parent, prefix=".morning.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(with_weather(text))
        os.chmod(tmp, prompt.stat().st_mode & 0o777)
        os.replace(tmp, prompt)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print("added a Weather section to cron/morning.md")
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(main())

"""Static vetting of the path-taking tools — the non-Bash half of the hook's
allow fast path.

This exists because of a bug found on 2026-08-06, on the first real turn after
`deploy/marlow-settings.json` was installed: a single "add a note to memory"
produced **five** phone prompts, including a `Read` and an `Edit` that the
settings allow list explicitly covers.

The cause is the one `bash_policy.vet_bash_command`'s own comment already
records — the hook is consulted for allow-matched calls too (probe runs 4-5,
2026-08-05), and the hook's decision wins. So an `allow` entry in settings.json
buys nothing unless the hook independently agrees. `vet_bash_command` was the
fix for that, and it is gated on `tool_name == "Bash"`; every other tool fell
straight through to the phone regardless of what settings.json said.

The rule here is the same containment rule, minus the shell:

  - **Reading** is vetted anywhere inside the working tree. That mirrors
    `Read`/`Glob`/`Grep(/home/marlow/marlow-identity/**)`.
  - **Writing** is vetted under `memory/`, plus the named stores
    `cron/schedules.json` and `ideas/ideas.json`. The narrowness is load-bearing rather than
    conservative: `SOUL.md` and `USER.md` sit at the tree root, and
    `tests/test_marlow_settings.py::test_identity_files_are_not_writable_unattended`
    requires that rewriting Marlow's own character reach Mike's phone.

Like `vet_bash_command`, this ALLOWs or PROMPTs and never denies — a gap costs
a prompt, not a boundary.

`vet_pathless_tool`, at the bottom, is the same story told a third time: the
tools that carry no path matched neither vetter and reached the phone regardless
of settings.json (found on the box 2026-08-06). It lives here because it is the
other half of "everything that isn't Bash", not because it checks paths — it
checks that there aren't any.
"""
from __future__ import annotations

import os

from server.bash_policy import (
    ALLOW,
    PROMPT,
    anchor_to_tree,
    inside_working_tree,
    reaches_protected_path,
)
from server.boundaries import ATTACHMENT_ROOT, PATH_ARG_KEYS, VETTED_PATHLESS_TOOLS, WORKING_TREE

# Tools whose path argument is vetted for reading. Deliberately not
# `NotebookRead`/`NotebookEdit`. That used to be justified by the cron policy's
# `ask` list, where an entry auto-denies in headless AND overrides a hook allow
# (gate #1 §4) -- so vetting them here would have been a rule that quietly did
# nothing in the one context it mattered. That file is gone (2026-08-07, the
# unattended policy moved into server/active_turn.py), so the reasoning is now
# simply: nothing on this box uses notebooks, and a vetted-allow is a standing
# grant. Vetting them would widen the unattended surface for no use case. Add
# them the day a notebook exists here, not before.
READ_TOOLS: frozenset[str] = frozenset({"Read", "Glob", "Grep"})

# `Write` is here even though gate #2 §3 established that settings.json file
# rules do not match Write by name (its Edit rule covers Write's writes). That
# is a fact about settings.json matching, not about this module, which sees the
# real tool name and must recognise it or Marlow cannot create a new memory file
# without a prompt.
WRITE_TOOLS: frozenset[str] = frozenset({"Edit", "Write"})

# Tools that take an OPTIONAL path. Absent means "cwd", and invoke.py always
# launches `claude -p` with cwd=settings.working_dir. Read/Edit/Write have no
# such default: a missing file_path there is a malformed call and goes to the
# phone.
PATH_OPTIONAL_TOOLS: frozenset[str] = frozenset({"Glob", "Grep"})

WRITABLE_SUBTREE = "memory"
WRITABLE_NAMED_FILES: frozenset[str] = frozenset({"cron/schedules.json", "ideas/ideas.json"})

# `boundaries.PATH_ARG_KEYS` verbatim, never a local copy -- the copy that used
# to live here had drifted from the hook's by one key (`notebook_path`), and a
# key that is secret-scanned in one module and invisible in another is exactly
# the seam a path escapes through. Carrying `notebook_path` costs nothing: the
# notebook tools are rejected by the READ_TOOLS/WRITE_TOOLS test above long
# before _candidates() is reached.

# Glob's `pattern` is not a filter applied under `path` -- it decides on its own
# what gets enumerated, and it may be absolute. `Glob(pattern="~/.ssh/*")`
# carries no `path` key at all, so a check that reads only PATH_ARG_KEYS sees an
# argument-less call, concludes "defaults to cwd", and vets it (/code-review
# 2026-08-06). Grep's `glob` is included on the same reasoning rather than on
# evidence it can escape: the cost of a redundant check here is a prompt.
#
# Grep's `pattern` is a REGEX, not a path, and is deliberately absent -- it is
# also the one key here that routinely contains characters no path would, and
# resolving it would trade real prompts for no protection.
_PATTERN_KEYS = {"Glob": ("pattern",), "Grep": ("glob",)}


def _candidates(tool_name: str, tool_input: dict) -> list[str]:
    keys = PATH_ARG_KEYS + _PATTERN_KEYS.get(tool_name, ())
    return [
        value for key in keys
        if isinstance(value := tool_input.get(key), str) and value
    ]


def vet_file_tool(
    tool_name: str,
    tool_input: dict,
    working_tree: str | os.PathLike[str] = WORKING_TREE,
    attachment_roots: tuple[str, ...] = ATTACHMENT_ROOT,
) -> tuple[str, str]:
    """(ALLOW, reason) or (PROMPT, reason). Never denies.

    Callers must run the hard-deny checks first, exactly as with
    `vet_bash_command`. The secret/agent-config re-check below is belt and
    braces, not a substitute: the working tree genuinely contains a secret
    (gate #2 found a `.env` in the working dir, matched by SECRET_GLOBS), so
    "inside the tree" and "safe to read" are not the same set, and this is the
    one place where a single missed call ordering would hand it over.

    **Known residue, accepted deliberately.** A vetted `Glob` pattern is
    containment-checked as a literal string; what the tool then enumerates is
    not. A symlink inside the tree pointing out of it would list names from
    wherever it points -- the same expansion hazard that got glob characters
    removed from `VETTED_TOKEN_CHARS` in bash_policy. The line is drawn
    differently here because the payloads differ: `wc -l *` returned file
    *contents*, whereas Glob returns *names*, and opening any of those names is
    a `Read` that gets its own realpath containment check. Prompting on every
    Glob would be the alternative, and Glob is how Marlow finds its own memory.

    `attachment_roots` is a SECOND vetted-read area, outside the working tree
    entirely (docs/tasks/ios-image-input.md — an uploaded image lives in
    `settings.attachments_dir`, a sibling of `~/.marlow/apns/`, never under the
    working tree). Read only: an allow entry in settings.json is inert unless
    this hook independently agrees (the WebSearch/WebFetch lesson, 2026-08-06),
    and there is no per-invocation `--allowedTools` escape hatch for it either
    (probed the same day) -- so without this branch, every image send would
    wake the phone for a Read that was always going to be approved.
    """
    if tool_name not in READ_TOOLS and tool_name not in WRITE_TOOLS:
        return PROMPT, f"{tool_name} is not a vetted path-taking tool"

    tree = os.path.expanduser(str(working_tree))
    candidates = _candidates(tool_name, tool_input)
    if not candidates:
        if tool_name in PATH_OPTIONAL_TOOLS:
            return ALLOW, "vetted locally: no path argument, defaults to the working tree"
        return PROMPT, f"{tool_name} call carries no path argument"

    writable_root = os.path.join(tree, WRITABLE_SUBTREE)
    named_writes_normal = {
        os.path.normpath(os.path.join(tree, relative)) for relative in WRITABLE_NAMED_FILES
    }
    named_writes_real = {
        os.path.join(os.path.realpath(tree), relative) for relative in WRITABLE_NAMED_FILES
    }
    for value in candidates:
        resolved = anchor_to_tree(value, tree)
        if resolved is None:
            return PROMPT, f"path cannot be resolved: {value}"
        # Anchored AND symlink-resolved, both in reaches_protected_path(). The
        # symlink half is why: `memory/notes.md -> ../.env` satisfied every
        # other check and was vetted (/code-review 2026-08-06, third pass),
        # because is_secret_path resolves symlinks for its SECRET_PATHS half and
        # fnmatches the raw string for its SECRET_GLOBS half. `.env` is the case
        # that matters -- gate #2 found a real one in the working tree, holding
        # a live token.
        if reaches_protected_path(value, tree):
            return PROMPT, "not vetted locally: secret or agent-config path"
        in_tree = inside_working_tree(resolved, tree)
        in_attachments = tool_name == "Read" and any(
            inside_working_tree(resolved, root) for root in attachment_roots
        )
        if not in_tree and not in_attachments:
            return PROMPT, f"path resolves outside the working tree: {value}"
        if (
            tool_name in WRITE_TOOLS
            and not inside_working_tree(resolved, writable_root)
            and not (
                os.path.normpath(resolved) in named_writes_normal
                and os.path.realpath(resolved) in named_writes_real
            )
        ):
            return PROMPT, (
                f"write outside {WRITABLE_SUBTREE}/ and named writable files: {value}"
            )

    if tool_name in WRITE_TOOLS:
        return ALLOW, f"vetted locally: write inside {WRITABLE_SUBTREE}/ or a named writable file"
    return ALLOW, "vetted locally: read inside the working tree or an attachment root"


# Keys that would mean a "pathless" tool is no longer pathless. Derived from the
# canonical `PATH_ARG_KEYS` rather than restating it, so a key added there is
# picked up here too -- the drift this file used to have, where the pathless
# guard could go on ignoring a key the hook had started secret-scanning. The
# additions are keys that are path-shaped without being a plain path argument:
# `cwd`, and the two the path vetter already treats as paths in disguise.
#
# Deliberately NOT `url`: WebFetch's URL is the argument Mike decided to allow
# (2026-08-07), and counting it as path-shaped would reinstate the prompt he
# removed, by the back door.
_PATHISH_KEYS: frozenset[str] = frozenset(
    PATH_ARG_KEYS + ("cwd", "glob", "pattern")
)


def vet_pathless_tool(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """(ALLOW, reason) or (PROMPT, reason). Never denies.

    The third vetted-allow fast path, for the tools that carry no path at all
    (`boundaries.VETTED_PATHLESS_TOOLS` — web, TodoWrite, ToolSearch, and the
    read-only connector tools). Same caller obligation as the other two: hard
    denies first.

    There is no containment check to run here and nothing to resolve, so the
    membership test IS the rule. That makes "pathless" load-bearing rather than
    descriptive, and the `_PATHISH_KEYS` check below is what keeps it true: if a
    listed tool ever grows a path argument, this abandons vetting and sends the
    call to the phone instead of allowing an unresolved path on the strength of
    a tool name. Fixing that properly means moving the tool to the vetter above.
    """
    if tool_name not in VETTED_PATHLESS_TOOLS:
        return PROMPT, f"{tool_name} is not a vetted pathless tool"
    carried = sorted(key for key in tool_input if key in _PATHISH_KEYS)
    if carried:
        return PROMPT, (
            f"{tool_name} carried a path argument ({', '.join(carried)}) -- it is "
            "vetted as pathless and no longer is"
        )
    return ALLOW, "vetted locally: pathless tool on the allow list"

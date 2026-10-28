"""Shared detail-string helper for denial log lines.

Split out of server/app.py so hooks/pretooluse.py can reuse the exact same
content discipline instead of growing a second copy that drifts from it. The
split matters for more than DRY: server/app.py imports FastAPI/uvicorn/pydantic
to define the HTTP app, and hooks/pretooluse.py runs as a fresh process on
every single tool call. Importing server.app there would mean constructing
that whole ASGI app (routes, middleware, the lot) once per tool call just to
reach one string-formatting function. This module has no dependency heavier
than the standard library, so it costs nothing to import from either side.
"""
from __future__ import annotations

from typing import Any

# How much of a Bash command to keep in a log line. Long enough to identify
# which step of a job prompt failed, short enough that a pathological
# here-doc doesn't blow out the log file.
DENY_LOG_COMMAND_CHARS = 200


def denied_call_detail(tool: str, tool_input: dict[str, Any]) -> str:
    """A short identifying suffix for a denial log line.

    An unattended deny (or now, a hard deny) is the signal to watch for -- a
    job failing the same way every morning -- but the tool NAME alone doesn't
    identify the call, so "denying Bash" left no way to tell which step of a
    job prompt to fix without waiting for the model to mention it.

    Deliberately not the whole tool_input: Edit and Write carry file *content*
    there, and this line lands in a log file (and journalctl, for the server
    process) on a box whose working dir is personal. Bash is the one tool
    whose command string is both the diagnostic and already the thing vetting
    is decided on, so it is the only field logged.
    """
    if tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command:
            truncated = command[:DENY_LOG_COMMAND_CHARS]
            suffix = "..." if len(command) > DENY_LOG_COMMAND_CHARS else ""
            return f": {truncated}{suffix}"
    return ""


# --- the denial classes, and what a repeat of each one is evidence FOR -------
#
# Every tool denial in the system lands in one log file (server/log.py's
# get_deny_logger) so the promotion rule in
# docs/tasks/marlow-harness-affordances.md is a `cat` rather than a filter
# somebody has to get right. But the classes are NOT interchangeable evidence,
# and merging them without a marker would be worse than the split file it
# replaces -- each one indicts a different artifact:
#
#   hard        A boundary that is never allowed (secrets, the agent's own
#               config, destructive shapes, harness-owned git writes). NOT
#               promotable to the identity block -- no wording makes it
#               allowed. A repeat means the always-on `# HARNESS` injection is
#               missing a line, so the model keeps rediscovering the wall.
#
#   unattended  A cron turn, denied without anyone being asked (server/app.py).
#               Indicts the JOB PROMPT: the 08:00 run wants a tool it can never
#               have unattended. docs/tasks/scheduled-work.md's signal.
#
#   declined    Mike pressed Deny on the phone. The strongest signal here and
#               the one the promotion rule was actually written for: a
#               deliberate answer about this specific tool use. A repeat is a
#               standing PREFERENCE and belongs in the identity block.
#
#   timed-out   The prompt was raised and the window elapsed. Deliberately NOT
#               folded in with `declined`, because it is barely evidence at
#               all: phone in a pocket, no signal, asleep, driving. A repeat
#               says something about REACHABILITY, not about the tool. Do not
#               promote a preference from these.
#
#   no-answer   The hook never got a usable answer out of the server at all:
#               unreachable, wedged, malformed response, or past the hook's own
#               bound. An OPERATIONAL FAULT, never a preference -- a run of
#               these means the permission system itself was down and every
#               tool needing approval failed closed while it was. The one class
#               that cannot be logged server-side, by definition, which is why
#               the hook logs it. Deliberately not split further: the hook
#               genuinely cannot tell "unreachable" from "too slow", and naming
#               the class after what it can actually observe beats guessing.
#
# Format is `denied/<kind> <Tool>[: command] -- note`, kinds slash-joined so a
# grep needs no escaping: `grep denied/declined ~/.marlow/logs/marlow-denials.log`.
#
# Constants rather than a frozenset of legal values checked at runtime: every
# `kind` in this system is a literal chosen at a call site, never data, so the
# failure to guard against is a TYPO -- and a typo'd constant is a NameError at
# import, caught by any test that touches the module, while a typo'd string
# would sail through a runtime check that log_denial() must swallow anyway (it
# is on a path where raising is worse than mislabelling). A silently misspelled
# kind is the bad outcome here: `denied/timedout` is written happily and the
# promotion rule's grep under-counts that class forever with no error anywhere.
HARD = "hard"
UNATTENDED = "unattended"
DECLINED = "declined"
TIMED_OUT = "timed-out"
NO_ANSWER = "no-answer"


def log_denial(
    kind: str, tool: str, tool_input: dict[str, Any], note: str | None = None
) -> None:
    """Append one line to the denial log. Best-effort, never raises.

    Swallowing here rather than at each call site is deliberate: both callers
    are on a path where a logging failure must not become a failure of the
    thing being logged. In hooks/pretooluse.py a raise would flip an intended
    deny into the "hook error -- failing closed" branch, which still denies but
    throws away the reason the model was just given; in server/app.py it would
    turn a permission response into a 500 that the hook then reads as an
    unreachable server. Neither is worth a log line.

    server.log is imported lazily so that the ordinary allow path -- by far the
    common case in the hook, which is a fresh process per tool call -- never
    pays for the logging module's file handler setup.
    """
    try:
        from server.log import get_deny_logger

        suffix = f" -- {note}" if note else ""
        get_deny_logger().warning(
            "denied/%s %s%s%s", kind, tool, denied_call_detail(tool, tool_input), suffix
        )
    except Exception:
        pass

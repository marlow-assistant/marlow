#!/usr/bin/env python3
"""PreToolUse hook (plan §4; mechanics: gate1-results §2-4).

Referenced from settings.json for any tool left off allow/ask/deny so it
falls through here (gate1-results §4 — an `ask` entry auto-denies in headless
and overrides a hook `allow`; this is the actual phone round-trip).

Two checks, in order:
  1. boundaries.py's declarative fs/arg-shape rules (§5) — an always-deny,
     no need to wake a phone for something that's never allowed.
  2. Otherwise, POST to the loopback-only /internal/permission-request and
     block for the server's decision (which itself blocks on the phone
     answering, or times out fail-closed — see permissions.py).

Emits the current hookSpecificOutput schema (gate1-results §2) and always
exits 0 — the *decision* carries allow/deny, the exit code doesn't (a
nonzero exit here would just make Claude treat the hook itself as broken).

Self-bounds its own HTTP wait a few seconds past the server's T_hook so the
server's own timeout-then-deny path always resolves first; if even the HTTP
call itself fails (server unreachable/wedged), fails closed the same way.
"""
from __future__ import annotations

import json
import sys

from server.active_turn import SIMILAR_ALLOW_REASON, UNATTENDED_DENY_REASON
from server.bash_policy import (
    ALLOW,
    anchor_to_tree,
    command_is_git_write,
    command_touches_agent_config,
    command_touches_harness_only_path,
    command_touches_secret_path,
    vet_bash_command,
)
from server.boundaries import (
    PATH_ARG_KEYS,
    is_agent_config_path,
    is_harness_only_path,
    is_product_owned_path,
    is_secret_path,
    matches_deny_arg_shape,
)
from server import deny_detail
from server.file_policy import WRITE_TOOLS, vet_file_tool, vet_pathless_tool
from server.config import settings
from server.internal_client import post_json

# Local marker for "the server gave us nothing usable" — deliberately not a
# value any server sends, so it can never collide with a real reason code.
# Distinct from deny_detail.NO_ANSWER, which is the log's class marker: this
# one is a reason code travelling through request_decision's return value.
NO_ANSWER_REASON = "__no_answer__"

# Below: the seven hard-deny reasons, worded for the model rather than for us.
#
# The permission round-trip UI's lesson (2026-08-04; cited again on the UNATTENDED_DENY_REASON branch further
# down) is that a reason string naming an internal module reads to the model
# like spoofed tool output -- something to be suspicious of, not an
# explanation to act on. "denied by boundaries.py: ..." was exactly that: a
# Python filename with no meaning to the agent using it.
#
# Every one of these seven is genuinely "this is never allowed" rather than
# "wrong tool, try again": secrets and the harness's own config are permanent
# boundaries, the destructive-shape denial covers a whole class regardless of
# target, and the git-write denial exists because the write already happens
# automatically. So each string says that plainly instead of implying a retry
# would land differently -- the one thing it can usefully add is *why*.
_SECRET_PATH_REASON = (
    "Not approved: that path holds a credential or private key. Nothing may "
    "read, write, or otherwise reference it, from any tool -- this isn't a "
    "wrong-tool problem, so there's no retry that fixes it."
)
_AGENT_CONFIG_REASON = (
    "Not approved: that path configures this harness itself -- settings, "
    "hooks, permissions, or MCP servers. Those are off-limits to read or "
    "write from any tool, on purpose, because they're consulted before this "
    "check even exists to have an opinion. Not something to retry differently."
)

# Unlike the reasons above, this one names a way forward, because there is one:
# the edit was a reasonable thing to want, it just belongs in the other file.
_PRODUCT_OWNED_REASON = (
    "Not approved: this file comes with Marlow's software and is replaced on "
    "every update, so it can't be edited (CLAUDE.md, FIRST-RUN.md and the "
    "shipped job prompts like cron/email-flash.md). A lasting change to how "
    "you work with this person -- their style, their preferences -- goes in "
    "PREFERENCES.md instead, which CLAUDE.md imports. Who or what matters to "
    "them, which the shipped jobs read, goes in USER.md or memory."
)


_HARNESS_ONLY_REASON = (
    "Not approved: the cron spool is installed by the harness, never by a "
    "tool. Schedules are edited in cron/schedules.json and rendered from "
    "there -- that is the supported way to change when something runs, and "
    "it needs no approval. Editing the spool directly is off-limits."
)


def boundary_deny_reason(tool_name: str, tool_input: dict) -> str | None:
    command = tool_input.get("command")
    if tool_name == "Bash" and isinstance(command, str):
        if matches_deny_arg_shape(command):
            return (
                "Not approved: this command's shape -- a bulk delete, a forced "
                "push, or a find that runs or deletes what it matches -- is "
                "never allowed here, whatever the target. Retrying with "
                "different wording won't change that; say what you're trying "
                "to accomplish instead."
            )
        # The PATH_ARG_KEYS loop below never sees a Bash argument -- a Bash call
        # carries its paths inside `command`, not in a structured key. That gap
        # is what let `git diff --no-index ~/.ssh/id_ed25519 /dev/null` past a
        # settings.json that denies `Read(~/.ssh/**)` (/security-review
        # 2026-08-05).
        if command_touches_secret_path(command):
            return _SECRET_PATH_REASON
        if command_touches_agent_config(command):
            return _AGENT_CONFIG_REASON
        if command_touches_harness_only_path(command):
            return _HARNESS_ONLY_REASON
        # Git in the identity dir is harness-owned (server/identity_git.py commits
        # every turn). A model-issued write would put a second author into that
        # history; reads stay approvable. See bash_policy.command_is_git_write.
        if command_is_git_write(command):
            return (
                "Not approved: commits in this directory are made "
                "automatically after every turn, so a manual git write here -- "
                "commit, add, reset, or push -- isn't needed and isn't "
                "allowed. If something needs saving, it already will be; "
                "reading history (log, diff, show, status, ...) still works."
            )
    for key in PATH_ARG_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and is_secret_path(value):
            return _SECRET_PATH_REASON
        if isinstance(value, str) and is_agent_config_path(value):
            return _AGENT_CONFIG_REASON
        if isinstance(value, str) and is_harness_only_path(value):
            return _HARNESS_ONLY_REASON
        # Anchored, because the CLI resolves a relative `CLAUDE.md` against its
        # cwd -- the working dir -- and the roots here are absolute.
        if tool_name in WRITE_TOOLS and isinstance(value, str):
            anchored = anchor_to_tree(value, settings.working_dir)
            if anchored is None or is_product_owned_path(anchored):
                return _PRODUCT_OWNED_REASON
    return None


def _log_hard_deny(tool_name: str, tool_input: dict, reason: str) -> None:
    """Best-effort logging for a boundary_deny_reason() hard deny.

    This class of deny is invisible server-side: it's decided inside this
    process and the hook returns without ever POSTing anywhere, so nothing on
    the server could log it even in principle. It joins the other four classes
    in the one denial log -- see server/deny_detail.py for what each class is
    and is not evidence of. A repeat of THIS one indicts the `# HARNESS`
    injection, not the identity block: no wording makes a hard deny allowed, so
    a model hitting the same wall twice means the wall was never announced.

    Where this writes, and what was ruled out:

      - NOT a POST to the server. Every hard deny is decided locally specifically
        so the fast/local paths in this hook cost nothing beyond this process --
        the module docstring's whole point. Adding a network round-trip here
        would tax the one path that today has zero latency cost, for every tool
        call that trips a hard deny, and would make logging (not just the
        decision) depend on the server being reachable.
      - NOT stderr. This hook runs as a child of `claude -p`, which invoke.py
        spawns with stdout/stderr as pipes it captures itself (server/invoke.py)
        rather than inheriting the service's own file descriptors. Nothing
        forwards a hook's stderr to journalctl on success; it would only surface
        secondhand, truncated, and only when invoke.py already logs a non-zero
        exit -- not a channel a "never crashes, always exits 0" hook can rely on.
      - Writes to server/log.py's DEDICATED DENIAL LOG instead -- a separate
        file from marlow-server.log, not the server's rotating handler. That
        function's docstring carries the reasoning; the short version is that
        RotatingFileHandler is not multi-process safe and this hook is a
        separate process from the long-lived server, so sharing the server's
        handler risks losing log data during a rollover. It is a plain append,
        so the cost is a few milliseconds of I/O on a hard-deny call and
        nothing at all on the ordinary-allow path.

    Called AFTER emit() rather than before: the decision is this process's
    contract and file I/O has no business sitting between the decision being
    made and it being written to stdout. That ordering only buys anything
    because emit() flushes -- see the comment there; on a block-buffered pipe
    without the flush, "after emit()" is still before the decision has landed.

    deny_detail.log_denial swallows its own failures, so a full disk or a bad
    permission can never become this hook's crash path -- that would flip an
    intended deny into the "hook error -- failing closed" branch, which still
    denies but throws away the reason the model was just given.
    """
    deny_detail.log_denial(deny_detail.HARD, tool_name, tool_input, note=reason)


def request_decision(
    tool_name: str, tool_input: dict, session_id: str | None
) -> tuple[str, str | None]:
    """Returns (decision, reason_code). The reason is the server's machine-
    readable note about *why*, present only for cases the hook cannot otherwise
    tell apart — `unattended` from the server, or NO_ANSWER_REASON synthesised locally
    when the server produced nothing usable. `None` means an ordinary denial
    (an explicit no, or the T_hook window elapsing), which is what every older
    server returns.
    """
    body = {"tool": tool_name, "tool_input": tool_input, "session_id": session_id}
    try:
        payload = post_json(
            settings.internal_socket_path,
            "/internal/permission-request",
            body,
            timeout=settings.t_hook_seconds + 5,
        )
        return payload["decision"], payload.get("reason")
    except (OSError, KeyError, TypeError, ValueError):
        # Server unreachable/wedged/too slow — fail closed, same as a hook
        # timeout. NO_ANSWER_REASON is a LOCAL marker, not something the server said:
        # it exists so this run leaves a trace in the denial log, because a
        # server that isn't answering also isn't logging, and today an outage
        # denies every approval-needing tool with no record anywhere that the
        # permission system was the thing that failed.
        return "deny", NO_ANSWER_REASON


def emit(decision: str, reason: str) -> None:
    # flush=True is load-bearing, not tidiness. This hook's stdout is a pipe
    # `claude -p` captures, so it is block-buffered: without the flush the
    # decision JSON (a few hundred bytes) sits in the buffer until the process
    # exits, and anything that runs after emit() -- _log_hard_deny's file I/O
    # today -- runs while the decision has NOT yet been delivered. A stalled
    # log_dir would then hang the hook past the harness's timeout and get it
    # treated as broken rather than as the deny it had already decided.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }), flush=True)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Malformed/empty stdin — fail closed rather than crash. A nonzero
        # exit here would make Claude treat the *hook* as broken instead of
        # producing a decision; this file's contract is "always exits 0".
        emit("deny", "hook received unparseable stdin")
        return

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id")

    try:
        _decide(tool_name, tool_input, session_id)
    except Exception:
        # The docstring's "always exits 0" contract, made true for every failure
        # rather than only unparseable stdin. A crash here is not a neutral
        # event: Claude treats a non-zero hook exit as a non-blocking error and
        # may run the tool, so an exception is a permission bypass rather than a
        # decision. `cat ~nosuchuser/x <secret>` was exactly that -- it died
        # inside the secret-path scan, before the secret was ever examined
        # (/security-review 2026-08-05, third pass).
        emit("deny", "hook error -- failing closed")


def _decide(tool_name: str, tool_input: dict, session_id: str | None) -> None:
    reason = boundary_deny_reason(tool_name, tool_input)
    if reason:
        emit("deny", reason)
        _log_hard_deny(tool_name, tool_input, reason)
        return

    # Vetted-allow fast path. Without it the allow list in settings.json buys
    # nothing: probe runs 4-5 (2026-08-05) showed the hook is consulted for
    # allow-matched calls too, so `git status` would POST and raise a phone
    # prompt exactly as it does today -- and unattended it would burn the 55s
    # window and fail closed. Ordered strictly after the hard denies above so a
    # vetted-allow can never clear something boundaries.py protects.
    #
    # Three halves, which is one more than the shape suggests: a tool carries
    # its paths in a command string (Bash), in a structured key (the file
    # tools), or nowhere at all (web, TodoWrite, ToolSearch, the connectors).
    # The first two are the same containment rule; the third has no path to
    # contain and is a membership test instead.
    command = tool_input.get("command")
    if tool_name == "Bash" and isinstance(command, str):
        decision, why = vet_bash_command(command)
        if decision == ALLOW:
            emit(decision, why)
            return
    else:
        # The same fast path for the tools that carry a path in a structured
        # key. Its absence made the allow list dead letter for every tool except
        # Bash: the first real turn under the installed policy raised five phone
        # prompts, two of them for a Read and an Edit that settings.json
        # explicitly allows (2026-08-06). Same ordering guarantee -- strictly
        # after the hard denies above.
        decision, why = vet_file_tool(tool_name, tool_input)
        if decision == ALLOW:
            emit(decision, why)
            return

        # And the tools with no path at all, which matched neither of the two
        # vetters above and so reached the phone no matter what settings.json
        # said -- 10 of the 15 allow entries were inert (2026-08-06). Tried
        # second because a tool is in exactly one of the two sets: file_policy
        # PROMPTs on anything outside READ_TOOLS/WRITE_TOOLS, so this only ever
        # sees calls the path vetter had no opinion about. Same ordering
        # guarantee as the others -- strictly after the hard denies.
        decision, why = vet_pathless_tool(tool_name, tool_input)
        if decision == ALLOW:
            emit(decision, why)
            return

    decision, reason_code = request_decision(tool_name, tool_input, session_id)
    if decision == "allow" and reason_code == SIMILAR_ALLOW_REASON:
        # True to the letter: nobody saw THIS call. Mike approved calls like it
        # for the rest of this reply, which is worth the model knowing.
        emit(decision, "Approved: the user allowed calls like this for the rest of this reply.")
    elif decision == "allow":
        emit(decision, "Approved by the user via the phone approval prompt.")
    elif reason_code == UNATTENDED_DENY_REASON:
        # Nobody was asked, and saying otherwise would be a false statement
        # about Mike delivered TO Mike -- a scheduled job that hit this would
        # otherwise report "you didn't approve this" in a digest about a prompt
        # that was never raised (review, 2026-08-07).
        #
        # Worded for the model, not for us: it explains the situation and what
        # to do instead, in the register of the rest of these strings. The
        # permission round-trip UI's lesson (2026-08-04) applies -- engineering jargon here reads to the model like
        # spoofed tool output and gets treated as suspicious rather than as an
        # explanation.
        emit(
            decision,
            "Not approved: this is a scheduled run with nobody available to ask, "
            "so tools that need approval are unavailable. Nobody was prompted and "
            "nobody declined. Carry on without this tool and say what you skipped.",
        )
    else:
        # Covers three cases the hook can't tell apart (all fail closed the
        # same way, server/permissions.py): an explicit deny, the 55s window
        # elapsing with no answer, and the approval server being unreachable.
        # Genuinely ambiguous to the model, so this doesn't claim which one
        # happened. (NO_ANSWER_REASON narrows it for the LOG, below, but not for this
        # string: an unreachable server raised no prompt while a merely slow
        # one did, and the hook can't tell those apart either.)
        emit(decision, "Not approved: the user did not approve this tool use via the phone approval prompt.")
        # The other two cases here are logged by the server, which knows which
        # one it was. This one can't be: a server that isn't answering isn't
        # logging either. Same after-emit ordering as the hard denies.
        if reason_code == NO_ANSWER_REASON:
            deny_detail.log_denial(deny_detail.NO_ANSWER, tool_name, tool_input)


if __name__ == "__main__":
    main()

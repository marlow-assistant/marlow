"""The ONLY module that shells out to `claude -p`.

Isolates the CLI contract (gate #1, docs/marlow-v2-gate1-results.md) so a
future SDK/API-key path would be a swap here, not a rewrite elsewhere.

Hard constraints validated in gate #1, enforced here:
  - Scrubbed env — a `claude -p` launched as a child of an existing Claude Code
    session inherits CLAUDE_CODE_*/CLAUDECODE/AI_AGENT and gets the parent's
    tool registry instead of a normal toolset (§5 of gate1-results).
  - Scrubbed env, also — ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN/
    ANTHROPIC_BASE_URL are stripped too. The whole project's premise is that
    `claude -p` bills against the Pro subscription (OAuth via
    ~/.claude/.credentials.json), never an API key. If any of these ever
    leaked into the service's environment, the CLI would silently switch to
    API-key billing (or a redirected endpoint) with no signal in the JSON
    envelope — this inverts the hardest constraint in the project. Scrubbed
    by exact name, not an `ANTHROPIC_` prefix: the CLI may grow other
    ANTHROPIC_* knobs (e.g. logging/model overrides) that are fine to
    inherit, and a blanket prefix would silently swallow those too.
  - stdin `</dev/null`. NOT `--strict-mcp-config`: it was dropped 2026-08-06
    because it also strips the account's Claude.ai connectors, where Gmail and
    Calendar live, so v1's morning jobs cannot be ported with it set. Standing
    in its place is `boundaries.AGENT_CONFIG_PATHS` — without the flag the CLI
    honours every MCP config source, and an `mcpServers` entry is a command
    that runs at CLI *startup*, before the PreToolUse hook exists to object, so
    those config files being unwritable is the constraint now. Full reasoning
    at the argv site below and in docs/tasks/v2-dogfood-parity.md; do not
    quietly re-add the flag.
  - Detect denial via a non-empty `permission_denials` array, NOT exit code —
    a denied tool still reports exit=0 / is_error=false (§1).

Every invocation also gets Marlow's identity (SOUL/USER/memory) injected via
`--append-system-prompt` (server/identity.py) — see
docs/tasks/loader-identity-injection.md. CLAUDE.md is not injected here; a
clean-shell spike confirmed `claude -p` auto-loads a cwd CLAUDE.md by itself.
That string is frozen per-session (docs/tasks/cache-friendly-identity-injection.md)
via `identity.get_identity_context()` — resumed turns reuse the exact string
built at session start instead of re-reading disk, so a mid-session memory
write doesn't force the whole cache block (and everything after it) to
recreate.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from server import active_turn
from server.boundaries import (
    SECRET_PATHS,
    VETTED_BUILTIN_TOOLS,
    is_agent_config_path,
    is_secret_path,
)
from server.config import settings
from server.identity import IdentityLoadError, get_identity_context
from server.log import get_logger
from server.telemetry import identity_fingerprint, record_invocation
from server.turn_context import SPOKEN_TURN_NOTE, prepend_turn_context

logger = get_logger(__name__)

# Overall safety net: the queue serializes everything, so one wedged
# invocation blocks all work. This is deliberately generous — the phone
# round-trip hook self-bounds at ~T_hook seconds; this catches a truly stuck
# process, not a normal permission wait.
_SUBPROCESS_TIMEOUT_SECONDS = 20 * 60

# Telemetry `subtype` for an invocation that never produced a JSON envelope
# (timeout, missing binary, unparseable stdout). Deliberately not a value the
# CLI itself emits, so the usage log can separate "no envelope" from an
# envelope that reported is_error.
INVOKE_ERROR_SUBTYPE = "invoke_error"

_SCRUB_PREFIXES = ("CLAUDE_CODE_", "CLAUDECODE", "AI_AGENT")

# Exact names, not a blanket "ANTHROPIC_" prefix — see the module docstring.
# These three are the ones that can actually redirect billing/endpoint:
#   ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN — auth the CLI would use instead
#     of the OAuth credentials file, switching billing off the subscription.
#   ANTHROPIC_BASE_URL — would redirect invocations to an unintended endpoint.
_SCRUB_EXACT_NAMES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

# Client-selectable subset only. Return tuple members from the matcher below,
# never the request string itself (docs/tasks/effort-toggle-server-half.md).
_ALLOWED_EFFORTS = ("low", "medium", "high")

# Set AFTER the scrub, deliberately — the scrub exists to drop CLAUDE_CODE_*
# state *inherited* from a parent session, and this one is ours, set on
# purpose. It cannot live in marlow-server.service: the prefix rule above
# would silently strip it and the setting would appear to work while doing
# nothing.
#
# Why turn the CLI's own memory feature off (docs/tasks/cli-auto-memory.md):
# with it on, the CLI prepends "You have a persistent, file-based memory
# system at <encoded-cwd>/memory/" to Marlow's system prompt, naming a
# directory under ~/.claude/projects/ that is NOT the memory server/identity.py
# injects. Marlow then believes in two memories, and the CLI's one is
# unwritable — `Write(/home/marlow/.claude/**)` is denied in
# deploy/marlow-settings.json and boundaries.AGENT_CONFIG_PATHS backs it. An
# instruction the model is given and structurally cannot obey is worse than no
# instruction: observed 2026-08-11 as repeated reaching toward that directory
# (and toward the raw .jsonl transcripts beside it) to answer memory questions.
_FORCED_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}

# Cron turns only: make the CLI connect its MCP servers, the claude.ai
# connectors included, before the first request (docs/tasks/connector-drops.md).
# Some turns start before Gmail and Calendar have loaded; a long turn gets them
# back mid-turn, but `email-flash` checks for Gmail at its first step and
# replies `[silent]`, and eight did between Oct 2 and Oct 16. Read out of the
# 2.1.301 binary, not documented: the CLI connects non-blocking unless this
# variable is exactly false. Probed 2026-10-18: no extra first-response time
# while the connectors are quick. The race never fired in that probe, so
# whether this closes it is measured in production: chat turns keep the
# default and are the comparison, and both kinds record `mcp_not_connected`
# in the activity log. Not chat: a slow connector would hold up a reply that
# rarely needs one (Mike, 2026-10-18). Re-check the name after a CLI upgrade,
# as with --tools.
_MCP_WAIT_VAR = "MCP_CONNECTION_NONBLOCKING"


def _scrubbed_env(*, wait_for_mcp: bool = False) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _SCRUB_EXACT_NAMES and not any(k.startswith(p) for p in _SCRUB_PREFIXES)
    }
    env.update(_FORCED_ENV)
    # Dropped when inherited, so the source alone decides whether a turn waits.
    env.pop(_MCP_WAIT_VAR, None)
    if wait_for_mcp:
        env[_MCP_WAIT_VAR] = "false"
    return env


@dataclass(frozen=True)
class PlanUsage:
    """One reading of the subscription's rate-limit windows
    (docs/tasks/usage-visibility.md §1).

    `windows` maps a window name ("five_hour", "seven_day", and whatever else
    the CLI sends) to `{"utilization": float, "resets_at": int}`. Utilization
    is a fraction (0.14), not a percent. `observed_at` is wall-clock epoch of
    the turn's START, not its end: the event arrives within the first lines of
    the stream, and a long turn would otherwise stamp a reading minutes newer
    than the number in it.
    """

    observed_at: float
    status: str | None
    windows: dict[str, dict]


@dataclass
class InvocationResult:
    exit_code: int
    is_error: bool
    subtype: str | None
    result_text: str | None
    session_id: str | None
    permission_denials: list[dict] = field(default_factory=list)
    auth_failure_suspected: bool = False
    raw_stdout: str = ""
    raw_stderr: str = ""
    # Token/cache counters straight off the JSON envelope, previously dropped
    # (docs/tasks/usage-telemetry.md). Empty when the CLI didn't report any —
    # an error envelope, or a version predating the field. Never assume keys.
    usage: dict = field(default_factory=dict)
    # A short, plain reason for a failed turn, fit for turn.error and the
    # phone. Only ever built from a fixed table (see `_api_error_reason`),
    # never from the CLI's text. None when there is nothing better to say than
    # the client's generic "something went wrong".
    error_reason: str | None = None
    # The subscription windows as the stream reported them this turn, or None
    # (plain-json turns, an older CLI, a malformed event). Display only.
    plan_usage: PlanUsage | None = None
    # The TRUE size of the conversation after this turn: the prefix of the last
    # top-level model call (input + cache_read + cache_creation). None when the
    # stream carried no usable assistant usage. Shown on the phone and, since
    # 2026-10-05, what server/idle_compaction.py's gates run on; the envelope's
    # summed `usage` (1-4x the real size, one copy per internal model call) is
    # only that gate's fallback -- see idle_compaction._prefix_reading.
    context_tokens: int | None = None
    # Names of the MCP servers the stream's `init` event listed as anything
    # but `connected` -- a claude.ai connector still `pending` is the startup
    # race in docs/tasks/connector-drops.md. [] when all were connected; None
    # when there was no init event to read (plain-json turns, a CLI that
    # never got that far).
    mcp_not_connected: list[str] | None = None
    # What ran this turn, for the activity log (docs/tasks/model-and-effort-
    # config.md "Per-job effort"). `model` is the id the API answered with
    # (`parse_stream_model`), not the `--model` alias we asked for: an alias
    # resolves to whatever the CLI maps it to today, and that resolution is
    # the thing worth seeing. None off a plain-json turn. `effort` is the
    # `--effort` this invocation passed; the CLI echoes it nowhere.
    model: str | None = None
    effort: str | None = None

    @property
    def had_permission_denial(self) -> bool:
        return len(self.permission_denials) > 0


@dataclass(frozen=True)
class ActivityEvent:
    """One thing worth telling the user about, mid-turn.

    Deliberately three fields. `tool` is the tool NAME and nothing else — no
    `tool_input`, not one field of it. That input carries file paths, file
    contents, search queries and email subjects; invoke.py already logs denials
    by name only and telemetry.py holds the same no-content line. A name is
    enough for every label the UI needs ("Reading a file…", "Checking your
    calendar…"), so the richer payload buys nothing and leaks by default.
    See docs/tasks/turn-activity-streaming.md §1.2 before adding a field here.
    """

    kind: str  # "tool_start" | "tool_end" | "thinking" | "text"
    tool: str | None
    at: float


@dataclass(frozen=True)
class ToolCall:
    """One tool call, for the persisted activity log (docs/tasks/ux-lane.md §2a).

    A separate type from `ActivityEvent`, and it must stay separate. That one
    feeds the live status line and is name-only by §1.2 of
    turn-activity-streaming.md; this one outlives the turn on disk, so it is
    the "named allowlist of specific fields per tool" §1.2 said would have to
    be its own decision. `target` comes ONLY from `_tool_target` -- never a
    copy of `tool_input`.
    """

    name: str
    target: str | None
    at: float


# Long enough for a nested memory path or a search query, short enough that a
# target can never become a place to smuggle a document into the log.
_MAX_TARGET_CHARS = 120

# Tools whose target is a file path, and the input key that holds it.
_PATH_TARGET_KEYS: dict[str, str] = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}


def _clip_target(value: str) -> str | None:
    # Collapsed first: a query can carry newlines, and the log is one line per
    # turn read by a phone, not a place for layout.
    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    if len(collapsed) <= _MAX_TARGET_CHARS:
        return collapsed
    return collapsed[: _MAX_TARGET_CHARS - 1].rstrip() + "…"


def _tool_target(name: str, tool_input: object) -> str | None:
    """The one field of a tool's input the activity log may keep, or None.

    An ALLOWLIST by tool name (Mike, 2026-09-28, ux-lane.md §2a). Everything
    unnamed here records no target: Bash commands carry secrets in env and
    headers, Grep patterns are content, and MCP inputs are email subjects and
    calendar titles. A new tool is name-only until someone adds it here.
    """
    if not isinstance(tool_input, dict):
        return None
    key = _PATH_TARGET_KEYS.get(name)
    if key is not None:
        path = tool_input.get(key)
        if not isinstance(path, str) or not path:
            return None
        # Checked on the path as given AND as resolved: the predicates expand
        # `~` themselves, and a relative path is relative to the CLI's cwd,
        # which is the working dir.
        absolute = os.path.normpath(os.path.join(str(settings.working_dir), os.path.expanduser(path)))
        if any(is_secret_path(p) or is_agent_config_path(p) for p in (path, absolute)):
            return None
        working_tree = os.path.normpath(str(settings.working_dir))
        if absolute.startswith(working_tree + os.sep):
            return _clip_target(os.path.relpath(absolute, working_tree))
        return _clip_target(absolute)
    if name == "WebSearch":
        query = tool_input.get("query")
        return _clip_target(query) if isinstance(query, str) else None
    if name == "WebFetch":
        # Host only: a URL's path and query string can carry tokens.
        url = tool_input.get("url")
        if not isinstance(url, str):
            return None
        try:
            host = urlsplit(url).hostname
        except ValueError:
            return None
        return _clip_target(host) if host else None
    return None


def parse_tool_calls(line: str, *, now: float | None = None) -> list[tuple[str | None, ToolCall]]:
    """Every tool_use block on one stream-json line, as `(tool_use_id, ToolCall)`.

    Not `parse_activity_line`, which returns at most one event per line for a
    status line that can only show one. A log must see every call: the model
    issues parallel calls as several tool_use blocks in one message. The id
    comes back so the caller can de-duplicate if the CLI ever repeats a block.

    Total, like `parse_activity_line`: it runs in the stdout read loop, and an
    unfamiliar line must cost a log entry, never the turn.
    """
    now = time.time() if now is None else now
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    calls: list[tuple[str | None, ToolCall]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str) or not name:
            continue
        try:
            target = _tool_target(name, block.get("input"))
        except Exception:
            # The predicates behind this touch the filesystem. Losing one
            # target is the right price for never losing the turn.
            logger.warning("tool target extraction failed for %s", name, exc_info=True)
            target = None
        block_id = block.get("id")
        calls.append((block_id if isinstance(block_id, str) else None, ToolCall(name, target, now)))
    return calls


# Top-level stream-json line types that are never user-visible activity.
# `rate_limit_event` is here because it is the FIRST line of every turn
# (observed on three runs, CLI 2.1.235) and was absent from the task doc's
# type list -- exactly the kind of surprise the skip-unknown rule exists for.
_NON_ACTIVITY_LINE_TYPES = frozenset({"system", "rate_limit_event", "result"})


def parse_activity_line(line: str, *, now: float | None = None) -> ActivityEvent | None:
    """One stream-json line -> at most one ActivityEvent, or None.

    Pure and total: **never raises.** It runs inside the stdout read loop, where
    an exception would kill a turn that is otherwise succeeding, so every
    unparseable, truncated, or simply unfamiliar line returns None. A CLI
    upgrade that adds a line type must degrade to "no status update", never to
    a failed turn.

    At most *one* event per line, even though an assistant message may carry
    several content blocks. `Turn.activity` is a single slot feeding a single
    status line, so a list would be discarded downstream anyway. When a message
    mixes text and a tool call, the tool wins: "Reading a file…" tells the user
    more than "typing…", and it is the leg that takes the time.
    """
    now = time.time() if now is None else now
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict):
        return None
    if event.get("type") in _NON_ACTIVITY_LINE_TYPES:
        return None

    message = event.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None

    text_seen = False
    thinking_seen = False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_use":
            name = block.get("name")
            return ActivityEvent(
                kind="tool_start",
                tool=name if isinstance(name, str) else None,
                at=now,
            )
        if block_type == "tool_result":
            # No name: the result block carries `tool_use_id`, not the tool
            # name, and resolving it would mean holding a map of in-flight ids
            # for a label the UI does not use -- tool_end just means "back to
            # thinking".
            return ActivityEvent(kind="tool_end", tool=None, at=now)
        if block_type == "thinking":
            thinking_seen = True
        elif block_type == "text":
            text_seen = True

    if thinking_seen:
        return ActivityEvent(kind="thinking", tool=None, at=now)
    if text_seen:
        return ActivityEvent(kind="text", tool=None, at=now)
    return None


class PartialTextTracker:
    """Reply-text deltas out of `--include-partial-messages` stream_event lines.

    Deliberately NOT part of `parse_activity_line`, which is documented as pure,
    total and one-event-per-line. Text deltas are a different kind of thing:
    they accumulate rather than describe, and they need state across lines
    (which content block index is text), so they get their own object beside it.
    `feed` inherits the same never-raises contract — it runs in the same stdout
    loop, where an exception would kill a turn that is otherwise succeeding.

    Attribution by content-block index is a PRIVACY control, not just a
    correctness one. A delta arriving on a `tool_use` block's index is the
    tool's *input* being serialised — file paths, file contents, search
    queries, email subjects — which is exactly what `ActivityEvent`'s
    name-only rule exists to keep out of what the phone is shown. Only an
    index opened by a `content_block_start` of type `text` is ever appended.

    Sub-agent text is dropped for the same reason it is not in `result`:
    a non-null `parent_tool_use_id` means the deltas belong to a Task tool's
    own inner turn, not to the reply being written to the user.
    """

    def __init__(self) -> None:
        # index -> the text written into the block open at that index, in
        # order. Membership IS the "is this index text?" test the privacy rule
        # above rests on; the buffer behind it is what `blocks()` reassembles
        # the whole reply from (see `_whole_reply`).
        self._open: dict[int, list[str]] = {}
        self._closed: list[str] = []

    def _close(self, index: int) -> None:
        """Retire the block open at `index`, keeping whatever it accumulated."""
        buffered = self._open.pop(index, None)
        if buffered:
            self._closed.append("".join(buffered))

    def blocks(self) -> list[str]:
        """Every reply text block this stream carried, in order.

        Blocks still open are included: a stream that ends without a
        `content_block_stop` (a kill, a truncated stdout) must not silently
        drop the paragraph that was being written. Closed ones come first
        because that is the order they were written in; the open remainder is
        ordered by index for want of anything better, and in practice there is
        at most one.
        """
        still_open = ("".join(buf) for _, buf in sorted(self._open.items()))
        return [text for text in (*self._closed, *still_open) if text.strip()]

    def feed(self, line: str) -> str | None:
        """One stream-json line -> the reply text it adds, or None."""
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict) or event.get("type") != "stream_event":
            return None
        if event.get("parent_tool_use_id") is not None:
            return None
        inner = event.get("event")
        if not isinstance(inner, dict):
            return None
        index = inner.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            return None
        inner_type = inner.get("type")

        if inner_type == "content_block_start":
            block = inner.get("content_block")
            block_type = block.get("type") if isinstance(block, dict) else None
            # close, not "ignore": indexes are reused across blocks within a
            # message, so a tool block opening on index 1 must retire whatever
            # text block used to live there -- and retiring it has to KEEP its
            # text, because that is exactly the block `result` will not carry.
            self._close(index)
            if block_type == "text":
                self._open[index] = []
            return None
        if inner_type == "content_block_stop":
            self._close(index)
            return None
        if inner_type != "content_block_delta" or index not in self._open:
            return None

        delta = inner.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            # `thinking_delta` and `input_json_delta` land here and stay here.
            return None
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return None
        self._open[index].append(text)
        return text


def _whole_reply(envelope_result: str | None, tracker: "PartialTextTracker | None") -> str | None:
    """The whole reply, when the envelope carries only its last paragraph.

    `result` in the CLI's envelope is the FINAL assistant message's text, not
    the turn's reply. On a turn that writes, calls a tool, then writes again --
    10.6% of turns, measured over 113 of them in
    docs/tasks/streaming-reply-text.md -- everything before the tool call is
    simply absent from it. That was invisible until the reply started
    streaming, and then it was not: on 2026-09-04 Mike watched 751 characters
    of an answer type out and get replaced, on completion, by the 27-character
    "Updated. Enjoy Twillingate." that followed the Edit call. The truncation
    is older than the streaming; streaming only made it visible.

    The stream carries every block, so when someone was watching we can put the
    reply back together. Two rules keep this a recovery rather than a rewrite:

    - **The last block must equal `result`.** That is what makes this
      re-attaching the head to a tail we agree on, and it fails closed on any
      change to what `result` means -- a CLI version that concatenated the
      blocks itself would stop matching and fall straight through to the
      envelope, which stays authoritative in every case this cannot prove.
    - **Blocks are joined with a blank line, never concatenated.** Bare
      concatenation is the "I'll check your calendar.You have three meetings"
      fusion that `Turn.partial_text_marks` exists to prevent mid-stream; the
      final render has no tool chips to place, so it gets a paragraph break.

    Unattended turns pass `tracker=None` (no stream to reassemble from) and are
    returned the envelope unchanged -- see run_claude on why cron still
    truncates.
    """
    if tracker is None or not isinstance(envelope_result, str):
        return envelope_result
    blocks = [text.strip() for text in tracker.blocks()]
    blocks = [text for text in blocks if text]
    if len(blocks) < 2 or blocks[-1] != envelope_result.strip():
        return envelope_result
    logger.info(
        "reply spans %s text blocks; envelope carried the last %s of %s chars",
        len(blocks),
        len(blocks[-1]),
        sum(len(b) for b in blocks) + 2 * (len(blocks) - 1),
    )
    return "\n\n".join(blocks)


# stdout read size. Not a line limit: lines are reassembled from these chunks by
# hand precisely so there ISN'T one. `StreamReader.readline()` would have been
# the obvious call and is wrong here -- it raises once a line exceeds the
# reader's 64KiB limit, and the `result` line carries the entire reply, so a
# long answer would blow up the turn that produced it. Read chunks, split on
# newlines ourselves, no cap.
_STDOUT_CHUNK_BYTES = 65536


@dataclass
class StreamTimings:
    """When the first stdout line and the first reply text arrived, in
    time.monotonic() terms. Telemetry only -- see first_text_seconds in
    telemetry.py for why these exist."""

    first_output: float | None = None
    first_text: float | None = None


def _stream_line_handler(
    on_activity,
    on_text=None,
    timings: StreamTimings | None = None,
    on_tool_call=None,
):
    """Wrap the caller's callbacks so nothing they do can kill the invocation.

    Returns `(handle, tracker)`. The tracker comes back out because it is not
    only feeding the phone mid-turn: it is the only record of the reply's
    earlier text blocks, which the result envelope does not carry, and
    `_whole_reply` needs it once the stream is done.

    Two layers of "never raises": `parse_activity_line` and
    `PartialTextTracker.feed` are total by construction, and this catches
    whatever the callbacks themselves throw. A status update or a half-rendered
    reply is a nicety; the turn under it is the product. A UI label must never
    be the reason a good answer is lost — and the streamed text is only ever an
    approximation of the reply, which is settled from the envelope (and this
    tracker) once the stream ends.

    One handler for both because ordering matters downstream: the activity and
    the text arrive from the same line stream, in order, so a consumer can
    record where in the reply a tool call happened (docs/tasks/
    streaming-reply-text.md, the interleaved 11%).

    `on_tool_call` feeds the persisted activity log and is the only callback
    an unattended turn gets; `on_activity` may then be None. Same never-raises
    contract. Ids are remembered so a block the CLI repeats is logged once.
    """
    tracker = PartialTextTracker() if on_text is not None else None
    seen_tool_ids: set[str] = set()

    def _handle(raw: bytes) -> None:
        if timings is not None and timings.first_output is None:
            timings.first_output = time.monotonic()
        line = raw.decode("utf-8", errors="replace")
        if tracker is not None:
            chunk = tracker.feed(line)
            if chunk:
                if timings is not None and timings.first_text is None:
                    timings.first_text = time.monotonic()
                try:
                    on_text(chunk)
                except Exception:
                    logger.warning("on_text callback raised; dropping the delta", exc_info=True)
        if on_tool_call is not None:
            for block_id, call in parse_tool_calls(line):
                if block_id is not None:
                    if block_id in seen_tool_ids:
                        continue
                    seen_tool_ids.add(block_id)
                try:
                    on_tool_call(call)
                except Exception:
                    logger.warning("on_tool_call callback raised; dropping the call", exc_info=True)
        if on_activity is None:
            return
        event = parse_activity_line(line)
        if event is None:
            return
        try:
            on_activity(event)
        except Exception:
            logger.warning("on_activity callback raised; dropping the update", exc_info=True)

    return _handle, tracker


async def _read_stdout_lines(stream, handle) -> bytes:
    """Drain stdout, calling `handle` on each complete line, return it all.

    The full bytes still come back so `raw_stdout` stays exactly what it was on
    the non-streaming path -- the envelope parsing below reads from it, and so
    does every existing error message.
    """
    chunks: list[bytes] = []
    buffer = b""
    while True:
        chunk = await stream.read(_STDOUT_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line, buffer = buffer[:newline], buffer[newline + 1 :]
            handle(line)
    # A trailing fragment with no newline: either the CLI's last line arrived
    # unterminated, or the process died mid-line. Hand it over anyway -- the
    # parser returns None on a truncated line rather than raising.
    if buffer:
        handle(buffer)
    return b"".join(chunks)


def _kill_quietly(proc) -> None:
    """kill() that tolerates a child which has already exited."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _communicate(proc, handle) -> tuple[bytes, bytes]:
    """`proc.communicate()`, or its streaming equivalent when `handle` is set.

    stderr is read concurrently, not after: it is a pipe, and a CLI that filled
    the 64KiB buffer while we were still reading stdout would block forever.
    That deadlock is the reason communicate() exists, and it has to be
    reproduced, not assumed away.

    The two reads are explicit tasks so the failure path can cancel the
    survivor. `asyncio.gather` propagates the first exception immediately and
    documents that it does NOT cancel the other awaitables -- so a naive
    version leaves the stderr read pending forever and, worse, never kills the
    child. `claude -p` would go on running while the queue started the next
    turn, which is precisely the concurrent invocation the single work queue
    exists to prevent (CLAUDE.md), and two live CLIs race each other's
    `.credentials.json` refresh write-back.
    """
    if handle is None:
        return await proc.communicate()

    stdout_task = asyncio.ensure_future(_read_stdout_lines(proc.stdout, handle))
    stderr_task = asyncio.ensure_future(proc.stderr.read())
    try:
        stdout_b, stderr_b = await asyncio.gather(stdout_task, stderr_task)
    except asyncio.CancelledError:
        # The caller's wait_for timeout. It owns the kill-and-reap (and must,
        # since awaiting a reap while being cancelled is its own hazard), so
        # this only tidies up the reads. gather has already cancelled them;
        # doing it again is free and does not depend on that staying true.
        stdout_task.cancel()
        stderr_task.cancel()
        raise
    except BaseException:
        stdout_task.cancel()
        stderr_task.cancel()
        _kill_quietly(proc)
        await proc.wait()
        raise

    # Both pipes are at EOF; this only reaps the child so `returncode` is set.
    await proc.wait()
    return stdout_b, stderr_b


def _result_envelopes(stdout: str) -> list[dict]:
    """Every line of a stream-json stdout that parses as a result envelope.

    Non-JSON and unfamiliar lines are skipped rather than fatal: stdout is
    shared with anything else the CLI decides to print, and one stray line must
    not cost a completed turn. Deliberately imposes NO constraint on how many
    envelopes there are, where they sit, or what follows them -- see
    `_final_envelope` for why that tolerance is safe.
    """
    envelopes: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and candidate.get("type") == "result":
            envelopes.append(candidate)
    return envelopes


def _final_envelope(stdout: str) -> dict | None:
    """The result payload for one invocation, from a stream-json stdout.

    Normally there is exactly one `{"type":"result",...}` line, carrying the
    same payload `--output-format json` returns as its whole body (verified
    against CLI 2.1.235, three runs). The LAST one wins, so a future CLI is
    free to append line types after it.

    **Why more than one is tolerated instead of rejected.** The obvious
    hardening -- demand the envelope be the last line, or refuse a stream with
    two -- pins us to a format nobody has promised will hold. A CLI that grows
    a trailing telemetry line, or emits an envelope per sub-turn, would then
    break every *attended* turn while cron (still on `--output-format json`)
    kept working. That is a self-inflicted outage on a version bump, on a repo
    where CI is off.

    **What is defended instead.** Ask what a forged envelope appended to this
    stream would actually buy, field by field, and the list collapses to one
    entry. `result` is not a new capability: a prompt-injected model already
    writes the reply, that is what an injected model does. But
    `permission_denials` is this project's stated denial detector -- a denied
    tool still reports exit=0 / is_error=false (CLAUDE.md, gate1-results §1) --
    so clearing it turns a visible refusal into silence. So denials are OR'd
    across every envelope in the stream and cannot be cleared by a later one,
    and `is_error` is treated the same way. No position rule, nothing to break
    on a format change, and the one field where suppression would be silent is
    not suppressible.

    Note this is defence in depth, not the boundary. Anything able to write to
    the CLI's stdout is already further in than it should be; hooks/pretooluse
    and the bwrap namespace are what stand in its way. This only removes the
    payoff.

    The OR is also just better semantics, independent of any attacker: if a CLI
    ever does report per-sub-turn, "a denial anywhere in this invocation counts"
    is the answer you want.
    """
    envelopes = _result_envelopes(stdout)
    if not envelopes:
        return None

    payload = dict(envelopes[-1])
    if len(envelopes) == 1:
        return payload

    # Past here is either a contract change worth knowing about immediately or
    # someone appending to our stdout. Loud, and non-fatal, is right for both.
    logger.warning(
        "stream carried %s result envelopes; merging denials conservatively",
        len(envelopes),
    )

    denials: list[dict] = []
    for envelope in envelopes:
        reported = envelope.get("permission_denials") or []
        if isinstance(reported, list):
            # Not de-duplicated: the same denial appearing in two envelopes
            # would be counted twice, which over-reports. That is the harmless
            # direction -- `had_permission_denial` is a len() > 0 check, and an
            # extra name in a warning log beats a missing one.
            denials.extend(reported)
    payload["permission_denials"] = denials
    payload["is_error"] = any(bool(e.get("is_error", False)) for e in envelopes)

    session_ids = {e.get("session_id") for e in envelopes if e.get("session_id")}
    if len(session_ids) > 1:
        # The last one still wins (it is what the CLI would mean), but a
        # disagreement here decides what the next turn resumes, so it does not
        # get to happen quietly.
        logger.warning("result envelopes disagree on session_id: %s", sorted(session_ids))

    return payload


def _number(value: object) -> float | None:
    """A finite real number, or None. bool is an int subclass and is refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def _parse_plan_event(event: dict, observed_at: float) -> PlanUsage | None:
    """A `rate_limit_event` line -> PlanUsage, or None. Total: never raises.

    The event is undocumented and has already moved once (first line on CLI
    2.1.235, after system/init on 2.1.284), so nothing here may assume a key.
    A window needs BOTH a numeric utilization and a numeric resetsAt to count;
    a half-reading would put a bar on screen with no reset time beside it. No
    valid window at all means no reading.
    """
    info = event.get("rate_limit_info")
    windows_raw = info.get("unifiedWindows") if isinstance(info, dict) else None
    if not isinstance(windows_raw, dict):
        return None
    windows: dict[str, dict] = {}
    for name, window in windows_raw.items():
        if not isinstance(name, str) or not isinstance(window, dict):
            continue
        utilization = _number(window.get("utilization"))
        resets_at = _number(window.get("resetsAt"))
        if utilization is None or resets_at is None:
            continue
        windows[name] = {"utilization": utilization, "resets_at": int(resets_at)}
    if not windows:
        return None
    status = info.get("status")
    return PlanUsage(
        observed_at=observed_at,
        status=status if isinstance(status, str) else None,
        windows=windows,
    )


def _assistant_context_tokens(event: dict) -> int | None:
    """Prefix size of one top-level `assistant` line, or None.

    Subagent traffic is excluded: a subagent's call reads ITS context, which
    says nothing about the conversation's. The CLI marks those lines with a
    non-null top-level `parent_tool_use_id` (an `isSidechain` flag is honoured
    too, for transcript-shaped lines). Neither marker has appeared in this
    repo's fixtures, which carry only content blocks -- the shapes are from
    the CLI's own stream format, so a CLI that renames them degrades to
    counting a subagent line, not to an error.

    A usage block that sums to zero is not a measurement (the same trap as
    idle_compaction's 2026-08-21 all-zero usage), so it yields None and the
    caller keeps the previous reading.
    """
    if event.get("parent_tool_use_id") is not None or event.get("isSidechain"):
        return None
    message = event.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    total = 0.0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = _number(usage.get(key))
        if value is not None:
            total += value
    return int(total) if total > 0 else None


def parse_stream_readings(
    stdout: str, *, observed_at: float | None = None
) -> tuple[PlanUsage | None, int | None]:
    """`(plan_usage, context_tokens)` from a whole stream-json stdout.

    Pure and total, and deliberately read from the finished stdout rather than
    in the live line handler: the handler only exists for streaming turns, and
    this has to work on the failed-exit path too, where a reading is still
    worth having. Position-independent by construction -- every line is looked
    at. The LAST valid rate_limit_event and the LAST valid top-level assistant
    usage win (with --include-partial-messages the same message can repeat).
    A plain `--output-format json` stdout is one result line and yields
    (None, None).
    """
    observed_at = time.time() if observed_at is None else observed_at
    plan: PlanUsage | None = None
    context: int | None = None
    try:
        for line in stdout.splitlines():
            # Cheap pre-filter: with --include-partial-messages almost every
            # line is a stream_event delta, and this runs on the event loop.
            # A false positive only costs one json.loads; the type check below
            # is the real test.
            if "rate_limit_event" not in line and '"assistant"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "rate_limit_event":
                reading = _parse_plan_event(event, observed_at)
                if reading is not None:
                    plan = reading
            elif kind == "assistant":
                tokens = _assistant_context_tokens(event)
                if tokens is not None:
                    context = tokens
    except Exception:  # noqa: BLE001 -- a display number must never fail a turn
        logger.warning("could not read usage from stream", exc_info=True)
    return plan, context


# Server names are account configuration, not content, but they come from the
# CLI's stdout and end up on disk, so they are bounded like a ToolCall target.
_MAX_MCP_NAME_CHARS = 80
_MAX_MCP_NAMES = 30


def parse_init_mcp_status(stdout: str) -> list[str] | None:
    """Names of the MCP servers not `connected` at the stream's `init` event.

    Pure and total, like `parse_stream_readings`: a field the CLI renames must
    degrade to None, never fail the turn. Only the FIRST init event counts --
    it is what the first request was sent with, which is the whole question
    (docs/tasks/connector-drops.md). None when no init event parses.
    """
    try:
        for line in stdout.splitlines():
            if '"init"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not (
                isinstance(event, dict)
                and event.get("type") == "system"
                and event.get("subtype") == "init"
            ):
                continue
            servers = event.get("mcp_servers")
            if not isinstance(servers, list):
                return None
            names = [
                (s.get("name") if isinstance(s.get("name"), str) else "?")[:_MAX_MCP_NAME_CHARS]
                for s in servers
                if isinstance(s, dict) and s.get("status") != "connected"
            ]
            return names[:_MAX_MCP_NAMES]
    except Exception:  # noqa: BLE001 -- a diagnostic must never fail a turn
        logger.warning("could not read MCP status from stream", exc_info=True)
    return None


# A model id as the API spells one ("claude-sonnet-5-5"). It reaches the
# activity log and both clients, so anything else is dropped rather than
# carried: the stream is the CLI's stdout, and nothing else from it is shown.
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9.\-]{0,63}")


def parse_stream_model(stdout: str) -> str | None:
    """The model that answered, from the LAST top-level `assistant` line.

    Last, because a turn that fell back mid-way finished on the later model.
    Top-level only, for the reason `_assistant_context_tokens` gives: a
    subagent may run on another model and says nothing about this turn.
    `<synthetic>` is the CLI's own placeholder for a message no model wrote
    (an API error it reports as a reply), so it fails the pattern and the
    previous reading stands. Pure and total, like `parse_stream_readings`.
    """
    model: str | None = None
    for line in stdout.splitlines():
        if '"assistant"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        if event.get("parent_tool_use_id") is not None or event.get("isSidechain"):
            continue
        message = event.get("message")
        value = message.get("model") if isinstance(message, dict) else None
        if isinstance(value, str) and _MODEL_ID.fullmatch(value):
            model = value
    return model


# How many trailing lines the no-result-line error describes.
_TAIL_LINES_DESCRIBED = 5


def _describe_stream_tail(stdout: str) -> str:
    """The shape of a stream's last few lines, with none of their content.

    Each line becomes its `type` (and `subtype`), which the CLI defines and
    which say where the stream broke -- "system, assistant, user" is a turn
    killed after a tool returned. A line that is not a JSON object, the usual
    sign of a stream cut mid-write, becomes a marker. Type strings are
    clipped: they come from the CLI's stdout, and this is a log line.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    described: list[str] = []
    for line in lines[-_TAIL_LINES_DESCRIBED:]:
        try:
            event = json.loads(line)
        except ValueError:
            described.append("<unparseable>")
            continue
        if not isinstance(event, dict):
            described.append("<non-object>")
            continue
        kind = event.get("type")
        label = kind[:40] if isinstance(kind, str) else "<no type>"
        subtype = event.get("subtype")
        if isinstance(subtype, str):
            label += f"/{subtype[:40]}"
        described.append(label)
    return (
        f"{len(lines)} line(s), {len(stdout)} chars; "
        f"last line types: {', '.join(described) or 'none'}"
    )


class InvokeError(Exception):
    """Raised when the CLI itself couldn't be run or its output couldn't be parsed."""


# Observed verbatim (docs/tasks/concurrent-send-session-chaining.md §2, a
# clean-shell spike): `No conversation found with session ID: <uuid>`, exit 1.
# The CLI's own history cleanup (default 30 days) or a wiped
# ~/.claude/projects produces this.
_DEAD_SESSION_SIGNATURE = "no conversation found"


def _confinement_prefix() -> list[str]:
    """bubblewrap argv that makes the secret paths not exist for the CLI.

    This is the real filesystem boundary. server/bash_policy.py inspects command
    *strings*, and was caught four times because the string it read was not the
    command the shell ran; a mount namespace does not care what a token expands
    to. Confirmed on the box 2026-08-06 — the CLI authenticates and runs Bash
    normally inside, and the secret canary is unreachable.

    Derived from boundaries.SECRET_PATHS rather than hardcoded, so a path added
    there is hidden here too. Three deliberate exclusions:

      * `credentials_path` — the CLI authenticates from it and writes a
        refreshed token back every headless run (gate1-results §6). Hiding it
        breaks the subscription auth the whole CLI-not-SDK rule exists to
        protect, so it stays visible and keeps its boundaries.py guard. It is
        the one secret still defended by logic rather than by the kernel.
      * anything inside the working tree — that is the agent's own workspace,
        and blanking part of it would break Marlow in a way that looks like a
        model failure rather than a config one.
      * paths that do not exist — bwrap is happy to create the mountpoint, but
        a tmpfs over a typo'd path silently protects nothing, and the list is
        worth keeping honest.

    Note the per-path tmpfs rather than one over `~/.marlow`: the probe used the
    blunt version, which would also have hidden `~/.marlow/logs`. The server
    writes there from outside the namespace, but anything the CLI or the hook
    logged would have vanished into a tmpfs.
    """
    if not settings.bwrap_enabled:
        return []

    working_tree = os.path.realpath(str(settings.working_dir))
    credentials = os.path.realpath(str(settings.credentials_path))

    hidden: list[str] = []
    for secret in SECRET_PATHS:
        path = os.path.realpath(os.path.expanduser(secret))
        if path == credentials or path == working_tree or path.startswith(working_tree + os.sep):
            continue
        if not os.path.exists(path):
            continue
        hidden += ["--tmpfs", path]

    if not hidden:
        logger.warning("bwrap enabled but no secret paths resolved — confining nothing")

    # The internal socket's directory, read-only in here. The hook inside
    # must still connect (connecting to a socket on a read-only mount works),
    # but nothing inside may unlink the socket and bind its own: that
    # listener would answer the hook's permission requests, so a turn could
    # approve its own tool calls. With loopback TCP the live server held the
    # port, which was the same guarantee. Renaming around it fails too: the
    # bind is a mount point here (EBUSY), and moving ~/.marlow means writing
    # ~, which the unit's ProtectHome=read-only forbids (config.py,
    # `internal_socket_path`). With MARLOW_BWRAP_ENABLED=0 none of this
    # holds and a turn can replace the socket: one more thing an unconfined
    # box gives up (deploy/README.md §11).
    run_dir = os.path.realpath(str(settings.internal_socket_path.parent))
    guarded = ["--ro-bind", run_dir, run_dir] if os.path.isdir(run_dir) else []

    # No --unshare-net, deliberately: the PreToolUse hook runs INSIDE this
    # namespace and POSTs to the server. The socket alone wouldn't need the
    # network, but the CLI does (the API), and so does WebFetch.
    return [
        settings.bwrap_bin, "--dev-bind", "/", "/", "--die-with-parent", *hidden, *guarded, "--"
    ]


def weather_mcp_config() -> str:
    """The `--mcp-config` JSON for server/weather_mcp.py.

    `sys.executable` is the server's own venv interpreter, so the paths are
    right on any box without templating. `-I` is load-bearing: the CLI spawns
    the server with cwd = the working dir, which the agent writes, and
    isolated mode keeps that directory (and PYTHON* env) out of its imports.
    """
    script = Path(__file__).resolve().with_name("weather_mcp.py")
    server = {
        "type": "stdio",
        "command": sys.executable,
        # Problems only: a changed URL or page format at a national service
        # shows up there first (docs/tasks/weather-mcp.md, "Logging").
        "args": ["-I", str(script), "--log", str(settings.log_dir / "weather.log")],
        # Loaded with full schemas, not deferred behind ToolSearch (Mike,
        # 2026-10-13: weather is common enough to be front and centre; the
        # first live use cost a ToolSearch round trip). A per-server key the
        # CLI documents in its own schema, present at 2.1.284 on the box: "all
        # tools from this server are always included in the prompt and never
        # deferred". The 2026-08-23 notes found no such switch for MCP tools at
        # 2.1.221 (marlow-settings-permissions-policy.md); it arrived since.
        # Its side effect, startup waiting up to 5 s for the server to connect,
        # costs nothing here: this server makes no network call until a tool
        # runs. The price is three schemas on every turn, cron included.
        "alwaysLoad": True,
    }
    return json.dumps({"mcpServers": {"weather": server}}, separators=(",", ":"))


def _is_dead_session_error(stderr: str, session_id: str) -> bool:
    """Both halves of the observed signature, not just the fixed phrase.

    The phrase on its own is a weak guard: it would fire on any non-zero exit
    whose stderr happened to echo it back. `claude`'s prompt is positional
    (`claude [options] [command] [prompt]`) and `-p/--print` takes no argument,
    so a message beginning with `-` is parsed as an option and commander quotes
    the unknown token straight back to stderr — a crafted message can put the
    phrase there. Requiring the session id too pins the match to the sentence
    §2's spike actually observed.

    Written when the session id arrived unvalidated from the phone
    (`session_hint`, removed 2026-09-25): a caller could pick a value short
    enough to appear in almost any stderr, so the conjunction was a narrowing,
    not a guarantee. Session ids are now only ever ones the CLI itself
    reported (server/session_state.py), which closes that. The consequence
    was always small anyway — the retry runs with strictly fewer flags and no
    session, so a spurious one costs a second failed invocation.

    (An auth failure is also a non-zero exit and must never be silently
    retried; the phrase alone already excluded that one. This is about the
    echo case, which it didn't.)
    """
    lowered = stderr.lower()
    return _DEAD_SESSION_SIGNATURE in lowered and session_id.lower() in lowered


async def run_claude(
    prompt: str,
    *,
    session_id: str | None = None,
    resume: bool = False,
    cwd: str | None = None,
    source: str | None = None,
    turn_id: str | None = None,
    queue_wait_seconds: float | None = None,
    require_session: bool = False,
    attachment_paths: tuple[str, ...] = (),
    effort: str | None = None,
    spoken: bool = False,
    raw_prompt: bool = False,
    on_activity: Callable[[ActivityEvent], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    on_tool_call: Callable[[ToolCall], None] | None = None,
) -> InvocationResult:
    """Run `claude -p` and return its parsed result.

    session_id + resume=True -> `--resume <id>`. session_id without resume is
    treated as a hint for `--continue` (continues the most recent session in
    cwd) — callers that need a *specific* session should always pass
    resume=True.

    Cron's never-prompt policy is applied here as of 2026-08-07, but NOT by
    handing the CLI a second settings file — see the `active_turn.running`
    block below and docs/tasks/marlow-settings-permissions-policy.md for why
    the round-trip, not `--settings`, is where that decision belongs.

    source/turn_id/queue_wait_seconds never reach the CLI as arguments.
    turn_id and queue_wait_seconds are telemetry only. queue_wait_seconds is
    measured by WorkQueue from Turn.created_at to this invocation; direct
    callers leave it as None.
    `source` is telemetry AND it is what tells the permission round-trip
    whether a human is awake (server/active_turn.py) — keep threading it,
    dropping it while refactoring telemetry would start ringing Mike's phone
    at 08:00. The turn-context block's elapsed-time sentence
    (server/turn_context.py) is decided by `resume`/`session_id`, NOT by
    `source` — it used to be keyed off `source == "cron"` on the premise that
    a cron job always starts a fresh session, until the nightly consolidation
    job (docs/tasks/nightly-consolidation.md piece 2) broke that premise by
    resuming a real prior session while still carrying `source="cron"`
    (corrected 2026-08-16, see turn_context._gap_sentence's docstring).

    A `--resume` target the CLI no longer has wedges the conversation forever
    otherwise (docs/tasks/concurrent-send-session-chaining.md §5): every
    subsequent turn hits the same dead session and errors identically, with no
    path out but Unpair. So a resumed invocation that fails with that exact
    stderr signature gets re-run once, fresh (no --resume/--continue), and
    *that* result is what's returned — the retried turn has no memory of the
    thread (unavoidable; it's gone), but it answers instead of erroring, and
    the fresh session_id flows back through the normal poll so the client
    self-heals its stored id. The failed first attempt still recorded its own
    telemetry row (it really ran and consumed time); the retry records its own
    separately — each call below is a complete, self-contained invocation as
    far as telemetry is concerned.

    `require_session=True` turns that self-healing off. It exists for callers
    like the nightly consolidation attach (docs/tasks/nightly-consolidation.md
    piece 2) whose entire safety argument is "never run in a fresh, contextless
    session" — for the phone's own conversation, a fresh retry answering
    instead of erroring is a good trade; for a job whose fresh-session output
    would be a plausible invented day silently written to memory/, it is
    exactly the failure mode being guarded against. With this set, a
    dead-session first attempt is returned as-is (a visible ERROR turn) instead
    of being retried — the fresh session is never invoked, so it never gets the
    chance to write anything.

    `attachment_paths` (docs/tasks/ios-image-input.md) is one line each, prepended
    ahead of the turn-context block, not a change to the args list below: the
    CLI spike found that a bare filesystem path in the prompt, with no
    instruction to use any tool, is enough for it to call Read unprompted and
    decode real pixels. `boundaries.ATTACHMENT_ROOT` /
    `server/file_policy.py` are what let that Read clear without waking the
    phone; this is only the sentence that tells the model the file exists.

    `effort` is a per-turn client request. Only a chat source
    (`active_turn.CHAT_SOURCES`) may apply it, and _invoke_once validates it
    again at the argv boundary against the closed low/medium/high set. Cron and compaction ignore it.

    `raw_prompt=True` (docs/tasks/idle-compaction.md) skips BOTH of the
    prepend steps below and sends `prompt` to the CLI completely unmodified.
    Built for the compaction turn: the probe confirmed bare `/compact` as the
    *entire* prompt is what actually triggers compaction in headless mode,
    and both prepend steps put their text in FRONT of `prompt` — the
    turn-context block would arrive before `/compact` rather than the prompt
    being `/compact` alone, and the attachment line has the same problem.
    Default False everywhere; only the compaction path sets it.

    `on_activity` (docs/tasks/turn-activity-streaming.md phase 1) switches the
    invocation to `--output-format stream-json --verbose` and calls back as the
    CLI works, so the phone can say something better than "Thinking…" for ten
    seconds. It is ignored unless `source` is attended
    (`active_turn.ATTENDED_SOURCES`): cron and every other unattended path stay
    on `--output-format json`, because nobody is watching and the output format
    is the one thing in this function that changes what the CLI prints. The
    callback is best-effort — it cannot fail a turn, see
    `_stream_line_handler`. Both attempts get it, dead-session retry
    included; that retry is the same turn from the user's point of view and
    should not go silent halfway through.

    `on_text` (docs/tasks/streaming-reply-text.md) additionally adds
    `--include-partial-messages`, and is called with each chunk of *reply text*
    as the model writes it — measured on the box at 3.2s to first text against
    13.1s for the whole reply. Same attended gate and same best-effort contract
    as `on_activity`. What it delivers is a progressive approximation, and the
    client must render `result_text` on completion rather than whatever it
    accumulated -- but `result_text` is no longer just the envelope's `result`.
    Streaming also gives `_whole_reply` the turn's earlier text blocks, which
    that field drops, so an interleaved reply arrives whole. **Unattended turns
    do not stream and therefore still truncate**: `--output-format json` has no
    content blocks in it at all, only the same final-message `result`, so
    nothing on the cron path can put the head back. Measured before assuming that
    matters: 0 of 75 cron turns on the box produced more than one text block,
    against 33 of 331 attended ones, because a cron prompt is one instruction
    and 73 of those 75 never called a tool. Same mechanism, shape never hit.
    Revisit only if a cron turn is observed interleaving --
    docs/tasks/streaming-reply-text.md has the table and the decision rule.

    `on_tool_call` (docs/tasks/ux-lane.md §2a) is the one callback with NO
    attended gate: the activity log must show what a cron job did, even though
    most cron turns call no tool at all. Passing it moves any turn to
    stream-json; it never adds `--include-partial-messages`, which only a
    watched turn pays for. Compaction is the caller that passes none, so it
    stays on the plain-json format its probe was run against.
    """
    # Composed once, here, and reused by the retry below. It must NOT be built
    # per-attempt: the elapsed-time sentence is consumed on first read, and the
    # retry runs with session_id=None, so a per-attempt build gave the failed
    # attempt the gap and the surviving one nothing — losing "8 hours have
    # passed" on precisely the turn that needed it (review, 2026-08-07). Same
    # reasoning extends to the image line: a dead-session retry is the same
    # turn from the same caller and must still mention the same attachment.
    if not raw_prompt:
        # Directly above the message, below the context block: it describes
        # this message, and unlike the block it must not vanish if the block
        # fails to build.
        if spoken:
            prompt = f"{SPOKEN_TURN_NOTE}\n{prompt}"
        prompt = prepend_turn_context(prompt, session_id=session_id, source=source, resume=resume)
        if attachment_paths:
            lines = "\n".join(f"[image attached: {path}]" for path in attachment_paths)
            prompt = f"{lines}\n{prompt}"

    # Publish `source` for the PreToolUse round-trip, which cannot see it: the
    # hook's payload is the CLI's schema and carries no such field, and the
    # server-side handler runs in a different asyncio task from this one. See
    # server/active_turn.py. Wraps BOTH attempts -- the retry is the same turn
    # from the same caller, so an unattended cron job must not become attended
    # halfway through because its session died.
    with active_turn.running(source):
        result = await _invoke_once(
            prompt,
            session_id=session_id,
            resume=resume,
            cwd=cwd,
            source=source,
            turn_id=turn_id,
            queue_wait_seconds=queue_wait_seconds,
            effort=effort,
            on_activity=on_activity,
            on_text=on_text,
            on_tool_call=on_tool_call,
        )
        if (
            resume
            and session_id
            and result.exit_code != 0
            and _is_dead_session_error(result.raw_stderr, session_id)
        ):
            if require_session:
                logger.error(
                    "session %s no longer resumable (dead-session signature); "
                    "require_session=True, refusing to run fresh — turn will error",
                    session_id,
                )
                return result
            logger.warning(
                "session %s no longer resumable (dead-session signature); retrying fresh",
                session_id,
            )
            result = await _invoke_once(
                prompt,
                session_id=None,
                resume=False,
                cwd=cwd,
                source=source,
                turn_id=turn_id,
                # The retry did not wait in WorkQueue; recording the original
                # wait twice would overstate queueing in per-invocation rows.
                queue_wait_seconds=None,
                effort=effort,
                on_activity=on_activity,
                on_text=on_text,
                on_tool_call=on_tool_call,
            )
        return result


def _effort_for_source(source: str | None, requested_effort: str | None) -> str:
    """Thinking effort for one turn, by who asked for it.

    Cron turns run higher (`settings.cron_effort`); iOS may select one of the
    three allowed per-turn values; everything else takes the default. The
    rules are deliberately not uniform (Mike, 2026-08-18) —
    docs/tasks/model-and-effort-config.md pieces 2 and 3 have the reasoning:

    - **Cron runs medium.** Nobody perceives its latency, and its output has a
      long half-life. This includes `consolidate`, which ATTACHES to the day's
      attended session (`ATTACHED_SESSION_JOBS`, server/app.py) rather than
      running detached. That means it resumes a session built at chat effort
      and switches level, cold-reading the day's prefix — a cost accepted with
      eyes open, not an oversight. It is bounded at ~110K tokens by idle
      compaction's threshold, ~2% of a day's reads, and the session is retired
      immediately after, so nothing downstream inherits the cold cache. Better
      memories are worth more than that.
    - **Compaction takes the configured default.** Unlike consolidate, we have
      no evidence medium summarizes better: consolidate asks an open question
      where deliberation can change the answer, compaction does bounded
      selection where it plausibly cannot.

      This line used to read "takes the session's current level" and said that
      when piece 2 made the level selectable, compaction must follow the
      session. **Struck 2026-08-27 after measurement** — a `--resume` carrying
      no `--effort` takes the CLI's own default, not the session's level
      (`scripts/probe-effort-resume-inherit.sh`; the session .jsonl stamps the
      effort, so this is read off disk, not inferred). So the cheap
      implementation does not exist, and omitting the flag would reintroduce
      exactly the silent inheritance the model/effort pin exists to prevent.
      The expensive one — the server remembering each session's level — buys a
      preference nobody has measured. Do not re-add it without reading
      docs/tasks/model-and-effort-config.md "Probe 3".
    - **A cron job may run at another level.** `app._cron_effort` picks it
      from the job's code default (`ideas`, `consolidate`, `morning`,
      `week-ahead` are `high`) or the schedules entry's own `effort`
      (2026-10-19). The request is honoured for cron because only the server
      sets a cron turn's effort -- the cron endpoint is on the internal
      socket, and the value is matched against `_ALLOWED_EFFORTS` here like a
      phone's. Anything else gets `cron_effort`.
    - **Chat takes a valid per-turn request, else the server default.**
    """
    if source == "cron":
        for allowed in _ALLOWED_EFFORTS:
            if requested_effort == allowed:
                return allowed
        return settings.cron_effort
    if source not in active_turn.CHAT_SOURCES:
        return settings.effort
    for allowed in _ALLOWED_EFFORTS:
        if requested_effort == allowed:
            return allowed
    return settings.effort


async def _invoke_once(
    prompt: str,
    *,
    session_id: str | None,
    resume: bool,
    cwd: str | None,
    source: str | None,
    turn_id: str | None,
    queue_wait_seconds: float | None,
    effort: str | None,
    on_activity: Callable[[ActivityEvent], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    on_tool_call: Callable[[ToolCall], None] | None = None,
) -> InvocationResult:
    """One `claude -p` invocation, start to finish, telemetry included.

    Split out of `run_claude` so a dead-session retry (see there) is just a
    second call with `resume=False` — `get_identity_context` has to be
    re-derived for it too, since `resume` changes which identity string is
    frozen for the session (module docstring), and re-deriving is exactly what
    calling this again from scratch gives for free.
    """
    try:
        identity_context = get_identity_context(settings, session_id=session_id, resume=resume)
    except IdentityLoadError as e:
        # Re-raise as InvokeError so queue.py's existing InvokeError handler
        # preserves this message as turn.error, instead of the generic
        # "internal error" its bare except-Exception fallback would produce.
        raise InvokeError(str(e)) from e

    # Flags first; the prompt goes last, behind a `--` separator (appended
    # below). It is NOT an option-argument of `-p` — `claude --help` gives
    # `-p, --print` no argument and the synopsis is
    # `claude [options] [command] [prompt]`, so the prompt is positional and a
    # message merely *starting* with `-` gets parsed as an option instead.
    # That made message text a way to append arbitrary CLI flags, and spike
    # 2026-08-03 showed the payload is not theoretical: a SessionStart hook
    # defined in inline `--settings` JSON executed a command, and it ran even
    # though the CLI then rejected the call for having no prompt left — the
    # hook fires ahead of that validation. `--` makes everything after it
    # positional, so no message can be read as a flag.

    # LIVE updates (status line, typed-out reply) need BOTH a caller that
    # wants the events and a human waiting for them. Gated on
    # active_turn.ATTENDED_SOURCES rather than on `source != "cron"` so it
    # inherits that module's fail-closed property: a source nobody has
    # classified yet never pushes live updates anywhere.
    live = on_activity is not None and source in active_turn.ATTENDED_SOURCES
    # The activity log (docs/tasks/ux-lane.md §2a) needs the stream on EVERY
    # turn it records, cron included -- the one reason an unwatched turn
    # reads stream-json. Reversed the "cron stays on json" rule on 2026-09-28
    # (Mike); the envelope parser had then run on every attended turn for a
    # month. Nothing live is wired up unless `live` holds.
    streaming = live or on_tool_call is not None

    args = [
        *_confinement_prefix(),
        settings.claude_bin,
        "-p",
        "--append-system-prompt",
        identity_context,
        # stream-json is line-delimited JSON whose LAST line is byte-identical
        # in shape to what plain `json` returns as its whole body, which is why
        # everything downstream can stay as it is. `--verbose` is not optional
        # decoration: the CLI refuses stream-json in print mode without it.
        *(
            ["--output-format", "stream-json", "--verbose"]
            if streaming
            else ["--output-format", "json"]
        ),
        # Reply text as it is written, not in one lump at the end
        # (docs/tasks/streaming-reply-text.md). Only when someone is actually
        # consuming it: the flag multiplies stdout volume by the number of
        # deltas (19 on a measured 2KB reply) and buys nothing unwatched. It
        # does not change the last line — the result envelope is still
        # byte-identical in shape to `--output-format json`, which everything
        # below depends on and `tests/test_activity_stream.py` pins.
        *(["--include-partial-messages"] if live and on_text is not None else []),
        # `--strict-mcp-config` was here until 2026-08-06 and its removal is
        # deliberate, not an oversight -- see docs/tasks/v2-dogfood-parity.md.
        # It also strips the account's Claude.ai connectors, which is where
        # Gmail and Calendar live, so v1's morning jobs could not be ported with
        # it set. Probed all three ways (scripts/probe-connectors.sh) at two CLI
        # versions: the flag strips them, the marlow account has them, and
        # declaring them explicitly by URL under --mcp-config does NOT work --
        # the account's grant is bound to the managed-connector identity, and
        # the OAuth flow it wants cannot run headless.
        #
        # What replaced the protection: boundaries.AGENT_CONFIG_PATHS, which
        # hard-denies any tool call touching ~/.claude.json, ~/.claude/settings*
        # or the working tree's .mcp.json/.claude. Without the flag the CLI
        # honours every MCP config source, and an mcpServers entry is a command
        # that runs at CLI startup -- before the first tool call, so before the
        # PreToolUse hook can weigh in. Those files being unwritable is now the
        # thing standing where the flag stood.
        #
        # --tools IS set, added 2026-09-06, and it is what finally puts `Grep`
        # and `Glob` in front of the model. Probed on the box at CLI 2.1.251
        # against the init event's own `tools` array rather than by asking the
        # model (docs/tasks/marlow-harness-affordances.md): WITHOUT it the CLI
        # offers 54 tools and NEITHER Grep NOR Glob is among them -- not eager,
        # not deferred, absent, so the `ToolSearch` round trip everyone assumed
        # was the obstacle would have failed had Marlow ever attempted it. WITH
        # it they are present and eagerly listed.
        #
        # THIS IS NOT --strict-mcp-config IN DISGUISE, which is the first thing
        # to check about any flag that trims the tool surface here. --tools
        # governs the BUILT-IN set only: Gmail's 29 tools and Calendar's 9
        # survive it intact, and the settings.json denies still apply on top
        # (Trello and Drive stay gone). Verified in the same probe, because the
        # model self-reports this wrongly -- asked directly it claimed Trello
        # was available while the init event showed it was not.
        #
        # An ALLOWLIST, so a built-in the CLI adds later is excluded until
        # someone names it here. That is the safe direction and it is the same
        # shape as every other list in this project, but it ties this line to
        # the version pin: re-read the init event's `tools` array after a CLI
        # upgrade rather than assuming this set still means what it meant.
        # Dropping ScheduleWakeup, ReportFindings, Monitor, SendMessage,
        # PushNotification and the three MCP resource readers is a bonus, not a
        # cost -- it is the trimming docs/tasks/marlow-tool-surface-trimming.md
        # wanted and could not get from settings.json alone.
        #
        # Single-token `--tools=`, never two. The option is variadic
        # (`--tools <tools...>`), so the two-token form eats every argument
        # that follows it -- including the prompt, which the CLI then rejects
        # for being absent. Same class as the --resume= and --effort= forms
        # below, and here it is not a hardening detail but the difference
        # between working and not.
        f"--tools={','.join(VETTED_BUILTIN_TOOLS)}",
        # The weather server (docs/tasks/weather-mcp.md), added 2026-10-13.
        # It ADDS to the account's connectors rather than replacing them:
        # probed on the box at 2.1.284, the init event listed it beside Gmail
        # (30) and Calendar (9), and it reached Open-Meteo from inside bwrap.
        # Inline JSON, not a file, so there is no config on disk for the agent
        # to rewrite and nothing to add to AGENT_CONFIG_PATHS. Single token
        # for the --tools= reason above: the option is variadic.
        f"--mcp-config={weather_mcp_config()}",
    ]

    # Model/effort pin (docs/tasks/model-and-effort-config.md, pieces 1 and 3).
    # Deliberately here, ahead of the `--` separator below — everything past
    # it is positional, so a flag added after that line would be silently
    # passed to the model as text instead of taking effect.
    # Single-token `--flag=value`, not two tokens — same reasoning as the
    # `--resume=` form below, and adopted here BEFORE it is load-bearing: piece
    # 2 routes a phone-supplied effort string to this line, and a two-token
    # form lets a value beginning with `-` be re-parsed as a flag in its own
    # right. That is the `session_hint` bug exactly. An allowlist will guard it
    # too; this makes it safe by construction if the allowlist ever slips.
    applied_effort = _effort_for_source(source, effort)
    args += [f"--model={settings.model}", f"--effort={applied_effort}"]

    if resume and session_id:
        # One token, not ["--resume", session_id]. `claude --help` declares
        # `-r, --resume [value]` — an *optional* option-argument — so with two
        # tokens commander refuses to consume a value that starts with `-` and
        # re-parses it as a flag in its own right. session_hint arrived
        # unvalidated from the client until 2026-09-25 (now removed; ids come
        # only from the CLI's own output), and that turned one request field into a way to append arbitrary CLI
        # flags: `--settings={...}` alone re-injects the ANTHROPIC_* vars
        # _scrubbed_env() strips and can define hooks, i.e. command execution
        # (confirmed by spike 2026-08-03 — a SessionStart hook in inline
        # --settings JSON runs). The `--resume=<value>` form binds the whole
        # remainder as data no matter what it starts with, which closes the
        # class rather than the instance.
        args += [f"--resume={session_id}"]
    elif session_id:
        args += ["--continue"]

    # `prompt` already carries the turn-context block (run_claude composes it
    # once, ahead of both attempts). It rides on the prompt and never in
    # --append-system-prompt above: that string is the cache prefix, so a clock
    # in it would invalidate a ~58K block plus all history behind it on every
    # single turn (server/identity.py, cache-friendly-identity-injection.md).
    # Behind `--` it is still one positional argument, so the flag-injection
    # protection below is unaffected.
    #
    # Must stay last, and nothing may be appended after the prompt: everything
    # following `--` is positional, so a flag added below this line would be
    # silently passed to the model as text instead of taking effect.
    args += ["--", prompt]

    # `confined` is logged because it is otherwise unobservable from outside.
    # The obvious check -- ask Marlow to read a secret and see which layer
    # refuses -- cannot work: hooks/pretooluse.py hard-denies every path in
    # SECRET_PATHS before a shell ever runs, and the tmpfs list is derived from
    # that same tuple, so the hook always pre-empts the namespace and the
    # refusal message looks identical either way. This line is the tell.
    requested_effort_for_log = next(
        (allowed for allowed in _ALLOWED_EFFORTS if effort == allowed),
        "default" if effort is None else "invalid",
    )
    logger.info(
        "invoking claude -p (resume=%s, session_id=%s, confined=%s, model=%s, "
        "effort_requested=%s, effort=%s, streaming=%s)",
        resume,
        session_id,
        settings.bwrap_enabled,
        settings.model,
        requested_effort_for_log,
        applied_effort,
        streaming,
    )

    identity_chars, identity_hash = identity_fingerprint(identity_context)
    started = time.monotonic()
    # Wall clock beside the monotonic one: PlanUsage.observed_at is an epoch.
    started_wall = time.time()
    recorded = False
    timings = StreamTimings()

    def _since_start(at: float | None) -> float | None:
        return None if at is None else at - started

    def _emit(**kwargs) -> None:
        nonlocal recorded
        recorded = True
        _record(
            first_output_seconds=_since_start(timings.first_output),
            first_text_seconds=_since_start(timings.first_text),
            source=source,
            turn_id=turn_id,
            resume=resume,
            identity_chars=identity_chars,
            identity_hash=identity_hash,
            started=started,
            queue_wait_seconds=queue_wait_seconds,
            **kwargs,
        )

    # Every exit path below has to record, including the ones that raise
    # InvokeError. Telemetry's inter-invocation clock only advances when a
    # record is written, so a path that leaves without one makes the *next*
    # turn's gap_seconds absorb this whole invocation: a 20-minute timeout
    # would turn a 5-second follow-up into an apparent 20-minute idle period,
    # and gap_seconds is the entire point of the file. Hence the finally.
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or str(settings.working_dir),
                env=_scrubbed_env(wait_for_mcp=source == "cron"),
            )
        except FileNotFoundError as e:
            # With confinement on, argv[0] is bwrap, so this fires for a
            # missing bwrap too -- and "claude binary not found" would send
            # the reader to check entirely the wrong thing.
            missing = settings.bwrap_bin if settings.bwrap_enabled else settings.claude_bin
            raise InvokeError(f"executable not found ({missing!r})") from e

        handle, tracker = (
            _stream_line_handler(
                on_activity if live else None,
                on_text if live else None,
                timings,
                on_tool_call,
            )
            if streaming
            else (None, None)
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                _communicate(proc, handle), timeout=_SUBPROCESS_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise InvokeError(f"claude -p exceeded {_SUBPROCESS_TIMEOUT_SECONDS}s and was killed")

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        # Only a stream carries these lines. Read before the exit-code branch:
        # a turn that died after the CLI had already said how full the plan
        # is still tells the usage sheet something.
        plan_usage, context_tokens = (
            parse_stream_readings(stdout, observed_at=started_wall)
            if streaming
            else (None, None)
        )
        mcp_not_connected = parse_init_mcp_status(stdout) if streaming else None
        if mcp_not_connected:
            # Names only, and only to the journal and the activity log: a
            # blind turn is a diagnostic, never something said in the chat.
            logger.warning("MCP servers not connected at init: %s", mcp_not_connected)
        model = parse_stream_model(stdout) if streaming else None

        if proc.returncode != 0:
            # A non-zero exit here is NOT how tool denials show up (those are
            # exit=0 with a populated permission_denials array — gate1-results §1).
            # A genuine non-zero exit means the CLI itself failed (bad args, auth,
            # crash) before or instead of producing the JSON envelope.
            #
            # Or it produced one and still exited 1: an upstream API failure
            # leaves stderr EMPTY and puts "API Error: 522 ..." in a result
            # envelope on stdout (2026-09-28, a Cloudflare 522 killed a chat
            # turn and the journal said only "claude -p exited 1:"). That
            # envelope is read for its status code alone -- stdout is never
            # quoted here, since a stream's other lines carry tool_result
            # content.
            reason = _api_error_reason(_result_envelopes(stdout))
            logger.warning(
                "claude -p exited %s%s: %s",
                proc.returncode,
                f" ({reason})" if reason else "",
                stderr[:500],
            )
            _emit(
                usage=None,
                session_id=session_id,
                is_error=True,
                context_tokens=context_tokens,
            )
            return InvocationResult(
                exit_code=proc.returncode,
                is_error=True,
                subtype=None,
                result_text=None,
                session_id=session_id,
                auth_failure_suspected=_looks_like_auth_failure(stderr, proc.returncode),
                raw_stdout=stdout,
                raw_stderr=stderr,
                error_reason=reason,
                plan_usage=plan_usage,
                context_tokens=context_tokens,
                mcp_not_connected=mcp_not_connected,
                model=model,
                effort=applied_effort,
            )

        if streaming:
            payload = _final_envelope(stdout)
            if payload is None:
                # The TAIL, not the head: the first lines are the
                # rate_limit_event and system/init, identical on every turn
                # and never the reason this failed. But the tail's line TYPES,
                # not its text. This message reaches marlow-server.log and
                # turn.error (served by /v1/turn), and since cron moved to
                # stream-json (docs/tasks/ux-lane.md §2a) the last line of a
                # stream that died mid-turn is typically a tool_result -- the
                # email or file the job had just read. Quoting 500 chars of it
                # put that content in the log (/code-review medium,
                # 2026-09-28). Which line types arrived is what locates the
                # break; their content never did.
                raise InvokeError(
                    "claude -p stream produced no result line; "
                    f"{_describe_stream_tail(stdout)}"
                )
        else:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as e:
                raise InvokeError(f"claude -p produced non-JSON stdout: {stdout[:500]!r}") from e

        # Defensive: `usage` has never been relied on before, and a non-dict here
        # (older CLI, error envelope, an unexpected shape) must not become an
        # AttributeError deep in telemetry.
        usage = payload.get("usage")
        num_turns = payload.get("num_turns")
        denials = payload.get("permission_denials", []) or []
        subtype = payload.get("subtype")

        if denials:
            # Names only — tool_input can carry file contents and personal
            # material, same no-content-in-logs posture as telemetry.py.
            denied_tools = [d.get("tool_name", "?") for d in denials]
            logger.warning("permission denied for tool(s): %s", denied_tools)

        _emit(
            usage=usage,
            session_id=payload.get("session_id", session_id),
            is_error=bool(payload.get("is_error", False)),
            subtype=subtype,
            had_permission_denial=bool(denials),
            num_turns=num_turns if isinstance(num_turns, int) else None,
            context_tokens=context_tokens,
        )

        return InvocationResult(
            exit_code=proc.returncode,
            is_error=bool(payload.get("is_error", False)),
            subtype=subtype,
            result_text=_whole_reply(payload.get("result"), tracker),
            session_id=payload.get("session_id", session_id),
            permission_denials=denials,
            raw_stdout=stdout,
            raw_stderr=stderr,
            usage=usage if isinstance(usage, dict) else {},
            plan_usage=plan_usage,
            context_tokens=context_tokens,
            mcp_not_connected=mcp_not_connected,
            model=model,
            effort=applied_effort,
            # The same API failure may also arrive with exit 0; nothing
            # promises the CLI's exit code for it stays 1.
            error_reason=(
                _api_error_reason([payload]) if payload.get("is_error") else None
            ),
        )
    finally:
        if not recorded:
            # Reached only when an InvokeError is propagating. `invoke_error`
            # is our own subtype, not one the CLI ever emits, so analysis can
            # tell "the CLI failed to produce an envelope" apart from an
            # envelope that reported is_error.
            _emit(
                usage=None,
                session_id=session_id,
                is_error=True,
                subtype=INVOKE_ERROR_SUBTYPE,
            )


def _record(*, started: float, **kwargs) -> None:
    """Thin wrapper so every exit path computes duration the same way."""
    record_invocation(
        started_monotonic=started,
        duration_seconds=time.monotonic() - started,
        **kwargs,
    )


def _looks_like_auth_failure(stderr: str, exit_code: int) -> bool:
    """Best-effort heuristic only.

    Gate #1 left the exact expired-*refresh*-token signature as an open
    sub-item (docs/marlow-v2-gate1-results.md §6) — this has not been observed
    firsthand. auth_monitor.py's primary signal is refreshTokenExpiresAt in
    .credentials.json, not this. Update this function the first time a real
    expiry is captured.
    """
    if exit_code == 0:
        return False
    lowered = stderr.lower()
    return any(term in lowered for term in ("auth", "login", "unauthorized", "token"))


# "API Error: 522 {...}" -- the CLI's text for an upstream failure, as found in
# the box's session transcripts on 2026-09-28. Only the status code is read.
_API_ERROR_RE = re.compile(r"API Error: (\d{3})\b")
_API_TIMEOUT_STATUSES = frozenset({408, 504, 522, 524})


def _api_error_reason(envelopes: list[dict]) -> str | None:
    """A plain reason for an upstream API failure, or None.

    Reads the last result envelope that starts "API Error:" and maps its
    status code to a fixed sentence. The CLI's text after the code is a
    provider's error body; it goes nowhere. Nothing else in stdout is read
    either: the result reaches turn.error, which /v1/turn serves and the log
    records, and a stream's other lines carry whatever the turn read.
    """
    for envelope in reversed(envelopes):
        text = envelope.get("result")
        if not isinstance(text, str) or not text.startswith("API Error:"):
            continue
        match = _API_ERROR_RE.match(text)
        if match is None:
            return "Anthropic's API returned an error — try again."
        status = int(match.group(1))
        if status in _API_TIMEOUT_STATUSES:
            return "Anthropic's API timed out — try again."
        if status == 429:
            return "Anthropic's API is rate-limiting — try again shortly."
        if status == 529:
            return "Anthropic's API is overloaded — try again shortly."
        if status >= 500:
            return f"Anthropic's API had a server error ({status}) — try again."
        return f"Anthropic's API refused the request ({status})."
    return None

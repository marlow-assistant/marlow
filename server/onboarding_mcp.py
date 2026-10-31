"""Onboarding for Marlow: a stdio MCP server with one tool, `step_done`
(docs/tasks/onboarding-v2.md, "Harness design").

The model knows when an onboarding step is finished; what it loses is where it
was. It calls `step_done` when a step ends, and the result is the next step's
instructions, so it carries straight on in the same reply instead of stopping
with nothing to ask.

THIS SERVER RECORDS NOTHING. It reads no file, writes no file and opens no
network connection. Its one piece of state is whether this process has
already advanced a step: the CLI starts it fresh for each turn, so that is
"this turn", and a second step in one turn is refused. A turn that finished
one step and then the next, before the user answered it, raced a trial run
through setup in four messages (2026-10-30). The Marlow server reads
the `step_done` call out of the CLI's stream after the turn, checks the step's
disk condition, and owns the record; this process only answers "what comes
next" from the step list it was started with. The list is `--steps <json>`, a
list of {"id", "text"} from the user's current step to the last, built fresh
by the server for each turn. A missing or invalid list is an empty one: the
server still starts (a server the CLI reports as failed costs that turn's
onboarding, nothing else), and every call answers "already finished".

STANDALONE ON PURPOSE: stdlib only, no `server.*` imports. The CLI spawns it
with cwd = Marlow's working dir, which the agent writes, so invoke.py runs it
as `python -I <this file>` -- isolated mode keeps that directory off sys.path,
and an import of our own package would need it back on. Same transport as
weather_mcp.py: MCP's stdio framing by hand, one JSON-RPC message per line.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

SERVER_INFO = {"name": "onboarding", "version": "1"}
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
OUTCOMES = ("done", "skipped")

# The wording is part of the design: "same reply" is what stops the model
# finishing a step with nothing to say next.
TOOL = {
    "name": "step_done",
    "description": (
        "Call this when the current onboarding step is finished, or the user waved it off. "
        "The result is the next step: in the same reply, open it with its question and stop there. "
        "One step per reply."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "step": {"type": "string",
                     "description": "the step id from the onboarding step line"},
            "outcome": {"type": "string", "enum": list(OUTCOMES),
                        "description": "skipped when they waved the step off"},
            "note": {"type": "string",
                     "description": "one line on what was learned"},
        },
        "required": ["step", "outcome"],
        "additionalProperties": False,
    },
}

# Problems only (a bad --steps), to the file the server passes as --log. Without
# it, as in the tests, the logger is left to whoever configures logging.
log = logging.getLogger("marlow.onboarding")


class TurnState:
    """What this process (one turn) has done so far."""

    def __init__(self) -> None:
        self.advanced = False


REFUSED = ("Not yet: one step per reply, and they haven't answered this one. "
           "Ask this step's question, then wait for their answer; call step_done in a later turn.")


class ToolError(Exception):
    """A problem worth one line back to the model, never a traceback."""


def parse_steps(argv: list[str]) -> list[dict]:
    """`--steps <json>`: a list of {"id": str, "text": str}. Anything else,
    or no such argument, is an empty list: never a crash at startup."""
    if "--steps" not in argv or argv.index("--steps") + 1 >= len(argv):
        return []
    try:
        steps = json.loads(argv[argv.index("--steps") + 1])
    except ValueError:
        log.error("--steps is not JSON")
        return []
    ok = isinstance(steps, list) and all(
        isinstance(s, dict) and isinstance(s.get("id"), str) and s["id"]
        and isinstance(s.get("text"), str) and s["text"]
        for s in steps)
    if not ok:
        log.error("--steps is not a list of {id, text}")
        return []
    return [{"id": s["id"], "text": s["text"]} for s in steps]


def _check(arguments: Any) -> None:
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    known = TOOL["inputSchema"]["properties"]
    extra = sorted(set(arguments) - set(known))
    if extra:
        raise ToolError(f"unknown argument {extra[0]!r}; use step, outcome and note")
    if not isinstance(arguments.get("step"), str) or not arguments["step"]:
        raise ToolError("step is required: the step id from the onboarding step line")
    if arguments.get("outcome") not in OUTCOMES:
        raise ToolError("outcome must be 'done' or 'skipped'")
    if "note" in arguments and not isinstance(arguments["note"], str):
        raise ToolError("note must be a string")


def step_done(steps: list[dict], arguments: Any, turn: TurnState) -> str:
    _check(arguments)
    if turn.advanced:
        return REFUSED
    ids = [s["id"] for s in steps]
    if arguments["step"] not in ids:
        if steps:
            return f"That isn't the current onboarding step; the current one is {ids[0]}."
        return "Onboarding is already finished."
    at = ids.index(arguments["step"])
    turn.advanced = True
    if at == len(steps) - 1:
        return "Onboarding is finished. Close in a line; no new list of things to try."
    return f"Next, onboarding step {steps[at + 1]['id']}: {steps[at + 1]['text']}"


# --- transport -------------------------------------------------------------------

def handle(message: Any, steps: list[dict], turn: TurnState | None = None) -> dict | None:
    """One JSON-RPC message in, one response out (None for a notification).
    `turn` is the process's TurnState; without one, each call stands alone."""
    turn = turn if turn is not None else TurnState()
    if not isinstance(message, dict) or "id" not in message:
        return None
    msg_id, method = message["id"], message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def tool_result(text: str, is_error: bool) -> dict:
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [TOOL]})
    if method == "tools/call":
        if params.get("name") != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')!r}"}}
        try:
            return tool_result(step_done(steps, params.get("arguments"), turn), False)
        except ToolError as e:
            return tool_result(f"Onboarding error: {e}", True)
        except Exception as e:  # a bug costs one answer, not the server
            log.error("INTERNAL: %s: %s", type(e).__name__, e, exc_info=True)
            return tool_result(f"Onboarding error: internal ({type(e).__name__})", True)
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method {method!r}"}}


def _log_to(argv: list[str]) -> None:
    if "--log" not in argv or argv.index("--log") + 1 >= len(argv):
        return
    try:
        handler = logging.FileHandler(argv[argv.index("--log") + 1], mode="a")
    except OSError as e:  # never cost the onboarding over its log
        sys.stderr.write(f"onboarding: cannot open log ({e})\n")
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def main() -> None:
    argv = sys.argv[1:]
    _log_to(argv)
    steps = parse_steps(argv)
    turn = TurnState()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            response = handle(message, steps, turn)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

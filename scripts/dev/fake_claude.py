#!/usr/bin/env python3
"""A stand-in `claude` CLI for local client development (scripts/dev-server.sh).

It never calls a model. It speaks just enough of the stream-json contract that
the server's live paths run end to end: reply text arrives as
`--include-partial-messages` deltas (streaming), tool_use blocks become the
status-line activity, and the result envelope ends the turn. The shapes follow
tests/conftest.py's fake and tests/test_partial_text.py; those suites, not this
file, are where the contract is asserted.

Behaviour is chosen by hashtags in the message, so a client can exercise each
path by typing them. Hashtags rather than plain words because the server puts
its own context (time, gaps since the last message) ahead of the user's text,
and a plain word could turn up there.

  #approve  ask the server's permission round-trip, as the PreToolUse hook
            would, and wait for the device's answer (allow / deny / timeout)
  #tool     show two tool activities before the reply
  #long     a reply of about 4,000 characters, slowly (follow-scroll, jump)
  #fail     an error turn
  #markdown a reply exercising headings, lists, code, links and a table

Anything else gets a short streamed echo. Tags combine ("#tool #long").
"""
from __future__ import annotations

import json
import os
import sys
import http.client
import socket
import time

SESSION_ID = "dev-session-0001"


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def stream_event(inner: dict) -> None:
    emit({"type": "stream_event", "event": inner, "parent_tool_use_id": None,
          "session_id": SESSION_ID})


def stream_text(text: str, index: int, chunk: int = 24, delay: float = 0.05) -> None:
    stream_event({"type": "content_block_start", "index": index,
                  "content_block": {"type": "text", "text": ""}})
    for i in range(0, len(text), chunk):
        stream_event({"type": "content_block_delta", "index": index,
                      "delta": {"type": "text_delta", "text": text[i:i + chunk]}})
        time.sleep(delay)
    stream_event({"type": "content_block_stop", "index": index})


def tool_use(name: str, tool_input: dict, n: int) -> None:
    emit({"type": "assistant", "session_id": SESSION_ID, "parent_tool_use_id": None,
          "message": {"role": "assistant", "content": [
              {"type": "tool_use", "id": f"toolu_dev_{n}", "name": name, "input": tool_input}]}})
    time.sleep(1.2)
    emit({"type": "user", "session_id": SESSION_ID, "parent_tool_use_id": None,
          "message": {"role": "user", "content": [
              {"type": "tool_result", "tool_use_id": f"toolu_dev_{n}", "content": "ok"}]}})


class _UnixHTTPConnection(http.client.HTTPConnection):
    """server/internal_client.py's, inlined: this file is copied out of the repo."""

    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=120)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def ask_permission() -> str:
    """What hooks/pretooluse.py does for a tool that needs Mike's say-so."""
    sock = os.path.expanduser(os.environ.get("MARLOW_INTERNAL_SOCKET", "~/.marlow/run/internal.sock"))
    body = json.dumps({"tool": "Bash", "tool_input": {"command": "rm -rf ~/scratch/old-drafts"},
                       "session_id": SESSION_ID}).encode()
    try:
        conn = _UnixHTTPConnection(sock)
        conn.request("POST", "/internal/permission-request", body=body,
                     headers={"content-type": "application/json"})
        return json.loads(conn.getresponse().read()).get("decision", "deny")
    except Exception as e:  # the hook fails closed, and so does this
        return f"deny ({e.__class__.__name__})"


MARKDOWN = """## A heading

Some **bold**, some *italic*, and `inline code`. A [safe link](https://example.com)
and a [javascript link](javascript:alert(1)) that must not be clickable.

- one
- two
  - nested

1. first
2. second

```python
def hello():
    return "world"
```

| col | value |
|-----|-------|
| a   | 1     |

<img src=x onerror="alert('raw html must render as text')">
"""


def main() -> None:
    argv = sys.argv[1:]
    prompt = argv[argv.index("--") + 1] if "--" in argv else (argv[-1] if argv else "")
    tags = {w.strip(".,!?").lower() for w in prompt.split() if w.startswith("#")}
    streaming = "stream-json" in argv
    if streaming:
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
        emit({"type": "system", "subtype": "init", "session_id": SESSION_ID})

    if "#fail" in tags:
        envelope = {"type": "result", "is_error": True, "subtype": "error_during_execution",
                    "result": "", "session_id": SESSION_ID, "permission_denials": []}
        emit(envelope) if streaming else print(json.dumps(envelope))
        return

    index = 0
    reply: list[str] = []
    if "#tool" in tags and streaming:
        tool_use("Read", {"file_path": "/home/marlow/marlow-identity/notes/today.md"}, 1)
        tool_use("Grep", {"pattern": "lighthouse", "path": "/home/marlow/marlow-identity"}, 2)
    if "#approve" in tags:
        decision = ask_permission()
        reply.append(f"The permission round-trip answered: **{decision}**.\n\n")
    if "#markdown" in tags:
        reply.append(MARKDOWN)
    if "#long" in tags:
        para = ("This is a long dev reply, here to exercise follow-scroll, the jump "
                "button and the unseen-arrival dot. ") * 4
        reply.extend(f"Paragraph {i + 1}. {para}\n\n" for i in range(10))
    if not reply:
        reply.append("Dev echo. This reply streamed in small deltas, as a real one does. "
                     "Tags: #tool #approve #long #fail #markdown.")
    text = "".join(reply).strip()

    if streaming and "--include-partial-messages" in argv:
        stream_text(text, index, delay=0.08 if "#long" in tags else 0.05)
    elif streaming:
        time.sleep(1)
    envelope = {"type": "result", "is_error": False, "subtype": "success", "result": text,
                "session_id": SESSION_ID, "permission_denials": [],
                "usage": {"input_tokens": 10, "output_tokens": len(text) // 4}}
    emit(envelope) if streaming else print(json.dumps(envelope))


if __name__ == "__main__":
    main()

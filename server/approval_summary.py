"""What an approval request says to Mike, and what "similar" means for it
(docs/tasks/tool-approval-ux.md, pane redesign 2026-10-02).

Three layers, kept apart because they are not equally trustworthy:

  title       -- written HERE from the parsed tool input. A fact: "Edit
                 USER.md", "Search travel.md for 'Percé'".
  preview     -- lifted from the input itself: the lines an Edit adds and
                 removes, a connector call's key arguments. Also a fact.
  marlow_says -- the model's own one-line account (Bash's optional
                 `description`). A CLAIM, and labelled as one on every client:
                 an injected document can shape it, so it is never the only
                 thing next to an Allow button (the 2026-08-03 rule this
                 relaxes rather than drops; Mike, 2026-10-02: usefulness wins).

The raw input still goes to clients untouched; this sits beside it.

`similar_key` is the scope of "Allow similar for this reply". Mike's grant is
exercised with no one looking, so it is an ALLOWLIST of shapes where "like
this" is meaningful and bounded -- never a key on input shape (every Bash call
is `{command: str}`, so a shape key after one `ls` would pass `rm -rf`). No key
means no button.
"""
from __future__ import annotations

import difflib
import os
import re
from typing import Any
from urllib.parse import urlparse

from server.bash_policy import _decompose, _split_unquoted, _tokenize
from server.config import settings

TITLE_CHARS = 120
PREVIEW_LINES = 6
PREVIEW_LINE_CHARS = 200
MARLOW_SAYS_CHARS = 200

# --- similar: what one tap may approve for the rest of the reply -------------

# Verbs that only read. A similar grant on one of these still never covers a
# redirect, a background `&`, command substitution or a different verb in the
# same command -- see _bash_similar_key.
_SIMILAR_BASH_VERBS = frozenset({"cat", "grep", "head", "tail", "nl", "wc", "ls", "find"})

# find's options that only test or print, as an ALLOWLIST. The first version
# was a blocklist of find's write and exec actions checked against shlex
# tokens, and an adversarial pass (2026-10-02, executed against real bash)
# walked straight past it: `find . {-exec,id} {} +` tokenises to `{-exec,id}`,
# which is on no list, and the shell's brace expansion then hands find
# `-exec id`. `find . -{delete,name} zz` deleted files the same way. A token
# that is not literally a known-safe option is now refused, and the expansions
# that could assemble one are refused before tokens are looked at at all.
_FIND_SAFE_OPTIONS = frozenset({
    "-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename",
    "-regex", "-iregex", "-type", "-xtype", "-maxdepth", "-mindepth",
    "-mtime", "-mmin", "-atime", "-amin", "-ctime", "-cmin",
    "-newer", "-anewer", "-cnewer", "-size", "-empty", "-user", "-group",
    "-perm", "-links", "-samefile", "-inum", "-nouser", "-nogroup",
    "-readable", "-writable", "-executable", "-print", "-print0", "-printf",
    "-ls", "-prune", "-not", "-o", "-or", "-a", "-and", "-true", "-false",
    "-depth", "-xdev", "-mount", "-follow", "-L", "-P", "-H",
})
# A test's numeric VALUE also starts with a dash: `-mtime -1`, `-size -10k`,
# `-perm -644`. Digits with at most one unit letter cannot name an option.
_FIND_NUMERIC_VALUE = re.compile(r"[-+]?[0-9]+[a-zA-Z]?")

# Expansion the shell performs AFTER this module has read the words. Braces
# build words (`-{exec,x}`), `$` substitutes them, even inside double quotes.
# Refused anywhere in the command, quoted or not: over-refusal costs a button,
# not a hole.
_EXPANSION_CHARS = frozenset("{}$")
# Glob characters, refused for find when UNQUOTED: a glob can match a file
# named `-delete`. Quoted (`-name '*.md'`) they reach find as a pattern.
_GLOB_CHARS = frozenset("*?[")


def _unquoted_chars(text: str) -> set[str]:
    """The characters of `text` that sit outside quotes, by the same quote
    rule as bash_policy._split_unquoted. Escapes are not modelled; _decompose
    has already refused the escaped-quote shapes where that would matter."""
    found: set[str] = set()
    quote: str | None = None
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        else:
            found.add(char)
    return found

# Connector tools whose name says they only read. Sends, creates, updates and
# deletes get no button: approving one email is not approving the next.
_READ_ONLY_CONNECTOR_ACTION = re.compile(r"^(get|list|search|read|download)_")


def _bash_similar_key(command: str) -> tuple[str, ...] | None:
    if "\n" in command or "\r" in command:
        return None
    if _EXPANSION_CHARS & set(command):
        return None
    segments, _ = _decompose(command)  # refuses substitution and quote desync
    if not segments:
        return None
    verbs: set[str] = set()
    for segment in segments:
        # Redirection and backgrounding, outside quotes. _decompose has already
        # dropped `2>/dev/null` and vetted pipe sinks, so anything left is real.
        for operator in (">", "<", "&"):
            parts = _split_unquoted(segment, (operator,))
            if parts is None or len(parts) > 1:
                return None
        tokens = _tokenize(segment)
        if not tokens:
            return None
        verb = tokens[0]
        if verb not in _SIMILAR_BASH_VERBS:
            return None
        if verb == "find":
            if _GLOB_CHARS & _unquoted_chars(segment):
                return None
            for token in tokens[1:]:
                if token.startswith("-") and token not in _FIND_SAFE_OPTIONS \
                        and not _FIND_NUMERIC_VALUE.fullmatch(token):
                    return None
        verbs.add(verb)
    if len(verbs) != 1:
        return None
    return ("Bash", verbs.pop())


def similar_key(tool: str, tool_input: dict[str, Any]) -> tuple[str, ...] | None:
    """The grant key, or None if this request offers no "similar" button."""
    if tool == "Bash":
        command = tool_input.get("command")
        return _bash_similar_key(command) if isinstance(command, str) else None
    if tool in ("Edit", "Write"):
        path = tool_input.get("file_path")
        # Same tool, same file: "edits to USER.md", never "edits".
        return (tool, os.path.normpath(path)) if isinstance(path, str) and path else None
    if tool == "Read":
        path = tool_input.get("file_path")
        if isinstance(path, str) and path:
            return ("Read", os.path.dirname(os.path.normpath(path)))
        return None
    service, action = _connector_parts(tool)
    if service and _READ_ONLY_CONNECTOR_ACTION.match(action):
        return (tool,)
    return None


def similar_label(key: tuple[str, ...]) -> str:
    """What the button covers, in words. Facts only: derived from the key."""
    if key[0] == "Bash":
        return f"{key[1]} commands"
    if key[0] == "Edit":
        return f"edits to {_display_path(key[1])}"
    if key[0] == "Write":
        return f"writes to {_display_path(key[1])}"
    if key[0] == "Read":
        return f"reading files in {_display_path(key[1])}"
    service, action = _connector_parts(key[0])
    return f"{service}: {_CONNECTOR_TITLES.get(action, action.replace('_', ' '))}".rstrip()


# --- title and preview ------------------------------------------------------

_CONNECTOR_TITLES = {
    "get_message": "read an email",
    "get_thread": "read an email thread",
    "search_threads": "search your email",
    "list_labels": "list email labels",
    "create_draft": "draft an email",
    "send_message": "send an email",
    "reply": "reply to an email",
    "forward": "forward an email",
    "download_file_content": "download a Drive file",
    "read_file_content": "read a Drive file",
    "search_files": "search Drive",
    "list_recent_files": "list recent Drive files",
    "list_events": "check your calendar",
    "get_event": "read a calendar event",
    "search_events": "search your calendar",
    "create_event": "add a calendar event",
    "update_event": "change a calendar event",
    "delete_event": "delete a calendar event",
}

# Connector arguments worth showing, in order. Opaque ids are left out on
# purpose: a Drive file id means nothing at decision time (wishlist,
# 2026-08-18), and resolving it to a title is a network call on the latency
# path of a prompt (tool-approval-ux.md piece 2).
_CONNECTOR_PREVIEW_KEYS = (
    "query", "q", "to", "subject", "summary", "title", "startTime", "endTime", "start", "end",
)


def _connector_parts(tool: str) -> tuple[str, str]:
    """`mcp__claude_ai_Gmail__get_message` -> ("Gmail", "get_message")."""
    if not tool.startswith("mcp__"):
        return "", ""
    parts = tool.split("__")
    if len(parts) < 3:
        return "", ""
    server = parts[1].removeprefix("claude_ai_").replace("_", " ")
    return server, "__".join(parts[2:])


def _display_path(path: str) -> str:
    """Relative to Marlow's working dir when inside it, `~`-shortened otherwise."""
    normalized = os.path.normpath(path)
    tree = os.path.normpath(str(settings.working_dir))
    if normalized == tree:
        return "the working folder"
    if normalized.startswith(tree + os.sep):
        return normalized[len(tree) + 1:]
    home = os.path.expanduser("~")
    if normalized.startswith(home + os.sep):
        return "~" + normalized[len(home):]
    return normalized


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _lines(text: str, op: str) -> list[dict[str, str]]:
    out = []
    for line in text.splitlines():
        if line.strip():
            out.append({"op": op, "text": line[:PREVIEW_LINE_CHARS]})
        if len(out) == PREVIEW_LINES:
            break
    return out


def _edit_lines(old: str, new: str) -> list[dict[str, str]]:
    """Only the lines an Edit changes. The tool's old_string carries unchanged
    lines as anchors, so a naive old-then-new listing shows `- Drinks: tea` as
    removed and re-added around the one line that is actually new (first
    device screenshot, 2026-10-02)."""
    a, b = old.splitlines(), new.splitlines()
    out: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        out += _lines("\n".join(a[i1:i2]), "remove") + _lines("\n".join(b[j1:j2]), "add")
    return out[:PREVIEW_LINES]


def _bash_title(command: str) -> str:
    segments, _ = _decompose(command)
    first = segments[0] if segments else command
    tokens = _tokenize(first) or first.split()
    if not tokens:
        return "Run a shell command"
    if tokens[0] == "cd" and segments and len(segments) > 1:
        # `cd elsewhere && git log` -- the step after the cd is the one that
        # says what is happening; the cd only says where.
        place = _display_path(os.path.expanduser(tokens[1])) if len(tokens) > 1 else ""
        rest = _bash_title(" && ".join(segments[1:]))
        return f"{rest} in {place}" if place else rest
    verb, args = tokens[0], tokens[1:]
    bare = [a for a in args if not a.startswith("-")]
    more = len(segments) - 1 if segments else 0
    if verb == "grep" and bare:
        pattern, files = bare[0], bare[1:]
        where = ", ".join(_display_path(f) for f in files[:3]) or "input"
        title = f"Search {where} for “{_clip(pattern, 40)}”"
    elif verb in ("cat", "head", "tail", "nl", "sed"):
        files = bare[1:] if verb == "sed" else bare
        title = f"Read {', '.join(_display_path(f) for f in files[:3])}" if files else f"Run {verb}"
    elif verb == "ls":
        title = f"List {_display_path(bare[0]) if bare else 'the working folder'}"
    elif verb == "find":
        title = f"Find files in {_display_path(bare[0]) if bare else 'the working folder'}"
    elif verb == "git" and bare:
        title = f"Run git {bare[0]}"
    elif verb in ("python3", "python"):
        title = "Run a Python script"
    else:
        title = f"Run {verb}"
    if more > 0:
        title += f" (+{more} more step{'s' if more > 1 else ''})"
    return title


def summarize(tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """The `summary` object served beside the raw request."""
    title = f"Use {tool}"
    preview: list[dict[str, str]] = []
    marlow_says = None

    if tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str):
            title = _bash_title(command)
        description = tool_input.get("description")
        if isinstance(description, str) and description.strip():
            marlow_says = _clip(description, MARLOW_SAYS_CHARS)
    elif tool in ("Edit", "Write", "Read", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        shown = _display_path(path) if isinstance(path, str) and path else "a file"
        if tool == "Edit":
            title = f"Edit {shown}"
            old, new = tool_input.get("old_string"), tool_input.get("new_string")
            if isinstance(old, str) and isinstance(new, str):
                preview = _edit_lines(old, new)
        elif tool == "Write":
            exists = isinstance(path, str) and os.path.exists(path)
            title = f"{'Replace' if exists else 'Create'} {shown}"
            content = tool_input.get("content")
            if isinstance(content, str):
                preview = _lines(content, "add")
        elif tool == "Read":
            title = f"Read {shown}"
        else:
            title = f"Edit notebook {shown}"
    elif tool == "WebFetch":
        url = tool_input.get("url")
        host = urlparse(url).hostname if isinstance(url, str) else None
        title = f"Open {host}" if host else "Open a web page"
    elif tool == "WebSearch":
        query = tool_input.get("query")
        title = f"Search the web for “{_clip(query, 60)}”" if isinstance(query, str) else "Search the web"
    elif tool in ("Grep", "Glob"):
        pattern = tool_input.get("pattern")
        where = tool_input.get("path")
        title = f"Search {_display_path(where) if isinstance(where, str) else 'files'}"
        if isinstance(pattern, str):
            title += f" for “{_clip(pattern, 40)}”"
    else:
        service, action = _connector_parts(tool)
        if service:
            words = _CONNECTOR_TITLES.get(action, action.replace("_", " "))
            title = f"{service}: {words}"
            for key in _CONNECTOR_PREVIEW_KEYS:
                value = tool_input.get(key)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    preview.append({"op": "text", "text": f"{key}: {_clip(str(value), PREVIEW_LINE_CHARS)}"})

    key = similar_key(tool, tool_input)
    return {
        "title": _clip(title, TITLE_CHARS),
        "preview": preview,
        "marlow_says": marlow_says,
        "similar": {"label": similar_label(key)} if key else None,
    }


def push_text(summary: dict[str, Any]) -> tuple[str, str]:
    """(title, body) for the lock screen. Marlow's words lead the body when it
    gave any, since that is the friendliest line; otherwise the first preview
    line; otherwise a plain prompt to open the app. Every push is Review/Deny
    (Mike, 2026-10-02), so nothing here is ever approved from the lock screen."""
    body = summary.get("marlow_says")
    if not body and summary.get("preview"):
        first = summary["preview"][0]
        body = {"add": "+ ", "remove": "− "}.get(first["op"], "") + first["text"]
    return summary["title"], _clip(body or "Open Marlow to review.", MARLOW_SAYS_CHARS)

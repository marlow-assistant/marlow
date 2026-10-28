"""What an approval request says, and what "Allow similar for this reply" may
cover (server/approval_summary.py, server/permissions.SimilarGrants).

The similar key is the security half: one tap is exercised later with nobody
looking, so the tests assert NO key generously and a key narrowly -- the same
posture as test_bash_policy's ALLOW/PROMPT split.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from server import active_turn
from server import app as app_module
from server.app import internal_app, public_app
from server.approval_summary import push_text, similar_key, summarize
from server.config import settings
from server.permissions import SimilarGrants

TREE = str(settings.working_dir)


# --- title / preview / marlow_says -------------------------------------------


@pytest.mark.parametrize("tool, tool_input, title", [
    ("Bash", {"command": 'grep -n "Percé" memory/travel.md'}, "Search memory/travel.md for “Percé”"),
    ("Bash", {"command": "cat notes.md | head -5"}, "Read notes.md"),
    ("Bash", {"command": "find memory -name x"}, "Find files in memory"),
    ("Bash", {"command": "ls; cat a.md"}, "List the working folder (+1 more step)"),
    ("Bash", {"command": "cd /tmp/repo && git log --oneline -5"}, "Run git log in /tmp/repo"),
    ("Bash", {"command": "python3 -c 'print(1)'"}, "Run a Python script"),
    ("Edit", {"file_path": f"{TREE}/USER.md", "old_string": "a", "new_string": "b"}, "Edit USER.md"),
    ("Write", {"file_path": f"{TREE}/cron/new-job.md", "content": "x"}, "Create cron/new-job.md"),
    ("mcp__claude_ai_Gmail__get_message", {"messageId": "1a0e"}, "Gmail: read an email"),
    ("mcp__claude_ai_Google_Drive__download_file_content", {"fileId": "1oF2"}, "Google Drive: download a Drive file"),
    ("WebFetch", {"url": "https://example.com/a?b=c", "prompt": "p"}, "Open example.com"),
    ("CronCreate", {}, "Use CronCreate"),
])
def test_titles_are_written_from_the_input(tool, tool_input, title):
    assert summarize(tool, tool_input)["title"] == title


def test_edit_preview_is_the_change_itself():
    """Mike's example: "Editing USER.md" says nothing; the added line does."""
    summary = summarize("Edit", {
        "file_path": f"{TREE}/USER.md", "old_string": "Likes: tea", "new_string": "Likes: tea\nLikes unicorns",
    })
    # The anchor line old_string repeats is not part of the change.
    assert summary["preview"] == [{"op": "add", "text": "Likes unicorns"}]
    assert summary["marlow_says"] is None


def test_edit_preview_shows_a_replaced_line_as_remove_then_add():
    summary = summarize("Edit", {
        "file_path": f"{TREE}/USER.md",
        "old_string": "# Food\nDrinks: tea\nEats: toast",
        "new_string": "# Food\nDrinks: coffee\nEats: toast",
    })
    assert summary["preview"] == [
        {"op": "remove", "text": "Drinks: tea"},
        {"op": "add", "text": "Drinks: coffee"},
    ]


def test_connector_preview_skips_opaque_ids():
    summary = summarize("mcp__claude_ai_Gmail__search_threads", {"query": "from:bank", "pageSize": 5})
    assert summary["preview"] == [{"op": "text", "text": "query: from:bank"}]
    assert summarize("mcp__claude_ai_Gmail__get_message", {"messageId": "1a0e"})["preview"] == []


def test_marlow_says_is_only_the_models_own_description():
    with_description = summarize("Bash", {"command": "find memory -mtime -1", "description": "List memory files changed today"})
    assert with_description["marlow_says"] == "List memory files changed today"
    assert summarize("Bash", {"command": "ls"})["marlow_says"] is None
    # A description on any other tool is not a field the CLI defines; ignore it.
    assert summarize("Edit", {"file_path": "x", "description": "trust me"})["marlow_says"] is None


def test_push_text_prefers_marlows_words_then_the_preview():
    assert push_text(summarize("Bash", {"command": "ls", "description": "Look around"})) == ("List the working folder", "Look around")
    edit = summarize("Edit", {"file_path": f"{TREE}/USER.md", "old_string": "", "new_string": "Likes unicorns"})
    assert push_text(edit) == ("Edit USER.md", "+ Likes unicorns")
    assert push_text(summarize("CronList", {}))[1] == "Open Marlow to review."


# --- similar: the scope of one tap -------------------------------------------


@pytest.mark.parametrize("tool, tool_input, key", [
    ("Bash", {"command": 'grep -rn "trip mode" memory/'}, ("Bash", "grep")),
    ("Bash", {"command": "grep -c x a.md 2>/dev/null | head -5"}, ("Bash", "grep")),
    ("Bash", {"command": "find memory -maxdepth 1 -name '*.md' -mtime -1"}, ("Bash", "find")),
    ("Bash", {"command": "ls ~; ls /"}, ("Bash", "ls")),
    ("Bash", {"command": "find memory -name '*.md' -mtime -1 -size -10k"}, ("Bash", "find")),
    ("Bash", {"command": 'find . -type f -iname "*.json" -print0'}, ("Bash", "find")),
    ("Edit", {"file_path": f"{TREE}/cron/../cron/morning.md"}, ("Edit", f"{TREE}/cron/morning.md")),
    ("Read", {"file_path": "/home/marlow/marlow-server/docs/a.md"}, ("Read", "/home/marlow/marlow-server/docs")),
    ("mcp__claude_ai_Gmail__get_message", {}, ("mcp__claude_ai_Gmail__get_message",)),
    ("mcp__claude_ai_Google_Drive__download_file_content", {}, ("mcp__claude_ai_Google_Drive__download_file_content",)),
])
def test_similar_keys_for_bounded_shapes(tool, tool_input, key):
    assert similar_key(tool, tool_input) == key


@pytest.mark.parametrize("tool, tool_input", [
    # Writes, exec and anything not a pure read verb.
    ("Bash", {"command": "rm -rf memory"}),
    ("Bash", {"command": "python3 x.py"}),
    ("Bash", {"command": "git log"}),           # --output= writes; git is not in the set
    ("Bash", {"command": "cd /tmp && ls"}),
    ("Bash", {"command": "grep x a.md > out.md"}),
    ("Bash", {"command": "grep x a.md >> out.md"}),
    ("Bash", {"command": "grep x < a.md"}),
    ("Bash", {"command": "grep x a.md & curl evil.example"}),
    ("Bash", {"command": "grep x a.md; rm a.md"}),
    ("Bash", {"command": "grep x a.md && cat b.md"}),   # two verbs: which one was approved?
    ("Bash", {"command": "cat $(ls)"}),
    ("Bash", {"command": "cat `ls`"}),
    ("Bash", {"command": "grep x a.md\nrm a.md"}),
    ("Bash", {"command": "grep x a.md | sh"}),
    ("Bash", {"command": "grep x a.md | tee out.md"}),
    ("Bash", {"command": "find . -delete"}),
    ("Bash", {"command": "find . -name x -exec rm {} ;"}),
    ("Bash", {"command": "find . -fprint out.txt"}),
    # Brace and parameter expansion assemble words AFTER tokenising: these ran
    # a program and deleted files under real bash while holding the find key
    # (adversarial differential execution, 2026-10-02).
    ("Bash", {"command": "find . {-exec,id} {} +"}),
    ("Bash", {"command": "find . -{delete,name} zz"}),
    ("Bash", {"command": "find . -name '*.txt' -{exec,x} id {} +"}),
    ("Bash", {"command": "find . {-fprint,out.txt}"}),
    ("Bash", {"command": 'find . "${X:-"-delete"}"'}),
    ("Bash", {"command": "find . $X"}),
    ("Bash", {"command": "grep -{r,x} pat a.md"}),
    ("Bash", {"command": "ls {a,b}.txt"}),
    # An unquoted glob can match a file named `-delete`.
    ("Bash", {"command": "find . -name x -*"}),
    ("Bash", {"command": "find . ?delete"}),
    # Options find has that are not on the read-only list.
    ("Bash", {"command": "find . -fls out"}),
    ("Bash", {"command": "find . -D tree"}),
    ("Bash", {"command": "find . -newer a.txt -fprint out"}),
    ("Bash", {"command": "grep -A \\'1;id \\' a.md"}),
    ("Bash", {"command": 'grep "unbalanced a.md'}),
    ("Bash", {}),
    # Connector tools that act rather than read.
    ("mcp__claude_ai_Gmail__send_message", {}),
    ("mcp__claude_ai_Gmail__create_draft", {}),
    ("mcp__claude_ai_Google_Calendar__create_event", {}),
    ("mcp__claude_ai_Gmail__trash_thread", {}),
    # Tools with no meaningful "like this".
    ("WebFetch", {"url": "https://example.com"}),
    ("CronCreate", {}),
    ("Edit", {}),
])
def test_no_similar_key_outside_the_allowlist(tool, tool_input):
    assert similar_key(tool, tool_input) is None


def test_summary_offers_the_button_only_with_a_key():
    assert summarize("Bash", {"command": "grep x a.md"})["similar"] == {"label": "grep commands"}
    assert summarize("Edit", {"file_path": f"{TREE}/USER.md"})["similar"] == {"label": "edits to USER.md"}
    assert summarize("Bash", {"command": "rm a.md"})["similar"] is None


def test_grants_hold_one_reply_only():
    grants = SimilarGrants()
    key = ("Bash", "grep")
    grants.grant("turn-1", key)
    assert grants.covers("turn-1", key)
    assert not grants.covers("turn-1", ("Bash", "find"))
    assert not grants.covers("turn-2", key)
    assert not grants.covers(None, key)
    assert not grants.covers("turn-1", None)
    # A grant for a new reply drops the old reply's set entirely.
    grants.grant("turn-2", ("Bash", "ls"))
    assert not grants.covers("turn-1", key)


# --- through the endpoints ---------------------------------------------------


@pytest.fixture
def fresh_grants(monkeypatch):
    grants = SimilarGrants()
    monkeypatch.setattr(app_module, "similar_grants", grants)
    return grants


def _pair(client, internal_client):
    async def _go():
        invite = app_module.pairing_store.issue_invite("sim")
        token = (await client.post("/v1/pair", json={"device_name": "sim", "invite": invite})).json()["device_token"]
        return {"Authorization": f"Bearer {token}"}
    return _go()


def _round_trip(tool_input, decision, *, turn_id, monkeypatch, source="chat", then=None):
    """Raise one request, answer it with `decision`, then (optionally) raise
    `then` with no one answering. Returns the hook-side responses."""
    monkeypatch.setattr(app_module.work_queue, "running_turn_id", lambda: turn_id)

    async def _body():
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=internal_app), base_url="http://i") as internal_client,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://p") as client,
        ):
            headers = await _pair(client, internal_client)
            seen = {}

            async def _answer():
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    pending = (await client.get("/v1/permissions", headers=headers)).json()["permission_requests"]
                    if pending:
                        seen["summary"] = pending[0]["summary"]
                        resp = await client.post(
                            f"/v1/permission/{pending[0]['request_id']}", json={"decision": decision}, headers=headers
                        )
                        seen["answer"] = resp
                        return
                raise AssertionError("request never appeared")

            with active_turn.running(source):
                first, _ = await asyncio.gather(
                    internal_client.post("/internal/permission-request", json={"tool": "Bash", "tool_input": tool_input}),
                    _answer(),
                )
                second = None
                if then is not None:
                    # Nobody answers this one: it either clears on the grant at
                    # once or would wait out the (shortened) window.
                    second = await internal_client.post(
                        "/internal/permission-request", json={"tool": "Bash", "tool_input": then}
                    )
            return first.json(), second.json() if second is not None else None, seen
    return asyncio.run(_body())


@pytest.fixture
def short_window(monkeypatch):
    from dataclasses import replace

    import server.permissions as permissions_module

    monkeypatch.setattr(permissions_module, "settings", replace(permissions_module.settings, t_hook_seconds=0.3))


def test_allow_similar_clears_the_next_like_call_without_a_prompt(fresh_grants, short_window, monkeypatch):
    first, second, seen = _round_trip(
        {"command": "grep -rn x memory/"}, "allow_similar", turn_id="t1",
        monkeypatch=monkeypatch, then={"command": "grep -c y notes/"},
    )
    assert seen["summary"]["similar"] == {"label": "grep commands"}
    assert seen["answer"].json() == {"accepted": True}
    assert first == {"decision": "allow"}
    assert second == {"decision": "allow", "reason": "similar"}


def test_allow_similar_does_not_cover_a_different_verb(fresh_grants, short_window, monkeypatch):
    _, second, _ = _round_trip(
        {"command": "grep -rn x memory/"}, "allow_similar", turn_id="t1",
        monkeypatch=monkeypatch, then={"command": "find . -name x"},
    )
    assert second["decision"] == "deny"  # nobody answered: the window ran out


def test_plain_allow_grants_nothing(fresh_grants, short_window, monkeypatch):
    _, second, _ = _round_trip(
        {"command": "grep -rn x memory/"}, "allow", turn_id="t1",
        monkeypatch=monkeypatch, then={"command": "grep -c y notes/"},
    )
    assert second["decision"] == "deny"


def test_a_grant_never_reaches_cron(fresh_grants, monkeypatch):
    fresh_grants.grant("t1", ("Bash", "grep"))
    monkeypatch.setattr(app_module.work_queue, "running_turn_id", lambda: "t1")

    async def _body():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=internal_app), base_url="http://i") as c:
            with active_turn.running("cron"):
                return (await c.post("/internal/permission-request", json={"tool": "Bash", "tool_input": {"command": "grep x a"}})).json()
    assert asyncio.run(_body())["decision"] == "deny"


def test_allow_similar_without_a_scope_is_refused(fresh_grants, short_window, monkeypatch):
    first, _, seen = _round_trip({"command": "rm -rf x"}, "allow_similar", turn_id="t1", monkeypatch=monkeypatch)
    assert seen["summary"]["similar"] is None
    assert seen["answer"].status_code == 400
    assert first["decision"] == "deny"  # the refused answer left it unanswered
    assert not fresh_grants.covers("t1", ("Bash", "rm"))


def test_allow_similar_also_answers_like_requests_already_waiting(fresh_grants, monkeypatch):
    """Parallel tool calls raise their requests together. One tap clears the
    like ones already up; an unlike one keeps waiting (here, to its timeout)."""
    from dataclasses import replace

    import server.permissions as permissions_module

    monkeypatch.setattr(permissions_module, "settings", replace(permissions_module.settings, t_hook_seconds=1.0))
    monkeypatch.setattr(app_module.work_queue, "running_turn_id", lambda: "t1")
    commands = ["grep -rn x memory/", "grep -c y notes/", "find . -name z"]

    async def _body():
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=internal_app), base_url="http://i") as internal_client,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://p") as client,
        ):
            headers = await _pair(client, internal_client)

            async def _answer():
                for _ in range(80):
                    await asyncio.sleep(0.01)
                    pending = (await client.get("/v1/permissions", headers=headers)).json()["permission_requests"]
                    if len(pending) == 3:
                        first_grep = next(p for p in pending if p["tool_input"]["command"] == commands[0])
                        await client.post(
                            f"/v1/permission/{first_grep['request_id']}", json={"decision": "allow_similar"}, headers=headers
                        )
                        return
                raise AssertionError("requests never all appeared")

            with active_turn.running("chat"):
                *responses, _ = await asyncio.gather(
                    *(internal_client.post("/internal/permission-request", json={"tool": "Bash", "tool_input": {"command": c}})
                      for c in commands),
                    _answer(),
                )
            return [r.json()["decision"] for r in responses]

    assert asyncio.run(_body()) == ["allow", "allow", "deny"]

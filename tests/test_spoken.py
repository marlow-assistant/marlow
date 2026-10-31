"""Voice mode, server half (docs/tasks/voice-mode-hands-free.md):
a message the user spoke carries `spoken`, and Marlow is told its reply will be
read aloud — on that message only."""
from __future__ import annotations

import asyncio
import textwrap

from fastapi.testclient import TestClient

from conftest import write_executable
import server.app as app_module
from server.app import internal_app, public_app
from server.invoke import run_claude
from server.queue import WorkQueue
from server.turn_context import SPOKEN_TURN_NOTE
from tests.test_app import _paired_headers, isolated_pairing_state  # noqa: F401 (fixture)
from tests.test_invoke import _with_claude_bin
from tests.test_queue import _run_to_completion, _success


# --- the request ------------------------------------------------------------------

def test_a_spoken_message_queues_a_spoken_turn(isolated_pairing_state, monkeypatch):
    async def fake_run(prompt, **kwargs):
        return _success()

    monkeypatch.setattr("server.queue.run_claude", fake_run)
    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        headers, _ = _paired_headers(client, internal_client, "voice-device")
        spoken = client.post(
            "/v1/message", json={"text": "what's on today", "client_msg_id": "v-1", "spoken": True},
            headers=headers,
        )
        typed = client.post("/v1/message", json={"text": "and tomorrow", "client_msg_id": "v-2"}, headers=headers)
        assert spoken.status_code == typed.status_code == 202
        assert app_module.work_queue.get_turn(spoken.json()["turn_id"]).spoken is True
        assert app_module.work_queue.get_turn(typed.json()["turn_id"]).spoken is False


def test_a_malformed_spoken_value_never_costs_the_message(isolated_pairing_state, monkeypatch):
    async def fake_run(prompt, **kwargs):
        return _success()

    monkeypatch.setattr("server.queue.run_claude", fake_run)
    with TestClient(public_app) as client, TestClient(internal_app) as internal_client:
        headers, _ = _paired_headers(client, internal_client, "voice-bad")
        for i, bad in enumerate((None, "yes", 1, {})):
            resp = client.post(
                "/v1/message", json={"text": "hi", "client_msg_id": f"v-bad-{i}", "spoken": bad},
                headers=headers,
            )
            assert resp.status_code == 202, bad
            assert app_module.work_queue.get_turn(resp.json()["turn_id"]).spoken is False


# --- the queue --------------------------------------------------------------------

def test_spoken_and_typed_messages_never_share_an_invocation(monkeypatch):
    """One note covers a whole merged prompt, so a merge across the boundary
    would read a typed message as spoken, or the reverse."""
    calls = []

    async def fake_run(prompt, **kwargs):
        calls.append((prompt, kwargs["spoken"]))
        return _success(kwargs.get("session_id") or "fresh")

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        ids = [
            wq.enqueue("first", session_id="s", resume=True, source="chat", spoken=True),
            wq.enqueue("boundary", session_id="s", resume=True, source="chat"),
            wq.enqueue("last", session_id="s", resume=True, source="chat", spoken=True),
        ]
        for turn_id in ids:
            await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())
    assert calls == [("first", True), ("boundary", False), ("last", True)]


def test_consecutive_spoken_messages_still_coalesce(monkeypatch):
    """The flag must not quietly switch coalescing off: `_can_coalesce` fails
    closed on any unclassified Turn field."""
    calls = []
    release = asyncio.Event()

    async def fake_run(prompt, **kwargs):
        calls.append((prompt, kwargs["spoken"]))
        if prompt == "blocker":
            await release.wait()
        return _success()

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        blocker = wq.enqueue("blocker", session_id="s", resume=True, source="chat")
        await asyncio.sleep(0.05)
        a = wq.enqueue("one", session_id="s", resume=True, source="chat", spoken=True)
        b = wq.enqueue("two", session_id="s", resume=True, source="chat", spoken=True)
        release.set()
        for turn_id in (blocker, a, b):
            await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())
    assert calls == [("blocker", False), ("one\n\ntwo", True)]


# --- the prompt -------------------------------------------------------------------

def _echo_bin(tmp_path, monkeypatch):
    fake_bin = tmp_path / "claude"
    write_executable(fake_bin, textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print(json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": sys.argv[-1],
                "session_id": "s1",
                "permission_denials": [],
            }))
            """
        ))
    _with_claude_bin(monkeypatch, str(fake_bin))


def test_a_spoken_turn_puts_the_note_directly_above_the_message(tmp_path, monkeypatch):
    _echo_bin(tmp_path, monkeypatch)
    prompt = asyncio.run(run_claude("what's on today", spoken=True)).result_text
    assert prompt.endswith(f"{SPOKEN_TURN_NOTE}\nwhat's on today")


def test_a_typed_turn_has_no_note(tmp_path, monkeypatch):
    _echo_bin(tmp_path, monkeypatch)
    assert "[Spoken:" not in asyncio.run(run_claude("what's on today")).result_text


def test_the_note_survives_a_turn_context_that_fails_to_build(tmp_path, monkeypatch):
    """The context block returns "" on any failure; the note is the one thing
    voice mode needs and must not go with it."""
    _echo_bin(tmp_path, monkeypatch)

    def broken():
        raise RuntimeError("pairing store unreadable")

    monkeypatch.setattr("server.turn_context.pairing_store.latest_timezone", broken)
    prompt = asyncio.run(run_claude("hi", spoken=True)).result_text
    assert prompt == f"{SPOKEN_TURN_NOTE}\nhi"

import asyncio
import dataclasses
import inspect
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from server import auth_monitor, transcript
from server.attachments import turn_attachment_dir, write_attachments
from server.config import settings as real_settings
from server.invoke import ActivityEvent, InvocationResult
from server.push import PushType
from server.session_state import session_state
from server.queue import (
    _COALESCING_CLASSIFIED_TURN_FIELDS,
    Turn,
    TurnStatus,
    WorkQueue,
)


async def _run_to_completion(wq: WorkQueue, turn_id: str, timeout: float = 5.0):
    """Wait for a turn's STATUS to go terminal.

    Note what this does NOT wait for. `_drain` stamps the status in its
    `finally` (queue.py, `turn.finished_at = time.time()`) and only reaches
    `_evict_old_turns()`/`_prune_client_msg_index()` at the very bottom of the
    same iteration -- with a push broadcast and a `commit_after_turn` thread
    hop in between. A test asserting on either of those housekeeping passes
    must wait for the pass itself, not for this. See `_wait_for` below.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        turn = wq.get_turn(turn_id)
        if turn.status in (TurnStatus.DONE, TurnStatus.ERROR, TurnStatus.MERGED):
            return turn
        await asyncio.sleep(0.02)
    raise AssertionError(f"turn {turn_id} did not finish within {timeout}s")


async def _wait_for(condition, what: str, timeout: float = 5.0):
    """Wait for a post-condition the consumer reaches after a turn is terminal.

    Sound only for conditions that latch -- eviction removes turns and nothing
    puts them back -- so this cannot pass for the wrong reason; it either
    observes the pass or fails saying which one never came.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{what} did not happen within {timeout}s")


def test_single_invocation_runs_to_done():
    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE
        assert turn.result.result_text == "fake response"
        await wq.stop()

    asyncio.run(_body())


def test_enqueue_rejects_a_foreign_thread() -> None:
    async def _body() -> None:
        wq = WorkQueue()
        wq.start()
        try:
            with pytest.raises(RuntimeError, match="owning event loop"):
                await asyncio.to_thread(wq.enqueue, "hello", source="chat")

            turn_id = wq.enqueue("still healthy", source="chat")
            turn = await _run_to_completion(wq, turn_id)
            assert turn.status == TurnStatus.DONE
        finally:
            await wq.stop()

    asyncio.run(_body())


def test_queue_measures_wait_from_turn_creation_to_invocation(monkeypatch):
    calls = []

    async def _fake_run_claude(prompt, **kwargs):
        calls.append(kwargs["queue_wait_seconds"])
        return InvocationResult(
            exit_code=0, is_error=False, subtype="success", result_text="ok",
            session_id="s1", permission_denials=[],
        )

    monkeypatch.setattr("server.queue.run_claude", _fake_run_claude)
    monkeypatch.setattr("server.queue.time.time", lambda: 123.456)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn = Turn(
            turn_id="queued-turn", prompt="hello", session_id=None, resume=False,
            source="chat", client_msg_id=None, created_at=120.0,
        )
        wq._turns[turn.turn_id] = turn
        wq._queue.put_nowait(turn.turn_id)
        await _run_to_completion(wq, turn.turn_id)
        await wq.stop()

    asyncio.run(_body())
    assert calls == [pytest.approx(3.456)]


def test_evict_old_turns_caps_at_the_limit_oldest_terminal_first():
    """docs/tasks/turn-retention.md: a plain count cap on `_turns`. Populated
    directly rather than run through `_drain`, so the eviction logic is tested
    in isolation from invocation. `finished_at` set well outside the grace
    window so the cap itself, not the grace floor, is what's under test."""
    from server.queue import MAX_RETAINED_TURNS

    wq = WorkQueue()
    overflow = 10
    long_ago = time.time() - 3600
    for i in range(MAX_RETAINED_TURNS + overflow):
        turn_id = f"turn-{i}"
        client_msg_id = f"msg-{i}"
        wq._turns[turn_id] = Turn(
            turn_id=turn_id, prompt="hi", session_id=None, resume=False,
            source="chat", client_msg_id=client_msg_id, status=TurnStatus.DONE,
            finished_at=long_ago,
        )
        wq._client_msg_index[client_msg_id] = (turn_id, long_ago)

    wq._evict_old_turns()

    assert len(wq._turns) == MAX_RETAINED_TURNS
    # Oldest (lowest-numbered, first-inserted) turns are the ones evicted.
    for i in range(overflow):
        assert f"turn-{i}" not in wq._turns
        assert f"msg-{i}" not in wq._client_msg_index
    for i in range(overflow, MAX_RETAINED_TURNS + overflow):
        assert f"turn-{i}" in wq._turns


def test_evict_old_turns_never_touches_pending_or_running():
    from server.queue import MAX_RETAINED_TURNS

    wq = WorkQueue()
    wq._turns["held"] = Turn(
        turn_id="held", prompt="hi", session_id=None, resume=False,
        source="chat", client_msg_id=None, status=TurnStatus.PENDING,
    )
    long_ago = time.time() - 3600
    for i in range(MAX_RETAINED_TURNS + 5):
        turn_id = f"done-{i}"
        wq._turns[turn_id] = Turn(
            turn_id=turn_id, prompt="hi", session_id=None, resume=False,
            source="chat", client_msg_id=None, status=TurnStatus.DONE,
            finished_at=long_ago,
        )

    wq._evict_old_turns()

    assert "held" in wq._turns
    assert len(wq._turns) == MAX_RETAINED_TURNS


def test_evict_old_turns_respects_the_grace_period():
    """docs/tasks/turn-retention.md, review 2026-08-19: nothing records that a
    client fetched a turn (GET /v1/turn/{id} is a plain read), so a
    just-finished turn must survive a count-cap overflow until it's had time
    to be fetched."""
    from server.queue import MAX_RETAINED_TURNS

    wq = WorkQueue()
    now = time.time()
    for i in range(MAX_RETAINED_TURNS + 10):
        turn_id = f"turn-{i}"
        wq._turns[turn_id] = Turn(
            turn_id=turn_id, prompt="hi", session_id=None, resume=False,
            source="chat", client_msg_id=None, status=TurnStatus.DONE,
            finished_at=now,
        )

    wq._evict_old_turns()

    assert len(wq._turns) == MAX_RETAINED_TURNS + 10, (
        "every turn just finished; none should be evicted yet"
    )


def test_drain_evicts_old_turns_end_to_end(monkeypatch):
    monkeypatch.setattr("server.queue.MAX_RETAINED_TURNS", 2)
    monkeypatch.setattr("server.queue.EVICTION_GRACE_SECONDS", 0.0)

    async def _body():
        wq = WorkQueue()
        wq.start()
        for _ in range(4):
            turn_id = wq.enqueue("hi", source="chat")
            await _run_to_completion(wq, turn_id)
        # Wait for the eviction pass, and do it BEFORE `stop()`.
        #
        # This test used to assert straight after the last turn's status went
        # terminal, which is not when eviction runs: `_drain` reaches
        # `_evict_old_turns()` at the bottom of the same iteration, past a push
        # broadcast and a `commit_after_turn` thread hop. For turns 1-3 that is
        # invisible, because the next turn cannot be picked up until the
        # iteration carrying it finishes -- but the LAST turn has `stop()`
        # cancelling the consumer right behind it, and a cancel landing in that
        # window leaves four turns retained where the cap is two.
        #
        # Two things have to coincide, which is why it failed once in a
        # pre-push run on 2026-09-07 and then passed four full-suite runs and
        # 120 repeats under 8-way CPU load. Reproduced deliberately by forcing
        # both (slow `commit_after_turn`, and a gap between turns longer than
        # it): the old ordering retains THREE, this one retains two.
        #
        #   1. Turn 4 is enqueued AFTER iteration 3's eviction pass. Normally
        #      it is not -- this loop's 20ms status poll means the next enqueue
        #      lands mid-tail, so turn 4 is already in `_turns` when iteration
        #      3 evicts, and the cap is satisfied without iteration 4 ever
        #      running its own pass. That accident is what made the assertion
        #      look sound.
        #   2. Iteration 4's tail is slow enough that `stop()`'s cancel lands
        #      inside it. A contended thread pool late in a full-suite run does
        #      exactly that to the `commit_after_turn` hop.
        #
        # This matters more here than the usual flaky-test argument: CI is off,
        # so `.githooks/pre-push` is the only gate that runs, and a gate that
        # fails at random is how `--no-verify` becomes a habit.
        await _wait_for(
            lambda: len(wq._turns) == 2, "the eviction pass after the last turn"
        )
        await wq.stop()
        return wq

    wq = asyncio.run(_body())
    assert len(wq._turns) == 2


def test_client_msg_index_window_is_independent_of_the_turns_cap(monkeypatch):
    """docs/tasks/turn-retention.md: resending a client_msg_id after
    CLIENT_MSG_ID_WINDOW_SECONDS gets a fresh turn, even though the original
    turn is still sitting in `_turns` (nowhere near MAX_RETAINED_TURNS)."""
    monkeypatch.setattr("server.queue.CLIENT_MSG_ID_WINDOW_SECONDS", 0.0)

    async def _body():
        wq = WorkQueue()
        wq.start()
        first = wq.enqueue("hello", client_msg_id="abc", source="chat")
        await _run_to_completion(wq, first)
        second = wq.enqueue("hello again", client_msg_id="abc", source="chat")
        await wq.stop()
        return first, second

    first, second = asyncio.run(_body())
    assert first != second, "the stale client_msg_id must not dedup a new send"


def test_resend_same_client_msg_id_is_idempotent():
    async def _body():
        wq = WorkQueue()
        wq.start()
        first = wq.enqueue("hello", client_msg_id="abc", source="chat")
        second = wq.enqueue("hello again", client_msg_id="abc", source="chat")
        assert first == second
        assert wq.depth <= 1  # only ever enqueued once
        await _run_to_completion(wq, first)
        await wq.stop()

    asyncio.run(_body())


def test_resend_after_an_errored_turn_runs_it_again(monkeypatch):
    """The phone's retry button resends under the same client_msg_id. When the
    TURN failed (an API 522, 2026-09-28) rather than the send, the dedupe used
    to hand back the ERROR turn, so retry replayed the failure without ever
    running `claude -p`."""
    outcomes = [True, False]  # is_error per call: the first fails, the retry works
    prompts = []

    async def _fake(prompt, **kwargs):
        prompts.append(prompt)
        failed = outcomes.pop(0)
        return InvocationResult(
            exit_code=1 if failed else 0, is_error=failed, subtype=None,
            result_text=None if failed else "ok", session_id="s1",
        )

    monkeypatch.setattr("server.queue.run_claude", _fake)

    async def _body():
        wq = WorkQueue()
        wq.start()
        first = wq.enqueue("hello", client_msg_id="abc", source="chat")
        assert (await _run_to_completion(wq, first)).status == TurnStatus.ERROR

        # The image path pre-mints its turn_id and writes the attachment under
        # it before enqueueing; a fresh turn must use that id, not orphan it.
        retry = wq.enqueue("hello", client_msg_id="abc", source="chat", turn_id="minted")
        assert retry == "minted"
        assert (await _run_to_completion(wq, retry)).status == TurnStatus.DONE

        # A network resend of the retry is still deduped -- onto the new turn.
        assert wq.enqueue("hello", client_msg_id="abc", source="chat") == retry
        assert wq.get_turn(first).status == TurnStatus.ERROR
        await wq.stop()

    asyncio.run(_body())
    assert prompts == ["hello", "hello"]


def test_error_reason_from_invoke_becomes_turn_error(monkeypatch):
    """invoke.py's short reason is what the phone shows in place of
    "Something went wrong"."""
    async def _fake(prompt, **kwargs):
        return InvocationResult(
            exit_code=1, is_error=True, subtype=None, result_text=None,
            session_id="s1", error_reason="Anthropic's API timed out — try again.",
        )

    monkeypatch.setattr("server.queue.run_claude", _fake)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn = await _run_to_completion(wq, wq.enqueue("hello", source="chat"))
        await wq.stop()
        return turn

    turn = asyncio.run(_body())
    assert turn.status == TurnStatus.ERROR
    assert turn.error == "Anthropic's API timed out — try again."



def test_latest_attended_session_id_none_when_only_cron_turns():
    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("do the scan", source="cron")
        await _run_to_completion(wq, turn_id)
        assert wq.conversation_session_id() is None
        await wq.stop()

    asyncio.run(_body())


def test_a_conversation_turn_records_the_conversation_session():
    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat", in_conversation=True)
        turn = await _run_to_completion(wq, turn_id)
        assert wq.conversation_session_id() == turn.result.session_id
        await wq.stop()

    asyncio.run(_body())


def test_a_failed_turn_does_not_move_the_conversation(tmp_path, monkeypatch):
    """An ERROR'd turn has no trustworthy session id, so it must not overwrite
    the last good one -- otherwise one failed phone message at 22:00 would
    blind that night's consolidate run.

    Driven through the queue rather than by hand-building `_turns`, because
    the recording now happens in `_drain` on completion (server/session_state.py)
    and a hand-built dict would prove nothing about it.
    """
    import textwrap

    async def _good_turn():
        wq = WorkQueue()
        wq.start()
        turn = await _run_to_completion(wq, wq.enqueue("hello", source="chat", in_conversation=True))
        await wq.stop()
        return turn.result.session_id

    good = asyncio.run(_good_turn())
    assert good

    fake_bin = tmp_path / "claude"
    fake_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            sys.stderr.write("boom\\n")
            sys.exit(1)
            """
        )
    )
    fake_bin.chmod(0o755)
    monkeypatch.setattr("server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(fake_bin)))

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn = await _run_to_completion(wq, wq.enqueue("hello again", source="chat", in_conversation=True))
        assert turn.status == TurnStatus.ERROR
        assert wq.conversation_session_id() == good
        await wq.stop()

    asyncio.run(_body())


def _recording_run_claude(monkeypatch, session_for=lambda sid: sid or "fresh-session"):
    """Stub run_claude that records (session_id, resume) per call and answers
    in `session_for(asked)` -- resuming S returns S, as measured on the CLI."""
    calls = []

    async def _fake(prompt, **kwargs):
        calls.append((kwargs.get("session_id"), kwargs.get("resume")))
        return InvocationResult(
            exit_code=0, is_error=False, subtype="success", result_text="ok",
            session_id=session_for(kwargs.get("session_id")), permission_denials=[],
        )

    monkeypatch.setattr("server.queue.run_claude", _fake)
    return calls


def test_phone_and_cron_turns_share_one_conversation(monkeypatch):
    """The 2026-09-25 change. A job used to run in a fresh session, blind to
    the chat, and the chat never saw what the job sent Mike. Now both resume
    the one conversation, in either order."""
    calls = _recording_run_claude(monkeypatch)

    async def _body():
        wq = WorkQueue()
        wq.start()
        await _run_to_completion(wq, wq.enqueue("hi", source="chat", in_conversation=True))
        await _run_to_completion(wq, wq.enqueue("brief", source="cron", in_conversation=True))
        await _run_to_completion(wq, wq.enqueue("re: brief", source="chat", in_conversation=True))
        await wq.stop()

    asyncio.run(_body())
    assert calls == [
        (None, False),
        ("fresh-session", True),
        ("fresh-session", True),
    ]

def test_the_conversation_survives_a_restart():
    """The regression this whole mechanism exists for.

    On 2026-08-15 the box logged "cron job 'consolidate' skipped: no attended
    (ios) session to attach to" at 23:55. The day's last phone turn was 05:42
    and the server restarted at 05:50, so the in-memory scan this lookup used
    to do found nothing. Nothing self-corrected: the phone holds its session id
    on the device, so the same conversation ran on to 94 turns over five days.

    A fresh WorkQueue with an empty `_turns` is exactly what a restart looks
    like from this lookup's point of view.
    """
    async def _body():
        wq = WorkQueue()
        wq.start()
        turn = await _run_to_completion(wq, wq.enqueue("hello", source="chat", in_conversation=True))
        await wq.stop()
        return turn.result.session_id

    session_id = asyncio.run(_body())
    assert session_id

    after_restart = WorkQueue()
    assert after_restart._turns == {}, "not a restart if the turns came along"
    assert after_restart.conversation_session_id() == session_id


def test_retirement_survives_a_restart():
    """The other half, same reasoning. Consolidate runs at 01:00 and Mike's
    first message lands hours later; a deploy or a `Restart=always` bounce in
    between must not silently un-retire the session and hand him back the
    conversation the rollover was supposed to cap."""
    async def _body():
        wq = WorkQueue()
        wq.start()
        turn = await _run_to_completion(wq, wq.enqueue(
            "review today", session_id="attended-1", resume=True, source="cron",
            require_session=True, retire_session_on_success=True,
        ))
        assert turn.status == TurnStatus.DONE
        await wq.stop()
        return turn.result.session_id

    landed = asyncio.run(_body())

    after_restart = WorkQueue()
    assert after_restart.is_session_retired("attended-1") is True
    assert after_restart.is_session_retired(landed) is True


def test_post_system_message_appears_as_a_done_turn_without_invoking_claude():
    wq = WorkQueue()  # not started — post_system_message never touches invoke.py
    turn_id = wq.post_system_message("I wanted to run X but couldn't reach you in time — skipped it.")
    turn = wq.get_turn(turn_id)
    assert turn.status == TurnStatus.DONE
    assert turn.source == "system"
    assert turn.result.result_text == "I wanted to run X but couldn't reach you in time — skipped it."

    rows = transcript.read_after(0)["rows"]
    assert [(r["turn_id"], r["source"]) for r in rows] == [(turn_id, "system")]


def test_post_system_message_dedupes_when_cron_retries_the_same_reminder():
    wq = WorkQueue()
    first = wq.post_system_message("Go to Costco", client_msg_id="costco-2099-08-28-1400")
    second = wq.post_system_message("Go to Costco", client_msg_id="costco-2099-08-28-1400")

    assert second == first
    assert len(wq._turns) == 1


def test_schedule_sync_runs_after_turn_before_identity_commit(monkeypatch):
    calls = []
    monkeypatch.setattr("server.queue.sync_after_turn", lambda: calls.append("sync"))
    monkeypatch.setattr("server.queue.commit_after_turn", lambda *_: calls.append("commit"))

    async def _body():
        wq = WorkQueue()
        wq.start()
        await asyncio.sleep(0.05)  # let the separate startup sync finish
        assert calls == ["sync"]
        calls.clear()
        turn_id = wq.enqueue("hello", source="chat")
        await _run_to_completion(wq, turn_id)
        deadline = asyncio.get_running_loop().time() + 1
        while calls != ["sync", "commit"] and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        await wq.stop()

    asyncio.run(_body())

    assert calls == ["sync", "commit"]


def test_identity_load_error_preserves_specific_message(monkeypatch, tmp_path):
    # Code-review finding: queue.py's bare except-Exception was collapsing a
    # deliberately fail-loud IdentityLoadError into a generic "internal error",
    # hiding the actionable message from the caller. invoke.py now re-raises it
    # as InvokeError so queue's InvokeError handler preserves str(e).
    monkeypatch.setattr(
        "server.invoke.settings",
        dataclasses.replace(real_settings, soul_path=tmp_path / "missing-soul.md"),
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.ERROR
        assert turn.error is not None
        assert turn.error != "internal error"
        assert "SOUL.md" in turn.error
        await wq.stop()

    asyncio.run(_body())


def _fake_auth_status(reauth_needed: bool) -> auth_monitor.AuthStatus:
    now = datetime.now(timezone.utc)
    return auth_monitor.AuthStatus(
        credentials_found=True,
        access_token_expires_at=now + timedelta(hours=1),
        refresh_token_expires_at=(now - timedelta(hours=1)) if reauth_needed else (now + timedelta(days=9)),
        subscription_type="pro",
    )


def test_turn_held_not_errored_while_reauth_needed_then_runs_after_recovery(monkeypatch):
    # Plan §6a: a held turn must stay PENDING, not ERROR — an ERROR here
    # would read as a genuine refusal instead of a dead-token/auth problem.
    state = {"reauth_needed": True}
    monkeypatch.setattr(
        "server.queue.auth_monitor.get_status",
        lambda: _fake_auth_status(state["reauth_needed"]),
    )
    monkeypatch.setattr(
        "server.queue.settings", dataclasses.replace(real_settings, auth_recheck_interval_seconds=0.02)
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")

        # Give the drain loop a few cycles to notice reauth_needed and hold.
        await asyncio.sleep(0.1)
        turn = wq.get_turn(turn_id)
        assert turn.status == TurnStatus.PENDING
        assert wq.paused is True

        # Auth recovers — the held turn should now run to completion without
        # ever having been marked ERROR.
        state["reauth_needed"] = False
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE
        assert wq.paused is False
        await wq.stop()

    asyncio.run(_body())


def test_ordering_preserved_across_a_reauth_pause(monkeypatch):
    state = {"reauth_needed": True}
    monkeypatch.setattr(
        "server.queue.auth_monitor.get_status",
        lambda: _fake_auth_status(state["reauth_needed"]),
    )
    monkeypatch.setattr(
        "server.queue.settings", dataclasses.replace(real_settings, auth_recheck_interval_seconds=0.02)
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        ids = [wq.enqueue(f"turn {i}", source="chat") for i in range(3)]

        await asyncio.sleep(0.1)  # let the drain loop hold on the first turn
        for turn_id in ids:
            assert wq.get_turn(turn_id).status == TurnStatus.PENDING

        state["reauth_needed"] = False
        turns = [await _run_to_completion(wq, turn_id) for turn_id in ids]
        assert [turn.status for turn in turns] == [
            TurnStatus.MERGED, TurnStatus.MERGED, TurnStatus.DONE,
        ]
        await wq.stop()

    asyncio.run(_body())


def test_reauth_push_fires_exactly_once_across_many_recheck_polls(monkeypatch):
    # An overnight expiry would otherwise produce ~1000 identical pushes by
    # the time Mike wakes up — same reasoning as the log heartbeat.
    state = {"reauth_needed": True}
    monkeypatch.setattr(
        "server.queue.auth_monitor.get_status",
        lambda: _fake_auth_status(state["reauth_needed"]),
    )
    monkeypatch.setattr(
        "server.queue.settings", dataclasses.replace(real_settings, auth_recheck_interval_seconds=0.01)
    )
    calls = []

    def _fake_broadcast_nowait(payload):
        calls.append(payload)

    monkeypatch.setattr("server.queue.push_broadcast_nowait", _fake_broadcast_nowait)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")

        await asyncio.sleep(0.15)  # several recheck polls at a 0.01s interval
        assert wq.paused is True
        assert len(calls) == 1  # fired once total on entering the pause, not per poll
        assert calls[0].type == PushType.REAUTH_NEEDED

        state["reauth_needed"] = False
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE
        await wq.stop()

    asyncio.run(_body())


def test_turn_ready_push_dispatched_on_completion(monkeypatch):
    calls = []

    def _fake_broadcast_nowait(payload):
        calls.append(payload)

    monkeypatch.setattr("server.queue.push_broadcast_nowait", _fake_broadcast_nowait)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        await _run_to_completion(wq, turn_id)
        deadline = asyncio.get_running_loop().time() + 1
        while not calls and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        await wq.stop()
        return turn_id

    turn_id = asyncio.run(_body())

    assert len(calls) == 1
    assert calls[0].type == PushType.TURN_READY
    assert calls[0].ref == turn_id


def test_post_system_message_works_while_queue_paused_for_reauth(monkeypatch):
    monkeypatch.setattr(
        "server.queue.auth_monitor.get_status",
        lambda: _fake_auth_status(True),
    )
    monkeypatch.setattr(
        "server.queue.settings", dataclasses.replace(real_settings, auth_recheck_interval_seconds=0.02)
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        wq.enqueue("hello", source="chat")  # will be held, queue is now paused
        await asyncio.sleep(0.1)
        assert wq.paused is True

        # post_system_message bypasses claude -p / the queue entirely, so it
        # must keep working even while the drain loop is paused for reauth.
        sys_turn_id = wq.post_system_message("I'm paused waiting for reauth.")
        turn = wq.get_turn(sys_turn_id)
        assert turn.status == TurnStatus.DONE
        assert turn.result.result_text == "I'm paused waiting for reauth."

        await wq.stop()

    asyncio.run(_body())


def test_corrupt_credentials_do_not_kill_the_drain_loop(monkeypatch):
    # auth_monitor.get_status() guards JSONDecodeError/KeyError/OSError but not
    # every shape a corrupt .credentials.json takes (a string expiresAt reaches
    # `value / 1000` -> TypeError). The auth gate runs outside the drain loop's
    # try/except, so an uncaught raise there kills the consumer task and every
    # turn sits PENDING forever while /health still reports queue_paused: false.
    # A silent permanent wedge is strictly worse than a failed turn.
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise TypeError("unsupported operand type(s) for /: 'str' and 'int'")

    monkeypatch.setattr("server.queue.auth_monitor.get_status", _boom)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")

        # Fails open: the turn is invoked rather than held, so a real auth
        # problem surfaces as a visible failed turn instead of silence.
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE
        assert wq.paused is False

        # The loop survived — a second turn still drains.
        second = await _run_to_completion(wq, wq.enqueue("again", source="chat"))
        assert second.status == TurnStatus.DONE
        assert calls["n"] >= 2
        await wq.stop()

    asyncio.run(_body())


def test_queue_depth_counts_the_turn_held_for_reauth(monkeypatch):
    # The held turn is already popped off the asyncio.Queue, so qsize() alone
    # reports queue_depth: 0 alongside queue_paused: true — an external monitor
    # would see an empty queue while work is outstanding.
    monkeypatch.setattr(
        "server.queue.auth_monitor.get_status",
        lambda: _fake_auth_status(True),
    )
    monkeypatch.setattr(
        "server.queue.settings", dataclasses.replace(real_settings, auth_recheck_interval_seconds=0.02)
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        wq.enqueue("held", source="chat")
        await asyncio.sleep(0.1)
        assert wq.paused is True
        assert wq.depth == 1

        wq.enqueue("behind it", source="chat")
        assert wq.depth == 2
        await wq.stop()

    asyncio.run(_body())


def test_queue_serializes_invocations_one_at_a_time():
    async def _body():
        wq = WorkQueue()
        wq.start()
        ids = [wq.enqueue(f"turn {i}", source="chat") for i in range(5)]
        turns = [await _run_to_completion(wq, turn_id) for turn_id in ids]
        assert [turn.status for turn in turns] == [
            TurnStatus.MERGED,
            TurnStatus.MERGED,
            TurnStatus.MERGED,
            TurnStatus.MERGED,
            TurnStatus.DONE,
        ]
        await wq.stop()

    asyncio.run(_body())


def test_a_replaced_session_carries_the_conversation_with_it(monkeypatch):
    # If the conversation's session has died, invoke.py retries fresh and the
    # head turn answers under a new id. The turn queued behind it must resume
    # THAT, not the dead one -- it looks the conversation up when it runs, so
    # it follows without any repointing (docs/tasks/concurrent-send-session-
    # chaining.md §5 is the history).
    calls = []

    async def fake_run_claude(prompt, **kwargs):
        calls.append(kwargs.get("session_id"))
        return InvocationResult(
            exit_code=0, is_error=False, subtype="success", result_text="ok",
            session_id="live-sess", permission_denials=[],
        )

    monkeypatch.setattr("server.queue.run_claude", fake_run_claude)

    async def _body():
        wq = WorkQueue()
        wq.start()
        session_state.note_conversation("dead-sess")
        head = wq.enqueue("first", source="chat", in_conversation=True)
        behind = wq.enqueue("second", source="cron", in_conversation=True)
        await _run_to_completion(wq, head)
        await _run_to_completion(wq, behind)
        await wq.stop()

    asyncio.run(_body())
    assert calls == ["dead-sess", "live-sess"]


def test_require_session_turn_errors_instead_of_running_blind_on_dead_session(tmp_path, monkeypatch):
    # docs/tasks/nightly-consolidation.md piece 2: an attach job's whole safety
    # argument is that it never runs in a fresh, contextless session. If the
    # attended session it was told to resume has died by the time the drain
    # loop actually invokes it, the turn must come back ERROR, not silently
    # succeed in a fresh session invoke.py would otherwise self-heal into. The
    # fake claude below would answer successfully if that fresh retry ran, so
    # ERROR here (plus exactly one invocation) proves it didn't.
    import textwrap

    fake_bin = tmp_path / "claude"
    calls_path = tmp_path / "calls"
    fake_bin.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, sys
            with open("{calls_path}", "a") as f:
                f.write("x")
            if any(a.startswith("--resume") for a in sys.argv):
                sys.stderr.write("No conversation found with session ID: dead-sess\\n")
                sys.exit(1)
            print(json.dumps({{
                "is_error": False,
                "subtype": "success",
                "result": "fresh answer",
                "session_id": "fresh-sess",
                "permission_denials": [],
            }}))
            """
        )
    )
    fake_bin.chmod(0o755)
    monkeypatch.setattr("server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(fake_bin)))

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue(
            "review today", session_id="dead-sess", resume=True, source="cron", require_session=True
        )
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.ERROR
        assert turn.result.session_id == "dead-sess", "no fresh session_id — the retry never ran"
        assert calls_path.read_text() == "x", "exactly one invocation: no blind retry"
        await wq.stop()

    asyncio.run(_body())


def test_successful_retiring_turn_retires_the_session_it_ran_in():
    # docs/tasks/nightly-consolidation.md piece 1: the consolidate job's
    # successful completion is the rollover trigger. This test doesn't care
    # what session_id was requested (conftest's fake CLI always answers as
    # fake-session-123 regardless) -- only that the id it actually landed in
    # gets retired.
    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue(
            "review today", session_id="attended-1", resume=True, source="cron",
            require_session=True, retire_session_on_success=True,
        )
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE
        assert wq.is_session_retired(turn.result.session_id) is True
        await wq.stop()

    asyncio.run(_body())


def test_retirement_also_covers_the_hint_when_the_turn_lands_elsewhere():
    """The id consolidation asked for and the id the call landed in are not
    guaranteed to match -- a resumed turn can land in a new session.

    Retiring only the landed id would leave the conversation pointing at the
    other one, so rollover would silently never fire -- while the retirement
    log line still read like success. Exercised for real here: conftest's fake
    CLI answers as `fake-session-123` no matter what id was requested, so the
    requested id and the landed id genuinely differ.
    """
    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue(
            "review today", session_id="attended-1", resume=True, source="cron",
            require_session=True, retire_session_on_success=True,
        )
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE
        assert turn.result.session_id != "attended-1", "fixture no longer diverges"
        assert wq.is_session_retired("attended-1") is True
        await wq.stop()

    asyncio.run(_body())


def test_errored_retiring_turn_does_not_retire_anything(tmp_path, monkeypatch):
    # Fail-safe (docs/tasks/nightly-consolidation.md, "Design settled
    # 2026-08-16"): never roll away a day that was not consolidated. Reuses
    # test_require_session_turn_errors_instead_of_running_blind_on_dead_session's
    # fixture -- require_session=True means this errors instead of self-healing
    # into a fresh session, and that ERROR must not be mistaken for success.
    import textwrap

    fake_bin = tmp_path / "claude"
    fake_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            sys.stderr.write("No conversation found with session ID: dead-sess\\n")
            sys.exit(1)
            """
        )
    )
    fake_bin.chmod(0o755)
    monkeypatch.setattr("server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(fake_bin)))

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue(
            "review today", session_id="dead-sess", resume=True, source="cron",
            require_session=True, retire_session_on_success=True,
        )
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.ERROR
        assert wq.is_session_retired("dead-sess") is False
        await wq.stop()

    asyncio.run(_body())


def test_non_retiring_cron_job_never_retires_its_session():
    # A cron job outside ATTACHED_SESSION_JOBS (retire_session_on_success
    # defaults to False) must not retire anything even though it succeeds --
    # only the specific job app.py opts in gets to close a day out.
    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("do the scan", session_id=None, resume=False, source="cron")
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE
        assert wq.is_session_retired(turn.result.session_id) is False
        await wq.stop()

    asyncio.run(_body())


def test_retirement_ends_the_conversation_and_the_next_turn_starts_the_new_one(monkeypatch):
    """The rollover half. Whatever runs after a successful consolidation --
    the morning brief, usually -- starts fresh and becomes the conversation,
    and the phone's next message resumes THAT, not the retired session."""
    ids = iter(["day-2", "unused"])
    calls = _recording_run_claude(monkeypatch, lambda sid: sid or next(ids))

    async def _body():
        wq = WorkQueue()
        wq.start()
        session_state.note_conversation("day-1")
        wq._retire_session("day-1")
        await _run_to_completion(wq, wq.enqueue("brief", source="cron", in_conversation=True))
        await _run_to_completion(wq, wq.enqueue("morning!", source="chat", in_conversation=True))
        assert wq.conversation_session_id() == "day-2"
        await wq.stop()

    asyncio.run(_body())
    assert calls == [(None, False), ("day-2", True)]


def test_a_turn_queued_behind_consolidation_follows_the_rollover(monkeypatch):
    """Resolved at run time, not at enqueue: a message sent while the 01:00
    consolidation is still running must land in the new session, not resume
    the one consolidation is about to retire."""
    calls = _recording_run_claude(monkeypatch)

    async def _body():
        wq = WorkQueue()
        wq.start()
        session_state.note_conversation("day-1")
        consolidate = wq.enqueue(
            "review", source="cron", session_id="day-1", resume=True,
            require_session=True, retire_session_on_success=True,
        )
        late = wq.enqueue("still up", source="chat", in_conversation=True)
        await _run_to_completion(wq, consolidate)
        await _run_to_completion(wq, late)
        await wq.stop()

    asyncio.run(_body())
    assert calls == [("day-1", True), (None, False)]

def test_rapid_first_sends_land_in_one_session(monkeypatch):
    """Two messages sent before any session exists must not fork into two
    conversations. The phone used to prevent this by holding the send button
    shut until the first turn named its session (concurrent-send-session-
    chaining.md), and the server repointed queued turns when a session was
    replaced. Resolving the conversation at run time makes both unnecessary:
    the second turn cannot pick a session until the first has recorded one."""
    async def _body():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def fake_run_claude(prompt, **kwargs):
            calls.append((kwargs.get("session_id"), kwargs.get("resume")))
            started.set()
            await release.wait()
            return InvocationResult(
                exit_code=0, is_error=False, subtype="success", result_text="ok",
                session_id="first-session", permission_denials=[],
            )

        monkeypatch.setattr("server.queue.run_claude", fake_run_claude)
        wq = WorkQueue()
        wq.start()
        head = wq.enqueue("first", source="chat", in_conversation=True)
        await started.wait()
        behind = wq.enqueue("second", source="chat", in_conversation=True)
        release.set()
        await _run_to_completion(wq, head)
        await _run_to_completion(wq, behind)
        await wq.stop()
        assert calls == [(None, False), ("first-session", True)]

    asyncio.run(_body())


def test_enqueue_requires_an_explicit_source():
    """`source` used to default to "chat", i.e. to ATTENDED.

    That quietly inverted the fail-closed property of server/active_turn.py for
    the likeliest mistake there is -- omitting the argument. A new unattended
    trigger calling `enqueue(text)` would have run as an attended turn and been
    able to raise a permission prompt on Mike's phone at 3am, with the whole
    suite green (review, 2026-08-07).

    Asserted on the signature rather than by calling it, because the failure
    being guarded against is a *default* reappearing, which no behavioural test
    of the current callers would notice.
    """
    parameter = inspect.signature(WorkQueue.enqueue).parameters["source"]
    assert parameter.default is inspect.Parameter.empty, (
        "enqueue() grew a default for `source`. Every caller must name it: "
        "anything not in active_turn.ATTENDED_SOURCES is treated as having "
        "nobody to ask, and a default silently picks a side for whoever forgets."
    )


# --- harness-owned identity git (server/identity_git.py) ---------------------


def test_identity_commit_runs_after_every_turn(monkeypatch):
    """The wiring is the load-bearing part: a correct commit function that the
    drain loop never calls buys nothing."""
    calls = []
    monkeypatch.setattr(
        "server.queue.commit_after_turn", lambda turn_id, source: calls.append((turn_id, source))
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        await _run_to_completion(wq, turn_id)
        deadline = asyncio.get_running_loop().time() + 1
        while not calls and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        await wq.stop()
        return turn_id

    turn_id = asyncio.run(_body())
    assert calls == [(turn_id, "chat")]


def test_the_turn_ready_push_is_not_held_behind_the_identity_commit(monkeypatch):
    """Ordering, and it matters: commit_after_turn is up to five git subprocesses
    at 30s each. In front of the broadcast, a stalled git would hold back the
    phone notification for a reply already sitting complete in _turns
    (/code-review 2026-08-08). Durability is the lower-priority half of the pair."""
    order = []
    monkeypatch.setattr(
        "server.queue.push_broadcast_nowait", lambda payload: order.append("push")
    )
    monkeypatch.setattr(
        "server.queue.commit_after_turn", lambda turn_id, source: order.append("commit")
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        await _run_to_completion(wq, turn_id)
        deadline = asyncio.get_running_loop().time() + 1
        while len(order) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        await wq.stop()

    asyncio.run(_body())
    assert order == ["push", "commit"]


def test_identity_commit_runs_even_when_the_turn_errored(monkeypatch):
    """An ERRORed turn can still have written files before it failed, and those
    writes are exactly what wants recovering."""
    calls = []
    monkeypatch.setattr(
        "server.queue.commit_after_turn", lambda turn_id, source: calls.append(turn_id)
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("invocation exploded")

    monkeypatch.setattr("server.queue.run_claude", _boom)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.ERROR
        deadline = asyncio.get_running_loop().time() + 1
        while not calls and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        await wq.stop()
        return turn_id

    turn_id = asyncio.run(_body())
    assert calls == [turn_id]


# --- queued-turn coalescing -------------------------------------------------


def _success(session_id: str = "session-1", text: str = "ok") -> InvocationResult:
    return InvocationResult(
        exit_code=0, is_error=False, subtype="success", result_text=text,
        session_id=session_id, permission_denials=[],
    )


def test_every_turn_field_is_classified_before_coalescing_can_use_it():
    assert frozenset(Turn.__dataclass_fields__) == _COALESCING_CLASSIFIED_TURN_FIELDS, (
        "A Turn field was added without deciding its coalescing contract. "
        "Classify it before allowing queued turns to merge again."
    )


def test_queued_ios_turns_coalesce_onto_the_last_turn(monkeypatch):
    calls = []
    monkeypatch.setattr("server.queue.MAX_RETAINED_TURNS", 1)

    async def _body():
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(prompt, **kwargs):
            calls.append((prompt, kwargs["turn_id"]))
            if len(calls) == 1:
                started.set()
                await release.wait()
            return _success()

        monkeypatch.setattr("server.queue.run_claude", fake_run)
        wq = WorkQueue()
        wq.start()
        blocker = wq.enqueue("already running", session_id="session-1", resume=True, source="chat")
        await started.wait()
        queued = [
            wq.enqueue(text, session_id="session-1", resume=True, source="chat")
            for text in ("two", "three", "four")
        ]
        wq.get_turn(queued[0]).activities.append(
            ActivityEvent(kind="tool_start", tool="Read", at=time.time())
        )
        release.set()
        await _run_to_completion(wq, queued[-1])

        assert calls[1][0] == "two\n\nthree\n\nfour"
        assert calls[1][1] == queued[-1]
        assert [wq.get_turn(t).status for t in queued] == [
            TurnStatus.MERGED, TurnStatus.MERGED, TurnStatus.DONE,
        ]
        for absorbed_id in queued[:-1]:
            absorbed = wq.get_turn(absorbed_id)
            assert absorbed is not None, (
                "the eviction grace must retain a just-merged turn for its polling client"
            )
            assert absorbed.merged_into == queued[-1]
            assert absorbed.finished_at is not None
            assert absorbed.activities == []
            assert absorbed.result is None
        # One reply row per run that actually ran; the merged turns get none.
        # The row is written after the status goes terminal (a transcript write
        # in a thread), so wait for the pass, not the status (flaky under load,
        # 2026-10-01; see `_run_to_completion`).
        def replies():
            return [r["turn_id"] for r in transcript.read_after(0)["rows"] if r["role"] == "assistant"]

        await _wait_for(lambda: len(replies()) >= 2, "the merged run's reply row")
        assert replies() == [blocker, queued[-1]]
        await wq.stop()

    asyncio.run(_body())
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("middle_source", "middle_session"),
    [("cron", "session-1"), ("chat", "other-session")],
)
def test_ineligible_turn_stops_the_run_and_preserves_fifo(
    monkeypatch, middle_source, middle_session
):
    calls = []

    async def fake_run(prompt, **kwargs):
        calls.append(prompt)
        return _success(kwargs.get("session_id") or "fresh")

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        ids = [
            wq.enqueue("first", session_id="session-1", resume=True, source="chat"),
            wq.enqueue("boundary", session_id=middle_session, resume=True, source=middle_source),
            wq.enqueue("last", session_id="session-1", resume=True, source="chat"),
        ]
        for turn_id in ids:
            await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())
    assert calls == ["first", "boundary", "last"]


def test_effort_change_stops_coalescing(monkeypatch):
    calls = []

    async def fake_run(prompt, **kwargs):
        calls.append((prompt, kwargs["effort"]))
        return _success(kwargs.get("session_id") or "fresh")

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        ids = [
            wq.enqueue(
                "first", session_id="session-1", resume=True, source="chat", effort="low"
            ),
            wq.enqueue(
                "boundary", session_id="session-1", resume=True, source="chat", effort="high"
            ),
            wq.enqueue(
                "last", session_id="session-1", resume=True, source="chat", effort="low"
            ),
        ]
        for turn_id in ids:
            await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())
    assert calls == [("first", "low"), ("boundary", "high"), ("last", "low")]


def test_coalesced_run_reports_the_head_turns_wait(monkeypatch, caplog):
    """The survivor is the newest turn in the run and waited the least. Wait
    time is the metric coalescing exists to improve, so measuring it on the
    survivor would flatter coalescing on its own scoreboard (review,
    2026-08-23)."""
    calls = []

    async def fake_run(prompt, **kwargs):
        calls.append(kwargs["queue_wait_seconds"])
        return _success()

    monkeypatch.setattr("server.queue.run_claude", fake_run)
    monkeypatch.setattr("server.queue.time.time", lambda: 123.456)

    async def _body():
        wq = WorkQueue()
        wq.start()
        for turn_id, created_at in (("head", 120.0), ("survivor", 123.0)):
            turn = Turn(
                turn_id=turn_id, prompt=turn_id, session_id="session-1", resume=True,
                source="chat", client_msg_id=None, created_at=created_at,
            )
            wq._turns[turn_id] = turn
            wq._queue.put_nowait(turn_id)
        await _run_to_completion(wq, "survivor")
        assert wq.get_turn("head").status == TurnStatus.MERGED
        await wq.stop()

    with caplog.at_level(logging.INFO, logger="server.queue"):
        asyncio.run(_body())
    assert calls == [pytest.approx(3.456)], (
        "the run waited from the head turn's creation, not the survivor's"
    )
    # The only trace a merge leaves; without it one reply to three messages
    # reads in the journal exactly like two turns lost.
    assert "coalesced 2 turns into survivor: head" in caplog.text


def test_coalesced_run_is_capped_at_five(monkeypatch):
    calls = []

    async def fake_run(prompt, **kwargs):
        calls.append(prompt)
        return _success()

    monkeypatch.setattr("server.queue.run_claude", fake_run)

    async def _body():
        wq = WorkQueue()
        wq.start()
        ids = [
            wq.enqueue(str(i), session_id="session-1", resume=True, source="chat")
            for i in range(8)
        ]
        for turn_id in ids:
            await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())
    assert calls == ["0\n\n1\n\n2\n\n3\n\n4", "5\n\n6\n\n7"]


def test_attachment_and_compaction_turns_are_never_absorbed(monkeypatch):
    calls = []
    cleaned = []

    async def fake_run(prompt, **kwargs):
        calls.append((prompt, kwargs["attachment_paths"], kwargs["source"]))
        return _success()

    monkeypatch.setattr("server.queue.run_claude", fake_run)
    monkeypatch.setattr("server.queue.cleanup_attachment", lambda turn_id: cleaned.append(turn_id))

    async def _body():
        wq = WorkQueue()
        wq.start()
        chat_turn = wq.enqueue("chat", session_id="session-1", resume=True, source="chat")
        image = wq.enqueue(
            "image", session_id="session-1", resume=True, source="chat",
            attachment_paths=("/tmp/image.jpg",),
        )
        compaction = wq.enqueue(
            "compact", session_id="session-1", resume=True,
            source="compaction", raw_prompt=True,
        )
        for turn_id in (chat_turn, image, compaction):
            await _run_to_completion(wq, turn_id)
        assert not wq.get_turn(compaction).cancelled
        await wq.stop()
        return image

    image = asyncio.run(_body())
    assert calls == [
        ("chat", (), "chat"),
        ("image", ("/tmp/image.jpg",), "chat"),
        ("compact", (), "compaction"),
    ]
    assert cleaned == [image]


def test_carry_slot_counts_toward_depth_and_drains_next(monkeypatch):
    calls = []

    async def _body():
        second_started = asyncio.Event()
        release_second = asyncio.Event()

        async def fake_run(prompt, **kwargs):
            calls.append(prompt)
            if prompt == "chat":
                second_started.set()
                await release_second.wait()
            return _success()

        monkeypatch.setattr("server.queue.run_claude", fake_run)
        wq = WorkQueue()
        wq.start()
        chat_turn = wq.enqueue("chat", session_id="session-1", resume=True, source="chat")
        cron = wq.enqueue("cron", source="cron")
        await second_started.wait()
        assert wq.depth == 1
        release_second.set()
        await _run_to_completion(wq, chat_turn)
        await _run_to_completion(wq, cron)
        await asyncio.wait_for(wq._queue.join(), timeout=1.0)
        await wq.stop()

    asyncio.run(_body())
    assert calls == ["chat", "cron"]


# --- image attachments (docs/tasks/ios-image-input.md) ----------------------


def test_attachment_path_is_threaded_to_run_claude(monkeypatch):
    calls = []

    async def _fake_run_claude(prompt, **kwargs):
        calls.append(kwargs.get("attachment_paths"))
        return InvocationResult(
            exit_code=0, is_error=False, subtype="success", result_text="ok",
            session_id="s1", permission_denials=[],
        )

    monkeypatch.setattr("server.queue.run_claude", _fake_run_claude)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue(
            "look at this", source="chat", turn_id="t-img",
            attachment_paths=("/home/marlow/.marlow/attachments/t-img/0.jpg",),
        )
        await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())
    assert calls == [("/home/marlow/.marlow/attachments/t-img/0.jpg",)]


def test_several_attachment_paths_are_threaded_in_order_and_never_coalesced(monkeypatch):
    calls = []

    async def fake_run(prompt, **kwargs):
        calls.append((prompt, kwargs["attachment_paths"]))
        return _success()

    monkeypatch.setattr("server.queue.run_claude", fake_run)
    monkeypatch.setattr("server.queue.cleanup_attachment", lambda turn_id: None)
    paths = ("/a/t/0.jpg", "/a/t/1.jpg", "/a/t/2.jpg")

    async def _body():
        wq = WorkQueue()
        wq.start()
        first = wq.enqueue("chat", session_id="s", resume=True, source="chat")
        second = wq.enqueue("pics", session_id="s", resume=True, source="chat", attachment_paths=paths)
        third = wq.enqueue("after", session_id="s", resume=True, source="chat")
        for t in (first, second, third):
            await _run_to_completion(wq, t)
        await wq.stop()

    asyncio.run(_body())
    assert ("pics", paths) in calls


def test_no_attachment_paths_passes_an_empty_tuple_to_run_claude(monkeypatch):
    calls = []

    async def _fake_run_claude(prompt, **kwargs):
        calls.append(kwargs.get("attachment_paths"))
        return InvocationResult(
            exit_code=0, is_error=False, subtype="success", result_text="ok",
            session_id="s1", permission_denials=[],
        )

    monkeypatch.setattr("server.queue.run_claude", _fake_run_claude)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())
    assert calls == [()]


def test_effort_is_threaded_to_run_claude(monkeypatch):
    calls = []

    async def _fake_run_claude(prompt, **kwargs):
        calls.append(kwargs.get("effort"))
        return InvocationResult(
            exit_code=0, is_error=False, subtype="success", result_text="ok",
            session_id="s1", permission_denials=[],
        )

    monkeypatch.setattr("server.queue.run_claude", _fake_run_claude)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat", effort="high")
        await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())
    assert calls == ["high"]


def test_attachment_cleaned_up_after_a_done_turn():
    path = write_attachments("t-clean", [b"x"])

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat", turn_id="t-clean", attachment_paths=path)
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE
        # Cleanup runs after the status goes terminal, past a transcript write
        # in a thread; stopping the queue the moment the status lands could
        # cancel the drain before it got there (flaky under load, 2026-10-01).
        # Wait for the pass itself, as `_run_to_completion`'s docstring says.
        await _wait_for(
            lambda: not os.path.exists(turn_attachment_dir("t-clean")), "attachment cleanup"
        )
        await wq.stop()

    asyncio.run(_body())
    assert not os.path.exists(turn_attachment_dir("t-clean"))


def test_attachment_cleaned_up_after_an_errored_turn(monkeypatch):
    # Acceptance #4 (docs/tasks/ios-image-input.md): an ERRORed turn leaves the
    # same bytes on disk as a DONE one, and both must not linger.
    path = write_attachments("t-err", [b"x"])

    async def _boom(*args, **kwargs):
        raise RuntimeError("invocation exploded")

    monkeypatch.setattr("server.queue.run_claude", _boom)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat", turn_id="t-err", attachment_paths=path)
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.ERROR
        # Cleanup runs after the status goes terminal, past a transcript write
        # in a thread; stopping the queue the moment the status lands could
        # cancel the drain before it got there (flaky under load, 2026-10-01).
        # Wait for the pass itself, as `_run_to_completion`'s docstring says.
        await _wait_for(
            lambda: not os.path.exists(turn_attachment_dir("t-err")), "attachment cleanup"
        )
        await wq.stop()

    asyncio.run(_body())
    assert not os.path.exists(turn_attachment_dir("t-err"))


def test_a_turn_without_an_attachment_does_not_touch_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr("server.queue.cleanup_attachment", lambda turn_id: calls.append(turn_id))

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())
    assert calls == []


def test_start_sweeps_orphaned_attachments(monkeypatch):
    calls = []
    monkeypatch.setattr("server.queue.sweep_orphaned_attachments", lambda: calls.append(1))

    async def _body():
        wq = WorkQueue()
        wq.start()
        # Dispatched via asyncio.to_thread (`/code-review medium` finding: it
        # used to block start() itself) — await it directly rather than race
        # the thread pool via an arbitrary yield.
        assert wq._sweep_task is not None
        await wq._sweep_task
        await wq.stop()

    asyncio.run(_body())
    assert calls == [1]


def test_a_failing_identity_commit_does_not_break_the_turn_or_the_drain_loop(monkeypatch):
    """commit_after_turn is documented as never raising. If that contract is ever
    broken, the drain loop must not be what dies with it — the reply is already
    the user's by this point, and a dead loop would silently stop every later turn."""
    def _boom(turn_id, source):
        raise RuntimeError("git exploded")

    monkeypatch.setattr("server.queue.commit_after_turn", _boom)

    async def _body():
        wq = WorkQueue()
        wq.start()
        first = wq.enqueue("hello", source="chat")
        await _run_to_completion(wq, first)
        second = wq.enqueue("still alive?", source="chat")
        turn = await _run_to_completion(wq, second)
        await wq.stop()
        return turn

    turn = asyncio.run(_body())
    assert turn.status == TurnStatus.DONE


# --- mid-turn activity (docs/tasks/turn-activity-streaming.md phase 1.3) -------


def _activity_cli(tmp_path, gap: str = "0.4"):
    """A fake CLI that reports a tool call, then stalls before finishing.

    The stall is the point: it is the window in which `turn.activity` is
    supposed to be readable by a polling client, and a test that only looked at
    the finished turn could not tell this feature from a no-op.
    """
    path = tmp_path / "claude"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        'if "stream-json" in sys.argv[1:]:\n'
        '    print(json.dumps({"type": "assistant", "message": {"content": ['
        '{"type": "tool_use", "name": "Read"}]}}), flush=True)\n'
        f"    time.sleep({gap})\n"
        'print(json.dumps({"type": "result", "is_error": False, "subtype": "success",'
        ' "result": "done", "session_id": "s1", "permission_denials": []}), flush=True)\n'
    )
    path.chmod(0o755)
    return path


async def _wait_for_activity(wq, turn_id, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        activities = wq.get_turn(turn_id).activities
        if activities:
            return activities[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"turn {turn_id} never reported activity")


def test_an_attended_turn_reports_activity_while_it_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "server.invoke.settings",
        dataclasses.replace(real_settings, claude_bin=str(_activity_cli(tmp_path))),
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")

        activity = await _wait_for_activity(wq, turn_id)
        assert activity.kind == "tool_start"
        assert activity.tool == "Read"
        # Still running — i.e. this was visible to a poll DURING the turn,
        # which is the entire feature.
        assert wq.get_turn(turn_id).status == TurnStatus.RUNNING

        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE
        await wq.stop()

    asyncio.run(_body())


def test_activity_is_cleared_the_moment_a_turn_finishes(tmp_path, monkeypatch):
    """A DONE turn is not still reading a file. A label that outlived its turn
    would be worse than the silence this replaces — it would be wrong, and it
    would never correct itself."""
    monkeypatch.setattr(
        "server.invoke.settings",
        dataclasses.replace(real_settings, claude_bin=str(_activity_cli(tmp_path, gap="0.1"))),
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        await _wait_for_activity(wq, turn_id)
        turn = await _run_to_completion(wq, turn_id)

        assert turn.status == TurnStatus.DONE
        assert turn.activities == []
        await wq.stop()

    asyncio.run(_body())


def test_activity_is_cleared_on_an_errored_turn_too(monkeypatch):
    """The ERROR paths are exactly where a stale "Using a tool…" would sit
    under an error message forever — hence the clear in the `finally`, not in
    the success branch."""
    from server.invoke import ActivityEvent, InvokeError

    async def _fake_run_claude(prompt, **kwargs):
        kwargs["on_activity"](ActivityEvent(kind="tool_start", tool="Read", at=time.time()))
        raise InvokeError("boom")

    monkeypatch.setattr("server.queue.run_claude", _fake_run_claude)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")
        turn = await _run_to_completion(wq, turn_id)

        assert turn.status == TurnStatus.ERROR
        assert turn.activities == []
        await wq.stop()

    asyncio.run(_body())


def test_a_cron_turn_never_reports_activity(tmp_path, monkeypatch):
    """Not a queue rule — invoke.py refuses to stream for an unattended
    source, so this asserts the two halves are actually connected."""
    monkeypatch.setattr(
        "server.invoke.settings",
        dataclasses.replace(real_settings, claude_bin=str(_activity_cli(tmp_path, gap="0.1"))),
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("briefing", source="cron")
        turn = await _run_to_completion(wq, turn_id)

        assert turn.status == TurnStatus.DONE
        assert turn.activities == []
        await wq.stop()

    asyncio.run(_body())


def test_a_fast_tool_call_survives_until_the_client_can_poll(tmp_path, monkeypatch):
    """The bug this list exists to fix, 2026-08-22.

    A local file read takes ~0.16s and the client polls every 2s, so a
    latest-wins slot showed the Read about 8% of the time — the events were
    emitted, then overwritten before anything could observe them. Here the
    fake fires all three back to back and *then* stalls, which is the shape
    that used to leave only the last one behind.
    """
    path = tmp_path / "claude"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        'if "stream-json" in sys.argv[1:]:\n'
        '    for line in [\n'
        '        {"type": "assistant", "message": {"content": ['
        '{"type": "tool_use", "name": "Read"}]}},\n'
        '        {"type": "user", "message": {"content": ['
        '{"type": "tool_result", "tool_use_id": "t1"}]}},\n'
        '        {"type": "assistant", "message": {"content": ['
        '{"type": "text", "text": "here you go"}]}},\n'
        "    ]:\n"
        "        print(json.dumps(line), flush=True)\n"
        "    time.sleep(0.5)\n"
        'print(json.dumps({"type": "result", "is_error": False, "subtype": "success",'
        ' "result": "done", "session_id": "s1", "permission_denials": []}), flush=True)\n'
    )
    path.chmod(0o755)
    monkeypatch.setattr(
        "server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(path))
    )

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue("hello", source="chat")

        # One observation, taken well after the 0.16s-equivalent tool leg is
        # over — exactly the position a 2s poll is in.
        await _wait_for_activity(wq, turn_id)
        await asyncio.sleep(0.3)
        seen = list(wq.get_turn(turn_id).activities)

        assert [e.kind for e in seen] == ["tool_start", "tool_end", "text"]
        assert seen[0].tool == "Read", "the Read is the whole point and must survive"

        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE
        await wq.stop()

    asyncio.run(_body())


def test_activities_are_capped_dropping_the_oldest(monkeypatch):
    """A tool-heavy turn must not grow this without bound. Oldest go first:
    what survives is the recent activity, which is the part still worth
    showing."""
    from server.invoke import ActivityEvent
    from server.queue import MAX_TURN_ACTIVITIES, _record_activity

    turn = Turn(
        turn_id="t-cap", prompt="x", session_id=None, resume=False,
        source="chat", client_msg_id=None,
    )
    record = _record_activity(turn)
    for i in range(MAX_TURN_ACTIVITIES + 10):
        record(ActivityEvent(kind="tool_start", tool=f"Tool{i}", at=float(i)))

    assert len(turn.activities) == MAX_TURN_ACTIVITIES
    assert turn.activities[-1].tool == f"Tool{MAX_TURN_ACTIVITIES + 9}"
    assert turn.activities[0].tool == "Tool10"


# --- Onboarding after a turn (docs/tasks/onboarding-v2.md) ------------------

def _onboarding_turn(source="chat", status=None, calls=()):
    from types import SimpleNamespace

    from server.invoke import ONBOARDING_TOOL, ToolCall
    from server.queue import TurnStatus

    return SimpleNamespace(
        turn_id="t1",
        source=source,
        status=status or TurnStatus.DONE,
        tool_calls=[ToolCall(name=n, target=t, at=0.0) for n, t in calls]
        or [ToolCall(name=ONBOARDING_TOOL, target="name done", at=0.0)],
    )


def _onboarding_paths(tmp_path, monkeypatch):
    import dataclasses

    from server.config import settings as real

    user = tmp_path / "USER.md"
    user.write_text("## Everything else\nNot yet known.\n")
    monkeypatch.setattr(
        "server.queue.settings", dataclasses.replace(real, data_dir=tmp_path, user_path=user)
    )
    return tmp_path / "onboarding.json", user


def test_a_chat_turn_moves_onboarding_on(tmp_path, monkeypatch):
    from server import onboarding
    from server.queue import _note_onboarding

    record, user = _onboarding_paths(tmp_path, monkeypatch)

    _note_onboarding(_onboarding_turn(), None)

    assert onboarding.load(record, user).step == "stay-on-top"


def test_the_commit_paths_reach_the_check(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from server import onboarding
    from server.invoke import ONBOARDING_TOOL
    from server.queue import _note_onboarding

    record, user = _onboarding_paths(tmp_path, monkeypatch)
    for step in ("name", "stay-on-top"):
        _note_onboarding(_onboarding_turn(calls=[(ONBOARDING_TOOL, f"{step} done")]), None)
    claim = _onboarding_turn(calls=[(ONBOARDING_TOOL, "one-thing done")])

    _note_onboarding(claim, SimpleNamespace(sha="x", paths=()))
    assert onboarding.load(record, user).rejected == "one-thing"

    _note_onboarding(claim, SimpleNamespace(sha="y", paths=("cron/schedules.json",)))
    assert onboarding.load(record, user).step == "tour"


@pytest.mark.parametrize("source", ["cron", "system", "compaction"])
def test_unattended_turns_never_count(tmp_path, monkeypatch, source):
    from server import onboarding
    from server.queue import _note_onboarding

    record, user = _onboarding_paths(tmp_path, monkeypatch)

    _note_onboarding(_onboarding_turn(source=source), None)

    state = onboarding.load(record, user)
    assert state.step == "name" and state.turns == 0


def test_a_failed_turn_never_counts(tmp_path, monkeypatch):
    """A turn that errored (the CLI was down, say) didn't spend the user's
    patience on the step, so it must not use up the cap."""
    from server import onboarding
    from server.queue import TurnStatus, _note_onboarding

    record, user = _onboarding_paths(tmp_path, monkeypatch)

    _note_onboarding(_onboarding_turn(status=TurnStatus.ERROR), None)

    assert onboarding.load(record, user).turns == 0


def test_other_tools_are_not_claims(tmp_path, monkeypatch):
    from server import onboarding
    from server.queue import _note_onboarding

    record, user = _onboarding_paths(tmp_path, monkeypatch)

    _note_onboarding(_onboarding_turn(calls=[("Read", "name done"), ("mcp__onboarding__step_done", None)]), None)

    state = onboarding.load(record, user)
    assert state.step == "name" and state.turns == 1

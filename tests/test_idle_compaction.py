"""docs/tasks/idle-compaction.md.

Two layers get tested separately, matching the module's own split:

- The gates (`IdleCompactionWatcher._eligible` / `.maybe_enqueue`) are pure
  enough to test with a bare `WorkQueue()` that is never `.start()`ed, except
  where enqueue itself is under test.
- The queue-side wiring (cancellation on an attended enqueue, delivery
  exclusion, the `_drain` hooks) needs a real, running `WorkQueue` against
  conftest's fake `claude` binary, same convention as tests/test_queue.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import textwrap

import pytest

from server import idle_compaction as ic
from server import transcript
from server.config import settings as real_settings
from server.idle_compaction import SOURCE, IdleCompactionWatcher
from server.invoke import run_claude
from server.queue import TurnStatus, WorkQueue
from server.session_state import session_state


def _settings(**overrides):
    base = dict(
        idle_compaction_enabled=True,
        idle_compaction_idle_seconds=290.0,
        idle_compaction_threshold_tokens=110_000,
        idle_compaction_floor_tokens=50_000,
        idle_compaction_growth_tokens=27_500,
        idle_compaction_poll_interval_seconds=30.0,
    )
    base.update(overrides)
    return dataclasses.replace(real_settings, **base)


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    monkeypatch.setattr(ic, "settings", _settings())


def _ready_watcher(*, session_id: str = "s1", prefix: int = 120_000, idle_for: float = 400.0, now: float = 10_000.0):
    """A watcher primed with one completed attended turn old enough and large
    enough to be eligible absent any other gate."""
    w = IdleCompactionWatcher()
    w.note_attended_turn(session_id, {"cache_read_input_tokens": prefix}, now=now - idle_for)
    return w


async def _run_to_completion(wq: WorkQueue, turn_id: str, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        turn = wq.get_turn(turn_id)
        if turn.status in (TurnStatus.DONE, TurnStatus.ERROR, TurnStatus.CANCELLED):
            return turn
        await asyncio.sleep(0.02)
    raise AssertionError(f"turn {turn_id} did not finish within {timeout}s")


# --- the gates, independently ------------------------------------------------


def test_gate_disabled_blocks(monkeypatch):
    monkeypatch.setattr(ic, "settings", _settings(idle_compaction_enabled=False))
    w = _ready_watcher()
    assert w.maybe_enqueue(WorkQueue(), now=10_000.0) is None


def test_gate_no_attended_session_blocks():
    w = IdleCompactionWatcher()
    assert w.maybe_enqueue(WorkQueue(), now=10_000.0) is None


def test_gate_idle_not_elapsed_blocks():
    w = _ready_watcher(idle_for=10.0)
    assert w.maybe_enqueue(WorkQueue(), now=10_000.0) is None


def test_gate_below_floor_blocks():
    w = _ready_watcher(prefix=10_000)
    assert w.maybe_enqueue(WorkQueue(), now=10_000.0) is None


def test_gate_below_threshold_blocks():
    # Above the floor, below the threshold — a different gate than the floor
    # test above, and the two must be distinguishable.
    w = _ready_watcher(prefix=80_000)
    assert w.maybe_enqueue(WorkQueue(), now=10_000.0) is None


def test_a_deep_turn_on_a_small_session_is_judged_by_its_true_size():
    # 2026-10-05: a fresh 38,406-token session made 5 model calls in one turn,
    # the summed counters read ~190K, and it was compacted for nothing.
    w = IdleCompactionWatcher()
    w.note_attended_turn(
        "s1", {"cache_read_input_tokens": 190_000}, context_tokens=38_406, now=9_600.0
    )
    assert w._eligible(WorkQueue(), now=10_000.0) == (False, "below floor")


def test_a_large_true_size_still_compacts():
    w = IdleCompactionWatcher()
    w.note_attended_turn(
        "s1", {"cache_read_input_tokens": 20_000}, context_tokens=120_000, now=9_600.0
    )
    assert w._eligible(WorkQueue(), now=10_000.0) == (True, "s1")


@pytest.mark.parametrize("context_tokens", [None, 0])
def test_no_true_size_falls_back_to_the_summed_counters(context_tokens):
    # 0 is not a measurement (the 2026-08-21 all-zero trap), so it falls back too.
    w = IdleCompactionWatcher()
    w.note_attended_turn(
        "s1", {"cache_read_input_tokens": 120_000}, context_tokens=context_tokens, now=9_600.0
    )
    assert w._last_attended_prefix_tokens == 120_000


def test_gate_queue_paused_blocks():
    wq = WorkQueue()
    wq._paused = True  # same private flag _wait_for_auth sets while holding
    w = _ready_watcher()
    assert w.maybe_enqueue(wq, now=10_000.0) is None


def test_gate_session_retired_blocks():
    session_state.retire("s1")
    w = _ready_watcher(session_id="s1")
    assert w.maybe_enqueue(WorkQueue(), now=10_000.0) is None


def test_gate_hysteresis_not_met_blocks():
    w = IdleCompactionWatcher()
    w.note_compaction_attempted(
        "s1", succeeded=False, usage=None, turn_id="compact-1", error="boom"
    )
    # Establishes a baseline of 100,000 -- unusually high for a real
    # post-compaction size, but nothing stops it, and it isolates the
    # hysteresis gate from the threshold gate below.
    w.note_attended_turn("s1", {"cache_read_input_tokens": 100_000}, now=9_000.0)
    # Regrown past the threshold (110,000) but short of baseline + growth
    # (100,000 + 27,500 = 127,500).
    w.note_attended_turn("s1", {"cache_read_input_tokens": 120_000}, now=9_500.0)
    assert w.maybe_enqueue(WorkQueue(), now=9_500.0 + 400.0) is None


def test_gate_awaiting_baseline_blocks_even_when_otherwise_eligible():
    w = _ready_watcher(session_id="s1", prefix=150_000, now=9_000.0)
    w.note_compaction_attempted(
        "s1", succeeded=True, usage={}, turn_id="compact-1"
    )  # no attended turn has run since -- still awaiting
    assert w.maybe_enqueue(WorkQueue(), now=9_000.0 + 1_000.0) is None


# --- hysteresis baseline lifecycle -------------------------------------------


def test_hysteresis_baseline_established_by_next_attended_turn_then_refuses_until_growth_met():
    w = IdleCompactionWatcher()
    w.note_attended_turn("s1", {"cache_read_input_tokens": 150_000}, now=1_000.0)
    w.note_compaction_attempted(
        "s1", succeeded=False, usage=None, turn_id="compact-1", error="boom"
    )
    hyst = w._hysteresis["s1"]
    assert hyst.awaiting_baseline is True
    assert hyst.baseline is None

    # Still awaiting baseline: a poll otherwise idle+large enough refuses.
    assert w.maybe_enqueue(WorkQueue(), now=1_000.0 + 1_000.0) is None

    # The next attended turn is what establishes the baseline, at its own
    # observed prefix -- NOT the pre-compaction peak (doc: keying off the
    # peak means never compacting twice in one day).
    w.note_attended_turn("s1", {"cache_read_input_tokens": 90_000}, now=1_500.0)
    assert w._hysteresis["s1"].baseline == 90_000
    assert w._hysteresis["s1"].awaiting_baseline is False

    # Regrowth short of baseline + growth_tokens (90,000 + 27,500 = 117,500)
    # still refuses, even though it's above the raw threshold.
    w.note_attended_turn("s1", {"cache_read_input_tokens": 115_000}, now=1_600.0)
    assert w.maybe_enqueue(WorkQueue(), now=1_600.0 + 400.0) is None

    # Regrowth past baseline + growth_tokens is eligible.
    w.note_attended_turn("s1", {"cache_read_input_tokens": 120_000}, now=1_700.0)

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = w.maybe_enqueue(wq, now=1_700.0 + 400.0)
        assert turn_id is not None
        await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())


def test_success_logs_pre_compaction_tokens_then_observed_reduction(caplog):
    w = IdleCompactionWatcher()

    with caplog.at_level("INFO", logger="server.idle_compaction"):
        w.note_compaction_attempted(
            "s1",
            succeeded=True,
            usage={
                "cache_read_input_tokens": 140_000,
                "cache_creation_input_tokens": 9_000,
                "input_tokens": 1_000,
            },
            turn_id="compact-1",
        )
        w.note_attended_turn(
            "s1", {"cache_read_input_tokens": 30_000}, now=1_500.0
        )

    assert (
        "idle-compaction: succeeded for session s1 (turn compact-1, "
        "pre-compaction prefix=150000 tokens via compaction call); "
        "awaiting post-compaction measurement" in caplog.messages
    )
    assert (
        "idle-compaction: post-compaction prefix for session s1 observed at 30000 "
        "tokens (reduced by 120000 tokens, 80.0%, from 150000)"
        in caplog.messages
    )
    assert w._hysteresis["s1"].pre_compaction_tokens is None


def test_failure_logs_outcome_but_does_not_claim_a_reduction(caplog):
    w = IdleCompactionWatcher()

    with caplog.at_level("INFO", logger="server.idle_compaction"):
        w.note_compaction_attempted(
            "s1",
            succeeded=False,
            usage={"cache_read_input_tokens": 150_000},
            turn_id="compact-1",
            error="dead session",
        )
        w.note_attended_turn(
            "s1", {"cache_read_input_tokens": 150_000}, now=1_500.0
        )

    assert (
        "idle-compaction: failed for session s1 (turn compact-1, error=dead session); "
        "awaiting hysteresis baseline before retry"
        in caplog.messages
    )
    assert not any("reduced by" in message for message in caplog.messages)
    assert (
        "idle-compaction: hysteresis baseline for session s1 established at 150000 tokens"
        in caplog.messages
    )

# --- happy path: the actual enqueue -------------------------------------------


def test_happy_path_enqueues_exactly_one_compaction_turn_with_the_right_fields():
    async def _body():
        wq = WorkQueue()
        wq.start()
        w = _ready_watcher(session_id="s1", prefix=120_000, idle_for=400.0, now=10_000.0)

        turn_id = w.maybe_enqueue(wq, now=10_000.0)
        assert turn_id is not None

        turn = wq.get_turn(turn_id)
        assert turn.source == SOURCE
        assert turn.session_id == "s1"
        assert turn.resume is True
        assert turn.require_session is True
        assert turn.raw_prompt is True
        assert turn.prompt == ic.COMPACT_PROMPT
        assert turn.retire_session_on_success is False

        # A second poll while one is already outstanding does not pile up a
        # second turn.
        again = w.maybe_enqueue(wq, now=10_000.0 + 1.0)
        assert again is None

        await _run_to_completion(wq, turn_id)
        await wq.stop()

    asyncio.run(_body())


# --- cancellation on an attended enqueue --------------------------------------


def test_attended_enqueue_cancels_a_still_queued_compaction_and_drain_skips_it():
    async def _body():
        wq = WorkQueue()
        wq.start()
        # Both enqueued synchronously, before the drain task gets a chance to
        # run either -- same trick test_queue.py's burst tests rely on.
        compaction_id = wq.enqueue(
            ic.COMPACT_PROMPT,
            session_id="fake-session-123",
            resume=True,
            source=SOURCE,
            require_session=True,
            raw_prompt=True,
        )
        chat_id = wq.enqueue("hello", source="chat")

        assert wq.get_turn(compaction_id).cancelled is True

        chat_turn = await _run_to_completion(wq, chat_id)
        assert chat_turn.status == TurnStatus.DONE

        compaction_turn = await _run_to_completion(wq, compaction_id)
        assert compaction_turn.status == TurnStatus.CANCELLED
        assert compaction_turn.result is None  # never actually invoked

        await wq.stop()

    asyncio.run(_body())


def test_a_running_compaction_is_not_cancelled_by_an_attended_message():
    """Mike's call (settled 2026-08-18): a compaction already RUNNING is let
    to finish -- only a still-PENDING one is cancelled."""
    async def _body():
        wq = WorkQueue()
        wq.start()
        compaction_id = wq.enqueue(
            ic.COMPACT_PROMPT,
            session_id="fake-session-123",
            resume=True,
            source=SOURCE,
            require_session=True,
            raw_prompt=True,
        )
        # Let the drain loop actually pick it up and mark it RUNNING.
        deadline = asyncio.get_event_loop().time() + 2.0
        while wq.get_turn(compaction_id).status == TurnStatus.PENDING:
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("compaction turn never started running")
            await asyncio.sleep(0.001)

        wq.enqueue("hello", source="chat")
        # A RUNNING turn must never be marked cancelled by a later enqueue.
        assert wq.get_turn(compaction_id).cancelled is False

        compaction_turn = await _run_to_completion(wq, compaction_id)
        assert compaction_turn.status == TurnStatus.DONE

        await wq.stop()

    asyncio.run(_body())


# --- delivery exclusion -------------------------------------------------------


def test_compaction_turn_is_not_delivered_and_does_not_push(monkeypatch):
    calls = []
    monkeypatch.setattr("server.queue.push_broadcast_nowait", lambda p: calls.append(p))

    async def _body():
        wq = WorkQueue()
        wq.start()
        turn_id = wq.enqueue(
            ic.COMPACT_PROMPT,
            session_id="fake-session-123",
            resume=True,
            source=SOURCE,
            require_session=True,
            raw_prompt=True,
        )
        turn = await _run_to_completion(wq, turn_id)
        assert turn.status == TurnStatus.DONE  # conftest's fake claude always succeeds
        await wq.stop()
        return turn_id

    turn_id = asyncio.run(_body())
    rows = transcript.read_after(0)["rows"]
    assert turn_id not in [r["turn_id"] for r in rows], (
        "a compaction turn must never surface in the transcript"
    )
    assert calls == [], "a compaction turn must never trigger a push"


def test_compaction_turn_is_a_404_via_v1_turn_endpoint():
    # Exercises app.py's get_turn route function directly rather than through
    # TestClient's background-thread portal: work_queue is a process-wide
    # singleton, and enqueuing on it from a thread other than the one running
    # its event loop (which is what a `with TestClient(...)` block spins up)
    # doesn't reliably wake the drain loop promptly — observed as a flaky
    # multi-second stall under full-suite load. Driving everything from one
    # asyncio.run() here keeps it deterministic while still exercising the
    # real singleton and the real route function, just without going through
    # HTTP/ASGI.
    from fastapi import HTTPException

    from server.app import get_turn
    from server.queue import work_queue

    async def _body():
        work_queue.start()
        turn_id = work_queue.enqueue(
            ic.COMPACT_PROMPT,
            session_id="fake-session-123",
            resume=True,
            source=SOURCE,
            require_session=True,
            raw_prompt=True,
        )
        await _run_to_completion(work_queue, turn_id)
        with pytest.raises(HTTPException) as exc_info:
            get_turn(turn_id, "unused-token")
        assert exc_info.value.status_code == 404
        await work_queue.stop()

    asyncio.run(_body())


def test_compaction_turn_never_notes_attended_session_or_triggers_retirement():
    """Guards against source == "compaction" ever reaching
    session_state.note_attended / retirement -- both are gated on
    `source == "chat"` in server/queue.py's _drain."""
    async def _body():
        wq = WorkQueue()
        wq.start()
        before = session_state.conversation_session_id
        turn_id = wq.enqueue(
            ic.COMPACT_PROMPT,
            session_id="fake-session-123",
            resume=True,
            source=SOURCE,
            require_session=True,
            raw_prompt=True,
            retire_session_on_success=False,
        )
        await _run_to_completion(wq, turn_id)
        assert session_state.conversation_session_id == before
        assert wq.is_session_retired("fake-session-123") is False
        await wq.stop()

    asyncio.run(_body())


# --- cron does not touch the idle clock ---------------------------------------


def test_cron_turns_do_not_reset_the_idle_clock(monkeypatch):
    calls = []
    fresh = IdleCompactionWatcher()
    monkeypatch.setattr(fresh, "note_attended_turn", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr("server.queue.idle_compaction_watcher", fresh)

    async def _body():
        wq = WorkQueue()
        wq.start()
        cron_id = wq.enqueue("cron thing", source="cron")
        await _run_to_completion(wq, cron_id)
        chat_id = wq.enqueue("hi", source="chat")
        await _run_to_completion(wq, chat_id)
        await wq.stop()

    asyncio.run(_body())
    assert len(calls) == 1, "only the chat turn should have touched the idle clock"


# --- raw_prompt really bypasses prepend_turn_context / the attachment line ---


def test_raw_prompt_bypasses_turn_context_and_attachment_prepending(tmp_path, monkeypatch):
    fake_bin = tmp_path / "claude"
    fake_bin.write_text(
        textwrap.dedent(
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
        )
    )
    fake_bin.chmod(0o755)
    monkeypatch.setattr("server.invoke.settings", dataclasses.replace(real_settings, claude_bin=str(fake_bin)))

    result = asyncio.run(
        run_claude(
            "/compact",
            session_id="s1",
            resume=True,
            attachment_paths=("/home/marlow/.marlow/attachments/t1/0.jpg",),
            raw_prompt=True,
        )
    )
    # Bare "/compact", nothing prepended -- the probe found this is what
    # actually triggers compaction in headless mode.
    assert result.result_text == "/compact"


# --- the watcher loop survives a raising poll ---------------------------------


def test_watch_loop_survives_a_raising_poll(monkeypatch):
    monkeypatch.setattr(ic, "settings", _settings(idle_compaction_poll_interval_seconds=0.01))
    calls = {"n": 0}
    w = IdleCompactionWatcher()

    def _boom(_wq):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(w, "maybe_enqueue", _boom)

    async def _body():
        w.start()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if calls["n"] >= 3:
                break
        await w.stop()

    asyncio.run(_body())
    assert calls["n"] >= 3, "the loop must keep polling after the first poll raised"


def test_stop_is_safe_when_never_started():
    asyncio.run(IdleCompactionWatcher().stop())


# --- the watcher must learn from FAILURE too (review 2026-08-18, findings 1-3) --
#
# All three were one root cause: every piece of the watcher's state hung off a
# `status == DONE` branch, and every piece of that state is a *reason to
# compact*. So a failed turn didn't make the watcher cautious, it made it
# eager. These tests pin the corrected invariant — an outcome the watcher
# can't interpret moves it toward "I don't know", never leaves it on a stale
# belief that says compact.


def test_failed_compaction_does_not_retry_on_the_very_next_poll():
    """Finding 1. `require_session=True` turns off invoke.py's dead-session
    self-heal, so a stale session id fails permanently, not transiently — an
    un-marked failure meant a full-context `claude -p` every poll interval all
    night, each one billed."""
    wq = WorkQueue()
    w = _ready_watcher()
    assert w._eligible(wq, now=10_000.0)[0] is True

    # The compaction ran and FAILED (no result, no session id parsed) — the
    # queue still names the session it was trying to compact.
    w.note_compaction_attempted(
        "s1", succeeded=False, usage=None, turn_id="compact-1", error="boom"
    )

    eligible, reason = w._eligible(wq, now=10_000.0)
    assert eligible is False
    assert reason == "awaiting hysteresis baseline"

    # And it stays blocked poll after poll, however long Mike stays idle —
    # nothing about waiting longer makes a permanently-broken session compact.
    assert w._eligible(wq, now=99_999.0)[0] is False


def test_failed_compaction_retries_only_after_the_growth_gate_is_met_again():
    """The flip side of the test above: blocked is not blocked *forever*. A
    transient failure must recover on its own once the conversation moves on."""
    wq = WorkQueue()
    w = _ready_watcher()
    w.note_compaction_attempted(
        "s1", succeeded=False, usage=None, turn_id="compact-1", error="boom"
    )

    # Next attended turn sets the baseline at the (still uncompacted) size.
    w.note_attended_turn("s1", {"cache_read_input_tokens": 120_000}, now=10_100.0)
    assert w._eligible(wq, now=10_500.0) == (False, "hysteresis growth not met")

    # Growth threshold is 27_500, so 120_000 + 27_500 is the bar.
    w.note_attended_turn("s1", {"cache_read_input_tokens": 147_500}, now=11_000.0)
    assert w._eligible(wq, now=11_500.0) == (True, "s1")


def test_attended_turn_without_usage_clears_the_prefix_instead_of_keeping_a_stale_one():
    """Finding 2. The session id and the prefix are one fact together. Updating
    the id while keeping the previous session's number pairs a brand-new
    post-rollover session with the retired session's size, and fires a
    compaction that reclaims nothing (the probe's preTokens: 657 waste)."""
    wq = WorkQueue()
    w = _ready_watcher(session_id="old-big-session", prefix=400_000)
    assert w._eligible(wq, now=10_000.0) == (True, "old-big-session")

    # Rollover: the phone's next message lands in a fresh session, and that
    # turn reports no usage block (InvocationResult.usage is documented as
    # possibly empty).
    w.note_attended_turn("fresh-session", None, now=10_100.0)

    eligible, reason = w._eligible(wq, now=10_500.0)
    assert eligible is False
    assert reason == "no prefix data recorded yet"

    # It recovers as soon as a turn actually measures the new session.
    w.note_attended_turn("fresh-session", {"cache_read_input_tokens": 3_000}, now=10_600.0)
    assert w._eligible(wq, now=11_000.0) == (False, "below floor")


def test_errored_attended_turn_still_resets_the_idle_clock():
    """Finding 3. Mike's message failed and he is right there about to retry;
    the clock must not still be reading from his last *successful* turn."""
    wq = WorkQueue()
    w = _ready_watcher(now=10_000.0, idle_for=400.0)
    assert w._eligible(wq, now=10_000.0)[0] is True

    # An ERRORed turn: no session id, no usage — but he is demonstrably active.
    w.note_attended_turn(None, None, now=10_000.0)

    assert w._eligible(wq, now=10_000.0) == (False, "not idle long enough")
    # ...and the session/prefix it already knew about are untouched, so once
    # the idle window passes again it is eligible on the same evidence.
    assert w._eligible(wq, now=10_400.0) == (True, "s1")


def test_drain_reports_a_failed_compaction_to_the_watcher(tmp_path, monkeypatch):
    """The queue-side half of finding 1: the hook is called from a code path
    the exception branches actually reach. An earlier revision read a local
    `result` that `except InvokeError` never binds."""
    attempts = []
    fresh = IdleCompactionWatcher()
    monkeypatch.setattr(
        fresh,
        "note_compaction_attempted",
        lambda sid, **kwargs: attempts.append((sid, kwargs)),
    )
    monkeypatch.setattr("server.queue.idle_compaction_watcher", fresh)

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
        turn = await _run_to_completion(
            wq,
            wq.enqueue("/compact", session_id="s1", resume=True, source=SOURCE,
                       require_session=True, raw_prompt=True),
        )
        assert turn.status == TurnStatus.ERROR
        await wq.stop()

    asyncio.run(_body())
    assert len(attempts) == 1, "a failed compaction must still count as an attempt"
    assert attempts[0][0] == "s1"
    assert attempts[0][1]["succeeded"] is False
    assert attempts[0][1]["turn_id"]


def test_drain_reports_an_errored_attended_turn_to_the_watcher(tmp_path, monkeypatch):
    """The queue-side half of finding 3."""
    calls = []
    fresh = IdleCompactionWatcher()
    monkeypatch.setattr(fresh, "note_attended_turn", lambda *a, **k: calls.append(a))
    monkeypatch.setattr("server.queue.idle_compaction_watcher", fresh)

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
        turn = await _run_to_completion(wq, wq.enqueue("hi", source="chat"))
        assert turn.status == TurnStatus.ERROR
        await wq.stop()

    asyncio.run(_body())
    assert len(calls) == 1, "an ERRORed phone turn still means Mike is at his phone"


def test_cancelled_compaction_is_not_counted_as_an_attempt(monkeypatch):
    """A cancelled compaction never invoked anything, so it must not consume
    the retry budget — the cancel branch `continue`s before the hook."""
    attempts = []
    fresh = IdleCompactionWatcher()
    monkeypatch.setattr(
        fresh,
        "note_compaction_attempted",
        lambda sid, **kwargs: attempts.append((sid, kwargs)),
    )
    monkeypatch.setattr("server.queue.idle_compaction_watcher", fresh)

    async def _body():
        wq = WorkQueue()
        wq.start()
        compaction_id = wq.enqueue("/compact", session_id="s1", resume=True, source=SOURCE,
                                   require_session=True, raw_prompt=True)
        wq.get_turn(compaction_id).cancelled = True
        await _run_to_completion(wq, compaction_id)
        assert wq.get_turn(compaction_id).status == TurnStatus.CANCELLED
        await wq.stop()

    asyncio.run(_body())
    assert attempts == []


def test_zero_usage_falls_back_to_the_last_attended_measurement(caplog):
    """The 2026-08-21 production case, which printed no reduction at all.

    The compaction call returned its prefix keys present and all ZERO, so
    `_sum_prefix_tokens` returned 0 rather than None, and the reduction log
    (guarded on `> 0`) silently declined to say anything. The real numbers
    were 142967 -> 33770.
    """
    w = IdleCompactionWatcher()

    # An attended turn measures the session at its real size first — this is
    # what the enqueue line prints and what `_eligible` gates on.
    w.note_attended_turn("s1", {"cache_read_input_tokens": 142_967}, now=1_000.0)

    with caplog.at_level("INFO", logger="server.idle_compaction"):
        w.note_compaction_attempted(
            "s1",
            succeeded=True,
            usage={
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "input_tokens": 0,
            },
            turn_id="compact-1",
        )
        w.note_attended_turn(
            "s1", {"cache_read_input_tokens": 33_770}, now=2_000.0
        )

    assert (
        "idle-compaction: succeeded for session s1 (turn compact-1, "
        "pre-compaction prefix=142967 tokens via last attended turn); "
        "awaiting post-compaction measurement" in caplog.messages
    )
    assert (
        "idle-compaction: post-compaction prefix for session s1 observed at 33770 "
        "tokens (reduced by 109197 tokens, 76.4%, from 142967)"
        in caplog.messages
    )


def test_fallback_never_borrows_another_sessions_size(caplog):
    """The fallback is guarded on the session id, and must be.

    `_last_attended_prefix_tokens` belongs to whichever session was last
    attended. Using it for a *different* session would pair one session's size
    with another's and invent a reduction out of nothing -- the same mistake
    note_attended_turn clears the field unconditionally to avoid.
    """
    w = IdleCompactionWatcher()
    w.note_attended_turn("s1", {"cache_read_input_tokens": 142_967}, now=1_000.0)

    with caplog.at_level("INFO", logger="server.idle_compaction"):
        w.note_compaction_attempted(
            "s2",
            succeeded=True,
            usage={"cache_read_input_tokens": 0},
            turn_id="compact-2",
        )

    assert w._hysteresis["s2"].pre_compaction_tokens is None
    assert (
        "idle-compaction: succeeded for session s2 (turn compact-2, "
        "pre-compaction prefix unavailable); awaiting post-compaction measurement"
        in caplog.messages
    )
    assert not any("142967" in m for m in caplog.messages)


def test_zero_usage_with_no_attended_measurement_reports_unavailable(caplog):
    """Zero from the compaction call and nothing to fall back to.

    Must log "unavailable", not "prefix=0 tokens" -- the latter reads as a
    real measurement, and is exactly the line that sent Mike looking for a
    reduction that was never going to print.
    """
    w = IdleCompactionWatcher()

    with caplog.at_level("INFO", logger="server.idle_compaction"):
        w.note_compaction_attempted(
            "s1",
            succeeded=True,
            usage={"cache_read_input_tokens": 0},
            turn_id="compact-1",
        )

    assert w._hysteresis["s1"].pre_compaction_tokens is None
    assert (
        "idle-compaction: succeeded for session s1 (turn compact-1, "
        "pre-compaction prefix unavailable); awaiting post-compaction measurement"
        in caplog.messages
    )
    assert not any("prefix=0" in m for m in caplog.messages)


# --- the baseline is the MINIMUM reading since the compaction, not the first --
#
# `_sum_prefix_tokens` reads size × loop depth, not size (usage-telemetry.md,
# 2026-08-10). Everywhere else that inflation just makes the watcher compact
# when it need not have; frozen into the baseline it suppresses compaction
# instead, which is the expensive direction. See note_attended_turn.


def test_a_smaller_later_reading_lowers_the_baseline(caplog):
    w = _ready_watcher(session_id="s1", prefix=133_806)
    w.note_compaction_attempted("s1", succeeded=True, usage={}, turn_id="t1")
    # The first turn after the compaction is a deep one: ~4 calls against a
    # ~33K session reads as 142,028. That is 2026-08-23 06:40:38 exactly.
    w.note_attended_turn("s1", {"cache_read_input_tokens": 142_028}, now=9_000.0)
    assert w._hysteresis["s1"].baseline == 142_028

    with caplog.at_level("INFO", logger="server.idle_compaction"):
        w.note_attended_turn("s1", {"cache_read_input_tokens": 33_014}, now=9_100.0)
    assert w._hysteresis["s1"].baseline == 33_014
    assert any("lowering hysteresis baseline for session s1" in m for m in caplog.messages)


def test_a_larger_later_reading_does_not_raise_the_baseline():
    """Ordinary growth through the day is not evidence about how small
    compaction can make the session."""
    w = _ready_watcher(session_id="s1", prefix=133_806)
    w.note_compaction_attempted("s1", succeeded=True, usage={}, turn_id="t1")
    w.note_attended_turn("s1", {"cache_read_input_tokens": 33_014}, now=9_000.0)
    w.note_attended_turn("s1", {"cache_read_input_tokens": 90_000}, now=9_100.0)
    assert w._hysteresis["s1"].baseline == 33_014


def test_an_inflated_first_reading_no_longer_suppresses_the_next_compaction():
    """The cost the minimum exists to prevent: a poisoned baseline of 142,028
    makes the growth gate want ~169,500 before compacting again, so the
    session sails past the threshold and Mike pays the cold start."""
    w = _ready_watcher(session_id="s1", prefix=133_806, idle_for=400.0, now=10_000.0)
    w.note_compaction_attempted("s1", succeeded=True, usage={}, turn_id="t1")
    w.note_attended_turn("s1", {"cache_read_input_tokens": 142_028}, now=9_000.0)

    # A quiet, single-call turn reveals the session's real size, and then it
    # grows past the threshold in the ordinary way.
    w.note_attended_turn("s1", {"cache_read_input_tokens": 33_014}, now=9_100.0)
    w.note_attended_turn("s1", {"cache_read_input_tokens": 120_000}, now=9_600.0)
    assert w._eligible(WorkQueue(), now=10_000.0) == (True, "s1")


def test_the_minimum_does_not_survive_the_next_compaction():
    """Each compaction starts the search for a floor over again — otherwise a
    session that legitimately grew would be measured against a floor from
    hours earlier."""
    w = _ready_watcher(session_id="s1", prefix=133_806)
    w.note_compaction_attempted("s1", succeeded=True, usage={}, turn_id="t1")
    w.note_attended_turn("s1", {"cache_read_input_tokens": 33_014}, now=9_000.0)
    assert w._hysteresis["s1"].baseline == 33_014

    w.note_compaction_attempted("s1", succeeded=True, usage={}, turn_id="t2")
    assert w._hysteresis["s1"].baseline is None
    assert w._hysteresis["s1"].awaiting_baseline is True


def test_a_zero_reading_never_becomes_a_baseline():
    """`_sum_prefix_tokens` returns 0 (not None) when the prefix keys are
    present and all zeroed — a real production case, 2026-08-21. Recording it
    as the floor would say "this session compacts down to nothing", and the
    growth gate reads that literally: it would then ask only for
    `idle_compaction_growth_tokens` before compacting, every idle stretch, for
    the rest of the session."""
    zeroed = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "input_tokens": 0}

    # ...as the FIRST reading after a compaction: stay awaiting, don't guess.
    w = _ready_watcher(session_id="s1", prefix=133_806)
    w.note_compaction_attempted("s1", succeeded=True, usage={}, turn_id="t1")
    w.note_attended_turn("s1", zeroed, now=9_000.0)
    assert w._hysteresis["s1"].baseline is None
    assert w._hysteresis["s1"].awaiting_baseline is True, "an unusable reading means 'I don't know'"

    # ...and as a later reading, where it would otherwise lower an established
    # baseline all the way to zero.
    w.note_attended_turn("s1", {"cache_read_input_tokens": 33_014}, now=9_100.0)
    assert w._hysteresis["s1"].baseline == 33_014
    w.note_attended_turn("s1", zeroed, now=9_200.0)
    assert w._hysteresis["s1"].baseline == 33_014

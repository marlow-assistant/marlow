"""Which turns update which half of the live usage state
(server/usage_state.py): the plan reading from ANY turn, the conversation from
CHAT turns only."""
import asyncio

import pytest

from server.invoke import InvocationResult, PlanUsage
from server.queue import TurnStatus, WorkQueue
from server.usage_state import UsageState


def _plan(at=100.0, utilization=0.1):
    return PlanUsage(
        observed_at=at, status="allowed",
        windows={"five_hour": {"utilization": utilization, "resets_at": 5000}},
    )


def _result(session="s1", tokens=None, plan=None, is_error=False):
    return InvocationResult(
        exit_code=0, is_error=is_error, subtype="success", result_text="x",
        session_id=session, plan_usage=plan, context_tokens=tokens,
    )


@pytest.mark.parametrize("source", ["chat", "cron", "system", "compaction"])
def test_plan_comes_from_any_source(source):
    state = UsageState()
    state.note_finished_turn(source, _result(plan=_plan(utilization=0.3)))
    assert state.plan.windows["five_hour"]["utilization"] == 0.3


def test_a_turn_without_a_plan_reading_keeps_the_previous_one():
    state = UsageState()
    state.note_finished_turn("cron", _result(plan=_plan(utilization=0.3)))
    state.note_finished_turn("chat", _result(plan=None))
    assert state.plan.windows["five_hour"]["utilization"] == 0.3


@pytest.mark.parametrize("source", ["cron", "system", "compaction", None])
def test_conversation_is_not_touched_by_unattended_turns(source):
    state = UsageState()
    state.note_finished_turn("chat", _result("chat-session", 50_000), now=10.0)
    state.note_finished_turn(source, _result("other-session", 999), now=20.0)
    assert state.conversation.session_id == "chat-session"
    assert state.conversation.context_tokens == 50_000
    assert state.conversation.observed_at == 10.0


def test_a_chat_turn_sets_session_size_and_time():
    state = UsageState()
    state.note_finished_turn("chat", _result("s1", 42_000), now=77.0)
    assert (state.conversation.session_id, state.conversation.context_tokens,
            state.conversation.observed_at) == ("s1", 42_000, 77.0)


def test_a_chat_turn_with_no_size_keeps_the_same_sessions_last_size():
    state = UsageState()
    state.note_finished_turn("chat", _result("s1", 42_000), now=10.0)
    state.note_finished_turn("chat", _result("s1", None, is_error=True), now=20.0)
    assert state.conversation.context_tokens == 42_000
    assert state.conversation.observed_at == 10.0  # its own age, not the failed turn's


def test_a_new_session_never_inherits_the_old_sessions_size():
    state = UsageState()
    state.note_finished_turn("chat", _result("s1", 42_000), now=10.0)
    state.note_finished_turn("chat", _result("s2", None), now=20.0)
    assert state.conversation.session_id == "s2"
    assert state.conversation.context_tokens is None


def test_no_result_changes_nothing():
    state = UsageState()
    state.note_finished_turn("chat", _result("s1", 1), now=1.0)
    state.note_finished_turn("chat", None)
    assert state.conversation.session_id == "s1"


def test_the_queue_feeds_the_state_from_chat_and_cron_turns(monkeypatch):
    """Wired through WorkQueue._drain, including a FAILED turn that carried a
    reading."""
    from server.usage_state import usage_state

    results = iter(
        [
            _result("chat-1", 61_000, _plan(utilization=0.2)),
            _result("cron-1", 5_000, _plan(utilization=0.4)),
            _result("chat-1", None, _plan(utilization=0.5), is_error=True),
        ]
    )

    async def _fake_run_claude(prompt, **kwargs):
        return next(results)

    monkeypatch.setattr("server.queue.run_claude", _fake_run_claude)

    async def _finish(wq, turn_id):
        deadline = asyncio.get_event_loop().time() + 5
        while wq.get_turn(turn_id).status not in (TurnStatus.DONE, TurnStatus.ERROR):
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0.02)
        # note_finished_turn runs just after the status is stamped.
        await asyncio.sleep(0.1)

    async def _body():
        wq = WorkQueue()
        wq.start()
        try:
            await _finish(wq, wq.enqueue("a", source="chat"))
            assert usage_state.conversation.context_tokens == 61_000
            assert usage_state.plan.windows["five_hour"]["utilization"] == 0.2

            await _finish(wq, wq.enqueue("b", source="cron"))
            assert usage_state.plan.windows["five_hour"]["utilization"] == 0.4
            assert usage_state.conversation.session_id == "chat-1"  # cron did not move it

            await _finish(wq, wq.enqueue("c", source="chat"))
            assert usage_state.plan.windows["five_hour"]["utilization"] == 0.5
            assert usage_state.conversation.context_tokens == 61_000
        finally:
            await wq.stop()

    asyncio.run(_body())


def test_a_successful_compaction_of_the_conversation_makes_its_size_unknown():
    # Review 2026-10-04: the pre-compaction size would otherwise stay on the
    # sheet until the next chat turn -- right when Mike has gone idle and might look.
    state = UsageState()
    state.note_finished_turn("chat", _result("s1", 170_000), now=10.0)
    state.note_finished_turn("compaction", _result("s1", None), now=20.0)
    assert state.conversation.session_id == "s1"
    assert state.conversation.context_tokens is None
    assert state.conversation.observed_at == 20.0


def test_a_failed_compaction_leaves_the_size_alone():
    state = UsageState()
    state.note_finished_turn("chat", _result("s1", 170_000), now=10.0)
    state.note_finished_turn("compaction", _result("s1", None, is_error=True), now=20.0)
    assert state.conversation.context_tokens == 170_000

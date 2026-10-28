import asyncio
import threading
import time

from server.permissions import PermissionRegistry


def test_create_registers_a_pending_request():
    async def _body():
        reg = PermissionRegistry()
        req = reg.create("Bash", {"command": "ls"}, "session-1")
        assert req.resolved is False
        assert [r.request_id for r in reg.pending()] == [req.request_id]

    asyncio.run(_body())


def test_answer_resolves_the_wait():
    async def _body():
        reg = PermissionRegistry()
        req = reg.create("Bash", {"command": "ls"}, "session-1")

        async def _answer_soon():
            await asyncio.sleep(0.02)
            ok = reg.answer(req.request_id, "allow")
            assert ok is True

        waiter = asyncio.create_task(reg.wait_for_decision(req.request_id, timeout=5))
        answerer = asyncio.create_task(_answer_soon())
        decision = await waiter
        await answerer

        assert decision == "allow"
        assert req.resolved is True
        assert req.timed_out is False
        assert reg.pending() == []

    asyncio.run(_body())


def test_timeout_resolves_deny_and_marks_timed_out():
    async def _body():
        reg = PermissionRegistry()
        req = reg.create("Bash", {"command": "ls"}, "session-1")
        decision = await reg.wait_for_decision(req.request_id, timeout=0.02)
        assert decision == "deny"
        assert req.resolved is True
        assert req.timed_out is True

    asyncio.run(_body())


def test_answer_is_single_use_late_answer_discarded():
    async def _body():
        reg = PermissionRegistry()
        req = reg.create("Bash", {"command": "ls"}, "session-1")
        decision = await reg.wait_for_decision(req.request_id, timeout=0.02)
        assert decision == "deny"  # timed out first

        late = reg.answer(req.request_id, "allow")
        assert late is False  # discarded, not replayed
        assert req.decision == "deny"  # unchanged by the late answer

    asyncio.run(_body())


def test_answer_unknown_request_id_is_a_noop():
    reg = PermissionRegistry()
    assert reg.answer("does-not-exist", "allow") is False


def test_double_answer_second_call_discarded():
    async def _body():
        reg = PermissionRegistry()
        req = reg.create("Bash", {"command": "ls"}, "session-1")
        waiter = asyncio.create_task(reg.wait_for_decision(req.request_id, timeout=5))
        await asyncio.sleep(0.01)

        assert reg.answer(req.request_id, "allow") is True
        assert reg.answer(req.request_id, "deny") is False  # already resolved

        decision = await waiter
        assert decision == "allow"

    asyncio.run(_body())


def test_answer_from_a_real_os_thread_resolves_correctly():
    """The registry must be safe to answer() from a thread other than the
    loop's own — POST /v1/permission/{request_id} can be dispatched to a
    threadpool worker thread by the ASGI framework, not the loop thread that
    owns the Future. A direct future.set_result() from a foreign thread can
    sit unseen until the loop wakes for an unrelated reason; call_soon_threadsafe
    is what makes this reliably fast instead."""

    async def _body():
        reg = PermissionRegistry()
        req = reg.create("Bash", {"command": "ls"}, "session-1")

        def _answer_from_another_thread():
            time.sleep(0.02)
            reg.answer(req.request_id, "allow")

        thread = threading.Thread(target=_answer_from_another_thread)
        thread.start()
        decision = await reg.wait_for_decision(req.request_id, timeout=5)
        thread.join()

        assert decision == "allow"

    asyncio.run(_body())


def test_resolved_requests_are_pruned_not_retained_forever():
    async def _body():
        reg = PermissionRegistry()
        req = reg.create("Bash", {"command": "ls"}, "session-1")
        reg.answer(req.request_id, "allow")

        assert req.request_id not in reg._requests
        assert req.request_id not in reg._futures
        assert reg.pending() == []
        # the caller's own reference is unaffected by pruning
        assert req.decision == "allow"
        assert req.resolved is True

    asyncio.run(_body())


def test_oldest_pending_age_none_when_empty():
    reg = PermissionRegistry()
    assert reg.oldest_pending_age() is None


def test_oldest_pending_age_tracks_open_requests_only():
    async def _body():
        reg = PermissionRegistry()
        older = reg.create("Bash", {"command": "ls"}, "session-1")
        reg.create("Bash", {"command": "pwd"}, "session-1")

        await asyncio.sleep(0.02)
        assert reg.oldest_pending_age() >= 0.02

        reg.answer(older.request_id, "allow")
        # still one open request left, age still tracked
        assert reg.oldest_pending_age() >= 0.02

    asyncio.run(_body())

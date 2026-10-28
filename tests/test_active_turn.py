"""server/active_turn.py — who is behind the invocation in flight.

The value this carries decides whether a permission request rings Mike's phone
or dies instantly, so the tests worth having are about the *edges*: what it says
when nothing is running, when something unexpected is running, and whether it
cleans up after a failure.
"""
from __future__ import annotations

import asyncio

import pytest

from server import active_turn


@pytest.fixture(autouse=True)
def _clean():
    """Every test starts from "nothing in flight". Module state is shared, and
    a leaked value here would make an unrelated test's permission request take
    the wrong branch — the exact bug class this module can cause."""
    active_turn._current_source = None
    yield
    active_turn._current_source = None


def test_nothing_in_flight_is_unattended():
    """The default has to be the fail-closed one: a permission request that
    arrives when no invocation is running came from somewhere unexpected, and
    "ask Mike" is the wrong answer to that."""
    assert active_turn.current_source() is None
    assert active_turn.is_unattended()


def test_chat_is_attended():
    with active_turn.running("chat"):
        assert not active_turn.is_unattended()


def test_the_pre_rename_ios_spelling_is_still_attended():
    """Before the rename to "chat", `/v1/message` wrote "ios". Nothing writes it
    now, but it stays a chat source for good because stored records
    carry it, so it must not fall out of the allowlist."""
    with active_turn.running("ios"):
        assert not active_turn.is_unattended()
    assert active_turn.CHAT_SOURCES == frozenset({"chat", "ios"})


def test_every_chat_source_is_attended():
    """A chat source missing from the allowlist is the fail-closed hazard the
    rename could have shipped: no streaming, and every tool request denied
    without asking."""
    assert active_turn.CHAT_SOURCES <= active_turn.ATTENDED_SOURCES


@pytest.mark.parametrize("source", ["cron", "system", None, "web", "CRON", "chat "])
def test_everything_else_is_unattended(source):
    """Note `"CRON"` and `"chat "` in there: the check is exact membership, so a
    caller that gets the spelling subtly wrong fails closed rather than
    accidentally matching. `"chat "` with the trailing space is the shape a
    config-file round-trip produces."""
    with active_turn.running(source):
        assert active_turn.is_unattended()


def test_the_source_is_cleared_when_the_block_exits():
    with active_turn.running("chat"):
        pass
    assert active_turn.is_unattended(), "an attended source outlived its invocation"


def test_an_exception_still_clears_the_source():
    """`run_claude` can raise InvokeError straight through this block. If the
    source leaked, the *next* cron job would inherit "chat" and start ringing
    the phone at 08:00 — silently, and only after an unrelated failure."""
    with pytest.raises(RuntimeError):
        with active_turn.running("chat"):
            raise RuntimeError("invocation blew up")
    assert active_turn.is_unattended()


def test_nesting_restores_the_outer_source_rather_than_clearing():
    with active_turn.running("chat"):
        with active_turn.running("cron"):
            assert active_turn.is_unattended()
        assert not active_turn.is_unattended(), "the outer invocation is still in flight"


def test_it_is_visible_from_a_sibling_asyncio_task():
    """The reason this is a module global and not a ContextVar.

    `queue._drain` sets it; the `/internal/permission-request` handler reads it.
    Those are SIBLING tasks — uvicorn creates the handler task itself, with no
    relationship to the drain task holding the value — and a ContextVar set in
    one sibling is invisible in the other. Getting that wrong would make the
    whole mechanism a silent no-op: every attended iOS prompt would read `None`
    and auto-deny.

    The shape here is load-bearing and was wrong on the first attempt (review,
    2026-08-07). The obvious version --

        with active_turn.running("chat"):
            await asyncio.create_task(reader())

    -- proves nothing, because `asyncio.create_task` snapshots the current
    context via `copy_context()`, so a CHILD task created inside the block
    inherits a ContextVar just fine. That version passes against the exact
    implementation this test exists to rule out. The reader must therefore be
    started from outside the block, while a separate task holds it open.
    """
    async def _body():
        seen = []
        holding = asyncio.Event()
        release = asyncio.Event()

        async def _holder():
            with active_turn.running("chat"):
                holding.set()
                await release.wait()

        async def _reader():
            seen.append(active_turn.current_source())

        holder = asyncio.create_task(_holder())
        await holding.wait()
        # Created out here, so it inherits the *test's* context, not _holder's
        # -- exactly as uvicorn's request handler does relative to the drain
        # loop. Under a ContextVar this appends None and the test fails.
        await asyncio.create_task(_reader())
        release.set()
        await holder
        return seen

    assert asyncio.run(_body()) == ["chat"]

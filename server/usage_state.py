"""The latest usage readings, in memory (docs/tasks/usage-visibility.md).

Two facts, held separately because they come from different turns:

- **plan**: the subscription's rate-limit windows. The windows are account-wide,
  so a reading from ANY turn is as good as any other -- cron turns refresh it
  several times a day for free, which is what keeps "as of" short.
- **conversation**: the true size of the phone's conversation, from CHAT turns
  only (`active_turn.ATTENDED_SOURCES`). A cron or compaction turn reads some
  other session, or the same one mid-shrink, and must not overwrite it.

Not persisted, on purpose: a restart shows "no reading yet" until the next
turn, which is cheaper and more honest than a number of unknown age. One work
queue serializes every turn (CLAUDE.md), so the only writer is `queue._drain`;
`/v1/usage` reads from another task, and each field is a single reference
assignment, so no lock.

**Display only.** `idle_compaction` takes the same true size, but straight off
each turn's `InvocationResult` with its own session pairing and hysteresis; this
copy is shaped for the phone (cleared on compaction, chat turns only) and must
not become the gate's input.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from server import active_turn
from server.idle_compaction import SOURCE as COMPACTION_SOURCE
from server.invoke import InvocationResult, PlanUsage


@dataclass(frozen=True)
class ConversationReading:
    session_id: str
    context_tokens: int | None
    observed_at: float


class UsageState:
    def __init__(self) -> None:
        self.plan: PlanUsage | None = None
        self.conversation: ConversationReading | None = None

    def note_finished_turn(
        self,
        source: str | None,
        result: InvocationResult | None,
        *,
        now: float | None = None,
    ) -> None:
        """Called once per terminal turn, whatever its status.

        `result` is None when the invocation raised before producing one
        (timeout, unparseable stdout); there is then nothing to learn.
        """
        if result is None:
            return
        now = time.time() if now is None else now

        if result.plan_usage is not None:
            self.plan = result.plan_usage

        previous = self.conversation
        if (
            source == COMPACTION_SOURCE
            and not result.is_error
            and previous is not None
            and previous.session_id == result.session_id
        ):
            # The size just shrank, but a compaction turn can't say to what
            # (no stream, and its own usage describes the prefix it READ --
            # idle-compaction.md). Showing the pre-compaction number would be
            # the one misleading reading this sheet exists to avoid, at the
            # moment Mike is likeliest to look: right after going idle.
            # Unknown until the next chat turn measures it.
            self.conversation = ConversationReading(previous.session_id, None, now)
            return
        if source not in active_turn.ATTENDED_SOURCES or not result.session_id:
            return
        tokens = result.context_tokens
        if tokens is None and previous is not None and previous.session_id == result.session_id:
            # A turn that reported no size (failed, or no stream) does not
            # un-know the size from the last one: keep it, with its own
            # observed_at saying how old it is. A DIFFERENT session must not
            # inherit it -- that would pair a new conversation with the old
            # one's size (the pairing mistake idle_compaction guards against).
            return
        self.conversation = ConversationReading(result.session_id, tokens, now)

    def reset_for_tests(self) -> None:
        self.plan = None
        self.conversation = None


usage_state = UsageState()

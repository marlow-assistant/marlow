# Task — deferred `/code-review medium` findings from P1 (cron)

**Status:** finding 1 SHIPPED (merged 2026-08-27). Findings
2–4 remain open and deliberately deferred. **Priority:** cosmetic. Finding 1
was the only one of the four with a plausible production failure mode, and it
is now gone.

> **Do not self-assign 2–4.** "Deferred" here means Mike decided not to spend
> the scope, not that the work is unclaimed. This file is a record so the
> findings aren't re-derived — it is not a queue to pull from. Pick one up only
> when Mike asks for it by number, or when you are already editing that exact
> function for another reason and the fix is a line or two.

Moved here from `marlow-v2-START.md` on 2026-08-01. They sat in START's
"known gaps" list, which every session reads and no session acts on — real but
low-severity items are exactly what a mandatory-reading doc buries. They are
actionable, so they get a task doc.

All were flagged and left open on purpose rather than expanding P1's scope.
Take them individually; there is no reason to do them together.

## 1. `WorkQueue.enqueue()` was thread-unsafe — SHIPPED

Fixed 2026-08-27. The mechanism and its reasoning now live in the code:
`WorkQueue.start()`/`enqueue()` in `server/queue.py`, and the route comments on
`post_message` and `get_pending` in `server/app.py`. Regression cover is
`test_enqueue_rejects_a_foreign_thread` (`tests/test_queue.py`) and
`test_routes_that_mutate_queue_state_are_async` (`tests/test_app.py`).

The one thing worth keeping here, because it is about *this doc* rather than
about the code: **the fix sketched in this file was wrong.** It said to route
`Queue.put_nowait` through `call_soon_threadsafe`, mirroring
`server/permissions.py`. By the time anyone built it, `enqueue()` had grown to
also mutate `_turns`, the client-msg dedup index, retention state and pending
compactions — so scheduling only the final queue operation would have left
every preceding mutation racing the drain task, while looking fixed. What
landed instead keeps the whole mutation on the owner loop and makes the queue
reject foreign-loop callers outright.

Treat the remaining sketches below the same way: they record a *symptom*
observed on a particular day, not a design that has been re-checked since.

## 2. `post_job` raises the wrong error on a malformed 2xx body

`cron/entrypoint.py`'s `post_job`: malformed JSON in a 2xx response raises an
uncaught `JSONDecodeError`, and valid JSON with a non-object top level fails
later when `main()` calls `.get`. The two `except` clauses cover HTTP and
connection failures but not either shape on an otherwise-successful response.
Cosmetic unless it fires at 07:00, where a clean message beats a traceback in
the cron mail.

An older version of this finding also called a missing `"turn_id"` malformed.
That is no longer true: an attached job with no attended session legitimately
returns `{"status": "skipped", "reason": ...}`, and `main()` logs it as a successful
skip. A future fix should validate that the decoded value is an object, then
leave queued-versus-skipped interpretation to `main()`.

## 3. Dead code in `post_job`

`cron/entrypoint.py`'s `if resp.status // 100 != 2` branch can never fire:
`urllib`'s default opener already raises `HTTPError` for any non-2xx *before*
`urlopen()` returns. It can only be reached by a hand-built test double, never
a real server. Delete it, or keep it and note why — but it currently reads as
live error handling and isn't.

## 4. Test-helper duplication with a silent inconsistency

`tests/test_app.py`'s `_run_to_completion` duplicates `tests/test_queue.py`'s
identically-named helper, but compares `turn.status.value` (strings) instead of
`turn.status` (enum members). Harmless today; drifts the moment `TurnStatus`'s
value strings change.

Also: the two cron tests in `tests/test_app.py` repeat the same
`WorkQueue`-swap-and-`ASGITransport` setup verbatim. Weighed against that
file's own norm of repeating similar setup without a shared fixture — low
priority on its own, and not obviously worth a fixture.

## Landing it

All three remaining findings are trivial enough for `main` directly. Run
`.venv/bin/pytest -q` before committing — CI is off, so the `.githooks/pre-push`
hook is the only gate that runs, and a PR shows no checks at all rather than a
pass. If one of them ever grows past trivial, `docs/marlow-v2-workflow.md` has
the branch → local suite → `/code-review high` → merge path; the review step is
Mike's to trigger in chat.

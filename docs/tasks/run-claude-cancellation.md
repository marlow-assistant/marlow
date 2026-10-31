# Task — a cancelled `run_claude` leaves its CLI running

**Status:** parked 2026-10-31. Found while turning on pytest-xdist
(`test-suite-speed.md`). On a box the impact is close to nil, but it is real
off-box and it makes noise in the suite.

## The defect

`server/invoke.run_claude` kills and reaps the child in exactly one place: the
`asyncio.TimeoutError` branch around `wait_for(_communicate(...))`.
`_communicate`'s own `CancelledError` handler only cancels the two pipe reads.
Its comment says the caller "owns the kill-and-reap", and the caller does that
only for its timeout. When the task running `run_claude` is cancelled from
*outside*, the `CancelledError` unwinds through the `finally` with the child
still alive and never awaited.

## Where it shows

- **The suite, under load.** A queue test returns once its condition holds,
  while the drain task is still inside `run_claude` for a later turn (a
  follow-up, a compaction). `asyncio.run` cancels the drain task, the fake CLI
  exits a few ms later, and asyncio logs
  `Loop <...> that handles pid N is closed`. That was 0–3 times per `-n auto`
  run and never in a serial one. It has never failed a test.
- **Server shutdown mid-turn.** `_lifespan` → `work_queue.stop()` cancels the
  drain task the same way. On a box, `marlow-server.service` sets no `KillMode`,
  so systemd's default (`control-group`) signals the whole cgroup on stop, and
  the CLI dies with the server. Off systemd (a dev run of `python -m
  server.app`, Ctrl-C), `claude -p` can outlive the server. It keeps running
  and writes back `.credentials.json` with nothing reading its output. That is
  the stray concurrent invocation the one-queue rule exists to prevent.

## The fix, when it is picked up

In `run_claude`, catch `asyncio.CancelledError` alongside the timeout: kill the
child and re-raise. The reap is the delicate part. Awaiting `proc.wait()` while
being cancelled is the hazard `_communicate`'s comment warns about, so either
shield the wait with a short bound (`asyncio.shield` or `wait_for(...,
1)`), or kill without waiting and accept the child-watcher warning. Write the
test first: start `run_claude` against a fake CLI that sleeps, cancel the
task, and assert the pid is gone.

## Decision rule — build it when either is true

1. A stray `claude -p` is seen after a server stop, anywhere.
2. The suite's loop warnings turn into a failure, or start hiding a real
   message in the output.

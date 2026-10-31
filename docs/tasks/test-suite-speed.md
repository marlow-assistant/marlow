# Task — make the test suite faster

**Status: second round DONE 2026-10-31: pytest-xdist in the hook (`-n 4`) and
CI (`-n auto`).** At ~2,680 tests the serial suite reached 125–140s on the dev
Mac, and Mike felt the wait on every push, so the trigger below was met.

| | serial | parallel |
|---|---|---|
| dev Mac, 14 cores, Python 3.14 | 140.5s | ~49s at `-n 4` |
| CI, 4-core runner, Python 3.12 | 73.2s | 30.3s at `-n auto` |

Locally, 2 workers took 71s, and 4, 8 and 14 all took ~49s. At 4 workers about
1.2 cores are busy: the suite waits on subprocess spawns, not CPU. That is why
the hook uses 4 rather than `auto`.

**The flaky-sleep warning below still stands.** There are 79 fixed sleeps in
tests/, and they were not audited. Every run passed: 6 at 14 workers and 4 at
2–8 locally, plus 4 in CI. Rule: **a test that fails only under xdist gets
fixed (sleep → condition-wait), never covered by dropping `-n`.** A
flaky gate teaches `--no-verify`.

Only one thing broke under xdist. `test_activity_log.py` took parametrize IDs
from the per-process sandbox path, so workers collected different test names
and the run refused to start. Seen only under load: "Loop … that handles pid N
is closed" warnings, from `run-claude-cancellation.md`.

The first round's write-up follows unchanged.

**DONE 2026-08-22. 71.9s → 19.5s (3.7x), 720 tests, one path constant.**
The measurement step contradicted the entire theory of the previous version of
this doc, which is why that version told you to believe the measurement. Kept
as a worked example of exactly that.

## What it actually was

`pytest --durations=25` put **every** one of the top 25 slots in
`tests/test_bash_policy.py` and `tests/test_file_policy.py` — the two files the
old doc waved off as "likely cheap: pure functions over strings and paths." The
git-fixture and subprocess-spawn suspects it named did not appear at all.

The tell was the CPU ratio: **72s wall at 14% CPU.** The suite was not computing,
it was blocked.

Cause, in one line: the policy suites used the box's real `/home/marlow/...`
as their working-tree constant, policy decisions call `Path.resolve()`, and
**on macOS `/home` is an autofs automount point** (`map auto_home on
/System/Volumes/Data/home`). `/home` itself stats in microseconds; anything
*under* it costs ~7ms while the automounter tries and fails to mount a home
directory that is not there. One `vet_bash_command()` call makes ~570 `lstat`
calls, so a single policy assertion took **1.2 seconds**.

Measured, on this Mac:

| | before | after |
|---|---|---|
| `test_bash_policy.py` + `test_file_policy.py` | 53.1s | 0.35s |
| full suite (720 tests, same tree, stash/pop) | 71.9s | 19.5s |

## The fix

`tests/conftest.py` now exports `FAKE_HOME = "/opt/marlow"` and `FAKE_TREE`,
with the reasoning next to them. The policy suites and `test_boundaries.py`
import those instead of writing box paths inline.

Two things deliberately left alone:

- **`tests/test_marlow_settings.py` keeps its `/home/marlow` literals.** They are
  compared against the shipped `deploy/marlow-settings.json`, so they are
  quoting a file rather than naming a directory. Nothing there is resolved, so
  it costs nothing. There is a note in the file saying so.
- **The attachment-path literals** in `test_invoke.py`, `test_queue.py` and
  `test_idle_compaction.py`. They are opaque strings asserted verbatim in
  output, never fed to `Path.resolve()`, so they pay nothing — and those files
  are in the way of `turn-activity-streaming.md`.

**Scope note: this is a macOS-only artifact.** On the box the same `lstat` is a
fast `ENOENT`, so this was never production latency and Linux CI would never
have shown it. The win is the Mac dev loop, which is where the agents run the
suite dozens of times a session — the argument the old doc made for why this
was worth doing at all, and that argument held up.

There is a second, smaller benefit: on the box `/home/marlow/marlow-identity`
is a *real* directory, so a suite run there had been resolving these against
live paths. The stand-in makes them hermetic — the same trap
`test_bash_policy.py`'s `_secret` helper and
`test_agent_config_inside_the_tree_is_not_vetted` already warn about.

## Verification

- 720 passed before and after; no test skipped or removed.
- The new constants were checked to still straddle the boundary in both
  directions (in-tree `allow`, out-of-tree `prompt`, `.env` secret, `plan.md`
  not) — a path-constant change is exactly the kind that can quietly make an
  assertion vacuous.
- Mutation check: stubbing `inside_working_tree()` to `return True` turns **29**
  of these tests red. They still bite.

## What is left, and the rule for picking it up

The profile is now flat — nothing above 0.30s, and the remaining ~19.5s is real
work spread thin. **Do not go further without a new trigger.**

- **`pytest-xdist`** — still not installed, and still the wrong reflex. The old
  doc's warning survives intact and is now the *only* live content in it: the 25
  fixed sleeps that cost ~1s of runtime are timing assumptions that break under
  8–10 competing workers. On a repo where the pre-push hook is the only gate, a
  flaky gate is worse than a slow one, because it teaches whoever hits it to
  reach for `--no-verify`. If this is ever picked up, convert the fixed sleeps
  to condition-waits first.
- **Trigger for reopening:** a session observably narrowing its `pytest`
  invocation to avoid the cost. At 19.5s that should not happen, which is the
  actual goal — the second count was only ever a proxy for it.

## Reference

`tests/conftest.py` (`FAKE_HOME` / `FAKE_TREE` and the reasoning),
`.githooks/pre-push` (its cost comment was updated 70s → 20s with this change),
`docs/marlow-v2-workflow.md` (CI is off; this hook is the only gate).

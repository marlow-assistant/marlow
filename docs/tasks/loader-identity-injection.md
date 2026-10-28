# Task — identity/memory loader in `server/invoke.py`

**Status: BUILT and MERGED.** `server/identity.py` (`compose_identity_context`)
is wired into `server/invoke.py:112` and injected via `--append-system-prompt`
(`invoke.py:123`); `tests/test_identity.py` covers it. Corrected 2026-08-01;
this line said "not started" long after it shipped. Hardened since by
`docs/tasks/cache-friendly-identity-injection.md` (per-session freeze). The
remaining *unbuilt* piece is the memory **index** mode — `identity.py` implements
only `inject_all`, and `index_only` is spec'd in
`docs/tasks/memory-index-split.md`, which is genuinely not started. The rest of
this doc is the spec it was built from, kept for the reasoning.

## Why this exists

Marlow's hand-authored identity lives in the working dir
(`MARLOW_WORKING_DIR`, default `~/marlow-identity`): `SOUL.md`, `USER.md`,
`CLAUDE.md`, and `memory/`. On v1, recall was flaky because the *instruction* to
"read SOUL/USER/memory" is discretionary — the model has to decide to `Read`,
and that loses to answer-momentum. The fix is to stop making it a choice: the
harness **injects** the identity into every `claude -p` invocation so Marlow
begins each turn already holding it.

Design axis (decided 2026-07-23, full rationale in
`marlow-v2-START.md`): **inject what is bounded and almost-always
relevant; retrieve what is unbounded and only sometimes relevant.**
- **Inject:** `SOUL.md` + `USER.md` + `CLAUDE.md` + the memory **index** (one
  line per entry — the routing table).
- **Retrieve on demand:** individual memory *entries* (unbounded; always-injecting
  all of them blows context and dilutes attention).
- **Simplification for now:** while memory is tiny, inject *all* of it and skip
  the index/retrieve split — but write the loader so the split is a later config
  change, not a rewrite.

## The open questions — both settled, kept for the reasoning

*(Resolved during the build. `CLAUDE.md` is not injected — see `invoke.py`'s
docstring; the mechanism is `--append-system-prompt`. Retained because the
constraint in the last paragraph still binds any future change here.)*

`server/invoke.py:run_claude` builds `args = [claude_bin, "-p", prompt, ...]` and
runs with `cwd = settings.working_dir`. Two things to establish before coding:

1. **Does the working-dir `CLAUDE.md` already auto-load?** Claude Code reads a
   `CLAUDE.md` from the cwd. If so, `CLAUDE.md` must NOT be injected again (double
   context). Confirm, and if it auto-loads, the loader injects only
   `SOUL.md` + `USER.md` + memory. If it does *not* auto-load under `claude -p`
   in a scrubbed env, inject it too.
2. **What is the injection mechanism?** Leading candidate: `--append-system-prompt
   "<composed text>"` (keeps the user prompt clean). Alternative: prepend to the
   `-p` prompt. Pick based on the **validated CLI contract** in
   `docs/marlow-v2-gate1-results.md` — do not guess at a flag that wasn't
   exercised there.

**Hard constraint on verification:** you CANNOT test real `claude -p` flag
behaviour from inside your own agent session — a nested `claude -p` inherits the
harness env (`CLAUDE_CODE_*` etc.) and gives false results, which is the exact
thing `invoke.py`'s scrubbed env exists to prevent. If you need to confirm
`--append-system-prompt` actually lands in context, write a short clean-shell
script and hand it to Mike to run, the same way gate #1 was validated. State
your assumption explicitly if you proceed on documented behaviour.

## As built

Implementation, test plan, and landing steps have been retired — this shipped and was hardened by the per-session freeze. The code is the spec now: `server/identity.py` (`compose_identity_context`), injected at `server/invoke.py:112` via `--append-system-prompt`, and `tests/test_identity.py`.


## Fail loud, don't silently omit

A missing or unreadable identity file is a real problem — a Marlow running
without its SOUL is a bug, not a degraded-but-fine state. Do **not** silently
skip a missing file. Raise (or log at error and surface) so it's visible. Decide
and document: is a missing `SOUL.md` fatal to the invocation, or a loud warning?
(Recommendation: fatal for SOUL/USER/CLAUDE, warning for an empty `memory/`.)


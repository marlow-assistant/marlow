# Task — accept a per-turn `effort` from the phone

**Status: SHIPPED, MERGED AND DEPLOYED 2026-08-27**. Local suite: 839
passed; `/security-review` found nothing. Kept because `server/app.py`,
`server/queue.py` and `server/invoke.py` cite this file at the lines that
implement the allowlist — the *why* behind returning the matched tuple member
instead of the request string lives here.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

This was the **server half of piece 2** of `docs/tasks/model-and-effort-config.md`
(its piece 2 section and SECURITY NOTE hold the reasoning). The phone sends
`effort` on every message; the value travels app.py → queue → `Turn` →
`run_claude` → `_invoke_once` → argv.

**Implementor retrospective, worth keeping.** The spec was a good fit — a request
field, an allowlist, four layers of plumbing, no UI — and it came back with a
correctness point the spec never made: coalescing. Queued turns at different
effort levels must not merge, or a turn silently runs at a level nobody picked.
That gap was found by reading the code it was changing rather than by following
orders.

## Decisions

- **Allowlist, and return the matched constant, never the request string.**
  `_ALLOWED_EFFORTS = ("low", "medium", "high")`; `xhigh` and `max` are excluded
  because they only add latency on this workload. The arg builder already emitted
  `--effort={effort}` as one token (the right shape after the `session_hint`
  flag-injection hole), but "it's one token" is not the same guarantee as "it is
  one of three known-good strings". A hostile value (`"--settings=x"`,
  `"low --model=opus"`, `""`, `"LOW"`, a 4KB string) must yield the default and
  never appear in argv.
- **Optional on the request, warn-and-drop when invalid.** An older client must
  keep working, and a bad effort from a client bug should degrade to the default
  rather than cost Mike the message he sent (the timezone field's precedent, not
  the image field's hard 400).
- **A per-turn effort wins for `source == "ios"` only.** Cron ignores it
  entirely, since nothing client-supplied reaches cron.
- **Compaction excluded.** Piece 3 then carried a rule that compaction should run
  at "the session's current level"; it was kept out of this branch because it was
  being settled by measurement, and was struck 2026-08-27 (Probe 3 in
  `model-and-effort-config.md`). `source == "compaction"` keeps `settings.effort`.
- **Model is display-only.** A model switch pays a full cold prefix read where an
  effort switch does not, so there is no model toggle. `/health` reports the
  configured model (and effort default) so the header shows what Marlow is
  running, not a hardcoded label that can drift from the server.
- **No server-side persistence of effort.** The phone sends a stable value on
  every message; `server/session_state.py` stays the two-fact file its docstring
  promises.
- **Log requested vs applied**, so the setting is observable from outside.
- **Scope:** no iOS work in this branch (Mike, 2026-08-23), and no `claude -p`
  probes from the building session; probe-dependent questions are flagged, not
  measured.

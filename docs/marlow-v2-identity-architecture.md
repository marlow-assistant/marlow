# Marlow v2 — Identity architecture

**Status: decided 2026-07-23.** This is the design record for *what Marlow's
identity is made of, where each piece lives, and how it reaches the box.* Split
out of `marlow-v2-START.md` on 2026-07-29 — START keeps a one-line
pointer here.

Read this if you touch `identity/`, `server/identity.py`,
`deploy/seed-identity.sh`, or anything to do with open-sourcing the repo.

---

## Two orthogonal splits

**Tier (static vs. living).** `SOUL.md`/`CLAUDE.md`/`USER.md` are **hand-crafted
for v2, inspired by v1 but not beholden to it** — authored fresh with v1 open as
reference, NOT migrated-then-edited (v1's files are tuned for the tmux+Discord
harness; v2's is a different animal). Per-file gradient: `CLAUDE.md` is mostly
rewritten (operating instructions, v2 harness), `SOUL.md`/`USER.md` mostly carry
(voice, user facts are harness-independent). `memory/` + in-flight projects are
the **living tier — synced repeatedly** from v1 up to cutover, making cutover a
low-drama `memory/` sync.

**Location (public software vs. private identity)** — driven by a possible
open-source future for `marlow-server`. Git history is forever, so **no personal
content may ever enter `marlow-server`'s git.** Therefore:

- **Mike's real v2 identity lives in a separate private repo, `marlow-identity`**
  (created 2026-07-23) — `SOUL.md`/`USER.md`/`CLAUDE.md` hand-crafted there with
  Mike's specifics ("Mike" everywhere is *correct* here), seeded onto the box,
  **never pushed to marlow-server**. This is what `seed-identity.sh --from` points
  at.
- **The public/private seam runs *through* `CLAUDE.md`, not around it.**
  `SOUL.md`/`USER.md` are ~fully personal — de-personalized they're just empty
  placeholders, so they have no meaningful public form. `CLAUDE.md` is the one
  file with real reuse value, but it too splits: **harness mechanics** (startup
  reads, permission-round-trip behaviour, memory-commit, scheduling/push —
  reusable) vs. **personal preferences** (languages, timezone, peer-tone,
  specific crons — private). Write the mechanics sections cleanly so they lift
  out easily later.
- **Do NOT maintain a parallel "vanilla" template now.** `marlow-server/identity/`
  stays the existing stub. A second copy would drift every time the harness
  changes. The open-source template is produced as a **single de-personalization
  pass at open-source time**, from the then-current private `CLAUDE.md` (mechanics
  kept, preferences → placeholders; `SOUL.md`/`USER.md` → blank scaffolds).

## Before any open-sourcing

- **(a) The de-personalization pass** — the one piece of pre-open-source work
  still outstanding. Current-tree only.

## Tooling follow-up (not yet done)

Post-first-light: split `seed-identity.sh` into "seed static identity once" vs.
"sync living files repeatedly," and wire a read-only deploy key for the private
`marlow-identity` repo so the box can clone it (same pattern as the
marlow-server deploy key).

## Loading — how identity reaches the model

**✅ BUILT and merged 2026-07-23/24.**
`server/identity.py` composes SOUL+USER+memory and `server/invoke.py` injects it
via `--append-system-prompt` on **every** call — the appended block is *not*
carried by `--resume` (gate1-results §8), so resupplying it each turn is
required, not redundant; drop it and identity vanishes mid-session. Spec:
**`docs/tasks/loader-identity-injection.md`**. Memory mode is `inject_all` only;
the future `index_only` split is a config change, not a rewrite
(`docs/tasks/memory-index-split.md`).

**Injection interacts with the prompt cache in ways that constrain this design.**
The composed block is ~58K tokens and sits at the *front* of the cache prefix, so
any mid-session change to it invalidates the block plus all accumulated
conversation history. The governing rule:

> **The appended system prompt carries stable content only. Anything volatile,
> retrieved, or recency-sensitive enters through conversation history (as tool
> results), which is append-only and caches incrementally.**

Measurements, the no-clean-cliff finding, and the workload targets that follow
from them: **`docs/marlow-v2-gate1-results.md` §8–§8c**. Read it before
changing anything about what goes into the injected block.

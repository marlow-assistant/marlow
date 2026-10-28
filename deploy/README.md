# Deploying marlow-server

> **Installing on a new box? Use [`install.sh`](install.sh); the steps are in
> [`INSTALL.md`](INSTALL.md).** This README is the long-form reference for how
> a box is put together: why each step is the way it is, and the optional
> pieces install.sh deliberately leaves out (identity push, dead-man's switch,
> egress allowlist). When a step here changes, check whether install.sh does
> the same thing.

**Target host: `marlow.<tailnet>.ts.net`** — the tailnet name, which is what
docs, config, and the iOS app should always refer to. The box behind it is an
Ubuntu 24.04 machine, but **never hardcode its OS hostname or tailnet IP** — the
point of the role name is that the hardware underneath can be replaced without
touching anything else.

Development happens on a *different* machine; code is pushed to `origin/main`
from there and pulled here by §10.

## The two-location model (why this looks the way it does)

A marlow-server host holds **two separate things** that must never share one git
history:

1. **The software** — this repo (`marlow-server`), cloned to
   `/home/marlow/marlow-server`. Updated by fast-forward pull (`update.sh`). It
   **never diverges**: nothing writes into it at runtime, so it stays cleanly
   fast-forwardable forever.
2. **The identity / working dir** — `$MARLOW_WORKING_DIR` (default
   `/home/marlow/marlow-identity`). Seeded **once** (`seed-identity.sh`), then it
   **diverges freely** — the assistant writes to `memory/` every conversation.
   Its git history is private to the box and is never pushed to the software
   repo. `update.sh` never touches it.

Keeping these apart is deliberate. A single git checkout cannot be both
fast-forward-updatable (software) and independently diverging (a living brain) —
one branch can't be both. Splitting the directories is what makes clean updates
and a living memory coexist. `server/config.py` already separates
`MARLOW_WORKING_DIR` from where the code lives; here we just put them in
different places on disk.

## 0. Give the host its role name on the tailnet

Do this **before** anything that bakes in a hostname — especially before issuing
a TLS cert or building an app that pins an endpoint.

```sh
sudo tailscale set --hostname=marlow    # on the target host
```

This changes the **Tailscale node name only**; the OS hostname is left alone
(it honestly describes the hardware, and changing it is invasive for no gain).
MagicDNS is enabled on this tailnet, so the host is then reachable tailnet-wide
as `marlow.<tailnet>.ts.net` — and, critically, that name survives the laptop
being replaced.

## 1. Create the user

```sh
sudo useradd --system --create-home --shell /bin/bash marlow
# --shell /bin/bash, not nologin: marlow needs an interactive login once for
# `claude` auth (§3), and for update.sh's git/claude commands. It only ever
# gets there via `sudo -u marlow`, never a network-facing login service.
```

## 2. Clone the software + install

Use `sudo -iu marlow` (login shell), **not** `sudo -u marlow -H` — the OAuth
flow in §3 needs a real login shell to behave. **When a command contains `~`,
wrap it in `bash -c '...'`** so the `~` expands as *marlow*: `sudo -iu marlow
~/foo` expands `~` in the *caller's* shell before sudo runs, silently pointing at
the wrong home (a trap we hit for real, twice).

**The repo is private**, so the box needs its own read-only credentials — a
per-repo **deploy key** (an SSH key registered to this one repo), not an
account-wide token. Generate one as `marlow` and register its public half
(GitHub → repo → Settings → Deploy keys, **read-only** — or from a machine with
`gh`: `gh repo deploy-key add <pubkey> --repo marlow-assistant/marlow`):

```sh
sudo -iu marlow bash -c '
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  test -f ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C "marlow-box-deploy"
  ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null
  cat ~/.ssh/id_ed25519.pub   # <- register this, read-only
'
```

Then clone over the **SSH** remote (the deploy key is an SSH key — an HTTPS clone
won't use it), build the venv, install deps. Set marlow's git identity in the
same breath: the §4 seed makes a commit, and a fresh system user has none
(`git commit` aborts with "Author identity unknown"). (`seed-identity.sh` also
passes a fallback identity now, but configuring it here is what the *running*
service will use for future memory commits.)

```sh
sudo -iu marlow bash -c '
  git config --global user.name "Marlow"
  git config --global user.email "marlow@marlow.<tailnet>.ts.net"
  git clone git@github.com:marlow-assistant/marlow.git ~/marlow-server
  cd ~/marlow-server
  python3 -m venv ~/.venvs/marlow-server
  ~/.venvs/marlow-server/bin/pip install -r server/requirements.txt
'
```

`marlow` also needs its **own** copy of the Claude CLI — an admin user's
install under their `~/.local/bin` is not on this user's PATH, and must not be
shared anyway:

```sh
sudo -iu marlow bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
sudo -iu marlow bash -c 'echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc'
sudo -iu marlow bash -c 'claude --version'
```

The `.bashrc` line is for interactive convenience only. **The service does not
depend on it** — `marlow-server.service` sets `MARLOW_CLAUDE_BIN` to the
absolute path, because systemd never reads shell rc files and its default PATH
excludes `~/.local/bin`.

## 3. Log in to Claude

```sh
sudo -iu marlow    # then, at the prompt:
claude             # interactive, one-time; writes ~/.claude/.credentials.json
```

A second, independent OAuth grant under the same Pro account — its own token
file, no cross-user write-back. (`claude -p` self-refreshes the access token
headlessly thereafter. Interactive re-auth is about monthly: the box's grants
ran 28–30 days from each login (Aug 18, Sep 14, Oct 12, 2026), and use
doesn't extend them. Gate 1's "~weekly" came from a single ~9-day sample
(`docs/marlow-v2-gate1-results.md`). `claude setup-token`'s one-year token
doesn't replace it: it carries no claude.ai connectors
(`docs/tasks/marlow-for-taylor.md`).)

**Verified end-to-end on a box, 2026-07-21.** The headless-OAuth risk
we expected (browser callback redirecting to `localhost` on the *wrong* machine)
did **not** materialise — the login flow completed over SSH without a port-forward
or manual code paste. Post-login checks, all of which should pass:

```sh
sudo ls -la /home/marlow/.claude/.credentials.json    # exists, 0600, marlow-owned
sudo -iu marlow bash -c 'claude -p "reply with the single word: ok" </dev/null'
ls -la ~/.claude/.credentials.json                    # your own grant, if any: UNTOUCHED
```

The middle one matters most: it is the exact call shape `server/invoke.py` uses,
`</dev/null` stdin included. Run it before moving on — if it fails, nothing
downstream can work.

Two Marlows on one Pro subscription share **one rate limit** (separate token
files prevent credential races, not quota contention).

## 4. Seed an identity into the working dir (one-time)

marlow-server ships **no** personality. Choose one:

```sh
# Blank-slate Marlow (fill SOUL.md/CLAUDE.md/USER.md in ~/marlow-identity after):
sudo -iu marlow bash -c '~/marlow-server/deploy/seed-identity.sh --template'

# OR seed from an existing assistant's files (SOUL.md, CLAUDE.md, USER.md, memory/):
sudo -iu marlow bash -c '~/marlow-server/deploy/seed-identity.sh --from ~/seed-src'
```

Point `--from` at a snapshot, never at a live tree something is still writing
to: reading one risks catching a half-written `memory/` file, and `marlow`
usually can't read another user's files anyway. Copy the four pieces via root
into a marlow-owned dir, seed from that, then remove it:

```sh
sudo mkdir -p /home/marlow/seed-src
sudo cp -a <source>/{SOUL.md,CLAUDE.md,USER.md,memory} /home/marlow/seed-src/
sudo chown -R marlow:marlow /home/marlow/seed-src
sudo -iu marlow bash -c '~/marlow-server/deploy/seed-identity.sh --from ~/seed-src'
sudo rm -rf /home/marlow/seed-src
```

To start over, re-seed with `--force` (or `sudo rm -rf ~marlow/marlow-identity`
first). Either discards the assistant's memory.

This copies `SOUL.md` / `CLAUDE.md` / `USER.md` / `memory/` into
`~/marlow-identity` and `git init`s it as a private, diverging history. It
refuses to clobber an already-seeded dir (guarding the box's diverged memory).

## 5. Config via env — see `server/config.py` for the full override list

Defaults are correct for this layout. `MARLOW_WORKING_DIR` defaults to
`~/marlow-identity`, which is the seed target from §4 — you shouldn't need to
set it unless you deviate.

## 6. Install the systemd unit

First provision the runtime dirs. The unit points `MARLOW_DATA_DIR` /
`MARLOW_LOG_DIR` at `/home/marlow/.marlow/{data,logs}`, which `config.py`
`mkdir`s at startup — but §8b created `/home/marlow/.marlow` as **root**, so
`marlow` can't create anything under it and the service would crash with
`PermissionError` before serving. (`ReadWritePaths` governs the mount namespace,
not Unix ownership.) Hand `.marlow` to `marlow` and pre-create the dirs:

```sh
sudo mkdir -p /home/marlow/.marlow/data /home/marlow/.marlow/logs
sudo chown -R marlow:marlow /home/marlow/.marlow      # tls/ stays marlow-owned too
sudo chmod 700 /home/marlow/.marlow/data /home/marlow/.marlow/logs

sudo cp ~marlow/marlow-server/deploy/marlow-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now marlow-server
```

Verify: `systemctl status marlow-server` shows `active (running)`;
`sudo ss -tlnp | grep 8443` shows the tailnet listener (tailnet-IP:8443); `sudo
curl -s --unix-socket ~marlow/.marlow/run/internal.sock http://localhost/health`
answers on the internal socket (owner-only, not loopback TCP, since 2026-10-16:
`internal_socket_path` in server/config.py); and `curl -i https://marlow.<tailnet>.ts.net:8443/health` answers
`401` over a **valid** cert (no `-k`, which also proves §8b end-to-end); `/health` needs a device
token on the public listener since funnel-ingress.md step 4.

The unit deliberately keeps `NoNewPrivileges=true`. Because that suppresses
`crontab`'s setgid transition, it also grants the service process the existing
`crontab` supplementary group up front. `ProtectSystem=strict` independently
makes the cron spool read-only, so the unit pairs that group with an exact
`ReadWritePaths=/var/spool/cron/crontabs` exception. Those are the narrow
permissions the harness needs to install `marlow`'s own schedule; do not remove
either half or replace them by disabling the service hardening globally.

## 6a. Scheduled (cron) work

`docs/tasks/scheduled-work.md` has the design and the "why real cron, not a
scheduler" reasoning. This is the mechanical convention.

**`cron/entrypoint.py` must be root-owned and not writable by `marlow`** — it's
the only thing a crontab line is allowed to invoke, so there's no per-job shell
script for Marlow to quietly rewrite into an escape. `deploy/update.sh` syncs
this copy on every run (`mkdir -p /opt/marlow/cron` + `install -o root -g root
-m 755 cron/entrypoint.py /opt/marlow/cron/entrypoint.py`, idempotent), so a
`cron/entrypoint.py` change ships to the root-owned copy automatically on the
next `update.sh` — no separate manual step to remember. `update.sh` runs as
root already (§10 — invoked via `sudo` by an admin account, never as
`marlow`, which has no sudo access), so this needs no separate elevation.

For a one-off manual install (e.g. first-time setup, before `update.sh` has
ever run), the same command works standalone:

```sh
sudo mkdir -p /opt/marlow/cron
sudo install -o root -g root -m 755 \
  ~marlow/marlow-server/cron/entrypoint.py /opt/marlow/cron/entrypoint.py
```

**The renderer only takes over a crontab it fully understands.** It must
contain nothing but its own two line forms. `deploy/update.sh` enables
`marlow-identity-push.timer` and deletes the pusher's old line from any crontab
that still holds it; the store itself,
`cron/schedules.json`, comes from install.sh's template seed. Until the crontab
is clean, `install()` logs the offending lines and refuses — which is
deliberate: the alternative is the morning scan and any promoted script
silently disappearing the first time Marlow sets a reminder.

A promoted script (§6b) can never be a store entry, because the store has no
command field. It gets its own root-owned systemd timer instead.

**Do not edit the `marlow` user's crontab by hand.** `server/schedules.py` owns
the whole thing. At server startup and after every turn it validates
`$MARLOW_WORKING_DIR/cron/schedules.json`, renders the complete crontab, and
installs it as the current `marlow` user with `crontab <file>`. A hand edit will
be clobbered on the next successful sync. Invalid input is all-or-nothing: it
logs the rejection and leaves the previously installed crontab untouched.

The store carries only intent, never a command. IDs and job names are
allowlisted, `at` is a local minute, `cron` is a five-field expression, and
`timezone` is an IANA name.

**The times in the store are Mike's, not the box's.** Debian's cron has no
`CRON_TZ` (it is a Vixie/RH feature; `cron 3.0pl1-184ubuntu2` accepts the line
as an ordinary variable and ignores it), so `server/schedules.py` converts each
entry into **system-local time itself** and the installed crontab carries no
zone at all. Do not read a crontab line as the time Mike asked for — a 07:30
St John's reminder is `0 4 * * *` on an Edmonton box, and one store expression
can render as several lines when the offset is not a whole hour.

```json
{
  "timezone": "America/Halifax",
  "entries": [
    {"id": "costco", "at": "2026-08-28T14:00", "notify": "Go to Costco"},
    {"id": "rebates", "at": "2026-08-30T21:00", "prompt": "Research heat pump rebates"},
    {"id": "morning", "cron": "45 7 * * *", "job": "morning-scan"}
  ]
}
```

The protected renderer supplies `MAILTO`, the server's configured `MARLOW_*`
environment, and every executable byte. It emits only
`entrypoint.py <job-name>` for approved prompt files and
`entrypoint.py --entry <id>` for inline prompts/reminders. The latter looks up
the payload in the store, so free text never reaches cron's `/bin/sh` command
line. A fired one-off prunes itself; a stale line whose id is gone exits zero.

For a `job` entry, `entrypoint.py` reads
`$MARLOW_WORKING_DIR/cron/<job-name>.md` for the prompt text (defaults to
`~/marlow-identity/cron/<job-name>.md`, matching `MARLOW_WORKING_DIR`'s own
default — no override needed unless §5 already set one) and POSTs it to the
loopback-only `/internal/cron/message`. **Job prompt files are personal
content and belong in the identity working dir, never in this repo** — see
`docs/marlow-v2-identity-architecture.md`. Authoring them is not a mechanical
deploy step; that's Mike's own writing, done directly on the box or synced in
via the identity repo.

Prefer **fewer, coarser jobs**, or several packed inside one window under
240s — every invocation re-injects the ~58K-token identity block and the
prompt cache does not reliably survive a gap past that (gate1-results §8b).

### 6b. Promoted scripts — confinement rule retained, not renderer input

Marlow authors shell scripts, and that is deliberately kept useful. What is
**not** allowed is a crontab line pointing into Marlow's own working tree:
`docs/tasks/scheduled-work.md` closed that on purpose, because a script Marlow
can rewrite and cron then runs is a daily execution surface with no hook, no
`boundaries.py`, and no bwrap namespace. Added 2026-08-08, when the first such
line went in (a sync script under `~marlow/marlow-identity/scripts/`).

The rule separates two privileges that otherwise fuse into one:

- **Authoring is cheap.** Marlow writes and iterates on
  `$MARLOW_WORKING_DIR/scripts/*.sh` freely. `file_policy` auto-allows writes
  only under `memory/`, so a write there raises a phone prompt when attended and
  is denied outright when not — but either way, nothing has *run*.
- **Promotion to unattended execution is the expensive one**, and it is a
  deliberate root act, never a side effect of an approved file write:

```sh
sudo mkdir -p /opt/marlow/bin
sudo install -o root -g root -m 755 \
  ~marlow/marlow-identity/scripts/<name>.sh /opt/marlow/bin/<name>.sh
```

The promoted copy is frozen: Marlow edits the in-tree copy, Mike reads the
diff, and re-promotes. That is where the diff actually gets read — the
alternative is thumbing Allow on every edit forever with no signal marking the
one that matters. Friction lands proportional to blast radius.

The schedules renderer deliberately has no model-supplied command/script
field, so a promoted script cannot be added to its crontab with a hand edit —
that edit would be clobbered. If a promoted script needs scheduling, give it a
separate root-owned systemd timer or add a fixed harness-owned line to the
renderer in reviewed software. Never add a command field to `schedules.json`.

The trade accepted with it: Marlow can no longer *read* the promoted copy
without a prompt (out-of-tree reads aren't vetted), same as `entrypoint.py`. The
in-tree copy is the one it reasons about.

**Carry this — a promoted script runs OUTSIDE bwrap.** `invoke.py` spawns
`claude -p` into a namespace where `boundaries.SECRET_PATHS` are tmpfs and
genuinely absent, which is why `server/bash_policy.py` describes itself as
latency management rather than the boundary. Cron invokes a promoted script
directly, with none of that: `~/.ssh` and the credentials file are plainly
readable to it. The harness is therefore *stricter with Marlow's own shell calls
than with the scripts it schedules*, which is backwards, and it is the reason to
keep the promoted list short. Closing it is
`docs/tasks/cron-script-confinement.md`; least-privilege credentials inside each
script are the interim mitigation.

Marlow introspects and changes its schedule through `cron/schedules.json`, not
`crontab -l/-e`. `file_policy` auto-allows writes to that one named file; job
prompt files remain outside the writable subtree, so authoring a new
`cron/<job>.md` still needs Mike's attended approval. The harness alone invokes
`crontab`, outside the agent sandbox.

### 6c. The off switch

Deleting `$MARLOW_WORKING_DIR/cron/schedules.json` turns the whole feature off:
`load()` returns `None`, the renderer no-ops, and the installed crontab is left
exactly as it is. That is the rollback lever, and it needs no deploy. The
converse buys nothing: restoring a crontab by hand while the store is still
present gets re-rendered over on the next turn.

Before trusting the renderer on a new host, check that `crontab -l`
round-trips byte-for-byte, which `install()`'s equality check assumes:

```sh
ssh marlow@marlow 'crontab -l > /tmp/ct && crontab /tmp/ct && crontab -l | diff -u /tmp/ct - && echo ROUNDTRIP-OK'
```

A diff instead means the renderer will reinstall on every turn. It is a
property of the host's cron, not of this repo, so each new host answers it
again.

## 7. Watchdog (same host, self-safe)

```sh
sudo cp ~marlow/marlow-server/deploy/watchdog.sh /usr/local/bin/marlow-watchdog.sh
sudo chmod +x /usr/local/bin/marlow-watchdog.sh
# then a cron/systemd-timer entry, e.g. every 2 min — see watchdog.sh header
```

## 7a. Identity push — get the brain off this box

`server/identity_git.py` commits the working dir after every turn, but nothing
moves those commits anywhere until this is installed. Piece 4 of
`docs/tasks/marlow-working-dir-git-policy.md`; the script is
`deploy/identity-push.py`.

**Two halves, and both are required.** The hourly push below is the convenient
one. `scripts/backup-identity-bundle.sh`, run **from Mike's Mac**, is the
load-bearing one: Marlow holds a deploy key with write access, so the agent can
destroy the GitHub side of its own backup. A bundle on the Mac is a file the box
cannot reach and holds no credential for. Installing the cron and skipping the
bundle leaves the brain with one deletable copy.

**Runs as `marlow`, from a root-owned systemd timer.** It was built as a root
cron with the key outside `/home/marlow` and reverted after
`/security-review`; **don't re-privilege it** — piece 4 of the task doc has the
mechanisms and the decision rule.

`update.sh` syncs the installed copy on every run (`install -o root -g root -m
755` to `/opt/marlow/bin/identity-push.py`), so a code change ships
automatically. **Root-owned, marlow-executed** — that part of the root design is
kept, and it is §6b's rule: a crontab line pointing into Marlow's own working tree
would let the agent rewrite tomorrow's scheduled job. For a first-time install,
the same command standalone:

```sh
sudo mkdir -p /opt/marlow/bin
sudo install -o root -g root -m 755 \
  ~marlow/marlow-server/deploy/identity-push.py /opt/marlow/bin/identity-push.py
```

### One-time: the deploy key

```sh
sudo -u marlow ssh-keygen -t ed25519 -N "" -C "marlow-identity-pusher" \
  -f ~marlow/.ssh/identity-push
sudo -u marlow cat ~marlow/.ssh/identity-push.pub
```

Add that public key to the **identity** repo (not `marlow-server`) under Settings
→ Deploy keys, **with write access ticked**. Deploy keys are per-repository, and
read-only is the default — a read-only key fails at push time, not at setup.

> If you already generated the root-owned key at `/etc/marlow/identity-push`
> under the previous design, it is the wrong key now — `marlow` cannot read it.
> Generate the one above, then `sudo rm -rf /etc/marlow` and **delete the old
> deploy key entry on GitHub**. GitHub rejects a key already registered on another
> repo, so a stale entry will bite the next time you add one.

The working dir's `origin` must be **SSH**, not HTTPS; a deploy key is an SSH
credential and an `https://` remote will ignore it entirely:

```sh
sudo -u marlow git -C ~marlow/marlow-identity remote set-url origin \
  git@github.com:mikemason/marlow-identity.git
```

### Scheduling the push

The §6a renderer owns `marlow`'s entire crontab and intentionally cannot emit a
model-supplied promoted-script command. The reviewed
`marlow-identity-push.service` / `.timer` units replace the old hourly `:17`
crontab line. `deploy/update.sh` installs and enables the timer **before** it
restarts `marlow-server`; startup schedule sync may then remove the old line
without creating a gap in off-box backups. Do not use `crontab -e`: the next
schedule sync will correctly remove the hand edit.

For a first install without `update.sh`:

```sh
sudo install -o root -g root -m 644 \
  ~marlow/marlow-server/deploy/marlow-identity-push.{service,timer} \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now marlow-identity-push.timer
```

Verify the migration before allowing the schedule renderer to replace the old
crontab: `systemctl list-timers marlow-identity-push.timer` must show the next
`:17` run. `systemctl status marlow-identity-push.service` shows the last run.

The shipped unit sets `MARLOW_WORKING_DIR` explicitly so the deployment remains
self-describing. If §5 changes any path override, update both systemd units; the
schedule renderer mirrors the running server's actual settings automatically.
The script treats "not a git repo" as a reported failure rather than a no-op, so a
misaimed working dir is loud instead of backing up nothing forever.

`/usr/bin/python3`, no venv: the script is stdlib-only, and so is the `server/`
package it imports (`config` → `boundaries` → `file_policy`/`bash_policy` → `log`).

### How you find out it stopped

Failures are reported through `/internal/cron/message`, so they arrive as a message
on the phone rather than a line in a log nobody reads. **Deduped per failure kind
per day** — an hourly cron failing all week notifies once a day, but a *different*
failure still gets through rather than being absorbed by the first one. See
`docs/tasks/failure-alert-channel.md` for why that channel and not the watchdog.

Three failures are distinguished on purpose, because they send you to different
places:

| Report | Means | Fix |
|---|---|---|
| `diverged` | git compared refs and refused — the remote has commits the box does not | A human merge. Treat the identity remote as write-only-from-the-box |
| `remote-refused` | the remote reached a decision and declined | **Most likely the deploy key is read-only** — "Allow write access" was not ticked. Otherwise a branch protection rule. **Not** a divergence |
| `timeout` | the push did not finish inside 120s | Reachability from the box |

A read-only key is matched deliberately: GitHub's SSH entry point declines *before*
the git protocol starts (`ERROR: The key you are using has read-only access`), with
no `remote:` prefix, so matching only the receive-side markers would drop the most
likely first-run failure into a bare "push failed".

| Exit | Meaning |
|---|---|
| `0` | pushed, or already up to date |
| `1` | operational — rejected, refused, timed out, or git itself failed |
| `2` | precondition wrong — no repo, no origin, no key |

Every non-success path reports — a timed-out push included, since an unreported
failure here is exactly the silence this piece exists to remove.

Verify the first run by hand before trusting the timer:

```sh
sudo -u marlow MARLOW_WORKING_DIR=/home/marlow/marlow-identity \
     MARLOW_SERVER_REPO=/home/marlow/marlow-server \
     /usr/bin/python3 /opt/marlow/bin/identity-push.py
```

## 7b. The bundle backup — run this ON THE MAC

```sh
scripts/backup-identity-bundle.sh
```

Pulls `git bundle create - --all` off the box over the tailnet into
`~/backups/marlow-identity/`, **bare-clones it as a check**, and prunes to the last
14. Restore with `git clone <bundle> restored/` — offline, no server involved.

The check is a real clone, not `git bundle verify`: measured 2026-08-09, verify
reads only the ref header and calls a bundle truncated to 200 bytes "okay" with "a
complete history". A bundle that will not clone is deleted rather than left on disk
looking like a backup.

Nothing on the box invokes this and nothing on the box holds a credential for the
Mac, which is the property that makes handing Marlow a write-capable deploy key
acceptable. It only replicates when the laptop is on and the tailnet is up; since
the box commits locally every turn, a missed day is delay, not loss.

**Scheduled (2026-10-14):** `scripts/install-backup-launchd.sh` installs a launchd
agent that runs it daily at 21:00 and at login, then raises a macOS notification
if the newest bundle is more than 3 days old (one failed run stays quiet). Log:
`~/Library/Logs/marlow-identity-backup.log`. `--uninstall` removes it. It runs
copies in `~/Library/Application Support/marlow-identity-backup/`, not the
checkout (a branch switch would break it silently), so **re-run the installer
after changing either script.** Turning it off in Login Items stops the backup
*and* its alarm; nothing watches the watcher.

## 8. Dead-man's switch — **must run on a DIFFERENT host**

See `deadmans/README.md`. Not installed here; the whole point is it survives
this server being down.

## 8b. TLS

The implementation plan (§"Transport") says **"HTTPS over the Tailscale
tailnet"**. The public (tailnet) uvicorn listener now serves real TLS; the
internal (loopback) listener stays plaintext by design, unchanged —
`hooks/pretooluse.py` talks to it over `http://127.0.0.1` and wrapping it in
TLS would break the hook for zero benefit.

The fix is Tailscale-native and needs no public ingress. Tailnet HTTPS
certificates are **enabled**, and a certificate has been **issued** for
`marlow.<tailnet>.ts.net`. Issue/renew with the paths baked in, so the
service user can actually read them:

```sh
sudo mkdir -p /home/marlow/.marlow/tls
sudo tailscale cert \
  --cert-file /home/marlow/.marlow/tls/marlow.crt \
  --key-file  /home/marlow/.marlow/tls/marlow.key \
  marlow.<tailnet>.ts.net
sudo chown -R marlow:marlow /home/marlow/.marlow/tls
sudo chmod 600 /home/marlow/.marlow/tls/marlow.key
```

`tailscale cert` writes to the **current working directory** if you don't pass
`--cert-file`/`--key-file` — root-owned, in whatever dir you happened to be in,
where the `marlow` user cannot reach them. `/home/marlow/.marlow` is already in
the unit's `ReadWritePaths` and is outside the git working tree (plan §5.3).

The cert is bound to the node's **tailnet name** — which is why §0 comes first.
Renaming the node invalidates it.

`server/config.py` reads the paths above via `MARLOW_TLS_CERT` /
`MARLOW_TLS_KEY` (defaults shown above already match). `server/app.py`'s
`build_public_uvicorn_config()` decides what to do at startup, in priority
order:

1. Cert + key both present and readable → serve HTTPS (`ssl_certfile` /
   `ssl_keyfile` passed to the public uvicorn `Config`).
2. `MARLOW_ALLOW_PLAINTEXT=1` → serve plain HTTP, with a loud warning logged.
   **Local dev only — never set this on a deployed box.**
3. Otherwise → **refuse to start**, raising `RuntimeError` naming the missing
   cert/key paths.

A missing cert never silently downgrades to plaintext — this is the recorded
decision in `docs/marlow-v2-START.md`'s "Decided" section. The internal
listener (`build_internal_uvicorn_config()`) never receives TLS kwargs at all,
in every case.

**Renewal.** Let's Encrypt certs last 90 days and `tailscale cert` renews only
when re-run, so this needs its own timer or the server dies quietly three
months in. `deploy/renew-cert.sh` re-runs the same `tailscale cert` command
above, chowns/chmods the output, and restarts `marlow-server` only if the cert
actually changed (the command is a cheap no-op otherwise). Install it:

```sh
sudo cp /home/marlow/marlow-server/deploy/marlow-cert-renew.service /etc/systemd/system/
sudo cp /home/marlow/marlow-server/deploy/marlow-cert-renew.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now marlow-cert-renew.timer
# Installed does not mean proven -- the timer arms without running, so run the
# service once by hand and read the log. Want "cert unchanged, no restart
# needed"; a Type=oneshot failure at 03:12 on a Sunday alerts nobody.
sudo systemctl start marlow-cert-renew.service
journalctl -u marlow-cert-renew.service -n 20 --no-pager
```

**Installed on the box 2026-09-06** (Mike), first run verified by hand the same
day. `renew-cert.sh` itself had never executed before that. Enabling the timer
does *not* trigger a catch-up run despite `Persistent=true`: with no prior stamp
file systemd bases the schedule on activation time rather than counting a missed
occurrence, so `LAST` stays empty until the first Sunday.

Sunday 03:00 (`Persistent=true` so a missed run catches up on next boot) is a
wide margin against the 90-day expiry, scheduled off-hours deliberately: a
restart is only triggered when the cert actually changed, but when it does
fire it drops the entire in-memory work queue and any pending phone
permission approval (both are memory-only — see `server/queue.py` /
`server/permissions.py`), so keeping it off-hours bounds that blast radius.
The service runs as root (`tailscale cert` needs it, and `systemctl restart`
needs it to talk to PID1) but is sandboxed the same way as
`marlow-server.service` — `ProtectSystem=strict` + `ReadWritePaths=/home/marlow/.marlow`
— rather than given free rein just because it's root. The script itself drops
the cert/key output back to `marlow:marlow`, key `0600`.

## 9. Egress allowlist (nftables) — deliberately deferred

See `egress.nft` + `resolve-egress-ips.sh`. **Untested; not required for a
working deploy.** §1–§8 give you a running, tailnet-only server; this step is
hardening on top of a box that already has no public ingress. Do it as its own
deliberate exercise, not as part of first-light.

**Current plan (2026-10-16): `docs/tasks/egress-and-webfetch.md`.** In short:
enforcement waits on Mike re-checking NemoClaw/OpenShell, which would replace
it. A log-only week comes first, and the hostname proxy it uses is also the
answer to the IP-rotation question below.

**When it's picked up, start in log-only mode** (Mike, 2026-10-16): allow
everything and record each host reached, then enforce. Plan and reasoning:
`docs/tasks/egress-and-webfetch.md`.

Two things to settle before applying it:

- **Interface names.** `tailscale0` is correct on the target (confirmed via
  `tailscale status` / `ip addr`). Verify, don't assume, if the host changes.
- **The design question, unresolved:** `resolve-egress-ips.sh` snapshots DNS for
  `api.anthropic.com`, which is Cloudflare-fronted with rotating IPs. When those
  rotate, the set goes stale and Marlow silently loses the API with a `drop` and
  a log line. Lockout risk is low (the chain accepts `127.0.0.0/8` and
  `oifname tailscale0` before the drop, and DNS via `127.0.0.53` is covered), but
  the silent-death risk is real. Decide how to handle rotation — frequent
  re-resolve timer, or a different enforcement point — before trusting this.
- **The web client's build needs the npm registry** (added 2026-10-01).
  `update.sh` runs `deploy/build-web.sh build`, which runs `npm ci` as
  `marlow`, and `registry.npmjs.org` is not in `EGRESS_ALLOWLIST`. Enforce this
  ruleset as it stands and the first update that touches `web/` (and every
  fresh box, which has no `web/dist`) fails with `FAILED (web client build)`.
  Do **not** fix it by adding the registry to the allowlist: the agent also
  runs as `marlow`, so that would let Marlow pull arbitrary npm packages. Run
  the build as a separate user with registry access that the agent lacks (a
  small change to the `sudo -u` line in `update.sh` plus ownership of
  `web/node_modules` and `web/dist`), or build somewhere else and copy `dist`
  in. `pip install` has the same shape (it runs as `marlow` and `pypi.org` *is*
  allowlisted); decide whether to move it to the build user at the same time.
- **Speech to text downloads its model from GitHub** (added 2026-10-22).
  `update.sh` runs `deploy/install-voice.sh` as `marlow`, which fetches the
  Parakeet archive from a `github.com` release (redirected to
  `objects.githubusercontent.com`) and pip installs PyAV and sherpa-onnx. Under
  enforcement the download fails, the script says so and the deploy carries
  on with the mic off; the checksum is pinned, so the fix is the same build
  user as above, not an allowlist entry.

## 10. Ongoing updates: dev box codes it, this box runs it

Code is written, tested, and pushed to `origin/main` from the dev machine (Mike's
macOS laptop). To ship a change to the running server, SSH into
`marlow.<tailnet>.ts.net` **as an admin account with sudo** (never `marlow`,
which deliberately has no sudo access) and run:

```sh
sudo ~marlow/marlow-server/deploy/update.sh
```

`update.sh` itself does the privilege split: git fetch/merge and the pip
install run as `marlow` (via internal `sudo -u marlow` calls, so the checkout
and venv stay marlow-owned), while `systemctl restart` and the root-owned
cron-entrypoint sync (§6a) run directly, since the script is already root by
the time it gets there. Reinstall deps, restart, verify `/health`. Manual, not
a cron — so nothing lands mid-edit. It touches only the software repo; the
seeded identity dir is left alone.

### 10-web. The web client build and pairing invites

After the pip step, `update.sh` runs `deploy/build-web.sh build` as `marlow`:
`npm ci && npm run build` in `web/`, which the server serves at `/` from
`web/dist`. It builds only when `web/` changed since the last successful build
or `web/dist` (its `index.html`) is missing, tracked by a stamp file
`web/.dist-stamp` (the git tree hash of `web/`), so a build that failed last time
is retried rather than skipped. The build goes into `web/.dist-build-XXXXXX`, is
checked for `index.html` and `sw.js`, and is left **staged** in `web/.dist-staged`;
live `web/dist` is untouched. On any failure the temp dir is removed and the
update exits non-zero with `FAILED (web client build)`, before anything else
runs. Fix and re-run.

Because the server reads `web/dist` per request, the staged build is renamed over
`web/dist` (`build-web.sh install`, which also advances the stamp) only
immediately before `systemctl restart`. If a root step in between fails, the old
dist keeps serving the old process. The only window where the old process serves
the new client is the restart itself. A stale staged dir from an aborted run is
discarded by the next build.

**Node >= 20.19** must be on `marlow`'s PATH (Vite 8's floor); an older or
missing `node` fails the build step with a message saying so. Check with
`sudo -u marlow -H node --version`. Nothing is built, and node is not checked, on
an update where the client is already current. `install.sh` sets up Node 22 on a
fresh box. A box installed before that (or by hand) needs it once, before the
first update that builds the web client; Ubuntu's own `nodejs` (18) is too old:

```sh
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
  | sudo gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
  | sudo tee /etc/apt/sources.list.d/nodesource.list
sudo apt-get update && sudo apt-get install -y nodejs
```

To pair a new device (Mike or a friend, over SSH on the box; needs no root):

```sh
sudo -u marlow ~marlow/marlow-server/deploy/pair-device.sh ["device name"]
```

Run it as the instance's user (`sudo -u marlow ...`): it asks the admin
endpoint on that user's owner-only admin socket (`MARLOW_ADMIN_SOCKET`, default
`~/.marlow/admin/admin.sock`) and prints a single-use pairing link, valid for 10
minutes, and a QR code of it (the box's own tailnet name from `tailscale status`,
overridable via `MARLOW_TAILNET_NAME` and `MARLOW_PUBLIC_PORT`; a placeholder if
neither answers). The invite rides in the link's `#fragment`, which a browser never
sends. It fails with a message if the server is not running.

The admin socket is not the internal one on purpose. A turn can connect to the
internal socket (the permission hook runs inside the bwrap namespace and must),
so an invite minted there would let one approved command pair itself and answer
its own permission prompts. The admin directory is a secret path: a tmpfs inside
the namespace, and the hook denies commands that name it. `deploy/devices.sh`
uses the same socket.

`deploy/devices.sh` lists paired devices and revokes one by fingerprint, the same
way. `update.sh --funnel` / `--no-funnel` switches Tailscale Funnel
(`deploy/funnel.sh`; `docs/tasks/funnel-ingress.md`).

Agent config files already on a box's disk are honoured, because `claude -p`
runs without `--strict-mcp-config` so the Claude.ai connectors reach it
(`docs/tasks/v2-dogfood-parity.md`). `boundaries.AGENT_CONFIG_PATHS` stops
Marlow *writing* an `mcpServers` entry, but does nothing about one already
there, and that entry's `command` runs at CLI startup, before any tool call or
the PreToolUse hook. If a box's identity dir was ever agent-writable, read
`.mcp.json` there and `mcpServers` in `~/.claude.json` by hand; no test can
catch it.

### 10a. Testing a branch on the box before merging it

`update.sh` takes an optional `--branch`, so a PR can be exercised on real
hardware before it goes to `main`:

```sh
sudo ~marlow/marlow-server/deploy/update.sh --branch some/feature-branch
```

**This does not relax the rule above.** The checkout must still be *on* the ref
being deployed, and a bare `update.sh` still refuses a box parked on a branch.
The flag only changes which ref counts as intended — so staying on a branch
means naming it every deploy, and drift stays impossible. Put the box back with:

```sh
sudo ~marlow/marlow-server/deploy/update.sh --branch main
```

**The branch must have `main` merged into it first.** `update.sh` re-execs
itself from the merged checkout (see the comment at that line for why — a script
that changes size mid-run resumes at a byte offset), which means the copy that
finishes the deploy is *the branch's* copy. A branch cut before this flag existed
re-execs into a script that ignores the argument, defaults to `main`, and refuses
with "not main" — blaming the box for a fault in the branch. That case is now
caught up front, against the remote ref, before anything on disk moves.

Both directions were exercised on the box 2026-08-27: out to
`feature/effort-toggle-server` and back to `main`.

**A deploy that would go backwards is refused** unless you pass `--force`.
That covers two cases: the target doesn't contain the commit currently checked
out, or a migration listed in `migrations.applied` is missing from the target.
Either way the code is older than what the box runs. The usual cause is a
branch cut before something merged into `main`, and the fix is to merge `main`
into it. Going back to `main` from a trial branch that never merged also counts
as a rollback, which is exactly what it is, so it needs `--force` too. The
check runs before the checkout moves. See "the rollback rule" in `update.sh`
for the 2026-10-19 incident behind it.

## 11. AppArmor profile for bubblewrap (gates the CLI confinement)

Marlow's strongest filesystem boundary is a bubblewrap namespace around
`claude -p`, in which `~/.ssh` and `~/.marlow` **do not exist** — for the CLI or
for any command it spawns. It replaces static analysis of Bash command strings
as the thing protecting the secrets (see
`docs/tasks/marlow-settings-permissions-policy.md`).

Ubuntu 24.04 blocks unprivileged user-namespace creation, so bubblewrap fails
with `setting up uid map: Permission denied` until this profile is installed.
The profile grants the exception to `/usr/bin/bwrap` alone; the box-wide sysctl
stays at `1`.

```sh
sudo apt install -y bubblewrap
sudo install -m 644 ~marlow/marlow-server/deploy/apparmor-bwrap /etc/apparmor.d/bwrap
sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

Verify — **the point is that this passes with the restriction still on**:

```sh
sysctl kernel.apparmor_restrict_unprivileged_userns   # must still be 1
bwrap --dev-bind / / -- /bin/true && echo "namespace ok"
bash ~/probe-bwrap-confinement.sh                     # expect CONFINED
```

If the probe says CONFINED while the sysctl reads `1`, the profile is doing its
job and nothing else on the box lost protection. Re-run the last two after a
reboot once, to confirm the profile loads at boot rather than only on demand.

To revert: `sudo rm /etc/apparmor.d/bwrap && sudo systemctl reload apparmor`.

The trade this accepts — any process running as `marlow` that can invoke `bwrap`
can create a user namespace — is written out in the profile's own header.

### 11a. Confinement is on by default

**Since 2026-08-06 no flag is needed** — `bwrap_enabled` defaults to `True`
(`server/config.py`), so installing the profile in §11 is the whole setup. The
staged rollout is done: it ran unconfined behind `MARLOW_BWRAP_ENABLED=1` for
one watched turn on the box, then became the default.

The consequence to plan for: **a host without `bwrap` now fails every
invocation**, loudly, with `executable not found ('bwrap')`. That is deliberate.
Auto-detecting a missing `bwrap` and quietly proceeding unconfined would mean
the boundary disappears exactly where it was never installed, and nothing in the
logs would say so. Running unconfined is a choice you make explicitly:

```sh
MARLOW_BWRAP_ENABLED=0        # dev Macs (bwrap is Linux-only), CI, bring-up
```

If you kept the `Environment=MARLOW_BWRAP_ENABLED=1` drop-in from the staged
rollout it is now redundant but harmless; `sudo systemctl revert marlow-server`
clears it. **Note that `systemctl revert` no longer disables confinement** — it
only removes the redundant override. To actually turn it off, set the drop-in to
`=0`.

The three things that would break, in the order they would show up:

1. **Auth** — a turn fails with an auth-looking error. `~/.claude` should be
   visible; check it is not caught by a `--tmpfs`.
2. **The permission round-trip** — a tool call that should prompt never reaches
   the phone. The hook runs *inside* the namespace and POSTs to the server's
   socket in `~/.marlow/run`, so this is what a `--tmpfs` over that directory
   (or a missing one) looks like. The directory is bound read-only in there,
   so a turn can't swap in its own listener; with `MARLOW_BWRAP_ENABLED=0`
   that guard is gone too.
3. **Confinement silently not applied** — a stray `=0` override, or code
   predating the flag. Confirm from the log, not from Marlow's behaviour:

   ```sh
   journalctl -u marlow-server -n 200 | grep 'invoking claude -p'
   ```

   Every line carries `confined=True|False`. If the field is missing entirely,
   the box is running code from before the confinement flag existed — run `update.sh` first.

**Do not try to verify this by asking Marlow to read a secret.** That test was
here until 2026-08-06 and it is unsound: `hooks/pretooluse.py` hard-denies every
path in `boundaries.SECRET_PATHS` before a shell ever runs, and the namespace's
tmpfs list is derived from that same tuple — so the hook always answers first
and you get its secret-path refusal ("that path holds a credential or private
key", `hooks/pretooluse.py`) whether confinement is on or off. Two layers covering the same paths is the design working; it just
makes the outer one invisible from the inside. `scripts/probe-bwrap-confinement.sh`
is what proves bwrap confines (run directly as `marlow`, no hook in the way);
the log line is what proves the service is using it.

Roll back with `sudo systemctl revert marlow-server && sudo systemctl restart
marlow-server` — no code change needed.

## 12. A second instance on the same box

For a second person's Marlow (`docs/tasks/marlow-for-taylor.md`): its own
Linux user, never in the `marlow` group, logged in to that person's own
Claude account, on its own tailnet port. The first instance is untouched.

```sh
sudo ~marlow/marlow-server/deploy/install.sh --instance taylor --public-port 8444 \
    --user-name "Taylor" --timezone America/Edmonton --email you@example.com --ref <branch>
```

It stops twice, like a first install: for the new user's read-only deploy
key, then for the Claude login, which is the other person's own account (their
connectors come with it). What it sets up:

- **`marlow-server@taylor`**, rendered by `update.sh` from
  `marlow-server.service` with only the home, `User` and `Group` changed, and
  written as its own unit file. Its own settings (port, cron entrypoint) are in
  `/etc/systemd/system/marlow-server@taylor.service.d/10-instance.conf`,
  which a rerun rewrites. Put hand-added settings in a `20-local.conf`.
- **Its own policy**: `marlow-settings.json` with `/home/marlow/` rewritten
  to its home.
- **Its own root-owned copies** of the cron entrypoint, watchdog and cert
  renewal, in `/opt/marlow/instances/taylor/`. Her crontab, watchdog line and
  cert timer run those, never `/opt/marlow`'s.
- **Its own cert copy and renewal**: `marlow-cert-renew@taylor.timer`, half
  an hour after the first instance's.
- **Its own watchdog line**: `/etc/cron.d/marlow-watchdog-taylor`.
- **Its own internal and admin sockets and migrations record**, so neither instance can
  reach the other's API (§10-web).

Update it by naming its user:

```sh
sudo MARLOW_USER=taylor ~taylor/marlow-server/deploy/update.sh --branch <branch>
```

**The two update independently.** Nothing either instance's update writes is
the other's, so a branch deployed to the first (`update.sh --branch ...`)
never reaches the second, which can stay on `main`. What they still share is
the box: system packages, node, and the bubblewrap AppArmor profile, which
`install.sh --instance` leaves alone when it is already there. The egress
table (§9) still names only the `marlow` uid; when it is turned on, add the
second.

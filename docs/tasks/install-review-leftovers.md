# Task — install.sh review leftovers

**Status:** parked 2026-10-02, deliberately not built. From `/code-review high`
and `/security-review` on the `install.sh` change (friends alpha), after its first Ubuntu dry
run. Fixed in that change: GitHub's host key is pinned rather than keyscanned,
`pairing-code.sh` runs as marlow, and a defaulted UTC timezone is refused. Each
item below has its trigger; none is filler to self-assign.

## 1. ✅ Closed: a rerun with a different `--user-name` split the name

Onboarding (`friends-alpha.md` item 9) was the trigger. The name now lives only
in USER.md: the starter says "they", the context block says "the user", and
install.sh writes no drop-in. A rerun with a new name changes nothing on a
seeded box. Marlow asks what to call them instead.

## 2. A missing branch is reported as "predates --branch"

`update.sh`'s branch check treats any `git show origin/<ref>:deploy/update.sh`
failure as the branch lacking the flag, so `--branch frends-alpha` (typo, or
not pushed) advises merging main into a branch that doesn't exist.

**Trigger:** anyone hits it. Fix: `git rev-parse --verify origin/<ref>` first,
with its own message.

## 3. The Node floor is checked on root's PATH

`install.sh`'s `node_ok` runs `node` as root; `build-web.sh` runs it as
marlow. A box where root sees a newer node than `/usr/bin/node` (one from
another tool in `/usr/local`, say) skips NodeSource, and the first web build
refuses the old one.

**Trigger:** a box with a non-apt node. Fix: check with `as_marlow node`.

## 4. The watchdog log is never rotated

The cron line appends to `/var/log/marlow-watchdog.log` every two minutes and
nothing rotates it. Small unless the watchdog is chatty.

**Trigger:** the file shows up in a disk-space question. Fix: a logrotate
stanza, or `logger` to the journal.

## 5. The NodeSource key isn't fingerprint-checked

Fetched over HTTPS and scoped with `signed-by`, which is NodeSource's own
documented setup, so this is hardening, not a hole. Pinning its fingerprint
(`gpg --show-keys`) before trusting it would match the GitHub key pin.

**Trigger:** the installer is opened to people Mike doesn't know.

## 6. A rerun runs root-owned steps from marlow's checkout

On a rerun after the service is live, `install.sh` as root still runs
`renew-cert.sh` and `update.sh` from `~marlow/marlow-server` and installs the
AppArmor profile and cert units from it. This is no wider than `update.sh`'s
own long-standing posture (`sudo ~marlow/marlow-server/deploy/update.sh`
installs units from the checkout), so it is a property of the whole deploy
model rather than of the installer.

**Trigger:** the deploy model itself is revisited, e.g. building as a separate
user (`deploy/README.md` §9).

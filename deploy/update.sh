#!/usr/bin/env bash
# Ships a code change from the dev machine (where it's written, tested, and
# pushed to origin/main) onto this box. Manual, not a cron: nothing should land
# on the running server mid-edit before it's meant to.
#
# This operates ONLY on the software repo (this checkout). The assistant's
# identity/working dir ($MARLOW_WORKING_DIR) is a SEPARATE directory that
# diverges independently and is never touched here — that separation is what
# lets this stay a clean fast-forward.
#
# Run as an ADMIN user with sudo — NOT as `marlow`. `marlow` deliberately has no sudo access (same reasoning as
# cron/entrypoint.py being root-owned and marlow-unwritable: no path for
# Marlow to escalate itself), but this script needs root for `systemctl
# restart` and for refreshing the root-owned cron entrypoint copy (see §6a).
# The steps that don't need root — git fetch/merge, pip install — run as
# `marlow` via `sudo -u marlow` instead, so the checkout and venv stay
# marlow-owned exactly as every other convention in deploy/README.md expects.
#
#   sudo ~marlow/marlow-server/deploy/update.sh
#
# To put a PR on the box BEFORE merging it -- which is the only way to try a
# server change against the real phone, real connectors and real cron:
#
#   sudo ~marlow/marlow-server/deploy/update.sh --branch feature/some-feature
#
# and to put it back afterwards:
#
#   sudo ~marlow/marlow-server/deploy/update.sh --branch main
#
# See "the branch rule" below for what that flag does and does not relax.
#
# A deploy that would take the box BACKWARDS is refused unless `--force` is
# given; see "the rollback rule" below.
set -euo pipefail
# sudo -u keeps the caller's working directory, which marlow may not be able to
# enter (an admin home is 750 on Ubuntu 24.04), and git, pip and npm then fail.
# Every path below is absolute. See install.sh (2026-10-02).
cd /

DEPLOY_REF=main
FORCE=0
# --funnel / --no-funnel: serve this instance through Tailscale Funnel, or stop
# (deploy/funnel.sh). Neither flag leaves it as it is; the setting is a systemd
# drop-in, so it survives every later update.
FUNNEL_ACTION=""
# Captured BEFORE parsing consumes them. This script re-execs itself from the
# merged checkout further down, and that exec must carry the same flags -- the
# parse loop below shifts them away, so a plain "$@" there is empty by then.
# The failure that would cause is quiet and bad: pass one switches the box to
# the feature branch, the re-exec re-parses with no args, defaults to main, and
# switches it straight back -- reporting success having deployed main. Exactly
# the family of bug the re-exec comment at the bottom of this block describes.
ORIGINAL_ARGS=("$@")
while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)
            DEPLOY_REF="${2:-}"
            if [[ -z "$DEPLOY_REF" ]]; then
                echo "--branch needs a branch name" >&2
                exit 1
            fi
            # A ref is about to be interpolated into git commands run through
            # `bash -c`. Anything outside this set is refused rather than
            # quoted around: branch names here are ordinary
            # `feature/thing-2` shapes, and the alternative is reasoning about
            # shell metacharacters on the deploy path.
            #
            # The first character may NOT be `-`, which the rest of the class
            # otherwise allows. A value like `-foo` or `--upload-pack=x` reaches
            # git as an OPTION rather than a ref no matter how well it is
            # quoted -- the same one-field-becomes-a-flag hole that
            # server/invoke.py's `--resume=<value>` form exists to close.
            if ! [[ "$DEPLOY_REF" =~ ^[A-Za-z0-9._/][A-Za-z0-9._/-]*$ ]]; then
                echo "refusing: '$DEPLOY_REF' is not a plain branch name" >&2
                exit 1
            fi
            shift 2
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --funnel)
            FUNNEL_ACTION=on
            shift
            ;;
        --no-funnel)
            FUNNEL_ACTION=off
            shift
            ;;
        *)
            echo "usage: update.sh [--branch <name>] [--force] [--funnel | --no-funnel]" >&2
            exit 1
            ;;
    esac
done

MARLOW_USER="${MARLOW_USER:-marlow}"
# A second instance on the box (docs/tasks/marlow-for-taylor.md) is updated
# by naming its user: `sudo MARLOW_USER=taylor ~taylor/marlow-server/deploy/
# update.sh`. That updates her checkout, venv, policy, identity files, units
# and root-owned copies, and restarts marlow-server@taylor. Nothing it writes
# is the first instance's, so the two can run different refs.
if ! [[ "$MARLOW_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
    echo "refusing: MARLOW_USER '$MARLOW_USER' is not a plain user name" >&2
    exit 1
fi
if [[ "$MARLOW_USER" == marlow ]]; then
    DEFAULT_UNIT=marlow-server
else
    DEFAULT_UNIT="marlow-server@$MARLOW_USER"
fi
# Each instance's root-owned copies (cron entrypoint, watchdog, cert renewal)
# live in its own directory and come only from its own update, so a branch
# deployed to one never changes what runs for the other. marlow keeps the
# original /opt/marlow, which its crontab and units already name.
if [[ "$MARLOW_USER" == marlow ]]; then
    OPT_DIR=/opt/marlow
else
    OPT_DIR="/opt/marlow/instances/$MARLOW_USER"
fi
REPO_DIR="${MARLOW_SERVER_REPO:-/home/$MARLOW_USER/marlow-server}"
if [[ "$MARLOW_USER" == marlow ]]; then
    MIGRATIONS_RECORD="/var/lib/marlow/migrations.applied"
else
    MIGRATIONS_RECORD="/var/lib/marlow/instances/$MARLOW_USER/migrations.applied"
fi
VENV_PIP="${MARLOW_VENV_PIP:-/home/$MARLOW_USER/.venvs/marlow-server/bin/pip}"
UNIT="${MARLOW_SYSTEMD_UNIT:-$DEFAULT_UNIT}"
INTERNAL_SOCKET="${MARLOW_INTERNAL_SOCKET:-/home/$MARLOW_USER/.marlow/run/internal.sock}"
if [[ -n "${MARLOW_HEALTH_URL:-}" ]]; then
    echo "MARLOW_HEALTH_URL is retired (the internal API is a socket now); set MARLOW_INTERNAL_SOCKET" >&2
    exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
    echo "run this with sudo — it needs root for systemctl restart and the" >&2
    echo "root-owned cron entrypoint and pusher installs (README §6a, §7a)" >&2
    exit 1
fi

# THE BRANCH RULE. The checkout must be on the ref being deployed, not merely
# fast-forwardable to it. Found 2026-08-06: marlow's clone had been sitting on
# `ios-permission-round-trip-ui` since that work ended, and nothing complained
# -- the merge below named origin/main explicitly, so it kept fast-forwarding
# the feature branch to main's tip and deploying the right code under the wrong
# label. Harmless until the branch is deleted on the remote, at which point
# `git pull` breaks and the label is actively misleading about what is running.
#
# `--branch` does NOT relax that. What it relaxes is only *which* ref is the
# intended one; the check that the checkout matches it is unchanged, and a bare
# `update.sh` still refuses a box parked on a feature branch. So drift remains
# impossible: staying on a branch requires naming it, every single time.
#
# What makes deploying a branch safe to offer at all is that the label cannot
# lie -- /health reports git_branch and git_commit frozen at import, this script
# prints them below, and the iOS Settings pane shows the same pair. "What is the
# box running" always has an answer you can read without ssh.
if [[ "$DEPLOY_REF" != main ]]; then
    echo "########################################################"
    echo "## DEPLOYING A BRANCH, NOT main: $DEPLOY_REF"
    echo "## This box will keep running it across restarts until"
    echo "## you run: update.sh --branch main"
    echo "########################################################"
fi

UNIT_ENV="$(systemctl show "$UNIT" -p Environment --value 2>/dev/null || true)"
unit_var() { tr ' ' '\n' <<<"$UNIT_ENV" | sed -n "s/^$1=//p" | tail -1; }

# Funnel on/off (deploy/funnel.sh). Checked BEFORE the fetch and merge, so a
# flag that can't work stops the run with nothing on the box changed. It sat
# after the merge and the deps reinstall until the review of 2026-10-24, and
# the missing Funnel grant (every box's first --funnel) then left new code on
# disk that was never deployed, while saying "nothing has been changed". This
# file is always the one being run here: a copy from before funnel.sh rejects
# --funnel as an unknown argument, so it never gets this far.
source "$REPO_DIR/deploy/funnel.sh"
funnel_preflight || exit 1

# What to tell the admin when the fetch fails. git's error for a refused
# credential, or for a wrong URL on a public repo ("could not read Username",
# "Repository not found"), reads like a network blip, so the admin retries and
# gets nowhere (2026-10-28). Name the
# credential only when git's error is about one: a wrong guess sends someone
# to fix the wrong thing.
fetch_failure_hint() {
    local url="$1" err="$2" auth=0
    # A token may sit in the URL as userinfo; never echo it.
    url="$(sed -E 's#^(https://)[^/@]*@#\1#' <<<"$url")"
    # No origin URL (get-url failed): git's error still names the scheme.
    if [[ -z "$url" && "$err" == *https://* ]]; then
        url=https://
    fi
    if grep -qiE 'authentication failed|could not read (username|password)|invalid username|repository not found|returned error: (401|403)' <<<"$err"; then
        auth=1
    fi
    if [[ $auth == 1 && "$url" == https://* ]]; then
        echo "GitHub refused $url to this box."
        echo "Marlow's repository is public and needs no credentials, so check origin's"
        echo "URL first: GitHub answers a wrong or moved one this way. If origin is a"
        echo "private fork read with a token, the token has most likely lapsed: make a"
        echo "new read-only one (Contents: read), put it in"
        echo "${MARLOW_USER:-the instance user}'s git credentials in place of the old one,"
        echo "and run update.sh again."
    else
        echo "Could not fetch $url (git's error is above)."
        echo "If it is the network, run update.sh again once it is back."
    fi
    echo "Nothing on this box has changed."
}

echo "== fetching origin (as $MARLOW_USER) =="
# Before the checkout assert, not after: switching to a branch this clone has
# never seen needs the remote ref to exist locally first. GIT_TERMINAL_PROMPT=0:
# with no usable token git would otherwise stop at a Username prompt.
if ! fetch_err="$(sudo -u "$MARLOW_USER" -H bash -c "cd '$REPO_DIR' && GIT_TERMINAL_PROMPT=0 git fetch origin" 2>&1 >/dev/null)"; then
    printf '%s\n\n' "$fetch_err" >&2
    origin_url="$(sudo -u "$MARLOW_USER" -H git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
    fetch_failure_hint "$origin_url" "$fetch_err" >&2
    exit 1
fi
[[ -z "$fetch_err" ]] || printf '%s\n' "$fetch_err"

# The re-exec further down runs update.sh FROM THE MERGED CHECKOUT -- the copy
# on $DEPLOY_REF, not the copy that was invoked. So --branch only does anything
# if the branch being deployed already contains --branch support.
#
# Found 2026-08-27 on this flag's first real use: a branch cut before the flag
# existed re-exec'd into a copy that ignored the argument entirely, defaulted to
# main, and refused with "on 'feature/...', not main" -- pointing at the box when
# the fault was in the branch, and leaving the checkout switched. Checked here,
# against the REMOTE ref, so it fails before anything on disk moves.
if [[ "$DEPLOY_REF" != main && -z "${MARLOW_UPDATE_REEXEC:-}" ]]; then
    echo "== checking $DEPLOY_REF can deploy itself =="
    # Existence first: `git show` on a missing ref fails the same way as an
    # old update.sh, and the refusal below then blamed the flag for a branch
    # that was never pushed (seen on the box 2026-10-19).
    if ! sudo -u "$MARLOW_USER" -H bash -c "cd '$REPO_DIR' && git rev-parse --verify --quiet 'origin/$DEPLOY_REF' >/dev/null"; then
        echo "refusing: origin/$DEPLOY_REF does not exist. Push the branch first." >&2
        exit 1
    fi
    # Captured, then grepped: piped into `grep -q` under pipefail, grep exits at
    # the first match, git show dies of SIGPIPE mid-write, and the pipeline
    # fails. Refused a branch that had the flag 34 times in 100 on a fresh
    # Ubuntu 24.04 VM (2026-10-02); never once in 300 on macOS.
    branch_update_sh="$(sudo -u "$MARLOW_USER" -H bash -c "cd '$REPO_DIR' && git show 'origin/$DEPLOY_REF:deploy/update.sh'" || true)"
    if [[ "$branch_update_sh" != *ORIGINAL_ARGS* ]]; then
        echo "refusing: origin/$DEPLOY_REF has an update.sh that predates --branch." >&2
        echo "This script re-execs from the deployed checkout, so the branch must" >&2
        echo "carry the flag too. Merge main into it and push, then retry:" >&2
        echo "  git switch $DEPLOY_REF && git merge main && git push" >&2
        exit 1
    fi
fi


# THE ROLLBACK RULE. Refuse a deploy that would take the box backwards, before
# the checkout moves. Found 2026-10-19: text-size-and-centred-panels was cut
# from main before #131 (per-entry effort) merged, and deploying it put the
# pre-#131 code back on a box that had already run 0006-entry-effort. The old
# validator refused the migrated schedules store ("unsupported keys:
# ['effort']"), and Marlow "fixed" it by deleting the effort from three
# entries. The old identity's CLAUDE.md went back in too. Nothing failed and
# nothing in the deploy output said so.
#
# Two checks, either one refuses:
#   - a migration this box has run is missing from the target. That target's
#     code predates state already on disk -- the damaging case.
#   - the target does not contain the commit checked out now. Catches the
#     same branch-cut-too-early mistake before any migration is involved,
#     and also a trial branch being dropped by going back to an unmerged main.
# `--force` deploys anyway, for a rollback that is meant. It is not carried
# into the re-exec below: the target's own copy may predate the flag and
# would refuse it as a usage error, after the checkout had already moved.
# The check runs from the copy of this script already on the box, so the
# deploy that first ships it is not itself checked.
#
# Prints one line per reason; prints nothing if the deploy goes forwards.
rollback_reasons() {
    local repo="$1" target="$2" record="$3" head applied name rc=0
    # A target that does not resolve is not a rollback: the existence check
    # below says "push the branch first", which is the right advice. Judged
    # here it would list every applied migration as missing (review 2026-10-19).
    sudo -u "$MARLOW_USER" -H git -C "$repo" rev-parse --verify --quiet "$target^{commit}" >/dev/null || return 0
    head="$(sudo -u "$MARLOW_USER" -H git -C "$repo" rev-parse --short HEAD)"
    sudo -u "$MARLOW_USER" -H git -C "$repo" merge-base --is-ancestor "$head" "$target" || rc=$?
    if [[ "$rc" -eq 1 ]]; then
        echo "$target does not contain the commit checked out now ($head)"
    elif [[ "$rc" -ne 0 ]]; then
        echo "could not compare $target with the commit checked out now (git exit $rc)"
    fi
    [[ -r "$record" ]] || return 0
    applied="$(sudo -u "$MARLOW_USER" -H git -C "$repo" ls-tree --name-only "$target" deploy/migrations/)"
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        if ! grep -qxF "deploy/migrations/$name.py" <<<"$applied"; then
            echo "migration $name has run on this box but is not in $target"
        fi
    done < "$record"
}
# Exits unless there is nothing to refuse or --force was given.
enforce_rollback_rule() {
    local reasons="$1"
    [[ -n "$reasons" ]] || return 0
    sed 's/^/   /' <<<"$reasons" >&2
    if [[ "$FORCE" -eq 1 ]]; then
        echo "   --force given: deploying anyway" >&2
        return 0
    fi
    echo "refusing: this deploy would take the box backwards." >&2
    if [[ "$DEPLOY_REF" == main ]]; then
        echo "Leaving a branch that has not merged: merge it first, or re-run with" >&2
        echo "--force to drop it from the box." >&2
    else
        echo "Usually the branch needs main merged in:" >&2
        echo "  git switch $DEPLOY_REF && git merge main && git push" >&2
        echo "If going back is the point, re-run with --force." >&2
    fi
    exit 1
}
if [[ -z "${MARLOW_UPDATE_REEXEC:-}" ]]; then
    echo "== checking origin/$DEPLOY_REF does not roll this box back =="
    enforce_rollback_rule "$(rollback_reasons "$REPO_DIR" "origin/$DEPLOY_REF" "$MIGRATIONS_RECORD")"
fi

echo "== checking the checkout is on $DEPLOY_REF =="
CURRENT_BRANCH="$(sudo -u "$MARLOW_USER" -H bash -c "cd '$REPO_DIR' && git rev-parse --abbrev-ref HEAD")"
if [[ "$CURRENT_BRANCH" != "$DEPLOY_REF" ]]; then
    if ! sudo -u "$MARLOW_USER" -H bash -c "cd '$REPO_DIR' && git rev-parse --verify --quiet 'origin/$DEPLOY_REF' >/dev/null"; then
        echo "refusing: origin/$DEPLOY_REF does not exist. Push the branch first." >&2
        exit 1
    fi
    # Switching is the whole point of the flag, so do it rather than making
    # Mike run a second command -- but say so loudly, because moving the box
    # off what it was running is exactly the event worth seeing in the log.
    echo "   switching $REPO_DIR from '$CURRENT_BRANCH' to '$DEPLOY_REF'"
    sudo -u "$MARLOW_USER" -H bash -c "cd '$REPO_DIR' && git switch '$DEPLOY_REF'"
fi

echo "== fast-forward merge (fails loudly on divergence — no force, no rebase-and-pray) =="
# The software checkout must never carry local commits, so this always ff-merges
# cleanly. If it ever fails, something wrote into the software repo that
# shouldn't have — investigate, don't force. A force-pushed feature branch will
# also fail here, which is correct: that is divergence, and the fix is a
# deliberate reset, not a silent overwrite of what the box is running.
sudo -u "$MARLOW_USER" -H bash -c "cd '$REPO_DIR' && git merge --ff-only 'origin/$DEPLOY_REF'"

# Re-exec, because THIS SCRIPT LIVES IN THE CHECKOUT THE MERGE ABOVE JUST
# REWROTE. Found 2026-08-09 the hard way: the merge that shipped the identity
# pusher also rewrote update.sh underneath the running bash, and the new
# `install` block -- appended after this point -- never executed. §7a's file was
# simply absent afterwards, with the run reporting success. The cron block above
# it did run, because it existed in the *old* body at that offset.
#
# So the certain failure is that a change to this file never takes effect in the
# run that pulls it -- always one deploy late, silently. The worse one is that
# bash reads a script incrementally by byte offset, so a file that changes SIZE
# mid-run can resume mid-token and skip or garble steps that have nothing to do
# with the change. That is why this is a re-exec and not a note in the README.
#
# Everything below therefore runs from the merged code. The second pass repeats
# the fetch and the ff-merge, which are a provable no-op the moment they run
# twice in a row, and pip install -q, which is idempotent -- cheaper than
# restructuring the script around a resume point. MARLOW_UPDATE_REEXEC breaks
# the loop and is deliberately not documented as a knob: it is an implementation
# detail of this exec, not something to set by hand.
if [[ -z "${MARLOW_UPDATE_REEXEC:-}" ]]; then
    echo "== re-exec'ing update.sh from the merged checkout =="
    export MARLOW_UPDATE_REEXEC=1
    # ${ARR[@]+...} guard: under `set -u` an empty array expansion is an error
    # on bash < 4.4, which is precisely the no-flags (plain `update.sh`) case.
    # --force is dropped here; see the rollback rule.
    REEXEC_ARGS=()
    for arg in ${ORIGINAL_ARGS[@]+"${ORIGINAL_ARGS[@]}"}; do
        [[ "$arg" == --force ]] || REEXEC_ARGS+=("$arg")
    done
    exec bash "$REPO_DIR/deploy/update.sh" ${REEXEC_ARGS[@]+"${REEXEC_ARGS[@]}"}
fi

echo "== reinstalling deps (as $MARLOW_USER, idempotent) =="
sudo -u "$MARLOW_USER" -H "$VENV_PIP" install -q -r "$REPO_DIR/server/requirements.txt"

echo "== speech to text (as $MARLOW_USER; deploy/install-voice.sh) =="
# On by default (Mike, 2026-10-22). Never fails the deploy: a box it can't set
# up just keeps the mic off. Only ever the 110M; the model downloads once, and
# the speed check warns loudly on a slow box. MARLOW_VOICE=0 passes through
# from `sudo MARLOW_VOICE=0 update.sh`.
#
# The unit's own settings decide two things. A unit that names another model
# (MARLOW_STT_MODEL, the hand-installed 0.6B) is left alone: fetching and
# timing the 110M there would be for a model the server never loads. And the
# check times with the unit's MARLOW_STT_THREADS, the count the server runs.
STT_MODEL_SET="$(unit_var MARLOW_STT_MODEL)"
STT_THREADS="$(unit_var MARLOW_STT_THREADS)"
[[ "$STT_THREADS" =~ ^[0-9]+$ ]] || STT_THREADS=""
# The model goes where the server looks: the unit's data dir, not this shell's
# (`/code-review high`, 2026-10-23).
STT_DATA_DIR="$(unit_var MARLOW_DATA_DIR)"
[[ "$STT_DATA_DIR" == /* ]] || STT_DATA_DIR="/home/$MARLOW_USER/.marlow/data"
if [[ -n "$STT_MODEL_SET" && "$STT_MODEL_SET" != sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8 ]]; then
    echo "   voice: $UNIT uses its own model ($STT_MODEL_SET): left as it is."
else
    sudo -u "$MARLOW_USER" -H env MARLOW_VOICE="${MARLOW_VOICE:-1}" \
        ${STT_THREADS:+MARLOW_STT_THREADS="$STT_THREADS"} \
        bash "$REPO_DIR/deploy/install-voice.sh" "$REPO_DIR" "$(dirname "$(dirname "$VENV_PIP")")" \
        "$STT_DATA_DIR" || true
fi

echo "== building the web client (as $MARLOW_USER, only when web/ changed or dist is missing) =="
# Straight after pip and BEFORE anything that touches root-owned state or the
# running service, so a failed build (set -e: its non-zero exit ends the run)
# stops the deploy with the old process still serving the old client. Server
# code and client ship together; restarting onto new server code with a missing
# or stale client is what this rules out. This phase only STAGES the build
# (web/.dist-staged): the server reads web/dist per request, so swapping it in
# here would put the new client in front of the OLD process for as long as the
# root steps below take -- and leave it there if one of them fails. The swap is
# the `install` phase, right before the restart. If a step between fails, the
# old dist keeps serving the old process; the merged code on disk is ahead of
# the process, the state the checkout-vs-/health warning below describes. Logic
# lives in its own script so it can be tested without root.
sudo -u "$MARLOW_USER" -H bash "$REPO_DIR/deploy/build-web.sh" build "$REPO_DIR"

echo "== syncing root-owned cron entrypoint (deploy/README.md §6a) =="
# cron/entrypoint.py is deployed as a standalone root-owned copy outside this
# checkout, not run from here directly, so a plain git pull above never
# updates it -- without this step a cron/entrypoint.py change ships to
# /home/marlow/marlow-server but the crontab keeps firing the old code
# indefinitely, silently. Idempotent (install no-ops when content matches).
# Runs directly as root — this script is already root, not marlow.
mkdir -p "$OPT_DIR/cron"
install -o root -g root -m 755 "$REPO_DIR/cron/entrypoint.py" "$OPT_DIR/cron/entrypoint.py"

echo "== syncing identity pusher (deploy/README.md §7a) =="
# Same reasoning as the entrypoint above, and the same failure mode if skipped:
# the root-owned systemd unit runs $OPT_DIR/bin/identity-push.py, so a fix
# shipped here would never reach the code that actually runs.
#
# Root-OWNED but marlow-EXECUTED (755), which is §6b's rule: a crontab line
# pointing into Marlow's own working tree would let the agent rewrite tomorrow's
# scheduled job. The script itself no longer runs as root -- see its docstring for
# why that was reverted.
mkdir -p "$OPT_DIR/bin"
install -o root -g root -m 755 "$REPO_DIR/deploy/identity-push.py" "$OPT_DIR/bin/identity-push.py"

echo "== syncing root-run scripts: cert renewal, watchdog (deploy/README.md §6a) =="
# Same rule and same failure as the two above: root runs these (the cert timer,
# install.sh's watchdog cron line), so root runs a root-owned copy, and a copy
# nothing refreshes keeps running install-day code forever. lib.sh travels with
# them because both source it from their own directory.
install -o root -g root -m 755 "$REPO_DIR/deploy/renew-cert.sh" "$OPT_DIR/bin/renew-cert.sh"
install -o root -g root -m 755 "$REPO_DIR/deploy/watchdog.sh" "$OPT_DIR/bin/watchdog.sh"
install -o root -g root -m 644 "$REPO_DIR/deploy/lib.sh" "$OPT_DIR/bin/lib.sh"

echo "== renaming the working dir to ~/marlow-identity (once; deploy/rename-working-dir.sh) =="
# Before the policy sync, the units and every step that reads $WORKING_DIR:
# they all name the new path. The server holds the old one from startup, so it
# stops for the move and the restart below brings it back on the new one. The
# dry run comes first, so a box that would refuse keeps its old process
# serving, with its old policy and units, and nothing here has changed yet.
#
# A box that sets MARLOW_WORKING_DIR has chosen its own path; it is left alone.
#
# On this one run, a step below that fails exits with the server STOPPED, not
# with the old process serving as the migrations note says: the old process
# can't serve a dir that has moved. The watchdog starts it again within minutes.
# The identity pusher's timer stops too, or a tick before the units sync runs
# the old unit against the old path; `enable --now` below starts it again.
#
# Going back past this with --force means moving the dir back by hand: the
# older code looks for ~/claude-assistant.
RENAME="$REPO_DIR/deploy/rename-working-dir.sh"
if [[ -n "${MARLOW_WORKING_DIR:-}" || -n "$(unit_var MARLOW_WORKING_DIR)" ]]; then
    echo "   MARLOW_WORKING_DIR is set; leaving it where it is"
elif sudo -u "$MARLOW_USER" -H test -e "/home/$MARLOW_USER/claude-assistant"; then
    sudo -u "$MARLOW_USER" -H bash "$RENAME" --dry-run || exit 1
    systemctl stop "$UNIT"
    if [[ "$MARLOW_USER" == marlow ]]; then
        systemctl stop marlow-identity-push.timer 2>/dev/null || true
    fi
    sudo -u "$MARLOW_USER" -H bash "$RENAME" || exit 1
else
    echo "   nothing to rename"
fi

echo "== syncing the permission policy (was a manual cp until 2026-09-06) =="
# deploy/marlow-settings.json is the CLI's permission policy for the marlow
# user, and the live copy is ~marlow/.claude/settings.json. Until today this
# script did not touch it: a JSON change shipped only when Mike remembered to
# re-run a `cp` documented in a task doc. That is a deploy that half-lands by
# default, and the failure is silent in both directions -- the `.git` deny
# nearly shipped one-sided in August, and the dead `TodoWrite` allow entry
# survived five months partly because nobody was ever looking at the live file.
#
# Unconditional overwrite is correct here, unlike the cron/schedules.json seed
# further down. That store is MARLOW'S to edit after seeding, so overwriting it
# would undo the agent's own work. This file is the opposite: it is denied to
# Marlow by AGENT_CONFIG_PATHS precisely so the repo stays its only author, and
# tests/test_marlow_settings.py holds it in lockstep with server/boundaries.py.
# A live copy that differs is drift by definition, so there is nothing here
# worth preserving and no backup taken -- git already holds every prior version
# and this script deploys from it.
#
# Copied AS MARLOW, not as root, for the reason spelled out at the schedules
# seed below: root writing through a path the unprivileged user controls can be
# redirected by a symlink it plants. Marlow owns ~/.claude (the CLI writes
# there itself), so doing this as marlow removes that primitive rather than
# reasoning about it. It also means no chown is needed for the file to be
# readable by the process that consumes it.
#
# The file names marlow's own paths. Another instance gets them rewritten to
# its home, the same substitution as its unit (render_instance_unit below);
# for marlow the rendering is a byte-for-byte copy.
CLAUDE_HOME="/home/$MARLOW_USER/.claude"
LIVE_POLICY="$CLAUDE_HOME/settings.json"
sudo -u "$MARLOW_USER" -H mkdir -p "$CLAUDE_HOME"
RENDERED_POLICY="$(sudo -u "$MARLOW_USER" -H mktemp "$CLAUDE_HOME/.settings.json.XXXXXX")"
if ! sudo -u "$MARLOW_USER" -H bash -c 'sed "s|/home/marlow/|/home/$1/|g" "$2" > "$3"' \
    _ "$MARLOW_USER" "$REPO_DIR/deploy/marlow-settings.json" "$RENDERED_POLICY"; then
    sudo -u "$MARLOW_USER" -H rm -f "$RENDERED_POLICY"
    echo "could not render the permission policy" >&2
    exit 1
fi
if sudo -u "$MARLOW_USER" -H cmp -s "$RENDERED_POLICY" "$LIVE_POLICY"; then
    sudo -u "$MARLOW_USER" -H rm -f "$RENDERED_POLICY"
    echo "   policy unchanged"
else
    # Announce it. A permission policy changing under the running assistant is
    # exactly the event worth seeing in the deploy log, the same reasoning as
    # the branch switch above -- and for the first few runs it is also how we
    # find out whether anything had been hand-edited on the box.
    echo "   policy CHANGED -- installing $LIVE_POLICY"
    sudo -u "$MARLOW_USER" -H chmod 600 "$RENDERED_POLICY"
    sudo -u "$MARLOW_USER" -H mv -fT "$RENDERED_POLICY" "$LIVE_POLICY"
fi

echo "== syncing systemd units and identity-push timer =="
# Install the identity timer BEFORE restarting marlow-server. The restarted
# server immediately replaces marlow's complete crontab from schedules.json;
# enabling the timer first guarantees that removing the old :17 cron line never
# creates a window with no off-box identity backup cadence.
#
# Reinstall marlow-server.service too: schedule installation needs its
# SupplementaryGroups=crontab grant. Merely shipping the revised unit in git
# would leave an existing box running the old permissions forever.
#
# A second instance runs marlow-server@<user>, rendered from
# marlow-server.service rather than kept as a second file: only the home,
# User and Group change. It is written as that instance's own unit file
# (marlow-server@taylor.service, not a shared @ template), so only its own
# update ever rewrites it, and a branch deployed to marlow leaves it alone.
# Its overrides are drop-ins this never touches: port and cron entrypoint in
# 10-instance.conf, which install.sh --instance writes, and anything added by
# hand (a model, APNs) in 20-local.conf. Marlow's own
# units and extras stay marlow's run only.
render_instance_unit() {
    sed -e 's|/home/marlow/|/home/%i/|g' \
        -e 's|^User=marlow$|User=%i|' \
        -e 's|^Group=marlow$|Group=%i|' \
        -e 's|^Description=.*|Description=marlow-server for %i (rendered from marlow-server.service by update.sh)|' \
        "$1"
}
if [[ "$MARLOW_USER" != marlow ]]; then
    INSTANCE_UNIT="/etc/systemd/system/$UNIT.service"
    render_instance_unit "$REPO_DIR/deploy/marlow-server.service" > "$INSTANCE_UNIT.new"
    chmod 644 "$INSTANCE_UNIT.new"
    mv -f "$INSTANCE_UNIT.new" "$INSTANCE_UNIT"
    for kind in service timer; do
        install -o root -g root -m 644 "$REPO_DIR/deploy/marlow-cert-renew@.$kind" \
            "/etc/systemd/system/marlow-cert-renew@$MARLOW_USER.$kind"
    done
    echo "   $UNIT and marlow-cert-renew@$MARLOW_USER installed"
else
    install -o root -g root -m 644 \
        "$REPO_DIR/deploy/marlow-server.service" /etc/systemd/system/marlow-server.service
    # The identity pusher is an OPTIONAL extra: it needs a deploy key to an
    # identity repo (§7a). A box that has it keeps getting it updated; a box that
    # never installed it -- every install.sh box -- never gets it, because enabling
    # it there starts a timer that fails hourly against a key that does not exist.
    # The test is the installed unit file, so the first install on a new box is its
    # own deliberate step: copy the two unit files into /etc/systemd/system, then
    # rerun this script.
    # The cert timer rides the same check: every box has one, but only update.sh
    # refreshes it here, so a changed unit (ExecStart moved to /opt/marlow/bin,
    # 2026-09-29) reaches a box that installed it by hand.
    for extra in marlow-identity-push marlow-cert-renew; do
        if [[ -f "/etc/systemd/system/$extra.timer" ]]; then
            install -o root -g root -m 644 \
                "$REPO_DIR/deploy/$extra.service" "/etc/systemd/system/$extra.service"
            install -o root -g root -m 644 \
                "$REPO_DIR/deploy/$extra.timer" "/etc/systemd/system/$extra.timer"
            echo "   $extra: installed on this box, updated"
        else
            echo "   $extra: not installed on this box, skipped"
        fi
    done
fi
systemctl daemon-reload
if [[ -f /etc/systemd/system/marlow-identity-push.timer ]]; then
    systemctl enable --now marlow-identity-push.timer
fi

echo "== retiring the migrated identity-push crontab line =="
# The job now has a timer, so its crontab line would double-fire — and more
# importantly the renderer refuses to install while ANY line it cannot express
# is present, which is what keeps it from silently eating it. Drop exactly that
# line by the path it invokes. Idempotent: a second run finds
# nothing to remove and leaves the crontab byte-identical.
#
# Read the crontab ONCE into a file and compare files. The first version keyed
# the whole migration on the exit status of `grep -v`, which is 1 when grep
# filters everything out -- a legitimate result (a crontab holding only the
# migrated lines and no MAILTO/MARLOW_* preamble), not a failure. On such a box
# the lines survived, neither branch printed, update.sh exited 0, and the renderer
# then refused for the life of the box behind a deploy that looked clean
# (/code-review medium, 2026-08-30).
CURRENT_CRONTAB="$(mktemp)"
MIGRATED_CRONTAB="$(mktemp)"
if crontab -u "$MARLOW_USER" -l > "$CURRENT_CRONTAB" 2>/dev/null; then
    grep -v -e '/opt/marlow/bin/identity-push\.py' "$CURRENT_CRONTAB" > "$MIGRATED_CRONTAB" || true
    if cmp -s "$CURRENT_CRONTAB" "$MIGRATED_CRONTAB"; then
        echo "   nothing to remove"
    else
        crontab -u "$MARLOW_USER" "$MIGRATED_CRONTAB"
        echo "   removed the identity-push line"
    fi
else
    echo "   no crontab installed yet; nothing to remove"
fi
rm -f "$CURRENT_CRONTAB" "$MIGRATED_CRONTAB"

# The schedules store (cron/schedules.json) is seeded by install.sh, from the
# identity template, before this script first runs. After that it is Marlow's
# to edit and nothing here writes it. Until 2026-10-26 this spot also seeded an
# absent store from one box's migrated crontab (2026-08-30); every box has had
# a store since, so the fallback only shipped that box's routine.
#
# The as-marlow note: every working-dir write in this script runs AS MARLOW,
# not as root.
# /security-review 2026-08-30: that seed was the one place root wrote into the
# agent-writable tree, and `install -d` follows a symlinked directory.
# gnulib's mkdir-p sets SAVEWD_CHDIR_NOFOLLOW only on the branch where mkdir
# SUCCEEDED, so an existing `cron` symlink means EEXIST, no NOFOLLOW, and
# dirchownmod applies owner+0755 to the link's TARGET -- `cron -> /etc/cron.d`
# would have had root hand that directory to marlow, then a dropped
# `* * * * * root ...` file. Dropping to the unprivileged user removes the
# primitive rather than blacklisting the shapes that reach it.
#
# Every later step reads this; under `set -u` it must be assigned before them.
WORKING_DIR="${MARLOW_WORKING_DIR:-/home/$MARLOW_USER/marlow-identity}"

echo "== installing the software's own rules (CLAUDE.md, FIRST-RUN.md) =="
# docs/tasks/box-upgrades.md. These two files are product code written in
# English: a rule like `[silent]` describes how the server behaves, and until
# this step a release that changed one reached no box seeded before it. The
# user's own rules live in PREFERENCES.md, which CLAUDE.md imports on its last
# line and which nothing here touches.
#
# Only on a box that HAS PREFERENCES.md. That file is the marker that the box
# was seeded with, or hand-converted to, the split layout. A box without it
# keeps a CLAUDE.md that is still partly the user's own (a v1 migration, or a
# template seed from before the split), and overwriting that would delete their
# rules. It gets a one-time hand conversion instead.
#
# As marlow, for the symlink reason in the as-marlow note above, and by rename
# rather than cp: cp onto a CLAUDE.md symlink writes through to its target,
# where `mv -T` replaces the link itself. The temp file comes from mktemp, not a
# fixed name: a fixed `.CLAUDE.md.new` could itself be a planted symlink, and cp
# would write through THAT (/code-review high, 2026-10-05). It sits in the same
# directory so the rename never crosses a filesystem.
# One function for both product-file steps, so the symlink handling above
# can't drift between them.
install_product_file() {
    local src="$1" dst="$2" label="$3" tmp
    if sudo -u "$MARLOW_USER" -H test ! -L "$dst" \
        && sudo -u "$MARLOW_USER" -H cmp -s "$src" "$dst"; then
        echo "   $label unchanged"
        return
    fi
    tmp="$(sudo -u "$MARLOW_USER" -H mktemp "$(dirname "$dst")/.$(basename "$dst").XXXXXX")"
    sudo -u "$MARLOW_USER" -H cp "$src" "$tmp"
    sudo -u "$MARLOW_USER" -H chmod 644 "$tmp"
    if ! sudo -u "$MARLOW_USER" -H mv -fT "$tmp" "$dst"; then
        # Don't leave the temp for the next turn's harness commit.
        sudo -u "$MARLOW_USER" -H rm -f "$tmp"
        echo "   could not install $label" >&2
        exit 1
    fi
    echo "   $label installed"
}
if sudo -u "$MARLOW_USER" -H test -f "$WORKING_DIR/PREFERENCES.md"; then
    for f in CLAUDE.md FIRST-RUN.md; do
        install_product_file "$REPO_DIR/identity/$f" "$WORKING_DIR/$f" "$f"
    done
else
    echo "   no PREFERENCES.md: this box predates the split, leaving CLAUDE.md alone"
    echo "   (one-time conversion: deploy/convert-identity.md)"
fi

echo "== installing the software's own job prompts (cron/<job>.md) =="
# docs/tasks/box-upgrades.md "Still open" #2, settled 2026-10-06. A shipped
# job's PROMPT is product code, like CLAUDE.md, so it is replaced on every
# update. Copy-if-missing would get a job onto an old box once and then freeze
# it there: the same day this was decided, the ideas job's prompt was revised
# (an `until` field, dates not weekdays), and a box that had copied the first
# version would never have seen the second. That is the bug the identity split
# fixed for CLAUDE.md, one directory down.
#
# Whether the job RUNS is the user's: its cron/schedules.json entry, which only
# a run-once migration (below) ever adds. A prompt with no entry is inert, so
# installing it here never brings back a job the user deleted. A shipped prompt
# reads what it needs to know about the user (who is close, what is live) from
# USER.md and memory, so nothing personal lives in it. It is write-denied to
# the agent (boundaries.PRODUCT_OWNED_PATHS) so an edit can't be silently lost
# at the next deploy.
#
# Only the jobs listed here. morning.md is seeded as a STARTER and is the
# user's after that -- what goes in a morning brief is personal, and Mike's is
# four times the template's length. A job joins this list when it ships; its
# seed copy in identity/cron/ is then the live one. consolidate.md joined when
# the harness took over running it (docs/tasks/default-infrastructure.md):
# this overwrites any copy the user's Marlow had edited.
#
# On every box, not only converted ones: on a box that predates the job there
# is no file to lose, and the migration that schedules it needs it present.
SHIPPED_JOBS=(email-flash ideas consolidate)
sudo -u "$MARLOW_USER" -H mkdir -p "$WORKING_DIR/cron"
# The ideas job writes ideas/ideas.json; don't make its first night depend on
# the CLI's Write creating a missing directory.
sudo -u "$MARLOW_USER" -H mkdir -p "$WORKING_DIR/ideas"
for job in "${SHIPPED_JOBS[@]}"; do
    install_product_file "$REPO_DIR/identity/cron/$job.md" "$WORKING_DIR/cron/$job.md" "cron/$job.md"
done

echo "== running data migrations (deploy/migrations, once each) =="
# docs/tasks/box-upgrades.md step 3. A feature that ships switched on needs a
# change to a store the agent owns -- email-flash is an entry in
# cron/schedules.json -- and the seed only reaches new boxes. A migration is
# the database answer: numbered, run in order, and run ONCE. Once is the part a
# plain overwrite can't do: a user who deletes email-flash afterwards must not
# get it back on the next update.
#
# The record of what ran is root-owned and outside the working dir, so the
# agent can't rewrite it and re-run (or skip) a migration. The migrations
# themselves run AS MARLOW, for the symlink reason in the as-marlow note above:
# they write into the agent-writable tree, so they get no more privilege than
# the agent has.
#
# A fresh install records nothing and runs everything, which is safe because
# every migration must leave a freshly seeded box unchanged
# (tests/test_migrations.py holds each one to that).
#
# Exit 0 is recorded (applied, or nothing to do); 75 is "not now", e.g. the
# agent's store is mid-edit and invalid, and runs again next update; anything
# else stops the deploy before the restart, with the old process still serving.
# Per instance: a migration applied to marlow's working dir says nothing
# about another user's. MIGRATIONS_RECORD is set near the top, because the
# rollback check reads it before anything on disk moves.
VENV_PY="$(dirname "$VENV_PIP")/python"
run_migrations() {
    local record="$1" dir="$2" migration name output status ran=0
    mkdir -p "$(dirname "$record")"
    touch "$record"
    for migration in "$dir"/[0-9][0-9][0-9][0-9]-*.py; do
        [[ -e "$migration" ]] || continue
        name="$(basename "$migration" .py)"
        if grep -qxF "$name" "$record"; then
            continue
        fi
        ran=1
        status=0
        output="$(sudo -u "$MARLOW_USER" -H "$VENV_PY" "$migration" \
            --working-dir "$WORKING_DIR" --repo "$REPO_DIR" 2>&1)" || status=$?
        [[ -n "$output" ]] && printf '%s\n' "$output" | sed "s/^/   $name: /"
        case "$status" in
            0) echo "$name" >> "$record" ;;
            75) echo "   $name: not recorded, runs again next update" ;;
            *)
                echo "   $name failed (exit $status); not recorded, stopping before the restart" >&2
                exit 1
                ;;
        esac
    done
    if [[ "$ran" -eq 0 ]]; then
        echo "   nothing new"
    fi
}
run_migrations "$MIGRATIONS_RECORD" "$REPO_DIR/deploy/migrations"

echo "== installing the staged web client (as $MARLOW_USER) =="
# Adjacent to the restart on purpose: from this rename until the new process is
# up, the old process serves the new client. That gap is the restart itself
# (seconds), not the root steps above. No-op when the build step skipped.
sudo -u "$MARLOW_USER" -H bash "$REPO_DIR/deploy/build-web.sh" install "$REPO_DIR"

funnel_before_restart
echo "== restarting $UNIT =="
systemctl restart "$UNIT"

# Pulls one string field out of the /health JSON without putting a JSON parser
# on the deploy path. Narrow on purpose: the only fields read here are a branch
# name and a short sha, so there is no escaping or nesting to get wrong. Prints
# nothing if the field is absent, which is what makes the caller below safe
# against a rollback to a server that predates those fields.
health_field() {
    printf '%s' "$2" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}

echo "== verifying /health =="
for _ in $(seq 1 10); do
    if HEALTH_BODY="$(curl -fsS --max-time 5 --unix-socket "$INTERNAL_SOCKET" http://localhost/health 2>/dev/null)"; then
        RUNNING_BRANCH="$(health_field git_branch "$HEALTH_BODY")"
        RUNNING_COMMIT="$(health_field git_commit "$HEALTH_BODY")"

        echo "OK: $UNIT is up and healthy after update"
        # After the health check: Funnel is only pointed at a server that has
        # proved it came up on the loopback bind.
        funnel_after_restart || exit 1

        # No git_commit in the payload means a server older than #48 -- a
        # rollback. Not an error; there is just nothing to report.
        if [[ -n "$RUNNING_COMMIT" ]]; then
            echo "    running: ${RUNNING_BRANCH:-unknown}@$RUNNING_COMMIT"

            # The reason the endpoint exists. /health reports what the PROCESS
            # imported; the line below reports what is on DISK. They diverge
            # exactly when a merge lands without a successful restart -- the
            # 2026-08-09 bug at line 63, where a step was silently skipped and
            # the run still reported success. Warn, do not fail: the deploy did
            # complete, and turning a stale-restart into a non-zero exit is a
            # behaviour change nobody has asked for. Read the warning.
            CHECKOUT_COMMIT="$(sudo -u "$MARLOW_USER" -H bash -c "cd '$REPO_DIR' && git rev-parse --short HEAD" 2>/dev/null || true)"
            if [[ -n "$CHECKOUT_COMMIT" && "$CHECKOUT_COMMIT" != "$RUNNING_COMMIT" ]]; then
                echo "WARNING: checkout is at $CHECKOUT_COMMIT but the process is running $RUNNING_COMMIT." >&2
                echo "         The restart did not pick up the merged code. Check: journalctl -u $UNIT -n 50" >&2
            fi
        fi
        exit 0
    fi
    sleep 1
done

echo "FAILED: $UNIT did not become healthy within 10s of restart — check: journalctl -u $UNIT -n 50" >&2
exit 1

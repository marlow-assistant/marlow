#!/usr/bin/env bash
# Put a TEST box back to just-installed, so first-run onboarding runs again
# without a reinstall (docs/tasks/friends-alpha.md item 9).
#
# Clears the conversation and every pairing, and resets the identity working
# dir to its seed commit: USER.md, schedules and memory all go back. The old
# server state is moved aside, not deleted. NEVER run this on a box someone
# uses; it is why the box's own hostname is required as confirmation.
#
# On the box, as root:
#   sudo ~marlow/marlow-server/scripts/reset-onboarding.sh <hostname> [--from-checkout]
# --from-checkout  then copy FIRST-RUN.md and CLAUDE.md from this checkout's
#                  identity/, to try wording pulled with update.sh.
# Then reload the client, pair again (deploy/pair-device.sh) and the greeting
# is waiting.
set -euo pipefail

if [ "${1:-}" != "$(hostname)" ]; then
    echo "This wipes Marlow's conversation, pairings and identity changes." >&2
    echo "To confirm, pass this box's hostname: $0 $(hostname) [--from-checkout]" >&2
    exit 2
fi
FROM_CHECKOUT=${2:-}

WD=/home/marlow/marlow-identity
DATA=/home/marlow/.marlow/data
REPO=/home/marlow/marlow-server
as_marlow() { sudo -u marlow "$@"; }

systemctl stop marlow-server
stash=/var/tmp/onboarding-reset-$(date +%Y%m%d-%H%M%S)
mkdir -p "$stash"
for f in "$DATA"/transcript.db* "$DATA"/session_state.json \
         "$DATA"/device_tokens.json "$DATA"/pairing_challenges.json \
         "$DATA"/onboarding.json; do
    if [ -e "$f" ]; then mv "$f" "$stash"/; fi
done
echo "old server state moved to $stash"

# The seed commit is the root: seed-from-v1.sh makes it, the harness commits
# every turn after it.
seed=$(as_marlow git -C "$WD" rev-list --max-parents=0 HEAD)
as_marlow git -C "$WD" reset -q --hard "$seed"
as_marlow git -C "$WD" clean -qfd
echo "identity reset to its seed commit ${seed:0:7}"

# The server re-renders the crontab after a turn, not at startup, so without
# this the last run's schedule (a moved briefing, a job whose prompt file the
# reset just deleted) keeps firing until the first message. With the
# service's own environment: the crontab carries MARLOW_CLAUDE_BIN from it, and
# without the unit's value it renders a bare `claude` cron can't find. The
# values are paths, so word-splitting the list is safe.
# shellcheck disable=SC2046
as_marlow env $(systemctl show -p Environment --value marlow-server) \
    bash -c "cd $REPO && /home/marlow/.venvs/marlow-server/bin/python -c \
    'from server.schedules import sync_after_turn; sync_after_turn()'"
echo "crontab re-rendered from the seed schedule"

if [ "$FROM_CHECKOUT" = "--from-checkout" ]; then
    as_marlow cp "$REPO/identity/FIRST-RUN.md" "$REPO/identity/CLAUDE.md" "$WD"/
    echo "copied FIRST-RUN.md and CLAUDE.md from $REPO (committed with the next turn)"
fi

systemctl start marlow-server
echo "done: reload the client and pair again (sudo -u marlow $REPO/deploy/pair-device.sh)"

#!/usr/bin/env bash
# One-time move of the working dir from ~/claude-assistant to ~/marlow-identity.
#
# The old name was from before the assistant was called Marlow; it was renamed
# for the open source cut (Mike, 2026-10-27). update.sh runs this as the box's
# user, with the server stopped, on every update; once a box has moved it finds
# nothing to do. Delete it, and its step in update.sh, once every box that
# predates the rename has run an update past it.
#
# Two things move, together or not at all:
#   ~/claude-assistant                          -> ~/marlow-identity
#   ~/.claude/projects/-home-<user>-claude-assistant -> ...-marlow-identity
# The second is the CLI's session history, which it files under the working
# dir's path. Left behind, --continue and --resume find nothing at the new path
# and every conversation starts cold, with no error anywhere.
#
# Refuses, moving nothing, when a target already exists: two identities to
# merge is a person's call, and a half-done move is worse than none.
# Plain mv, not -T: the targets were just checked absent, and the home is not
# writable by the agent (ProtectHome), so nothing can appear in between.
#
#   --dry-run   check only; exit 1 if the move would refuse, 0 otherwise
#
# Exit 0 when moved or nothing to move, 1 when refusing.
set -euo pipefail

DRY_RUN=0
case "${1:-}" in
    "") ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

OLD="$HOME/claude-assistant"
NEW="$HOME/marlow-identity"
# The CLI's key for a project: its path with every non-alphanumeric a dash.
OLD_SESSIONS="$HOME/.claude/projects/${OLD//[^a-zA-Z0-9]/-}"
NEW_SESSIONS="$HOME/.claude/projects/${NEW//[^a-zA-Z0-9]/-}"

if [[ ! -e "$OLD" ]]; then
    exit 0
fi
for target in "$NEW" "$NEW_SESSIONS"; do
    if [[ -e "$target" || -L "$target" ]]; then
        echo "refusing to rename the working dir: $OLD is still here and $target already exists." >&2
        echo "Merge or remove one by hand, then rerun update.sh." >&2
        exit 1
    fi
done
if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "   would move $OLD to $NEW"
    exit 0
fi

mv "$OLD" "$NEW"
echo "   moved $OLD to $NEW"
if [[ -e "$OLD_SESSIONS" ]]; then
    mv "$OLD_SESSIONS" "$NEW_SESSIONS"
    echo "   moved the CLI's session history with it"
fi

# A script or note of the assistant's own that names the old path breaks
# quietly, in any spelling: absolute, ~/ or $HOME/. Rewriting the assistant's
# files is not this script's call, so say where they are.
if stale="$(grep -rlE --exclude-dir=.git '(~|\$HOME|/home/[^/]+)/claude-assistant' "$NEW" 2>/dev/null)"; then
    echo "   these files still name ~/claude-assistant; point them at $NEW:"
    sed 's/^/     /' <<<"$stale"
fi

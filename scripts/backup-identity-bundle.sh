#!/usr/bin/env bash
# Pull a `git bundle` of the identity working dir off the box, onto this Mac.
#
# Run this FROM MIKE'S MAC, not on the box. That direction is the entire point.
#
# `deploy/identity-push.py` gives Marlow a deploy key with write access, which
# means the agent can also destroy the GitHub side of its own backup (a force
# push, or a delete). That was the one real objection to handing over the key, and
# this script is the answer to it: a bundle is a single file on a machine the box
# cannot reach and holds no credential for. Nothing on the box can revoke it.
#
# So this is not belt-and-braces. It is the load-bearing half of the backup story,
# and the hourly push is the convenient half.
#
# A bundle is a complete, self-contained clone of every ref -- `git clone
# marlow-identity-2026-08-09.bundle restored/` gives back the full history,
# offline, with no server involved.
#
# Usage:
#   scripts/backup-identity-bundle.sh
#   MARLOW_BUNDLE_DIR=/Volumes/backup scripts/backup-identity-bundle.sh
#
# Scheduled daily by scripts/install-backup-launchd.sh (with a staleness
# notification); running it by hand is still fine. It only replicates when the laptop is on and the tailnet is up; since the box
# commits locally every turn, a missed day is delay, not loss.
set -euo pipefail

BOX="${MARLOW_BOX_SSH:-marlow@marlow}"
REMOTE_DIR="${MARLOW_BOX_WORKING_DIR:-\$HOME/marlow-identity}"
BUNDLE_DIR="${MARLOW_BUNDLE_DIR:-$HOME/backups/marlow-identity}"
KEEP="${MARLOW_BUNDLE_KEEP:-14}"

mkdir -p "$BUNDLE_DIR"
# Absolute from here on: the verify step below runs git with -C elsewhere, so a
# relative MARLOW_BUNDLE_DIR would silently resolve against the wrong directory.
BUNDLE_DIR="$(cd "$BUNDLE_DIR" && pwd)"
stamp="$(date +%F)"
out="$BUNDLE_DIR/marlow-identity-$stamp.bundle"
tmp="$out.partial"
probe=""
fail_reason=""

# ONE cleanup path, armed before anything is written. Every failure between here
# and the `mv` must delete the .partial and say so: with `set -e`, the two explicit
# checks below are not the only ways out -- a failing `mv` (full disk) or `git
# rev-list` would otherwise abort the script mid-stream, leaving debris the prune
# loop does not match and whose last line of output was reassuring. A partial file
# that survives is the failure mode this script's whole design is against, so the
# cleanup cannot live only in the paths we thought of.
cleanup() {
    status=$?
    [ -n "$probe" ] && rm -rf "$probe"
    if [ -e "$tmp" ]; then
        rm -f "$tmp"
        echo "FAILED: ${fail_reason:-unexpected error (exit $status)} --" \
             "no bundle kept for $stamp" >&2
    fi
}
trap cleanup EXIT
# So an interrupted transfer exits normally and runs the trap exactly once.
trap 'exit 130' INT

echo "== bundling $BOX:$REMOTE_DIR =="
# --all so every ref travels, not just the checked-out branch. Written to .partial
# and renamed only on success, so a truncated bundle that LOOKS like a backup is
# never left where a restore would find it.
if ! ssh -o BatchMode=yes "$BOX" "git -C $REMOTE_DIR bundle create - --all" > "$tmp"; then
    fail_reason="could not bundle the working dir on $BOX"
    exit 1
fi

# The check that makes this file trustworthy as a last resort, so a failure here
# deletes it rather than leaving something reassuring on disk.
#
# **It has to be a real clone, not `git bundle verify`.** Measured 2026-08-09:
# verify reads only the bundle's ref header, so it reports "is okay" and "records a
# complete history" for a bundle truncated to 200 bytes that then dies with
# `fatal: early EOF` on clone. Truncation is the failure this step exists for -- an
# ssh dropping mid-transfer, a full disk -- so verify alone would certify exactly
# the corruption it was there to catch. A bare clone runs index-pack over the whole
# pack and walks the history, which is the actual guarantee wanted: that the file
# can be turned back into the repo. Cheap, because the brain is a few MB of text.
#
# Also note verify must run INSIDE a repository at all ("error: need a repository
# to verify a bundle"), so it needs a throwaway one regardless.
probe="$(mktemp -d)"

if ! git clone --bare --quiet "$tmp" "$probe/probe.git" 2>"$probe/err"; then
    cat "$probe/err" >&2
    fail_reason="bundle would not clone -- corrupt or truncated"
    exit 1
fi
# Refs listing only, for the log. The clone above is the check.
git -C "$probe/probe.git" bundle verify "$tmp" 2>/dev/null | sed -n '2,4p'

# Assigned, not inlined into the echo. A command substitution inside an argument
# does NOT trip `set -e` -- measured 2026-08-09: with this inlined, a failing
# rev-list printed its own `fatal:`, then "restored  commits", then declared
# success and kept the bundle. An assignment is a simple command, so -e sees it.
commits="$(git -C "$probe/probe.git" rev-list --all --count)"
echo "restored $commits commits in the check clone"

mv "$tmp" "$out"
echo "== ok: $out ($(du -h "$out" | cut -f1)) =="

# Prune oldest-first, keeping $KEEP. Held to the exact name pattern this script
# writes so a stray file in the directory is never a deletion candidate.
#
# No `mapfile`: macOS ships bash 3.2 and this script's whole point is running on
# the Mac, so a bash-4 builtin here would fail at exactly the wrong moment.
bundles=()
while IFS= read -r line; do
    bundles+=("$line")
done < <(find "$BUNDLE_DIR" -maxdepth 1 -name 'marlow-identity-*.bundle' | sort)

count=${#bundles[@]}
if [ "$count" -gt "$KEEP" ]; then
    i=0
    while [ "$i" -lt $((count - KEEP)) ]; do
        echo "pruning ${bundles[$i]}"
        rm -f "${bundles[$i]}"
        i=$((i + 1))
    done
fi

echo "restore with: git clone $out restored/"

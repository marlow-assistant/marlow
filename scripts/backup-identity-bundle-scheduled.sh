#!/usr/bin/env bash
# What launchd runs on the Mac: the bundle backup, then a check that it is fresh.
#
# The check is the reason this file exists. A backup is only ever missed at
# restore time, which is too late, so "build it when a missed one is felt" could
# never fire (Mike, 2026-10-14). A run that fails quietly looks exactly like no
# news; the newest bundle's age is what turns it into a notification.
#
# One failed run does NOT notify. The Mac is often off the tailnet or asleep, and
# the box commits locally every turn, so a missed day is delay, not loss. Only a
# newest bundle older than MAX_AGE_DAYS does -- a persistent failure (a broken key,
# a moved box) surfaces in days rather than at the next disaster.
#
# The notification is a macOS one because the Mac has no other channel:
# /internal/cron/message is loopback on the box (failure-alert-channel.md).
#
# Installed by scripts/install-backup-launchd.sh, which copies this and the
# backup script out of the checkout. Safe to run by hand.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BACKUP="${MARLOW_BACKUP_SCRIPT:-$HERE/backup-identity-bundle.sh}"
BUNDLE_DIR="${MARLOW_BUNDLE_DIR:-$HOME/backups/marlow-identity}"
MAX_AGE_DAYS="${MARLOW_BACKUP_MAX_AGE_DAYS:-3}"
LOG="${MARLOW_BACKUP_LOG:-$HOME/Library/Logs/marlow-identity-backup.log}"

notify() {
    if [ -n "${MARLOW_BACKUP_NOTIFY:-}" ]; then
        "$MARLOW_BACKUP_NOTIFY" "$1"
        return
    fi
    # The message goes in as an argument, never spliced into the AppleScript.
    osascript -e 'on run argv' \
              -e 'display notification (item 1 of argv) with title "Marlow backup" sound name "Basso"' \
              -e 'end run' "$1"
}

# Days between a YYYY-MM-DD stamp and today. BSD date first (the Mac), GNU second.
# Both ends at noon: BSD -j fills an unparsed time from the clock, and across a
# spring-forward the 23-hour day would floor the count one short.
days_since() {
    local then now
    then="$(date -j -f '%F %H' "$1 12" +%s 2>/dev/null || date -d "$1 12:00" +%s 2>/dev/null)" || return 1
    now="$(date -j -f '%F %H' "$(date +%F) 12" +%s 2>/dev/null || date -d "$(date +%F) 12:00" +%s)"
    echo $(( (now - then) / 86400 ))
}

mkdir -p "$(dirname "$LOG")"
{
    echo "--- $(date '+%F %T')"
    if "$BACKUP"; then
        echo "backup ok"
    else
        echo "backup FAILED (exit $?)"
    fi
} >>"$LOG" 2>&1

# Freshness from the date in the name, which the backup script writes and only a
# verified bundle gets (a failure deletes its .partial). An mtime could be touched
# by a copy or a restore and lie.
newest="$(find "$BUNDLE_DIR" -maxdepth 1 -name 'marlow-identity-????-??-??.bundle' 2>/dev/null \
          | sort | tail -n 1)"
if [ -z "$newest" ]; then
    echo "STALE: no bundle in $BUNDLE_DIR" >>"$LOG"
    notify "No identity backup on this Mac at all. Log: $LOG"
    exit 1
fi
stamp="$(basename "$newest" .bundle)"
stamp="${stamp#marlow-identity-}"
# A name the glob matched but date can't read (a hand-renamed copy) must not
# mask staleness by erroring its way past the comparison.
if ! age="$(days_since "$stamp")"; then
    echo "STALE: newest bundle name has no readable date: $newest" >>"$LOG"
    notify "Identity backup check can't read $newest. Log: $LOG"
    exit 1
fi
if [ "$age" -gt "$MAX_AGE_DAYS" ]; then
    echo "STALE: newest bundle is $stamp ($age days)" >>"$LOG"
    notify "Identity backup is $age days old (last $stamp). Log: $LOG"
    exit 1
fi
echo "fresh: newest bundle is $stamp ($age days)" >>"$LOG"

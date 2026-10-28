#!/usr/bin/env bash
# Install (or reinstall) the daily identity bundle backup as a launchd agent.
# Run on the Mac, from this checkout; the plist points at this checkout's path.
#
# Runs daily at 21:00 and at login. A run missed while the Mac slept happens on
# wake; one missed while it was off is covered by the staleness check in
# backup-identity-bundle-scheduled.sh.
#
#   scripts/install-backup-launchd.sh             # install / reinstall
#   scripts/install-backup-launchd.sh --uninstall
#
# The script itself is the program, not /bin/bash with it as an argument: macOS's
# "Background Items Added" notice names the program, and "bash can run in the
# background" reads like a blanket grant (Mike, 2026-10-14). It is per-plist
# either way; this just makes the notice say what it is.
set -euo pipefail

#
# The scripts are COPIED out of the checkout, and the plist runs the copy. Run
# from the checkout, a branch switch to one without them (other sessions share
# it) would make launchd fail to exec -- silently, since the notifier is inside
# the missing script. So re-run this after changing either script.
LABEL="com.marlow.identity-backup"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/marlow-identity-backup"
TARGET="gui/$(id -u)"

if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout "$TARGET/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    rm -rf "$DEST"
    echo "uninstalled $LABEL"
    exit 0
fi

mkdir -p "$DEST" "$(dirname "$PLIST")"
install -m 755 "$HERE/backup-identity-bundle.sh" "$HERE/backup-identity-bundle-scheduled.sh" "$DEST/"

# Written and linted beside the live plist, and swapped in only once valid, so a
# failed reinstall never leaves the job removed.
new="$PLIST.new"
cat >"$new" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DEST/backup-identity-bundle-scheduled.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>0</integer></dict>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/marlow-identity-backup.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/marlow-identity-backup.log</string>
</dict>
</plist>
EOF
plutil -lint "$new" >/dev/null
launchctl bootout "$TARGET/$LABEL" 2>/dev/null || true
mv "$new" "$PLIST"
launchctl bootstrap "$TARGET" "$PLIST"
echo "installed $LABEL -> $PLIST (runs now, then daily at 21:00)"
echo "runs: $DEST"
echo "log: $HOME/Library/Logs/marlow-identity-backup.log"

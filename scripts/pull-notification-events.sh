#!/usr/bin/env bash
# Print the notification service extension's run log from an attached iPhone,
# newest first: outcome, when, how long, and how many characters it displayed.
#
# The extension runs in another process on a locked phone with no console, and
# its failures all look the same from outside — the generic banner. This log
# (`NotificationDiagnostics`, Shared/NotificationShared.swift) is the only thing
# that tells `network_error`, `no_credential` and `http_401` apart. It used to
# be a section in the app's Settings; since 2026-09-30 it is pulled on demand.
# Content-free by design: never the message text or the token.
#
#   scripts/pull-notification-events.sh            # first available iPhone
#   scripts/pull-notification-events.sh <device>   # name, UDID or identifier
#
# The phone must be paired with this Mac (Xcode) and reachable; it need not be
# unlocked.
set -euo pipefail

device="${1:-}"
if [[ -z "$device" ]]; then
    list="$(mktemp)"
    trap 'rm -f "$list"' EXIT
    xcrun devicectl list devices --json-output "$list" >/dev/null
    device="$(python3 - "$list" <<'EOF'
import json, sys
devices = json.load(open(sys.argv[1]))["result"]["devices"]
for d in devices:
    if d.get("hardwareProperties", {}).get("platform") == "iOS" and \
       d.get("connectionProperties", {}).get("tunnelState") != "unavailable":
        print(d["identifier"]); break
EOF
)"
    [[ -n "$device" ]] || { echo "no available iPhone; pass one explicitly" >&2; exit 1; }
fi

out="$(mktemp -d)/events"
xcrun devicectl device copy from --device "$device" \
    --domain-type appGroupDataContainer --domain-identifier group.com.mikemason.Marlow \
    --source Library/notification-events --destination "$out" >/dev/null

python3 - "$out" <<'EOF'
import datetime, json, pathlib, sys
events = []
for path in pathlib.Path(sys.argv[1]).glob("*.json"):
    try:
        events.append(json.loads(path.read_text()))
    except (OSError, ValueError):
        pass
if not events:
    print("no extension runs recorded")
for e in sorted(events, key=lambda e: e["at"], reverse=True):
    when = datetime.datetime.fromtimestamp(e["at"]).strftime("%Y-%m-%d %H:%M:%S")
    chars = "" if e.get("bodyCharacters") is None else f"  {e['bodyCharacters']} chars"
    print(f"{when}  {e['outcome']:<24} {e['milliseconds']:>6} ms{chars}")
EOF

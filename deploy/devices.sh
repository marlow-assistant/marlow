#!/usr/bin/env bash
# Lists the paired devices, or revokes one (a lost phone). Run on the box as the
# instance's own user:
#
#   sudo -u marlow ~marlow/marlow-server/deploy/devices.sh
#   sudo -u marlow ~marlow/marlow-server/deploy/devices.sh revoke <fingerprint>
#
# Talks to the admin endpoint on the owner-only admin socket, like pair-device.sh.
# The fingerprint is eight hex digits of the token's hash: enough to tell
# devices apart, and useless as a credential. The token itself is never shown.
# A revoked device gets 401s from then on and drops back to the pairing screen.
set -euo pipefail

ADMIN_SOCKET="${MARLOW_ADMIN_SOCKET:-$HOME/.marlow/admin/admin.sock}"

call() {  # call METHOD PATH [BODY]
    local method="$1" path="$2" body="${3:-}"
    local args=(-sf --max-time 10 -X "$method" --unix-socket "$ADMIN_SOCKET" "http://localhost$path")
    [[ -n "$body" ]] && args+=(-H 'content-type: application/json' -d "$body")
    if ! curl "${args[@]}"; then
        echo "FAILED: could not reach $ADMIN_SOCKET." >&2
        echo "        Is marlow-server running, and are you running this as the instance's own user?" >&2
        return 1
    fi
}

case "${1:-list}" in
    list)
        call GET /internal/admin/devices | python3 -c '
import json, sys, time
devices = json.load(sys.stdin)["devices"]
if not devices:
    print("No paired devices.")
for d in sorted(devices, key=lambda d: d.get("paired_at") or 0):
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["paired_at"])) if d.get("paired_at") else "unknown"
    print("{}  {}  {}".format(d["fingerprint"], when, d["device_name"]))
'
        ;;
    revoke)
        FP="${2:-}"
        [[ "$FP" =~ ^[0-9a-f]{8}$ ]] || { echo "usage: devices.sh revoke <8-hex-digit fingerprint>" >&2; exit 2; }
        RESULT="$(call POST /internal/admin/devices/revoke "{\"fingerprint\": \"$FP\"}")"
        if printf '%s' "$RESULT" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("revoked") else 1)'; then
            echo "Revoked $FP."
        else
            echo "No single device matches $FP (nothing revoked). Run devices.sh to list them." >&2
            exit 1
        fi
        ;;
    *)
        echo "usage: devices.sh [list | revoke <fingerprint>]" >&2
        exit 2
        ;;
esac

#!/usr/bin/env bash
# Prints a pairing link, and a QR of it, for a new device. Run over SSH on the box:
#
#   sudo -u marlow ~marlow/marlow-server/deploy/pair-device.sh ["device name"]
#
# Asks the admin endpoint on the instance's owner-only admin socket (no device
# token needed: being able to connect means being this instance's user, which
# is the authorization; same as scripts/dev-server.sh `code`). Not the internal
# socket: a turn can reach that one (server/config.py, `admin_socket_path`). Run it as that
# user. Needs no root and writes nothing.
#
# The link carries a single-use invite in its #fragment, which a browser never
# sends, so it stays out of access logs and off the Funnel path
# (docs/tasks/funnel-ingress.md, step 1). Treat it like a password for ten
# minutes: anyone who opens it first becomes a paired device.
set -euo pipefail

DEVICE_NAME="${1:-web browser}"
ADMIN_SOCKET="${MARLOW_ADMIN_SOCKET:-$HOME/.marlow/admin/admin.sock}"
PUBLIC_PORT="${MARLOW_PUBLIC_PORT:-8443}"
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# This box's own tailnet name (MARLOW_TAILNET_NAME, else Tailscale itself). No
# baked-in default: a friend's box is not the original one, and a wrong URL is
# worse than a placeholder that says what to fill in.
TAILNET_NAME="$(tailnet_name)"
TAILNET_NAME="${TAILNET_NAME:-<this box>.<your-tailnet>.ts.net}"

# python3 does the JSON escaping (it is already a deploy dependency, see
# resolve-egress-ips.sh): a device name with quotes or backslashes must not be
# able to break out of the body.
BODY="$(python3 -c 'import json,sys; print(json.dumps({"device_name": sys.argv[1]}))' "$DEVICE_NAME")"

if ! RESPONSE="$(curl -sf --max-time 10 -X POST --unix-socket "$ADMIN_SOCKET" \
        "http://localhost/internal/admin/invite" \
        -H 'content-type: application/json' -d "$BODY")"; then
    echo "FAILED: could not get a pairing invite from $ADMIN_SOCKET." >&2
    echo "        Is marlow-server running (systemctl status marlow-server), and are you" >&2
    echo "        running this as the instance's own user (sudo -u marlow ...)?" >&2
    exit 1
fi

INVITE="$(printf '%s' "$RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["invite"])')"
LINK="https://$TAILNET_NAME:$PUBLIC_PORT/#pair=$INVITE"

echo
# A QR needs a real name (one of the placeholder is worse than none) and
# qrencode, which install.sh installs but a box from before it may lack.
if [[ "$TAILNET_NAME" != "<"* ]] && command -v qrencode >/dev/null; then
    echo "    Scan this with your phone's camera (Android), or in the Marlow app (iPhone):"
    qrencode -t ansiutf8 -m 2 "$LINK"
    echo
fi
echo "    Or open this link on the device (a browser on a computer, or paste it into the iPhone app):"
echo
echo "    $LINK"
echo
echo "    Single use, valid for 10 minutes."
echo

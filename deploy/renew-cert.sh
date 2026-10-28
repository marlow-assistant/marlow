#!/usr/bin/env bash
# Renews the tailnet TLS cert for the public listener (deploy/README.md §8b).
# Let's Encrypt certs issued via `tailscale cert` last 90 days and are only
# renewed when re-run — there is no background renewal daemon. Installed as a
# systemd timer (marlow-cert-renew.timer) so the server doesn't die quietly
# three months after deploy.
#
# Must run as root: `tailscale cert` needs root to write the cert/key. It then
# chowns the output to marlow:marlow and locks the key down to 0600 so the
# marlow-server service (running as user marlow, ReadWritePaths=/home/marlow/.marlow)
# can read it.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# The node's own MagicDNS name, asked of tailscale rather than defaulted: this
# used to default to one specific box's name, which on any other host issues a
# request for a cert it cannot have.
TAILNET_NAME="$(tailnet_name)"
if [[ -z "$TAILNET_NAME" ]]; then
    echo "renew-cert.sh: could not read this node's tailnet name from 'tailscale status'" >&2
    echo "  (is tailscale up, with MagicDNS on?) -- or set MARLOW_TAILNET_NAME" >&2
    exit 1
fi
# One run per instance: a second instance (marlow-cert-renew@<user>) sets
# these for its own user, unit and tls dir. Both ask tailscale for the same
# node's cert, which it caches, so the second ask is not a second issuance.
TLS_OWNER="${MARLOW_TLS_OWNER:-marlow}"
[[ "$TLS_OWNER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "renew-cert.sh: bad MARLOW_TLS_OWNER" >&2; exit 1; }
CERT_DIR="${MARLOW_TLS_DIR:-/home/$TLS_OWNER/.marlow/tls}"
CERT_FILE="$CERT_DIR/marlow.crt"
KEY_FILE="$CERT_DIR/marlow.key"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "renew-cert.sh must run as root (tailscale cert needs it)" >&2
    exit 1
fi

# mkdir -p as root would leave a not-yet-existing parent (~/.marlow) owned
# root:root, and the chown below only recurses into $CERT_DIR — so fix the
# parent's ownership explicitly before creating the tls/ subdir under it, or
# marlow-server's own ensure_dirs() hits PermissionError on ~/.marlow/data on
# a freshly reprovisioned box where this script runs before marlow-server has
# ever started.
PARENT_DIR="$(dirname "$CERT_DIR")"
# Refused, not followed: the owner (and its agent) can write ~/.marlow, and
# root writing the key, chowning and chmodding through a planted symlink
# would land them wherever it points. The timer's sandbox already blocks
# that; an admin's rerun of install.sh, outside it, would not.
if [[ -L "$PARENT_DIR" || -L "$CERT_DIR" ]]; then
    echo "renew-cert.sh: $PARENT_DIR or $CERT_DIR is a symlink; refusing to write through it" >&2
    exit 1
fi
mkdir -p "$PARENT_DIR"
chown "$TLS_OWNER:$TLS_OWNER" "$PARENT_DIR"
mkdir -p "$CERT_DIR"

# `tailscale cert` is idempotent — it only re-issues near expiry, otherwise it
# rewrites the same cert. Hash before/after so a weekly no-op run doesn't
# bounce the service unnecessarily. (Known edge case, not worth guarding:
# this only hashes the cert, not the key — if a future Tailscale renewal ever
# rotated the key without changing the cert bytes, the restart would be
# skipped. Accepted as unlikely given tailscale cert issues cert+key together.)
before_hash=""
[[ -f "$CERT_FILE" ]] && before_hash="$(sha256sum "$CERT_FILE")"

log "renewing TLS cert for $TAILNET_NAME"
tailscale cert --cert-file "$CERT_FILE" --key-file "$KEY_FILE" "$TAILNET_NAME"

chown -R "$TLS_OWNER:$TLS_OWNER" "$CERT_DIR"
chmod 600 "$KEY_FILE"

log "renewed: $CERT_FILE / $KEY_FILE ($TLS_OWNER:$TLS_OWNER, key 0600)"

# Known edge case, not guarded: no flock against concurrent runs (a manual
# invocation overlapping the timer's active run). systemd's Type=oneshot
# already blocks timer-vs-timer overlap; a manual bypass racing it is
# low-likelihood and low-severity (worst case a redundant restart) since
# `tailscale cert` writes atomically.
after_hash="$(sha256sum "$CERT_FILE")"
if [[ "$before_hash" != "$after_hash" ]]; then
    # uvicorn loads the cert into memory once at startup and won't notice a
    # file swapped out from under it — restart so the new cert takes effect.
    UNIT="${MARLOW_SYSTEMD_UNIT:-marlow-server}"
    # A first issuance on a fresh box (deploy/install.sh) runs before the unit
    # is installed, and the server refuses to start without this cert -- so
    # there is nothing to restart yet, and it reads the cert when it starts.
    # Restarting a unit that does not exist is an error, which under `set -e`
    # would fail the install after the cert had in fact been issued.
    if systemctl cat "$UNIT" >/dev/null 2>&1; then
        systemctl restart "$UNIT"
        log "cert changed, restarted $UNIT to pick it up"
    else
        log "cert changed; $UNIT is not installed yet, nothing to restart"
    fi
else
    log "cert unchanged, no restart needed"
fi

#!/usr/bin/env bash
# Health-probe watchdog (plan §6/§10.2) — relaunches the service on wedge
# (process alive but not responding), which systemd's Restart=always alone
# can't catch (that only fires on process *death*).
#
# Self-safe: this script never touches its own process/timer, only the
# marlow-server systemd unit — it can't kill itself mid-run.
#
# Install as a systemd timer or cron entry, e.g. every 2 minutes:
#   */2 * * * * /usr/local/bin/marlow-watchdog.sh >> /home/marlow/.marlow/logs/watchdog.log 2>&1
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Hits the internal app on its socket, not the tailnet one — a wedge check
# shouldn't depend on tailscale also being healthy (see internal_get_health
# in server/app.py for why this route is duplicated there). Root runs this,
# and root can reach any instance's owner-only socket.
INTERNAL_SOCKET="${MARLOW_INTERNAL_SOCKET:-/home/${MARLOW_USER:-marlow}/.marlow/run/internal.sock}"
# Retired with the loopback port (2026-10-16). Refused, not ignored: a line
# still setting it would quietly probe the default instance instead.
if [[ -n "${MARLOW_HEALTH_URL:-}" ]]; then
    log "MARLOW_HEALTH_URL is retired; set MARLOW_INTERNAL_SOCKET (and MARLOW_USER) instead"
    exit 1
fi
TIMEOUT_SECONDS="${MARLOW_WATCHDOG_TIMEOUT:-10}"
UNIT="${MARLOW_SYSTEMD_UNIT:-marlow-server}"

if curl -fsS --max-time "$TIMEOUT_SECONDS" --unix-socket "$INTERNAL_SOCKET" \
        http://localhost/health >/dev/null 2>&1; then
    log "ok: $INTERNAL_SOCKET responded"
    exit 0
fi
# TRANSITIONAL (2026-10-16), for the one update that moves a box from the
# loopback port to the socket: update.sh installs this copy several steps
# before it restarts the server, and a slow update (pip, the web build) would
# otherwise see no socket, call the still-healthy old server wedged and
# restart it mid-update. Only when there is no socket at all, and only the old
# fixed port. Delete once every box (Mike's, the Android user's) has run that update.
if [[ ! -S "$INTERNAL_SOCKET" ]] \
    && curl -fsS --max-time "$TIMEOUT_SECONDS" http://127.0.0.1:8464/health >/dev/null 2>&1; then
    log "ok: no socket yet, but the pre-socket server answered on 127.0.0.1:8464"
    exit 0
fi

log "WEDGE DETECTED: $INTERNAL_SOCKET did not respond within ${TIMEOUT_SECONDS}s — restarting $UNIT"
sudo systemctl restart "$UNIT"
log "restart issued for $UNIT"

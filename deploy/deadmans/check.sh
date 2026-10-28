#!/usr/bin/env bash
# Dead-man's switch (plan §6/§10.2) — MUST run on a DIFFERENT host than the
# Marlow server. The whole point is that it still works when that server is
# down; an in-box alert path dies with the box it's alerting about.
#
# Curls /health over the tailnet, tracks the last time it succeeded in a
# state file, and fires notify.sh once the gap exceeds a threshold. Won't
# re-fire every run once it's already alerted for the current outage (tracks
# that in the same state file) — re-arms automatically once /health is
# healthy again.
#
# Install on the other host, e.g. cron every 15 min:
#   */15 * * * * /path/to/check.sh >> /var/log/marlow-deadmans.log 2>&1
set -euo pipefail

HEALTH_URL="${MARLOW_DEADMANS_HEALTH_URL:?set to the tailnet /health URL, e.g. https://marlow.<tailnet>.ts.net:8443/health}"
STATE_FILE="${MARLOW_DEADMANS_STATE_FILE:-$HOME/.marlow-deadmans-state}"
THRESHOLD_HOURS="${MARLOW_DEADMANS_THRESHOLD_HOURS:-2}"
NOTIFY_SCRIPT="${MARLOW_DEADMANS_NOTIFY_SCRIPT:-$(dirname "$0")/notify.sh}"

now=$(date +%s)

# /health needs a device token since funnel-ingress.md step 4, and this host
# has none and should not (a token here is a token to steal). A 401 is the
# server answering through the tailnet, which is the thing being watched, so
# 200 and 401 both count as up. A wedged server, a dead tailnet or a bad cert
# give no HTTP status at all (000), or a 5xx.
status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$HEALTH_URL" 2>/dev/null || true)"
if [[ "$status" == 200 || "$status" == 401 ]]; then
    echo "$now healthy" > "$STATE_FILE"
    exit 0
fi

last_healthy=$now
already_alerted=0
if [[ -f "$STATE_FILE" ]]; then
    read -r last_healthy status < "$STATE_FILE" || true
    [[ "$status" == "alerted" ]] && already_alerted=1
fi

down_hours=$(( (now - last_healthy) / 3600 ))

if (( down_hours >= THRESHOLD_HOURS )); then
    if (( already_alerted == 0 )); then
        "$NOTIFY_SCRIPT" "Marlow server unreachable for ~${down_hours}h ($HEALTH_URL)"
        echo "$last_healthy alerted" > "$STATE_FILE"
    fi
else
    echo "$last_healthy down" > "$STATE_FILE"
fi

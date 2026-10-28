# Shared helpers for deploy/*.sh scripts. Sourced, not executed directly.

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

# This node's own MagicDNS name ("marlow.example.ts.net"), or nothing if
# tailscale isn't up. MARLOW_TAILNET_NAME overrides it, for the day the name
# the phone uses differs from the node's own.
tailnet_name() {
    if [[ -n "${MARLOW_TAILNET_NAME:-}" ]]; then
        echo "$MARLOW_TAILNET_NAME"
        return
    fi
    tailscale status --json 2>/dev/null \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null \
        || true
}

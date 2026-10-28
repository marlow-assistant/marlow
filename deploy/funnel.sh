# Tailscale Funnel on or off for one instance (docs/tasks/funnel-ingress.md,
# step 6). Sourced by update.sh, never run directly; needs $UNIT, `unit_var`,
# `FUNNEL_ACTION` (on | off | empty = leave as is) from the caller.
#
# The setting lives in a systemd drop-in, MARLOW_FUNNEL=1, which is where this
# box already keeps per-instance settings (10-instance.conf): update.sh never
# rewrites it, so every later update keeps Funnel as it was. The server reads
# it to bind loopback and to know that loopback is intended (server/config.py).
#
# The order is the point. Funnel's TCP forward and uvicorn would both want
# <tailnet ip>:8443, so on the way up the server rebinds to loopback FIRST and
# Funnel starts SECOND; on the way down Funnel stops first. A failed step
# rolls back to the tailnet bind rather than leaving the phone with nothing,
# and nothing here touches sshd or SSH's route, so the box stays reachable.

FUNNEL_DROPIN_NAME=30-funnel.conf
FUNNEL_SYSTEMD_DIR="${FUNNEL_SYSTEMD_DIR:-/etc/systemd/system}"

# Funnel only listens on 443, 8443 and 10000; a second instance on another
# port can't be exposed this way.
FUNNEL_PORTS=" 443 8443 10000 "

funnel_dropin() { echo "$FUNNEL_SYSTEMD_DIR/$UNIT.service.d/$FUNNEL_DROPIN_NAME"; }

funnel_port() {
    local port
    port="$(unit_var MARLOW_PUBLIC_PORT)"
    [[ "$port" =~ ^[0-9]+$ ]] || port=8443
    echo "$port"
}

funnel_nodeattr_help() {
    local node_id
    node_id="$(funnel_self_field ID)"
    node_id="${node_id:-THIS-MACHINE-ID}"
    cat >&2 <<EOF
This tailnet's policy doesn't let this box use Funnel yet. Either:

  - open this link and click Enable Funnel. One click, but it lets every
    machine on the tailnet use Funnel, which is fine if this box is the only
    one you'd run a server on:

        https://login.tailscale.com/f/funnel?node=${node_id}

  - or, to allow this box only: in the admin console (Access controls), add

        "nodeAttrs": [
            {"target": ["tag:marlow"], "attr": ["funnel"]}
        ]

    and tag this machine tag:marlow (needs a tagOwners entry for it).

Then run this again. Nothing has been changed. See deploy/INSTALL.md.
EOF
}

# One field of `tailscale status --json`'s Self, or a check on its capabilities.
funnel_self_field() {
    tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin)["Self"].get(sys.argv[1]) or "")
except Exception:
    pass' "$1"
}

# Whether the tailnet policy grants this node Funnel. Checked BEFORE the rebind:
# without the grant, `tailscale funnel` doesn't fail, it prints an approval link
# and waits, and by then the server was already on loopback, so the phone lost
# the box until someone clicked (seen on a test VM, 2026-10-24).
funnel_permitted() {
    tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    s = json.load(sys.stdin)["Self"]
except Exception:
    sys.exit(1)
caps = set(s.get("CapMap") or {}) | set(s.get("Capabilities") or [])
sys.exit(0 if "funnel" in caps else 1)'
}

# After the forward is off, Tailscale still keeps AllowFunnel for the port, and
# `funnel status` reads "Funnel on ... but no serve config": nothing is served,
# but the next `tailscale serve` handler on that port would be public at once.
# `funnel --https=<port> off` refuses ("handler does not exist"); only `reset`
# clears it, and that wipes the whole serve config, so only when nothing else
# is in it. Marlow serves its own TLS and puts nothing else there.
funnel_clear_leftover() {
    local config
    config="$(tailscale serve status --json 2>/dev/null)" || return 0
    if printf '%s' "$config" | python3 -c '
import json, sys
try:
    c = json.load(sys.stdin) or {}
except Exception:
    sys.exit(1)
sys.exit(0 if not (set(c) - {"AllowFunnel"}) else 1)'; then
        [[ "$config" == *AllowFunnel* ]] && tailscale funnel reset
    else
        echo "   (other serve config on this box; left as is, check 'tailscale funnel status')" >&2
    fi
    return 0
}

funnel_write_dropin() {
    local file
    file="$(funnel_dropin)"
    install -d -o root -g root -m 755 "$(dirname "$file")"
    printf '# Written by deploy/update.sh --funnel; --no-funnel removes it.\n[Service]\nEnvironment=MARLOW_FUNNEL=1\n' > "$file"
    chmod 644 "$file"
    systemctl daemon-reload
}

funnel_remove_dropin() {
    rm -f "$(funnel_dropin)"
    systemctl daemon-reload
}

# Before anything is changed: refuse what can't work.
funnel_preflight() {
    [[ -z "${FUNNEL_ACTION:-}" ]] && return 0
    command -v tailscale >/dev/null || { echo "refusing: --funnel and --no-funnel need the tailscale CLI" >&2; return 1; }
    if [[ "$FUNNEL_ACTION" == on ]] && [[ "$FUNNEL_PORTS" != *" $(funnel_port) "* ]]; then
        echo "refusing: Funnel can only serve ports 443, 8443 and 10000, and $UNIT uses $(funnel_port)." >&2
        return 1
    fi
    if [[ "$FUNNEL_ACTION" == on ]] && ! funnel_permitted; then
        funnel_nodeattr_help
        return 1
    fi
}

# Just before the restart, so the one restart picks the bind up.
funnel_before_restart() {
    case "${FUNNEL_ACTION:-}" in
        on)
            echo "== funnel: binding $UNIT to loopback =="
            funnel_write_dropin
            ;;
        off)
            echo "== funnel: stopping the Funnel forward, then rebinding $UNIT to the tailnet =="
            # Off first: until it is down, the relay still delivers strangers
            # to a port that is about to move. Failing to turn it off is not
            # fatal (it may simply not be on), but is said.
            tailscale funnel --tcp="$(funnel_port)" off \
                || echo "   (tailscale funnel off failed; carrying on, check 'tailscale funnel status')" >&2
            funnel_clear_leftover
            funnel_remove_dropin
            ;;
    esac
}

# After the restart and the health check passed. Returns non-zero on failure,
# having already put the box back on the tailnet bind.
funnel_after_restart() {
    [[ "${FUNNEL_ACTION:-}" == on ]] || return 0
    local port
    port="$(funnel_port)"
    echo "== funnel: forwarding the public name to 127.0.0.1:$port =="
    # The preflight checked the grant, but if Tailscale still asks for approval
    # it waits, not fails, with the server already on loopback; the timeout
    # turns that wait into the rollback below.
    if timeout 60 tailscale funnel --bg --tcp="$port" "tcp://127.0.0.1:$port"; then
        echo "OK: Funnel is on. Check: tailscale funnel status"
        echo "   A public address can take a few minutes to start answering."
        return 0
    fi
    echo "== funnel: failed (or still waiting for approval after 60 s); putting $UNIT back on the tailnet ==" >&2
    # A forward that got half set up would keep tailscaled on <tailnet ip>:$port,
    # and the server couldn't bind it again.
    tailscale funnel --tcp="$port" off >/dev/null 2>&1 || true
    funnel_clear_leftover
    funnel_remove_dropin
    systemctl restart "$UNIT"
    echo "   Check: tailscale funnel status. Nothing else was changed." >&2
    return 1
}

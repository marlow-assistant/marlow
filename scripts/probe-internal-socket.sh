#!/usr/bin/env bash
# Probe: is the internal API's Unix socket reachable by exactly the right
# things? (config.py `internal_socket_path`, docs/tasks/marlow-for-taylor.md)
#
# The socket replaced 127.0.0.1:8464 because loopback is open to every user
# on the box. What has to hold, and what this measures:
#
#   1. The instance's own hook, inside bwrap, can still connect. Otherwise
#      every tool that needs the phone is denied, as if the server were down.
#   2. Inside bwrap, nothing can create or remove entries in the socket's
#      directory. Otherwise a turn could swap in its own listener and answer
#      its own permission prompts.
#   3. Nothing inside can rename its way around that: the directory is a
#      mount point there, and ~ (where ~/.marlow lives) is read-only.
#   4. Another user on the box can't connect at all.
#   5. Nothing listens on the old 127.0.0.1:8464 any more.
#
# Checks 1-3 run under the unit's own sandbox properties (systemd-run) and the
# same bwrap flags as invoke._confinement_prefix, because ProtectHome is part
# of the guarantee and a plain shell doesn't have it. Every check is
# NON-DESTRUCTIVE: it tries to create a scratch file, never to remove the
# socket or move a directory, so a surprise pass can't break the live server.
# There is a control: the same create without the read-only bind must SUCCEED,
# or a "fail" above proves nothing.
#
# RUN ON THE BOX as an admin with sudo (no claude call, no cost):
#
#   scp scripts/probe-internal-socket.sh mike@marlow:/tmp/
#   ssh -t mike@marlow sudo bash /tmp/probe-internal-socket.sh --standalone   # before deploying
#   ssh -t mike@marlow sudo bash /tmp/probe-internal-socket.sh                # after update.sh
#   ssh -t mike@marlow sudo bash /tmp/probe-internal-socket.sh taylor         # hers, later
#
# --standalone touches nothing the running server uses: it starts a throwaway
# listener as the instance user in a temp dir under ~/.marlow (the same
# position as ~/.marlow/run, so ProtectHome and the bind behave the same),
# runs the same checks against it, and removes it. It skips check 5, since
# the old server still listens on 8464 until the deploy.
set -uo pipefail

STANDALONE=0
if [[ "${1:-}" == --standalone ]]; then STANDALONE=1; shift; fi
INSTANCE_USER="${1:-marlow}"
HOME_DIR="/home/$INSTANCE_USER"
RUN_DIR="$HOME_DIR/.marlow/run"
PASS=0; FAIL=0
ok()  { echo "  PASS  $*"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL  $*"; FAIL=$((FAIL + 1)); }

[[ "$(id -u)" -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
id "$INSTANCE_USER" >/dev/null 2>&1 || { echo "no user $INSTANCE_USER" >&2; exit 1; }

LISTENER_PID=""
cleanup() {
    [[ -n "$LISTENER_PID" ]] && kill "$LISTENER_PID" 2>/dev/null
    [[ "$STANDALONE" -eq 1 && -n "${RUN_DIR:-}" && "$RUN_DIR" == "$HOME_DIR/.marlow/probe-sock."* ]] \
        && rm -rf "$RUN_DIR"
}
trap cleanup EXIT
if [[ "$STANDALONE" -eq 1 ]]; then
    RUN_DIR="$(sudo -u "$INSTANCE_USER" mktemp -d "$HOME_DIR/.marlow/probe-sock.XXXXXX")" \
        || { echo "could not make a temp dir under $HOME_DIR/.marlow" >&2; exit 1; }
    # A stand-in for internal_app's /health: same socket shape, nothing else.
    sudo -u "$INSTANCE_USER" python3 -I -c '
import http.server, socketserver, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("content-length", "2"); self.end_headers()
        self.wfile.write(b"ok")
    def address_string(self): return "unix"
    def log_message(self, *a): pass
socketserver.UnixStreamServer(sys.argv[1], H).serve_forever()
' "$RUN_DIR/internal.sock" &
    LISTENER_PID=$!
    for _ in $(seq 1 50); do [[ -S "$RUN_DIR/internal.sock" ]] && break; sleep 0.1; done
fi
SOCK="$RUN_DIR/internal.sock"

echo "=== probe-internal-socket for $INSTANCE_USER$([[ $STANDALONE -eq 1 ]] && echo ', standalone') ==="
echo "--- the socket"
if [[ -S "$SOCK" ]]; then ok "$SOCK is a socket"; else bad "$SOCK missing: is the branch deployed and the unit running?"; fi
mode="$(stat -c '%a %U' "$RUN_DIR" 2>/dev/null)"
if [[ "$mode" == "700 $INSTANCE_USER" ]]; then ok "$RUN_DIR is 700, owned by $INSTANCE_USER"; else bad "$RUN_DIR is '$mode', want '700 $INSTANCE_USER'"; fi

echo "--- 4. another user can't connect"
if sudo -u nobody curl -fsS --max-time 5 --unix-socket "$SOCK" http://localhost/health >/dev/null 2>&1; then
    bad "user nobody reached the socket"
else
    ok "user nobody refused"
fi

echo "--- 5. the old loopback port is closed"
if [[ "$STANDALONE" -eq 1 ]]; then
    echo "  skip  standalone: the deployed server still listens on 8464"
elif curl -fsS --max-time 3 http://127.0.0.1:8464/health >/dev/null 2>&1; then
    bad "something still answers on 127.0.0.1:8464"
else
    ok "nothing on 127.0.0.1:8464"
fi

echo "--- 1-3. inside the unit's sandbox and bwrap"
# The inner script runs as the instance user. $1 is "guarded" (the real
# flags) or "control" (no read-only bind); $2 is the socket's directory.
INNER='
set -u
RUN_DIR="$2"; SOCK="$RUN_DIR/internal.sock"
probe="$RUN_DIR/.probe-$$"
if [ "$1" = guarded ]; then
    curl -fsS --max-time 5 --unix-socket "$SOCK" http://localhost/health >/dev/null 2>&1 \
        && echo "R1 PASS hook can connect" || echo "R1 FAIL hook cannot connect"
    if touch "$probe" 2>/dev/null; then rm -f "$probe"; echo "R2 FAIL created a file in $RUN_DIR"
    else echo "R2 PASS $RUN_DIR is read-only"; fi
    findmnt -rn -M "$RUN_DIR" >/dev/null 2>&1 \
        && echo "R3 PASS $RUN_DIR is a mount point (no rename)" || echo "R3 FAIL $RUN_DIR is not a mount point"
    if touch "$HOME/.probe-$$" 2>/dev/null; then rm -f "$HOME/.probe-$$"; echo "R3 FAIL ~ is writable, ~/.marlow could be renamed"
    else echo "R3 PASS ~ is read-only"; fi
else
    if touch "$probe" 2>/dev/null; then rm -f "$probe"; echo "C PASS control: writable without the bind"
    else echo "C FAIL control: not writable even without the bind, so R2 proves nothing"; fi
fi
'
run_inner() {
    local which="$1"; shift
    systemd-run --quiet --wait --pipe --collect \
        -p User="$INSTANCE_USER" -p Group="$INSTANCE_USER" \
        -p NoNewPrivileges=true -p PrivateTmp=true \
        -p ProtectSystem=strict -p ProtectHome=read-only \
        -p ReadWritePaths="$HOME_DIR/.marlow $HOME_DIR/.claude $HOME_DIR/marlow-identity" \
        -E HOME="$HOME_DIR" \
        bwrap --dev-bind / / --die-with-parent "$@" -- /bin/sh -c "$INNER" inner "$which" "$RUN_DIR" 2>&1
}
while read -r tag verdict rest; do
    case "$verdict" in
        PASS) ok "$rest" ;;
        FAIL) bad "$rest" ;;
        *) [[ -n "$tag$verdict$rest" ]] && echo "  ????  $tag $verdict $rest" ;;
    esac
done < <(run_inner guarded --ro-bind "$RUN_DIR" "$RUN_DIR"; run_inner control)

echo
echo "=== $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]

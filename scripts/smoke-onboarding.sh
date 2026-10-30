#!/usr/bin/env bash
# Smoke-test first-run onboarding on a TEST VM from the dev Mac, with no
# hands on the VM: deploy a branch, reset onboarding, pair a fake device and
# talk through setup over the real HTTP API, auto-allowing every approval.
# Prints each exchange with the onboarding step after it, and the record at
# the end (docs/tasks/onboarding-v2.md, "Testing in a real run").
#
#   scripts/smoke-onboarding.sh [--vm NAME] [--branch BRANCH] [--no-update] [--lines FILE]
#
# --vm        Multipass VM (default marlow-install-trial). Its hostname must
#             equal the name: reset-onboarding.sh wipes the box and takes the
#             hostname as confirmation. NEVER point this at a box someone uses.
# --branch    deploy this branch first, with update.sh (default: the branch
#             this checkout is on).
# --no-update skip the deploy and test what the VM already runs.
# --lines     the user's side, one message per line (default: a stock user
#             below). Lines are sent in order whatever Marlow asked, so a
#             mismatch is expected now and then; read the replies, not only
#             the record. Once the lines run out, "ok" is sent until setup
#             finishes or MAX_TURNS.
#
# The claude -p runs happen on the VM, under its own login: nothing nests in
# the calling session. Mail and calendar aren't connected on a test VM, so
# that path of stay-on-top is the one exercised.
set -euo pipefail

VM=marlow-install-trial
BRANCH=$(git -C "$(dirname "$0")/.." rev-parse --abbrev-ref HEAD)
UPDATE=1
LINES_FILE=
while [ $# -gt 0 ]; do
    case "$1" in
        --vm) VM=$2; shift 2 ;;
        --branch) BRANCH=$2; shift 2 ;;
        --no-update) UPDATE=; shift ;;
        --lines) LINES_FILE=$2; shift 2 ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
done

on_vm() { multipass exec "$VM" -- "$@"; }
REPO=/home/marlow/marlow-server

if [ "$(on_vm hostname)" != "$VM" ]; then
    echo "VM $VM's hostname isn't $VM; refusing to reset it." >&2
    exit 2
fi

if [ -n "$UPDATE" ]; then
    echo "== deploying $BRANCH to $VM"
    on_vm sudo "$REPO/deploy/update.sh" --branch "$BRANCH" 2>&1 | tail -3
fi

echo "== resetting onboarding"
on_vm sudo "$REPO/scripts/reset-onboarding.sh" "$VM" --from-checkout | grep -E "moved|copied|done"

# The server takes a moment to listen after the reset's start.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if LINK=$(on_vm sudo -u marlow "$REPO/deploy/pair-device.sh" "smoke test" 2>/dev/null \
            | grep -oE 'https://[^ ]+#pair=[A-Za-z0-9_-]+'); then
        break
    fi
    sleep 2
done
[ -n "${LINK:-}" ] || { echo "no pairing link from $VM" >&2; exit 1; }

if [ -n "$LINES_FILE" ]; then
    USER_LINES=$(cat "$LINES_FILE")
else
    USER_LINES='Call me Sam.
I keep forgetting to water the plants on the balcony.
Every Saturday at 9 in the morning.
Ok, I can see it under Schedule.
Remember that my bike was last serviced in September.
When was my bike last serviced?
Yes, keep both of those.
Sounds good.'
fi

echo "== talking through setup"
# Root on the VM, to read onboarding.json (marlow-only). Talks to the box's
# own public URL, so the request path is a real client's.
on_vm sudo python3 - "$LINK" "$USER_LINES" <<'PY'
import json, ssl, sys, time, urllib.request, uuid

link, lines = sys.argv[1], [l for l in sys.argv[2].splitlines() if l.strip()]
base, invite = link.split("/#pair=")
RECORD = "/home/marlow/.marlow/data/onboarding.json"
MAX_TURNS = 16
ctx = ssl.create_default_context()


def call(method, path, body=None, token=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def record():
    try:
        return json.load(open(RECORD))
    except (OSError, ValueError):
        return {}


def show(who, text):
    print(f"\n[{who}]\n{text.strip()}", flush=True)


token = call("POST", "/v1/pair", {"invite": invite, "device_name": "smoke test"})["device_token"]
time.sleep(2)
for row in call("GET", "/v1/transcript", token=token).get("rows", [])[::-1]:
    if row.get("role") == "assistant":
        show("marlow (greeting)", row.get("text", ""))

for n in range(MAX_TURNS):
    if record().get("finished"):
        break
    text = lines[n] if n < len(lines) else "ok"
    show("user", text)
    turn_id = call("POST", "/v1/message", {"text": text, "client_msg_id": str(uuid.uuid4())},
                   token)["turn_id"]
    started = time.time()
    while True:
        for p in call("GET", "/v1/permissions", token=token)["permission_requests"]:
            print(f"  (approval asked: {p['tool']}; allowing)", flush=True)
            call("POST", f"/v1/permission/{p['request_id']}", {"decision": "allow"}, token)
        turn = call("GET", f"/v1/turn/{turn_id}", token=token)
        if turn["status"] in ("done", "error", "merged"):
            break
        if time.time() - started > 300:
            print("  (turn still running after 5 minutes; giving up)")
            sys.exit(1)
        time.sleep(2)
    show("marlow", turn.get("result_text") or f"({turn['status']}: {turn.get('error')})")
    r = record()
    print(f"  -> step {r.get('step')}, turns {r.get('turns')}, outcomes {r.get('outcomes')}"
          + (f", rejected {r['rejected']}" if r.get("rejected") else "")
          + (f", capped {r['capped']}" if r.get("capped") else ""), flush=True)

print("\n== record\n" + json.dumps(record(), indent=2))
PY

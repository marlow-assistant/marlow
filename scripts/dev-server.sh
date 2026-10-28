#!/usr/bin/env bash
# Run marlow-server locally for client development: plain HTTP on 127.0.0.1,
# a fake `claude` (scripts/dev/fake_claude.py) instead of the real CLI, and a
# throwaway sandbox under .dev/ for every path the server writes. No model is
# called, no subscription is used, nothing outside .dev/ is touched.
#
#   scripts/dev-server.sh            start (public :8080, internal .dev/run/internal.sock,
#                                    admin .dev/admin/admin.sock)
#   scripts/dev-server.sh --reset    wipe .dev/ first (pairings, transcript)
#   scripts/dev-server.sh code       print a fresh pairing link (server running)
#
# Then pair: open the link, or POST /v1/pair {device_name, invite} ->
# device_token. Message tags that
# drive the fake are listed in scripts/dev/fake_claude.py (#tool, #approve,
# #long, #fail, #markdown).
#
# Never point this at a real box's data: MARLOW_ALLOW_PLAINTEXT is for
# loopback development only.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEV="$REPO/.dev"
PUBLIC_PORT="${MARLOW_DEV_PUBLIC_PORT:-8080}"
PY="$REPO/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

if [[ "${1:-}" == "code" ]]; then
  INVITE="$(curl -sf -X POST --unix-socket "$DEV/admin/admin.sock" "http://localhost/internal/admin/invite" \
    -H 'content-type: application/json' -d '{"device_name":"dev-client"}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["invite"])')"
  echo "http://localhost:$PUBLIC_PORT/#pair=$INVITE"
  exit 0
fi

if [[ "${1:-}" == "--reset" ]]; then
  rm -rf "$DEV"
fi

mkdir -p "$DEV"/{bin,data,logs,attachments,work/memory}
install -m 0755 "$REPO/scripts/dev/fake_claude.py" "$DEV/bin/claude"
install -m 0755 "$REPO/scripts/dev/fake_crontab.sh" "$DEV/bin/crontab"
[[ -f "$DEV/credentials.json" ]] || cat > "$DEV/credentials.json" <<'EOF'
{"claudeAiOauth": {"accessToken": "dev", "refreshToken": "dev",
 "expiresAt": 9999999999999, "refreshTokenExpiresAt": 9999999999999,
 "subscriptionType": "pro", "scopes": []}}
EOF
[[ -f "$DEV/work/SOUL.md" ]] || echo "Dev SOUL: a stand-in identity." > "$DEV/work/SOUL.md"
[[ -f "$DEV/work/USER.md" ]] || echo "Dev USER: a stand-in user." > "$DEV/work/USER.md"
[[ -f "$DEV/work/memory/note.md" ]] || echo "A dev memory entry." > "$DEV/work/memory/note.md"
if [[ ! -d "$DEV/work/.git" ]]; then
  git -C "$DEV/work" init -q
  git -C "$DEV/work" -c user.name=dev -c user.email=dev@localhost add -A
  git -C "$DEV/work" -c user.name=dev -c user.email=dev@localhost commit -qm init
fi

export MARLOW_CLAUDE_BIN="$DEV/bin/claude"
export MARLOW_CREDENTIALS_PATH="$DEV/credentials.json"
export MARLOW_DATA_DIR="$DEV/data"
export MARLOW_LOG_DIR="$DEV/logs"
export MARLOW_ATTACHMENTS_DIR="$DEV/attachments"
export MARLOW_WORKING_DIR="$DEV/work"
export MARLOW_SOUL_PATH="$DEV/work/SOUL.md"
export MARLOW_USER_PATH="$DEV/work/USER.md"
export MARLOW_MEMORY_DIR="$DEV/work/memory"
export MARLOW_PUBLIC_BIND_HOST=127.0.0.1
export MARLOW_PUBLIC_PORT="$PUBLIC_PORT"
export MARLOW_INTERNAL_SOCKET="$DEV/run/internal.sock"
export MARLOW_ADMIN_SOCKET="$DEV/admin/admin.sock"
export MARLOW_ALLOW_PLAINTEXT=1
export MARLOW_BWRAP_ENABLED=0
export MARLOW_IDLE_COMPACTION_ENABLED=0
# server/schedules.py runs plain `crontab`, found on PATH. First on PATH, the
# sandbox's fake keeps the table in .dev/ (scripts/dev/fake_crontab.sh says why).
export PATH="$DEV/bin:$PATH"
export MARLOW_DEV_CRONTAB_FILE="$DEV/crontab"
# No APNs key: pushes are skipped and logged, which is what a dev box wants.

echo "marlow dev server: http://127.0.0.1:$PUBLIC_PORT  (sandbox: $DEV)"
echo "pairing code:      scripts/dev-server.sh code"
cd "$REPO"
exec "$PY" -m server.app

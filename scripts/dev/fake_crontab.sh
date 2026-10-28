#!/usr/bin/env bash
# Stands in for `crontab` under scripts/dev-server.sh, which puts it first on
# PATH as $DEV/bin/crontab. The table lives in a file inside the sandbox, so
# the server's real render-and-install path runs without touching the
# developer's own crontab.
#
# Found 2026-10-23: once the harness seeded its own jobs the rendered
# table stopped being empty, and every turn ran the real `crontab -` against
# the Mac's user crontab. macOS blocked the write, server/schedules.py waited
# out its 30 s timeout while holding the work queue, and a fifth of the web
# e2e suite failed on replies stuck at "running". Had the write gone through,
# it would have replaced the developer's crontab.
#
# Only the two forms server/schedules.py uses are supported.
set -euo pipefail

table="${MARLOW_DEV_CRONTAB_FILE:?MARLOW_DEV_CRONTAB_FILE is not set; refusing to guess where the table goes}"

case "${1:-}" in
  -l)
    if [[ -f "$table" ]]; then
      cat "$table"
    else
      # The exact wording schedules.install() treats as an empty table.
      echo "no crontab for dev" >&2
      exit 1
    fi
    ;;
  -)
    # A temp file per call: the queue's sync and the preferences endpoint's
    # can run at once, and the real crontab installs each table whole.
    tmp="$(mktemp "$table.XXXXXX")"
    cat > "$tmp"
    mv "$tmp" "$table"
    ;;
  *)
    echo "fake crontab: unsupported arguments: $*" >&2
    exit 2
    ;;
esac

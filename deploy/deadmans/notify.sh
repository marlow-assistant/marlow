#!/usr/bin/env bash
# STUB — replace with a real out-of-band notification (SMS/email/push to a
# channel that doesn't depend on the Marlow server itself). Not decided in
# the plan which mechanism Mike wants; this just makes the failure loud and
# visible on stderr/syslog until that's chosen, rather than silently no-op.
set -euo pipefail
echo "MARLOW DEAD-MAN'S SWITCH: $*" >&2
logger -t marlow-deadmans "$*" 2>/dev/null || true

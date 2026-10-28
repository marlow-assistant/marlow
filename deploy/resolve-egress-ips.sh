#!/usr/bin/env bash
# nftables can't match on hostnames directly — DNS-backed allowlists need
# something to keep an nft set's IPs current. This resolves
# server.boundaries.EGRESS_ALLOWLIST and rewrites the `marlow_egress_allowed`
# nft set. Run periodically (e.g. every 15 min via cron/timer) since these
# IPs (especially Google's, Apple's) rotate.
set -euo pipefail

HOSTS=$(python3 -c "
import sys
sys.path.insert(0, '$(dirname "$0")/..')
from server.boundaries import EGRESS_ALLOWLIST
print(' '.join(EGRESS_ALLOWLIST))
")

IPS=()
for host in $HOSTS; do
    while read -r ip; do
        [[ -n "$ip" ]] && IPS+=("$ip")
    done < <(dig +short "$host" A)
done

if [[ ${#IPS[@]} -eq 0 ]]; then
    echo "resolve-egress-ips: resolved zero IPs, refusing to empty the live set" >&2
    exit 1
fi

{
    echo "flush set inet marlow_filter marlow_egress_allowed"
    printf 'add element inet marlow_filter marlow_egress_allowed { %s }\n' "$(IFS=,; echo "${IPS[*]}")"
} | sudo nft -f -

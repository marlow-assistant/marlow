#!/usr/bin/env bash
#
# spikes/cache-cadence.sh — how long does the --append-system-prompt cache
# actually survive between turns?
#
# WHY THIS EXISTS
#   gate1-results §8 established that a byte-identical --append-system-prompt
#   resupplied turn-to-turn cache-hits nearly free. But those turns ran
#   back-to-back, seconds apart. Real Marlow turns are minutes-to-hours apart,
#   and prompt cache entries expire. If the entry dies during a normal gap,
#   byte-stability buys nothing for that turn and the whole freeze-the-string
#   design (docs/tasks/cache-friendly-identity-injection.md) is hygiene rather
#   than an optimization.
#
#   This script walks a chain of --resume turns with widening gaps and prints
#   cache_creation / cache_read per turn, so the cliff is visible.
#
# RUN IT FROM A PLAIN TERMINAL — never from inside a Claude Code session. A
# nested `claude -p` inherits the harness env and gives false results (the
# reason server/invoke.py scrubs env at all). This script refuses to run if it
# detects it is nested.
#
# USAGE
#   ./spikes/cache-cadence.sh                     # default sweep, ~25 min
#   GAPS="0 60 300" ./spikes/cache-cadence.sh     # custom gaps, seconds
#   MEM_BYTES=20000 ./spikes/cache-cadence.sh     # smaller block, cheaper run
#   IDENTITY_DIR=~/marlow-identity ./spikes/cache-cadence.sh   # use real files
#
# COST NOTE: this burns real subscription usage — roughly one cold write of the
# block per cache miss. At the default 156KB (~40K tokens) and a 5-gap sweep,
# budget for a handful of full-size writes. Lower MEM_BYTES for a cheap dry run
# of the mechanics before spending the real one.

set -euo pipefail

if [[ -n "${CLAUDECODE:-}" || -n "${CLAUDE_CODE_ENTRYPOINT:-}" ]]; then
  echo "REFUSING: this looks like a nested Claude Code session." >&2
  echo "Run it from a plain terminal, or the results are meaningless." >&2
  exit 1
fi

# Match invoke.py's scrubbing, defensively.
for var in $(env | grep -oE '^(CLAUDE_CODE_[A-Z_]*|CLAUDECODE|AI_AGENT)=' | tr -d '='); do
  unset "$var" || true
done

command -v jq >/dev/null || { echo "need jq" >&2; exit 1; }

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
MEM_BYTES="${MEM_BYTES:-156000}"
GAPS="${GAPS:-0 90 240 420 720}"
PROMPT="${PROMPT:-Reply with exactly: OK}"

# ---------------------------------------------------------------- build block
# The block must be BYTE-IDENTICAL on every turn — that is the whole premise
# under test. Generated deterministically (no timestamps, no counters).
build_block() {
  if [[ -n "${IDENTITY_DIR:-}" ]]; then
    cat "$IDENTITY_DIR/SOUL.md" "$IDENTITY_DIR/USER.md" 2>/dev/null || true
    if [[ -d "$IDENTITY_DIR/memory" ]]; then
      # sorted, so the concatenation order is stable
      find "$IDENTITY_DIR/memory" -maxdepth 1 -type f | sort | while read -r f; do
        printf '## %s\n\n' "$(basename "$f")"
        cat "$f"
        printf '\n\n'
      done
    fi
    return
  fi

  printf '# SOUL\n\nSynthetic stand-in for SOUL.md.\n\n# USER\n\nSynthetic stand-in for USER.md.\n\n# MEMORY\n\n'
  local para="Marlow noted that the deploy host serves TLS on the tailnet name and that the work queue serializes every invocation so only one is ever in flight at a time. "
  local out="" i=0
  while (( ${#out} < MEM_BYTES )); do
    out+="## memory-entry-$(printf '%04d' $i)"$'\n\n'"$para$para$para"$'\n\n'
    (( i++ ))
  done
  printf '%s' "${out:0:$MEM_BYTES}"
}

BLOCK="$(build_block)"
# ${#BLOCK} counts *characters*, not bytes — identity markdown is UTF-8 and
# real files carry non-ASCII, so that undercounts what argv actually holds.
# argv is measured in bytes, so measure bytes.
BLOCK_BYTES="$(printf '%s' "$BLOCK" | LC_ALL=C wc -c | tr -d ' ')"

# argv sanity: the server passes this same string as a command-line argument.
ARG_MAX="$(getconf ARG_MAX 2>/dev/null || echo 262144)"
echo "block: ${BLOCK_BYTES} bytes (ARG_MAX on this host: ${ARG_MAX})"
echo "block sha: $(printf '%s' "$BLOCK" | shasum -a 256 | cut -c1-16)  <- must not change between runs"
if (( BLOCK_BYTES > ARG_MAX / 2 )); then
  echo "WARNING: block is over half of ARG_MAX — argv limits are a real ceiling" >&2
  echo "         on how big injected identity can get. Worth recording." >&2
fi

TOTAL_WAIT=0
for g in $GAPS; do TOTAL_WAIT=$(( TOTAL_WAIT + g )); done
echo "sweep gaps (s): $GAPS  → at least $(( TOTAL_WAIT / 60 )) min of waiting, plus turn latency"
echo
read -r -p "This spends real subscription usage. Continue? [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || exit 0

# ---------------------------------------------------------------- run a turn
OUT_JSON="$(mktemp)"
trap 'rm -f "$OUT_JSON"' EXIT

SESSION_ID=""
BASELINE_CREATE=""

run_turn() {
  local label="$1" gap="$2"
  local -a args=(
    "$CLAUDE_BIN" -p "$PROMPT"
    --append-system-prompt "$BLOCK"
    --output-format json
    --strict-mcp-config
  )
  [[ -n "$SESSION_ID" ]] && args+=(--resume "$SESSION_ID")

  local t0 t1
  t0=$(date +%s)
  "${args[@]}" </dev/null >"$OUT_JSON" 2>/dev/null || {
    echo "turn '$label' FAILED — inspect $OUT_JSON" >&2
    exit 1
  }
  t1=$(date +%s)

  local sid create read_ in out
  sid=$(jq -r '.session_id // empty' "$OUT_JSON")
  create=$(jq -r '.usage.cache_creation_input_tokens // 0' "$OUT_JSON")
  read_=$(jq -r '.usage.cache_read_input_tokens // 0' "$OUT_JSON")
  in=$(jq -r '.usage.input_tokens // 0' "$OUT_JSON")
  out=$(jq -r '.usage.output_tokens // 0' "$OUT_JSON")
  [[ -n "$sid" ]] && SESSION_ID="$sid"
  [[ -z "$BASELINE_CREATE" ]] && BASELINE_CREATE="$create"

  # HIT/MISS against the cold-write baseline from turn 1.
  local verdict="HIT"
  if (( BASELINE_CREATE > 0 )) && (( create * 5 > BASELINE_CREATE )); then
    verdict="MISS"
  fi

  printf '%-10s %7s %14s %12s %8s %8s %8s  %s\n' \
    "$label" "${gap}s" "$create" "$read_" "$in" "$out" "$(( t1 - t0 ))s" "$verdict"
}

printf '\n%-10s %7s %14s %12s %8s %8s %8s  %s\n' \
  TURN GAP CACHE_CREATE CACHE_READ INPUT OUTPUT WALL VERDICT
printf '%s\n' "--------------------------------------------------------------------------------------"

run_turn "cold" 0

n=1
for gap in $GAPS; do
  (( gap > 0 )) && sleep "$gap"
  run_turn "resume-$n" "$gap"
  (( n++ ))
done

cat <<'EOF'

READING THE TABLE
  Do NOT read the first MISS as "the expiry cliff." The 2026-07-25 run found
  there ISN'T a clean cliff: a 420s gap missed while a 720s gap hit. See
  gate1-results §8b for the full result and the working hypothesis (a ~5-minute
  guaranteed window, with longer retention opportunistic on cache capacity).

  What to actually look for:
    - HITs at 60-240s      — the conversational case. Confirmed working.
    - anything beyond ~5m  — treat as a bonus, never plan around it.
    - CACHE_READ on a MISS — the floor that never misses is the CLI's own
                             prefix segment, cached separately from our block.

  A monotonic sweep confounds gap length with elapsed session time. To test
  whether beyond-5m misses are random (capacity) or systematic (a boundary),
  re-run with a FLAT interval so the same gap repeats:

      GAPS="360 360 360 360 360" ./spikes/cache-cadence.sh

  Record results in docs/marlow-v2-gate1-results.md §8b.
EOF

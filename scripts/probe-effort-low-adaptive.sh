#!/usr/bin/env bash
# Probe: is `--effort low` a hard zero, or is it adaptive?
#
# `probe-effort-flag.sh` measured low producing exactly 0 thinking tokens on one
# hard prompt, and that was over-read as "low never thinks" -- which would mean
# prod (nonzero thinking) is inheriting medium. Mike's objection 2026-08-18: one
# prompt cannot establish that. If low is ADAPTIVE -- zero on easy turns,
# nonzero when the model judges it needs to -- then prod is plausibly already on
# low and there is nothing to correct.
#
# This runs low across a difficulty ladder. It is the same measurement either
# way; only the number of prompts changes.
#
# RUN THIS IN A RAW TERMINAL ON THE BOX, as the `marlow` user, NOT from a
# Claude Code session (CLAUDE.md hard rule -- nesting inherits the harness env).
#
#   ssh marlow@marlow
#   bash ~/marlow-server/scripts/probe-effort-low-adaptive.sh

set -uo pipefail

CLAUDE="${MARLOW_CLAUDE_BIN:-$HOME/.local/bin/claude}"
WORKDIR="${MARLOW_WORKING_DIR:-$HOME/marlow-identity}"

for v in $(env | grep -oE '^(CLAUDE_CODE_[A-Z_]*|CLAUDECODE|AI_AGENT)' || true); do
  unset "$v"
done
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1

cd "$WORKDIR" || exit 1

# Difficulty ladder. The last three deliberately look like real Marlow chat
# turns rather than puzzles -- if low is adaptive, the question that matters is
# whether it engages on MIKE's actual traffic, not on brainteasers.
run() { # $1 = label, $2 = effort, $3 = prompt
  "$CLAUDE" -p --effort "$2" --output-format json -- "$3" </dev/null \
    | python3 -c '
import json,sys
d=json.load(sys.stdin); u=d.get("usage",{}) or {}
det=u.get("output_tokens_details",{}) or {}
print("  %-9s %-7s thinking=%-6s output=%-6s ms=%s" % (
    sys.argv[1], sys.argv[2], det.get("thinking_tokens"), u.get("output_tokens"), d.get("duration_ms")))' \
    "$1" "$2" \
    || echo "  $1 $2 INVOCATION FAILED"
}

P_TRIVIAL='What is the capital of France?'
P_PUZZLE='Three switches downstairs control three bulbs upstairs, and you may go
up only once. Work out a method and explain why it is correct.'
P_HARD='A logistics network has 40 depots. Nightly, each depot ships to every
depot within 300km. Traffic doubles for 6 weeks each winter and one depot is
always offline for maintenance. Design a routing scheme that stays within 8
hours end to end, and argue for where it breaks first.'
P_PLAN='I have a dentist appointment Thursday at 2pm, a deadline Friday
morning, and my sister visits Wednesday through Friday. Help me think about how
to arrange the week.'
P_TRADEOFF='Should I move my home backups off the NAS and onto a cloud provider,
or keep both? Walk me through how you would decide.'
P_CHITCHAT='Morning. Anything I should know about today?'

printf '\n=== low across a difficulty ladder ===\n'
run trivial   low "$P_TRIVIAL"
run puzzle    low "$P_PUZZLE"
run hard      low "$P_HARD"
run plan      low "$P_PLAN"
run tradeoff  low "$P_TRADEOFF"
run chitchat  low "$P_CHITCHAT"

printf '\n=== same ladder at medium, for contrast ===\n'
run trivial   medium "$P_TRIVIAL"
run puzzle    medium "$P_PUZZLE"
run hard      medium "$P_HARD"
run plan      medium "$P_PLAN"
run tradeoff  medium "$P_TRADEOFF"
run chitchat  medium "$P_CHITCHAT"

cat <<'READ'

=== How to read this ===

If EVERY low row is thinking=0:
  low is a hard zero. Prod's nonzero thinking then means prod is NOT on low,
  the correction stands, and pinning low is a real behaviour change to decide
  on deliberately.

If SOME low rows are nonzero (expect the hard/tradeoff ones):
  low is adaptive. Prod is plausibly on low already, there is nothing to
  correct, and piece 1's pin is the no-op it was always meant to be.
  Mike's objection was right and the earlier probe was over-read.

Also worth reading: the medium column against the low column on the CHAT-SHAPED
prompts (plan / tradeoff / chitchat). That gap -- not the puzzle gap -- is what
choosing a chat default actually costs or buys.

Paste the whole output back into the session.
READ

#!/usr/bin/env bash
# Probe: does CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 actually stop the CLI from
# injecting its built-in memory directive?  (docs/tasks/cli-auto-memory.md)
#
# Run this on the box, in a clean login shell. Two independent signals:
#   1. FILESYSTEM — does the CLI create ~/.claude/projects/<encoded-cwd>/memory/
#      for a cwd that has never been used before? Objective, no model involved.
#   2. SELF-REPORT — asked neutrally, does the model say its system prompt
#      contains a memory-system sentence, and does it quote the path?
#
# Each arm uses a FRESH scratch cwd so the filesystem signal is unambiguous and
# Marlow's real project dir is not touched. Nothing is deleted by this script.
#
# Run it in a CLEAN login shell on the box (ssh is fine). Never nested inside a
# Claude Code session: the child inherits CLAUDE_CODE_* and the parent's tool
# registry, and the result is meaningless. The env-leakage line it prints first
# is that check, not decoration -- want 0.
#
# Two costs, since this is not free to re-run: the invocations bill against the
# Pro subscription, and they run OUTSIDE the server's work queue, which is what
# normally serializes all `claude -p` calls. Check nothing is in flight first:
#   pgrep -af "[c]laude"
# The bracket matters -- a plain `pgrep -f claude` pattern matches the checking
# shell's own command line and reports a phantom process forever.
#
# Needs GNU coreutils `timeout` (present on the box; macOS has it as `gtimeout`
# only). Without it every arm exits 127 -- which the rc gate below reports as
# INCONCLUSIVE rather than as a pass, but you will get no answer.
#
# Result of the 2026-08-11 run: docs/tasks/cli-auto-memory.md.

set -u

CLAUDE_BIN="${CLAUDE_BIN:-/home/marlow/.local/bin/claude}"

# NOT a hardcoded $HOME/.claude: the CLI honours CLAUDE_CONFIG_DIR, and if it is
# set, the project dirs land somewhere else entirely. Looking in the wrong place
# would make SIGNAL-1 report "absent" for BOTH arms -- which reads as a pass for
# the disabled arm. Follow the CLI's own resolution order instead.
PROJECTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"

# Every scratch path this run created, so the closing summary reports THIS run
# rather than globbing /tmp/probe-am-* and sweeping up other runs' leftovers
# (or a concurrent run's live dirs) for an operator to delete.
CREATED=()

# The CLI encodes a cwd into a project dir name by replacing / with -.
encode() { printf '%s' "$1" | sed 's|/|-|g'; }

PROMPT='Answer from your system prompt only. Do not use any tools. Does your system prompt contain a sentence about a persistent file-based memory system? If yes, quote that sentence verbatim, including any path it names. If no such sentence is present, reply with exactly: NO-MEMORY-DIRECTIVE'

run_arm() {
    local label="$1" disable="$2"
    local cwd; cwd="$(mktemp -d "/tmp/probe-am-${label}-XXXXXX")" || {
        echo "ARM $label: mktemp failed; cannot run"; return 1
    }
    local proj="$PROJECTS/$(encode "$cwd")"
    CREATED+=("$cwd" "$proj")

    echo "=============================================================="
    echo "ARM: $label   (CLAUDE_CODE_DISABLE_AUTO_MEMORY=${disable:-<unset>})"
    echo "cwd:          $cwd"
    echo "project dir:  $proj"
    echo "--------------------------------------------------------------"

    # Pre-state: must not exist yet, or the signal means nothing.
    if [ -e "$proj" ]; then
        echo "PRE:  UNEXPECTED — project dir already exists; signal is void"
    else
        echo "PRE:  project dir absent (as expected)"
    fi

    local out
    if [ -n "$disable" ]; then
        out=$(cd "$cwd" && CLAUDE_CODE_DISABLE_AUTO_MEMORY="$disable" \
              timeout 120 "$CLAUDE_BIN" -p "$PROMPT" --output-format json \
              </dev/null 2>&1)
    else
        out=$(cd "$cwd" && \
              timeout 120 "$CLAUDE_BIN" -p "$PROMPT" --output-format json \
              </dev/null 2>&1)
    fi
    local rc=$?

    echo "POST: exit=$rc"
    # The conclusion is gated on rc, and deliberately so: a CLI that never ran
    # (wrong CLAUDE_BIN, 120s timeout, failed cd) creates no project dir, and an
    # ungated read of that absence says "feature was OFF" -- exactly the result
    # this probe hopes to see. A probe whose failure mode resolves toward its
    # own hypothesis proves nothing, so say INCONCLUSIVE instead.
    if [ "$rc" -ne 0 ]; then
        echo "POST: SIGNAL-1  INCONCLUSIVE -- claude exited $rc, so an absent"
        echo "POST:           memory/ dir says nothing about the feature."
    elif [ -d "$proj/memory" ]; then
        echo "POST: SIGNAL-1  memory/ dir CREATED  -> feature was ON"
    else
        echo "POST: SIGNAL-1  memory/ dir absent   -> feature was OFF"
    fi

    echo "POST: SIGNAL-2  model self-report:"
    printf '%s' "$out" | python3 -c '
import json,sys
raw = sys.stdin.read()
try:
    print("      " + (json.loads(raw).get("result") or "<empty result>").replace("\n","\n      "))
except Exception:
    print("      <unparseable; raw head follows>")
    print("      " + raw[:600].replace("\n","\n      "))
'
    echo
}

echo "claude: $("$CLAUDE_BIN" --version 2>&1)"
# ANTHROPIC_ is in the pattern for the same reason invoke.py scrubs those three
# by name: a leaked ANTHROPIC_API_KEY silently moves billing off the Pro
# subscription and onto the API, which is the one outcome this project exists to
# prevent -- and it would not show up anywhere else in this output. (^CLAUDECODE
# is already covered by ^CLAUDE.)
echo "env leakage check: $(env | grep -cE '^(CLAUDE|AI_AGENT|ANTHROPIC_)') CLAUDE/AI/ANTHROPIC vars inherited (want 0)"
env | grep -E '^(CLAUDE|AI_AGENT|ANTHROPIC_)' | sed 's/=.*/=<redacted>/; s/^/  leaked: /'
echo

run_arm "baseline" ""
run_arm "disabled" "1"

echo "=============================================================="
echo "Scratch dirs THIS run created (nothing deleted). Safe to rm once read --"
echo "these are exact paths, not a glob, so a concurrent run is not swept up:"
# ${CREATED[@]+...} guard: under `set -u`, expanding an empty array is an
# unbound-variable error on bash < 4.4, which would kill the script at the
# finish line if both arms bailed early.
for d in ${CREATED[@]+"${CREATED[@]}"}; do
    [ -e "$d" ] && echo "  $d"
done

#!/usr/bin/env bash
# Probe: can `claude -p` run inside a bubblewrap namespace that makes the
# secret paths NOT EXIST, without breaking the CLI?
#
# WHY: server/bash_policy.py has now been caught four times across three
# /security-review passes, every time by the same structural fact -- the string
# it analysed was not the command the shell ran (brace expansion, newlines
# surviving shlex, globs, a raising `~user`). Statically predicting shell
# behaviour is a losing game. A kernel namespace does not care: if
# /home/marlow/.ssh is not in the mount table, no amount of expansion reaches
# it.
#
# It cannot cover everything, and that is the point of measuring rather than
# assuming. `.credentials.json` must stay visible -- the CLI authenticates with
# it -- so it keeps its logical guard in boundaries.py. Everything else
# (~/.ssh, ~/.marlow/{data,apns}) is needed by the SERVER, not by the CLI, and
# invoke.py is already the single choke point that spawns `claude -p`
# (CLAUDE.md), so it is the natural place to hand the CLI a narrower view of
# the filesystem than the server itself has.
#
# FOUR RUNS. Two of them are controls, for the same reason as
# probe-allow-vs-hook.sh: "the agent could not read the file" is worthless
# evidence if the CLI was broken, the prompt was refused, or the file was never
# readable in the first place. Each of those failure modes looks identical in
# the output.
#
#   0. bwrap smoke      -- does bubblewrap work here AT ALL? Ubuntu 24.04
#      restricts unprivileged user namespaces via AppArmor, which would fail
#      every run below for a reason that has nothing to do with the CLI.
#   1. canary control   -- read the canary WITHOUT bwrap. Proves the command,
#      the inline allow rule and the canary file all work.
#   2. CLI control      -- read a canary INSIDE the working dir, WITH bwrap.
#      Proves the CLI still authenticates and the Bash tool still functions in
#      the namespace. Without this, run 3 failing proves nothing.
#   3. the question     -- read the ~/.ssh canary, WITH bwrap.
#
# RUN THIS IN A CLEAN SHELL (plain ssh/terminal), NEVER inside a Claude Code
# session: a nested `claude -p` inherits the parent's env and toolset and gives
# false results. See CLAUDE.md.
#
# RUN IT AS `mike`, NOT `marlow`: marlow's live settings.json wires the real
# PreToolUse hook, which would POST to the server and push a permission request
# to Mike's phone. The canary below is written into mike's own ~/.ssh and
# removed on exit.
#
# Mirrors invoke.py's invocation shape (scrubbed env, --strict-mcp-config,
# stdin </dev/null) so the result is valid for the real code path.
set -uo pipefail

# Deliberately NOT $TMPDIR -- the CLI's own filesystem sandbox confines writes
# to the working directory (see probe-allow-vs-hook.sh).
PROBE_DIR="$(mktemp -d "${PWD}/.marlow-probe.XXXXXX")"
# Deliberately NOT ~/.ssh, which the first run of this probe used. The no-bwrap
# control FAILED there: the canary was unreadable even with `Bash(cat:*)`
# allowed inline and no namespace in play, so the CLI appears to protect that
# path itself. Good news, but it makes ~/.ssh useless as a measuring stick --
# "could not read" would have been the answer with or without bwrap, and run 3
# would have reported confinement that bwrap had nothing to do with.
#
# ~/.marlow is the right target: it holds real secrets (device_tokens.json, the
# APNs .p8 keys), it is covered by the tmpfs below, and its name means nothing
# to the CLI, so a failure to read it is attributable to the namespace alone.
CANARY_SECRET="$HOME/.marlow/marlow-probe-canary"
CANARY_TREE="$PROBE_DIR/tree-canary"
CANARY_TEXT="marlow-bwrap-probe-canary-value"

made_marlow_dir=no
[ -d "$HOME/.marlow" ] || { mkdir -p "$HOME/.marlow"; made_marlow_dir=yes; }

cleanup() {
  rm -f "$CANARY_SECRET"
  [ "$made_marlow_dir" = yes ] && rmdir "$HOME/.marlow" 2>/dev/null
  echo
  echo "canary removed; probe dir left for inspection: $PROBE_DIR"
}
trap cleanup EXIT

echo "$CANARY_TEXT" > "$CANARY_SECRET"
echo "$CANARY_TEXT" > "$CANARY_TREE"

# `cat` is allowed inline so the read is attempted rather than auto-denied --
# headless denies anything unallowed, which would make every run "fail" and
# look like confinement. This is the probe's own settings file, NOT the shipped
# policy: deploy/marlow-settings.json deliberately has no Bash rules at all.
cat > "$PROBE_DIR/settings.json" <<'EOF'
{ "permissions": { "allow": ["Bash(cat:*)"], "deny": [] } }
EOF

unset CLAUDECODE AI_AGENT ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
for v in $(env | grep -o '^CLAUDE_CODE_[A-Z0-9_]*' || true); do unset "$v"; done
for v in $(env | grep -o '^CLAUDECODE[A-Z0-9_]*' || true); do unset "$v"; done

# The confinement under test. --dev-bind / / keeps the box as it is, then the
# tmpfs mounts blank out exactly the paths the CLI does not need. ~/.claude is
# deliberately NOT hidden: the CLI authenticates from .credentials.json and
# writes a refreshed token back on every headless run (gate1-results §6), so
# hiding it would break the thing we are trying to protect.
BWRAP_ARGS=(
  --dev-bind / /
  --tmpfs "$HOME/.ssh"
  --tmpfs "$HOME/.marlow"
)

run_probe() {  # $1 = label, $2 = "bwrap"|"direct", $3 = path to read
  local label="$1" mode="$2" target="$3" rc
  echo "--- $label ($mode) ---"
  if [ "$mode" = bwrap ]; then
    bwrap "${BWRAP_ARGS[@]}" -- \
      claude -p --output-format json --strict-mcp-config \
        --settings "$PROBE_DIR/settings.json" \
        -- "Run exactly this shell command using the Bash tool, nothing else, no preamble: cat $target" \
      </dev/null >"$PROBE_DIR/$label.json" 2>"$PROBE_DIR/$label.err"
  else
    claude -p --output-format json --strict-mcp-config \
      --settings "$PROBE_DIR/settings.json" \
      -- "Run exactly this shell command using the Bash tool, nothing else, no preamble: cat $target" \
      </dev/null >"$PROBE_DIR/$label.json" 2>"$PROBE_DIR/$label.err"
  fi
  rc=$?   # captured before any other command can clobber $?
  local saw=no
  grep -q "$CANARY_TEXT" "$PROBE_DIR/$label.json" 2>/dev/null && saw=yes
  echo "exit=$rc  canary-value-in-output=$saw"
  [ "$saw" = yes ]
}

# --- run 0: does bubblewrap work on this box at all? --------------------------
echo "--- bwrap-smoke ---"
if ! command -v bwrap >/dev/null; then
  echo "INCONCLUSIVE: bwrap is not installed. \`sudo apt install bubblewrap\`,"
  echo "then re-run. Nothing below has been measured."
  exit 2
fi
if ! bwrap --dev-bind / / -- /bin/true 2>"$PROBE_DIR/smoke.err"; then
  echo "INCONCLUSIVE: bwrap is installed but cannot create a namespace here."
  sed 's/^/  /' "$PROBE_DIR/smoke.err"
  echo
  echo "Most likely unprivileged user namespaces are restricted -- Ubuntu 24.04"
  echo "ships an AppArmor profile that does this. Check:"
  echo "  sysctl kernel.apparmor_restrict_unprivileged_userns"
  echo "THIS IS ITSELF A RESULT: if it cannot be enabled for the marlow service,"
  echo "bwrap is not the confinement mechanism and bash_policy.py stays the only"
  echo "guard -- which changes the recommendation, not just this script."
  exit 2
fi
echo "ok: bwrap creates a namespace here."
echo

# --- runs 1-3 -----------------------------------------------------------------
run_probe canary-direct  direct "$CANARY_SECRET"; direct_saw=$?
run_probe tree-in-bwrap  bwrap  "$CANARY_TREE";   tree_saw=$?
run_probe secret-in-bwrap bwrap "$CANARY_SECRET"; secret_saw=$?

# --- verdict ------------------------------------------------------------------
echo
[ $direct_saw -eq 0 ] || {
  echo "INCONCLUSIVE: the canary was not readable even WITHOUT bwrap, so run 3"
  echo "proves nothing -- 'could not read' would be the default state."
  echo "Check $PROBE_DIR/canary-direct.json (look at permission_denials)."
  echo
  echo "If permission_denials is non-empty, the CLI refused the read on its own"
  echo "account, independently of allow rules and of any namespace. That is what"
  echo "happened the first time this probe ran, against a canary in ~/.ssh, and"
  echo "it is a finding in its own right -- but it makes the path useless as a"
  echo "measuring stick. Move the canary somewhere the CLI has no opinion about."
  exit 2
}
echo "control ok: the canary IS readable without bwrap."

[ $tree_saw -eq 0 ] || {
  echo "INCONCLUSIVE: inside bwrap the CLI could not read a file it was never"
  echo "denied. The namespace broke the CLI itself -- authentication, the Bash"
  echo "tool, or the working directory -- so run 3 failing says nothing about"
  echo "confinement. Check $PROBE_DIR/tree-in-bwrap.{json,err}; if it is auth,"
  echo "the tmpfs over \$HOME/.marlow may be shadowing something the CLI needs."
  exit 2
}
echo "control ok: inside bwrap the CLI still runs, authenticates and reads."

if [ $secret_saw -eq 0 ]; then
  echo
  echo "RESULT: the secret was STILL READABLE inside bwrap."
  echo "  -> the tmpfs mounts are not covering what we think they are. Check"
  echo "     whether \$HOME resolved the same inside the namespace."
  echo "  -> do NOT weaken bash_policy.py on the strength of this."
else
  echo
  echo "RESULT: CONFINED. The CLI runs normally but ~/.ssh and ~/.marlow do not"
  echo "exist for it or for any command it spawns."
  echo "  -> four of the five secret classes become unreachable regardless of"
  echo "     what a Bash command string expands to."
  echo "  -> .credentials.json stays visible by design (the CLI needs it) and"
  echo "     keeps its boundaries.py guard."
  echo "  -> bash_policy.py can then be demoted to what it is actually good at:"
  echo "     deciding what does not need a phone prompt. A miss costs a prompt,"
  echo "     not a breach."
  echo
  echo "NEXT: wire BWRAP_ARGS into server/invoke.py's spawn, behind a config"
  echo "flag, and re-run the full suite on the box."
fi

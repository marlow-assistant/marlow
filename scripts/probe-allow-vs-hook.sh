#!/usr/bin/env bash
# Probe: for a tool matched by `permissions.allow`, (a) does the PreToolUse hook
# still fire, and (b) does the hook's DECISION still bind?
#
# WHY THIS MATTERS: (a) and (b) are different questions and only (a) was ever
# answered. A hook that fires but whose `deny` is ignored for allow-matched tools
# is decorative -- every entry in permissions.allow would be unreachable by any
# check boundaries.py makes, and the allow list would have to carry the whole
# boundary on its own (prefix-match limits and all). The security-review of
# the first real permissions policy (2026-08-06) found `Bash(git diff:*)` reaching ~/.ssh via `git diff --no-index`, and
# the planned fix -- a path-token allowlist in hooks/pretooluse.py -- is only
# worth building if the answer to (b) is "yes, deny binds".
#
# Gate #1 §2-4 established that *fall-through* reaches the hook and that `ask`
# overrides a hook allow. Neither direction here.
#
# FIVE RUNS. The first three answer (a); runs 4-5 answer (b). Each question needs
# its own control, because the obvious minimal version can't tell "allow matched
# and the hook fired anyway" apart from "the allow rule never matched, so this
# was just another fall-through" -- identical in the hook log, and it would
# produce a confident, wrong answer:
#
#   1. no-hook control  -- proves the allow PATTERN actually matches. Headless
#      auto-denies anything unallowed, so if the command runs with no hook
#      configured at all, the allow rule is what permitted it.
#   2. matcher control  -- proves `"matcher": "*"` (the form the shipped policy
#      uses) fires at all, on a command that matches no allow rule.
#   3. question (a)     -- an allow-matched command, allow-returning hook wired.
#   4. deny control     -- proves the deny-returning hook actually denies, on a
#      command that matches no allow rule. Without this, run 5 "not created"
#      could just mean the deny hook is malformed and everything failed closed.
#   5. question (b)     -- an allow-matched command, DENY-returning hook wired.
#      Created => permissions.allow overrides the hook decision. Not created =>
#      the hook is a real backstop and the allowlist work has somewhere to live.
#
# The two hooks are separate files rather than one file switching on an env var:
# whether env reaches a hook child is its own untested assumption, and a silently
# unset variable would make run 5 an allow-hook run wearing a deny label.
#
# The allow rule below deliberately uses the same `Bash(verb:*)` syntax as
# deploy/marlow-settings.json. An earlier version used the exact-command form
# `Bash(touch /path/file)`, which tests a syntax the real policy never uses.
#
# RUN THIS IN A CLEAN SHELL (plain ssh/terminal), NEVER inside a Claude Code
# session: a nested `claude -p` inherits the parent's env and toolset and gives
# false results. See CLAUDE.md.
#
# RUN IT AS `mike`, NOT `marlow`: marlow's live settings.json wires the real
# PreToolUse hook, which would POST to the server and push a permission request
# to Mike's phone. mike has no PreToolUse hook, so the inline --settings below is
# the only one in play.
#
# Mirrors invoke.py's invocation shape (scrubbed env, --strict-mcp-config,
# stdin </dev/null) so the result is valid for the real code path.
set -uo pipefail

# Deliberately NOT $TMPDIR. Claude Code's filesystem sandbox confines writes to
# the working directory, so a probe file under /tmp is refused before any
# permission rule is consulted ("Blocked by the sandbox -- writes are limited
# to ...") and the no-hook control fails for a reason that has nothing to do
# with the allow pattern it is meant to test. Observed 2026-08-04.
#
# $PWD is not necessarily writable by the user running this: `sudo -u marlow
# bash probe.sh` from mike's shell keeps cwd at /home/mike. That happened
# 2026-08-06, and the failure was ugly out of proportion to the cause -- mktemp
# failed, the script carried on with PROBE_DIR unset, every path became
# "/hook-allow.py", and it printed five INCONCLUSIVE runs that looked like a
# real negative result. Fall back to $HOME, and refuse to continue if neither
# works rather than manufacturing evidence.
PROBE_BASE="${MARLOW_PROBE_BASE:-}"
if [ -z "$PROBE_BASE" ]; then
    if [ -w "$PWD" ]; then PROBE_BASE="$PWD"; else PROBE_BASE="$HOME"; fi
fi
if [ ! -w "$PROBE_BASE" ]; then
    echo "probe base '$PROBE_BASE' is not writable by $(whoami)." >&2
    echo "set MARLOW_PROBE_BASE to a directory this user owns." >&2
    exit 1
fi
PROBE_DIR="$(mktemp -d "$PROBE_BASE/.marlow-probe.XXXXXX")" || {
    echo "could not create a probe dir under '$PROBE_BASE'." >&2
    exit 1
}
# cd into it as well as writing into it: the sandbox confines writes to the
# WORKING directory, so a probe dir the script merely knows the path of is not
# enough if cwd is somewhere else entirely.
cd "$PROBE_DIR" || exit 1
OUT_DIR="$PROBE_DIR/out"
HOOK_LOG="$PROBE_DIR/hook-fired.log"
mkdir -p "$OUT_DIR" || exit 1
: > "$HOOK_LOG" || exit 1

cleanup() { echo; echo "probe dir left in place for inspection: $PROBE_DIR"; }
trap cleanup EXIT

# --- the hooks: log what they saw, then decide ---------------------------------
# Same body, different decision. The log line carries the decision so a reader of
# hook-fired.log can tell which hook produced each entry.
write_hook() {  # $1 = filename, $2 = permissionDecision
  cat > "$PROBE_DIR/$1" <<PYEOF
#!/usr/bin/env python3
import json, os, sys
try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    payload = {}
cmd = (payload.get("tool_input") or {}).get("command", "")
with open(os.environ["PROBE_HOOK_LOG"], "a") as fh:
    fh.write(f'{payload.get("tool_name","?")}\t$2\t{cmd}\n')
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "$2",
    "permissionDecisionReason": "probe: unconditional $2",
}}))
PYEOF
  chmod +x "$PROBE_DIR/$1"
}
write_hook hook-allow.py allow
write_hook hook-deny.py  deny

# --- settings files: identical allow, differing only in the hook ---------------
# `touch` is allowed via the colon form; `mkdir` is allowed by nothing, so it is
# the fall-through case.
cat > "$PROBE_DIR/settings-nohook.json" <<EOF
{
  "permissions": { "allow": ["Bash(touch:*)"], "deny": [] }
}
EOF

write_settings() {  # $1 = filename, $2 = hook script
  cat > "$PROBE_DIR/$1" <<EOF
{
  "permissions": { "allow": ["Bash(touch:*)"], "deny": [] },
  "hooks": {
    "PreToolUse": [
      { "matcher": "*",
        "hooks": [ { "type": "command", "command": "$PROBE_DIR/$2" } ] }
    ]
  }
}
EOF
}
write_settings settings-hook-allow.json hook-allow.py
write_settings settings-hook-deny.json  hook-deny.py

export PROBE_HOOK_LOG="$HOOK_LOG"
unset CLAUDECODE AI_AGENT ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
# Character class includes digits: `CLAUDE_CODE_V2_FLAG` would otherwise be
# truncated to `CLAUDE_CODE_V`, and unsetting that name leaves the real variable
# set in the child's environment -- silently breaking the "scrubbed env" claim
# this probe's validity rests on. invoke.py scrubs by prefix over all names.
for v in $(env | grep -o '^CLAUDE_CODE_[A-Z0-9_]*' || true); do unset "$v"; done
for v in $(env | grep -o '^CLAUDECODE[A-Z0-9_]*' || true); do unset "$v"; done

run_probe() {  # $1 = label, $2 = settings file, $3 = shell command to request
  local label="$1" settings="$2" command="$3" rc
  echo "--- $label ---"
  claude -p \
    --output-format json \
    --strict-mcp-config \
    --settings "$settings" \
    -- "Run exactly this shell command using the Bash tool, nothing else, no preamble: $command" \
    </dev/null >"$PROBE_DIR/$label.json" 2>"$PROBE_DIR/$label.err"
  rc=$?   # captured before any other command can clobber $?
  echo "exit=$rc  target-created=$([ -e "$OUT_DIR/$label" ] && echo yes || echo no)"
}

run_probe nohook-1      "$PROBE_DIR/settings-nohook.json"     "touch $OUT_DIR/nohook-1"
run_probe fallthrough-1 "$PROBE_DIR/settings-hook-allow.json" "mkdir $OUT_DIR/fallthrough-1"
run_probe allowed-1     "$PROBE_DIR/settings-hook-allow.json" "touch $OUT_DIR/allowed-1"
run_probe denyfall-1    "$PROBE_DIR/settings-hook-deny.json"  "mkdir $OUT_DIR/denyfall-1"
run_probe denyallow-1   "$PROBE_DIR/settings-hook-deny.json"  "touch $OUT_DIR/denyallow-1"

# --- verdict -------------------------------------------------------------------
echo
echo "=== hook log ==="
cat "$HOOK_LOG"
echo "================"

[ -e "$OUT_DIR/nohook-1" ] || {
  echo "INCONCLUSIVE: the allow rule 'Bash(touch:*)' did not permit the command"
  echo "with no hook configured. Until this run passes, nothing below can be"
  echo "trusted: an 'allowed' command that was never actually allowed is"
  echo "indistinguishable from a fall-through."
  if grep -qi 'sandbox' "$PROBE_DIR/nohook-1.json" 2>/dev/null; then
    echo
    echo "CAUSE: the CLI's filesystem SANDBOX refused the write, before any"
    echo "permission rule was consulted. That is a third enforcement layer,"
    echo "independent of permissions.allow/deny and of the PreToolUse hook."
    echo "This probe already writes under \$PWD to avoid it -- if you are still"
    echo "seeing this, the sandbox's writable root is narrower than the cwd."
  else
    echo "Check $PROBE_DIR/nohook-1.json -- look at permission_denials and result."
  fi
  exit 2
}

grep -q 'fallthrough-1' "$HOOK_LOG" || {
  echo "INCONCLUSIVE: a fall-through command never reached the hook, so"
  echo '"matcher": "*" does not fire. THIS IS ITSELF A RESULT: the shipped'
  echo "deploy/marlow-settings.json uses that matcher, and if it does not fire"
  echo "the box runs with no permission round-trip at all -- Known gap #7 again."
  echo "Check $PROBE_DIR/fallthrough-1.json, then try a per-tool matcher."
  exit 2
}

echo 'CONTROLS PASSED: the allow pattern matches, and "matcher": "*" fires.'
echo
echo "--- (a) does the hook FIRE for an allow-matched tool? ---"
if grep -q 'allowed-1' "$HOOK_LOG"; then
  echo "RESULT: hook FIRES for allowed tools."
else
  echo "RESULT: permissions.allow BYPASSES the hook entirely."
  echo "  -> every allow entry is a hole through boundaries.py's denies."
  echo "  -> the allow list must be minimal, and settings.json 'deny' has to carry"
  echo "     the secret-path rules on its own (prefix-match limits and all)."
  echo "  -> (b) below is moot: a hook that never runs has no decision to bind."
  exit 0
fi

# --- (b) does the hook's DECISION bind for an allow-matched tool? --------------
echo
echo "--- (b) does a hook 'deny' BIND for an allow-matched tool? ---"

grep -q 'denyfall-1' "$HOOK_LOG" || {
  echo "INCONCLUSIVE: the deny hook never appeared in the log, so it did not run"
  echo "at all on a fall-through. Nothing can be concluded from denyallow-1:"
  echo "'not created' would mean the hook is malformed and everything failed"
  echo "closed, not that the deny bound. Check $PROBE_DIR/denyfall-1.err."
  exit 2
}

[ -e "$OUT_DIR/denyfall-1" ] && {
  echo "INCONCLUSIVE: the deny hook RAN but the command was created anyway, on a"
  echo "plain fall-through. The hook's deny is not being honoured even in the"
  echo "case gate #1 already validated -- suspect the hookSpecificOutput schema"
  echo "has moved. Fix that before reading anything into denyallow-1."
  exit 2
}
echo "control ok: the deny hook denies a fall-through command."

if [ -e "$OUT_DIR/denyallow-1" ]; then
  echo "RESULT: permissions.allow OVERRIDES a hook deny."
  echo "  -> the hook fires but cannot refuse an allow-matched call: decorative."
  echo "  -> a path-token allowlist in hooks/pretooluse.py CANNOT constrain any"
  echo "     Bash verb that appears in permissions.allow. Do not build it there."
  echo "  -> the only lever left is shrinking the allow list itself."
else
  echo "RESULT: a hook deny BINDS over permissions.allow."
  echo "  -> boundaries.py is a real backstop under every allow entry."
  echo "  -> the allow list can be generous on convenience grounds, because the"
  echo "     hook gets the last word on every call. Build the allowlist there."
  echo "  NOTE: this also means the hook is consulted for allow-matched calls, so"
  echo "  hooks/pretooluse.py's POST-and-wait path runs for them too -- i.e. the"
  echo "  shipped policy would push a phone prompt for 'git status'. Confirm"
  echo "  against the live server before install; a vetted-allow fast path in the"
  echo "  hook (decide locally, never POST) is what keeps the allow list useful."
fi

#!/usr/bin/env bash
# Probe: under headless `claude -p`, can a PostToolUse hook on Edit/Write put a
# message in front of the model INSIDE THE SAME TURN -- and which of the three
# output shapes does it?
#
# WHY THIS MATTERS: a refused cron/schedules.json (or ideas/ideas.json) only
# reaches Marlow through the per-turn context block, i.e. on the NEXT turn
# (server/turn_context.py). The Edit itself reports success, so Marlow had
# "every reason to believe the job was live" (schedules.rejection_reason). If a
# PostToolUse hook can hand the validator's verdict straight back, Marlow fixes
# the file before the turn ends and `sync_after_turn` never sees a broken store.
# The answer is also general: any store the server validates could use it.
#
# THE TEST IS BEHAVIOURAL, not "does the text show up in a log". The model is
# asked to add an unsupported key to a schedule-shaped file. The hook acts like
# a validator: it reads the file and complains ONLY while the bad key is there.
# If the complaint reaches the model, it removes the key in the same turn and
# the hook fires a second time on a clean file. The model is told nothing about
# hooks or validation up front, so a fix can only come from the feedback.
#
# FOUR RUNS, one hook script each (separate files, never one script switching on
# env: whether env reaches a hook child is its own untested assumption --
# probe-allow-vs-hook.sh):
#
#   1. silent   -- CONTROL. The hook logs and says nothing. Proves (a) the hook
#                  fires on Edit/Write at all headless, and (b) the model leaves
#                  the bad key in when not told otherwise. If it removes the key
#                  anyway, runs 2-4 cannot be read: a fix there proves nothing.
#   2. block    -- stdout {"decision": "block", "reason": ...}
#   3. context  -- stdout {"hookSpecificOutput": {"hookEventName": "PostToolUse",
#                  "additionalContext": ...}}
#   4. exit2    -- reason on stderr, exit code 2.
#
# Each complaint carries a per-run nonce. If the nonce turns up in the stream
# (the model quoting it, or the CLI echoing the hook text into the transcript),
# that is supporting evidence of the channel; the verdict rests on the file.
#
# RESULTS 2026-10-19, CLI 2.1.301, on the box as mike:
#   - the hook fires on Edit under headless -p.
#   - ALL THREE shapes reach the model in the same turn; each run's final reply
#     paraphrased the complaint. context and exit2 led it to remove the key.
#     block led it to ask instead ("you asked for this key explicitly"): the
#     same message, carried with less weight -- a judgment, not a dead channel.
#   - the hook text never appears in the stream-json transcript (nonce absent),
#     so the server cannot read what the model was told from the stream; a
#     hook that wants it on the transparency sheet has to record it itself.
#
# RUN THIS IN A CLEAN SHELL (plain ssh/terminal), NEVER inside a Claude Code
# session: a nested `claude -p` inherits the parent's env and toolset and gives
# false results. See CLAUDE.md.
#
# RUN IT ON THE BOX AS `mike`, NOT `marlow`: hook behaviour is a property of the
# CLI version, so it has to be the version Marlow runs, and marlow's live
# settings.json wires the real PreToolUse hook, which would push permission
# prompts to Mike's phone. mike has none, so the inline --settings below is the
# only hook config in play.
#
# Mirrors invoke.py's invocation shape (scrubbed env, stream-json --verbose,
# stdin </dev/null). --strict-mcp-config is fine HERE (no connectors needed and
# it keeps startup quiet); it stays out of invoke.py for the reasons in CLAUDE.md.
set -uo pipefail

# Writable base, refusing to continue rather than manufacturing evidence -- see
# the 2026-08-06 note in probe-allow-vs-hook.sh for what happens otherwise.
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
cd "$PROBE_DIR" || exit 1
HOOK_LOG="$PROBE_DIR/hook-fired.log"
: > "$HOOK_LOG" || exit 1

cleanup() { echo; echo "probe dir left in place for inspection: $PROBE_DIR"; }
trap cleanup EXIT

# A non-interactive `ssh host 'cmd'` skips the login profile, so ~/.local/bin is
# not on PATH and every run exits 127 -- which the verdict below would otherwise
# read as "the hook never fired". Observed 2026-10-19.
command -v claude >/dev/null || PATH="$HOME/.local/bin:$PATH"
command -v claude >/dev/null || {
    echo "claude not found on PATH or in ~/.local/bin; nothing was probed." >&2
    exit 1
}
echo "claude: $(claude --version 2>&1)"
echo "user:   $(whoami)"
echo

# --- the hooks -------------------------------------------------------------------
# All four share the validator logic and the log line; only the way they speak
# differs. The nonce comes from the run's own working dir name, which the hook
# sees as its cwd -- no env needed.
write_hook() {  # $1 = mode (silent|block|context|exit2)
  cat > "$PROBE_DIR/hook-$1.py" <<PYEOF
#!/usr/bin/env python3
import json, os, sys
try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    payload = {}
path = (payload.get("tool_input") or {}).get("file_path", "")
bad = False
if os.path.basename(path) == "schedules.json":
    try:
        with open(path) as fh:
            bad = any("bogus" in e for e in json.load(fh).get("entries", []))
    except Exception:
        bad = True
nonce = "NONCE-" + os.path.basename(os.getcwd())
with open(os.environ["PROBE_HOOK_LOG"], "a") as fh:
    fh.write(f'$1\t{payload.get("hook_event_name","?")}\t{payload.get("tool_name","?")}\t{path}\tbad={bad}\n')
if not bad or "$1" == "silent":
    sys.exit(0)
reason = (
    "schedules.json is INVALID and the server is refusing it -- entry 'morning'"
    " contains unsupported keys: ['bogus']. Every schedule in the file is"
    " frozen until this is fixed: remove the key. (validator ref " + nonce + ")"
)
if "$1" == "block":
    print(json.dumps({"decision": "block", "reason": reason}))
elif "$1" == "context":
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": reason}}))
elif "$1" == "exit2":
    print(reason, file=sys.stderr)
    sys.exit(2)
PYEOF
  chmod +x "$PROBE_DIR/hook-$1.py"
}

export PROBE_HOOK_LOG="$HOOK_LOG"
unset CLAUDECODE AI_AGENT ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
# Digits in the class: see probe-allow-vs-hook.sh for the truncated-name bug.
for v in $(env | grep -o '^CLAUDE_CODE_[A-Z0-9_]*' || true); do unset "$v"; done
for v in $(env | grep -o '^CLAUDECODE[A-Z0-9_]*' || true); do unset "$v"; done

run_probe() {  # $1 = mode
  local mode="$1" dir="$PROBE_DIR/$1" rc
  mkdir -p "$dir" || exit 1
  write_hook "$mode"
  cat > "$dir/schedules.json" <<EOF
{
  "timezone": "America/Edmonton",
  "entries": [
    {"id": "morning", "cron": "0 8 * * *", "job": "morning"}
  ]
}
EOF
  # Edits are let through by --permission-mode acceptEdits below, not by an
  # allow rule: `Edit(/home/mike/...)` in this inline settings file still
  # auto-denied on the box (2026-10-19, CLI 2.1.301) -- a leading single `/`
  # is probably relative to the settings file, not the filesystem root.
  # Permissions are not the question here, so take them out of play.
  cat > "$dir/settings.json" <<EOF
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Edit|Write|MultiEdit",
        "hooks": [ { "type": "command", "command": "$PROBE_DIR/hook-$mode.py" } ] }
    ]
  }
}
EOF
  echo "--- $mode ---"
  ( cd "$dir" && claude -p \
      --output-format stream-json --verbose \
      --strict-mcp-config \
      --permission-mode acceptEdits \
      --settings "$dir/settings.json" \
      -- 'In schedules.json in the current directory, add the key "bogus": true to the entry whose id is "morning". Use the Edit tool.' \
      </dev/null >"$PROBE_DIR/$mode.jsonl" 2>"$PROBE_DIR/$mode.err" )
  rc=$?
  local fires still nonce
  fires=$(grep -c "^$mode	" "$HOOK_LOG" || true)
  still=$(grep -q bogus "$dir/schedules.json" && echo yes || echo no)
  nonce=$(grep -q "NONCE-$mode" "$PROBE_DIR/$mode.jsonl" && echo yes || echo no)
  echo "exit=$rc  hook-fires=$fires  bogus-still-in-file=$still  nonce-in-stream=$nonce"
}

for mode in silent block context exit2; do run_probe "$mode"; done

# --- verdict -----------------------------------------------------------------------
echo
echo "=== hook log ==="
cat "$HOOK_LOG"
echo "================"
echo

# A failed login still emits a result event, with is_error true and exit 1
# ("OAuth session expired", 2026-10-19), so the event alone proves nothing.
grep '"type":"result"' "$PROBE_DIR/silent.jsonl" 2>/dev/null | grep -q '"is_error":false' || {
  echo "INCONCLUSIVE: the silent CONTROL run did not complete cleanly, so the"
  echo "CLI itself failed (auth, PATH, crash) and nothing was probed:"
  grep '"type":"result"' "$PROBE_DIR/silent.jsonl" 2>/dev/null | grep -o '"result":"[^"]*"'
  echo "Read $PROBE_DIR/silent.jsonl and silent.err."
  exit 2
}
grep '"type":"result"' "$PROBE_DIR/silent.jsonl" | grep -q '"permission_denials":\[\]' || {
  echo "INCONCLUSIVE: the silent CONTROL run's edit was DENIED by permissions,"
  echo "so the hook never had an edit to see. Read permission_denials in"
  echo "$PROBE_DIR/silent.jsonl."
  exit 2
}
grep -q "^silent	" "$HOOK_LOG" || {
  echo "INCONCLUSIVE / RESULT: the PostToolUse hook never fired on an Edit under"
  echo "headless -p. Check $PROBE_DIR/silent.jsonl -- did the Edit happen at all"
  echo "(permission_denials)? If it did and the log is empty, PostToolUse is not"
  echo "an option and the next-turn warning is as good as it gets."
  exit 2
}
grep -q bogus "$PROBE_DIR/silent/schedules.json" || {
  echo "INCONCLUSIVE: in the silent CONTROL the bad key is not in the file, so"
  echo "either the edit never happened or the model removed it unprompted."
  echo "A fix in the other runs would prove nothing. Read silent.jsonl."
  exit 2
}
echo "CONTROL PASSED: the hook fires on Edit headless, and an unprompted model"
echo "leaves the bad key in place."
echo

any=no
for mode in block context exit2; do
  if grep -q bogus "$PROBE_DIR/$mode/schedules.json"; then
    # Not the same as "not seen": the request asks for the bad key, so a model
    # that read the complaint may reasonably ask before undoing it. Read the
    # result text before calling the channel dead (block, 2026-10-19).
    echo "$mode: NOT acted on (key still present) -- check $mode.jsonl's result: seen and declined, or never seen?"
  elif grep -q "^$mode	.*bad=True" "$HOOK_LOG"; then
    echo "$mode: ACTED ON in the same turn -- the hook saw the bad file, then the model fixed it"
    any=yes
  else
    echo "$mode: INCONCLUSIVE -- key absent but the hook never saw it present; read $mode.jsonl"
  fi
done
echo
if [ "$any" = yes ]; then
  echo "RESULT: same-turn validator feedback works. Build hooks/posttooluse.py on"
  echo "the shape(s) marked ACTED ON; prefer the one that also shows the nonce in"
  echo "the stream, so the transparency sheet can see what the model was told."
else
  echo "RESULT: no output shape reached the model. Park same-turn feedback; the"
  echo "per-turn context warning stays the only channel."
fi

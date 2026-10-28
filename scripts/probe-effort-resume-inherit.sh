#!/usr/bin/env bash
# Probe: on `--resume` with NO `--effort` flag, does the CLI inherit the
# session's previously-set level, or fall back to its own default? And does
# `/compact` honour the level at all?
#
# Blocking unknown for docs/tasks/model-and-effort-config.md piece 2's
# "compaction takes the session's current level" rule. If resume-without-flag
# inherits, that rule needs a one-line conditional in invoke.py's arg builder
# (omit --effort when source == "compaction") instead of somewhere to remember
# each session's chosen level. If `/compact` ignores effort entirely, the rule
# is moot and can be struck from the doc.
#
# RUN THIS IN A RAW TERMINAL ON THE BOX, as the `marlow` user, NOT from a
# Claude Code session -- a nested invocation inherits the harness env and gives
# false results (CLAUDE.md hard rule).
#
#   ssh marlow@marlow
#   bash ~/marlow-server/scripts/probe-effort-resume-inherit.sh
#
# It does NOT touch the live phone session; it builds throwaway ones.
#
# FIRST RUN 2026-08-27, and revised after it. Two flaws in the original are
# fixed here -- if you are comparing against a log from that run, note:
#   * step 1 reused the SAME follow-up prompt for all 3 runs in one session,
#     so runs 2-3 degenerated into "as I said above" (77-142 output tokens,
#     ~2.5s) and measured nothing. Distinct prompts now.
#   * step 2 never printed is_error. Its /compact returned in ~85ms with
#     thinking/output/cache_read all zero, which is an ERROR envelope, not
#     evidence that /compact ignores effort. It prints is_error now, and warms
#     the session first so there is something to compact.
# What that run DID settle: the session transcript stamps a top-level `effort`
# field, so the inherited level can be read off disk rather than inferred from
# token counts, which the task doc warns are noisy run to run. That is the
# authoritative signal below; the thinking-token columns are corroboration.

set -uo pipefail

CLAUDE="${MARLOW_CLAUDE_BIN:-$HOME/.local/bin/claude}"
WORKDIR="${MARLOW_WORKING_DIR:-$HOME/marlow-identity}"
PROJDIR="$HOME/.claude/projects/-home-marlow-marlow-identity"

# Match invoke.py's scrub so this behaves like a server-driven call.
for v in $(env | grep -oE '^(CLAUDE_CODE_[A-Z_]*|CLAUDECODE|AI_AGENT)' || true); do
  unset "$v"
done
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1

cd "$WORKDIR" || exit 1

say() { printf '\n=== %s ===\n' "$*"; }

# Reads the authoritative `usage` block off the result envelope, not a sum over
# the transcript -- so it is not subject to the double-write caveat in the doc.
read_usage() {
  python3 -c '
import json,sys
d=json.load(sys.stdin); u=d.get("usage",{}) or {}
det=u.get("output_tokens_details",{}) or {}
print("  %-26s is_error=%-5s thinking=%-6s output=%-6s cache_read=%-8s ms=%s" % (
    sys.argv[1], d.get("is_error"), det.get("thinking_tokens"), u.get("output_tokens"),
    u.get("cache_read_input_tokens"), d.get("duration_ms")))
# A zero-everything, sub-100ms envelope is an ERROR, not a cheap turn. Print
# the result text whenever that shape appears so it cannot be misread as data.
r = str(d.get("result") or "")[:200]
if d.get("is_error") or not u.get("output_tokens"):
    print("    result: %r" % r)
sid = d.get("session_id")
if sid: print("SESSION_ID=%s" % sid, file=sys.stderr)
' "$1"
}

# Hard enough that effort has somewhere to show up. The doc measured a trivial
# prompt thinking ~0 at every level, which proves nothing either way.
HARD='Design a delivery routing scheme for 40 vans across 3 depots with
time windows and driver-hours limits. Explain the tradeoffs you rejected.'

# ---------------------------------------------------------------------------
say "0. Confirm the transcript still stamps `effort` (found 2026-08-27; the direct signal)"
# If the CLI writes the request's effort/model into the session .jsonl, we can
# read the inherited level off disk instead of inferring it from token counts,
# which the doc warns are noisy run to run.
SID_HI=$("$CLAUDE" -p --effort high --output-format json -- "$HARD" </dev/null \
  | tee /tmp/probe-hi.json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))')
read_usage "build high" < /tmp/probe-hi.json 2>/dev/null
echo "  session=$SID_HI"
if [ -n "$SID_HI" ] && [ -f "$PROJDIR/$SID_HI.jsonl" ]; then
  echo "  keys in the request/usage records mentioning effort/thinking/model:"
  python3 - "$PROJDIR/$SID_HI.jsonl" <<'PY'
import json,sys
seen=set()
for line in open(sys.argv[1]):
    try: rec=json.loads(line)
    except Exception: continue
    def walk(o,p=""):
        if isinstance(o,dict):
            for k,v in o.items():
                kp=f"{p}.{k}" if p else k
                if any(t in k.lower() for t in ("effort","thinking","model","reasoning")):
                    if kp not in seen:
                        seen.add(kp); print("    %s = %r" % (kp, v if not isinstance(v,(dict,list)) else "<nested>"))
                walk(v,kp)
        elif isinstance(o,list):
            for i in o[:3]: walk(i,p+"[]")
    walk(rec)
PY
else
  echo "  (no transcript found -- fall through to the inference below)"
fi

# ---------------------------------------------------------------------------
# The authoritative read: what `effort` did the CLI stamp on this session's
# records? Beats inferring from thinking_tokens, which the task doc measured
# at 211 and then 0 for the SAME prompt at the SAME level.
effort_on_disk() {
  python3 - "$PROJDIR/$1.jsonl" <<'PY'
import json,sys
vals=[]
for line in open(sys.argv[1]):
    try: rec=json.loads(line)
    except Exception: continue
    if "effort" in rec: vals.append(rec["effort"])
print("    effort stamped in transcript, in order: %s" % (vals[-6:] or "<none found>"))
PY
}

say "1. THE QUESTION: resume with no --effort -- inherited, or CLI default?"
for level in high low; do
  sid=$("$CLAUDE" -p --effort "$level" --output-format json -- "$HARD" </dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))')
  echo "  built at $level -> session $sid"
  [ -z "$sid" ] && continue
  # Three DIFFERENT follow-ups. Reusing one prompt made runs 2-3 restatements
  # of the first answer and measured nothing -- see the header.
  i=0
  for q in 'Now reconsider the depot assignment under a 20% demand spike. Which constraint binds first?' \
           'A depot floods and is offline for a week. Re-plan, and say what you would monitor.' \
           'Driver-hours rules tighten by 15%. What breaks first, and what is the cheapest fix?'; do
    i=$((i+1))
    "$CLAUDE" -p --resume="$sid" --output-format json -- "$q" </dev/null \
      | read_usage "built-$level q$i no-flag" 2>/dev/null || echo "  built-$level q$i FAILED"
  done
  effort_on_disk "$sid"
done
echo "  READ IT AS: the stamped effort is the answer. built-high stamping 'high'"
echo "              after a no-flag resume => inherits, and compaction can just"
echo "              omit the flag. Stamping the default => it does not inherit."

say "2. Does /compact work at all here, and does effort reach it?"
# invoke.py sends /compact as the ENTIRE prompt, raw -- that is what triggers
# real compaction rather than a chat turn about compacting.
for level in low high; do
  sid=$("$CLAUDE" -p --effort "$level" --output-format json -- "$HARD" </dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))')
  [ -z "$sid" ] && { echo "  $level: no session"; continue; }
  # Warm it first: compacting a one-turn session may legitimately no-op.
  "$CLAUDE" -p --resume="$sid" --effort "$level" --output-format json -- \
    'Summarise the plan in three bullets.' </dev/null >/dev/null 2>&1
  "$CLAUDE" -p --resume="$sid" --effort "$level" --output-format json -- "/compact" </dev/null \
    | read_usage "/compact at $level" 2>/dev/null || echo "  /compact $level FAILED"
  effort_on_disk "$sid"
done
echo "  READ IT AS: is_error=True, or a sub-100ms all-zero envelope, means the"
echo "              CALL failed -- fix that before concluding anything about"
echo "              effort. Only a real compaction's numbers are evidence."

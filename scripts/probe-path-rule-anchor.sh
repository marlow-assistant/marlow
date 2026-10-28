#!/usr/bin/env bash
# Probe: does a single-slash path rule like `Edit(/home/marlow/.claude/**)` in
# the USER settings file mean the absolute path it spells, or is it anchored
# somewhere else and so matches nothing?
#
# WHY THIS MATTERS: every path rule in deploy/marlow-settings.json is written
# `/home/marlow/...` with ONE leading slash, and that file is installed as
# ~/.claude/settings.json. The permissions docs (fetched 2026-10-19) say:
#   `//path`  absolute from the filesystem root
#   `~/path`  from the home directory
#   `/path`   relative to the settings SOURCE -- for user settings that is
#             `~/.claude/path`; for `--settings <file>`, the file's directory
# If so, `Edit(/home/marlow/.claude/**)` means
# `/home/marlow/.claude/home/marlow/.claude/**` and every path rule in the
# shipped file is inert. On the deny side an inert rule is SILENT -- the same
# lesson the Write(...) denies taught 2026-08-23, now on the spelling itself.
#
# WHERE THE SETTINGS FILE LIVES: passed with --settings, but placed INSIDE
# ~/.claude/. Per the docs that gives it the same anchor directory as the user
# settings file, so a single-slash rule resolves exactly as it would in
# ~/.claude/settings.json -- without touching the user's real settings (which
# for mike carry v1's live config). Run `anchor-moves` places a file elsewhere to
# show the anchor follows the file's directory; that is the mechanism the docs
# describe, and what makes the ~/.claude placement a fair stand-in.
# What it does NOT prove: that the CLI has no special case for the literal file
# ~/.claude/settings.json. The docs list it as the same rule; nothing here
# observes it directly.
#
# RUNS, each one turn asking the model to change a token in one file:
#   none        no rule at all            -> must be DENIED (headless auto-deny;
#                                            if not, nothing below means anything)
#   allow-dbl   allow Edit(//abs/file)    -> must be EDITED (control: the tool,
#                                            the rule shape, and `//` all work)
#   allow-sgl   allow Edit(/abs/file)     -> THE QUESTION, allow half
#   anchor-moves  settings in $D, allow Edit(/sub/file) for $D/sub/file
#                                         -> EDITED means `/` anchors at the
#                                            settings file's directory
#   deny-dbl    allow //dir/**, deny Edit(//abs/file) -> must be DENIED (control)
#   deny-sgl    allow //dir/**, deny Edit(/abs/file)  -> THE QUESTION, deny half
#   rdeny-dbl   deny Read(//abs/file)     -> token must NOT leak (control)
#   rdeny-sgl   deny Read(/abs/file)      -> THE QUESTION, Read deny
#
# Denial is judged by a non-empty `permission_denials` array plus the file's
# contents, never by exit code -- a denied tool still exits 0 (CLAUDE.md).
#
# RUN AS `mike` over plain ssh, NEVER inside a Claude Code session (a nested
# `claude -p` inherits the parent's env and toolset), and never as `marlow`,
# whose live hook would push prompts to the phone. mike's own user settings
# still load; none of its rules name the probe dir, and the `none` control is
# what would catch it if one did.
#
#   scp scripts/probe-path-rule-anchor.sh mike@marlow:
#   ssh mike@marlow 'bash -l probe-path-rule-anchor.sh'
set -uo pipefail

# Under $HOME, not $TMPDIR: the CLI's filesystem sandbox confines writes to the
# working directory (probe-allow-vs-hook.sh, observed 2026-08-04).
PROBE_BASE="${MARLOW_PROBE_BASE:-$HOME}"
PROBE_DIR="$(mktemp -d "$PROBE_BASE/.marlow-probe.XXXXXX")" || {
    echo "could not create a probe dir under '$PROBE_BASE'." >&2; exit 1; }
cd "$PROBE_DIR" || exit 1
TGT="$PROBE_DIR/tgt"            # absolute; starts with a single /
ANCHOR="$PROBE_DIR/anch"        # settings dir for the anchor-moves run
mkdir -p "$TGT" "$ANCHOR/sub" || exit 1
# The ~/.claude-placed settings files. Unknown filenames there are ignored by
# the CLI; they are removed on exit and named so a leftover is obvious.
UCFG_PREFIX="$HOME/.claude/marlow-probe-$(basename "$PROBE_DIR")"

cleanup() {
    rm -f "$UCFG_PREFIX".*.json
    echo; echo "probe dir left in place for inspection: $PROBE_DIR"
}
trap cleanup EXIT

unset CLAUDECODE AI_AGENT ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
for v in $(env | grep -o '^CLAUDE_CODE_[A-Z0-9_]*' || true); do unset "$v"; done
for v in $(env | grep -o '^CLAUDECODE[A-Z0-9_]*' || true); do unset "$v"; done

claude --version

# $1 settings path, $2 allow JSON list, $3 deny JSON list
write_settings() {
    printf '{ "permissions": { "allow": %s, "deny": %s } }\n' "$2" "$3" > "$1"
}

# $1 label, $2 settings path, $3 target file, $4 mode (edit|read)
run_probe() {
    local label="$1" settings="$2" file="$3" mode="$4" prompt
    printf 'token-%s-ORIGINAL\n' "$label" > "$file"
    if [ "$mode" = edit ]; then
        prompt="Use the Edit tool to change ORIGINAL to CHANGED in the file $file. Read it first if the tool requires that. Do nothing else and do not retry with other tools if refused."
    else
        prompt="Use the Read tool to read the file $file and reply with its exact contents. Do not use any other tool."
    fi
    claude -p --output-format json --strict-mcp-config --max-turns 4 \
        --settings "$settings" -- "$prompt" \
        </dev/null >"$PROBE_DIR/$label.json" 2>"$PROBE_DIR/$label.err"
    python3 - "$PROBE_DIR/$label.json" "$file" "$mode" "$label" <<'PY'
import json, sys
path, target, mode, label = sys.argv[1:]
try:
    out = json.load(open(path))
except Exception as e:
    print(f"{label:13} UNPARSEABLE output ({e}); see {path}"); sys.exit()
denials = out.get("permission_denials") or []
tools = sorted({d.get("tool_name", "?") for d in denials})
body = open(target).read()
if mode == "edit":
    seen = "EDITED" if "CHANGED" in body else "unchanged"
else:
    seen = "LEAKED" if f"token-{label}-ORIGINAL" in (out.get("result") or "") else "not-leaked"
print(f"{label:13} {seen:10} denials={len(denials)} {tools}")
PY
}

dq() { printf '"%s"' "$1"; }   # JSON string for a rule

echo
echo "label         outcome    permission_denials"

S="$UCFG_PREFIX.none.json";      write_settings "$S" '[]' '[]'
run_probe none "$S" "$TGT/none" edit

S="$UCFG_PREFIX.allow-dbl.json"; write_settings "$S" "[$(dq "Edit(/$TGT/allow-dbl)")]" '[]'
run_probe allow-dbl "$S" "$TGT/allow-dbl" edit

S="$UCFG_PREFIX.allow-sgl.json"; write_settings "$S" "[$(dq "Edit($TGT/allow-sgl)")]" '[]'
run_probe allow-sgl "$S" "$TGT/allow-sgl" edit

S="$ANCHOR/settings.json";       write_settings "$S" "[$(dq "Edit(/sub/anchor-moves)")]" '[]'
run_probe anchor-moves "$S" "$ANCHOR/sub/anchor-moves" edit

S="$UCFG_PREFIX.deny-dbl.json";  write_settings "$S" "[$(dq "Edit(/$TGT/**)")]" "[$(dq "Edit(/$TGT/deny-dbl)")]"
run_probe deny-dbl "$S" "$TGT/deny-dbl" edit

S="$UCFG_PREFIX.deny-sgl.json";  write_settings "$S" "[$(dq "Edit(/$TGT/**)")]" "[$(dq "Edit($TGT/deny-sgl)")]"
run_probe deny-sgl "$S" "$TGT/deny-sgl" edit

S="$UCFG_PREFIX.rdeny-dbl.json"; write_settings "$S" '[]' "[$(dq "Read(/$TGT/rdeny-dbl)")]"
run_probe rdeny-dbl "$S" "$TGT/rdeny-dbl" read

S="$UCFG_PREFIX.rdeny-sgl.json"; write_settings "$S" '[]' "[$(dq "Read($TGT/rdeny-sgl)")]"
run_probe rdeny-sgl "$S" "$TGT/rdeny-sgl" read

cat <<'EOF'

READING IT (controls first; if one fails, stop -- the questions mean nothing):
  none=unchanged, allow-dbl=EDITED, deny-dbl=unchanged, rdeny-dbl=not-leaked
Then:
  allow-sgl unchanged + deny-sgl EDITED + rdeny-sgl LEAKED
      -> single `/` is NOT absolute here: every `/home/...` rule is inert.
  allow-sgl EDITED + deny-sgl unchanged + rdeny-sgl not-leaked
      -> single `/` is absolute at this scope; the docs are wrong for it.
  anchor-moves EDITED -> `/` anchors at the settings file's directory.
EOF

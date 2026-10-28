#!/usr/bin/env bash
# One-time identity seed for a marlow-server instance.
#
# marlow-server ships no personality. This copies an assistant brain
# (SOUL.md / CLAUDE.md / PREFERENCES.md / USER.md / FIRST-RUN.md / memory/) into
# the runtime WORKING DIR, where it then diverges independently. Software updates
# leave it alone, with one exception: on a box seeded with PREFERENCES.md,
# update.sh replaces CLAUDE.md and FIRST-RUN.md, which belong to the software
# (docs/tasks/box-upgrades.md).
#
# Sources:
#   --template            Use this repo's starter identity/ (fresh Marlow). Needs
#                          --user-name and --timezone: the starter's {{USER_NAME}}
#                          and {{TIMEZONE}} placeholders are filled from them.
#   --from <v1-checkout>   Copy identity out of an existing v1 assistant checkout
#                          (the assistant before marlow-server). Migration.
#
# Optional:
#   --origin <git-url>     Set the working dir's `origin` so the diverging brain
#                          has somewhere to be backed up. Sets the remote only —
#                          never pushes, and installs no credential (the pusher is
#                          a root-owned cron; see the task doc).
#
# Target working dir: $MARLOW_WORKING_DIR, else ~/marlow-identity.
# Refuses to overwrite an already-seeded working dir unless --force — a seeded
# dir has its own diverged memory that must not be clobbered.
#
# Examples:
#   deploy/seed-identity.sh --template --user-name Priya --timezone Europe/London
#   deploy/seed-identity.sh --from /path/to/v1-checkout
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKING_DIR="${MARLOW_WORKING_DIR:-$HOME/marlow-identity}"
WORKING_DIR="${WORKING_DIR/#\~/$HOME}"

SOURCE=""
FORCE=0
ORIGIN=""
USER_NAME=""
TIMEZONE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --template) SOURCE="$REPO_DIR/identity"; shift ;;
        --from) SOURCE="${2:?--from needs a path}"; shift 2 ;;
        --origin) ORIGIN="${2:?--origin needs a git URL}"; shift 2 ;;
        --force) FORCE=1; shift ;;
        --user-name) USER_NAME="${2:?--user-name needs a name}"; shift 2 ;;
        --timezone) TIMEZONE="${2:?--timezone needs an IANA zone}"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$SOURCE" ]; then
    echo "error: pick a source — --template or --from <v1-checkout>" >&2
    exit 2
fi
if [ ! -d "$SOURCE" ]; then
    echo "error: source not found: $SOURCE" >&2
    exit 2
fi

# The starter is written in placeholders, and a seed that left them unfilled
# would boot an assistant addressing "{{USER_NAME}}" with a schedule store the
# server rejects outright (an invalid `timezone` freezes every entry). So the
# template refuses to seed half-filled rather than defaulting to a guess.
if [ "$SOURCE" = "$REPO_DIR/identity" ]; then
    if [ -z "$USER_NAME" ] || [ -z "$TIMEZONE" ]; then
        echo "error: --template needs --user-name and --timezone" >&2
        exit 2
    fi
    case "$USER_NAME" in
        *$'\n'*|*'{{'*|*'}}'*) echo "error: --user-name must be a plain one-line name" >&2; exit 2 ;;
    esac
    if ! python3 -c 'import sys, zoneinfo; zoneinfo.ZoneInfo(sys.argv[1])' "$TIMEZONE" 2>/dev/null \
        || [[ "$TIMEZONE" != */* && "$TIMEZONE" != UTC ]]; then
        echo "error: --timezone must be an IANA name like Europe/London, got: $TIMEZONE" >&2
        exit 2
    fi
fi

# Guard: don't clobber an already-seeded (diverged) working dir.
if [ -e "$WORKING_DIR/SOUL.md" ] && [ "$FORCE" -ne 1 ]; then
    echo "error: $WORKING_DIR already has an identity (SOUL.md present)." >&2
    echo "       Seeding is one-time. Pass --force only if you truly mean to overwrite it." >&2
    exit 1
fi

mkdir -p "$WORKING_DIR"

echo "== seeding identity from $SOURCE -> $WORKING_DIR =="
# FIRST-RUN.md is the starter's onboarding script; CLAUDE.md points at it only
# while USER.md is still the template. A v1 checkout has none, and skips it.
# PREFERENCES.md is the user's half of the starter's rules; a v1 checkout has
# none either, which is what keeps update.sh off its hand-grown CLAUDE.md.
for f in SOUL.md CLAUDE.md PREFERENCES.md USER.md FIRST-RUN.md; do
    if [ -f "$SOURCE/$f" ]; then
        cp "$SOURCE/$f" "$WORKING_DIR/$f"
        echo "  + $f"
    else
        echo "  ! $f not in source, skipped"
    fi
done
if [ -d "$SOURCE/memory" ]; then
    mkdir -p "$WORKING_DIR/memory"
    cp -R "$SOURCE/memory/." "$WORKING_DIR/memory/"
    echo "  + memory/"
fi
# The schedule store and its job prompts. This is the only place a store is
# seeded; update.sh never writes one.
if [ -d "$SOURCE/cron" ]; then
    mkdir -p "$WORKING_DIR/cron"
    cp -R "$SOURCE/cron/." "$WORKING_DIR/cron/"
    echo "  + cron/"
fi

# Fill the starter's placeholders. python3 rather than sed: a name is free text,
# and `&`, `/` or `\` in it would be sed syntax. Only the files seeded above.
if [ -n "$USER_NAME" ] || [ -n "$TIMEZONE" ]; then
    python3 - "$WORKING_DIR" "$USER_NAME" "$TIMEZONE" <<'PY'
import pathlib, sys
root, name, zone = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
files = [root / f for f in ("SOUL.md", "CLAUDE.md", "PREFERENCES.md", "USER.md")]
if (root / "cron").is_dir():
    files += sorted(p for p in (root / "cron").iterdir() if p.suffix in (".md", ".json"))
for path in files:
    if not path.is_file():
        continue
    text = path.read_text()
    if name:
        text = text.replace("{{USER_NAME}}", name)
    if zone:
        text = text.replace("{{TIMEZONE}}", zone)
    path.write_text(text)
PY
    echo "  filled in: ${USER_NAME:+name=$USER_NAME }${TIMEZONE:+timezone=$TIMEZONE}"
fi

# The working dir is the assistant's own living brain: version it locally so
# memory writes are recoverable. This is a SEPARATE, private history from the
# marlow-server software repo — it diverges freely and is never pushed back to
# the product repo.
if [ ! -d "$WORKING_DIR/.git" ]; then
    echo "== git init $WORKING_DIR (private, diverging identity history) =="
    # `-b main` explicitly: without it this inherits git's default branch name,
    # which produced a `master` on the box that no remote's default matched
    # (docs/tasks/marlow-working-dir-git-policy.md). Older git ignores -b, hence
    # the rename fallback.
    git -C "$WORKING_DIR" init -q -b main 2>/dev/null \
        || { git -C "$WORKING_DIR" init -q; git -C "$WORKING_DIR" symbolic-ref HEAD refs/heads/main; }
    git -C "$WORKING_DIR" add -A
    # Pass an explicit committer identity: a fresh system user (e.g. `marlow`)
    # has no git config, and a bare `git commit` there aborts with "Author
    # identity unknown" — which previously slipped through a `|| true` and left
    # an initialized-but-empty history (no seed commit). Env overrides win if set.
    git -C "$WORKING_DIR" \
        -c user.name="${GIT_AUTHOR_NAME:-Marlow}" \
        -c user.email="${GIT_AUTHOR_EMAIL:-marlow@localhost}" \
        commit -q -m "seed: initial identity"
fi

# The brain is a living, diverging history that exists on exactly one machine
# unless something backs it up. Versioning it locally (above) makes a bad *edit*
# recoverable and losing the box not. `--origin` is how an install says where it
# belongs -- for Mike's box, the private `marlow-identity` repo.
#
# Set as a remote only. This script does NOT push and installs no credential: the
# push is its own hourly cron with its own deploy key, installed separately per
# deploy/README.md 7a (docs/tasks/marlow-working-dir-git-policy.md, piece 4).
# Seeding a box and giving it a write credential stay separate steps.
if [ -n "$ORIGIN" ]; then
    if git -C "$WORKING_DIR" remote get-url origin >/dev/null 2>&1; then
        echo "== origin already set, leaving it alone: $(git -C "$WORKING_DIR" remote get-url origin) =="
    else
        git -C "$WORKING_DIR" remote add origin "$ORIGIN"
        echo "== origin -> $ORIGIN (no push, no credential — see piece 4) =="
    fi
fi

echo "done. Working dir seeded. update.sh replaces only CLAUDE.md and FIRST-RUN.md."

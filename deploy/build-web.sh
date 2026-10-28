#!/usr/bin/env bash
# Builds the web client (web/ -> web/dist, which the server serves at /).
# Called by update.sh AS THE `marlow` USER, like pip -- never run it as root:
# that would leave a root-owned node_modules and dist the next update (and the
# server) cannot replace. Usage: build-web.sh build|install <repo dir>
#
# TWO PHASES, because the server reads web/dist per request: whatever sits at
# web/dist is live the instant it lands, but the restart onto the matching
# server code comes later in update.sh, after root steps that can fail.
#   build   -- build + verify into web/.dist-staged. Touches nothing live.
#              Runs right after pip; a failure aborts the update there.
#   install -- swap the staged build in and advance the stamp. Runs immediately
#              before `systemctl restart`, so a failing step in between leaves
#              the old dist serving the old process. A no-op if nothing staged.
#
# Rebuilds only when it has to. "Has to" is decided by a STAMP -- the git tree
# hash of web/ at HEAD, written next to dist after a successful swap -- not by
# diffing the pre- and post-merge revisions. Two reasons:
#   * update.sh re-execs itself after the merge, so the second pass sees
#     HEAD == origin and has no pre-merge revision unless one is smuggled
#     across the exec.
#   * a diff forgets a failed build: merge lands, build fails, update exits;
#     the retry sees "web/ unchanged since the merge" and skips, leaving the
#     OLD client against NEW server code with a green run. The stamp only
#     advances when a build actually completed.
# Equivalent to "web/ changed in this update" in the normal case, and correct
# in the failure case. A missing dist (or one without index.html) always builds.
set -euo pipefail

MODE="${1:-}"
REPO_DIR="${2:?usage: build-web.sh build|install <repo dir>}"
WEB="$REPO_DIR/web"
DIST="$WEB/dist"
STAMP="$WEB/.dist-stamp"
STAGED="$WEB/.dist-staged"
NODE_FLOOR="20.19.0"   # Vite 8's floor; web/package.json "engines" says the same

build_phase() {
    # Swept BEFORE the skip check: a staged build left by an aborted run must
    # never survive into this update's install phase (e.g. after a branch switch
    # back to a web/ it does not match). Its stamp never advanced, so it is
    # simply rebuilt if still wanted. Also clears leftovers from a killed run.
    rm -rf "$WEB"/.dist-build-* "$WEB"/.dist-old-* "$STAGED"
    WANT="$(git -C "$REPO_DIR" rev-parse HEAD:web)"
    HAVE="$(cat "$STAMP" 2>/dev/null || true)"
    if [[ -f "$DIST/index.html" && "$HAVE" == "$WANT" ]]; then
        echo "   web/ unchanged since the last build and dist is present; skipping"
        return 0
    fi

    # Checked only once a build is actually needed: an old node must not break an
    # update that has no client work to do.
    if ! command -v node >/dev/null 2>&1; then
        echo "FAILED (web client build): node is not installed; need >= $NODE_FLOOR" >&2
        exit 1
    fi
    NODE_VERSION="$(node --version)"
    NODE_VERSION="${NODE_VERSION#v}"
    if [[ "$(printf '%s\n%s\n' "$NODE_FLOOR" "$NODE_VERSION" | sort -V | head -n1)" != "$NODE_FLOOR" ]]; then
        echo "FAILED (web client build): node $NODE_VERSION is too old; need >= $NODE_FLOOR" >&2
        exit 1
    fi

    # Same filesystem as dist (a sibling inside web/), so the rename below is an
    # atomic rename(2), never a cross-device copy.
    TMP="$(mktemp -d "$WEB/.dist-build-XXXXXX")"
    cleanup() { rm -rf "$TMP"; }
    trap cleanup EXIT

    echo "   building web client with node $NODE_VERSION (into ${TMP##*/})"
    if ! ( cd "$WEB" && npm ci && MARLOW_WEB_OUT_DIR="$TMP" npm run build ); then
        echo "FAILED (web client build): npm ci / npm run build failed; the previous dist is untouched" >&2
        exit 1
    fi
    for f in index.html sw.js; do
        if [[ ! -f "$TMP/$f" ]]; then
            echo "FAILED (web client build): the build produced no $f; the previous dist is untouched" >&2
            exit 1
        fi
    done
    chmod 755 "$TMP"
    mv "$TMP" "$STAGED"
    echo "   web client built and staged; it goes live just before the restart"
}

install_phase() {
    WANT="$(git -C "$REPO_DIR" rev-parse HEAD:web)"
    if [[ ! -d "$STAGED" ]]; then
        echo "   no staged web build; dist left as is"
        return 0
    fi
    OLD=""
    trap 'rm -rf ${OLD:+"$OLD"}' EXIT
    # Swap by renaming the old dist aside, the new one in, then deleting the old.
    # A symlink flip would avoid the instant with no dist, but needs dist to become
    # a symlink (migrating a real directory is this same two-rename dance once) and
    # makes every later reader chase it; the gap here is two renames long, and the
    # restart follows immediately. If the second rename
    # fails the old dist is put back, so a failure never leaves dist missing.
    if [[ -e "$DIST" ]]; then
        OLD="$WEB/.dist-old-$$"
        mv "$DIST" "$OLD"
    fi
    if ! mv "$STAGED" "$DIST"; then
        [[ -n "$OLD" ]] && mv "$OLD" "$DIST" && OLD=""
        echo "FAILED (web client build): could not move the new build into place" >&2
        exit 1
    fi
    # Stamp after the swap: dying in between only costs one redundant rebuild.
    printf '%s\n' "$WANT" > "$STAMP"
    echo "   web client installed ($WANT)"
}

case "$MODE" in
    build) build_phase ;;
    install) install_phase ;;
    *) echo "usage: build-web.sh build|install <repo dir>" >&2; exit 1 ;;
esac

#!/usr/bin/env bash
# Speech to text for the web client's mic and voice mode
# (docs/tasks/voice-mode-android.md): the Python packages, the Parakeet model,
# and a one-time check that this box is fast enough. On by default, so a box
# is useful without anyone knowing to ask (Mike, 2026-10-22); a slow box gets
# a loud warning, not a refusal.
#
# Run AS the instance user (never root): update.sh and install.sh call it
# through `sudo -u`. Everything it writes is that user's: the venv, and
# data_dir/models, which sits with the server's private state where the agent
# can't write (config.py, `stt_model`).
#
#   install-voice.sh <repo dir> <venv dir> <data dir>
#
# Never fails the deploy. Without network, or on a platform with no wheels,
# it says so and exits 0: the server answers `GET /v1/transcribe` with
# "available: false" and the web client shows no mic, which is what the box
# had before.
#
# Off for good on a box: `touch <data dir>/models/OFF` and delete the model
# folder; every later update then skips this. Env (all optional):
#   MARLOW_VOICE=0          skip it this once (the model is ~110 MB, PyAV ~130 MB)
#   MARLOW_STT_MODEL, MARLOW_STT_MODEL_URL, MARLOW_STT_MODEL_SHA256
#                           another model and source (the tests use these;
#                           update.sh and install.sh never pass them)
set -uo pipefail

REPO_DIR="${1:?repo dir}"
VENV="${2:?venv dir}"
DATA_DIR="${3:?data dir}"
MODEL="${MARLOW_STT_MODEL:-sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8}"

# The bar from the task doc: 15 s of speech in under ~2 s is as good as iOS.
# Mike's 2015 laptop box: 0.68 s; the 2-vCPU VPS: 1.09 s.
SLOW_SECONDS=2.0

say() { echo "   voice: $*"; }
off() {
    say "$*"
    say "dictation and voice mode stay off on this box until the next update fixes it."
    exit 0
}

if [[ "${MARLOW_VOICE:-1}" == 0 ]]; then
    say "MARLOW_VOICE=0: skipped."
    exit 0
fi
if [[ -e "$DATA_DIR/models/OFF" ]]; then
    say "turned off on this box ($DATA_DIR/models/OFF): skipped."
    exit 0
fi

# Same rule as config.py: a folder name, never a path.
if ! [[ "$MODEL" =~ ^[A-Za-z0-9_-][A-Za-z0-9._-]*$ ]]; then
    off "refusing MARLOW_STT_MODEL '$MODEL': not a plain folder name."
fi

# Pinned, so a swapped release asset is refused rather than loaded into the
# server. Computed from the sherpa-onnx `asr-models` release, 2026-10-22.
# Only the 110M: the 0.6B is ~480 MB to fetch and ~1 GB resident, too big to
# put on every box unasked (Mike, 2026-10-22). A box that wants it installs it
# by hand and sets MARLOW_STT_MODEL in its unit.
RELEASE=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models
case "$MODEL" in
    sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8)
        SHA256=f628312e9fdf8686374cb01a69425c41732529d540860311f16f37cbc32cfe9b ;;
    *)
        SHA256="" ;;
esac
URL="${MARLOW_STT_MODEL_URL:-$RELEASE/$MODEL.tar.bz2}"
SHA256="${MARLOW_STT_MODEL_SHA256:-$SHA256}"
[[ -n "$SHA256" ]] || off "no pinned checksum for model '$MODEL'; set MARLOW_STT_MODEL_SHA256 to install it."

if ! "$VENV/bin/pip" install -q --disable-pip-version-check -r "$REPO_DIR/server/requirements-voice.txt"; then
    off "couldn't install the speech packages (server/requirements-voice.txt)."
fi

MODELS="$DATA_DIR/models"
TARGET="$MODELS/$MODEL"
BENCH="$TARGET/.bench-seconds"
# What ParakeetEngine.available() looks for.
complete() {
    [[ -f "$TARGET/tokens.txt" ]] && compgen -G "$TARGET/encoder*.onnx" >/dev/null \
        && compgen -G "$TARGET/decoder*.onnx" >/dev/null && compgen -G "$TARGET/joiner*.onnx" >/dev/null
}

if complete; then
    say "model present: $MODEL"
else
    say "downloading $MODEL"
    install -d -m 700 "$DATA_DIR" "$MODELS" || off "couldn't create $MODELS."
    WORK="$(mktemp -d "$MODELS/.download-XXXXXX")" || off "couldn't make a download folder in $MODELS."
    trap 'rm -rf "$WORK"' EXIT
    curl -fsSL --retry 2 "$URL" -o "$WORK/model.tar.bz2" || off "download failed: $URL"
    GOT="$(sha256sum "$WORK/model.tar.bz2" | cut -d' ' -f1)"
    [[ "$GOT" == "$SHA256" ]] || off "checksum mismatch for $MODEL (got $GOT); not installed."
    # Only the one top-level folder named for the model, nothing absolute or
    # climbing out: checked before extracting, though the checksum already
    # pins what is in it.
    LISTING="$(tar -tjf "$WORK/model.tar.bz2")" || off "couldn't read the $MODEL archive."
    STRAY="$(grep -Ev "^(\./)?$MODEL(/|$)" <<<"$LISTING" || true)"
    CLIMB="$(grep -E '(^|/)\.\.(/|$)' <<<"$LISTING" || true)"
    [[ -z "$STRAY" && -z "$CLIMB" ]] || off "the archive holds more than $MODEL/; not installed."
    mkdir "$WORK/x" && tar -xjf "$WORK/model.tar.bz2" -C "$WORK/x" --no-same-owner --no-same-permissions \
        || off "couldn't unpack $MODEL."
    # Regular files and folders only: a link would let the server load a file
    # from anywhere it can read.
    ODD="$(find "$WORK/x" ! -type f ! -type d)"
    [[ -z "$ODD" ]] || off "the archive holds links or special files; not installed."
    rm -rf "$TARGET"
    mv "$WORK/x/$MODEL" "$TARGET" || off "couldn't move $MODEL into place."
    chmod -R go-rwx "$TARGET"
    say "installed: $TARGET"
fi

# Once per model, not every update: the check loads a second copy of the
# model beside the running server's, briefly, and takes a few seconds. Except
# that a slow figure is measured again next time, since an update can land
# while a turn has the CPU and one bad reading shouldn't brand the box.
#
# Its own log folder: the check imports server.log, whose rotating handler
# must never share marlow-server.log with the running server (server/log.py).
# MARLOW_STT_THREADS, when update.sh passes the unit's, times what the server
# will run.
slow() { awk -v s="$1" -v bar="$SLOW_SECONDS" 'BEGIN { exit !(s > bar) }'; }
if [[ ! -s "$BENCH" ]] || slow "$(cat "$BENCH")"; then
    say "timing 15 s of speech on this box (a few seconds)"
    LOGS="$(mktemp -d)"
    SECS="$(cd "$REPO_DIR" && MARLOW_LOG_DIR="$LOGS" "$VENV/bin/python" -m server.transcribe bench "$TARGET" 2>/dev/null | tail -1)"
    rm -rf "$LOGS"
    if [[ "$SECS" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        echo "$SECS" > "$BENCH"
    else
        # The packages and the model are in place, so the server will use
        # them: say that, not "off". Tried again on the next update.
        say "WARNING: the model is installed and dictation is on, but the speed check failed,"
        say "so this box's speed is unknown. Check: $VENV/bin/python -m server.transcribe bench $TARGET"
        exit 0
    fi
fi

SECS="$(cat "$BENCH")"
if slow "$SECS"; then
    cat <<EOF

   ##########################################################################
   #
   #   WARNING: speech to text is SLOW on this box.
   #
   #   15 s of speech took ${SECS} s to transcribe; the bar is ${SLOW_SECONDS} s.
   #   Dictation and voice mode are on, but every utterance will keep
   #   people waiting after they stop talking. A faster CPU fixes it.
   #   To turn it off for good: touch $MODELS/OFF
   #   then delete $TARGET and restart.
   #   docs/tasks/voice-mode-android.md has the measurements.
   #
   ##########################################################################

EOF
else
    say "ready: 15 s of speech in ${SECS} s on this box."
fi
exit 0

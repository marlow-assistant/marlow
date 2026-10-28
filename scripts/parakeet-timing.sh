#!/usr/bin/env bash
# How fast, and how much memory, would this machine need to turn speech into
# text with Parakeet?
#
# For a Marlow box owner to run once and paste the output back. It answers the
# two questions that decide whether web/Android dictation and voice mode can
# transcribe on the box (docs/tasks/voice-mode-android.md): how long after you
# stop talking until the text is ready, and how much memory the model holds
# while it does.
#
# It installs nothing system-wide and touches nothing of Marlow's: a Python
# venv and the model go in one folder under /tmp, deleted at the end. It
# downloads about 100 MB (NVIDIA's Parakeet TDT 110M, int8, as packaged by
# sherpa-onnx) and needs about 1 GB of free memory while it runs. The test
# clips are the model's own sample recordings, not anyone's voice.
#
#   bash scripts/parakeet-timing.sh            # as any user, no sudo
#   KEEP=1 bash scripts/parakeet-timing.sh     # keep the folder for a rerun
#   MODEL=sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8 bash scripts/parakeet-timing.sh
#                                              # the larger model (~480 MB)
#
# Memory grows with how long the speech is, and each length runs in a fresh
# process so the figures are not polluted by a longer run before it.
set -euo pipefail

# Any sherpa-onnx NeMo transducer from the asr-models release.
MODEL="${MODEL:-sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8}"
URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$MODEL.tar.bz2"
DIR="${PARAKEET_DIR:-/tmp/parakeet-timing}"

mkdir -p "$DIR"
cleanup() { [ "${KEEP:-0}" = 1 ] || rm -rf "$DIR"; }
trap cleanup EXIT

echo "== this machine"
echo "cpu:    $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | xargs || uname -m)"
echo "cores:  $(nproc 2>/dev/null || sysctl -n hw.ncpu)"
echo "memory: $(free -h 2>/dev/null | awk '/Mem:/ {print $2 " total, " $7 " available"}' || echo unknown)"
echo "load:   $(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || uptime)"
echo "model:  $MODEL"

echo "== setting up (a few minutes: Python packages, then the model)"
if [ ! -x "$DIR/venv/bin/python" ]; then
  python3 -m venv "$DIR/venv"
  "$DIR/venv/bin/pip" install --quiet --disable-pip-version-check sherpa-onnx numpy
fi
if [ ! -d "$DIR/$MODEL" ]; then
  curl -fL --progress-bar "$URL" -o "$DIR/model.tar.bz2"
  tar -xjf "$DIR/model.tar.bz2" -C "$DIR"
  rm "$DIR/model.tar.bz2"
fi

cat > "$DIR/time_one.py" <<'PY'
import glob, os, sys, time, wave
import numpy as np
import sherpa_onnx

model, seconds, first = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1"

def one(pattern):
    return sorted(glob.glob(os.path.join(model, pattern)))[0]

def rss_mb():
    # Linux: current resident memory. Elsewhere: the peak, which is what
    # resource reports (bytes on macOS, kilobytes on Linux).
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024

start = time.monotonic()
recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
    encoder=one("encoder*.onnx"), decoder=one("decoder*.onnx"),
    joiner=one("joiner*.onnx"), tokens=one("tokens.txt"),
    num_threads=os.cpu_count() or 1, model_type="nemo_transducer",
)
load = time.monotonic() - start
loaded_mb = rss_mb()

def read(path):
    with wave.open(path) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, path
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return w.getframerate(), data.astype(np.float32) / 32768

clips = [read(p) for p in sorted(glob.glob(os.path.join(model, "test_wavs", "*.wav")))]
rate = clips[0][0]
assert all(r == rate for r, _ in clips), "sample clips at different rates"
pool = np.concatenate([s for _, s in clips] + [np.zeros(rate // 2, dtype=np.float32)])

def transcribe(samples):
    stream = recognizer.create_stream()
    stream.accept_waveform(rate, samples)
    t = time.monotonic()
    recognizer.decode_stream(stream)
    return time.monotonic() - t, stream.result.text

transcribe(pool[: rate * 2])  # warm-up, not counted
need = rate * seconds
samples = np.tile(pool, need // len(pool) + 1)[:need]
best = min(transcribe(samples)[0] for _ in range(3))
after_mb = rss_mb()

if first:
    print(f"model load: {load:.1f} s (once, when the server starts)")
    print(f"memory with the model loaded and idle: {loaded_mb:.0f} MB")
    print()
    print("speech   wait after you stop talking   (x real time)   memory after")
print(f"{seconds:4d} s   {best:6.2f} s                      {best / seconds:.2f}            {after_mb:.0f} MB")
if first:
    print(f"\nsample transcript: {transcribe(clips[0][1])[1].strip()!r}\n", file=sys.stderr)
PY

echo "== timing"
first=1
for seconds in 5 15 30 60; do
  "$DIR/venv/bin/python" -I "$DIR/time_one.py" "$DIR/$MODEL" "$seconds" "$first"
  first=0
done
echo
echo "== done; paste everything above back to Mike"

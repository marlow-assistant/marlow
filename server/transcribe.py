"""Speech to text on the box, for the web client's dictation and voice mode
(docs/tasks/voice-mode-android.md).

The web client records an utterance as a run of short WebM/Opus segments, cut
at pauses, and uploads each one as soon as it ends, so a long monologue waits
about as long as a short one after the speaker stops. Each segment is decoded
with PyAV to 16 kHz mono and transcribed by Parakeet (sherpa-onnx, int8, CPU);
the client is handed back the whole utterance's text with the final segment.

Three bounds, all measured on Mike's box on 2026-10-21 (the task doc has the
tables):
  - **One segment at a time, box-wide.** Transcription runs beside a
    `claude -p` turn on the same cores, and the model's arenas grow with the
    longest segment decoded and are not given back. A single worker thread is
    the whole policy; it is its own executor, not the loop's default one,
    which the queue's drain already shares (see post_message in app.py).
  - **A segment over `MAX_SEGMENT_SECONDS` is refused.** Memory is set by
    segment length (~7–13 MB a second, steady state ~0.7 GB for the 110M at
    30 s); the client cuts at ~25 s, so 35 s is a guard, not a limit anyone
    should meet.
  - **The model stays loaded** once the first segment arrives. Loading per
    request added ~6 s to every utterance on the VPS. Lazily the first time,
    so a box whose clients never dictate never holds the ~220 MB; but the
    first segment ever transcribed leaves a marker (`settings.stt_used_marker_path`),
    and from then on every start loads it in the background (Mike,
    2026-10-22), so the first utterance after an update doesn't pay for it.

`deploy/install-voice.sh` installs the dependencies and the model, and runs
`python -m server.transcribe bench <model dir>` once to warn about a box too
slow for it.

The engine is behind `SpeechEngine`, so the tests run against a fake with no
model and no PyAV. `av`, `numpy` and `sherpa_onnx` are imported only by the
real engine, and are not in requirements.txt: a box without them, or without
the model files, answers 503 and the web client hides its mic.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from server.log import get_logger

logger = get_logger(__name__)

SAMPLE_RATE = 16_000

# The client cuts at ~25 s; this is the server's guard (module docstring).
MAX_SEGMENT_SECONDS = 35.0

# 24 kbps Opus is ~3 KB a second, so a 35 s segment is ~105 KB. A megabyte
# leaves room for a browser that ignores the requested bitrate, and stops a
# body being read into memory without end.
MAX_SEGMENT_BYTES = 1024 * 1024

# The client cuts at the first pause after ~12 s, so this is ~50 minutes of
# one utterance at the shortest. Each held segment costs its text only.
MAX_SEGMENTS = 256

# An utterance nobody finished (the tab closed mid-recording) is forgotten
# after this, and at most this many are held at once.
UTTERANCE_TTL_SECONDS = 600.0
MAX_UTTERANCES = 32

UTTERANCE_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")

# Containers the decoder is allowed to open. PyAV would otherwise probe the
# upload against every demuxer ffmpeg has, and each one is parser surface for
# bytes a client chose. Chrome and Firefox record WebM (Matroska's demuxer);
# Firefox can also record Ogg.
ALLOWED_FORMATS = {"audio/webm": "matroska", "audio/ogg": "ogg"}


class TranscribeError(Exception):
    """A segment that can't be transcribed; `status` is the HTTP answer."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def check_length(seconds: float) -> None:
    """The one place the segment cap is enforced: the engine calls it while
    decoding, to stop early, and the transcriber after, for any engine."""
    if seconds > MAX_SEGMENT_SECONDS:
        raise TranscribeError(413, f"segment is over {MAX_SEGMENT_SECONDS:.0f} seconds")


@dataclass(frozen=True)
class Audio:
    """Decoded speech: 16 kHz mono float samples. `samples` is whatever the
    engine's recognizer takes (a numpy array for the real one)."""

    samples: Any
    seconds: float


class SpeechEngine(Protocol):
    def available(self) -> bool:
        """Cheap: can this box transcribe at all? Never loads the model."""

    def decode(self, data: bytes, container: str) -> Audio:
        """Compressed upload to 16 kHz mono. Raises TranscribeError 400 for
        audio it can't read, 413 for a segment over MAX_SEGMENT_SECONDS
        (stopping as soon as it knows, rather than decoding the rest)."""

    def recognize(self, audio: Audio) -> str:
        """Decoded speech to text. Loads the model on first use."""

    def load(self) -> None:
        """Loads the model now, if it isn't yet. Slow; run on the worker."""


# --- the real engine: PyAV + sherpa-onnx Parakeet ------------------------------

class ParakeetEngine:
    def __init__(self, model_dir: Path, num_threads: int):
        self.model_dir = model_dir
        self.num_threads = num_threads
        self._recognizer: Any = None
        self._lock = threading.Lock()
        self._available = False

    def _model_file(self, pattern: str) -> Path | None:
        found = sorted(self.model_dir.glob(pattern))
        return found[0] if found else None

    def available(self) -> bool:
        # Asked on the event loop for every segment, so a yes is kept: it
        # costs three module lookups and four globs. A no is asked again, so
        # installing the model needs no restart. A model deleted after a yes
        # fails at load instead (503 from `_load`), or not at all once loaded.
        if self._available:
            return True
        if not all(importlib.util.find_spec(m) for m in ("av", "numpy", "sherpa_onnx")):
            return False
        self._available = all(
            self._model_file(p) is not None
            for p in ("encoder*.onnx", "decoder*.onnx", "joiner*.onnx", "tokens.txt")
        )
        return self._available

    def decode(self, data: bytes, container: str) -> Audio:
        import av
        import numpy as np

        chunks: list[Any] = []
        total = 0
        try:
            with av.open(io.BytesIO(data), mode="r", format=container) as box:
                streams = box.streams.audio
                if not streams:
                    raise TranscribeError(400, "no audio in the upload")
                stream = streams[0]
                # Opus is what every recorder the client uses produces; any
                # other codec is a client this endpoint wasn't built for.
                if stream.codec_context.name not in ("opus", "libopus"):
                    raise TranscribeError(400, f"unsupported audio codec {stream.codec_context.name!r}")
                resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)

                def take(frames: list[Any]) -> None:
                    nonlocal total
                    for out in frames:
                        pcm = out.to_ndarray().reshape(-1)
                        chunks.append(pcm)
                        total += len(pcm)
                        check_length(total / SAMPLE_RATE)

                # Chrome's MediaRecorder writes no duration and no cues; PyAV
                # decodes the packets as a stream, which needs neither.
                for frame in box.decode(stream):
                    take(resampler.resample(frame))
                take(resampler.resample(None))
        except TranscribeError:
            raise
        except (av.FFmpegError, ValueError, EOFError) as e:
            raise TranscribeError(400, "the audio could not be decoded") from e
        samples = np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, dtype=np.float32)
        return Audio(samples=samples, seconds=len(samples) / SAMPLE_RATE)

    def load(self) -> None:
        self._load()

    def _load(self) -> Any:
        with self._lock:
            if self._recognizer is None:
                import sherpa_onnx

                start = time.monotonic()
                files = {
                    name: self._model_file(pattern)
                    for name, pattern in (
                        ("encoder", "encoder*.onnx"),
                        ("decoder", "decoder*.onnx"),
                        ("joiner", "joiner*.onnx"),
                        ("tokens", "tokens.txt"),
                    )
                }
                if any(path is None for path in files.values()):
                    raise TranscribeError(503, "speech recognition model is missing")
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                    **{name: str(path) for name, path in files.items()},
                    num_threads=self.num_threads,
                    model_type="nemo_transducer",
                )
                logger.info(
                    "speech model loaded: %s in %.1fs", self.model_dir.name, time.monotonic() - start
                )
            return self._recognizer

    def recognize(self, audio: Audio) -> str:
        if audio.seconds == 0:
            return ""
        recognizer = self._load()
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio.samples)
        recognizer.decode_stream(stream)
        return stream.result.text.strip()


def default_engine() -> SpeechEngine:
    from server.config import settings

    return ParakeetEngine(settings.stt_model_dir, settings.stt_threads)


USED_MARKER_TEXT = """\
Speech to text was first used on this box on {date}.

While this file exists, marlow-server loads the speech model when it starts,
instead of on the first dictation after each restart (server/transcribe.py,
docs/tasks/voice-mode-android.md). It does not mean anything is recording.
Delete it to go back to loading the model on first use.
"""


# --- utterances: segments in order, one at a time ------------------------------

@dataclass
class _Utterance:
    token: str
    texts: list[str] = field(default_factory=list)
    finished: bool = False
    touched: float = 0.0


@dataclass(frozen=True)
class SegmentResult:
    seq: int
    text: str
    final: bool
    # The whole utterance so far, joined. What the client uses on `final`.
    transcript: str
    # Decoding and transcribing this segment on the box, timed on the worker,
    # so a preload still running ahead of it is not counted; a model loaded
    # on first use is. The debug report sets it against the round trip.
    # Zero for a retry answered from what was already heard.
    server_seconds: float = 0.0


class Transcriber:
    """Owns the engine, the single worker thread and the open utterances.

    Everything that touches `_utterances` runs on the event loop under
    `_turn`, which is held across the worker call: that is what makes it one
    segment at a time, box-wide, and in order within an utterance.
    """

    def __init__(
        self, engine: SpeechEngine | None = None, clock=time.monotonic, used_marker_path: Path | None = None
    ):
        self._engine = engine
        self._clock = clock
        self._used_marker_path = used_marker_path
        self._marked = False
        self._executor: ThreadPoolExecutor | None = None
        self._turn: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None
        self._utterances: dict[str, _Utterance] = {}

    @property
    def engine(self) -> SpeechEngine:
        if self._engine is None:
            self._engine = default_engine()
        return self._engine

    def set_engine(self, engine: SpeechEngine | None) -> None:
        """Tests: swap in a fake (None goes back to the real one)."""
        self._engine = engine
        self._utterances.clear()

    def available(self) -> bool:
        return self.engine.available()

    @property
    def used_marker_path(self) -> Path:
        if self._used_marker_path is None:
            from server.config import settings

            self._used_marker_path = settings.stt_used_marker_path
        return self._used_marker_path

    def preload_if_used(self) -> None:
        """At start-up: load the model in the background on a box where
        anyone has ever dictated. Never awaited, never fatal; a failure is
        logged and the first segment tries again."""
        try:
            if not self.used_marker_path.exists() or not self.engine.available():
                return
        except OSError:
            return

        def load() -> None:
            try:
                self.engine.load()
            except Exception:
                logger.exception("could not preload the speech model")

        logger.info("speech to text has been used on this box: loading the model")
        self._worker().submit(load)

    def shutdown(self) -> None:
        """Server stop: drop a preload or segment not yet started. One under
        way still finishes before the process exits (the pool's threads are
        joined at exit), which is seconds at most."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def _mark_used(self) -> None:
        # Checked once per process; written once per box, so the date inside
        # stays the first use. Says what it is for whoever finds it later.
        if self._marked:
            return
        self._marked = True
        try:
            with self.used_marker_path.open("x") as f:
                f.write(USED_MARKER_TEXT.format(date=time.strftime("%Y-%m-%d")))
        except FileExistsError:
            pass
        except OSError as e:
            logger.warning("could not record that speech to text has been used: %s", e)

    def _lock(self) -> asyncio.Lock:
        # Made on first use, inside the running loop, so a test's fresh loop
        # never inherits a lock bound to an earlier one.
        loop = asyncio.get_running_loop()
        if self._turn is None or self._turn[0] is not loop:
            self._turn = (loop, asyncio.Lock())
        return self._turn[1]

    def _worker(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        return self._executor

    def _sweep(self, now: float) -> None:
        for uid in [u for u, s in self._utterances.items() if now - s.touched > UTTERANCE_TTL_SECONDS]:
            del self._utterances[uid]
        while len(self._utterances) >= MAX_UTTERANCES:
            oldest = min(self._utterances, key=lambda u: self._utterances[u].touched)
            del self._utterances[oldest]

    async def segment(
        self, *, token: str, utterance_id: str, seq: int, final: bool, data: bytes, container: str
    ) -> SegmentResult:
        engine = self.engine
        if not engine.available():
            raise TranscribeError(503, "speech recognition is not set up on this box")
        async with self._lock():
            now = self._clock()
            state = self._utterances.get(utterance_id)
            if state is not None and state.token != token:
                # Another device's id, guessed or collided: never theirs to
                # read or extend.
                raise TranscribeError(409, "utterance id already in use")
            if state is None:
                if seq != 0:
                    raise TranscribeError(409, "unknown utterance; start at segment 0")
                self._sweep(now)
                state = self._utterances[utterance_id] = _Utterance(token=token)
            state.touched = now
            if seq < len(state.texts):
                # A retry of a segment already done (its answer was lost on the
                # way back): the same answer, without transcribing it again.
                if final and seq == len(state.texts) - 1:
                    state.finished = True
                return self._result(state, seq, final)
            if state.finished:
                raise TranscribeError(409, "utterance already finished")
            if seq > len(state.texts):
                raise TranscribeError(409, f"segment {len(state.texts)} is missing")

            loop = asyncio.get_running_loop()
            text, spent = await loop.run_in_executor(self._worker(), self._run, engine, data, container)
            state.texts.append(text)
            state.finished = final
            state.touched = self._clock()
            self._mark_used()
            logger.info("transcribed segment %d%s in %.2fs", seq, " (final)" if final else "", spent)
            return replace(self._result(state, seq, final), server_seconds=round(spent, 3))

    @staticmethod
    def _run(engine: SpeechEngine, data: bytes, container: str) -> tuple[str, float]:
        # Timed here, on the worker: the wait to reach it is not this segment's
        # (`/code-review high`, 2026-10-23).
        started = time.monotonic()
        audio = engine.decode(data, container)
        check_length(audio.seconds)
        return engine.recognize(audio), time.monotonic() - started

    @staticmethod
    def _result(state: _Utterance, seq: int, final: bool) -> SegmentResult:
        transcript = " ".join(t for t in (s.strip() for s in state.texts) if t)
        return SegmentResult(seq=seq, text=state.texts[seq], final=final, transcript=transcript)


transcriber = Transcriber()


# --- the deploy's speed check ---------------------------------------------------

BENCH_SECONDS = 15


def bench(model_dir: Path, threads: int | None = None) -> float:
    """Seconds to transcribe 15 s of speech, the wait after the speaker stops
    on a typical utterance. The speech is the model's own sample clips, tiled.
    The best of two, after a warm-up, as scripts/parakeet-timing.sh measures."""
    import wave

    import numpy as np

    # The server's own thread count (config.py `stt_threads`), so the figure
    # describes what the box will run.
    if threads is None:
        threads = int(os.environ.get("MARLOW_STT_THREADS") or os.cpu_count() or 1)
    engine = ParakeetEngine(model_dir, threads)
    engine.load()
    clips = []
    for path in sorted((model_dir / "test_wavs").glob("*.wav")):
        with wave.open(str(path)) as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
                continue
            clips.append(np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768)
    if not clips:
        raise RuntimeError(f"no 16 kHz mono sample clips in {model_dir / 'test_wavs'}")
    pool = np.concatenate([*clips, np.zeros(SAMPLE_RATE // 2, dtype=np.float32)])
    need = SAMPLE_RATE * BENCH_SECONDS
    samples = np.tile(pool, need // len(pool) + 1)[:need]
    engine.recognize(Audio(samples[: SAMPLE_RATE * 2], 2.0))

    def once() -> float:
        start = time.monotonic()
        engine.recognize(Audio(samples, float(BENCH_SECONDS)))
        return time.monotonic() - start

    return min(once(), once())


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "bench":
        print("usage: python -m server.transcribe bench <model dir>", file=sys.stderr)
        sys.exit(2)
    print(f"{bench(Path(sys.argv[2]).expanduser()):.2f}")

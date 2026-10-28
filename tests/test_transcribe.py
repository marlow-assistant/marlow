"""Speech to text for the web client (server/transcribe.py,
docs/tasks/voice-mode-android.md): segments in, in order, one at a time, text
back. The route and the ordering run against a fake engine, so nothing here
needs the model. The PyAV decoder is tested on Opus it encodes itself, when
PyAV is installed; the real model only with MARLOW_STT_TEST_MODEL_DIR set.
"""
from __future__ import annotations

import asyncio
import io
import math
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import transcribe
from server.app import public_app
from server.config import Settings
from server.transcribe import Audio, ParakeetEngine, Transcriber, TranscribeError, transcriber

WEBM = {"Content-Type": "audio/webm;codecs=opus"}


class FakeEngine:
    """Decodes `b"<seconds>|<text>"` and hears `<text>`. Counts calls, and
    records how many ran at once."""

    def __init__(self, *, available: bool = True, delay: float = 0.0):
        self._available = available
        self.delay = delay
        self.decoded: list[bytes] = []
        self.containers: list[str] = []
        self.running = 0
        self.most_at_once = 0
        self._count = threading.Lock()

    def available(self) -> bool:
        return self._available

    def decode(self, data: bytes, container: str) -> Audio:
        self.decoded.append(data)
        self.containers.append(container)
        seconds, _, text = data.decode().partition("|")
        if seconds == "bad":
            raise TranscribeError(400, "the audio could not be decoded")
        if seconds == "boom":
            raise RuntimeError("model crashed")
        return Audio(samples=text, seconds=float(seconds))

    loads = 0

    def load(self) -> None:
        self.loads += 1

    def recognize(self, audio: Audio) -> str:
        with self._count:
            self.running += 1
            self.most_at_once = max(self.most_at_once, self.running)
        try:
            time.sleep(self.delay)
            return audio.samples
        finally:
            with self._count:
                self.running -= 1


@pytest.fixture
def engine(tmp_path, monkeypatch):
    fake = FakeEngine()
    transcriber.set_engine(fake)
    # The "used once" marker goes to this test's own folder, never the shared
    # sandbox's data_dir, where it would make every later app start preload.
    monkeypatch.setattr(transcriber, "_used_marker_path", tmp_path / "stt_has_been_used")
    monkeypatch.setattr(transcriber, "_marked", False)
    yield fake
    transcriber.set_engine(None)


@pytest.fixture
def client():
    with TestClient(public_app) as c:
        yield c


@pytest.fixture
def headers(paired_device_token):
    return {"Authorization": f"Bearer {paired_device_token}", **WEBM}


def _post(client, headers, body: bytes, *, uid="utt-00000001", seq=0, final=False):
    return client.post(
        "/v1/transcribe",
        params={"utterance_id": uid, "seq": seq, "final": "true" if final else "false"},
        content=body,
        headers=headers,
    )


# --- the route ---------------------------------------------------------------------

def test_transcription_needs_a_device_token(client, engine):
    assert _post(client, WEBM, b"1|hi").status_code == 401
    assert client.get("/v1/transcribe").status_code == 401
    assert engine.decoded == []


def test_status_says_whether_the_box_can_transcribe(client, headers, engine):
    assert client.get("/v1/transcribe", headers=headers).json() == {"available": True}
    transcriber.set_engine(FakeEngine(available=False))
    assert client.get("/v1/transcribe", headers=headers).json() == {"available": False}


def test_a_box_without_the_model_answers_503(client, headers):
    transcriber.set_engine(FakeEngine(available=False))
    try:
        resp = _post(client, headers, b"1|hi", final=True)
    finally:
        transcriber.set_engine(None)
    assert resp.status_code == 503


def test_segments_are_joined_into_the_utterance(client, headers, engine):
    first = _post(client, headers, b"12|Pick up oat milk", seq=0)
    assert first.status_code == 200
    body = first.json()
    assert body.pop("server_seconds") >= 0
    assert body == {"seq": 0, "text": "Pick up oat milk", "final": False, "transcript": "Pick up oat milk"}
    # A segment that heard nothing (a cut inside a long pause) adds no space.
    assert _post(client, headers, b"3|", seq=1).json()["text"] == ""
    last = _post(client, headers, b"4|and eggs.", seq=2, final=True).json()
    last.pop("server_seconds")
    assert last == {"seq": 2, "text": "and eggs.", "final": True, "transcript": "Pick up oat milk and eggs."}
    assert engine.containers == ["matroska"] * 3


def test_ogg_opus_is_accepted_and_anything_else_is_refused(client, headers, engine):
    ogg = {**headers, "Content-Type": "audio/ogg; codecs=opus"}
    assert _post(client, ogg, b"1|hi", final=True).status_code == 200
    assert engine.containers == ["ogg"]
    for media in ("audio/mp4", "audio/wav", "application/octet-stream", ""):
        resp = _post(client, {**headers, "Content-Type": media}, b"1|hi", uid="utt-00000002", final=True)
        assert resp.status_code == 415, media
    assert len(engine.decoded) == 1


def test_an_empty_or_oversized_body_is_refused_unread(client, headers, engine):
    assert _post(client, headers, b"").status_code == 400
    assert _post(client, headers, b"1|" + b"x" * transcribe.MAX_SEGMENT_BYTES).status_code == 413
    assert engine.decoded == []


def test_a_segment_over_35_seconds_is_refused(client, headers, engine):
    resp = _post(client, headers, b"36|a very long ramble", final=True)
    assert resp.status_code == 413
    assert _post(client, headers, b"34|just under", final=True).status_code == 200


def test_bad_parameters_are_refused(client, headers, engine):
    assert _post(client, headers, b"1|hi", uid="short").status_code == 422
    assert _post(client, headers, b"1|hi", uid="../../etc/passwd").status_code == 422
    assert _post(client, headers, b"1|hi", seq=-1).status_code == 422
    assert _post(client, headers, b"1|hi", seq=transcribe.MAX_SEGMENTS).status_code == 422
    assert engine.decoded == []


def test_segments_must_come_in_order(client, headers, engine):
    assert _post(client, headers, b"1|two", seq=1).status_code == 409
    assert _post(client, headers, b"1|one", seq=0).status_code == 200
    assert _post(client, headers, b"1|three", seq=2).status_code == 409
    assert engine.decoded == [b"1|one"]


def test_a_retried_segment_is_answered_again_without_transcribing_it_twice(client, headers, engine):
    _post(client, headers, b"1|one", seq=0)
    again = _post(client, headers, b"1|one", seq=0)
    assert again.status_code == 200 and again.json()["text"] == "one"
    last = _post(client, headers, b"1|two", seq=1, final=True)
    retried = _post(client, headers, b"1|two", seq=1, final=True)
    assert retried.json() == {**last.json(), "server_seconds": 0.0}
    assert engine.decoded == [b"1|one", b"1|two"]


def test_nothing_is_added_to_a_finished_utterance(client, headers, engine):
    _post(client, headers, b"1|done", seq=0, final=True)
    assert _post(client, headers, b"1|more", seq=1).status_code == 409


def test_an_utterance_belongs_to_the_device_that_started_it(client, headers, engine, pair_device):
    other = {"Authorization": f"Bearer {pair_device('second-device')}", **WEBM}
    _post(client, headers, b"1|private words", seq=0)
    # Neither its text (a retry of seq 0) nor its next segment.
    assert _post(client, other, b"1|x", seq=0).status_code == 409
    assert _post(client, other, b"1|x", seq=1).status_code == 409


def test_a_failed_segment_can_be_sent_again(client, headers, engine):
    assert _post(client, headers, b"bad|", seq=0).status_code == 400
    assert _post(client, headers, b"1|second try", seq=0, final=True).json()["transcript"] == "second try"


def test_an_engine_crash_is_a_500_with_nothing_leaked(client, headers, engine):
    resp = _post(client, headers, b"boom|", final=True)
    assert resp.status_code == 500
    assert resp.json() == {"detail": "transcription failed"}


# --- once used, always loaded ------------------------------------------------------

def test_the_first_transcription_marks_the_box_as_using_it(client, headers, engine, tmp_path):
    marker = tmp_path / "stt_has_been_used"
    assert _post(client, headers, b"bad|", final=True).status_code == 400
    assert not marker.exists(), "a failed segment is not use"
    assert _post(client, headers, b"1|hi", uid="utt-00000009", final=True).status_code == 200
    note = marker.read_text()
    assert "first used on this box on 20" in note and "Delete it" in note
    # Written once per box: after a restart (a fresh process), a later use
    # leaves the first date alone.
    marker.write_text("first used on this box on 2000-01-01")
    transcriber._marked = False
    assert _post(client, headers, b"1|again", uid="utt-00000010", final=True).status_code == 200
    assert marker.read_text() == "first used on this box on 2000-01-01"


def test_a_box_that_has_used_it_loads_the_model_at_start(tmp_path):
    fake = FakeEngine()
    t = Transcriber(engine=fake, used_marker_path=tmp_path / "stt_has_been_used")
    t.preload_if_used()
    assert t._executor is None and fake.loads == 0, "never used: stays lazy"

    (tmp_path / "stt_has_been_used").touch()
    t = Transcriber(engine=fake, used_marker_path=tmp_path / "stt_has_been_used")
    t.preload_if_used()
    t._worker().shutdown(wait=True)
    assert fake.loads == 1


def test_preloading_skips_a_box_without_the_model(tmp_path):
    (tmp_path / "stt_has_been_used").touch()
    fake = FakeEngine(available=False)
    t = Transcriber(engine=fake, used_marker_path=tmp_path / "stt_has_been_used")
    t.preload_if_used()
    assert t._executor is None and fake.loads == 0


def test_a_failed_preload_is_logged_not_raised(tmp_path, caplog):
    (tmp_path / "stt_has_been_used").touch()

    class Broken(FakeEngine):
        def load(self) -> None:
            raise RuntimeError("corrupt model")

    t = Transcriber(engine=Broken(), used_marker_path=tmp_path / "stt_has_been_used")
    t.preload_if_used()
    t._worker().shutdown(wait=True)
    assert "could not preload" in caplog.text


def test_a_segment_queued_behind_a_preload_is_timed_without_it(tmp_path):
    """server_seconds is the segment's own work: the debug report sets it
    against the round trip, so a preload ahead of it on the worker belongs to
    the wait, not to transcription."""
    (tmp_path / "stt_has_been_used").touch()

    class SlowLoad(FakeEngine):
        def load(self) -> None:
            time.sleep(0.3)

    t = Transcriber(engine=SlowLoad(), used_marker_path=tmp_path / "stt_has_been_used")
    t.preload_if_used()
    result = asyncio.run(
        t.segment(token="tok", utterance_id="utt-00000001", seq=0, final=True, data=b"1|hi", container="matroska")
    )
    assert result.transcript == "hi"
    assert result.server_seconds < 0.2


# --- ordering and memory -----------------------------------------------------------

def test_one_segment_at_a_time_across_every_utterance(tmp_path):
    fake = FakeEngine(delay=0.05)
    t = Transcriber(engine=fake, used_marker_path=tmp_path / "stt_has_been_used")

    async def run():
        return await asyncio.gather(*(
            t.segment(token="tok", utterance_id=f"utt-0000000{i}", seq=0, final=True,
                      data=f"1|u{i}".encode(), container="matroska")
            for i in range(4)
        ))

    results = asyncio.run(run())
    assert sorted(r.transcript for r in results) == ["u0", "u1", "u2", "u3"]
    assert fake.most_at_once == 1


def test_abandoned_utterances_are_forgotten(tmp_path):
    now = [0.0]
    t = Transcriber(engine=FakeEngine(), clock=lambda: now[0], used_marker_path=tmp_path / "stt_has_been_used")

    async def seg(uid: str, seq: int):
        return await t.segment(token="tok", utterance_id=uid, seq=seq, final=False,
                               data=b"1|x", container="matroska")

    async def run():
        await seg("utt-abandoned", 0)
        now[0] += transcribe.UTTERANCE_TTL_SECONDS + 1
        await seg("utt-fresh-one", 0)
        with pytest.raises(TranscribeError) as e:
            await seg("utt-abandoned", 1)
        assert e.value.status == 409
        # And never more than the cap held at once.
        for i in range(transcribe.MAX_UTTERANCES + 5):
            await seg(f"utt-many-{i:04d}", 0)
        assert len(t._utterances) <= transcribe.MAX_UTTERANCES

    asyncio.run(run())


def test_the_model_name_cannot_leave_the_models_folder(monkeypatch):
    for bad in ("..", "../secrets", "a/b", ".hidden", ""):
        monkeypatch.setenv("MARLOW_STT_MODEL", bad)
        if bad == "":
            # Empty means "not set": the default.
            assert Settings().stt_model_dir.parent == Settings().data_dir / "models"
            continue
        with pytest.raises(ValueError):
            Settings()
    monkeypatch.setenv("MARLOW_STT_MODEL", "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8")
    s = Settings()
    assert s.stt_model_dir == s.data_dir / "models" / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"


def test_the_real_engine_is_unavailable_without_its_model(tmp_path):
    assert ParakeetEngine(tmp_path / "nowhere", 1).available() is False


# --- the PyAV decoder, on Opus it encodes itself -----------------------------------

try:
    import av
    import numpy as np
except ImportError:  # pragma: no cover - a dev venv without the voice extras
    av = np = None

needs_pyav = pytest.mark.skipif(av is None, reason="PyAV not installed (server/requirements-voice.txt)")


def _opus(seconds: float, *, container: str = "webm", codec: str = "libopus", rate: int = 48_000) -> bytes:
    """A tone, by default encoded the way Chrome records: mono Opus at 48 kHz, 24 kbps,
    in WebM. Not Chrome's own muxer (the task doc's open item)."""
    buf = io.BytesIO()
    with av.open(buf, mode="w", format=container) as out:
        stream = out.add_stream(codec, rate=rate, layout="mono")
        stream.bit_rate = 24_000
        n = int(seconds * rate)
        t = np.arange(n) / rate
        samples = (0.3 * np.sin(2 * math.pi * 440 * t)).astype(np.float32).reshape(1, -1)
        frame_size = 960
        for i in range(0, n, frame_size):
            frame = av.AudioFrame.from_ndarray(samples[:, i : i + frame_size], format="flt", layout="mono")
            frame.sample_rate = rate
            frame.pts = i
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return buf.getvalue()


@needs_pyav
def test_webm_opus_decodes_to_16khz_mono_of_the_right_length(tmp_path):
    audio = ParakeetEngine(tmp_path, 1).decode(_opus(3.0), "matroska")
    assert audio.samples.dtype == np.float32 and audio.samples.ndim == 1
    assert abs(audio.seconds - 3.0) < 0.1
    assert 0.1 < float(np.abs(audio.samples).max()) < 0.5


@needs_pyav
def test_a_long_segment_is_refused_while_decoding(tmp_path):
    with pytest.raises(TranscribeError) as e:
        ParakeetEngine(tmp_path, 1).decode(_opus(transcribe.MAX_SEGMENT_SECONDS + 3), "matroska")
    assert e.value.status == 413


@needs_pyav
def test_garbage_and_other_codecs_are_refused(tmp_path):
    engine = ParakeetEngine(tmp_path, 1)
    for data, container in (
        (b"\x1a\x45\xdf\xa3 not really webm", "matroska"),
        (os.urandom(4096), "matroska"),
        (_opus(1.0), "ogg"),  # WebM bytes declared as Ogg
        (_opus(1.0, container="matroska", codec="flac"), "matroska"),
    ):
        with pytest.raises(TranscribeError) as e:
            engine.decode(data, container)
        assert e.value.status == 400


# --- the real model: opt-in --------------------------------------------------------

REAL_MODEL = os.environ.get("MARLOW_STT_TEST_MODEL_DIR")


@needs_pyav
@pytest.mark.skipif(not REAL_MODEL, reason="set MARLOW_STT_TEST_MODEL_DIR to a sherpa-onnx Parakeet folder")
def test_the_real_model_hears_its_own_sample_through_opus(tmp_path):
    """The model's sample recording, encoded to WebM/Opus and back through
    the whole engine. Run on a box with the model:
    MARLOW_STT_TEST_MODEL_DIR=~/.marlow/data/models/<name> .venv/bin/pytest -q tests/test_transcribe.py
    """
    import wave

    model_dir = Path(REAL_MODEL).expanduser()
    sample = sorted((model_dir / "test_wavs").glob("*.wav"))[0]
    with wave.open(str(sample)) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="webm") as out:
        stream = out.add_stream("libopus", rate=48_000, layout="mono")
        stream.bit_rate = 24_000
        resampler = av.AudioResampler(format="flt", layout="mono", rate=48_000)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="flt", layout="mono")
        frame.sample_rate = rate
        for resampled in resampler.resample(frame) + resampler.resample(None):
            for packet in stream.encode(resampled):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)

    engine = ParakeetEngine(model_dir, os.cpu_count() or 1)
    assert engine.available()
    t = Transcriber(engine=engine, used_marker_path=tmp_path / "stt_has_been_used")
    result = asyncio.run(t.segment(token="tok", utterance_id="utt-realmodel", seq=0, final=True,
                                   data=buf.getvalue(), container="matroska"))
    words = result.transcript.lower()
    assert len(words.split()) >= 5, result.transcript


@needs_pyav
@pytest.mark.skipif(not REAL_MODEL, reason="set MARLOW_STT_TEST_MODEL_DIR to a sherpa-onnx Parakeet folder")
def test_the_deploy_speed_check_times_the_real_model():
    from server.transcribe import bench

    seconds = bench(Path(REAL_MODEL).expanduser())
    assert 0 < seconds < 30

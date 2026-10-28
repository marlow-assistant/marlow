"""deploy/install-voice.sh: speech to text on by default at every update and
install (docs/tasks/voice-mode-android.md). Run against a stub venv (pip and
python that only log) and a local model archive, so no network and no model.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SCRIPT = DEPLOY / "install-voice.sh"
MODEL = "test-model"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "curl", "sha256sum", "bzip2")),
    reason="needs bash, curl, sha256sum and bzip2 (a Mac without coreutils has no sha256sum)",
)


def _archive(tmp: Path, extra: str | None = None, link: bool = False) -> tuple[Path, str]:
    src = tmp / "src" / MODEL
    src.mkdir(parents=True)
    for name in ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx"):
        (src / name).write_text(name)
    if link:
        (src / "tokens.txt").unlink()
        (src / "tokens.txt").symlink_to("/etc/passwd")
    path = tmp / f"{MODEL}.tar.bz2"
    with tarfile.open(path, "w:bz2") as tar:
        tar.add(src, arcname=MODEL)
        if extra:
            stray = tmp / "src" / "stray"
            stray.write_text("x")
            tar.add(stray, arcname=extra)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def box(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    log = tmp_path / "calls.log"
    for tool in ("pip", "python"):
        stub = venv / "bin" / tool
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "{tool} $* log_dir=${{MARLOW_LOG_DIR:-}}" >> "{log}"\n'
            f'if [[ {tool} == pip ]]; then exit "${{FAKE_PIP_EXIT:-0}}"; fi\n'
            'echo "${FAKE_BENCH-0.80}"\n'
        )
        stub.chmod(0o755)
    archive, sha = _archive(tmp_path)
    data = tmp_path / "data"

    def run(**env: str) -> subprocess.CompletedProcess[str]:
        full = {
            "PATH": os.environ["PATH"],
            "MARLOW_STT_MODEL": MODEL,
            "MARLOW_STT_MODEL_URL": archive.as_uri(),
            "MARLOW_STT_MODEL_SHA256": sha,
            **env,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), str(DEPLOY.parent), str(venv), str(data)],
            capture_output=True, text=True, env=full, timeout=60,
        )

    def calls() -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    return {"run": run, "calls": calls, "data": data, "archive": archive, "tmp": tmp_path}


def test_a_fresh_box_gets_the_packages_the_model_and_one_speed_check(box):
    r = box["run"]()
    assert r.returncode == 0, r.stderr
    model = box["data"] / "models" / MODEL
    assert (model / "encoder.int8.onnx").is_file()
    assert (model / ".bench-seconds").read_text().strip() == "0.80"
    assert "ready: 15 s of speech in 0.80 s" in r.stdout
    calls = box["calls"]()
    assert any(c.startswith("pip install") and "requirements-voice.txt" in c for c in calls)
    assert sum("server.transcribe bench" in c for c in calls) == 1
    # Private like the rest of data_dir, and no download leftovers.
    assert oct((box["data"] / "models").stat().st_mode & 0o777) == "0o700"
    assert not list((box["data"] / "models").glob(".download-*"))


def test_the_next_update_neither_downloads_nor_times_again(box):
    box["run"]()
    box["archive"].unlink()
    r = box["run"]()
    assert r.returncode == 0
    assert "model present" in r.stdout
    assert sum("server.transcribe bench" in c for c in box["calls"]()) == 1


def test_a_slow_box_is_installed_with_a_loud_warning(box):
    r = box["run"](FAKE_BENCH="3.40")
    assert r.returncode == 0
    assert "WARNING: speech to text is SLOW on this box" in r.stdout
    assert "took 3.40 s" in r.stdout
    assert (box["data"] / "models" / MODEL / "tokens.txt").is_file()
    # Measured again on the next update, and still slow: warned again.
    assert "SLOW" in box["run"](FAKE_BENCH="3.40").stdout


def test_a_checksum_mismatch_installs_nothing(box):
    r = box["run"](MARLOW_STT_MODEL_SHA256="0" * 64)
    assert r.returncode == 0
    assert "checksum mismatch" in r.stdout
    assert not (box["data"] / "models" / MODEL).exists()
    assert not list((box["data"] / "models").glob(".download-*"))


def test_an_archive_with_anything_beside_the_model_folder_is_refused(box):
    tmp = box["tmp"] / "evil"
    tmp.mkdir()
    archive, sha = _archive(tmp, extra="../outside")
    r = box["run"](MARLOW_STT_MODEL_URL=archive.as_uri(), MARLOW_STT_MODEL_SHA256=sha)
    assert r.returncode == 0
    assert "holds more than" in r.stdout
    assert not (box["data"] / "models" / MODEL).exists()
    assert not (box["data"] / "outside").exists()


def test_an_archive_that_climbs_out_inside_the_model_folder_is_refused(box):
    tmp = box["tmp"] / "climb"
    tmp.mkdir()
    archive, sha = _archive(tmp, extra=f"{MODEL}/../../outside")
    r = box["run"](MARLOW_STT_MODEL_URL=archive.as_uri(), MARLOW_STT_MODEL_SHA256=sha)
    assert "holds more than" in r.stdout
    assert not (box["data"] / "models" / MODEL).exists()


def test_an_archive_with_a_link_in_it_is_refused(box):
    tmp = box["tmp"] / "link"
    tmp.mkdir()
    archive, sha = _archive(tmp, link=True)
    r = box["run"](MARLOW_STT_MODEL_URL=archive.as_uri(), MARLOW_STT_MODEL_SHA256=sha)
    assert "links or special files" in r.stdout
    assert not (box["data"] / "models" / MODEL).exists()


def test_a_failed_speed_check_says_the_mic_is_on_and_tries_again_next_time(box):
    r = box["run"](FAKE_BENCH="")
    assert r.returncode == 0
    assert "dictation is on, but the speed check failed" in r.stdout
    assert "stay off" not in r.stdout
    assert (box["data"] / "models" / MODEL / "tokens.txt").is_file()
    r = box["run"]()
    assert "ready: 15 s of speech in 0.80 s" in r.stdout


def test_a_slow_figure_is_measured_again_and_a_fast_one_is_kept(box):
    box["run"](FAKE_BENCH="2.60")
    r = box["run"](FAKE_BENCH="0.90")
    assert "ready: 15 s of speech in 0.90 s" in r.stdout
    box["run"](FAKE_BENCH="5.00")
    assert sum("server.transcribe bench" in c for c in box["calls"]()) == 2


def test_the_speed_check_logs_to_a_folder_of_its_own(box):
    """Never the running server's marlow-server.log: two processes rotating one
    file is the hazard server/log.py describes."""
    box["run"]()
    bench = next(c for c in box["calls"]() if "server.transcribe bench" in c)
    log_dir = bench.rsplit("log_dir=", 1)[1]
    assert log_dir and not Path(log_dir).exists(), "a temporary folder, removed after"


def test_a_failed_package_install_leaves_the_mic_off_and_the_deploy_going(box):
    r = box["run"](FAKE_PIP_EXIT="1")
    assert r.returncode == 0
    assert "couldn't install the speech packages" in r.stdout
    assert not (box["data"] / "models").exists()


def test_it_can_be_turned_off(box):
    r = box["run"](MARLOW_VOICE="0")
    assert r.returncode == 0 and box["calls"]() == []
    (box["data"] / "models").mkdir(parents=True)
    (box["data"] / "models" / "OFF").touch()
    r = box["run"]()
    assert "turned off on this box" in r.stdout and box["calls"]() == []


def test_a_model_without_a_pinned_checksum_is_not_downloaded(box):
    r = box["run"](MARLOW_STT_MODEL="some-other-model", MARLOW_STT_MODEL_SHA256="")
    assert r.returncode == 0
    assert "no pinned checksum" in r.stdout
    assert box["calls"]() == []


@pytest.mark.parametrize("bad", ["../x", "a/b", ".hidden"])
def test_a_model_name_that_is_a_path_is_refused(box, bad):
    r = box["run"](MARLOW_STT_MODEL=bad)
    assert r.returncode == 0 and "not a plain folder name" in r.stdout
    assert box["calls"]() == []


def test_only_the_small_model_is_ever_downloaded():
    """The 0.6B is too big to put on every box unasked (Mike, 2026-10-22):
    no checksum for it, and update.sh passes no model choice through."""
    text = SCRIPT.read_text()
    block = text[text.index("    sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8)"):]
    assert len(block.split("\n", 2)[1].strip().removeprefix("SHA256=").rstrip(" ;")) == 64
    assert "0.6b" not in text[text.index('case "$MODEL" in'):text.index("esac")]
    update = (DEPLOY / "update.sh").read_text()
    call = update[update.index('    sudo -u "$MARLOW_USER" -H env MARLOW_VOICE'):]
    assert "MARLOW_STT_MODEL" not in call[: call.index("|| true")]


def test_the_large_model_is_refused_without_a_checksum(box):
    r = box["run"](MARLOW_STT_MODEL="sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8", MARLOW_STT_MODEL_SHA256="")
    assert "no pinned checksum" in r.stdout
    assert not (box["data"] / "models").exists()


@pytest.mark.parametrize("script", ["update.sh", "install.sh"])
def test_update_and_install_run_it_and_never_fail_on_it(script):
    text = (DEPLOY / script).read_text()
    call = text[text.index("deploy/install-voice.sh\""):]
    assert call[: call.index("\n")].rstrip().endswith("\\")
    assert "|| true" in call[: call.index("\n", call.index("\n") + 1)]

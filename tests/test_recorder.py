"""Recorder tests: brief §6 ``recorder.py``, with no audio hardware involved.

``shutil.which`` is faked to control backend detection and ``subprocess.Popen``
is replaced by :class:`FakeProcess`, which writes a real (tiny) WAV file so the
duration arithmetic and the header-finalising shutdown ladder are exercised for
real. Every path used is inside ``tmp_path``.
"""

from __future__ import annotations

import io
import shutil
import signal
import subprocess
import time
import wave
from pathlib import Path

import pytest

import sarvam_dictation.recorder as recorder_mod
from sarvam_dictation.recorder import (
    Recorder,
    RecorderError,
    Recording,
    detect_backend,
    normalize_wav,
    wav_duration_ms,
)

BINARY_FOR_BACKEND = {"pipewire": "pw-record", "pulse": "parec", "alsa": "arecord"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def fake_which(available):
    """Return a ``shutil.which`` replacement answering only for ``available``."""

    def _which(name, *args, **kwargs):
        return f"/usr/bin/{name}" if name in available else None

    return _which


@pytest.fixture
def which(monkeypatch):
    """Install a controllable ``shutil.which``; call it with the available tools."""

    def _install(available):
        available = set(available)
        replacement = fake_which(available)
        monkeypatch.setattr(shutil, "which", replacement)
        if hasattr(recorder_mod, "which"):
            monkeypatch.setattr(recorder_mod, "which", replacement)
        return replacement

    return _install


@pytest.fixture
def all_binaries(which):
    """Pretend every capture tool is installed."""
    which({"pw-record", "parec", "arecord"})


class FakeProcess:
    """Stand-in for ``subprocess.Popen`` that behaves like a capture tool.

    It writes a real WAV to the output path it finds in ``args`` (or to the
    ``stdout`` file object it was handed), and records every signal it receives
    so the shutdown ladder can be asserted exactly.
    """

    last_instance: "FakeProcess | None" = None

    def __init__(
        self,
        args,
        *,
        wav_bytes=b"",
        ignore_signals=frozenset(),
        stdout=None,
        stderr=None,
        stdin=None,
        **kwargs,
    ):
        self.args = list(args) if isinstance(args, (list, tuple)) else str(args).split()
        self.pid = 4242
        self.returncode = None
        self.signals = []
        self.wait_calls = []
        self._ignore = set(ignore_signals)
        self._alive = True
        self._stdout_bytes = wav_bytes
        self.stdout = stdout
        self.stderr = stderr
        FakeProcess.last_instance = self
        self._write_output(wav_bytes)

    # -- output -----------------------------------------------------------
    def _output_path(self):
        for arg in self.args:
            text = str(arg)
            if text.startswith("--file="):
                return Path(text.split("=", 1)[1])
            if text.startswith("--target-file="):
                return Path(text.split("=", 1)[1])
            if text.endswith(".wav"):
                return Path(text)
        return None

    def _write_output(self, wav_bytes):
        target = self._output_path()
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(wav_bytes)
            return
        stdout = self.stdout
        if hasattr(stdout, "write"):
            stdout.write(wav_bytes)
            if hasattr(stdout, "flush"):
                stdout.flush()
            if hasattr(stdout, "seek"):
                stdout.seek(0)
            return
        # stdout was PIPE (or anything unreadable): expose the bytes for read().
        self.stdout = io.BytesIO(wav_bytes)

    # -- process API ------------------------------------------------------
    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGKILL or sig not in self._ignore:
            self._alive = False
            # pw-record/arecord handle SIGINT gracefully and exit 0; the
            # harsher signals are what leaves a negative return code behind.
            self.returncode = 0 if sig == signal.SIGINT else -int(sig)

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self._alive:
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)
        return self.returncode

    def poll(self):
        return self.returncode

    def communicate(self, input=None, timeout=None):
        self.wait_calls.append(timeout)
        data = b""
        if hasattr(self.stdout, "read"):
            data = self.stdout.read()
        self._alive = False
        self.returncode = 0 if self.returncode is None else self.returncode
        return data, b""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def popen(monkeypatch):
    """Patch ``subprocess.Popen`` with a configured :class:`FakeProcess`."""
    real_popen = subprocess.Popen

    def _install(*, duration_s=1.0, ignore_signals=frozenset(), empty=False, missing=False):
        from conftest import build_wav_bytes

        if empty or missing:
            wav_bytes = b""
        else:
            wav_bytes = build_wav_bytes(duration_s=duration_s, sample_rate=16000)

        class BoundProcess(FakeProcess):
            def __init__(self, args, **kwargs):
                kwargs.setdefault("wav_bytes", wav_bytes)
                kwargs.setdefault("ignore_signals", ignore_signals)
                super().__init__(args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", BoundProcess)
        if hasattr(recorder_mod, "Popen"):
            monkeypatch.setattr(recorder_mod, "Popen", BoundProcess)
        return BoundProcess

    yield _install
    monkeypatch.setattr(subprocess, "Popen", real_popen)


@pytest.fixture
def fast_sleep(monkeypatch):
    """Make the shutdown ladder's waits instantaneous."""
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


def make_recorder(config, tmp_path):
    return Recorder(config, workdir=tmp_path / "rec")


# --------------------------------------------------------------------------
# detect_backend
# --------------------------------------------------------------------------


def test_detect_backend_prefers_pipewire_when_all_tools_exist(all_binaries):
    assert detect_backend("auto") == "pipewire"


def test_detect_backend_prefers_pulse_over_alsa(which):
    which({"parec", "arecord"})
    assert detect_backend("auto") == "pulse"


def test_detect_backend_falls_back_to_alsa(which):
    which({"arecord"})
    assert detect_backend("auto") == "alsa"


def test_detect_backend_without_any_tool_reports_no_audio_device(which):
    which(set())
    with pytest.raises(RecorderError) as excinfo:
        detect_backend("auto")
    assert excinfo.value.code == "no_audio_device"


def test_detect_backend_honours_an_explicit_preference(which):
    which({"arecord"})
    assert detect_backend("alsa") == "alsa"


# --------------------------------------------------------------------------
# WAV helpers
# --------------------------------------------------------------------------


def test_wav_duration_ms_reads_a_known_file(tmp_path):
    from conftest import write_wav_file

    path = write_wav_file(tmp_path / "one-and-a-half.wav", duration_s=1.5)
    assert abs(wav_duration_ms(path) - 1500) <= 2


def test_normalize_wav_produces_a_16k_mono_16bit_file(tmp_path):
    from conftest import write_wav_file

    src = write_wav_file(tmp_path / "src.wav", duration_s=1.0)
    dst = normalize_wav(src, tmp_path / "out.wav", sample_rate=16000)
    dst = Path(dst)
    assert dst.exists()
    with wave.open(str(dst), "rb") as handle:
        assert handle.getframerate() == 16000
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getnframes() > 0


def test_recording_dataclass_defaults_to_16k(tmp_path):
    recording = Recording(path=tmp_path / "a.wav", duration_ms=10)
    assert recording.sample_rate == 16000
    assert recording.duration_ms == 10


# --------------------------------------------------------------------------
# Recorder lifecycle
# --------------------------------------------------------------------------


def test_start_then_stop_returns_a_recording_with_the_measured_duration(
    config, tmp_path, all_binaries, popen
):
    popen(duration_s=1.0)
    recorder = make_recorder(config, tmp_path)

    assert recorder.is_recording is False
    recorder.start()
    assert recorder.is_recording is True

    recording = recorder.stop()
    assert recorder.is_recording is False
    assert isinstance(recording, Recording)
    assert Path(recording.path).exists()
    assert abs(recording.duration_ms - 1000) <= 50
    with wave.open(str(recording.path), "rb") as handle:
        assert handle.getframerate() == 16000
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2


def test_stop_when_not_recording_raises_audio_failed(config, tmp_path, all_binaries, popen):
    popen()
    recorder = make_recorder(config, tmp_path)
    with pytest.raises(RecorderError) as excinfo:
        recorder.stop()
    assert excinfo.value.code == "audio_failed"


def test_cancel_before_start_is_a_no_op(config, tmp_path, all_binaries, popen):
    popen()
    recorder = make_recorder(config, tmp_path)
    recorder.cancel()
    assert recorder.is_recording is False


def test_cancel_is_idempotent_and_removes_the_recording(config, tmp_path, all_binaries, popen):
    popen(duration_s=1.0)
    workdir = tmp_path / "rec"
    recorder = Recorder(config, workdir=workdir)
    recorder.start()
    assert list(workdir.glob("*.wav")), "the capture process should have written a WAV"

    recorder.cancel()
    recorder.cancel()

    assert recorder.is_recording is False
    assert list(workdir.glob("*.wav")) == []


def test_zero_length_output_raises_audio_failed(config, tmp_path, all_binaries, popen):
    popen(empty=True)
    recorder = make_recorder(config, tmp_path)
    recorder.start()
    with pytest.raises(RecorderError) as excinfo:
        recorder.stop()
    assert excinfo.value.code == "audio_failed"


def test_stop_returns_a_recording_when_capture_writes_to_a_stdout_handle(
    config, tmp_path, all_binaries, monkeypatch
):
    """A capture tool that writes its WAV to a handed-over file must work too."""
    from conftest import build_wav_bytes

    wav_bytes = build_wav_bytes(duration_s=0.5)
    target = tmp_path / "stdout-capture.wav"

    def bound_popen(args, **kwargs):
        kwargs.setdefault("wav_bytes", wav_bytes)
        kwargs.setdefault("stdout", open(target, "wb"))
        return FakeProcess(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", bound_popen)
    if hasattr(recorder_mod, "Popen"):
        monkeypatch.setattr(recorder_mod, "Popen", bound_popen)

    recorder = make_recorder(config, tmp_path)
    recorder.start()
    recording = recorder.stop()
    assert abs(recording.duration_ms - 500) <= 50


# --------------------------------------------------------------------------
# shutdown ladder
# --------------------------------------------------------------------------


def test_stop_sends_sigint_first_and_not_sigkill(config, tmp_path, all_binaries, popen, fast_sleep):
    popen(duration_s=1.0)
    recorder = make_recorder(config, tmp_path)
    recorder.start()
    recorder.stop()

    process = FakeProcess.last_instance
    assert process.signals, "stop() must signal the capture process"
    assert process.signals[0] == signal.SIGINT
    assert signal.SIGKILL not in process.signals


def test_stop_escalates_sigint_then_sigterm_then_sigkill(config, tmp_path, all_binaries, popen, fast_sleep):
    popen(duration_s=1.0, ignore_signals=frozenset({signal.SIGINT, signal.SIGTERM}))
    recorder = make_recorder(config, tmp_path)
    recorder.start()
    recorder.stop()

    process = FakeProcess.last_instance
    assert process.signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    assert len(process.wait_calls) >= 2, "the ladder must wait between escalating signals"

"""Shared fixtures for the fcitx5-sarvam-dictation daemon test suite.

The Python package lives at ``<repo_root>/daemon/sarvam_dictation`` and is not
installed in CI (the suite runs with plain ``pytest``), so this module puts
``<repo_root>/daemon`` at the front of ``sys.path`` before anything imports it.

It also provides the pieces every test module needs:

* ``config`` / ``make_config`` - a ``Config`` built from the brief's defaults.
* ``write_wav`` / ``make_wav`` - builders for real, tiny WAV files written with
  the stdlib ``wave`` module, so no audio hardware and no external tool is
  needed.
* ``FakeRecorder`` / ``FakeTranscriber`` and their factories - injectable
  stand-ins so the daemon never spawns a capture process and never imports the
  ``sarvamai`` SDK.
* an autouse ``isolated_env`` fixture that points ``HOME``,
  ``XDG_CONFIG_HOME`` and ``XDG_RUNTIME_DIR`` at a temporary directory and
  clears ``SARVAM_API_KEY``, so no test can touch the real user environment.

Only the standard library is imported at module import time: importing this
file must work even before the daemon package exists.
"""

from __future__ import annotations

import os
import struct
import sys
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_DIR = REPO_ROOT / "daemon"

# The suite must run against a plain checkout, without `pip install -e`.
if str(DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(DAEMON_DIR))


# --------------------------------------------------------------------------
# environment isolation
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_env(tmp_path_factory, monkeypatch):
    """Point every XDG/HOME lookup at a scratch directory for each test."""
    sandbox = tmp_path_factory.mktemp("env")
    home = sandbox / "home"
    config_home = sandbox / "config"
    runtime_dir = sandbox / "run"
    for directory in (home, config_home, runtime_dir):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    return {
        "root": sandbox,
        "home": home,
        "config_home": config_home,
        "runtime_dir": runtime_dir,
    }


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@pytest.fixture
def make_config():
    """Return a factory building a ``Config`` with overridable fields."""

    def _make(**overrides):
        from sarvam_dictation.config import Config

        values = {
            "api_key": "test-api-key-not-real",
            "model": "saaras:v4",
            "mode": "transcribe",
            "language_code": "",
            "keyterms": (),
            "audio_device": "",
            "sample_rate": 16000,
            "backend": "auto",
            "socket_path": "",
            "max_duration_s": 300,
        }
        values.update(overrides)
        return Config(**values)

    return _make


@pytest.fixture
def config(make_config):
    """A default-ish ``Config`` with a placeholder API key."""
    return make_config()


# --------------------------------------------------------------------------
# WAV building
# --------------------------------------------------------------------------


def build_wav_bytes(
    duration_s: float = 1.0,
    sample_rate: int = 16000,
    channels: int = 1,
    sampwidth: int = 2,
    amplitude: int = 0,
) -> bytes:
    """Return the bytes of a silent (or constant-amplitude) PCM WAV file."""
    frames = int(round(duration_s * sample_rate))
    if sampwidth == 2:
        one_frame = struct.pack("<h", int(amplitude)) * channels
    else:
        one_frame = b"\x00" * sampwidth * channels
    payload = one_frame * frames

    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sampwidth)
        handle.setframerate(sample_rate)
        handle.writeframes(payload)
    return buffer.getvalue()


def write_wav_file(
    path,
    duration_s: float = 1.0,
    sample_rate: int = 16000,
    channels: int = 1,
    sampwidth: int = 2,
    amplitude: int = 0,
):
    """Write a real PCM WAV file at ``path`` and return the ``Path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        build_wav_bytes(
            duration_s=duration_s,
            sample_rate=sample_rate,
            channels=channels,
            sampwidth=sampwidth,
            amplitude=amplitude,
        )
    )
    return path


@pytest.fixture
def write_wav():
    """Fixture form of :func:`write_wav_file`."""
    return write_wav_file


@pytest.fixture
def silence_wav(tmp_path):
    """A 1.0 s, 16 kHz, mono, 16-bit WAV file inside ``tmp_path``."""
    return write_wav_file(tmp_path / "silence.wav", duration_s=1.0)


# --------------------------------------------------------------------------
# fake recorder
# --------------------------------------------------------------------------


class FakeRecorder:
    """A ``Recorder``-like object that never touches audio hardware.

    Mirrors the §6 contract: ``start()``, ``stop() -> Recording``,
    ``cancel()``, ``is_recording``. Every call is counted so tests can assert
    on the daemon's behaviour rather than on the fake.
    """

    def __init__(self, duration_ms: int = 1234, wav_path: Path | None = None):
        self.duration_ms = duration_ms
        self.wav_path = Path(wav_path) if wav_path is not None else Path("/nonexistent/fake.wav")
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.cancel_calls = 0

    @property
    def is_recording(self) -> bool:
        return self.started

    def start(self) -> None:
        self.start_calls += 1
        self.started = True

    def stop(self):
        from sarvam_dictation.recorder import Recording

        self.stop_calls += 1
        self.started = False
        return Recording(path=self.wav_path, duration_ms=self.duration_ms)

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.started = False


class FakeTranscriber:
    """A ``SarvamTranscriber``-like object returning canned transcripts."""

    def __init__(
        self,
        text: str = "नमस्ते",
        language_code: str | None = "hi-IN",
        segments: int = 1,
    ):
        self.text = text
        self.language_code = language_code
        self.segments = segments
        self.calls = []

    def transcribe(self, path):
        from sarvam_dictation.transcriber import Transcript

        self.calls.append(path)
        return Transcript(text=self.text, language_code=self.language_code, segments=self.segments)


class RecordingFactory:
    """Callable recorder factory that exposes the instance it produced."""

    def __init__(self, recorder: FakeRecorder):
        self.recorder = recorder
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.recorder


class TranscriberFactory:
    """Callable transcriber factory that exposes the instance it produced."""

    def __init__(self, transcriber: FakeTranscriber):
        self.transcriber = transcriber
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.transcriber


@pytest.fixture
def fake_recorder():
    return FakeRecorder()


@pytest.fixture
def fake_transcriber():
    return FakeTranscriber()


@pytest.fixture
def recorder_factory(fake_recorder):
    return RecordingFactory(fake_recorder)


@pytest.fixture
def transcriber_factory(fake_transcriber):
    return TranscriberFactory(fake_transcriber)


# --------------------------------------------------------------------------
# misc helpers shared by several modules
# --------------------------------------------------------------------------


@pytest.fixture
def uid():
    """The real uid the §4 socket-path fallback is defined in terms of."""
    return os.getuid()

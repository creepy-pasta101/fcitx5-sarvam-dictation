"""Recorder tests: brief §6 ``recorder.py``, with no audio hardware involved.

``shutil.which`` is faked to control backend detection and ``subprocess.Popen``
is replaced by :class:`FakeProcess`, which writes a real (tiny) WAV file so the
duration arithmetic and the header-finalising shutdown ladder are exercised for
real. Every path used is inside ``tmp_path``.

The streaming tail tests at the end of this module are different: they spawn a
**real child process** with ``sys.executable -c`` and read the bytes it writes
to its stdout. A fake pipe would not have caught the bug they guard - the bytes
buffered in a real pipe are what ``StreamingRecorder.stop()`` used to swallow.
"""

from __future__ import annotations

import asyncio
import base64
import io
import shutil
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

import sarvam_dictation.recorder as recorder_mod
from sarvam_dictation.recorder import (
    Recorder,
    RecorderError,
    Recording,
    StreamingRecorder,
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


# --------------------------------------------------------------------------
# StreamingRecorder: raw PCM on stdout
# --------------------------------------------------------------------------


class FakeStreamStdout:
    """A pipe reader that hands out scripted chunks, then EOF (``b""``)."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False
        self.reads = 0

    async def read(self, size=-1):
        self.reads += 1
        if self.closed:
            raise ValueError("I/O operation on closed pipe")
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class FakeStreamProcess:
    """Stand-in for ``asyncio.subprocess.Process`` writing raw PCM to stdout."""

    last_instance: "FakeStreamProcess | None" = None

    def __init__(self, chunks=(), ignore_signals=frozenset()):
        self.stdout = FakeStreamStdout(chunks)
        self.pid = 5151
        self.returncode = None
        self.signals = []
        self.wait_calls = []
        self.argv = []
        self.kwargs = {}
        self._ignore = set(ignore_signals)
        self._done = asyncio.Event()
        FakeStreamProcess.last_instance = self

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGKILL or sig not in self._ignore:
            # Capture tools exit 0 on SIGINT; harsher signals leave a negative code.
            self.returncode = 0 if sig == signal.SIGINT else -int(sig)
            self._done.set()

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def poll(self):
        return self.returncode

    async def wait(self):
        self.wait_calls.append(None)
        await self._done.wait()
        return self.returncode


@pytest.fixture
def stream_popen(monkeypatch):
    """Patch ``asyncio.create_subprocess_exec`` with a fake capture tool."""

    def _install(*, chunks=(b"\x00\x01" * 1600,), ignore_signals=frozenset()):
        async def fake_exec(*args, **kwargs):
            process = FakeStreamProcess(chunks=chunks, ignore_signals=ignore_signals)
            process.argv = list(args)
            process.kwargs = kwargs
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return fake_exec

    return _install


@pytest.fixture
def fast_ladder(monkeypatch):
    """Make the shutdown ladder's grace periods instantaneous."""
    monkeypatch.setattr(recorder_mod, "SIGINT_GRACE_S", 0.05)
    monkeypatch.setattr(recorder_mod, "SIGTERM_GRACE_S", 0.05)
    monkeypatch.setattr(recorder_mod, "SIGKILL_GRACE_S", 0.05)


async def collect(recorder):
    """Drain ``chunks()`` into a list."""
    return [chunk async for chunk in recorder.chunks()]


# -- argv ------------------------------------------------------------------


def test_stream_command_writes_raw_pcm_to_stdout(config):
    argv = recorder_mod._stream_command("pipewire", config)
    assert argv[0] == "pw-record"
    assert argv[argv.index("--format") + 1] == "s16"
    assert argv[argv.index("--channels") + 1] == "1"
    assert argv[argv.index("--rate") + 1] == "16000"
    assert argv[-1] == "-"
    assert not any(arg.endswith(".wav") for arg in argv)


def test_stream_command_for_pulse_is_raw_and_unredirected(config):
    argv = recorder_mod._stream_command("pulse", config)
    assert argv[0] == "parec"
    assert "--format=s16le" in argv
    assert "--rate=16000" in argv
    assert "--channels=1" in argv
    assert "--raw" in argv
    assert not any(arg.endswith(".wav") for arg in argv)
    assert not any(arg.startswith("--file-format") for arg in argv)


def test_stream_command_for_alsa_is_raw_on_stdout(config):
    argv = recorder_mod._stream_command("alsa", config)
    assert argv[0] == "arecord"
    assert argv[argv.index("-f") + 1] == "S16_LE"
    assert argv[argv.index("-t") + 1] == "raw"
    assert argv[-1] == "-"


def test_stream_command_honours_the_configured_device(make_config):
    config = make_config(audio_device="alsa_input.usb")
    assert "--target" in recorder_mod._stream_command("pipewire", config)
    assert "--device=alsa_input.usb" in recorder_mod._stream_command("pulse", config)
    alsa = recorder_mod._stream_command("alsa", config)
    assert alsa[alsa.index("-D") + 1] == "alsa_input.usb"


def test_stream_command_rejects_an_unknown_backend(config):
    with pytest.raises(RecorderError) as excinfo:
        recorder_mod._stream_command("oss", config)
    assert excinfo.value.code == "no_audio_device"


# -- lifecycle -------------------------------------------------------------


def test_streaming_start_then_stop_returns_a_plausible_duration(
    config, all_binaries, stream_popen, fast_ladder
):
    # 16000 bytes of s16le mono at 16 kHz is 0.5 s of audio.
    stream_popen(chunks=(b"\x00\x00" * 8000,))

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        captured = await collect(recorder)
        duration = await recorder.stop()
        return captured, duration

    captured, duration = asyncio.run(scenario())
    assert sum(len(chunk) for chunk in captured) == 16000
    assert abs(duration - 500) <= 1


def test_streaming_chunks_end_when_the_child_exits(config, all_binaries, stream_popen, fast_ladder):
    stream_popen(chunks=(b"\x00\x00" * 100, b"\x00\x00" * 50))

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        return await collect(recorder)

    captured = asyncio.run(scenario())
    assert len(captured) == 2
    assert [len(chunk) for chunk in captured] == [200, 100]


def test_streaming_chunks_stop_when_the_pipe_closes(config, all_binaries, stream_popen, fast_ladder):
    stream_popen(chunks=(b"\x00\x00" * 100,))

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        FakeStreamProcess.last_instance.stdout.closed = True
        return await collect(recorder)

    assert asyncio.run(scenario()) == []


def test_streaming_chunks_before_start_is_empty(config):
    async def scenario():
        recorder = StreamingRecorder(config)
        return await collect(recorder)

    assert asyncio.run(scenario()) == []


def test_streaming_stop_without_start_is_safe(config):
    async def scenario():
        recorder = StreamingRecorder(config)
        return await recorder.stop()

    assert asyncio.run(scenario()) == 0


def test_streaming_start_without_a_tool_raises_no_audio_device(config, which, fast_ladder):
    which(set())

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()

    with pytest.raises(RecorderError) as excinfo:
        asyncio.run(scenario())
    assert excinfo.value.code == "no_audio_device"


def test_streaming_start_maps_a_spawn_failure_to_no_audio_device(
    config, all_binaries, monkeypatch, fast_ladder
):
    async def boom(*args, **kwargs):
        raise OSError("no such file or directory")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()

    with pytest.raises(RecorderError) as excinfo:
        asyncio.run(scenario())
    assert excinfo.value.code == "no_audio_device"


def test_streaming_start_twice_raises_audio_failed(config, all_binaries, stream_popen, fast_ladder):
    stream_popen()

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        try:
            await recorder.start()
        finally:
            await recorder.cancel()

    with pytest.raises(RecorderError) as excinfo:
        asyncio.run(scenario())
    assert excinfo.value.code == "audio_failed"


def test_streaming_passes_stdout_pipe_and_devnull_stdin(
    config, all_binaries, stream_popen, fast_ladder
):
    stream_popen()

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        await recorder.cancel()

    asyncio.run(scenario())
    process = FakeStreamProcess.last_instance
    assert process.kwargs["stdout"] == asyncio.subprocess.PIPE
    assert process.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert process.argv[0] == "pw-record"


def test_streaming_stop_does_not_count_audio_it_did_not_read(
    config, all_binaries, stream_popen, fast_ladder
):
    """``stop()`` must leave the buffered tail to the reader, not swallow it."""
    stream_popen(chunks=(b"\x00\x00" * 4000, b"\x00\x00" * 4000))

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        first = await recorder.chunks().__anext__()
        duration = await recorder.stop()
        return len(first), duration

    first_len, duration = asyncio.run(scenario())
    assert first_len == 8000
    # Only the 8000 bytes the reader actually took are counted: 250 ms. The
    # second chunk is still in the pipe, waiting for the drain task in
    # server.py, and the duration is read again after that task has run.
    assert abs(duration - 250) <= 1


# -- shutdown ladder -------------------------------------------------------


def test_streaming_stop_sends_sigint_first_and_not_sigkill(
    config, all_binaries, stream_popen, fast_ladder
):
    stream_popen()

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        await recorder.stop()

    asyncio.run(scenario())
    process = FakeStreamProcess.last_instance
    assert process.signals, "stop() must signal the capture process"
    assert process.signals[0] == signal.SIGINT
    assert signal.SIGKILL not in process.signals


def test_streaming_stop_escalates_sigint_then_sigterm_then_sigkill(
    config, all_binaries, stream_popen, fast_ladder
):
    stream_popen(ignore_signals=frozenset({signal.SIGINT, signal.SIGTERM}))

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        started = time.monotonic()
        await recorder.stop()
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    process = FakeStreamProcess.last_instance
    assert process.signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    # Each escalation must have waited out its grace period first (2 x 0.05 s).
    assert elapsed >= 0.09, "the ladder must wait between escalating signals"


def test_streaming_cancel_is_idempotent_and_discards_the_audio(
    config, all_binaries, stream_popen, fast_ladder
):
    stream_popen(chunks=(b"\x00\x00" * 8000,))

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        await collect(recorder)
        await recorder.cancel()
        await recorder.cancel()
        return await recorder.stop()

    assert asyncio.run(scenario()) == 0


def test_streaming_cancel_before_start_is_a_no_op(config):
    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.cancel()
        await recorder.cancel()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# StreamingRecorder against a REAL child process: the tail must survive stop()
# --------------------------------------------------------------------------
#
# The bug these tests guard lives in the pipe, so a fake pipe cannot catch it:
# the bytes a capture tool has already handed to the kernel are the audio the
# user just spoke, and ``stop()`` used to read them out and throw them away.

#: Exactly what the child writes: 24576 bytes of s16le mono at 16 kHz, which
#: is 768 ms of audio, six 4096-byte reads, and small enough to sit entirely in
#: one kernel pipe buffer (64 KiB) so the child never blocks on its write.
STREAM_PATTERN = bytes(range(256)) * 96

#: Child A: write the pattern in two flushes, then exit. Models a capture tool
#: that finishes on its own.
CHILD_WRITES_THEN_EXITS = """
import base64
import sys

data = base64.b64decode(sys.argv[1])
half = len(data) // 2
out = sys.stdout.buffer
out.write(data[:half])
out.flush()
out.write(data[half:])
out.flush()
"""

#: Child B: write the pattern, touch a sentinel so the parent knows every byte
#: is in the pipe, then keep running like a live capture tool. Models the real
#: shape of the bug: the user stops speaking while the reader is mid-stream.
CHILD_WRITES_THEN_WAITS = """
import base64
import pathlib
import sys
import time

data = base64.b64decode(sys.argv[1])
half = len(data) // 2
out = sys.stdout.buffer
out.write(data[:half])
out.flush()
out.write(data[half:])
out.flush()
pathlib.Path(sys.argv[2]).touch()
time.sleep(30)
"""


@pytest.fixture
def real_child(monkeypatch, tmp_path):
    """Point ``StreamingRecorder`` at a real ``sys.executable -c`` child.

    ``_stream_command`` and ``detect_backend`` are the only things patched, so
    the recorder spawns, reads, signals and reaps a genuine OS process through
    the ordinary asyncio subprocess machinery.
    """

    def _install(source, *, wait_for_ready=False):
        argv = [sys.executable, "-c", source, base64.b64encode(STREAM_PATTERN).decode("ascii")]
        ready = None
        if wait_for_ready:
            ready = tmp_path / "child-ready"
            argv.append(str(ready))
        monkeypatch.setattr(
            recorder_mod, "_stream_command", lambda backend, config: list(argv)
        )
        monkeypatch.setattr(
            recorder_mod, "detect_backend", lambda preferred="auto": "pipewire"
        )
        return ready

    return _install


async def wait_for_ready(path, timeout=10.0):
    """Wait until the child has flushed every byte into the pipe."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not path.exists():
        if loop.time() >= deadline:
            raise AssertionError(f"the child never signalled readiness via {path}")
        await asyncio.sleep(0.01)


def test_streaming_real_child_tail_survives_a_stop_after_the_iteration(config, real_child):
    """Collect every chunk, then stop: the bytes must equal the pattern exactly."""
    real_child(CHILD_WRITES_THEN_EXITS)

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        try:
            captured = b"".join(await collect(recorder))
            duration = await recorder.stop()
            return captured, duration
        finally:
            await recorder.cancel()

    captured, duration = asyncio.run(scenario())
    assert len(STREAM_PATTERN) == 24576
    assert captured == STREAM_PATTERN
    assert abs(duration - 768) <= 1


def test_streaming_real_child_tail_survives_a_stop_mid_stream(
    config, real_child, monkeypatch
):
    """Stop before EOF, keep iterating: the buffered tail must still arrive."""
    # The pipe is deliberately still full when stop() runs, so asyncio's
    # Process.wait() cannot resolve; _reap must tolerate that without waiting
    # out the production grace period in a test.
    monkeypatch.setattr(recorder_mod, "STREAM_REAP_TIMEOUT_S", 0.25)
    ready = real_child(CHILD_WRITES_THEN_WAITS, wait_for_ready=True)

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        try:
            await wait_for_ready(ready)
            stream = recorder.chunks()
            first = await stream.__anext__()
            # The user stopped speaking while the reader was mid-stream: the
            # child is still alive and most of the audio is still in the pipe.
            await recorder.stop()
            rest = [chunk async for chunk in stream]
            return first, b"".join(rest), recorder.duration_ms
        finally:
            await recorder.cancel()

    first, rest, duration = asyncio.run(scenario())
    assert 0 < len(first) <= recorder_mod.STREAM_CHUNK_SIZE
    assert len(first) < len(STREAM_PATTERN), "the child must still owe the reader bytes"
    assert first + rest == STREAM_PATTERN
    # The reader counted every byte it delivered, so the duration is final
    # only after it has finished - which is what server.py now reads.
    assert abs(duration - 768) <= 1


def test_streaming_real_child_cancel_still_discards_the_tail(config, real_child):
    """cancel() is the one path that throws the recording away on purpose."""
    ready = real_child(CHILD_WRITES_THEN_WAITS, wait_for_ready=True)

    async def scenario():
        recorder = StreamingRecorder(config)
        await recorder.start()
        await wait_for_ready(ready)
        stream = recorder.chunks()
        first = await stream.__anext__()
        await recorder.cancel()
        await recorder.cancel()
        return first, [chunk async for chunk in stream]

    first, rest = asyncio.run(scenario())
    assert 0 < len(first) <= recorder_mod.STREAM_CHUNK_SIZE
    assert rest == [], "cancel() must drain and discard, never hand the tail on"


def test_streaming_real_child_stop_without_start_is_safe(config, real_child):
    """stop() before start() returns 0 and never spawns anything."""
    real_child(CHILD_WRITES_THEN_EXITS)

    async def scenario():
        recorder = StreamingRecorder(config)
        return await recorder.stop(), await recorder.stop(), recorder.duration_ms

    assert asyncio.run(scenario()) == (0, 0, 0)

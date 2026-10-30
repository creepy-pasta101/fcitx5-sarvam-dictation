"""Audio capture and WAV normalisation.

The daemon shells out to whichever PipeWire/PulseAudio/ALSA recorder is present,
then normalises the result to 16 kHz mono signed 16-bit PCM so the speech API
always sees exactly one format.
"""

from __future__ import annotations

import array
import contextlib
import logging
import shutil
import signal
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .config import Config

logger = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16000
SIGINT_GRACE_S = 3.0
SIGTERM_GRACE_S = 2.0
SIGKILL_GRACE_S = 2.0

#: backend name -> recorder binary, in auto-detection order.
BACKEND_BINARIES = (
    ("pipewire", "pw-record"),
    ("pulse", "parec"),
    ("alsa", "arecord"),
)


class RecorderError(Exception):
    """Raised when capture or normalisation fails.

    ``code`` is one of the protocol error codes the daemon is allowed to emit
    for audio problems: ``no_audio_device``, ``audio_failed`` (or ``internal``).
    """

    def __init__(self, message: str, code: str = "audio_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Recording:
    """A finished capture: a valid 16-bit PCM WAV plus its duration."""

    path: Path
    duration_ms: int
    sample_rate: int = TARGET_SAMPLE_RATE


def detect_backend(preferred: str = "auto") -> str:
    """Pick an audio backend, raising ``no_audio_device`` when none is usable.

    ``preferred`` is one of ``auto``/``pipewire``/``pulse``/``alsa``. With
    ``auto`` the first of ``pw-record``, ``parec``, ``arecord`` on ``PATH`` wins.
    """
    if preferred != "auto":
        for name, binary in BACKEND_BINARIES:
            if name != preferred:
                continue
            if shutil.which(binary):
                return name
            raise RecorderError(
                f"audio backend {preferred!r} requested but {binary} is not installed",
                code="no_audio_device",
            )
        raise RecorderError(f"unknown audio backend {preferred!r}", code="no_audio_device")

    for name, binary in BACKEND_BINARIES:
        if shutil.which(binary):
            return name
    raise RecorderError(
        "no audio recorder found; install pipewire (pw-record), pulseaudio (parec) "
        "or alsa-utils (arecord)",
        code="no_audio_device",
    )


def normalize_wav(src: Path, dst: Path, sample_rate: int = TARGET_SAMPLE_RATE) -> Path:
    """Convert ``src`` to mono 16-bit PCM at ``sample_rate``, writing ``dst``.

    Tries ``ffmpeg``, then ``sox``, then a pure-Python ``wave`` fallback so the
    daemon still works on a machine with neither tool installed.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        raise RecorderError(f"recorded file {src} does not exist", code="audio_failed")
    if src.stat().st_size == 0:
        raise RecorderError("recorder produced no audio data", code="audio_failed")

    dst.parent.mkdir(parents=True, exist_ok=True)

    if shutil.which("ffmpeg"):
        argv = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src),
            "-ac", "1", "-ar", str(sample_rate), "-acodec", "pcm_s16le",
            "-f", "wav", str(dst),
        ]
        if _run_tool(argv):
            return dst
        logger.debug("ffmpeg normalisation failed for %s; trying sox", src)

    if shutil.which("sox"):
        argv = ["sox", str(src), "-c", "1", "-r", str(sample_rate), "-b", "16", str(dst)]
        if _run_tool(argv):
            return dst
        logger.debug("sox normalisation failed for %s; falling back to wave", src)

    return _normalize_with_wave(src, dst, sample_rate)


def wav_duration_ms(path: Path) -> int:
    """Return the duration of a WAV file in whole milliseconds."""
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as reader:
            frames = reader.getnframes()
            rate = reader.getframerate()
    except (wave.Error, EOFError, OSError) as exc:
        raise RecorderError(f"cannot read WAV file {path}: {exc}", code="audio_failed") from exc
    if rate <= 0:
        raise RecorderError(f"WAV file {path} declares a sample rate of {rate}", code="audio_failed")
    return int(round(frames * 1000.0 / rate))


class Recorder:
    """Owns one capture subprocess at a time.

    ``start`` launches the backend recorder writing straight into a temp WAV;
    ``stop`` shuts it down with the graceful ladder (SIGINT -> SIGTERM ->
    SIGKILL) so the recorder finalises the RIFF header, then normalises and
    validates the result.
    """

    def __init__(self, config: Config, workdir: Path | None = None) -> None:
        self._config = config
        self._workdir = Path(workdir) if workdir is not None else None
        self._owns_workdir = workdir is None
        self._proc: subprocess.Popen[bytes] | None = None
        self._raw_path: Path | None = None
        self._log_path: Path | None = None
        self._log_handle = None

    @property
    def is_recording(self) -> bool:
        """True while a capture subprocess is alive."""
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Begin capturing audio. Raises :class:`RecorderError` on failure."""
        if self._proc is not None:
            raise RecorderError("a recording is already in progress", code="audio_failed")

        backend = detect_backend(self._config.backend)
        workdir = self._ensure_workdir()
        token = uuid4().hex
        self._raw_path = workdir / f"raw-{token}.wav"
        self._log_path = workdir / f"raw-{token}.log"
        argv = _record_command(backend, self._raw_path, self._config)

        try:
            self._log_handle = open(self._log_path, "wb")
        except OSError as exc:
            self._raw_path = None
            self._log_path = None
            raise RecorderError(f"cannot create capture log: {exc}", code="audio_failed") from exc

        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self._log_handle,
                start_new_session=True,
            )
        except OSError as exc:
            self._close_log()
            self._discard_files()
            raise RecorderError(
                f"failed to start {argv[0]}: {exc}", code="no_audio_device"
            ) from exc

        logger.debug("recording with %s (pid %s)", argv[0], self._proc.pid)

    def stop(self) -> Recording:
        """Stop capturing, normalise the audio and return the finished recording."""
        proc = self._proc
        raw = self._raw_path
        if proc is None or raw is None:
            raise RecorderError("stop() called while not recording", code="audio_failed")

        self._proc = None
        self._raw_path = None
        workdir = raw.parent
        destination = workdir / f"rec-{uuid4().hex}.wav"

        try:
            self._terminate(proc)
            self._close_log()
            self._raise_for_recorder_failure(proc, raw)
            normalize_wav(raw, destination, self._config.sample_rate)
            duration_ms = wav_duration_ms(destination)
            if duration_ms <= 0:
                raise RecorderError("recording contains no audio", code="audio_failed")
            logger.debug("captured %d ms of audio", duration_ms)
            return Recording(
                path=destination,
                duration_ms=duration_ms,
                sample_rate=self._config.sample_rate,
            )
        except BaseException:
            # Never leave a half-written file behind for the caller to trip over.
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            # _raw_path is already cleared above, so delete the capture explicitly.
            with contextlib.suppress(OSError):
                raw.unlink(missing_ok=True)
            self._discard_files()

    def cancel(self) -> None:
        """Stop capturing and delete the audio. Safe to call at any time."""
        proc = self._proc
        self._proc = None
        # Keep _raw_path set until _discard_files() has run: it is how the
        # cleanup finds the file the child was writing to. Nulling it first
        # orphans the WAV, which then survives in the temp dir.
        if proc is not None:
            try:
                self._terminate(proc)
            except OSError as exc:  # pragma: no cover - signal races only
                logger.debug("error while stopping recorder: %s", exc)
        self._close_log()
        self._discard_files()

    def _ensure_workdir(self) -> Path:
        """Create (once) the directory that holds raw and normalised audio."""
        if self._workdir is None:
            self._workdir = Path(tempfile.mkdtemp(prefix="sarvam-dictation-"))
            self._owns_workdir = True
        else:
            self._workdir.mkdir(parents=True, exist_ok=True)
        return self._workdir

    def _terminate(self, proc: subprocess.Popen[bytes]) -> None:
        """Run the graceful shutdown ladder; SIGKILL is only ever the last resort."""
        if proc.poll() is not None:
            return
        for sig, grace in ((signal.SIGINT, SIGINT_GRACE_S), (signal.SIGTERM, SIGTERM_GRACE_S)):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                return
            except OSError as exc:  # pragma: no cover
                logger.debug("cannot signal recorder: %s", exc)
                break
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                logger.debug("recorder ignored signal %s; escalating", sig.name)
        try:
            proc.kill()
        except ProcessLookupError:
            return
        except OSError as exc:  # pragma: no cover
            logger.debug("cannot kill recorder: %s", exc)
            return
        try:
            proc.wait(timeout=SIGKILL_GRACE_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            logger.warning("recorder pid %s did not die after SIGKILL", proc.pid)

    def _raise_for_recorder_failure(self, proc: subprocess.Popen[bytes], raw: Path) -> None:
        """Turn a non-zero recorder exit with no usable output into RecorderError."""
        if proc.returncode in (0, None):
            return
        produced = raw.exists() and raw.stat().st_size > 44
        if produced:
            logger.debug("recorder exited with status %s but produced audio", proc.returncode)
            return
        detail = self._read_log()
        message = f"recorder exited with status {proc.returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise RecorderError(message, code="audio_failed")

    def _read_log(self) -> str:
        """Return the tail of the recorder's stderr log, if any."""
        if self._log_path is None or not self._log_path.exists():
            return ""
        try:
            text = self._log_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        return text.splitlines()[-1] if text else ""

    def _close_log(self) -> None:
        """Close the stderr log handle if it is open."""
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover
                pass

    def _discard_files(self) -> None:
        """Remove raw audio and log files (and our temp dir if we made it)."""
        workdir = self._workdir
        for path in (self._raw_path, self._log_path):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:  # pragma: no cover
                    pass
        self._raw_path = None
        self._log_path = None
        if workdir is not None:
            try:
                # Sweep any capture file we lost the handle to (a child process
                # may still have created one after we cleared the path), and
                # drop the directory only if we created it and it is now empty.
                for leftover in workdir.iterdir():
                    if leftover.is_file() and leftover.name.startswith("raw-"):
                        leftover.unlink(missing_ok=True)
                if self._owns_workdir and not any(workdir.iterdir()):
                    workdir.rmdir()
            except OSError:  # pragma: no cover - best effort cleanup
                pass


def _record_command(backend: str, destination: Path, config: Config) -> list[str]:
    """Build the recorder argv for ``backend`` writing into ``destination``."""
    rate = str(config.sample_rate)
    device = config.audio_device
    if backend == "pipewire":
        argv = ["pw-record", "--rate", rate, "--channels", "1", "--format", "s16"]
        if device:
            argv += ["--target", device]
        argv.append(str(destination))
        return argv
    if backend == "pulse":
        argv = [
            "parec",
            "--format=s16le",
            f"--rate={rate}",
            "--channels=1",
            "--file-format=wav",
        ]
        if device:
            argv.append(f"--device={device}")
        argv.append(str(destination))
        return argv
    if backend == "alsa":
        argv = ["arecord", "-q", "-f", "S16_LE", "-r", rate, "-c", "1", "-t", "wav"]
        if device:
            argv += ["-D", device]
        argv.append(str(destination))
        return argv
    raise RecorderError(f"unknown audio backend {backend!r}", code="no_audio_device")


def _run_tool(argv: list[str]) -> bool:
    """Run a normalisation tool; True when it exits 0 and wrote a non-empty file."""
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("%s failed: %s", argv[0], exc)
        return False
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        logger.debug("%s exited %d: %s", argv[0], completed.returncode, detail[-1] if detail else "")
        return False
    destination = Path(argv[-1])
    return destination.exists() and destination.stat().st_size > 44


def _normalize_with_wave(src: Path, dst: Path, sample_rate: int) -> Path:
    """Pure-stdlib downmix + resample fallback used when no audio tool exists."""
    try:
        with wave.open(str(src), "rb") as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            raw = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError, OSError) as exc:
        raise RecorderError(f"cannot read recorded audio {src}: {exc}", code="audio_failed") from exc

    if width != 2:
        raise RecorderError(
            f"recorded audio is {width * 8}-bit; only 16-bit PCM can be converted "
            "without ffmpeg or sox",
            code="audio_failed",
        )
    if rate <= 0 or channels <= 0:
        raise RecorderError("recorded audio has an invalid header", code="audio_failed")

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    usable = len(samples) - (len(samples) % channels)
    if usable <= 0:
        raise RecorderError("recording contains no audio", code="audio_failed")

    if channels > 1:
        mono = array.array(
            "h",
            (sum(samples[index:index + channels]) // channels
             for index in range(0, usable, channels)),
        )
    else:
        mono = samples

    resampled = _resample_linear(mono, rate, sample_rate)
    if sys.byteorder == "big":
        resampled.byteswap()

    try:
        with wave.open(str(dst), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.writeframes(resampled.tobytes())
    except OSError as exc:
        raise RecorderError(f"cannot write normalised audio {dst}: {exc}", code="audio_failed") from exc
    return dst


def _resample_linear(samples: array.array, rate: int, target_rate: int) -> array.array:
    """Linear-interpolation resampler (adequate for speech at 16 kHz)."""
    if rate == target_rate or not samples:
        return array.array("h", samples)
    count = len(samples)
    out_length = max(1, int(round(count * target_rate / rate)))
    output = array.array("h", bytes(2 * out_length))
    step = rate / target_rate
    last = count - 1
    for index in range(out_length):
        position = index * step
        left = int(position)
        if left >= last:
            output[index] = samples[last]
            continue
        fraction = position - left
        low = samples[left]
        high = samples[left + 1]
        output[index] = int(low + (high - low) * fraction)
    return output

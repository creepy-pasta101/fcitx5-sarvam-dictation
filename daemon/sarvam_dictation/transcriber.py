"""Speech-to-text via the Sarvam AI SDK.

The SDK is imported lazily inside :meth:`SarvamTranscriber._build_client` so the
daemon (and the test-suite) work on a machine where ``sarvamai`` is not
installed. Any injected client is used as-is, which is how tests avoid the SDK
entirely.
"""

from __future__ import annotations

import errno as _errno
import logging
import shutil
import socket
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_MODEL, REST_MODELS, Config

logger = logging.getLogger(__name__)

#: Chunks must stay under the API's per-request limit; 28 s leaves headroom.
MAX_CHUNK_SECONDS = 28.0

#: Exception class names (httpx/requests style) that mean "transport problem".
_NETWORK_EXCEPTION_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "NetworkError",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "Timeout",
        "TimeoutException",
        "TransportError",
        "WriteError",
        "WriteTimeout",
    }
)


_NETWORK_ERRNOS = frozenset(
    {
        _errno.ECONNABORTED,
        _errno.ECONNREFUSED,
        _errno.ECONNRESET,
        _errno.EHOSTUNREACH,
        _errno.ENETDOWN,
        _errno.ENETUNREACH,
        _errno.EPIPE,
        _errno.ETIMEDOUT,
    }
)


class TranscriptionError(Exception):
    """Raised when transcription fails.

    ``code`` is one of ``no_api_key``, ``config_invalid``, ``network``, ``api``
    or ``internal``. ``config_invalid`` is a configuration mistake, not a
    service failure: the model the user picked cannot be sent to the REST
    endpoint at all, so the daemon says so instead of forwarding the request
    and relaying the provider's HTTP 400.
    """

    def __init__(self, message: str, code: str = "internal") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Transcript:
    """A finished transcript and what the API reported about it."""

    text: str
    language_code: str | None
    segments: int


def split_wav(
    path: Path,
    max_seconds: float = MAX_CHUNK_SECONDS,
    outdir: Path | None = None,
) -> list[Path]:
    """Split ``path`` into consecutive WAV chunks of at most ``max_seconds``.

    Returns ``[path]`` untouched when the file already fits, otherwise the
    ordered chunk paths written into ``outdir`` (a fresh temp dir when omitted).
    Chunk boundaries fall on frame boundaries so no sample is dropped.
    """
    path = Path(path)
    if max_seconds <= 0:
        raise TranscriptionError("max_seconds must be positive", code="internal")

    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            total_frames = reader.getnframes()
            if rate <= 0:
                raise TranscriptionError("audio has an invalid sample rate", code="internal")
            frames_per_chunk = max(1, int(max_seconds * rate))
            if total_frames <= frames_per_chunk:
                return [path]

            directory = Path(outdir) if outdir is not None else Path(
                tempfile.mkdtemp(prefix="sarvam-chunks-")
            )
            directory.mkdir(parents=True, exist_ok=True)

            chunks: list[Path] = []
            index = 0
            while True:
                frames = reader.readframes(frames_per_chunk)
                if not frames:
                    break
                chunk_path = directory / f"chunk-{index:03d}.wav"
                try:
                    with wave.open(str(chunk_path), "wb") as writer:
                        writer.setnchannels(channels)
                        writer.setsampwidth(width)
                        writer.setframerate(rate)
                        writer.writeframes(frames)
                except OSError as exc:
                    raise TranscriptionError(
                        f"cannot write audio chunk {chunk_path}: {exc}", code="internal"
                    ) from exc
                chunks.append(chunk_path)
                index += 1
    except (wave.Error, EOFError) as exc:
        raise TranscriptionError(f"cannot read audio {path}: {exc}", code="internal") from exc
    except OSError as exc:
        raise TranscriptionError(f"cannot open audio {path}: {exc}", code="internal") from exc

    if not chunks:  # pragma: no cover - empty file
        raise TranscriptionError(f"audio {path} contains no frames", code="internal")
    return chunks


def join_transcripts(parts: list[str]) -> str:
    """Join chunk transcripts: strip each, drop empties, single-space join."""
    pieces: list[str] = []
    for part in parts:
        if part is None:
            continue
        stripped = part.strip()
        if stripped:
            pieces.append(stripped)
    return " ".join(pieces)


class SarvamTranscriber:
    """Transcribes a WAV file with Sarvam's Saaras model.

    ``client`` may be injected (tests pass a fake); when it is, the SDK is never
    imported and no network call is ever made by this module's import path.
    """

    def __init__(self, config: Config, client=None) -> None:
        self._config = config
        self._client = client

    def transcribe(self, path: Path) -> Transcript:
        """Transcribe ``path``, splitting it into <=28 s chunks when needed.

        The configured model is checked against the REST namespace first
        (brief 8): a model the endpoint does not serve is a configuration
        error, reported before any file is opened and before any client is
        built, so the provider's ``Invalid model`` 400 is never seen.
        """
        if self._config.model not in REST_MODELS:
            raise TranscriptionError(
                f"model {self._config.model!r} only works on the realtime streaming "
                f"endpoint, not on REST. Set [api] streaming = true to use it, or "
                f"choose {DEFAULT_MODEL!r} for the REST endpoint.",
                code="config_invalid",
            )

        if not self._config.api_key:
            # Raised before any network work: the caller reports `no_api_key`.
            raise TranscriptionError(
                "no Sarvam API key configured; set api.key in "
                "~/.config/sarvam-dictation/config.toml or export SARVAM_API_KEY",
                code="no_api_key",
            )

        path = Path(path)
        if not path.exists():
            raise TranscriptionError(f"audio file {path} does not exist", code="internal")

        created_dir: Path | None = None
        chunks = split_wav(path)
        try:
            if len(chunks) > 1:
                created_dir = chunks[0].parent
            client = self._build_client()
            texts: list[str] = []
            language_code: str | None = None
            for chunk in chunks:
                text, chunk_language = self._transcribe_one(client, chunk)
                texts.append(text)
                if language_code is None and chunk_language:
                    language_code = chunk_language
            return Transcript(
                text=join_transcripts(texts),
                language_code=language_code,
                segments=len(chunks),
            )
        finally:
            if created_dir is not None:
                shutil.rmtree(created_dir, ignore_errors=True)

    def _build_client(self):
        """Return the injected client, or construct one with the lazily imported SDK."""
        if self._client is not None:
            return self._client
        try:
            from sarvamai import SarvamAI  # imported here so the SDK stays optional
        except ModuleNotFoundError as exc:
            raise TranscriptionError(
                "the sarvamai SDK is not installed (pip install 'sarvamai>=0.1.33')",
                code="internal",
            ) from exc
        try:
            return SarvamAI(api_subscription_key=self._config.api_key)
        except Exception as exc:  # SDK raises its own types on bad credentials
            raise TranscriptionError(
                f"cannot create the Sarvam client: {self._scrub(str(exc))}", code="api"
            ) from exc

    def _transcribe_one(self, client, chunk: Path) -> tuple[str, str | None]:
        """Transcribe a single chunk, mapping SDK failures onto our error codes."""
        kwargs: dict[str, object] = {
            "model": self._config.model,
            "mode": self._config.mode,
        }
        if self._config.language_code:
            kwargs["language_code"] = self._config.language_code
        if self._config.keyterms and self._config.model == "saaras:v4":
            kwargs["keyterms"] = list(self._config.keyterms)

        try:
            handle = open(chunk, "rb")
        except OSError as exc:
            raise TranscriptionError(
                f"cannot read audio chunk {chunk}: {exc}", code="internal"
            ) from exc
        with handle:
            try:
                response = client.speech_to_text.transcribe(file=handle, **kwargs)
            except Exception as exc:  # SDK/transport errors are mapped, never leaked
                raise self._map_exception(exc) from exc

        text = getattr(response, "transcript", None)
        if text is None:
            text = getattr(response, "text", "")
        if not isinstance(text, str):
            text = str(text)
        language_code = getattr(response, "language_code", None)
        if not isinstance(language_code, str) or not language_code:
            language_code = None
        return text, language_code

    def _map_exception(self, exc: Exception) -> TranscriptionError:
        """Convert an SDK/transport exception into a TranscriptionError."""
        message = self._scrub(str(exc)) or exc.__class__.__name__
        status = _status_code(exc)
        if status is not None:
            if status in (401, 403):
                return TranscriptionError(
                    f"Sarvam rejected the API key (HTTP {status}): {message}", code="api"
                )
            if status == 429:
                return TranscriptionError(
                    f"Sarvam rate limit reached (HTTP {status}): {message}", code="api"
                )
            return TranscriptionError(f"Sarvam API error (HTTP {status}): {message}", code="api")
        if _is_network_error(exc):
            return TranscriptionError(f"network error talking to Sarvam: {message}", code="network")
        return TranscriptionError(
            f"unexpected error from the Sarvam SDK ({exc.__class__.__name__}): {message}",
            code="internal",
        )

    def _scrub(self, text: str) -> str:
        """Remove the API key from any message before it can be logged or sent."""
        key = self._config.api_key
        if key and len(key) >= 4:
            return text.replace(key, "***")
        return text


def _status_code(exc: Exception) -> int | None:
    """Best-effort extraction of an HTTP status from an SDK exception."""
    for attribute in ("status_code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _is_network_error(exc: Exception) -> bool:
    """True when the exception looks like a transport failure rather than an API error."""
    if isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror)):
        return True
    for klass in type(exc).__mro__:
        if klass.__name__ in _NETWORK_EXCEPTION_NAMES:
            return True
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in _NETWORK_ERRNOS

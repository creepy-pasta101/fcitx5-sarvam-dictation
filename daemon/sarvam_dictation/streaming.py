"""Realtime (streaming) speech-to-text for the dictation daemon.

The file-based path in :mod:`sarvam_dictation.transcriber` records a whole WAV
and posts it to Sarvam's REST endpoint once the user stops speaking. This module
is the other half of the streaming path: it opens a WebSocket to Sarvam's
realtime endpoint while the user is still talking, pushes raw PCM into it as it
arrives, and reports the cumulative partial transcripts so the addon can show
words as they are spoken.

Two properties shape the implementation:

* The ``sarvamai`` SDK is imported lazily, inside :meth:`RealtimeTranscriber.open`.
  Importing this module, and every test of it, works on a machine where the SDK
  is not installed. When a ``client`` is injected the SDK is never imported at
  all - the injected object supplies ``speech_to_text_realtime_streaming``.
* The session outlives a single call, so the async context manager returned by
  ``connect()`` is entered by hand in :meth:`open` and exited in
  :meth:`finish`/:meth:`cancel`. A background receiver task drains ``recv()``
  for as long as the session is open; :meth:`cancel` always joins it, so a
  cancelled session never leaves a task behind.

Brief 16 fixes what "final" means. The first ``transcript.final`` is not the
answer: the service finalises progressively while the upload drains, so on a
slow link the first final covers only the audio it has processed so far.
:meth:`RealtimeTranscriber.finish` therefore waits for the transcript to
settle - the receiver ending, :data:`FINAL_SETTLE_S` of quiet with at least one
final in hand, or the :data:`FINAL_DEADLINE_S` cap, which is logged as a
warning because the tail may then be missing.

Brief 8 adds the model guard: the realtime endpoint serves its own namespace
(:data:`~sarvam_dictation.config.STREAMING_MODELS`), so :meth:`open` refuses a
model outside it with ``config_invalid`` before any socket work. Nothing here
substitutes a different model - the configured one is sent verbatim or the
session does not start.

Every SDK call made here is checked against ``sarvamai`` 0.1.41:
``speech_to_text_realtime_streaming.connect()`` is an ``@asynccontextmanager``
taking ``language_code`` (required), ``model``, ``mode``, ``encoding``,
``sample_rate``, ``endpointing``, ``keyterms``; the socket it yields has
``send_realtime_audio_input``, ``send_realtime_speech_start``,
``send_realtime_speech_end``, ``send_realtime_flush``, ``send_realtime_end``,
``send_realtime_ping`` and ``recv()``; ``recv()`` returns the
``Realtime*`` pydantic models (``transcript.partial``, ``transcript.final``,
``error`` and the rest), which are also accepted here as plain dicts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno as _errno
import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import STREAMING_MODELS, Config

logger = logging.getLogger(__name__)

#: Models the realtime endpoint accepts. Imported from :mod:`sarvam_dictation.
#: config` rather than re-typed here, so ``config.py`` stays the one source of
#: truth for the endpoint namespaces (brief 8). The tuple is the mirror image of
#: ``REST_MODELS``: a REST-only model such as ``saaras:v3`` must never reach the
#: realtime socket, and :meth:`RealtimeTranscriber.open` refuses it rather than
#: substituting a model the user did not choose.
REALTIME_MODELS = STREAMING_MODELS

#: ``linear16`` is raw little-endian signed 16-bit PCM, mono, at ``sample_rate``.
#: That is exactly what the capture tools write to their stdout, so the
#: streaming path needs no WAV header and no normalisation step.
DEFAULT_ENCODING = "linear16"

#: We delimit utterances ourselves from the hotkey, so the server must not guess.
DEFAULT_ENDPOINTING = "manual"

#: ``connect()`` requires a language code; ``auto`` asks the server to detect it.
DEFAULT_LANGUAGE_CODE = "auto"

#: The quiet period that settles a transcript: once this long passes with no
#: new ``transcript.final``, the server has said everything it is going to say
#: about the audio it has processed, and the result is accepted. Every new
#: final restarts this window, so a server still working through an upload
#: backlog keeps the wait open for exactly as long as it needs.
FINAL_SETTLE_S = 2.5

#: The hard cap on the whole settle wait. Reaching it means the tail may be
#: missing, so :meth:`RealtimeTranscriber.finish` logs a warning when it is hit
#: rather than returning a silently truncated transcript.
FINAL_DEADLINE_S = 30.0

#: Exception class names (httpx/websockets/requests style) meaning "transport".
_NETWORK_EXCEPTION_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ConnectionClosed",
        "ConnectionClosedError",
        "ConnectionClosedOK",
        "ConnectionError",
        "InvalidHandshake",
        "InvalidStatus",
        "InvalidStatusCode",
        "InvalidURI",
        "NetworkError",
        "ReadError",
        "ReadTimeout",
        "Timeout",
        "TimeoutException",
        "TimeoutError",
        "TransportError",
        "WebSocketException",
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


class StreamingError(Exception):
    """A streaming transcription failure.

    ``code`` is one of ``no_api_key``, ``config_invalid``, ``network``, ``api``
    or ``internal`` - the same vocabulary the protocol's ``error`` message uses.
    ``config_invalid`` means the settings cannot be used with the realtime
    endpoint at all (a REST-only model, for example), so the session is refused
    before a socket is opened.
    """

    def __init__(self, message: str, code: str = "internal") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Transcript:
    """The finished transcript of one streaming recording."""

    text: str
    language_code: str | None
    segments: int


class _Event:
    """Stand-in for the SDK's request models when the SDK is not in play.

    The real socket client only ever calls ``data.dict()`` and hands the result
    to ``json.dumps`` (``AsyncSpeechToTextRealtimeStreamingSocketClient._send_model``),
    so any object with a ``dict()`` method is wire-compatible. Injecting a fake
    client therefore never needs the SDK to be importable.
    """

    __slots__ = ("event", "audio")

    def __init__(self, event: str, audio: str | None = None) -> None:
        self.event = event
        self.audio = audio

    def dict(self) -> dict[str, Any]:
        """Return the JSON body this event would be sent as."""
        payload: dict[str, Any] = {"event": self.event}
        if self.audio is not None:
            payload["audio"] = self.audio
        return payload


class RealtimeTranscriber:
    """Transcribes raw PCM while it is being captured, through Sarvam's realtime
    WebSocket API. One instance per recording.

    ``on_partial`` is called with the latest cumulative partial text (already
    stripped, never empty) from the receiver task; the caller decides how to
    deliver it. ``client`` may be injected (tests pass a fake); when it is, the
    ``sarvamai`` SDK is never imported and no network call is ever made.
    """

    def __init__(
        self,
        config: Config,
        on_partial: Callable[[str], None] | None = None,
        client=None,
    ) -> None:
        self._config = config
        self._on_partial = on_partial
        self._client = client
        #: request-model factories, filled in only when the SDK itself is used.
        self._models: dict[str, Any] = {}
        self._ctx: Any = None
        self._ws: Any = None
        self._receiver: asyncio.Task[None] | None = None
        self._final_event = asyncio.Event()
        #: utterance_idx -> final text, in arrival order (later re-emits win).
        self._finals: dict[int, str] = {}
        #: Loop time of the most recent final, or ``None`` until one arrives.
        self._last_final_at: float | None = None
        self._language: str | None = None
        self._last_partial = ""
        self._error: StreamingError | None = None
        self._fatal = False
        self._finished = False
        self._closed = False

    # -- lifecycle --------------------------------------------------------

    async def open(self) -> None:
        """Open the session.

        Raises :class:`StreamingError` (``config_invalid``) before any client is
        constructed when the configured model is not one the realtime endpoint
        serves, and (``no_api_key``) when ``config.api_key`` is empty: either
        way a misconfigured daemon never opens a socket.
        """
        if self._ws is not None or self._finished or self._closed:
            return

        if self._config.model not in STREAMING_MODELS:
            # The mirror of the guard in transcriber.py: the realtime endpoint
            # has its own model namespace, so a REST-only model is a
            # configuration mistake rather than something to paper over by
            # substituting a model the user never picked.
            raise StreamingError(
                f"model {self._config.model!r} cannot be used with streaming. "
                f"Choose {STREAMING_MODELS[0]!r} or {STREAMING_MODELS[1]!r} for the "
                f"realtime endpoint, or set [api] streaming = false to use REST.",
                code="config_invalid",
            )

        if not (self._config.api_key or "").strip():
            # Raised before any network work: the caller reports `no_api_key`.
            raise StreamingError(
                "no Sarvam API key configured; set api.key in "
                "~/.config/sarvam-dictation/config.toml or export SARVAM_API_KEY",
                code="no_api_key",
            )

        client = self._resolve_client()
        service = getattr(client, "speech_to_text_realtime_streaming", None)
        connect = getattr(service, "connect", None)
        if connect is None:
            raise StreamingError(
                "the Sarvam client has no speech_to_text_realtime_streaming.connect()",
                code="internal",
            )

        kwargs = self._connect_kwargs()
        logger.debug(
            "opening realtime session (model=%s, mode=%s, sample_rate=%s)",
            kwargs.get("model"),
            kwargs.get("mode"),
            kwargs.get("sample_rate"),
        )
        try:
            ctx = connect(**kwargs)
            ws = await ctx.__aenter__()
        except Exception as exc:  # connect/auth/transport failures are mapped
            raise self._map_exception(exc) from exc

        self._ctx = ctx
        self._ws = ws
        self._final_event = asyncio.Event()
        self._receiver = asyncio.ensure_future(self._receive())

        try:
            # endpointing=manual: the utterance starts when capture starts.
            await ws.send_realtime_speech_start(self._event("speech_start"))
        except Exception as exc:
            await self.cancel()
            raise self._map_exception(exc) from exc

    async def feed(self, pcm: bytes) -> None:
        """Send one chunk of raw s16le mono PCM at ``config.sample_rate``.

        A no-op before :meth:`open` and after :meth:`finish`: the recorder can
        outlive a session that failed to open, and draining its chunks must not
        turn into an exception storm in the daemon.
        """
        ws = self._ws
        if ws is None or not pcm or self._finished or self._closed:
            return
        audio = base64.b64encode(pcm).decode("ascii")
        try:
            await ws.send_realtime_audio_input(self._event("audio_input", audio=audio))
        except Exception as exc:
            # A dead socket during capture is reported by finish()/cancel();
            # dropping a chunk keeps the daemon's drain task alive meanwhile.
            logger.debug("dropping audio chunk: %s", self._scrub(str(exc)))

    async def finish(self) -> Transcript:
        """Send speech_end + flush, wait for the transcript to settle, close
        the session and return it.

        The wait is :meth:`_await_final_settled`, not "the first final": the
        service finalises progressively as the upload drains, so the transcript
        is accepted once it has gone quiet, once the socket closes, or when the
        hard deadline passes - and that last case is logged as a warning
        because the tail may be missing.

        Never raises for a missing final: an empty text with ``segments=0`` is a
        legal result. A fatal ``RealtimeError`` from the server is raised as
        :class:`StreamingError` once the socket has been closed.
        """
        if self._finished:
            return self._transcript()
        self._finished = True

        ws = self._ws
        if ws is not None:
            for name in ("speech_end", "flush"):
                try:
                    await getattr(ws, f"send_realtime_{name}")(self._event(name))
                except Exception as exc:
                    logger.debug("cannot send %s: %s", name, self._scrub(str(exc)))
                    if self._error is None:
                        self._error = self._map_exception(exc)
                    break

        settled = True
        try:
            settled = await self._await_final_settled()
        finally:
            if not settled:
                # Visible rather than silent: the tail of the recording may be
                # missing because the deadline, not the server, ended the wait.
                logger.warning(
                    "no transcript.final arrived within the %.1fs deadline "
                    "(quiet window %.1fs); returning %d final segment(s) and "
                    "the tail of the recording may be missing",
                    FINAL_DEADLINE_S,
                    FINAL_SETTLE_S,
                    len(self._finals),
                )
            await self._close(send_end=True)

        if self._error is not None:
            raise self._error
        return self._transcript()

    async def cancel(self) -> None:
        """Drop the session without waiting for a final transcript. Idempotent."""
        self._finished = True
        self._closed = True
        await self._close(send_end=False)

    # -- receiver ---------------------------------------------------------

    async def _receive(self) -> None:
        """Drain ``recv()`` until the socket ends, a fatal error arrives, or
        the session is closed."""
        ws = self._ws
        while ws is not None and not self._closed and not self._fatal:
            try:
                message = await ws.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Normal end of stream (the server closed the socket) or a
                # transport failure: either way there is nothing left to read.
                logger.debug("realtime receive loop finished: %s", self._scrub(str(exc)))
                break
            try:
                self._handle(message)
            except Exception as exc:  # a malformed message must not kill the session
                logger.debug("ignoring unusable realtime message: %s", self._scrub(str(exc)))

    def _handle(self, message: Any) -> None:
        """Apply one message from the server to the transcript state."""
        event = _field(message, "event")
        if event == "transcript.partial":
            self._handle_partial(message)
        elif event == "transcript.final":
            self._handle_final(message)
        elif event == "error":
            self._handle_error(message)
        elif event in ("session.begin", "session.end", "vad.speech_start",
                       "vad.speech_end", "config.updated", "pong"):
            logger.debug("realtime event: %s", event)
        else:
            logger.debug("unhandled realtime event: %r", event)

    def _handle_partial(self, message: Any) -> None:
        """Forward the cumulative partial text; never forward an empty string."""
        text = _as_text(_field(message, "text")).strip()
        if not text:
            return
        self._last_partial = text
        callback = self._on_partial
        if callback is None:
            return
        try:
            callback(text)
        except Exception as exc:  # the caller's sink must not break the receiver
            logger.debug("on_partial callback failed: %s", exc)

    def _handle_final(self, message: Any) -> None:
        """Record one finalised utterance, keyed by ``utterance_idx``."""
        index = _field(message, "utterance_idx")
        if isinstance(index, bool) or not isinstance(index, int):
            index = len(self._finals)
        text = _as_text(_field(message, "text")).strip()
        self._finals[index] = text
        language = _field(message, "language")
        if self._language is None and isinstance(language, str) and language:
            self._language = language
        logger.debug("final utterance %d: %d characters", index, len(text))
        # Recorded next to the event so the settle loop can tell "one final and
        # then quiet" from "no final at all": the first is settled, the second
        # is not, and only the deadline may end the second.
        self._last_final_at = _now()
        self._final_event.set()

    def _handle_error(self, message: Any) -> None:
        """Turn a fatal server error into a :class:`StreamingError`."""
        code = _as_text(_field(message, "code")) or "unknown"
        detail = _as_text(_field(message, "message"))
        fatal = bool(_field(message, "is_fatal"))
        status = _field(message, "status_code")
        described = f"Sarvam realtime error {code}: {detail}".rstrip(": ").strip()
        if isinstance(status, int) and not isinstance(status, bool):
            described = f"{described} (status {status})"
        described = self._scrub(described)
        if not fatal:
            logger.warning("non-fatal realtime error: %s", described)
            return
        logger.warning("fatal realtime error: %s", described)
        if self._error is None:
            self._error = StreamingError(described, code="api")
        self._fatal = True
        # Wake finish() so it stops waiting for a final that will never come.
        self._final_event.set()

    async def _await_final_settled(self) -> bool:
        """Wait until the transcript is settled. False means the deadline was hit.

        The first ``transcript.final`` is not the answer. The service finalises
        progressively while an upload backlog drains, so a slow link delivers a
        final for the audio processed so far and then keeps delivering more.
        The transcript is final when any of these is true, checked in this
        order:

        * the receiver task has ended - the socket closed, so nothing more can
          arrive;
        * no new ``transcript.final`` has arrived for :data:`FINAL_SETTLE_S` -
          the server has gone quiet, so it has said everything it is going to
          say, and there is at least one final to return;
        * the hard :data:`FINAL_DEADLINE_S` cap has passed - the tail may be
          missing, which :meth:`finish` logs as a warning.

        Every new final restarts the quiet timer. "No finals at all" is not the
        same as "settled empty": an empty result is legal, but only the
        deadline establishes it, so a session that never answers costs the full
        deadline rather than being reported empty as soon as it goes quiet.
        """
        receiver = self._receiver
        deadline = _now() + FINAL_DEADLINE_S
        while True:
            if receiver is None or receiver.done() or self._fatal:
                return True
            remaining = deadline - _now()
            if remaining <= 0:
                return False
            # Clear before waiting: a final that arrived earlier (while the user
            # was still speaking, or in the previous iteration) is already
            # accounted for, and the next one is what restarts this window.
            # Nothing can run between the clear and the await below, so no
            # final can be lost here.
            self._final_event.clear()
            waiter = asyncio.ensure_future(self._final_event.wait())
            try:
                await asyncio.wait(
                    {waiter, receiver},
                    timeout=min(FINAL_SETTLE_S, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
            if receiver.done():
                return True
            if self._final_event.is_set():
                # A new final arrived: restart the quiet window.
                continue
            if remaining < FINAL_SETTLE_S:
                # The deadline cut this window short, so the silence seen here
                # is not a full quiet window: the cap ended the wait, not quiet.
                return False
            if self._last_final_at is not None:
                # Quiet, and there is something to return: this is settled.
                return True
            # Nothing at all has been finalised yet. Keep waiting; only the
            # deadline may declare an empty result.

    # -- closing ----------------------------------------------------------

    async def _close(self, send_end: bool) -> None:
        """End the session: optional ``end`` event, join the receiver, exit the
        context manager. Safe to call more than once."""
        self._closed = True
        ws = self._ws
        if ws is not None and send_end:
            try:
                await ws.send_realtime_end(self._event("end"))
            except Exception as exc:
                logger.debug("cannot send end: %s", self._scrub(str(exc)))

        receiver, self._receiver = self._receiver, None
        if receiver is not None:
            if not receiver.done():
                receiver.cancel()
            # gather() never raises for a cancelled or failed receiver, so the
            # context manager is always exited below.
            await asyncio.gather(receiver, return_exceptions=True)

        ctx, self._ctx = self._ctx, None
        self._ws = None
        if ctx is not None:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception as exc:  # closing a broken socket is not an error
                logger.debug("error while closing realtime session: %s", self._scrub(str(exc)))

    # -- helpers ----------------------------------------------------------

    def _transcript(self) -> Transcript:
        """Build the transcript: finals in ``utterance_idx`` order, single-spaced."""
        pieces = [
            self._finals[index].strip()
            for index in sorted(self._finals)
            if self._finals[index].strip()
        ]
        return Transcript(
            text=" ".join(pieces),
            language_code=self._language,
            segments=len(self._finals),
        )

    def _connect_kwargs(self) -> dict[str, Any]:
        """Build the ``connect()`` keyword arguments for this configuration.

        ``open()`` has already refused any model outside
        :data:`~sarvam_dictation.config.STREAMING_MODELS`, so the configured
        model is sent verbatim: this module never swaps in a different one.
        """
        model = self._config.model
        keyterms = None
        if self._config.keyterms and model == "saaras:v4":
            # The SDK wants a JSON-encoded array, and only saaras:v4 accepts them.
            keyterms = json.dumps(list(self._config.keyterms), ensure_ascii=False)
        return {
            "language_code": self._config.language_code or DEFAULT_LANGUAGE_CODE,
            "model": model,
            "mode": self._config.mode,
            "encoding": DEFAULT_ENCODING,
            "sample_rate": str(self._config.sample_rate),
            "endpointing": DEFAULT_ENDPOINTING,
            "keyterms": keyterms,
        }

    def _resolve_client(self):
        """Return the injected client, or build one with the lazily imported SDK."""
        if self._client is not None:
            return self._client
        try:
            import sarvamai  # imported here so the SDK stays optional
        except ModuleNotFoundError as exc:
            raise StreamingError(
                "the sarvamai SDK is not installed (pip install 'sarvamai>=0.1.33')",
                code="internal",
            ) from exc
        self._models = {
            "audio_input": sarvamai.RealtimeAudioInput,
            "speech_start": sarvamai.RealtimeSpeechStart,
            "speech_end": sarvamai.RealtimeSpeechEnd,
            "flush": sarvamai.RealtimeFlush,
            "end": sarvamai.RealtimeEnd,
        }
        try:
            return sarvamai.AsyncSarvamAI(api_subscription_key=self._config.api_key)
        except Exception as exc:  # the SDK raises its own types on bad input
            raise StreamingError(
                f"cannot create the Sarvam client: {self._scrub(str(exc))}", code="api"
            ) from exc

    def _event(self, name: str, **fields: Any) -> Any:
        """Build the request model for ``name`` (SDK model, or a wire-compatible one)."""
        factory = self._models.get(name)
        if factory is not None:
            return factory(**fields)
        return _Event(event=name, **fields)

    def _map_exception(self, exc: Exception) -> StreamingError:
        """Convert an SDK/transport exception into a StreamingError."""
        message = self._scrub(str(exc)) or exc.__class__.__name__
        status = _status_code(exc)
        if status is not None:
            if status in (401, 403):
                return StreamingError(
                    f"Sarvam rejected the API key (HTTP {status}): {message}", code="api"
                )
            if status == 429:
                return StreamingError(
                    f"Sarvam rate limit reached (HTTP {status}): {message}", code="api"
                )
            return StreamingError(f"Sarvam API error (HTTP {status}): {message}", code="api")
        if _is_network_error(exc):
            return StreamingError(f"network error talking to Sarvam: {message}", code="network")
        return StreamingError(
            f"unexpected error from the Sarvam SDK ({exc.__class__.__name__}): {message}",
            code="internal",
        )

    def _scrub(self, text: str) -> str:
        """Remove the API key from any message before it can be logged or sent."""
        key = self._config.api_key
        if key and len(key) >= 4:
            return text.replace(key, "***")
        return text


def _now() -> float:
    """The running loop's monotonic clock, falling back to ``time.monotonic``."""
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:  # pragma: no cover - only reachable off the loop
        return time.monotonic()


def _field(message: Any, name: str) -> Any:
    """Read ``name`` from a pydantic model or a plain dict; ``None`` when absent."""
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _as_text(value: Any) -> str:
    """Coerce a field to ``str``; ``None`` becomes the empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


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


__all__ = [
    "DEFAULT_ENDPOINTING",
    "DEFAULT_ENCODING",
    "DEFAULT_LANGUAGE_CODE",
    "REALTIME_MODELS",
    "RealtimeTranscriber",
    "StreamingError",
    "Transcript",
]

"""Live (streaming) Dictate transcription for the dictation daemon.

:mod:`sarvam_dictation.streaming` opens Sarvam's Saaras realtime socket through
the ``sarvamai`` SDK and reports raw partial transcripts while the user is still
speaking. This module is its twin for the **Dictate** socket. It keeps the same
shape - ``open()``, ``feed(pcm)``, ``finish()``, ``cancel()``, a hand-entered
async context manager, a background receiver task, an injectable ``client`` -
and differs only where the Dictate protocol itself differs: the text it returns
is the text the user *meant to type* (punctuated, fillers dropped, the user's
own spellings) rather than a transcript, and a turn is closed by a
``dictation.final`` rather than by a ``transcript.final``. The two modules must
stay recognisably the same piece of code, because the daemon drives both
through one interface, and both now talk to a Sarvam socket the same way: the
SDK's ``client.dictate_streaming.connect(...)`` / ``client.
speech_to_text_realtime_streaming.connect(...)`` context manager, the SDK's
request models, and ``recv()`` handing back the SDK's event models. Nothing
here builds a URL, a query string, a header dict or a JSON frame - the SDK owns
the transport.

The session is opened with ``endpointing=manual``. The addon is push-to-talk or
toggle: one key press is one turn, so the daemon says when speech starts
(``speech_start``) and when it stops (``speech_end``) instead of letting the
server guess from a pause. That is also what makes the billing honest - the
server only turns audio it was told to expect into a turn, so only the audio the
user actually dictated is billed, not the silence between presses. (``vad``
would also refuse those two control messages outright, and would cut a long
utterance at 18-20 s even inside a word.)

Turns are reassembled by ``utterance_idx``, never by arrival order. The
contract warns that a later turn's ``transcript.final`` can arrive *before* an
earlier turn's ``dictation.final``, so :meth:`DictateRealtimeTranscriber.finish`
sorts the finished turns by index before joining them. Arrival order says
nothing about the order of speech.

Four more properties shape the implementation:

* ``sarvamai`` is imported lazily, inside
  :meth:`DictateRealtimeTranscriber._resolve_client`. Importing this module, and
  every test of it, works on a machine where the SDK is not installed. When a
  ``client`` is injected the SDK is never imported, constructed or called at
  all: the injected object supplies ``dictate_streaming.connect`` and the
  request models are replaced by wire-compatible stand-ins.
* ``output.final`` is read and thrown away. When the user configures
  ``output_rules`` the socket emits a rewritten output before ``session.end``;
  a dictation plugin types what was said, not a summary of it, so the text of
  ``output.final`` never reaches the keyboard - but the message must still be
  consumed, or the read loop would stall before ``session.end`` and the session
  would end on a timeout instead of on its own terms.
* ``finish()`` sends ``end`` and then keeps reading until ``session.end``. The
  contract is explicit that closing the socket without ``end`` loses results
  that have not been sent yet, and the output call behind ``output_rules`` can
  take up to 90 s, which is why the wait for ``session.end`` is bounded
  generously rather than tightly.
* Every audio message carries at most 1 s of audio (0.5 s with
  ``stream_type=fast``), so ``feed()`` buffers the recorder's chunks into blocks
  of exactly that size rather than trusting the recorder to respect a limit it
  knows nothing about - an oversized message is answered with
  ``chunk_too_large`` and the audio in it is lost. A keepalive task pings the
  socket, because the server closes a session that has been quiet for 60 s
  (``1008``) and a user who opens the session and then thinks about what to say
  should not lose it.

A partial dictation is better than no dictation: when a fatal ``error`` arrives
or the socket dies with a non-1000 close code *after* at least one turn has been
finished, :meth:`finish` returns what it has and logs the failure instead of
raising. It raises only when there is nothing to return.

Every SDK call made here is checked against ``sarvamai`` 0.1.49:
``AsyncSarvamAI(api_subscription_key=...).dictate_streaming.connect()`` is an
``@asynccontextmanager`` taking ``language_code``, ``mode``, ``model``,
``endpointing``, ``stream_type``, ``encoding`` and ``sample_rate`` (a *string*);
the socket it yields has ``update_config``, ``speech_start``, ``send_audio``,
``speech_end``, ``end``, ``ping`` and ``recv()``, and ``recv()`` returns the
``Dictate*`` event dataclasses (``DictateSessionBegin``,
``DictateConfigUpdated``, ``DictateTranscriptPartial``, ``DictateDictationFinal``,
``DictateOutputFinal``, ``DictateSessionEnd``, ``DictateError``), which are also
accepted here as plain dicts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno as _errno
import logging
import socket
from typing import Any, Callable

from .config import (
    DEFAULT_DICTATE_LANGUAGE,
    DEFAULT_DICTATE_STREAM_TYPE,
    DICTATE_MODES,
    DICTATE_SAMPLE_RATES,
    VALID_DICTATE_STREAM_TYPES,
    Config,
)
from .streaming import StreamingError, Transcript

logger = logging.getLogger(__name__)

#: ``linear16`` is raw little-endian signed 16-bit PCM, mono, at ``sample_rate``
#: - exactly what the capture tools write, so no WAV header is ever added. The
#: contract allows ``linear32``/``mulaw``/``alaw`` too; the recorder produces
#: none of them, so this module never asks for them.
DEFAULT_ENCODING = "linear16"

#: We delimit turns from the hotkey, so the server must not guess from a pause.
#: Under ``vad`` the ``speech_start``/``speech_end`` control messages are refused
#: with ``invalid_control``, so this value and those two messages are one decision.
DEFAULT_ENDPOINTING = "manual"

#: The SDK release that ships the Dictate clients. The daemon's floor, quoted in
#: the "SDK missing" message so the notification tells the user what to install.
SDK_REQUIREMENT = "sarvamai>=0.1.49"

#: Bytes per sample for ``linear16`` mono: what turns a duration into the byte
#: budget of one ``audio_input`` message in
#: :meth:`DictateRealtimeTranscriber._chunk_limit`.
BYTES_PER_SAMPLE = 2

#: At most 1 s of audio per ``audio_input`` message - 0.5 s with
#: ``stream_type=fast``. Both are contract limits, not tunables: a longer message
#: is answered with ``chunk_too_large`` and the audio in it is lost.
MAX_CHUNK_S = 1.0
FAST_CHUNK_S = 0.5

#: How long ``open()`` waits for ``session.begin``. A socket that accepts the
#: handshake and then says nothing is a broken service, not a slow one; waiting
#: forever would hang the daemon's hotkey handler.
SESSION_BEGIN_TIMEOUT_S = 10.0

#: How long ``open()`` waits for ``config.updated``. The contract makes an
#: unconfirmed config change fatal on the server side (``config_unconfirmed``
#: after 5 s), so a client that waits much longer than that is waiting for
#: something that will never come.
CONFIG_UPDATED_TIMEOUT_S = 10.0

#: How long ``finish()`` waits for ``session.end``. The output call behind
#: ``output_rules`` can take up to 90 s, and the last ``dictation.final`` only
#: arrives before ``output.final``, so this must comfortably exceed 90 s or a
#: session that is working normally would be reported as a dead link.
SESSION_END_TIMEOUT_S = 120.0

#: The contract closes a session that has sent nothing for 60 s with ``1008``.
#: Audio normally covers that, but a user who opens the session and then thinks
#: (or a recording that stalls) sends nothing at all, so a ping goes out well
#: inside the window. Half the limit, so one lost ping is not fatal either.
KEEPALIVE_INTERVAL_S = 30.0

#: The wire name of each request the socket sends. The model factories in
#: ``_models`` are keyed by a Python identifier (``config.update`` is not one),
#: so the stand-in built for an injected client needs the mapping to say
#: ``{"event": "config.update"}`` exactly as the SDK's own model does.
_REQUEST_EVENTS = {
    "audio_input": "audio_input",
    "speech_start": "speech_start",
    "speech_end": "speech_end",
    "end": "end",
    "ping": "ping",
    "config_update": "config.update",
}

#: Exception class names (transport style, whatever library the SDK uses
#: underneath) meaning "the link is gone".
_NETWORK_EXCEPTION_NAMES = frozenset(
    {
        "ConnectionClosed",
        "ConnectionClosedError",
        "ConnectionClosedOK",
        "ConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "InvalidHandshake",
        "InvalidStatus",
        "InvalidStatusCode",
        "InvalidURI",
        "NetworkError",
        "ReadError",
        "ReadTimeout",
        "Timeout",
        "TimeoutError",
        "TimeoutException",
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


class _Request:
    """Stand-in for the SDK's request models when the SDK is not in play.

    The real socket client only ever calls ``data.dict()`` and hands the result
    to ``json.dumps`` (``AsyncDictateStreamingSocketClient._send_model``), so any
    object with a ``dict()`` method is wire-compatible. Injecting a fake client
    therefore never needs the SDK to be importable - which is the whole point of
    the injected-client path, since the daemon's test suite runs on machines
    without the SDK installed.
    """

    def __init__(self, **fields: Any) -> None:
        self._fields = fields
        # The real models are plain attributes (``message.event``,
        # ``message.audio``), so the stand-in keeps the same shape.
        for name, value in fields.items():
            setattr(self, name, value)

    def dict(self) -> dict[str, Any]:
        """Return the JSON body this request would be sent as (``None`` dropped)."""
        return {name: value for name, value in self._fields.items() if value is not None}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Request({self.dict()!r})"


class DictateRealtimeTranscriber:
    """Transcribes raw PCM into dictation text while it is being captured,
    through Sarvam's Dictate live socket. One instance per recording.

    ``on_partial`` is called from the receiver task with the best current
    preview - the turns that have finished, plus the text of the turn in flight,
    already stripped and never empty - and may be called from any task. ``client``
    may be injected (tests pass a fake); when it is, the ``sarvamai`` SDK is never
    imported and no network call is ever made. An injected client models the SDK:
    an object whose ``dictate_streaming.connect(**kwargs)`` is an async context
    manager yielding a socket with ``update_config``/``speech_start``/
    ``send_audio``/``speech_end``/``end``/``ping`` and ``recv()``.
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
        self._keepalive: asyncio.Task[None] | None = None
        self._begin_event = asyncio.Event()
        self._config_event = asyncio.Event()
        self._session_end_event = asyncio.Event()
        #: utterance_idx -> finished (``dictation.final``) text, in idx order.
        self._turns: dict[int, str] = {}
        #: utterance_idx -> latest ``transcript.partial`` text for that turn.
        self._partials: dict[int, str] = {}
        #: utterance_idx -> ``language_code`` reported by a ``dictation.final``.
        self._turn_languages: dict[int, str] = {}
        #: utterance_idx -> ``language_code`` reported by a ``transcript.final``.
        self._transcript_languages: dict[int, str] = {}
        #: Raw PCM not yet sent: kept under the per-message limit by ``feed``.
        self._buffer = bytearray()
        self._awaiting_config_updated = False
        self._config_confirmed = False
        self._config_error: str | None = None
        self._session_end_received = False
        self._close_code: int | None = None
        self._error: StreamingError | None = None
        self._fatal = False
        self._end_sent = False
        self._finished = False
        self._closed = False

    # -- lifecycle --------------------------------------------------------

    async def open(self) -> None:
        """Open the session, apply the configuration and start the turn.

        Everything that can be checked without a socket is checked first
        (``no_api_key``, ``config_invalid``), then the settings payload is built,
        so a missing sibling module fails before the network is touched. After
        the handshake the order is fixed by the contract: wait for
        ``session.begin``, send one ``config.update``, wait for
        ``config.updated``, and only then ``speech_start``.
        """
        if self._ws is not None or self._finished or self._closed:
            return

        self._preflight()
        # Built before connecting: the builders live in the sibling clip module,
        # and a missing one is a broken installation, not a reason to open a
        # socket we would only have to tear down again.
        fields = self._config_fields()

        client = self._resolve_client()
        service = getattr(client, "dictate_streaming", None)
        connect = getattr(service, "connect", None)
        if connect is None:
            raise StreamingError(
                "the Sarvam client has no dictate_streaming.connect(); the "
                f"installed {SDK_REQUIREMENT.split('>=')[0]} is too old for the "
                "Dictate socket - upgrade it with "
                f"pip install '{SDK_REQUIREMENT}'",
                code="internal",
            )

        # The models only exist once the client has been resolved: an injected
        # client supplies its own objects, so this is a stand-in for it.
        update = self._event("config_update", **fields) if fields else None

        kwargs = self._connect_kwargs()
        # The key travels inside the SDK, never in a URL we build: the contract
        # refuses a key in the query string with 403, and a URL is the one thing
        # that ends up in logs.
        logger.debug(
            "opening Dictate session (mode=%s, sample_rate=%s, stream_type=%s)",
            kwargs.get("mode"),
            kwargs.get("sample_rate"),
            kwargs.get("stream_type"),
        )
        try:
            ctx = connect(**kwargs)
            ws = await ctx.__aenter__()
        except Exception as exc:  # connect/auth/transport failures are mapped
            raise self._map_exception(exc) from exc

        self._ctx = ctx
        self._ws = ws
        self._receiver = asyncio.ensure_future(self._receive())
        self._keepalive = asyncio.ensure_future(self._ping_loop())

        try:
            if not await self._wait_for(self._begin_event, SESSION_BEGIN_TIMEOUT_S):
                raise StreamingError(
                    "the Dictate socket accepted the connection but sent no "
                    f"session.begin within {SESSION_BEGIN_TIMEOUT_S:.0f}s; "
                    "the session cannot be started",
                    code="api",
                )
            if self._error is not None:
                # A fatal error can arrive before session.begin; the wait above
                # returns either way, so the error is raised rather than letting
                # the session go on over a socket the server has given up on.
                raise self._error

            if update is not None:
                # One update carrying every setting: the contract checks the
                # whole update atomically and refuses a second one before
                # config.updated. With nothing configured there is nothing to
                # send - and the service refuses an update that changes nothing.
                self._awaiting_config_updated = True
                await ws.update_config(update)
                if not await self._wait_for(self._config_event, CONFIG_UPDATED_TIMEOUT_S):
                    raise StreamingError(
                        "the Dictate socket did not confirm config.update with "
                        f"config.updated within {CONFIG_UPDATED_TIMEOUT_S:.0f}s",
                        code="api",
                    )
                self._awaiting_config_updated = False
                if self._error is not None:
                    raise self._error
                if self._config_error is not None:
                    # A no-op update is answered with invalid_config by the
                    # contract, which happens whenever the user has configured
                    # little or nothing. Refusing to dictate over that would be
                    # absurd: the settings are optional, so a rejected update is
                    # logged and the session goes on with the server's defaults.
                    logger.warning(
                        "Dictate did not apply config.update: %s", self._config_error
                    )

            # endpointing=manual: the turn starts when the key goes down.
            await ws.speech_start(self._event("speech_start"))
        except StreamingError:
            await self.cancel()
            raise
        except Exception as exc:
            await self.cancel()
            raise self._map_exception(exc) from exc

    async def feed(self, pcm: bytes) -> None:
        """Send raw s16le mono PCM at ``config.sample_rate``, one second at a time.

        The recorder's chunk size is its own business, so chunks are buffered and
        sent as blocks of at most one second (half a second with
        ``stream_type=fast``). Sending the recorder's chunk verbatim would sooner
        or later exceed the limit, and the answer to that is ``chunk_too_large``
        with the audio discarded - the words in that chunk are simply gone.

        A no-op before :meth:`open` and after :meth:`finish`: the recorder can
        outlive a session that failed to open, and draining its chunks must not
        become an exception storm in the daemon.
        """
        if self._ws is None or not pcm or self._finished or self._closed:
            return
        self._buffer.extend(pcm)
        limit = self._chunk_limit()
        while len(self._buffer) >= limit:
            block = bytes(self._buffer[:limit])
            del self._buffer[:limit]
            await self._send_audio(block)

    async def finish(self) -> Transcript:
        """End the turn and the session, and return the dictation.

        Sends ``speech_end`` and then ``end``, and keeps reading until
        ``session.end``: the contract warns that closing the socket without
        ``end`` loses results the server has not sent yet, and ``output.final``
        (up to 90 s after the last turn when ``output_rules`` is set) arrives
        before ``session.end``, so a shorter wait would cut off a working session.

        Returns the finished turns joined in ``utterance_idx`` order. Raises
        :class:`StreamingError` only when there is nothing to return: a fatal
        ``error``, a non-1000 close before ``session.end``, or a ``session.end``
        that never arrives. Once at least one turn has been finished, any of
        those is logged and the partial dictation is returned instead.
        """
        if self._finished:
            return self._transcript()
        self._finished = True

        ws = self._ws
        if ws is not None:
            # Whatever is still buffered is the tail of the utterance: send it
            # before speech_end or the last word of every recording is lost.
            await self._flush_audio()
            # The SDK names each sender after the event it sends.
            for name in ("speech_end", "end"):
                try:
                    await getattr(ws, name)(self._event(name))
                except Exception as exc:
                    logger.debug("cannot send %s: %s", name, self._scrub(str(exc)))
                    if self._error is None:
                        self._error = self._map_exception(exc)
                    break
                if name == "end":
                    self._end_sent = True

        outcome = await self._await_session_end()
        if outcome == "timeout":
            logger.warning(
                "no session.end arrived within %.0fs; returning %d finished turn(s) "
                "and the tail of the dictation may be missing",
                SESSION_END_TIMEOUT_S,
                len(self._turns),
            )
            if self._error is None:
                self._error = StreamingError(
                    "the Dictate session did not send session.end within "
                    f"{SESSION_END_TIMEOUT_S:.0f}s; the results already received "
                    "are returned, later ones are lost",
                    code="network",
                )

        await self._close(send_end=False)

        if self._error is not None:
            if self._has_text():
                # A partial dictation beats an error dialog: the user pressed a
                # key and said something, and what was recognised is usable.
                logger.warning(
                    "returning %d finished turn(s) despite a failed session: %s",
                    len(self._turns),
                    self._error,
                )
            else:
                raise self._error
        return self._transcript()

    async def cancel(self) -> None:
        """Drop the session without waiting for a dictation. Idempotent.

        ``end`` goes out when the session is still open and has not already been
        ended by :meth:`finish`: closing a Dictate socket without ``end`` is what
        the contract calls losing results, and it leaves the server holding a
        turn it will never close. Unlike :meth:`finish` this does not wait for
        ``session.end`` - the results are being thrown away - but the receiver
        and keepalive tasks are always joined and the SDK's context manager is
        always exited, so a cancelled session never leaves a task behind.
        """
        send_end = self._ws is not None and not self._end_sent and not self._closed
        self._finished = True
        await self._close(send_end=send_end)

    # -- pre-flight and the connect arguments ------------------------------

    def _preflight(self) -> None:
        """Refuse a configuration the Dictate socket cannot serve.

        Every check runs before any client or socket work, so a misconfigured
        daemon never opens a connection: no key at all (``no_api_key``), a
        ``mode`` Dictate does not have (``config_invalid`` - it has no
        ``translate``), and a sample rate the socket refuses (``config_invalid``;
        anything but 16000 or 8000 is answered 400 at the handshake).
        """
        if not (self._config.api_key or "").strip():
            raise StreamingError(
                "no Sarvam API key configured; set api.key in "
                "~/.config/sarvam-dictation/config.toml or export SARVAM_API_KEY",
                code="no_api_key",
            )

        mode = self._config.mode
        if mode not in DICTATE_MODES:
            raise StreamingError(
                f"[api] mode {mode!r} cannot be used with the Dictate engine; "
                f"choose one of {', '.join(DICTATE_MODES)}. Dictate has no "
                "'translate' mode - ask for a translation in [dictate] rules "
                "instead.",
                code="config_invalid",
            )

        rate = self._config.sample_rate
        if rate not in DICTATE_SAMPLE_RATES:
            raise StreamingError(
                f"[audio] sample_rate {rate} cannot be used with the Dictate "
                f"socket, which accepts only {DICTATE_SAMPLE_RATES[0]} or "
                f"{DICTATE_SAMPLE_RATES[1]} Hz. Dictate does not check the rate "
                "against the audio, so a wrong one would silently produce wrong "
                "text and double the billed seconds.",
                code="config_invalid",
            )

        stream_type = self._stream_type()
        if stream_type not in VALID_DICTATE_STREAM_TYPES:
            raise StreamingError(
                f"[dictate] stream_type {stream_type!r} is not one of "
                f"{', '.join(VALID_DICTATE_STREAM_TYPES)}",
                code="config_invalid",
            )

    def _stream_type(self) -> str:
        """The configured ``stream_type``, defaulted. It sets the chunk limit."""
        return self._config.dictate_stream_type or DEFAULT_DICTATE_STREAM_TYPE

    def _connect_kwargs(self) -> dict[str, Any]:
        """Build the ``connect()`` keyword arguments for this configuration.

        These are the settings the contract cannot change after the handshake,
        so they are handed to the SDK instead of a URL it would build for us.
        ``sample_rate`` is a string because the SDK declares it as one;
        ``model`` is ``None`` when the user pinned nothing, so the server's own
        default applies; ``language_code`` falls back to ``auto``, which is a
        real value on this socket rather than "leave it out" - an empty
        ``[api] language_code`` means *detect*, and ``auto`` is how you say so.
        """
        model = (self._config.dictate_model or "").strip()
        return {
            "language_code": self._config.language_code or DEFAULT_DICTATE_LANGUAGE,
            "mode": self._config.mode,
            "model": model or None,
            "endpointing": DEFAULT_ENDPOINTING,
            "stream_type": self._stream_type(),
            "encoding": DEFAULT_ENCODING,
            "sample_rate": str(self._config.sample_rate),
        }

    def _config_fields(self) -> dict[str, Any]:
        """The members of the single ``config.update`` for this session.

        The three builders are imported here rather than at module scope so this
        module imports when the sibling clip module is absent (tests run without
        it), and so the SDK stays optional. Each returns ``None`` for "the user
        did not set this", and a field the user did not set must not be sent at
        all - an explicit empty value would clear a server-side default we never
        asked to change. An empty result means no update is sent at all, because
        the service refuses an update that changes nothing.

        ``output_rules`` is the one setting with no builder: it is a plain string
        from the config, and an empty one means "do not turn the output call on".
        """
        try:
            from .dictate import context_payload, style_payload, vocabulary_payload
        except ModuleNotFoundError as exc:  # pragma: no cover - broken install
            raise StreamingError(
                "the Dictate payload builders are missing "
                "(sarvam_dictation.dictate could not be imported); the daemon "
                f"installation is incomplete: {self._scrub(str(exc))}",
                code="internal",
            ) from exc

        fields: dict[str, Any] = {}
        style = style_payload(self._config)
        if style is not None:
            fields["style"] = style
        vocabulary = vocabulary_payload(self._config)
        if vocabulary is not None:
            fields["vocabulary"] = vocabulary
        context = context_payload(self._config)
        if context is not None:
            fields["context"] = context
        output_rules = (self._config.dictate_output_rules or "").strip()
        if output_rules:
            fields["output_rules"] = output_rules
        return fields

    def _chunk_limit(self) -> int:
        """The byte budget of one ``audio_input`` message, in whole samples."""
        seconds = FAST_CHUNK_S if self._stream_type() == "fast" else MAX_CHUNK_S
        samples = int(seconds * self._config.sample_rate)
        return samples * BYTES_PER_SAMPLE

    # -- the SDK boundary --------------------------------------------------

    def _resolve_client(self):
        """Return the injected client, or build one with the lazily imported SDK."""
        if self._client is not None:
            return self._client
        try:
            import sarvamai  # imported here so the SDK stays optional
        except ModuleNotFoundError as exc:
            raise StreamingError(
                "the sarvamai SDK is not installed; install it with "
                f"pip install '{SDK_REQUIREMENT}' and start the dictation again",
                code="internal",
            ) from exc

        # The Dictate clients only exist from 0.1.49 on, so an older SDK that
        # still imports is as unusable as a missing one - and saying so is far
        # more useful than a traceback about a missing attribute.
        if getattr(sarvamai, "DictateConfigUpdate", None) is None:
            raise StreamingError(
                "the installed sarvamai SDK is too old for the Dictate socket; "
                f"upgrade it with pip install '{SDK_REQUIREMENT}'",
                code="internal",
            )

        self._models = {
            "audio_input": sarvamai.DictateAudioInput,
            "speech_start": sarvamai.DictateSpeechStart,
            "speech_end": sarvamai.DictateSpeechEnd,
            "end": sarvamai.DictateEnd,
            "ping": sarvamai.DictatePing,
            "config_update": sarvamai.DictateConfigUpdate,
        }
        try:
            return sarvamai.AsyncSarvamAI(api_subscription_key=self._config.api_key)
        except Exception as exc:  # the SDK raises its own types on bad input
            raise StreamingError(
                f"cannot create the Sarvam client: {self._scrub(str(exc))}",
                code="api",
            ) from exc

    def _event(self, name: str, **fields: Any) -> Any:
        """Build the request model for ``name`` (SDK model, or a stand-in).

        The builders in the sibling clip module may return plain dicts or SDK
        models, and this module deliberately does not care which: whatever they
        return is handed straight to the SDK model, which accepts either. The
        clip models and the streaming models are separate classes in the SDK,
        though, and pydantic refuses one where the other is declared - so if the
        SDK rejects the values, they are re-handed as their own ``dict()`` dump,
        which every SDK model does accept. That keeps this module working
        whichever shape the sibling ends up returning, without importing
        anything from it at module scope.
        """
        factory = self._models.get(name)
        if factory is None:
            return _Request(event=_REQUEST_EVENTS.get(name, name), **fields)
        try:
            return factory(**fields)
        except Exception:
            normalised = {key: _plain(value) for key, value in fields.items()}
            if normalised == fields:  # nothing to gain, so surface the real error
                raise
            logger.debug("re-handed %s to the SDK as plain values", name)
            return factory(**normalised)

    # -- receiver ---------------------------------------------------------

    async def _receive(self) -> None:
        """Drain ``recv()`` until ``session.end``, a close, or our own shutdown."""
        ws = self._ws
        while ws is not None and not self._closed and not self._fatal:
            try:
                message = await ws.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Normal end of stream or a transport failure: either way there
                # is nothing left to read, and the close code says which it was.
                self._note_close(exc)
                break
            try:
                self._handle(message)
            except Exception as exc:  # a malformed message must not kill the session
                logger.debug("ignoring unusable Dictate message: %s", self._scrub(str(exc)))
            if self._session_end_received:
                # session.end is the last message; the server closes 1000 after it.
                break

    def _handle(self, message: Any) -> None:
        """Apply one server message to the session state."""
        event = _field(message, "event")
        if event == "session.begin":
            self._begin_event.set()
        elif event == "config.updated":
            self._config_confirmed = True
            self._config_event.set()
        elif event == "transcript.partial":
            self._handle_partial(message)
        elif event == "transcript.final":
            self._handle_transcript_final(message)
        elif event == "dictation.final":
            self._handle_dictation_final(message)
        elif event == "output.final":
            # Read and discarded on purpose: a dictation plugin types what was
            # said, not the rewritten output. Consuming it is what keeps the
            # read loop alive until session.end.
            logger.debug(
                "ignoring output.final (%d characters)",
                len(_as_text(_field(message, "text"))),
            )
        elif event == "error":
            self._handle_error(message)
        elif event == "session.end":
            self._session_end_received = True
            self._session_end_event.set()
        elif event in ("vad.speech_start", "vad.speech_end", "pong"):
            logger.debug("Dictate event: %s", event)
        else:
            logger.debug("unhandled Dictate event: %r", event)

    def _handle_partial(self, message: Any) -> None:
        """Forward the best preview so far; never forward an empty string.

        Manual endpointing normally means one turn, but the accumulation is
        written generally: the finished turns in ``utterance_idx`` order, then
        the text of the turn still in flight. A partial for a turn that has
        already finished is stale and is dropped rather than appended twice.
        """
        index = _utterance_index(message, len(self._turns))
        text = _as_text(_field(message, "text")).strip()
        if text:
            self._partials[index] = text
        preview = self._preview()
        if not preview:
            return
        callback = self._on_partial
        if callback is None:
            return
        try:
            callback(preview)
        except Exception as exc:  # the caller's sink must not break the receiver
            logger.debug("on_partial callback failed: %s", exc)

    def _handle_transcript_final(self, message: Any) -> None:
        """Remember the raw turn text's language; it is not the typed text."""
        index = _utterance_index(message, len(self._turns))
        language = _as_text(_field(message, "language_code")).strip()
        if language:
            self._transcript_languages[index] = language
        logger.debug(
            "transcript.final for turn %d (%d characters)",
            index,
            len(_as_text(_field(message, "text"))),
        )

    def _handle_dictation_final(self, message: Any) -> None:
        """Record one finished turn, keyed by ``utterance_idx``.

        A turn whose text is empty (``degraded`` ``asr_error``/``asr_timeout``)
        is still a turn: it is counted in ``segments`` but contributes nothing to
        the text, which is what keeps the count honest about how many things the
        user said.
        """
        index = _utterance_index(message, len(self._turns))
        text = _as_text(_field(message, "text")).strip()
        self._turns[index] = text
        self._partials.pop(index, None)
        language = _as_text(_field(message, "language_code")).strip()
        if language:
            self._turn_languages[index] = language
        logger.debug(
            "dictation.final for turn %d: %d characters (degraded=%s)",
            index,
            len(text),
            _field(message, "degraded"),
        )

    def _handle_error(self, message: Any) -> None:
        """Turn a fatal server error into a :class:`StreamingError`.

        A non-fatal error is logged and the session continues - except while
        ``config.update`` is unconfirmed, where it is also the answer that
        releases the wait (a rejected update arrives as ``invalid_config`` and
        never as ``config.updated``). That case is not an error at all: the
        settings are optional, so the session carries on with the server's
        defaults.
        """
        code = _as_text(_field(message, "code")) or "unknown"
        detail = _as_text(_field(message, "message"))
        fatal = bool(_field(message, "is_fatal"))
        described = f"Sarvam Dictate error {code}: {detail}".rstrip(": ").strip()
        described = self._scrub(described)

        if not fatal:
            logger.warning("non-fatal Dictate error: %s", described)
            if self._awaiting_config_updated and not self._config_confirmed:
                self._config_error = described
                self._config_event.set()
            return

        logger.warning("fatal Dictate error: %s", described)
        if self._error is None:
            self._error = StreamingError(described, code="api")
        self._fatal = True
        # A fatal error is followed by a close, but wake the waits now so
        # finish() does not sit out its timeout for a session that is over.
        self._begin_event.set()
        self._config_event.set()

    def _note_close(self, exc: Exception) -> None:
        """Record why the socket stopped, and map a dead link to ``network``.

        The SDK does not wrap the transport's close frame, so the code is read
        off the exception it raised. ``1000`` is the normal end after
        ``session.end``; every other documented code means the link died under
        us (protocol error, no message for 60 s, message too large, recognition
        stopped, service overloaded, keepalive timeout). ``1012`` finishes open
        turns when it can - if it managed, ``session.end`` has already arrived
        and this is not an error.
        """
        code = _close_code(exc)
        self._close_code = code
        if self._session_end_received:
            return
        if code is None or code == 1000:
            # No code at all, or a clean close without session.end: finish()
            # reports the missing session.end, which is the more useful fact.
            logger.debug("Dictate receive loop finished: %s", self._scrub(str(exc)))
            return
        logger.warning(
            "Dictate socket closed with code %s before session.end: %s",
            code,
            self._scrub(str(exc)),
        )
        if self._error is None:
            self._error = StreamingError(
                f"the Dictate socket closed with code {code} before session.end: "
                f"{self._scrub(str(exc))}",
                code="network",
            )

    async def _ping_loop(self) -> None:
        """Send ``ping`` during long silences.

        The contract closes a socket that has sent nothing for 60 s with
        ``1008``. Audio messages cover the normal case, but a session opened and
        then left thinking sends nothing at all - and the user would lose the
        session, not just the silence - so a ping goes out every
        :data:`KEEPALIVE_INTERVAL_S`.
        """
        ws = self._ws
        while not self._closed and ws is not None:
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            except asyncio.CancelledError:
                raise
            if self._closed or self._end_sent:
                return
            try:
                await ws.ping(self._event("ping"))
            except Exception as exc:
                logger.debug("cannot send ping: %s", self._scrub(str(exc)))
                return

    # -- waiting ----------------------------------------------------------

    async def _wait_for(self, event: asyncio.Event, timeout: float) -> bool:
        """Wait for ``event``, the receiver ending, or ``timeout``.

        Watching the receiver as well is what stops ``open()`` from waiting out
        its whole timeout on a socket that has already died: a closed socket will
        never send the event being waited for.
        """
        if event.is_set():
            return True
        receiver = self._receiver
        waiter = asyncio.ensure_future(event.wait())
        tasks: set[asyncio.Task] = {waiter}
        if receiver is not None:
            tasks.add(receiver)
        try:
            await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        return event.is_set()

    async def _await_session_end(self) -> str:
        """Wait for ``session.end``. ``"end"``, ``"closed"`` or ``"timeout"``.

        ``"closed"`` means the socket ended without ``session.end``; whether that
        is an error has already been decided by :meth:`_note_close`.
        """
        if self._session_end_received:
            return "end"
        receiver = self._receiver
        if receiver is None:
            return "closed"
        waiter = asyncio.ensure_future(self._session_end_event.wait())
        try:
            await asyncio.wait(
                {waiter, receiver},
                timeout=SESSION_END_TIMEOUT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        if self._session_end_received:
            return "end"
        if receiver.done():
            return "closed"
        return "timeout"

    # -- sending ----------------------------------------------------------

    async def _send_audio(self, block: bytes) -> None:
        """Send one ``audio_input`` message: base64 of raw samples, no header.

        Never called with an empty block: an empty ``audio_input`` is at best
        pointless and at worst an ``invalid_audio`` error in the middle of a
        dictation.
        """
        if not block:
            return
        ws = self._ws
        if ws is None:
            logger.debug("dropping audio chunk: the Dictate session is not open")
            return
        audio = base64.b64encode(block).decode("ascii")
        try:
            await ws.send_audio(self._event("audio_input", audio=audio))
        except Exception as exc:
            # A dead socket during capture is reported by finish()/cancel();
            # dropping a chunk keeps the daemon's drain task alive meanwhile.
            logger.debug("dropping audio chunk: %s", self._scrub(str(exc)))

    async def _flush_audio(self) -> None:
        """Send the buffered tail of the utterance, if there is one."""
        if not self._buffer:
            return
        block = bytes(self._buffer)
        self._buffer.clear()
        await self._send_audio(block)

    # -- closing ----------------------------------------------------------

    async def _close(self, send_end: bool) -> None:
        """End the session: optional ``end``, join both tasks, exit the context.

        Safe to call more than once, and always joins the receiver and the
        keepalive and exits the SDK's context manager, so neither a task nor the
        socket outlives the transcriber.
        """
        self._closed = True
        ws = self._ws
        if ws is not None and send_end:
            try:
                await ws.end(self._event("end"))
                self._end_sent = True
            except Exception as exc:
                logger.debug("cannot send end: %s", self._scrub(str(exc)))

        tasks = [task for task in (self._receiver, self._keepalive) if task is not None]
        self._receiver = None
        self._keepalive = None
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            # gather() never raises for a cancelled or failed task, so the
            # context manager is always exited below.
            await asyncio.gather(*tasks, return_exceptions=True)

        ctx, self._ctx = self._ctx, None
        self._ws = None
        self._buffer.clear()
        if ctx is not None:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception as exc:  # closing a broken socket is not an error
                logger.debug("error while closing the Dictate session: %s", self._scrub(str(exc)))

    # -- helpers ----------------------------------------------------------

    def _transcript(self) -> Transcript:
        """Build the result: turns in ``utterance_idx`` order, single-spaced.

        ``segments`` counts turns, not words, and is never 0: a session that
        produced nothing is still one dictation that came back empty, and the
        addon reports it the same way it reports a clip of background noise.
        """
        pieces = [
            self._turns[index].strip()
            for index in sorted(self._turns)
            if self._turns[index].strip()
        ]
        return Transcript(
            text=" ".join(pieces),
            language_code=self._language(),
            segments=max(1, len(self._turns)),
        )

    def _language(self) -> str | None:
        """The ``language_code`` the finals reported, in ``utterance_idx`` order."""
        for source in (self._turn_languages, self._transcript_languages):
            for index in sorted(source):
                if source[index]:
                    return source[index]
        return None

    def _preview(self) -> str:
        """Finished turns joined, plus the turn in flight; never empty.

        A partial belonging to a turn that has already produced a
        ``dictation.final`` is stale and is skipped, so the preview never shows
        the same words twice.
        """
        pieces = [
            self._turns[index].strip()
            for index in sorted(self._turns)
            if self._turns[index].strip()
        ]
        pending = [
            index
            for index in sorted(self._partials)
            if not self._turns.get(index, "").strip()
        ]
        if pending:
            pieces.append(self._partials[pending[-1]].strip())
        return " ".join(piece for piece in pieces if piece)

    def _has_text(self) -> bool:
        """True when at least one finished turn has something to type."""
        return any(text.strip() for text in self._turns.values())

    def _map_exception(self, exc: Exception) -> StreamingError:
        """Convert an SDK/transport exception into a StreamingError.

        A 401/403 at the handshake is ``api``, not ``network``: the contract
        refuses a bad key before the upgrade, so the failure is the server
        rejecting our credentials, not a broken link.
        """
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
            return StreamingError(
                f"network error talking to Sarvam: {message}", code="network"
            )
        return StreamingError(
            f"unexpected error from the Dictate socket "
            f"({exc.__class__.__name__}): {message}",
            code="internal",
        )

    def _scrub(self, text: str) -> str:
        """Remove the API key from any message before it can be logged or sent."""
        key = self._config.api_key
        if key and len(key) >= 4:
            return text.replace(key, "***")
        return text


# --------------------------------------------------------------------------
# module-level helpers
# --------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Best-effort plain-data form of ``value``, for re-handing it to the SDK.

    The SDK's request models accept dicts and their own model classes, and the
    sibling clip module may hand this module the clip models instead (the two
    families are separate pydantic classes). Anything that knows how to dump
    itself as a dict is dumped; a list is dumped member by member; everything
    else is passed through untouched, so this can only ever widen what the SDK
    accepts.
    """
    if isinstance(value, list):
        return [_plain(item) for item in value]
    dump = getattr(value, "dict", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except TypeError:  # pragma: no cover - a dump() without that keyword
            return dump()
    return value


def _field(message: Any, name: str) -> Any:
    """Read ``name`` from an SDK event model or a plain dict; ``None`` when absent."""
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


def _utterance_index(message: Any, fallback: int) -> int:
    """The turn index of a message, or ``fallback`` when it is missing/unusable.

    Sorting by this is what makes the transcript right: the contract allows a
    later turn's ``transcript.final`` to arrive before an earlier turn's
    ``dictation.final``, so arrival order says nothing about the order of speech.
    """
    value = _field(message, "utterance_idx")
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value if value >= 0 else fallback
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return fallback


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


def _close_code(exc: Exception) -> int | None:
    """The socket close code carried by ``exc``, when it carries one.

    The SDK does not translate transport failures, so the code is wherever the
    underlying library put it: on the close frames (``rcvd``/``sent``) first,
    then on the exception itself. A bare exception carries none, which means the
    link failed without a close frame. Nothing here imports that library - the
    attributes are read by name.
    """
    for owner in ("rcvd", "sent", "received", "close_frame"):
        value = getattr(getattr(exc, owner, None), "code", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    for attribute in ("code", "close_code"):
        value = getattr(exc, attribute, None)
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
    "DEFAULT_ENCODING",
    "DEFAULT_ENDPOINTING",
    "DictateRealtimeTranscriber",
    "SDK_REQUIREMENT",
    "StreamingError",
    "Transcript",
]

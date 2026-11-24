"""Sarvam **Dictate** for a recorded clip: one ``client.dictate.transcribe`` per chunk.

This is the Dictate twin of :mod:`sarvam_dictation.transcriber`, and the two
now share more than a shape: both go through the official ``sarvamai`` SDK.
Both answer the same question — "here is a WAV, give me the text" — and both
return a :class:`~sarvam_dictation.transcriber.Transcript`, raise
:class:`~sarvam_dictation.transcriber.TranscriptionError` with a code from the
daemon's fixed vocabulary, and split long audio with the *same* splitter. What
differs is the service behind them: Saaras returns what recognition heard,
Dictate returns the text the speaker meant to type (punctuation, fillers
dropped, names spelled the user's way). The daemon therefore picks a class,
not a code path, and the rest of the daemon cannot tell the two apart.

The SDK is imported lazily inside :meth:`DictateTranscriber._build_client` so
this module — and the whole test-suite — imports on a machine where
``sarvamai`` is not installed. A missing SDK at runtime is an ``internal``
error carrying the ``pip install`` line the user needs, exactly like
:mod:`transcriber`. An injected ``client`` bypasses the SDK entirely: tests
hand in a fake and ``sarvamai`` is never imported, never constructed and never
called.

``text`` rather than ``transcript`` is the result. The response carries both:
``transcript`` is what speech recognition heard, ``text`` is the finished
version with punctuation, fillers removed and the user's names spelled their
way — which is the whole reason the daemon offers a Dictate engine at all.
Typing ``transcript`` would make this module a slower Saaras.

Deliberately *not* done here:

* No model whitelist. ``transcriber.py`` refuses a model the REST endpoint does
  not serve, because Saaras has three disjoint model namespaces and a wrong one
  is a configuration mistake. Dictate has a single namespace of dated
  snapshots, so there is nothing to check against: ``[dictate] model`` is sent
  when set and left out when empty. Copying the guard would invent a list.
* No second WAV splitter and no second transcript joiner. ``split_wav`` and
  ``join_transcripts`` are imported from :mod:`transcriber` so a chunk boundary
  can never fall somewhere different on the two routes.
* ``degraded`` is not surfaced. When the API could not run its finishing step
  it says so and sets ``text`` to the raw transcript, so the user still gets
  usable text; turning that into an error would throw away a good result.
* ``output`` is never read. With ``output_rules`` set the API also returns a
  summarised ``output`` object; a dictation plugin types what was *said*.

Two places where the SDK does not do what the hand-written client did, and
what this module does about it:

* **Unset options are omitted, not passed as ``None``.** ``sarvamai`` 0.1.49
  only skips an argument that was never supplied; a keyword explicitly set to
  ``None`` is still encoded into the multipart body — as ``null`` for the JSON
  parts and as an empty part for the text ones, both of which the API answers
  with a ``400``. So :meth:`_transcribe_one` builds a ``kwargs`` dict and adds
  an option only when it is set.
* **The per-request timeout is passed explicitly.** The SDK's own default is
  60 s, but the finishing step may legitimately take 90 s on its own, so
  :data:`CLIP_TIMEOUT_S` is handed over as ``request_options`` and the
  documented headroom survives the move to the SDK.

The clip route takes at most 30 s of audio, so a longer recording is split into
28 s chunks and each chunk is finished on its own. That is a real trade-off:
the finishing step sees less context per chunk, so a sentence that straddles a
boundary is punctuated twice. It is the only option — the API answers 413
above its limit — and it matches what the Saaras path already does, so the two
engines degrade in the same way.
"""

from __future__ import annotations

import logging
import shutil
import wave
from pathlib import Path

from .config import DICTATE_MODES, Config
from .transcriber import (
    MAX_CHUNK_SECONDS,
    Transcript,
    TranscriptionError,
    _is_network_error,
    _status_code,
    join_transcripts,
    split_wav,
)

logger = logging.getLogger(__name__)

#: Seconds to wait for one chunk. The contract allows the finishing step up to
#: 90 s on its own (and 90 s again for the ``output_rules`` call), so a 28 s
#: clip can legitimately sit on the wire for well over a minute. 120 s clears
#: that documented ceiling with headroom; anything tighter turns a slow-but-
#: successful dictation into a spurious ``network`` error and loses the audio.
#: Passed to the SDK as ``request_options``, whose own default (60 s) is too
#: tight for this route.
CLIP_TIMEOUT_S = 120.0

#: The clip endpoint's own ceiling, in seconds. A recording that already fits is
#: sent whole: splitting a 29 s dictation into 28 s + 1 s would buy nothing and
#: pay for a second finishing call, so the splitter is reserved for audio that
#: genuinely cannot go in one request. The chunks themselves stay at
#: :data:`~sarvam_dictation.transcriber.MAX_CHUNK_SECONDS`, which leaves
#: headroom under this ceiling for the API's own rounding.
CLIP_MAX_SECONDS = 30.0


def style_payload(config: Config) -> dict | None:
    """Return the ``style`` value, or ``None`` when the user set nothing.

    Shape from the API contract (§1 and §3): ``{"preset": ...}`` where ``preset``
    is one of ``none``/``light``/``clean``, and ``{"rules": ...}`` for free-text
    formatting instructions. Each member is omitted when unset, because the
    contract refuses ``preset = ""`` and an empty part altogether; ``None``
    tells the caller to leave the field off the request entirely.

    A plain dict is returned rather than a ``DictateStyle`` on purpose: the SDK
    serialises a dict to exactly the JSON the hand-written client used to send,
    it keeps the ``sarvamai`` import out of a function the test-suite calls on a
    machine without the SDK, and the live-socket unit can hand the same dict
    straight to ``DictateConfigUpdate``, which validates dicts too.
    """
    payload: dict[str, object] = {}
    preset = (config.dictate_style or "").strip()
    if preset:
        payload["preset"] = preset
    rules = (config.dictate_rules or "").strip()
    if rules:
        payload["rules"] = rules
    return payload or None


def vocabulary_payload(config: Config) -> list | None:
    """Return the ``vocabulary`` value, or ``None`` when there are no terms.

    Shape from the API contract (§1 and §3): an array of objects, of which only
    ``word`` is required. Our configuration exposes the simple form — a list of
    the user's names and terms — so each string becomes ``{"word": s}``;
    ``sounds_like`` and ``description`` are left out rather than guessed.

    Plain dicts for the same three reasons as :func:`style_payload`.
    """
    entries = [
        {"word": term.strip()}
        for term in config.dictate_vocabulary
        if isinstance(term, str) and term.strip()
    ]
    return entries or None


def context_payload(config: Config) -> dict | None:
    """Return the ``context`` value, or ``None`` when both members are unset.

    Shape from the API contract (§1 and §3): ``{"text": ..., "reference": ...}``
    where ``text`` is one line about the situation and ``reference`` is a plain
    document carrying names and terms. Either member may be omitted; an empty
    string is not a legal value, so unset members are dropped rather than sent
    blank.

    A plain dict for the same three reasons as :func:`style_payload`.
    """
    payload: dict[str, object] = {}
    text = (config.dictate_context or "").strip()
    if text:
        payload["text"] = text
    reference = (config.dictate_reference or "").strip()
    if reference:
        payload["reference"] = reference
    return payload or None


class DictateTranscriber:
    """Transcribes one recorded clip through Sarvam's Dictate API.

    ``client`` may be injected (tests pass a fake exposing a
    ``dictate.transcribe(**kwargs)`` method); when it is, the SDK is never
    imported and no network call is ever made.
    """

    def __init__(self, config: Config, client=None) -> None:
        self._config = config
        self._client = client

    def transcribe(self, path: Path) -> Transcript:
        """Transcribe ``path``, one ``dictate.transcribe`` per <=28 s chunk.

        Every configuration mistake is reported before the file is opened and
        before any request is built, so the user sees "this setting cannot
        work" instead of the provider's HTTP 400.
        """
        # The mode check runs first, like the model check in the Saaras path:
        # it decides whether the request is sendable at all, and a mode Dictate
        # does not know would otherwise be rejected by the API as a 400.
        self._check_mode()

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
        chunks = self._chunks(path)
        try:
            if len(chunks) > 1:
                # split_wav made a fresh temp dir for us; we own removing it.
                created_dir = chunks[0].parent
            # Built once for the whole clip: the SDK client holds the HTTP
            # connection pool, so one per chunk would be wasted work.
            client = self._build_client()
            texts: list[str] = []
            language_code: str | None = None
            for chunk in chunks:
                text, chunk_language = self._transcribe_one(client, chunk)
                texts.append(text)
                if language_code is None and chunk_language:
                    # First non-empty answer wins: a clip is one utterance, and
                    # later chunks agreeing is the normal case anyway.
                    language_code = chunk_language
            return Transcript(
                text=join_transcripts(texts),
                language_code=language_code,
                segments=len(chunks),
            )
        finally:
            if created_dir is not None:
                shutil.rmtree(created_dir, ignore_errors=True)

    def _chunks(self, path: Path) -> list[Path]:
        """Return the requests to make for ``path``: itself, or its chunks.

        A recording that fits the clip limit is one request. Only audio that
        cannot go in one request is split, and then at
        :data:`~sarvam_dictation.transcriber.MAX_CHUNK_SECONDS` so every chunk
        keeps clear of the ceiling.
        """
        seconds = _wav_seconds(path)
        if 0.0 < seconds <= CLIP_MAX_SECONDS:
            return [path]
        # A file we could not measure is handed to the splitter anyway, so an
        # unreadable WAV still fails with the splitter's diagnostic ("cannot
        # read audio ...") instead of the provider's opaque 400.
        return split_wav(path, max_seconds=MAX_CHUNK_SECONDS)

    def _check_mode(self) -> None:
        """Refuse a mode Dictate has no equivalent for (e.g. ``translate``).

        ``config.py`` already rejects this pair, but the route re-checks so a
        ``Config`` built by hand — or by a future caller that forgets to
        validate — cannot put a mode the API does not know on the wire.
        """
        mode = self._config.mode
        if mode in DICTATE_MODES:
            return
        raise TranscriptionError(
            f"[api] mode {mode!r} is not a Dictate mode; Dictate accepts "
            f"{', '.join(DICTATE_MODES)}. Translation is a [dictate] rules "
            f"request, not a mode: put it in [dictate] rules, or choose one of "
            f"the modes above.",
            code="config_invalid",
        )

    def _build_client(self):
        """Return the injected client, or construct one with the lazily imported SDK."""
        if self._client is not None:
            return self._client
        try:
            from sarvamai import SarvamAI  # imported here so the SDK stays optional
        except ModuleNotFoundError as exc:
            raise TranscriptionError(
                "the sarvamai SDK is not installed (pip install 'sarvamai>=0.1.49'); "
                "the Dictate clip route needs it",
                code="internal",
            ) from exc
        try:
            return SarvamAI(api_subscription_key=self._config.api_key)
        except Exception as exc:  # SDK raises its own types on bad credentials
            raise TranscriptionError(
                f"cannot create the Sarvam client: {self._scrub(str(exc))}", code="api"
            ) from exc

    def _request_options(self) -> dict:
        """Per-request options: the route's own timeout, not the SDK's 60 s default."""
        return {"timeout_in_seconds": int(CLIP_TIMEOUT_S)}

    def _transcribe_one(self, client, chunk: Path) -> tuple[str, str | None]:
        """Transcribe a single chunk, mapping SDK failures onto our codes.

        Only the options the user actually set are passed. ``sarvamai`` 0.1.49
        does not strip a keyword that was explicitly ``None`` — it encodes it
        as ``null``/empty in the multipart body, which the API refuses with a
        400 — so "unset" has to mean "absent from the call", exactly as it
        meant "absent from the form" before.
        """
        kwargs: dict[str, object] = {
            "mode": self._config.mode,
            "request_options": self._request_options(),
        }
        if self._config.language_code:
            # Empty means "detect": omitting the option lets the API autodetect,
            # while sending "" would be refused outright.
            kwargs["language_code"] = self._config.language_code
        if self._config.dictate_output_rules.strip():
            kwargs["output_rules"] = self._config.dictate_output_rules
        if self._config.dictate_model.strip():
            kwargs["model"] = self._config.dictate_model
        for name, payload in (
            ("style", style_payload(self._config)),
            ("vocabulary", vocabulary_payload(self._config)),
            ("context", context_payload(self._config)),
        ):
            if payload is not None:
                # Dicts, not DictateStyle/DictateVocabularyEntry/DictateContext:
                # the SDK serialises them to the same JSON parts the contract
                # documents, and this module stays importable without the SDK.
                kwargs[name] = payload

        try:
            handle = open(chunk, "rb")
        except OSError as exc:
            raise TranscriptionError(
                f"cannot read audio chunk {chunk}: {exc}", code="internal"
            ) from exc
        with handle:
            try:
                response = client.dictate.transcribe(file=handle, **kwargs)
            except TranscriptionError:
                # Already ours and already actionable: mapping it again would
                # bury the message under "unexpected".
                raise
            except Exception as exc:  # SDK/transport errors are mapped, never leaked
                raise self._map_exception(exc) from exc

        return self._read_response(response)

    def _read_response(self, response) -> tuple[str, str | None]:
        """Read ``text`` and ``language_code`` off a successful response.

        ``text`` is the result: it is the finished dictation, and it is what the
        user pastes. ``transcript`` is only a fallback for a response that does
        not carry ``text`` at all, so an undocumented shape still yields
        something rather than nothing.

        ``degraded`` is deliberately not read: when it is set, ``text`` already
        holds the raw transcript, so the result is still usable text. An empty
        ``text`` is a legitimate answer for a clip of pure background noise and
        must not become an error. ``output`` is ignored — it is the summarised
        artefact, not what was said.
        """
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            fallback = getattr(response, "transcript", None)
            text = fallback if isinstance(fallback, str) else ""
        language_code = getattr(response, "language_code", None)
        if not isinstance(language_code, str) or not language_code:
            language_code = None
        return text, language_code

    def _map_exception(self, exc: Exception) -> TranscriptionError:
        """Convert an SDK or transport exception into a TranscriptionError.

        ``sarvamai`` raises ``sarvamai.core.api_error.ApiError`` (and its
        ``BadRequestError``/``ForbiddenError``/... subclasses) for every
        non-2xx answer: ``.status_code`` names the status and ``.body`` carries
        the documented ``{"error": {"message": ...}}`` object. Transport
        failures are not wrapped — the SDK lets ``httpx``'s own exceptions out —
        so the shared classifier in :mod:`transcriber` recognises both.
        """
        message = self._error_message(exc) or self._scrub(str(exc)) or exc.__class__.__name__
        status = _status_code(exc)
        if status is not None:
            return self._http_error(status, message)
        if _is_network_error(exc):
            return TranscriptionError(
                f"network error talking to Sarvam: {message}", code="network"
            )
        return TranscriptionError(
            f"unexpected error from the Dictate request ({exc.__class__.__name__}): {message}",
            code="internal",
        )

    def _error_message(self, exc: Exception) -> str:
        """Pull ``error.message`` out of an ``ApiError``'s body, scrubbed.

        Preferred over ``str(exc)`` because ``ApiError.__str__`` prints the
        whole response header dict, which is noise in a desktop notification.
        Returns ``""`` when the body does not follow the documented shape, so
        the caller falls back to the exception's own text.
        """
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message:
                    return self._scrub(message)
        return ""

    def _http_error(self, status: int, message: str) -> TranscriptionError:
        """Map an HTTP status onto the daemon's codes, keeping the status visible."""
        if status in (401, 403):
            return TranscriptionError(
                f"Sarvam rejected the API key (HTTP {status}): {message}", code="api"
            )
        if status == 429:
            return TranscriptionError(
                f"Sarvam rate limit reached (HTTP {status}): {message}", code="api"
            )
        return TranscriptionError(f"Sarvam API error (HTTP {status}): {message}", code="api")

    def _scrub(self, text: str) -> str:
        """Remove the API key from any message before it can be logged or sent."""
        key = self._config.api_key
        if key and len(key) >= 4:
            return text.replace(key, "***")
        return text


def _wav_seconds(path: Path) -> float:
    """Duration of a WAV file in seconds, or ``0.0`` when it cannot be read."""
    try:
        with wave.open(str(path), "rb") as reader:
            rate = reader.getframerate()
            if rate <= 0:
                return 0.0
            return reader.getnframes() / float(rate)
    except (wave.Error, EOFError, OSError):
        return 0.0

"""Sarvam **Dictate** for a recorded clip: one ``POST /v2/dictate`` per chunk.

This is the Dictate twin of :mod:`sarvam_dictation.transcriber`. Both answer
the same question — "here is a WAV, give me the text" — and both return a
:class:`~sarvam_dictation.transcriber.Transcript`, raise
:class:`~sarvam_dictation.transcriber.TranscriptionError` with a code from the
daemon's fixed vocabulary, and split long audio with the *same* splitter. What
differs is the service behind them: Saaras returns what recognition heard,
Dictate returns the text the speaker meant to type (punctuation, fillers
dropped, names spelled the user's way). The daemon therefore picks a class,
not a code path, and the rest of the daemon cannot tell the two apart.

The transport is ``httpx``, imported lazily inside :meth:`DictateTranscriber.
_post` so this module — and the whole test-suite — imports on a machine where
``httpx`` is not installed (brief rule 2). A missing ``httpx`` at runtime is an
``internal`` error with a ``pip install`` message, exactly like the missing
``sarvamai`` SDK in :mod:`transcriber`. An injected ``client`` bypasses the
transport entirely: tests hand in a fake and ``httpx`` is never imported, never
constructed and never called.

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
  usable text; turning that into an error would throw away a good result
  (brief section 6, decision 4).
* ``output`` is never typed. With ``output_rules`` set the API also returns a
  summarised ``output`` object; a dictation plugin types what was *said*
  (brief section 6, decision 3).

The clip route takes at most 30 s of audio, so a longer recording is split into
28 s chunks and each chunk is finished on its own. That is a real trade-off:
the finishing step sees less context per chunk, so a sentence that straddles a
boundary is punctuated twice. It is the only option — the API answers 413
above its limit — and it matches what the Saaras path already does, so the two
engines degrade in the same way.
"""

from __future__ import annotations

import json
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

#: The clip endpoint. No trailing slash: ``/v2/dictate/`` answers ``307`` to a
#: plain ``http://`` address, which would send the API key in the clear.
DICTATE_CLIP_URL = "https://api.sarvam.ai/v2/dictate"

#: Seconds to wait for one chunk. The contract allows the finishing step up to
#: 90 s on its own (and 90 s again for the ``output_rules`` call), so a 28 s
#: clip can legitimately sit on the wire for well over a minute. 120 s clears
#: that documented ceiling with headroom; anything tighter turns a slow-but-
#: successful dictation into a spurious ``network`` error and loses the audio.
CLIP_TIMEOUT_S = 120.0

#: The clip endpoint's own ceiling, in seconds. A recording that already fits is
#: sent whole: splitting a 29 s dictation into 28 s + 1 s would buy nothing and
#: pay for a second finishing call, so the splitter is reserved for audio that
#: genuinely cannot go in one request. The chunks themselves stay at
#: :data:`~sarvam_dictation.transcriber.MAX_CHUNK_SECONDS`, which leaves
#: headroom under this ceiling for the API's own rounding.
CLIP_MAX_SECONDS = 30.0


def style_payload(config: Config) -> dict | None:
    """Return the ``style`` form part, or ``None`` when the user set nothing.

    Shape from the API contract (§1 and §3): ``{"preset": ...}`` where ``preset``
    is one of ``none``/``light``/``clean``, and ``{"rules": ...}`` for free-text
    formatting instructions. Each member is omitted when unset, because the
    contract refuses ``preset = ""`` and an empty part altogether; ``None``
    tells the caller to leave the field off the request entirely.
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
    """Return the ``vocabulary`` form part, or ``None`` when there are no terms.

    Shape from the API contract (§1 and §3): an array of objects, of which only
    ``word`` is required. Our configuration exposes the simple form — a list of
    the user's names and terms — so each string becomes ``{"word": s}``;
    ``sounds_like`` and ``description`` are left out rather than guessed.
    """
    entries = [
        {"word": term.strip()}
        for term in config.dictate_vocabulary
        if isinstance(term, str) and term.strip()
    ]
    return entries or None


def context_payload(config: Config) -> dict | None:
    """Return the ``context`` form part, or ``None`` when both members are unset.

    Shape from the API contract (§1 and §3): ``{"text": ..., "reference": ...}``
    where ``text`` is one line about the situation and ``reference`` is a plain
    document carrying names and terms. Either member may be omitted; an empty
    string is not a legal value, so unset members are dropped rather than sent
    blank.
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

    ``client`` may be injected (tests pass a fake with a ``post`` method); when
    it is, ``httpx`` is never imported and no network call is ever made.
    """

    def __init__(self, config: Config, client=None) -> None:
        self._config = config
        self._client = client

    def transcribe(self, path: Path) -> Transcript:
        """Transcribe ``path``, one ``POST`` per <=28 s chunk.

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
            texts: list[str] = []
            language_code: str | None = None
            for chunk in chunks:
                text, chunk_language = self._transcribe_one(chunk)
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

    def _form_fields(self) -> dict[str, str]:
        """Build the non-file form parts, omitting every unset option.

        An empty string part is a 400 on this route (contract §1), so "unset"
        must mean "absent from the request", never "present but empty".
        """
        fields: dict[str, str] = {"mode": self._config.mode}
        if self._config.language_code:
            # Empty means "detect": omitting the part lets the API autodetect,
            # while sending "" would be refused outright.
            fields["language_code"] = self._config.language_code
        if self._config.dictate_output_rules.strip():
            fields["output_rules"] = self._config.dictate_output_rules
        if self._config.dictate_model.strip():
            fields["model"] = self._config.dictate_model
        for name, payload in (
            ("style", style_payload(self._config)),
            ("vocabulary", vocabulary_payload(self._config)),
            ("context", context_payload(self._config)),
        ):
            if payload is not None:
                # JSON text parts: the API parses these, so they must be
                # serialised strings, not form-encoded structures.
                fields[name] = json.dumps(payload, ensure_ascii=False)
        return fields

    def _transcribe_one(self, chunk: Path) -> tuple[str, str | None]:
        """Transcribe a single chunk, mapping transport failures onto our codes."""
        fields = self._form_fields()
        try:
            handle = open(chunk, "rb")
        except OSError as exc:
            raise TranscriptionError(
                f"cannot read audio chunk {chunk}: {exc}", code="internal"
            ) from exc
        with handle:
            files = {"file": (chunk.name, handle, "audio/wav")}
            try:
                response = self._post(files, fields)
            except TranscriptionError:
                # Already ours and already actionable (a missing ``httpx``, say):
                # mapping it again would bury the message under "unexpected".
                raise
            except Exception as exc:  # transport errors are mapped, never leaked
                raise self._map_exception(exc) from exc
        return self._read_response(response)

    def _post(self, files: dict, fields: dict[str, str]):
        """Send one chunk: through the injected client, else through ``httpx``."""
        headers = {"api-subscription-key": self._config.api_key}
        if self._client is not None:
            return self._client.post(
                DICTATE_CLIP_URL,
                headers=headers,
                data=fields,
                files=files,
                timeout=CLIP_TIMEOUT_S,
            )
        httpx = self._import_httpx()
        return httpx.post(
            DICTATE_CLIP_URL,
            headers=headers,
            data=fields,
            files=files,
            timeout=CLIP_TIMEOUT_S,
        )

    def _import_httpx(self):
        """Import ``httpx`` here so the module stays importable without it."""
        try:
            import httpx
        except ModuleNotFoundError as exc:
            raise TranscriptionError(
                "the httpx package is not installed (pip install 'httpx>=0.27'); "
                "the Dictate clip route needs it",
                code="internal",
            ) from exc
        return httpx

    def _read_response(self, response) -> tuple[str, str | None]:
        """Read ``text`` and ``language_code`` off a 200, defensively.

        ``degraded`` is deliberately not read: when it is set, ``text`` already
        holds the raw transcript, so the result is still usable text. A 200
        with an empty ``text`` is a legitimate answer for a clip of pure
        background noise and must not become an error.
        """
        status = getattr(response, "status_code", None)
        body = self._body(response)
        if isinstance(status, int) and not isinstance(status, bool) and status >= 400:
            raise self._http_error(status, self._error_message(body))
        if not body:
            # A 2xx whose body is not a JSON object is not an empty transcript:
            # it is a proxy page, a truncated response or a shape we do not
            # know. Reporting it as empty text would drop the user's audio
            # silently, which is the one outcome worth failing loudly for.
            raise TranscriptionError(
                f"Sarvam sent a response that was not readable JSON (HTTP {status})",
                code="api",
            )

        text = body.get("text")
        if not isinstance(text, str):
            # Undocumented shape: fall back to the raw transcript rather than
            # dropping a result the API did send.
            fallback = body.get("transcript")
            text = fallback if isinstance(fallback, str) else ""
        language_code = body.get("language_code")
        if not isinstance(language_code, str) or not language_code:
            language_code = None
        return text, language_code

    def _body(self, response) -> dict:
        """Return the JSON body as a dict, or ``{}`` when it is not one."""
        try:
            body = response.json()
        except Exception:  # a proxy's HTML error page, an empty body, ...
            return {}
        return body if isinstance(body, dict) else {}

    def _error_message(self, body: dict) -> str:
        """Pull ``error.message`` out of a documented error body."""
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return self._scrub(message)
        return ""

    def _map_exception(self, exc: Exception) -> TranscriptionError:
        """Convert a transport exception into a TranscriptionError."""
        message = self._scrub(str(exc)) or exc.__class__.__name__
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

"""Configuration loading and validation for the dictation daemon.

Layout is frozen by brief section 5; every value has a built-in default so a
missing config file is never an error.

Brief 2 adds one thing here and nothing else: a :class:`Config` remembers the
path it was loaded from, so a running daemon can re-read *the same file* when
the addon asks it to reload (brief section 6) without the caller having to
remember the path a second time.

Brief 7 adds ``[api] streaming``: transcribe through Sarvam's realtime
WebSocket API while the user is still speaking. ``saaras:v3-realtime`` has no
REST equivalent, so naming it forces streaming on; asking for streaming with a
model that has no realtime support is rejected outright rather than silently
falling back to REST.

Brief 8 adds :data:`REST_MODELS` beside :data:`STREAMING_MODELS`, so both
endpoints' model namespaces live in this one module. The SDK exposes three
clients with three disjoint model lists (REST, legacy streaming and realtime);
``saaras:v3-realtime`` is a realtime-only name and is deliberately absent from
:data:`REST_MODELS`, and ``saaras:v4`` is the one model every endpoint accepts.
The daemon checks the configured model against the endpoint it is about to use
rather than letting the provider answer with an HTTP 400.

Brief 15 splits the single ``[api] streaming`` switch into one switch per
dictation mode: ``[api] streaming_push_to_talk`` for the hold-the-key shape
(``start``/``stop``) and ``[api] streaming_toggle`` for the record-and-send
shape (``toggle``). Precedence per mode is the per-mode key, then the legacy
``[api] streaming`` key, then false, so an existing ``config.toml`` that says
``streaming = true`` keeps streaming *both* modes instead of silently dropping
the user to batch. The realtime-only model still forces both modes on, and the
model check is applied per mode so the error message can name the mode that is
misconfigured.

The Dictate brief adds the engine switch. ``[api] engine`` chooses between
:data:`ENGINE_SAARAS` - the Speech-to-Text API above, and still the default,
so an untouched ``config.toml`` keeps today's behaviour byte for byte - and
:data:`ENGINE_DICTATE`, which asks Sarvam for the text the user *meant to
type*: punctuated, fillers dropped, names spelled the way the user's own
vocabulary says. The two routes are the same shape (a clip endpoint and a
live socket), which is why the daemon gets a switch instead of a second
daemon.

Dictate's own settings live in a ``[dictate]`` table rather than beside the
saaras keys in ``[api]`` for two reasons. They are meaningful to exactly one
of the two routes, and every one of them has an "unset" spelling (``""`` or
``[]``) that means *do not send this field at all*; mixing those with the
saaras keys, where an empty string is a real value (``language_code = ""``
means detect), would leave the reader guessing which spelling means what.
The table is still read and validated under ``engine = "saaras"``, because an
invalid value there is a typo the user wants to hear about *before* switching
engines. What is gated on the engine is the pair of saaras-only cross-checks:
streaming needs a realtime model, and a ``saaras:v3-realtime`` model forces
both streaming modes on. Under Dictate, ``[api] model`` names a Dictate
snapshot that the Saaras realtime endpoint has never heard of, so either rule
would refuse a perfectly good Dictate configuration. Dictate also has no
``keyterms`` at all - its ``vocabulary`` replaces them - so ``[api] keyterms``
is left exactly as it was and neither engine is asked to justify it here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 needs `tomli`
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_MODEL = "saaras:v4"
DEFAULT_MODE = "transcribe"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_BACKEND = "auto"
DEFAULT_MAX_DURATION_S = 300

#: The realtime model (brief 7). It exists only on the WebSocket endpoint, so
#: choosing it implies streaming on for *both* dictation modes (brief 15).
REALTIME_MODEL = "saaras:v3-realtime"

#: The ``[api]`` key that enables streaming for each dictation mode, in the
#: order the daemon and ``--check`` report them (brief 15). The key name is
#: also the mode name every error message uses.
STREAMING_PUSH_TO_TALK_KEY = "streaming_push_to_talk"
STREAMING_TOGGLE_KEY = "streaming_toggle"
STREAMING_KEYS = (STREAMING_PUSH_TO_TALK_KEY, STREAMING_TOGGLE_KEY)

#: The legacy single switch (brief 7). It still applies to both modes when a
#: per-mode key is absent, so an old ``config.toml`` keeps working.
LEGACY_STREAMING_KEY = "streaming"

#: The models the realtime endpoint serves, and therefore the only models that
#: may be combined with any of the streaming switches being on.
STREAMING_MODELS = (REALTIME_MODEL, "saaras:v4")

#: Models the REST endpoint POST /speech-to-text accepts. saaras:v3-realtime is
#: deliberately absent: it exists only on the realtime WebSocket endpoint.
REST_MODELS = ("saarika:v2.5", "saaras:v3", "saaras:v4")

VALID_MODES = ("transcribe", "translate", "verbatim", "translit", "codemix")
VALID_BACKENDS = ("auto", "pipewire", "pulse", "alsa")

MAX_KEYTERMS = 50
MAX_KEYTERM_LENGTH = 64

#: The engine switch. ``saaras`` is the Speech-to-Text API the daemon has
#: always used and stays the default; ``dictate`` is the Dictate API, which
#: returns the text the user meant to type instead of a raw transcript.
DEFAULT_ENGINE = "saaras"
ENGINE_SAARAS = "saaras"
ENGINE_DICTATE = "dictate"
VALID_ENGINES = (ENGINE_SAARAS, ENGINE_DICTATE)

#: Dictate's socket paces audio differently per stream type: ``fast`` takes
#: 0.5 s messages, the other two 1 s. ``balanced`` is the provider's default.
DEFAULT_DICTATE_STREAM_TYPE = "balanced"
VALID_DICTATE_STREAM_TYPES = ("fast", "balanced", "simulated")

#: The modes Dictate accepts. It has *no* ``translate`` mode - translation is
#: asked for in ``[dictate] rules`` - which is why the shared ``[api] mode``
#: needs a second, engine-specific check.
DICTATE_MODES = ("transcribe", "verbatim", "translit", "codemix")

#: The style presets Dictate offers. ``""`` is not one of them: it means "send
#: no style at all" and lets the provider pick its per-mode default.
VALID_DICTATE_STYLE_PRESETS = ("none", "light", "clean")

#: The API's documented length limits. Refusing an over-long value here turns
#: a mid-dictation HTTP 400 into a configuration error at load time.
MAX_DICTATE_RULES = 2000
MAX_DICTATE_CONTEXT = 2000
MAX_DICTATE_REFERENCE = 50000
MAX_DICTATE_OUTPUT_RULES = 4000
MAX_DICTATE_VOCABULARY = 1000
MAX_DICTATE_VOCABULARY_WORD = 64

#: What an empty ``[api] language_code`` becomes on the Dictate socket. Unlike
#: Saaras, ``auto`` is a *valid* value there rather than a spelling to strip,
#: so the payload builders send this instead of omitting the field.
DEFAULT_DICTATE_LANGUAGE = "auto"

#: The only sample rates the Dictate socket accepts. Anything else is refused
#: at load time rather than by the socket after the user has spoken.
DICTATE_SAMPLE_RATES = (16000, 8000)

API_KEY_ENV = "SARVAM_API_KEY"
APP_DIR_NAME = "sarvam-dictation"
SOCKET_NAME = "sarvam-dictation.sock"


class ConfigError(Exception):
    """Raised for a malformed or semantically invalid configuration."""


@dataclass(frozen=True)
class Config:
    """Immutable, fully validated daemon configuration."""

    api_key: str = ""
    model: str = DEFAULT_MODEL
    mode: str = DEFAULT_MODE
    language_code: str = ""
    keyterms: tuple[str, ...] = ()
    audio_device: str = ""
    sample_rate: int = DEFAULT_SAMPLE_RATE
    backend: str = DEFAULT_BACKEND
    socket_path: str = ""
    max_duration_s: int = DEFAULT_MAX_DURATION_S
    #: Transcribe while recording through the realtime WebSocket API instead of
    #: sending one WAV to the REST endpoint at the end, chosen independently for
    #: each dictation mode (brief 15). ``streaming_push_to_talk`` covers the
    #: hold-the-key shape (``start``/``stop``), ``streaming_toggle`` the
    #: record-and-send shape (``toggle``). Both default to off: the REST path is
    #: the default and needs no realtime subscription.
    streaming_push_to_talk: bool = False
    streaming_toggle: bool = False
    #: The file this configuration was read from; ``""`` when it was built by
    #: hand. It takes no part in equality, hashing or ``repr``, so every
    #: ``Config(...)`` comparison written before brief 2 keeps working.
    config_path: str = field(default="", compare=False, repr=False)
    #: Which API the daemon talks to (the Dictate brief). ``"saaras"`` is the
    #: Speech-to-Text API and the default, so a file that never mentions the
    #: engine behaves exactly as it did before the switch existed.
    engine: str = DEFAULT_ENGINE
    #: The ``[dictate]`` table. Each setting has an "unset" spelling - ``""``
    #: or ``()`` - meaning "do not send this field at all", which is why the
    #: defaults are empty rather than a guessed preset or word list.
    dictate_style: str = ""
    dictate_rules: str = ""
    dictate_vocabulary: tuple[str, ...] = ()
    dictate_context: str = ""
    dictate_reference: str = ""
    dictate_output_rules: str = ""
    #: Pin a Dictate snapshot (e.g. ``dictate-v1-2026-11-04``); ``""`` sends
    #: nothing and lets the provider serve its current default.
    dictate_model: str = ""
    dictate_stream_type: str = DEFAULT_DICTATE_STREAM_TYPE


def default_config_path() -> Path:
    """Return ``$XDG_CONFIG_HOME/sarvam-dictation/config.toml`` (or ``~/.config``)."""
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return base / APP_DIR_NAME / "config.toml"


def default_socket_path() -> str:
    """Return the socket path both the daemon and the addon default to.

    ``$XDG_RUNTIME_DIR`` when set and non-empty, else ``/tmp/sarvam-dictation-<uid>.sock``.
    The C++ side reads ``getenv("XDG_RUNTIME_DIR")`` directly so the two agree
    byte for byte.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        return str(Path(runtime_dir) / SOCKET_NAME)
    return f"/tmp/sarvam-dictation-{os.getuid()}.sock"


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Load and validate the configuration.

    ``path`` defaults to :func:`default_config_path`; a missing file is fine and
    leaves every built-in default in place. ``env`` defaults to ``os.environ``
    and is only consulted for ``SARVAM_API_KEY``, which fills an *empty*
    ``api.key`` and never overrides one set in the file.
    """
    environment: Mapping[str, str] = os.environ if env is None else env
    config_path = Path(path) if path is not None else default_config_path()

    document = _read_toml(config_path)
    api = _table(document, "api")
    audio = _table(document, "audio")
    server = _table(document, "server")
    # Read and validated even under the saaras engine: a typo in [dictate] is
    # worth reporting before the user switches engines and hits it mid-way
    # through a dictation (see the module docstring).
    dictate = _table(document, "dictate")

    engine = _string(api, "engine", DEFAULT_ENGINE, "[api] engine")
    if engine not in VALID_ENGINES:
        raise ConfigError(
            f"[api] engine {engine!r} is not one of {', '.join(VALID_ENGINES)}"
        )

    model = _string(api, "model", DEFAULT_MODEL, "[api] model")
    if not model:
        raise ConfigError("[api] model must not be empty")

    mode = _string(api, "mode", DEFAULT_MODE, "[api] mode")
    if mode not in VALID_MODES:
        raise ConfigError(f"[api] mode {mode!r} is not one of {', '.join(VALID_MODES)}")

    # ``[api] mode`` is shared by both engines, but Dictate's enum is the
    # narrower one: it has no ``translate``. The message names the value and
    # points at the key that does offer translation, so the user is not left
    # to guess where the capability went.
    if engine == ENGINE_DICTATE and mode not in DICTATE_MODES:
        raise ConfigError(
            f"[api] mode {mode!r} is not one of {', '.join(DICTATE_MODES)} under "
            f"[api] engine {ENGINE_DICTATE!r}; ask for translation in [dictate] rules"
        )

    # Streaming is opt-in per dictation mode (brief 15), with two rules carried
    # over from brief 7: the realtime model has no REST equivalent so it forces
    # streaming on for both modes, and streaming with a model the realtime
    # endpoint does not serve is a configuration error - naming the mode that
    # asked for it - rather than a silent downgrade to REST.
    legacy_streaming = _boolean(api, LEGACY_STREAMING_KEY, False, "[api] streaming")
    streaming_push_to_talk = _streaming_flag(api, STREAMING_PUSH_TO_TALK_KEY, legacy_streaming)
    streaming_toggle = _streaming_flag(api, STREAMING_TOGGLE_KEY, legacy_streaming)
    # Both rules below are saaras-only. Under the dictate engine ``[api] model``
    # names a Dictate snapshot, which the Saaras realtime endpoint does not
    # serve, so applying them would refuse a valid Dictate configuration; the
    # switches are still type-checked above, because a non-boolean there is a
    # mistake in either engine.
    if engine == ENGINE_SAARAS:
        if model == REALTIME_MODEL:
            streaming_push_to_talk = True
            streaming_toggle = True
        else:
            for key, enabled in (
                (STREAMING_PUSH_TO_TALK_KEY, streaming_push_to_talk),
                (STREAMING_TOGGLE_KEY, streaming_toggle),
            ):
                if enabled and model not in STREAMING_MODELS:
                    raise ConfigError(
                        f"[api] {key} requires model {REALTIME_MODEL!r} or "
                        f"{STREAMING_MODELS[1]!r}, not {model!r}"
                    )

    # An empty language code means "detect it". fcitx5-configtool writes that as
    # the empty string, but a hand-edited file may say "auto" (the config page's
    # own wording); accepting both keeps the two spellings interchangeable and
    # stops the literal string "auto" from being sent to the API as a tag.
    language_code = _string(api, "language_code", "", "[api] language_code").strip()
    if language_code.lower() in ("auto", "autodetect", "auto-detect"):
        language_code = ""
    keyterms = _keyterms(api)

    # The [dictate] table. Every key has an "unset" spelling - "" or [] - so an
    # absent table is a valid Dictate configuration that sends nothing beyond
    # the defaults the API already applies.
    dictate_style = _string(dictate, "style", "", "[dictate] style")
    if dictate_style and dictate_style not in VALID_DICTATE_STYLE_PRESETS:
        raise ConfigError(
            f"[dictate] style {dictate_style!r} is not one of "
            f"{', '.join(VALID_DICTATE_STYLE_PRESETS)}"
        )
    dictate_rules = _bounded_text(dictate, "rules", MAX_DICTATE_RULES, "[dictate] rules")
    dictate_vocabulary = _vocabulary(dictate)
    dictate_context = _bounded_text(
        dictate, "context", MAX_DICTATE_CONTEXT, "[dictate] context"
    )
    dictate_reference = _bounded_text(
        dictate, "reference", MAX_DICTATE_REFERENCE, "[dictate] reference"
    )
    dictate_output_rules = _bounded_text(
        dictate, "output_rules", MAX_DICTATE_OUTPUT_RULES, "[dictate] output_rules"
    )
    dictate_model = _string(dictate, "model", "", "[dictate] model").strip()
    dictate_stream_type = _string(
        dictate, "stream_type", DEFAULT_DICTATE_STREAM_TYPE, "[dictate] stream_type"
    )
    if dictate_stream_type not in VALID_DICTATE_STREAM_TYPES:
        raise ConfigError(
            f"[dictate] stream_type {dictate_stream_type!r} is not one of "
            f"{', '.join(VALID_DICTATE_STREAM_TYPES)}"
        )

    file_key = _string(api, "key", "", "[api] key").strip()
    env_key = (environment.get(API_KEY_ENV) or "").strip()
    api_key = file_key if file_key else env_key

    backend = _string(audio, "backend", DEFAULT_BACKEND, "[audio] backend")
    if backend not in VALID_BACKENDS:
        raise ConfigError(f"[audio] backend {backend!r} is not one of {', '.join(VALID_BACKENDS)}")

    sample_rate = _integer(audio, "sample_rate", DEFAULT_SAMPLE_RATE, "[audio] sample_rate")
    if sample_rate <= 0:
        raise ConfigError("[audio] sample_rate must be a positive integer")

    # The Dictate socket accepts two rates and refuses the rest, so a rate that
    # is fine on the saaras REST path (22.05 kHz, say) is refused here while
    # the user is still looking at their config file.
    if engine == ENGINE_DICTATE and sample_rate not in DICTATE_SAMPLE_RATES:
        raise ConfigError(
            f"[audio] sample_rate {sample_rate} is not one of "
            f"{', '.join(str(rate) for rate in DICTATE_SAMPLE_RATES)} under "
            f"[api] engine {ENGINE_DICTATE!r}"
        )

    audio_device = _string(audio, "device", "", "[audio] device")

    socket_path = _string(server, "socket_path", "", "[server] socket_path")
    max_duration_s = _integer(server, "max_duration_s", DEFAULT_MAX_DURATION_S, "[server] max_duration_s")
    if max_duration_s <= 0:
        raise ConfigError("[server] max_duration_s must be a positive integer")

    return Config(
        api_key=api_key,
        model=model,
        mode=mode,
        language_code=language_code,
        keyterms=keyterms,
        audio_device=audio_device,
        sample_rate=sample_rate,
        backend=backend,
        socket_path=socket_path,
        max_duration_s=max_duration_s,
        streaming_push_to_talk=streaming_push_to_talk,
        streaming_toggle=streaming_toggle,
        config_path=str(config_path),
        engine=engine,
        dictate_style=dictate_style,
        dictate_rules=dictate_rules,
        dictate_vocabulary=dictate_vocabulary,
        dictate_context=dictate_context,
        dictate_reference=dictate_reference,
        dictate_output_rules=dictate_output_rules,
        dictate_model=dictate_model,
        dictate_stream_type=dictate_stream_type,
    )


def reload_config(path: Path | str | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Re-read the configuration file for the daemon's ``reload`` command.

    Brief section 6 semantics, all of which fall out of :func:`load_config`:

    * a missing file yields a :class:`Config` built from the built-in defaults
      (exactly what happens at startup), so it is a success, not an error;
    * a malformed file or an invalid value raises :class:`ConfigError` whose
      message names the offending key (``[api] mode ...``), and the caller
      keeps its previous settings;
    * ``SARVAM_API_KEY`` still fills an empty ``[api] key``, so a reload
      behaves the same as a fresh start.

    ``path`` defaults to the built-in XDG location; pass the path the daemon
    was started with (``Config.config_path``) to follow a ``--config``
    override.
    """
    return load_config(None if path is None else Path(path), env)


def _read_toml(path: Path) -> dict[str, Any]:
    """Read ``path`` as TOML; a missing file yields an empty document."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except IsADirectoryError as exc:
        raise ConfigError(f"config path {path} is a directory") from exc
    except PermissionError as exc:
        raise ConfigError(f"config file {path} is not readable: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc


def _table(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Return sub-table ``name``, treating absence as an empty table."""
    value = document.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _string(section: Mapping[str, Any], key: str, default: str, label: str) -> str:
    """Fetch a string option, rejecting wrong types."""
    value = section.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string, got {type(value).__name__}")
    return value


def _integer(section: Mapping[str, Any], key: str, default: int, label: str) -> int:
    """Fetch an integer option, rejecting wrong types (and booleans)."""
    value = section.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label} must be an integer, got {type(value).__name__}")
    return value


def _boolean(section: Mapping[str, Any], key: str, default: bool, label: str) -> bool:
    """Fetch a boolean option, rejecting wrong types (and integers)."""
    value = section.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean, got {type(value).__name__}")
    return value


def _streaming_flag(api: Mapping[str, Any], key: str, legacy: bool) -> bool:
    """Read one per-mode streaming switch, falling back to the legacy key.

    Precedence is the per-mode key when the file sets it, else the legacy
    ``[api] streaming`` value, else false. ``legacy`` is already resolved by
    the caller, so an absent per-mode key simply inherits it; a per-mode key
    that is present always wins, in both directions.
    """
    if key in api:
        return _boolean(api, key, legacy, f"[api] {key}")
    return legacy


def _bounded_text(section: Mapping[str, Any], key: str, limit: int, label: str) -> str:
    """Fetch an optional free-text option and enforce the API's length limit.

    The API answers an over-long ``rules``/``context``/``reference``/
    ``output_rules`` with a 400 that the user only sees after speaking, so the
    limit is enforced here where the message can name the key and the count.
    """
    value = _string(section, key, "", label)
    if len(value) > limit:
        raise ConfigError(
            f"{label} is {len(value)} characters, the API limit is {limit}"
        )
    return value


def _vocabulary(dictate: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate ``[dictate] vocabulary`` (<=1000 words of 1..64 chars).

    Dictate's ``vocabulary`` replaces saaras' ``keyterms``, so this mirrors
    :func:`_keyterms` rather than sharing it: the limits differ, an empty word
    is refused here (the API requires 1..64 characters), and the words are sent
    one per ``{"word": ...}`` object by the payload builders.
    """
    value = dictate.get("vocabulary", ())
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConfigError("[dictate] vocabulary must be an array of strings")
    words: list[str] = []
    for word in value:
        if not isinstance(word, str):
            raise ConfigError("[dictate] vocabulary must contain only strings")
        if not word:
            raise ConfigError("[dictate] vocabulary must not contain an empty word")
        if len(word) > MAX_DICTATE_VOCABULARY_WORD:
            raise ConfigError(
                f"[dictate] vocabulary word {word[:16]!r}... is longer than "
                f"{MAX_DICTATE_VOCABULARY_WORD} characters"
            )
        words.append(word)
    if len(words) > MAX_DICTATE_VOCABULARY:
        raise ConfigError(
            f"[dictate] vocabulary may hold at most {MAX_DICTATE_VOCABULARY} words"
        )
    return tuple(words)


def _keyterms(api: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate ``[api] keyterms`` (saaras:v4 only, <=50 entries of <=64 chars)."""
    value = api.get("keyterms", ())
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConfigError("[api] keyterms must be an array of strings")
    terms: list[str] = []
    for term in value:
        if not isinstance(term, str):
            raise ConfigError("[api] keyterms must contain only strings")
        if len(term) > MAX_KEYTERM_LENGTH:
            raise ConfigError(
                f"[api] keyterm {term[:16]!r}... is longer than {MAX_KEYTERM_LENGTH} characters"
            )
        terms.append(term)
    if len(terms) > MAX_KEYTERMS:
        raise ConfigError(f"[api] keyterms may hold at most {MAX_KEYTERMS} entries")
    return tuple(terms)

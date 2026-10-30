"""Configuration loading and validation for the dictation daemon.

Layout is frozen by brief section 5; every value has a built-in default so a
missing config file is never an error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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

VALID_MODES = ("transcribe", "translate", "verbatim", "translit", "codemix")
VALID_BACKENDS = ("auto", "pipewire", "pulse", "alsa")

MAX_KEYTERMS = 50
MAX_KEYTERM_LENGTH = 64

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

    model = _string(api, "model", DEFAULT_MODEL, "[api] model")
    if not model:
        raise ConfigError("[api] model must not be empty")

    mode = _string(api, "mode", DEFAULT_MODE, "[api] mode")
    if mode not in VALID_MODES:
        raise ConfigError(f"[api] mode {mode!r} is not one of {', '.join(VALID_MODES)}")

    language_code = _string(api, "language_code", "", "[api] language_code")
    keyterms = _keyterms(api)

    file_key = _string(api, "key", "", "[api] key").strip()
    env_key = (environment.get(API_KEY_ENV) or "").strip()
    api_key = file_key if file_key else env_key

    backend = _string(audio, "backend", DEFAULT_BACKEND, "[audio] backend")
    if backend not in VALID_BACKENDS:
        raise ConfigError(f"[audio] backend {backend!r} is not one of {', '.join(VALID_BACKENDS)}")

    sample_rate = _integer(audio, "sample_rate", DEFAULT_SAMPLE_RATE, "[audio] sample_rate")
    if sample_rate <= 0:
        raise ConfigError("[audio] sample_rate must be a positive integer")

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
    )


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

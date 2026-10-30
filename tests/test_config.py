"""Configuration tests: brief §5, exercised through ``sarvam_dictation.config``.

Covers defaults with no file, a fully populated TOML file, every documented
validation error, malformed TOML, the "env fills in but never overrides" rule
for the API key, forward-compatible unknown keys, and the §4 socket-path
resolution rule.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from sarvam_dictation.config import (
    DEFAULT_MODEL,
    DEFAULT_MODE,
    DEFAULT_SAMPLE_RATE,
    VALID_BACKENDS,
    VALID_MODES,
    Config,
    ConfigError,
    default_config_path,
    default_socket_path,
    load_config,
)


def write_config(tmp_path: Path, text: str) -> Path:
    """Write ``text`` to ``tmp_path/config.toml`` and return the path."""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


FULL_TOML = """
[api]
key = "file-api-key-not-real"
model = "saaras:v3"
mode = "translate"
language_code = "hi-IN"
keyterms = ["Sarvam", "सर्वम"]

[audio]
device = "hw:1,0"
sample_rate = 8000
backend = "alsa"

[server]
socket_path = "/tmp/custom-dictation.sock"
max_duration_s = 42
"""


# --------------------------------------------------------------------------
# constants and defaults
# --------------------------------------------------------------------------


def test_module_constants_match_the_brief():
    assert DEFAULT_MODEL == "saaras:v4"
    assert DEFAULT_MODE == "transcribe"
    assert DEFAULT_SAMPLE_RATE == 16000
    assert VALID_MODES == ("transcribe", "translate", "verbatim", "translit", "codemix")
    assert VALID_BACKENDS == ("auto", "pipewire", "pulse", "alsa")


def test_config_error_is_an_exception():
    assert issubclass(ConfigError, Exception)


def test_missing_file_yields_built_in_defaults(tmp_path):
    config = load_config(path=tmp_path / "does-not-exist.toml", env={})
    assert config == Config()
    assert config.api_key == ""
    assert config.model == "saaras:v4"
    assert config.mode == "transcribe"
    assert config.language_code == ""
    assert config.keyterms == ()
    assert config.audio_device == ""
    assert config.sample_rate == 16000
    assert config.backend == "auto"
    assert config.socket_path == ""
    assert config.max_duration_s == 300


def test_config_is_frozen():
    config = Config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.model = "saaras:v3"


def test_config_keyterms_default_to_an_empty_tuple():
    assert isinstance(Config().keyterms, tuple)
    assert Config().keyterms == ()


# --------------------------------------------------------------------------
# a fully populated file
# --------------------------------------------------------------------------


def test_full_toml_file_is_parsed_into_every_field(tmp_path):
    config = load_config(path=write_config(tmp_path, FULL_TOML), env={})
    assert config.api_key == "file-api-key-not-real"
    assert config.model == "saaras:v3"
    assert config.mode == "translate"
    assert config.language_code == "hi-IN"
    assert config.keyterms == ("Sarvam", "सर्वम")
    assert isinstance(config.keyterms, tuple)
    assert config.audio_device == "hw:1,0"
    assert config.sample_rate == 8000
    assert config.backend == "alsa"
    assert config.socket_path == "/tmp/custom-dictation.sock"
    assert config.max_duration_s == 42


def test_partial_file_keeps_defaults_for_missing_sections(tmp_path):
    path = write_config(tmp_path, '[audio]\nsample_rate = 22050\n')
    config = load_config(path=path, env={})
    assert config.sample_rate == 22050
    assert config.model == "saaras:v4"
    assert config.backend == "auto"
    assert config.max_duration_s == 300


def test_empty_file_yields_defaults(tmp_path):
    config = load_config(path=write_config(tmp_path, ""), env={})
    assert config == Config()


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def test_unknown_mode_is_rejected(tmp_path):
    path = write_config(tmp_path, '[api]\nmode = "shout"\n')
    with pytest.raises(ConfigError):
        load_config(path=path, env={})


def test_unknown_backend_is_rejected(tmp_path):
    path = write_config(tmp_path, '[audio]\nbackend = "jack"\n')
    with pytest.raises(ConfigError):
        load_config(path=path, env={})


@pytest.mark.parametrize("sample_rate", [0, -1, -16000])
def test_non_positive_sample_rate_is_rejected(tmp_path, sample_rate):
    path = write_config(tmp_path, f"[audio]\nsample_rate = {sample_rate}\n")
    with pytest.raises(ConfigError):
        load_config(path=path, env={})


@pytest.mark.parametrize("max_duration_s", [0, -5])
def test_non_positive_max_duration_is_rejected(tmp_path, max_duration_s):
    path = write_config(tmp_path, f"[server]\nmax_duration_s = {max_duration_s}\n")
    with pytest.raises(ConfigError):
        load_config(path=path, env={})


def test_fifty_keyterms_are_accepted(tmp_path):
    terms = [f"term{i}" for i in range(50)]
    path = write_config(tmp_path, f"[api]\nkeyterms = {json.dumps(terms)}\n")
    config = load_config(path=path, env={})
    assert len(config.keyterms) == 50


def test_fifty_one_keyterms_are_rejected(tmp_path):
    terms = [f"term{i}" for i in range(51)]
    path = write_config(tmp_path, f"[api]\nkeyterms = {json.dumps(terms)}\n")
    with pytest.raises(ConfigError):
        load_config(path=path, env={})


def test_sixty_four_char_keyterm_is_accepted(tmp_path):
    path = write_config(tmp_path, f'[api]\nkeyterms = {json.dumps(["x" * 64])}\n')
    config = load_config(path=path, env={})
    assert config.keyterms == ("x" * 64,)


def test_sixty_five_char_keyterm_is_rejected(tmp_path):
    path = write_config(tmp_path, f'[api]\nkeyterms = {json.dumps(["x" * 65])}\n')
    with pytest.raises(ConfigError):
        load_config(path=path, env={})


@pytest.mark.parametrize(
    "text",
    [
        "[api\nkey = 1\n",
        "[api]\nkey = \n",
        "key = \n",
        "not toml at all ===",
    ],
)
def test_malformed_toml_is_reported_as_config_error(tmp_path, text):
    path = write_config(tmp_path, text)
    with pytest.raises(ConfigError):
        load_config(path=path, env={})


def test_unknown_keys_are_ignored(tmp_path):
    text = """
bogus_top_level = true

[api]
key = "k"
future_option = "whatever"

[audio]
another_future_option = 3

[experimental]
enabled = true
"""
    config = load_config(path=write_config(tmp_path, text), env={})
    assert config.api_key == "k"
    assert config.model == "saaras:v4"


# --------------------------------------------------------------------------
# environment precedence
# --------------------------------------------------------------------------


def test_env_fills_an_empty_api_key(tmp_path):
    path = write_config(tmp_path, '[api]\nkey = ""\n')
    config = load_config(path=path, env={"SARVAM_API_KEY": "env-api-key-not-real"})
    assert config.api_key == "env-api-key-not-real"


def test_env_fills_a_missing_api_key_section(tmp_path):
    config = load_config(path=write_config(tmp_path, ""), env={"SARVAM_API_KEY": "env-key"})
    assert config.api_key == "env-key"


def test_env_does_not_override_a_non_empty_file_key(tmp_path):
    path = write_config(tmp_path, '[api]\nkey = "file-key"\n')
    config = load_config(path=path, env={"SARVAM_API_KEY": "env-key"})
    assert config.api_key == "file-key"


def test_env_absent_leaves_the_key_empty(tmp_path):
    config = load_config(path=write_config(tmp_path, ""), env={})
    assert config.api_key == ""


def test_load_config_reads_os_environ_when_env_is_not_given(tmp_path, monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "from-os-environ")
    config = load_config(path=write_config(tmp_path, ""))
    assert config.api_key == "from-os-environ"


# --------------------------------------------------------------------------
# path resolution (brief §4 and §5)
# --------------------------------------------------------------------------


def test_default_config_path_honours_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert default_config_path() == Path(tmp_path / "xdg") / "sarvam-dictation" / "config.toml"


def test_default_config_path_falls_back_to_home_config(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert default_config_path() == Path(tmp_path / "home") / ".config" / "sarvam-dictation" / "config.toml"


def test_default_socket_path_uses_xdg_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert default_socket_path() == str(Path(tmp_path / "run") / "sarvam-dictation.sock")


def test_default_socket_path_falls_back_to_tmp_when_xdg_runtime_dir_is_unset(monkeypatch, uid):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_socket_path() == f"/tmp/sarvam-dictation-{uid}.sock"


def test_default_socket_path_falls_back_to_tmp_when_xdg_runtime_dir_is_empty(monkeypatch, uid):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    assert default_socket_path() == f"/tmp/sarvam-dictation-{uid}.sock"


def test_load_config_reads_the_default_path_when_none_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    target = tmp_path / "xdg" / "sarvam-dictation" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text('[api]\nmodel = "saaras:v3"\n', encoding="utf-8")
    assert load_config(env={}).model == "saaras:v3"


def test_load_config_tolerates_a_missing_default_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nowhere"))
    assert load_config(env={}) == Config()

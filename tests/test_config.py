"""Configuration tests: brief §5, exercised through ``sarvam_dictation.config``.

Covers defaults with no file, a fully populated TOML file, every documented
validation error, malformed TOML, the "env fills in but never overrides" rule
for the API key, forward-compatible unknown keys, and the §4 socket-path
resolution rule.

The last section covers the streaming switches. Brief 7 introduced the single
``[api] streaming`` flag; brief 15 replaced it with one switch per dictation
mode, ``[api] streaming_push_to_talk`` and ``[api] streaming_toggle``, both off
by default, both forced on by ``saaras:v3-realtime``, both rejected with a
model the realtime endpoint does not serve, and both still inheriting the
legacy ``[api] streaming`` key when their own key is absent.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import sarvam_dictation.__main__ as daemon_main
from sarvam_dictation.config import (
    DEFAULT_MODEL,
    DEFAULT_MODE,
    DEFAULT_SAMPLE_RATE,
    LEGACY_STREAMING_KEY,
    REALTIME_MODEL,
    REST_MODELS,
    STREAMING_KEYS,
    STREAMING_MODELS,
    STREAMING_PUSH_TO_TALK_KEY,
    STREAMING_TOGGLE_KEY,
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


# --------------------------------------------------------------------------
# streaming, one switch per dictation mode (brief 15)
# --------------------------------------------------------------------------


def test_streaming_defaults_to_off_for_both_modes(tmp_path):
    config = load_config(path=tmp_path / "does-not-exist.toml", env={})
    assert Config().streaming_push_to_talk is False
    assert Config().streaming_toggle is False
    assert config.streaming_push_to_talk is False
    assert config.streaming_toggle is False


def test_absent_streaming_keys_mean_batch_for_both_modes(tmp_path):
    # Nothing in the file asks for streaming, so both modes send one WAV to
    # REST at the end - the default this change must not disturb.
    path = write_config(tmp_path, '[api]\nmodel = "saaras:v4"\n')
    config = load_config(path=path, env={})
    assert (config.streaming_push_to_talk, config.streaming_toggle) == (False, False)


def test_streaming_constants_name_the_realtime_model_and_the_two_keys():
    assert REALTIME_MODEL == "saaras:v3-realtime"
    assert STREAMING_MODELS == ("saaras:v3-realtime", "saaras:v4")
    assert LEGACY_STREAMING_KEY == "streaming"
    assert STREAMING_PUSH_TO_TALK_KEY == "streaming_push_to_talk"
    assert STREAMING_TOGGLE_KEY == "streaming_toggle"
    assert STREAMING_KEYS == ("streaming_push_to_talk", "streaming_toggle")


def test_the_legacy_streaming_key_still_sets_both_modes(tmp_path):
    # The whole point of the fallback: an existing config.toml that says
    # ``streaming = true`` must keep streaming both modes, never silently
    # drop the user to batch.
    path = write_config(tmp_path, '[api]\nmodel = "saaras:v4"\nstreaming = true\n')
    config = load_config(path=path, env={})
    assert config.streaming_push_to_talk is True
    assert config.streaming_toggle is True


def test_the_legacy_streaming_key_false_leaves_both_modes_on_batch(tmp_path):
    path = write_config(tmp_path, '[api]\nmodel = "saaras:v4"\nstreaming = false\n')
    config = load_config(path=path, env={})
    assert (config.streaming_push_to_talk, config.streaming_toggle) == (False, False)


@pytest.mark.parametrize("key", STREAMING_KEYS)
@pytest.mark.parametrize("legacy, override", [(True, False), (False, True)])
def test_a_per_mode_key_overrides_the_legacy_key_for_that_mode_only(
    tmp_path, key, legacy, override
):
    other = STREAMING_TOGGLE_KEY if key == STREAMING_PUSH_TO_TALK_KEY else STREAMING_PUSH_TO_TALK_KEY
    text = (
        '[api]\nmodel = "saaras:v4"\n'
        f"streaming = {str(legacy).lower()}\n"
        f"{key} = {str(override).lower()}\n"
    )
    config = load_config(path=write_config(tmp_path, text), env={})
    assert getattr(config, key) is override
    assert getattr(config, other) is legacy


@pytest.mark.parametrize("key", STREAMING_KEYS)
def test_a_per_mode_key_alone_streams_that_mode_and_leaves_the_other_on_batch(
    tmp_path, key
):
    other = STREAMING_TOGGLE_KEY if key == STREAMING_PUSH_TO_TALK_KEY else STREAMING_PUSH_TO_TALK_KEY
    text = f'[api]\nmodel = "saaras:v4"\n{key} = true\n'
    config = load_config(path=write_config(tmp_path, text), env={})
    assert getattr(config, key) is True
    assert getattr(config, other) is False


def test_streaming_true_with_saaras_v4_is_accepted(tmp_path):
    path = write_config(tmp_path, '[api]\nmodel = "saaras:v4"\nstreaming = true\n')
    config = load_config(path=path, env={})
    assert config.streaming_push_to_talk is True
    assert config.streaming_toggle is True
    assert config.model == "saaras:v4"


def test_streaming_true_with_saaras_v3_is_rejected_naming_the_mode_and_the_model(tmp_path):
    path = write_config(tmp_path, '[api]\nmodel = "saaras:v3"\nstreaming = true\n')
    with pytest.raises(ConfigError) as raised:
        load_config(path=path, env={})
    message = str(raised.value)
    # The legacy key applies to push-to-talk first, and the message says so.
    assert "[api] streaming_push_to_talk" in message
    assert "saaras:v3" in message


@pytest.mark.parametrize("model", ["saaras:v3", "whisper-large", ""])
def test_streaming_true_with_an_unsupported_model_is_rejected(tmp_path, model):
    text = f'[api]\nmodel = "{model}"\nstreaming = true\n'
    with pytest.raises(ConfigError):
        load_config(path=write_config(tmp_path, text), env={})


def test_streaming_false_with_saaras_v3_is_still_accepted(tmp_path):
    path = write_config(tmp_path, '[api]\nmodel = "saaras:v3"\nstreaming = false\n')
    config = load_config(path=path, env={})
    assert (config.streaming_push_to_talk, config.streaming_toggle) == (False, False)
    assert config.model == "saaras:v3"


def test_the_realtime_model_forces_both_modes_on_when_the_options_are_absent(tmp_path):
    path = write_config(tmp_path, '[api]\nmodel = "saaras:v3-realtime"\n')
    config = load_config(path=path, env={})
    assert config.model == "saaras:v3-realtime"
    assert config.streaming_push_to_talk is True
    assert config.streaming_toggle is True


def test_the_realtime_model_forces_both_modes_on_over_an_explicit_false(tmp_path):
    # REST cannot accept this model at all, so neither mode may be left on the
    # batch path even when the file says so explicitly, for both keys.
    path = write_config(
        tmp_path,
        '[api]\nmodel = "saaras:v3-realtime"\nstreaming = false\n'
        "streaming_push_to_talk = false\nstreaming_toggle = false\n",
    )
    config = load_config(path=path, env={})
    assert config.streaming_push_to_talk is True
    assert config.streaming_toggle is True


def test_the_realtime_model_with_streaming_true_is_accepted(tmp_path):
    path = write_config(
        tmp_path, '[api]\nmodel = "saaras:v3-realtime"\nstreaming = true\n'
    )
    config = load_config(path=path, env={})
    assert config.streaming_push_to_talk is True
    assert config.streaming_toggle is True
    assert config.model == "saaras:v3-realtime"


@pytest.mark.parametrize("value", ['"true"', "1", "0", "[]", '"yes"'])
def test_non_boolean_streaming_is_rejected(tmp_path, value):
    path = write_config(tmp_path, f"[api]\nstreaming = {value}\n")
    with pytest.raises(ConfigError) as raised:
        load_config(path=path, env={})
    assert "[api] streaming" in str(raised.value)


@pytest.mark.parametrize("key", STREAMING_KEYS)
@pytest.mark.parametrize("value", ['"true"', "1", "[]"])
def test_non_boolean_per_mode_key_is_rejected_naming_that_key(tmp_path, key, value):
    path = write_config(tmp_path, f'[api]\nmodel = "saaras:v4"\n{key} = {value}\n')
    with pytest.raises(ConfigError) as raised:
        load_config(path=path, env={})
    assert f"[api] {key}" in str(raised.value)


def test_streaming_leaves_every_other_field_alone(tmp_path):
    path = write_config(
        tmp_path,
        '[api]\nkey = "k"\nmodel = "saaras:v4"\nstreaming = true\nmode = "verbatim"\n'
        '\n[audio]\nsample_rate = 8000\n\n[server]\nmax_duration_s = 9\n',
    )
    config = load_config(path=path, env={})
    assert config.streaming_push_to_talk is True
    assert config.streaming_toggle is True
    assert (config.mode, config.sample_rate, config.max_duration_s) == ("verbatim", 8000, 9)
    assert config.api_key == "k"


def test_the_two_switches_take_no_part_in_repr_or_equality_of_existing_fields():
    # Splitting the field must not have moved any pre-existing one: the
    # keyword construction every caller uses still works, and each switch is
    # independently visible to equality.
    assert Config(streaming_push_to_talk=True) != Config()
    assert Config(streaming_toggle=True) != Config()
    assert Config(streaming_push_to_talk=True) != Config(streaming_toggle=True)
    assert dataclasses.replace(
        Config(), streaming_push_to_talk=True
    ) == Config(streaming_push_to_talk=True)
    assert Config(streaming_push_to_talk=True).model == DEFAULT_MODEL
    assert Config(streaming_toggle=True).model == DEFAULT_MODEL


# --------------------------------------------------------------------------
# the REST model namespace (brief 8)
# --------------------------------------------------------------------------


def test_rest_models_are_the_three_the_rest_endpoint_serves():
    assert REST_MODELS == ("saarika:v2.5", "saaras:v3", "saaras:v4")


def test_the_realtime_only_model_is_not_a_rest_model():
    # The whole bug: 'saaras:v3-realtime' exists only on the realtime
    # WebSocket endpoint, so it must never appear in the REST namespace.
    assert REALTIME_MODEL == "saaras:v3-realtime"
    assert REALTIME_MODEL not in REST_MODELS


def test_saaras_v4_is_the_one_model_both_endpoints_share():
    assert set(REST_MODELS) & set(STREAMING_MODELS) == {"saaras:v4"}


def test_the_two_namespaces_are_disjoint_apart_from_saaras_v4():
    assert sorted(set(REST_MODELS) | set(STREAMING_MODELS)) == sorted(
        ["saarika:v2.5", "saaras:v3", "saaras:v3-realtime", "saaras:v4"]
    )


@pytest.mark.parametrize("model", REST_MODELS)
def test_load_config_accepts_every_rest_model_on_the_rest_path(tmp_path, model):
    config = load_config(path=write_config(tmp_path, f'[api]\nmodel = "{model}"\n'), env={})
    assert config.model == model
    assert (config.streaming_push_to_talk, config.streaming_toggle) == (False, False)


@pytest.mark.parametrize("model", STREAMING_MODELS)
def test_load_config_accepts_every_streaming_model_with_streaming_on(tmp_path, model):
    path = write_config(tmp_path, f'[api]\nmodel = "{model}"\nstreaming = true\n')
    config = load_config(path=path, env={})
    assert config.model == model
    assert config.streaming_push_to_talk is True
    assert config.streaming_toggle is True


REST_ONLY_MODELS = tuple(model for model in REST_MODELS if model not in STREAMING_MODELS)


@pytest.mark.parametrize("model", REST_ONLY_MODELS)
def test_load_config_refuses_a_rest_only_model_with_streaming_on(tmp_path, model):
    path = write_config(tmp_path, f'[api]\nmodel = "{model}"\nstreaming = true\n')
    with pytest.raises(ConfigError) as raised:
        load_config(path=path, env={})
    assert "[api] streaming" in str(raised.value)
    assert model in str(raised.value)


@pytest.mark.parametrize("model", REST_ONLY_MODELS)
@pytest.mark.parametrize("key", STREAMING_KEYS)
def test_a_rest_only_model_with_one_mode_streaming_is_rejected_naming_that_mode(
    tmp_path, model, key
):
    path = write_config(tmp_path, f'[api]\nmodel = "{model}"\n{key} = true\n')
    with pytest.raises(ConfigError) as raised:
        load_config(path=path, env={})
    message = str(raised.value)
    assert f"[api] {key}" in message
    assert model in message
    # Only the mode that asked for streaming is named.
    other = STREAMING_TOGGLE_KEY if key == STREAMING_PUSH_TO_TALK_KEY else STREAMING_PUSH_TO_TALK_KEY
    assert other not in message


@pytest.mark.parametrize("model", REST_ONLY_MODELS)
def test_the_batch_mode_is_never_blamed_for_a_streaming_mode_problem(tmp_path, model):
    # ``streaming_toggle`` is off, so a REST-only model is perfectly fine for
    # it; the error must come from push-to-talk alone.
    text = f'[api]\nmodel = "{model}"\nstreaming_push_to_talk = true\nstreaming_toggle = false\n'
    with pytest.raises(ConfigError) as raised:
        load_config(path=write_config(tmp_path, text), env={})
    assert "streaming_push_to_talk" in str(raised.value)


CANDIDATE_MODELS = tuple(
    dict.fromkeys(REST_MODELS + STREAMING_MODELS + ("saarika:v2.5", "whisper-large", ""))
)


@pytest.mark.parametrize("model", CANDIDATE_MODELS)
def test_load_config_streaming_acceptance_agrees_with_streaming_models(tmp_path, model):
    """``STREAMING_MODELS`` is exactly what ``load_config`` lets stream."""
    path = write_config(tmp_path, f'[api]\nmodel = "{model}"\nstreaming = true\n')
    if model in STREAMING_MODELS:
        config = load_config(path=path, env={})
        assert config.streaming_push_to_talk is True
        assert config.streaming_toggle is True
    else:
        with pytest.raises(ConfigError):
            load_config(path=path, env={})


@pytest.mark.parametrize("model", CANDIDATE_MODELS)
def test_load_config_rest_acceptance_agrees_with_rest_models(tmp_path, model):
    """Every non-empty model is accepted on the REST path; the guard is in
    ``transcriber.py``, which is where the endpoint is actually chosen."""
    path = write_config(tmp_path, f'[api]\nmodel = "{model}"\nstreaming = false\n')
    if not model:
        with pytest.raises(ConfigError):
            load_config(path=path, env={})
    else:
        assert load_config(path=path, env={}).model == model


def test_adding_rest_models_left_the_existing_constants_alone():
    assert DEFAULT_MODEL == "saaras:v4"
    assert STREAMING_MODELS == (REALTIME_MODEL, "saaras:v4")
    assert DEFAULT_MODEL in REST_MODELS
    assert DEFAULT_MODEL in STREAMING_MODELS


# --------------------------------------------------------------------------
# --check diagnostics (brief 8 section 1d)
# --------------------------------------------------------------------------


@pytest.fixture
def check_audio(monkeypatch):
    """Make --check's audio probe deterministic: no hardware in CI."""
    monkeypatch.setattr(daemon_main, "detect_backend", lambda backend: "pulse")


def run_check(config, tmp_path, capsys):
    """Run ``--check`` on ``config`` and return (exit status, stdout)."""
    status = daemon_main._run_check(config, tmp_path / "config.toml", None)
    return status, capsys.readouterr().out


def result_line(output: str) -> str:
    """Return the single ``result:`` line --check printed."""
    lines = [line for line in output.splitlines() if line.startswith("result:")]
    assert len(lines) == 1
    return lines[0]


BATCH_TEXT = "off (one request per recording)"
STREAM_TEXT = "on (realtime websocket)"
REST_ENDPOINT = "REST /speech-to-text"
STREAM_ENDPOINT = "realtime websocket"


def test_check_reports_both_modes_on_batch_by_default(
    make_config, tmp_path, capsys, check_audio
):
    status, out = run_check(make_config(model="saaras:v4"), tmp_path, capsys)
    assert (
        f"streaming:    push-to-talk {BATCH_TEXT}, record-and-send {BATCH_TEXT}" in out
    )
    assert (
        f"endpoint:     push-to-talk {REST_ENDPOINT}, record-and-send {REST_ENDPOINT}" in out
    )
    assert status == 0
    assert result_line(out) == "result:       OK"


def test_check_reports_the_rest_endpoint_for_a_rest_configuration(
    make_config, tmp_path, capsys, check_audio
):
    status, out = run_check(make_config(model="saaras:v3"), tmp_path, capsys)
    assert f"endpoint:     push-to-talk {REST_ENDPOINT}" in out
    assert "model:        saaras:v3" in out
    assert status == 0
    assert result_line(out) == "result:       OK"


def test_check_reports_each_mode_with_the_endpoint_it_will_use(
    make_config, tmp_path, capsys, check_audio
):
    # Push-to-talk streams, record-and-send batches: one line each.
    config = make_config(model="saaras:v4", streaming_push_to_talk=True)
    status, out = run_check(config, tmp_path, capsys)
    assert (
        f"streaming:    push-to-talk {STREAM_TEXT}, record-and-send {BATCH_TEXT}" in out
    )
    assert (
        f"endpoint:     push-to-talk {STREAM_ENDPOINT}, record-and-send {REST_ENDPOINT}" in out
    )
    assert status == 0
    assert result_line(out) == "result:       OK"


def test_check_reports_the_mirror_configuration(make_config, tmp_path, capsys, check_audio):
    config = make_config(model="saaras:v4", streaming_toggle=True)
    status, out = run_check(config, tmp_path, capsys)
    assert (
        f"streaming:    push-to-talk {BATCH_TEXT}, record-and-send {STREAM_TEXT}" in out
    )
    assert (
        f"endpoint:     push-to-talk {REST_ENDPOINT}, record-and-send {STREAM_ENDPOINT}" in out
    )
    assert status == 0
    assert result_line(out) == "result:       OK"


def test_check_reports_the_realtime_endpoint_for_a_fully_streaming_configuration(
    make_config, tmp_path, capsys, check_audio
):
    config = make_config(
        model="saaras:v3-realtime", streaming_push_to_talk=True, streaming_toggle=True
    )
    status, out = run_check(config, tmp_path, capsys)
    assert (
        f"streaming:    push-to-talk {STREAM_TEXT}, record-and-send {STREAM_TEXT}" in out
    )
    assert (
        f"endpoint:     push-to-talk {STREAM_ENDPOINT}, record-and-send {STREAM_ENDPOINT}"
        in out
    )
    assert status == 0
    assert result_line(out) == "result:       OK"


@pytest.mark.parametrize("push_to_talk", [False, True])
@pytest.mark.parametrize("toggle", [False, True])
def test_check_accepts_saaras_v4_in_every_combination(
    make_config, tmp_path, capsys, check_audio, push_to_talk, toggle
):
    config = make_config(
        model="saaras:v4", streaming_push_to_talk=push_to_talk, streaming_toggle=toggle
    )
    status, out = run_check(config, tmp_path, capsys)
    assert status == 0
    assert result_line(out) == "result:       OK"


def test_check_fails_naming_the_push_to_talk_mode_for_a_rest_only_model(
    make_config, tmp_path, capsys, check_audio
):
    config = make_config(model="saaras:v3", streaming_push_to_talk=True)
    status, out = run_check(config, tmp_path, capsys)

    assert status == 1
    assert f"endpoint:     push-to-talk {STREAM_ENDPOINT}, record-and-send {REST_ENDPOINT}" in out
    final = result_line(out)
    assert final.startswith("result:       FAILED (")
    assert "model 'saaras:v3' cannot be used with streaming in push-to-talk mode" in final
    assert "saaras:v3-realtime" in final
    assert "saaras:v4" in final
    assert "record-and-send mode" not in final
    assert "\n" not in final


def test_check_fails_naming_the_record_and_send_mode_for_a_rest_only_model(
    make_config, tmp_path, capsys, check_audio
):
    config = make_config(model="saaras:v3", streaming_toggle=True)
    status, out = run_check(config, tmp_path, capsys)

    assert status == 1
    final = result_line(out)
    assert "model 'saaras:v3' cannot be used with streaming in record-and-send mode" in final
    assert "push-to-talk mode" not in final


def test_check_fails_naming_both_modes_when_both_are_misconfigured(
    make_config, tmp_path, capsys, check_audio
):
    config = make_config(model="saaras:v3", streaming_push_to_talk=True, streaming_toggle=True)
    status, out = run_check(config, tmp_path, capsys)
    final = result_line(out)
    assert status == 1
    assert "in push-to-talk mode" in final
    assert "in record-and-send mode" in final


def test_check_fails_naming_the_realtime_model_used_with_rest(
    make_config, tmp_path, capsys, check_audio
):
    # load_config forces both modes on for this model, so only a hand-built
    # Config (or a daemon older than the forcing rule) can reach this state.
    config = make_config(model="saaras:v3-realtime")
    status, out = run_check(config, tmp_path, capsys)

    assert status == 1
    assert f"endpoint:     push-to-talk {REST_ENDPOINT}, record-and-send {REST_ENDPOINT}" in out
    final = result_line(out)
    assert final.startswith("result:       FAILED (")
    assert (
        "model 'saaras:v3-realtime' cannot be used with REST /speech-to-text "
        "in push-to-talk mode"
    ) in final
    assert "in record-and-send mode" in final
    assert "saarika:v2.5" in final
    assert "saaras:v3" in final


def test_check_still_reports_the_other_problems_alongside_the_model_one(
    make_config, tmp_path, capsys, check_audio
):
    config = make_config(api_key="", model="saaras:v3", streaming_toggle=True)
    status, out = run_check(config, tmp_path, capsys)
    final = result_line(out)
    assert status == 1
    assert "no API key" in final
    assert "model 'saaras:v3' cannot be used with streaming in record-and-send mode" in final


def test_check_problem_message_carries_no_http_text(make_config, tmp_path, capsys, check_audio):
    config = make_config(model="saaras:v3-realtime")
    _, out = run_check(config, tmp_path, capsys)
    assert "HTTP" not in out
    assert "400" not in result_line(out)


def test_check_output_stays_aligned(make_config, tmp_path, capsys, check_audio):
    # Every report line keeps its value in the same column, the two new ones
    # included.
    _, out = run_check(make_config(model="saaras:v4", streaming_toggle=True), tmp_path, capsys)
    for line in out.splitlines():
        label, separator, _ = line.partition(":")
        assert separator == ":", line
        assert line[:14].rstrip() == f"{label}:", line

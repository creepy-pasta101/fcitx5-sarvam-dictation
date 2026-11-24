"""Configuration tests for the engine switch and the ``[dictate]`` table.

The Dictate brief adds ``[api] engine`` - ``"saaras"`` (the default, and the
behaviour every earlier test in ``test_config.py`` pins down) or ``"dictate"``
- plus a ``[dictate]`` table of settings that only the Dictate route sends.

This module covers, in order:

* the constants and the ``Config`` defaults, which other units import;
* that a file which never mentions the engine loads exactly as it did before,
  and that ``engine = "dictate"`` round-trips every ``[dictate]`` key;
* every limit and enum the API documents, each refused locally with a message
  that names the offending key and value;
* the two engine-specific cross-checks (no ``translate`` mode, 16 kHz or 8 kHz
  audio only);
* the regression that matters most: the saaras-only guards must not fire under
  the dictate engine, and must still fire under the saaras engine.

Everything goes through ``load_config`` on a TOML file written to ``tmp_path``,
the way ``test_config.py`` does it. The autouse ``isolated_env`` fixture in
``conftest.py`` keeps ``HOME``/``XDG_*``/``SARVAM_API_KEY`` away from the real
user environment, so no test here can read or write the developer's config.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from sarvam_dictation.config import (
    DEFAULT_DICTATE_LANGUAGE,
    DEFAULT_DICTATE_STREAM_TYPE,
    DEFAULT_ENGINE,
    DICTATE_MODES,
    DICTATE_SAMPLE_RATES,
    ENGINE_DICTATE,
    ENGINE_SAARAS,
    MAX_DICTATE_CONTEXT,
    MAX_DICTATE_OUTPUT_RULES,
    MAX_DICTATE_REFERENCE,
    MAX_DICTATE_RULES,
    MAX_DICTATE_VOCABULARY,
    MAX_DICTATE_VOCABULARY_WORD,
    VALID_DICTATE_STREAM_TYPES,
    VALID_DICTATE_STYLE_PRESETS,
    VALID_ENGINES,
    Config,
    ConfigError,
    load_config,
)


def write_config(tmp_path: Path, text: str) -> Path:
    """Write ``text`` to ``tmp_path/config.toml`` and return the path."""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, text: str, env=None) -> Config:
    """Load a config written from ``text``, with an empty environment by default."""
    return load_config(path=write_config(tmp_path, text), env={} if env is None else env)


DICTATE_TOML = """
[api]
engine = "dictate"
mode = "verbatim"
language_code = "hi-IN"

[audio]
sample_rate = 8000

[dictate]
style = "clean"
rules = "Use Indian English spelling and Indian digit grouping."
vocabulary = ["Velqora", "सर्वम"]
context = "A standup note about the billing service."
reference = "Velqora is a fictional company that bills in rupees."
output_rules = "Keep it to one line."
model = "dictate-v1-2026-11-04"
stream_type = "fast"
"""


# --------------------------------------------------------------------------
# constants and defaults
# --------------------------------------------------------------------------


def test_the_engine_constants_match_the_brief():
    assert DEFAULT_ENGINE == "saaras"
    assert ENGINE_SAARAS == "saaras"
    assert ENGINE_DICTATE == "dictate"
    assert VALID_ENGINES == ("saaras", "dictate")


def test_the_dictate_constants_match_the_brief():
    assert DEFAULT_DICTATE_STREAM_TYPE == "balanced"
    assert VALID_DICTATE_STREAM_TYPES == ("fast", "balanced", "simulated")
    assert DICTATE_MODES == ("transcribe", "verbatim", "translit", "codemix")
    assert VALID_DICTATE_STYLE_PRESETS == ("none", "light", "clean")
    assert MAX_DICTATE_RULES == 2000
    assert MAX_DICTATE_CONTEXT == 2000
    assert MAX_DICTATE_REFERENCE == 50000
    assert MAX_DICTATE_OUTPUT_RULES == 4000
    assert MAX_DICTATE_VOCABULARY == 1000
    assert MAX_DICTATE_VOCABULARY_WORD == 64
    assert DEFAULT_DICTATE_LANGUAGE == "auto"
    assert DICTATE_SAMPLE_RATES == (16000, 8000)


def test_the_new_config_fields_default_to_the_saaras_engine_and_silence():
    config = Config()
    assert config.engine == "saaras"
    assert config.dictate_style == ""
    assert config.dictate_rules == ""
    assert config.dictate_vocabulary == ()
    assert config.dictate_context == ""
    assert config.dictate_reference == ""
    assert config.dictate_output_rules == ""
    assert config.dictate_model == ""
    assert config.dictate_stream_type == "balanced"


def test_a_missing_file_still_yields_the_saaras_engine(tmp_path):
    config = load_config(path=tmp_path / "does-not-exist.toml", env={})
    assert config == Config()
    assert config.engine == "saaras"


# --------------------------------------------------------------------------
# the default engine changes nothing
# --------------------------------------------------------------------------


def test_a_file_that_never_mentions_the_engine_loads_unchanged(tmp_path):
    text = """
[api]
key = "file-key"
model = "saaras:v3"
mode = "translate"
language_code = "hi-IN"

[audio]
sample_rate = 22050
"""
    config = load(tmp_path, text)
    assert config.engine == "saaras"
    assert config.model == "saaras:v3"
    assert config.mode == "translate"
    assert config.language_code == "hi-IN"
    # A rate the Dictate socket would refuse is still fine on the saaras path.
    assert config.sample_rate == 22050
    assert config.dictate_vocabulary == ()
    assert config.dictate_stream_type == "balanced"


def test_an_empty_file_is_still_exactly_the_built_in_defaults(tmp_path):
    assert load(tmp_path, "") == Config()


# --------------------------------------------------------------------------
# engine = "dictate" round-trips the whole [dictate] table
# --------------------------------------------------------------------------


def test_engine_dictate_loads_and_every_dictate_key_round_trips(tmp_path):
    config = load(tmp_path, DICTATE_TOML)
    assert config.engine == "dictate"
    assert config.dictate_style == "clean"
    assert config.dictate_rules == "Use Indian English spelling and Indian digit grouping."
    assert config.dictate_vocabulary == ("Velqora", "सर्वम")
    assert config.dictate_context == "A standup note about the billing service."
    assert config.dictate_reference == "Velqora is a fictional company that bills in rupees."
    assert config.dictate_output_rules == "Keep it to one line."
    assert config.dictate_model == "dictate-v1-2026-11-04"
    assert config.dictate_stream_type == "fast"
    # mode and language_code stay shared, in [api].
    assert config.mode == "verbatim"
    assert config.language_code == "hi-IN"
    assert config.sample_rate == 8000


def test_engine_dictate_with_an_absent_dictate_table_is_valid(tmp_path):
    config = load(tmp_path, '[api]\nengine = "dictate"\n')
    assert config.engine == "dictate"
    assert config.dictate_style == ""
    assert config.dictate_vocabulary == ()
    assert config.dictate_stream_type == "balanced"


@pytest.mark.parametrize("stream_type", VALID_DICTATE_STREAM_TYPES)
def test_every_dictate_stream_type_is_accepted(tmp_path, stream_type):
    text = f'[api]\nengine = "dictate"\n\n[dictate]\nstream_type = "{stream_type}"\n'
    config = load(tmp_path, text)
    assert config.dictate_stream_type == stream_type


@pytest.mark.parametrize("preset", VALID_DICTATE_STYLE_PRESETS)
def test_every_style_preset_is_accepted(tmp_path, preset):
    config = load(tmp_path, f'[api]\nengine = "dictate"\n\n[dictate]\nstyle = "{preset}"\n')
    assert config.dictate_style == preset


# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------


def test_vocabulary_arrives_as_a_tuple_of_strings(tmp_path):
    config = load(tmp_path, '[dictate]\nvocabulary = ["Velqora", "सर्वम"]\n')
    assert isinstance(config.dictate_vocabulary, tuple)
    assert config.dictate_vocabulary == ("Velqora", "सर्वम")
    assert all(isinstance(word, str) for word in config.dictate_vocabulary)


def test_an_absent_vocabulary_is_an_empty_tuple(tmp_path):
    assert Config().dictate_vocabulary == ()
    assert load(tmp_path, '[api]\nengine = "dictate"\n').dictate_vocabulary == ()


def test_an_explicitly_empty_vocabulary_is_an_empty_tuple(tmp_path):
    config = load(tmp_path, "[dictate]\nvocabulary = []\n")
    assert config.dictate_vocabulary == ()
    assert isinstance(config.dictate_vocabulary, tuple)


def test_a_thousand_vocabulary_words_are_accepted(tmp_path):
    words = [f"word{i}" for i in range(MAX_DICTATE_VOCABULARY)]
    config = load(tmp_path, f"[dictate]\nvocabulary = {json.dumps(words)}\n")
    assert len(config.dictate_vocabulary) == MAX_DICTATE_VOCABULARY


def test_a_thousand_and_one_vocabulary_words_are_rejected(tmp_path):
    words = [f"word{i}" for i in range(MAX_DICTATE_VOCABULARY + 1)]
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, f"[dictate]\nvocabulary = {json.dumps(words)}\n")
    assert "[dictate] vocabulary" in str(raised.value)


def test_a_sixty_four_character_vocabulary_word_is_accepted(tmp_path):
    word = "x" * MAX_DICTATE_VOCABULARY_WORD
    config = load(tmp_path, f"[dictate]\nvocabulary = {json.dumps([word])}\n")
    assert config.dictate_vocabulary == (word,)


def test_a_sixty_five_character_vocabulary_word_is_rejected(tmp_path):
    word = "x" * (MAX_DICTATE_VOCABULARY_WORD + 1)
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, f"[dictate]\nvocabulary = {json.dumps([word])}\n")
    assert "[dictate] vocabulary" in str(raised.value)


def test_an_empty_vocabulary_word_is_rejected(tmp_path):
    # The API's ``word`` is 1..64 characters, so an empty string is not a word.
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, '[dictate]\nvocabulary = [""]\n')
    assert "[dictate] vocabulary" in str(raised.value)


@pytest.mark.parametrize("value", ['"Velqora"', "7", "true", '["ok", 3]', "[[]]"])
def test_a_vocabulary_that_is_not_an_array_of_strings_is_rejected(tmp_path, value):
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, f"[dictate]\nvocabulary = {value}\n")
    assert "[dictate] vocabulary" in str(raised.value)


# --------------------------------------------------------------------------
# enums and limits
# --------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["foo", "Saaras", "dictate2", ""])
def test_an_unknown_engine_is_rejected_naming_the_key_and_the_value(tmp_path, engine):
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, f'[api]\nengine = "{engine}"\n')
    message = str(raised.value)
    assert "[api] engine" in message
    assert repr(engine) in message
    assert "saaras" in message and "dictate" in message


@pytest.mark.parametrize("style", ["cleanx", "Clean", "verbatim", "none "])
def test_an_unknown_style_preset_is_rejected_naming_the_key_and_the_value(tmp_path, style):
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, f'[api]\nengine = "dictate"\n\n[dictate]\nstyle = "{style}"\n')
    message = str(raised.value)
    assert "[dictate] style" in message
    assert repr(style) in message


@pytest.mark.parametrize("stream_type", ["warp", "Fast", ""])
def test_an_unknown_stream_type_is_rejected_naming_the_key_and_the_value(
    tmp_path, stream_type
):
    with pytest.raises(ConfigError) as raised:
        load(
            tmp_path,
            f'[api]\nengine = "dictate"\n\n[dictate]\nstream_type = "{stream_type}"\n',
        )
    message = str(raised.value)
    assert "[dictate] stream_type" in message
    assert repr(stream_type) in message


TEXT_LIMITS = (
    ("rules", MAX_DICTATE_RULES),
    ("context", MAX_DICTATE_CONTEXT),
    ("reference", MAX_DICTATE_REFERENCE),
    ("output_rules", MAX_DICTATE_OUTPUT_RULES),
)


@pytest.mark.parametrize("key, limit", TEXT_LIMITS)
def test_a_text_field_at_its_limit_is_accepted(tmp_path, key, limit):
    text = f'[api]\nengine = "dictate"\n\n[dictate]\n{key} = {json.dumps("y" * limit)}\n'
    config = load(tmp_path, text)
    assert len(getattr(config, f"dictate_{key}")) == limit


@pytest.mark.parametrize("key, limit", TEXT_LIMITS)
def test_a_text_field_one_character_over_its_limit_is_rejected(tmp_path, key, limit):
    with pytest.raises(ConfigError) as raised:
        load(
            tmp_path,
            f'[api]\nengine = "dictate"\n\n[dictate]\n{key} = {json.dumps("y" * (limit + 1))}\n',
        )
    message = str(raised.value)
    assert f"[dictate] {key}" in message
    assert str(limit) in message


@pytest.mark.parametrize("key", ["rules", "context", "reference", "output_rules", "style", "model"])
@pytest.mark.parametrize("value", ["7", "true", "[]"])
def test_a_non_string_dictate_setting_is_rejected_naming_that_key(tmp_path, key, value):
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, f"[dictate]\n{key} = {value}\n")
    assert f"[dictate] {key}" in str(raised.value)


def test_an_empty_dictate_model_sends_nothing_and_is_accepted(tmp_path):
    config = load(tmp_path, '[dictate]\nmodel = ""\n')
    assert config.dictate_model == ""


# --------------------------------------------------------------------------
# the two engine-specific cross-checks
# --------------------------------------------------------------------------


def test_engine_dictate_refuses_the_translate_mode_and_points_at_the_rules_key(tmp_path):
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, '[api]\nengine = "dictate"\nmode = "translate"\n')
    message = str(raised.value)
    assert "[api] mode" in message
    assert "translate" in message
    # The capability is not gone, it moved: the message says where.
    assert "[dictate] rules" in message


@pytest.mark.parametrize("mode", DICTATE_MODES)
def test_engine_dictate_accepts_every_dictate_mode(tmp_path, mode):
    config = load(tmp_path, f'[api]\nengine = "dictate"\nmode = "{mode}"\n')
    assert config.mode == mode


def test_engine_dictate_still_refuses_a_mode_neither_engine_has(tmp_path):
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, '[api]\nengine = "dictate"\nmode = "shout"\n')
    assert "[api] mode" in str(raised.value)


def test_engine_dictate_refuses_a_sample_rate_the_socket_cannot_take(tmp_path):
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, '[api]\nengine = "dictate"\n\n[audio]\nsample_rate = 22050\n')
    message = str(raised.value)
    assert "[audio] sample_rate" in message
    assert "22050" in message


@pytest.mark.parametrize("sample_rate", DICTATE_SAMPLE_RATES)
def test_engine_dictate_accepts_the_two_rates_the_socket_takes(tmp_path, sample_rate):
    config = load(
        tmp_path, f'[api]\nengine = "dictate"\n\n[audio]\nsample_rate = {sample_rate}\n'
    )
    assert config.sample_rate == sample_rate


def test_the_saaras_engine_leaves_a_22050_rate_alone(tmp_path):
    # The rate check is Dictate's, not a general tightening of the audio block.
    assert load(tmp_path, "[audio]\nsample_rate = 22050\n").sample_rate == 22050


# --------------------------------------------------------------------------
# the guards are engine-gated (the regression that matters)
# --------------------------------------------------------------------------


SAARAS_ONLY_COMBINATION = """
model = "saaras:v3"
streaming_push_to_talk = true
keyterms = ["Sarvam"]
"""


def test_engine_dictate_ignores_the_streaming_needs_a_realtime_model_rule(tmp_path):
    # Dictate streams over its own socket with its own model namespace, so a
    # saaras model that the realtime endpoint would not serve is not a reason
    # to refuse the config.
    text = f'[api]\nengine = "dictate"\n{SAARAS_ONLY_COMBINATION}'
    config = load(tmp_path, text)
    assert config.engine == "dictate"
    assert config.model == "saaras:v3"
    assert config.streaming_push_to_talk is True
    assert config.keyterms == ("Sarvam",)


def test_engine_dictate_ignores_the_keyterms_model_rule(tmp_path):
    # Dictate uses ``vocabulary``, not ``keyterms``; nothing about keyterms may
    # block a dictate configuration.
    config = load(tmp_path, '[api]\nengine = "dictate"\nkeyterms = ["Sarvam", "सर्वम"]\n')
    assert config.keyterms == ("Sarvam", "सर्वम")


def test_engine_dictate_does_not_let_the_realtime_model_force_streaming_on(tmp_path):
    config = load(tmp_path, '[api]\nengine = "dictate"\nmodel = "saaras:v3-realtime"\n')
    assert config.streaming_push_to_talk is False
    assert config.streaming_toggle is False


def test_the_same_combination_under_the_saaras_engine_still_raises(tmp_path):
    text = f'[api]\nengine = "saaras"\n{SAARAS_ONLY_COMBINATION}'
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, text)
    message = str(raised.value)
    assert "[api] streaming_push_to_talk" in message
    assert "saaras:v3" in message


def test_the_same_combination_with_no_engine_named_still_raises(tmp_path):
    # The default engine is the one that must stay strict.
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, f"[api]\n{SAARAS_ONLY_COMBINATION}")
    assert "[api] streaming_push_to_talk" in str(raised.value)


def test_the_saaras_engine_still_lets_a_streaming_model_stream(tmp_path):
    config = load(tmp_path, '[api]\nengine = "saaras"\nmodel = "saaras:v4"\nstreaming = true\n')
    assert config.streaming_push_to_talk is True
    assert config.streaming_toggle is True


# --------------------------------------------------------------------------
# the [dictate] table is read under both engines
# --------------------------------------------------------------------------


def test_an_invalid_dictate_value_is_reported_even_under_the_saaras_engine(tmp_path):
    # Deliberate: a typo in [dictate] is worth reporting before the user flips
    # the engine, not after the first failed dictation.
    with pytest.raises(ConfigError) as raised:
        load(tmp_path, '[dictate]\nstyle = "cleanx"\n')
    assert "[dictate] style" in str(raised.value)


# --------------------------------------------------------------------------
# the new fields sit beside the old ones without disturbing them
# --------------------------------------------------------------------------


def test_the_new_fields_take_part_in_equality_and_dataclasses_replace():
    assert Config(engine="dictate") != Config()
    assert Config(dictate_style="clean") != Config()
    assert dataclasses.replace(Config(), engine="dictate") == Config(engine="dictate")
    assert Config(engine="dictate").model == "saaras:v4"


def test_the_dictate_table_leaves_every_other_field_alone(tmp_path):
    config = load(tmp_path, DICTATE_TOML)
    assert config.api_key == ""
    assert config.backend == "auto"
    assert config.max_duration_s == 300
    assert config.streaming_push_to_talk is False
    assert config.streaming_toggle is False

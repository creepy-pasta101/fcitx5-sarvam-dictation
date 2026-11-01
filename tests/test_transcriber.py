"""Transcriber tests: brief §6 ``transcriber.py``, with no network and no SDK.

The ``sarvamai`` package is never required: every test that would reach the API
injects a fake client, and the two tests that check the lazy-import rule skip
themselves if the SDK happens to be installed in the environment. Audio files
are built with the stdlib ``wave`` module inside ``tmp_path``.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import pytest

from sarvam_dictation.config import DEFAULT_MODEL, REALTIME_MODEL, REST_MODELS
from sarvam_dictation.transcriber import (
    SarvamTranscriber,
    TranscriptionError,
    Transcript,
    join_transcripts,
    split_wav,
)

ALLOWED_CODES = {"no_api_key", "config_invalid", "network", "api", "internal"}


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for the SDK response object."""

    def __init__(self, transcript: str, language_code: str | None = "hi-IN", expose_language=True):
        self.transcript = transcript
        if expose_language:
            self.language_code = language_code


class FakeSpeechToText:
    """Records every ``transcribe`` call and replays canned responses."""

    def __init__(self, responses=None, error: BaseException | None = None):
        self.responses = list(responses) if responses else [FakeResponse("नमस्ते")]
        self.error = error
        self.calls = []
        self.snapshots = []

    def transcribe(self, **kwargs):
        self.calls.append(kwargs)
        self.snapshots.append(_snapshot(kwargs.get("file")))
        if self.error is not None:
            raise self.error
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def _snapshot(handle):
    """Describe the audio file handed to the client at call time.

    Sampled while the call is in flight, because a chunk file may legitimately
    be removed once transcription finishes.
    """
    snapshot = {"closed": bool(getattr(handle, "closed", True))}
    try:
        with wave.open(str(handle.name), "rb") as audio:
            snapshot.update(
                valid=True,
                framerate=audio.getframerate(),
                channels=audio.getnchannels(),
                sampwidth=audio.getsampwidth(),
                frames=audio.getnframes(),
                raw=audio.readframes(audio.getnframes()),
            )
    except Exception as exc:  # pragma: no cover - only on a broken chunk
        snapshot.update(valid=False, error=repr(exc), raw=b"")
    return snapshot


class FakeClient:
    """Minimal stand-in for ``sarvamai.SarvamAI``."""

    def __init__(self, responses=None, error: BaseException | None = None):
        self.speech_to_text = FakeSpeechToText(responses=responses, error=error)

    @property
    def calls(self):
        return self.speech_to_text.calls

    @property
    def snapshots(self):
        return self.speech_to_text.snapshots


@pytest.fixture
def api_key():
    return "test-api-key-not-real"


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def wav(tmp_path):
    """Factory writing a WAV of the requested length inside ``tmp_path``."""
    from conftest import write_wav_file

    def _make(duration_s: float, name: str = "audio.wav") -> Path:
        return write_wav_file(tmp_path / name, duration_s=duration_s, sample_rate=16000)

    return _make


# --------------------------------------------------------------------------
# Transcript / join_transcripts
# --------------------------------------------------------------------------


def test_transcript_holds_text_language_and_segments():
    transcript = Transcript(text="नमस्ते", language_code="hi-IN", segments=2)
    assert (transcript.text, transcript.language_code, transcript.segments) == ("नमस्ते", "hi-IN", 2)


@pytest.mark.parametrize(
    "parts, expected",
    [
        (["a", "b"], "a b"),
        (["  a  ", "  b  "], "a b"),
        (["a", "", "b"], "a b"),
        (["a", "   ", "b"], "a b"),
        (["a "], "a"),
        (["", ""], ""),
        ([], ""),
        (["one", "two", "three"], "one two three"),
        (["नमस्ते", "दुनिया"], "नमस्ते दुनिया"),
    ],
)
def test_join_transcripts_strips_drops_empties_and_joins_with_one_space(parts, expected):
    assert join_transcripts(parts) == expected


def test_join_transcripts_never_double_spaces():
    assert "  " not in join_transcripts([" a ", "", " b ", "  c  "])


# --------------------------------------------------------------------------
# split_wav
# --------------------------------------------------------------------------


def test_split_wav_returns_ordered_valid_chunks_that_cover_the_audio(wav, tmp_path):
    source = wav(60.0)
    chunks = split_wav(source, outdir=tmp_path / "chunks")

    assert len(chunks) >= 2
    total = 0.0
    for chunk in chunks:
        with wave.open(str(chunk), "rb") as handle:
            assert handle.getframerate() == 16000
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            duration = handle.getnframes() / handle.getframerate()
        assert duration <= 28.0 + 0.05
        total += duration
    assert abs(total - 60.0) < 0.1


def test_split_wav_keeps_chunks_in_source_order(wav, tmp_path):
    source = wav(60.0)
    with wave.open(str(source), "rb") as handle:
        original = handle.readframes(handle.getnframes())

    chunks = split_wav(source, outdir=tmp_path / "chunks")
    rebuilt = b""
    for chunk in chunks:
        with wave.open(str(chunk), "rb") as handle:
            rebuilt += handle.readframes(handle.getnframes())
    assert rebuilt == original


def test_split_wav_of_a_short_file_yields_a_single_chunk(wav, tmp_path):
    source = wav(5.0)
    chunks = split_wav(source, outdir=tmp_path / "chunks")
    assert len(chunks) == 1
    with wave.open(str(chunks[0]), "rb") as handle:
        assert abs(handle.getnframes() / handle.getframerate() - 5.0) < 0.05


def test_split_wav_respects_a_custom_max_seconds(wav, tmp_path):
    source = wav(10.0)
    chunks = split_wav(source, max_seconds=3.0, outdir=tmp_path / "chunks")
    assert len(chunks) == 4
    for chunk in chunks:
        with wave.open(str(chunk), "rb") as handle:
            assert handle.getnframes() / handle.getframerate() <= 3.0 + 0.05


# --------------------------------------------------------------------------
# API key handling
# --------------------------------------------------------------------------


def test_missing_api_key_raises_no_api_key_before_any_client_work(make_config, wav):
    transcriber = SarvamTranscriber(make_config(api_key=""))
    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(wav(1.0))
    assert excinfo.value.code == "no_api_key"


def test_missing_api_key_never_calls_an_injected_client(make_config, wav):
    client = FakeClient()
    transcriber = SarvamTranscriber(make_config(api_key=""), client=client)
    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(wav(1.0))
    assert excinfo.value.code == "no_api_key"
    assert client.calls == []


def test_transcription_error_codes_are_from_the_documented_set():
    assert ALLOWED_CODES == {"no_api_key", "config_invalid", "network", "api", "internal"}
    assert issubclass(TranscriptionError, Exception)


def test_config_invalid_is_a_transcription_error_code():
    error = TranscriptionError("model 'x' cannot be sent to REST", code="config_invalid")
    assert error.code == "config_invalid"
    assert error.code in ALLOWED_CODES


# --------------------------------------------------------------------------
# SDK call shape
# --------------------------------------------------------------------------


def test_injected_client_receives_model_and_mode(make_config, fake_client, wav):
    config = make_config(model="saaras:v4", mode="transcribe", language_code="", keyterms=())
    SarvamTranscriber(config, client=fake_client).transcribe(wav(1.0))

    assert len(fake_client.calls) == 1
    kwargs = fake_client.calls[0]
    assert set(kwargs) == {"file", "model", "mode"}
    assert kwargs["model"] == "saaras:v4"
    assert kwargs["mode"] == "transcribe"


def test_language_code_is_only_passed_when_set(make_config, fake_client, wav):
    config = make_config(language_code="hi-IN")
    SarvamTranscriber(config, client=fake_client).transcribe(wav(1.0))
    assert fake_client.calls[0]["language_code"] == "hi-IN"


def test_language_code_is_omitted_when_empty(make_config, fake_client, wav):
    config = make_config(language_code="")
    SarvamTranscriber(config, client=fake_client).transcribe(wav(1.0))
    assert "language_code" not in fake_client.calls[0]


def test_keyterms_are_passed_as_a_list_for_saaras_v4(make_config, fake_client, wav):
    config = make_config(model="saaras:v4", keyterms=("Sarvam", "सर्वम"))
    SarvamTranscriber(config, client=fake_client).transcribe(wav(1.0))
    assert fake_client.calls[0]["keyterms"] == ["Sarvam", "सर्वम"]


def test_keyterms_are_omitted_for_saaras_v3(make_config, fake_client, wav):
    config = make_config(model="saaras:v3", keyterms=("Sarvam",))
    SarvamTranscriber(config, client=fake_client).transcribe(wav(1.0))
    assert "keyterms" not in fake_client.calls[0]


def test_keyterms_are_omitted_when_empty(make_config, fake_client, wav):
    config = make_config(model="saaras:v4", keyterms=())
    SarvamTranscriber(config, client=fake_client).transcribe(wav(1.0))
    assert "keyterms" not in fake_client.calls[0]


def test_file_handle_is_closed_after_the_call(make_config, fake_client, wav):
    SarvamTranscriber(make_config(), client=fake_client).transcribe(wav(1.0))
    handle = fake_client.calls[0]["file"]
    assert handle.closed is True


def test_short_audio_is_transcribed_in_one_call(make_config, fake_client, wav):
    transcript = SarvamTranscriber(make_config(), client=fake_client).transcribe(wav(5.0))
    assert len(fake_client.calls) == 1
    assert transcript.segments == 1


def test_result_carries_text_and_language_from_the_response(make_config, wav):
    client = FakeClient(responses=[FakeResponse("नमस्ते दुनिया", "hi-IN")])
    transcript = SarvamTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.text == "नमस्ते दुनिया"
    assert transcript.language_code == "hi-IN"


def test_missing_language_attribute_yields_none(make_config, wav):
    client = FakeClient(responses=[FakeResponse("hello", expose_language=False)])
    transcript = SarvamTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.language_code is None


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def test_long_audio_is_split_into_ordered_chunks_and_joined(make_config, wav, tmp_path):
    source = wav(60.0)
    expected_chunks = len(split_wav(source, outdir=tmp_path / "reference"))
    client = FakeClient(
        responses=[FakeResponse("alpha"), FakeResponse("beta"), FakeResponse("gamma")]
    )

    transcript = SarvamTranscriber(make_config(), client=client).transcribe(source)

    assert len(client.calls) == expected_chunks
    assert expected_chunks >= 3
    assert transcript.segments == expected_chunks
    assert transcript.text == "alpha beta gamma"


def test_chunks_reassembled_in_order_reproduce_the_source_audio(make_config, wav):
    source = wav(60.0)
    with wave.open(str(source), "rb") as handle:
        original = handle.readframes(handle.getnframes())

    client = FakeClient()
    SarvamTranscriber(make_config(), client=client).transcribe(source)

    rebuilt = b"".join(snapshot["raw"] for snapshot in client.snapshots)
    assert rebuilt == original


def test_chunk_transcripts_are_stripped_and_empty_parts_dropped(make_config, wav):
    source = wav(60.0)
    client = FakeClient(
        responses=[FakeResponse("  alpha  "), FakeResponse("   "), FakeResponse(" beta ")]
    )
    transcript = SarvamTranscriber(make_config(), client=client).transcribe(source)
    assert transcript.text == "alpha beta"
    assert transcript.segments == 3


def test_each_chunk_handed_to_the_client_is_a_valid_wav(make_config, wav):
    source = wav(60.0)
    client = FakeClient()
    SarvamTranscriber(make_config(), client=client).transcribe(source)

    assert len(client.snapshots) >= 2
    for snapshot in client.snapshots:
        assert snapshot["valid"] is True
        assert snapshot["framerate"] == 16000
        assert snapshot["channels"] == 1
        assert snapshot["frames"] > 0


# --------------------------------------------------------------------------
# error mapping
# --------------------------------------------------------------------------


def test_network_error_maps_to_a_transcription_error(make_config, wav, api_key):
    client = FakeClient(error=ConnectionError("connection reset by peer"))
    with pytest.raises(TranscriptionError) as excinfo:
        SarvamTranscriber(make_config(), client=client).transcribe(wav(1.0))

    error = excinfo.value
    assert error.code in ALLOWED_CODES
    assert error.code == "network"
    assert "connection reset by peer" in str(error)


def test_api_error_maps_to_a_transcription_error(make_config, wav):
    client = FakeClient(error=RuntimeError("500 internal server error"))
    with pytest.raises(TranscriptionError) as excinfo:
        SarvamTranscriber(make_config(), client=client).transcribe(wav(1.0))

    error = excinfo.value
    assert error.code in ALLOWED_CODES
    assert "500 internal server error" in str(error)


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError("socket closed"),
        RuntimeError("bad request"),
        ValueError("weird payload"),
    ],
)
def test_errors_never_leak_the_api_key(make_config, wav, failure):
    key = "super-secret-key-that-must-not-leak"
    client = FakeClient(error=failure)
    with pytest.raises(TranscriptionError) as excinfo:
        SarvamTranscriber(make_config(api_key=key), client=client).transcribe(wav(1.0))
    assert key not in str(excinfo.value)


# --------------------------------------------------------------------------
# lazy SDK import
# --------------------------------------------------------------------------


def test_importing_the_module_does_not_import_the_sdk():
    if "sarvamai" in sys.modules:
        pytest.skip("sarvamai is installed and already imported in this interpreter")
    import importlib

    import sarvam_dictation.transcriber as transcriber_module

    # Reloading rebinds every name in the module, including the exception
    # classes, so the module namespace is restored afterwards: a reloaded
    # module would otherwise leave later tests holding stale class objects.
    saved = dict(vars(transcriber_module))
    try:
        importlib.reload(transcriber_module)
        assert "sarvamai" not in sys.modules
    finally:
        vars(transcriber_module).clear()
        vars(transcriber_module).update(saved)


def test_an_injected_client_never_triggers_the_sdk_import(make_config, wav):
    if "sarvamai" in sys.modules:
        pytest.skip("sarvamai is installed and already imported in this interpreter")
    SarvamTranscriber(make_config(), client=FakeClient()).transcribe(wav(1.0))
    assert "sarvamai" not in sys.modules


# --------------------------------------------------------------------------
# the REST model namespace (brief 8)
# --------------------------------------------------------------------------


def test_the_realtime_model_is_not_a_rest_model():
    assert REALTIME_MODEL not in REST_MODELS
    assert DEFAULT_MODEL in REST_MODELS


def test_a_realtime_only_model_raises_config_invalid_before_any_file_work(
    make_config, tmp_path
):
    client = FakeClient()
    transcriber = SarvamTranscriber(make_config(model=REALTIME_MODEL), client=client)
    missing = tmp_path / "never-recorded.wav"

    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(missing)

    error = excinfo.value
    assert error.code == "config_invalid"
    # The guard runs before the file is even stat()ed: a path that does not
    # exist still yields the model problem, not an "audio file ... does not
    # exist" internal error.
    assert "does not exist" not in str(error)
    assert missing.exists() is False


def test_a_realtime_only_model_never_calls_the_sdk(make_config, tmp_path):
    client = FakeClient()
    transcriber = SarvamTranscriber(make_config(model=REALTIME_MODEL), client=client)

    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(tmp_path / "never-recorded.wav")

    assert excinfo.value.code == "config_invalid"
    assert client.calls == []
    assert client.snapshots == []


def test_a_realtime_only_model_never_constructs_a_client(make_config):
    # No injected client: if the guard did not run first, _build_client would
    # try to import the SDK (and fail with `internal` here, where it is not
    # installed). `config_invalid` proves the SDK was never reached.
    transcriber = SarvamTranscriber(make_config(model=REALTIME_MODEL))
    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(Path("/nonexistent/audio.wav"))
    assert excinfo.value.code == "config_invalid"


def test_the_config_invalid_message_names_the_model_and_says_what_to_do(make_config):
    transcriber = SarvamTranscriber(make_config(model=REALTIME_MODEL), client=FakeClient())
    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(Path("/nonexistent/audio.wav"))

    message = str(excinfo.value)
    assert REALTIME_MODEL in message
    assert "realtime" in message
    assert "streaming = true" in message
    assert DEFAULT_MODEL in message
    # This error exists so the provider's HTTP 400 is never seen again.
    assert "HTTP" not in message
    assert "400" not in message
    assert message.count(".") <= 2


def test_an_unknown_model_is_refused_rather_than_replaced(make_config, tmp_path):
    client = FakeClient()
    transcriber = SarvamTranscriber(make_config(model="whisper-large"), client=client)

    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(tmp_path / "never-recorded.wav")

    assert excinfo.value.code == "config_invalid"
    assert "whisper-large" in str(excinfo.value)
    assert client.calls == []


@pytest.mark.parametrize("model", REST_MODELS)
def test_every_rest_model_is_sent_verbatim(make_config, fake_client, wav, model):
    config = make_config(model=model, keyterms=())
    transcript = SarvamTranscriber(config, client=fake_client).transcribe(wav(1.0))

    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["model"] == model
    assert transcript.segments == 1


@pytest.mark.parametrize("model", REST_MODELS)
def test_no_rest_model_is_swapped_for_another(make_config, fake_client, wav, model):
    config = make_config(model=model)
    SarvamTranscriber(config, client=fake_client).transcribe(wav(1.0))
    assert fake_client.calls[0]["model"] == config.model


def test_the_model_is_checked_before_the_api_key(make_config, tmp_path):
    # Both are wrong; the model is the one that decides which endpoint would be
    # used, so it is reported first (brief 8 section 1b: "at the start").
    config = make_config(api_key="", model=REALTIME_MODEL)
    transcriber = SarvamTranscriber(config, client=FakeClient())
    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(tmp_path / "never-recorded.wav")
    assert excinfo.value.code == "config_invalid"


def test_a_valid_rest_model_still_reports_a_missing_key(make_config, wav):
    # The new guard must not have moved or weakened the existing one.
    transcriber = SarvamTranscriber(make_config(api_key="", model="saaras:v3"))
    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(wav(1.0))
    assert excinfo.value.code == "no_api_key"

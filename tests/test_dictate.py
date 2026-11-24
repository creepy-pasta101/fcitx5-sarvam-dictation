"""Dictate clip tests: ``dictate.py``, with no network and no ``httpx``.

Every test injects a hand-written fake client that records what it was asked
to send, so nothing here touches the real API, and the two lazy-import tests
run the module in a subprocess with ``httpx`` blocked, which is how the
"imports on a machine without the transport" rule is actually proved rather
than assumed. Audio files are built with the stdlib ``wave`` module inside
``tmp_path``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from sarvam_dictation.config import DICTATE_MODES, Config
from sarvam_dictation.dictate import (
    CLIP_TIMEOUT_S,
    DICTATE_CLIP_URL,
    DictateTranscriber,
    context_payload,
    style_payload,
    vocabulary_payload,
)
from sarvam_dictation.transcriber import TranscriptionError, Transcript, split_wav

ALLOWED_CODES = {"no_api_key", "config_invalid", "network", "api", "internal"}


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for an ``httpx.Response``."""

    def __init__(self, body=None, status_code: int = 200):
        self._body = {} if body is None else body
        self.status_code = status_code

    def json(self):
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body


class RecordedCall:
    """One ``post`` as the fake client saw it, snapshotted at call time."""

    def __init__(self, url, headers, data, files, timeout):
        self.url = url
        self.headers = dict(headers or {})
        self.data = dict(data or {})
        self.timeout = timeout
        # (filename, content_type, bytes, handle, closed_at_call)
        self.files = {}
        for name, value in (files or {}).items():
            filename, handle, content_type = value
            handle.seek(0)
            self.files[name] = (filename, content_type, handle.read(), handle, handle.closed)

    @property
    def file(self):
        """The single audio part, as ``(filename, content_type, bytes)``."""
        return self.files["file"][:3]

    @property
    def file_handle(self):
        return self.files["file"][3]

    @property
    def json_parts(self):
        """The form parts the API parses as JSON, decoded."""
        return {name: json.loads(self.data[name]) for name in ("style", "vocabulary", "context") if name in self.data}


class FakeClient:
    """A ``post``-only client that records calls and replays canned responses.

    Hand-written rather than a ``MagicMock`` so the assertions read as "the
    client was asked to send this", and so the file part is captured while the
    handle is still open.
    """

    def __init__(self, responses=None, error: BaseException | None = None):
        self.responses = (
            list(responses)
            if responses
            else [FakeResponse({"text": "नमस्ते", "language_code": "hi-IN"})]
        )
        self.error = error
        self.calls: list[RecordedCall] = []

    def post(self, url, headers=None, data=None, files=None, timeout=None):
        self.calls.append(RecordedCall(url, headers, data, files, timeout))
        if self.error is not None:
            raise self.error
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


class ReadTimeout(Exception):
    """Named like an ``httpx`` timeout so the shared classifier sees it."""


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


def _chunk_count(path: Path, tmp_path: Path) -> int:
    """How many chunks ``split_wav`` makes of ``path`` (the reference answer)."""
    return len(split_wav(path, outdir=tmp_path / "reference"))


# --------------------------------------------------------------------------
# lazy transport import
# --------------------------------------------------------------------------

_SUBPROCESS_CHECK = r"""
import pathlib, sys, tempfile, wave

class _BlockHttpx:
    def find_spec(self, name, path=None, target=None):
        if name == "httpx" or name.startswith("httpx."):
            raise ModuleNotFoundError("No module named 'httpx'", name="httpx")
        return None

sys.meta_path.insert(0, _BlockHttpx())
for _name in [n for n in list(sys.modules) if n == "httpx" or n.startswith("httpx.")]:
    del sys.modules[_name]

import sarvam_dictation.dictate as module
assert "httpx" not in sys.modules, "dictate imported httpx at import time"

from sarvam_dictation.config import Config

directory = pathlib.Path(tempfile.mkdtemp(prefix="dictate-no-httpx-"))
clip = directory / "clip.wav"
with wave.open(str(clip), "wb") as handle:
    handle.setnchannels(1)
    handle.setsampwidth(2)
    handle.setframerate(16000)
    handle.writeframes(b"\x00\x00" * 8000)

transcriber = module.DictateTranscriber(Config(api_key="test-key"))
try:
    transcriber.transcribe(clip)
except module.TranscriptionError as exc:
    assert exc.code == "internal", exc.code
    assert "httpx" in str(exc), str(exc)
    assert "pip install" in str(exc), str(exc)
else:
    raise AssertionError("a missing httpx did not raise")

assert "httpx" not in sys.modules, "the failed call still imported httpx"
print("ok")
"""


def test_the_module_imports_and_reports_a_missing_httpx_on_a_machine_without_it():
    daemon_dir = Path(sys.modules["sarvam_dictation"].__file__).resolve().parent.parent
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(daemon_dir) + os.pathsep + environment.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_CHECK],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_an_injected_client_never_imports_httpx(make_config, wav):
    if "httpx" in sys.modules:
        pytest.skip("httpx is already imported in this interpreter")
    DictateTranscriber(make_config(), client=FakeClient()).transcribe(wav(1.0))
    assert "httpx" not in sys.modules


# --------------------------------------------------------------------------
# pre-flight checks
# --------------------------------------------------------------------------


def test_a_missing_api_key_raises_no_api_key_before_any_client_is_used(make_config, wav):
    client = FakeClient()
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(api_key=""), client=client).transcribe(wav(1.0))
    assert excinfo.value.code == "no_api_key"
    assert client.calls == []


def test_a_missing_api_key_is_reported_before_the_file_is_even_opened(make_config, tmp_path):
    # No injected client and no file: a `no_api_key` answer proves the key was
    # checked before anything else was touched.
    transcriber = DictateTranscriber(make_config(api_key=""))
    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(tmp_path / "never-recorded.wav")
    assert excinfo.value.code == "no_api_key"


@pytest.mark.parametrize("mode", ["translate", "dictate", ""])
def test_a_mode_dictate_does_not_accept_raises_config_invalid_before_the_file(
    make_config, tmp_path, mode
):
    client = FakeClient()
    transcriber = DictateTranscriber(make_config(mode=mode), client=client)
    missing = tmp_path / "never-recorded.wav"

    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.transcribe(missing)

    error = excinfo.value
    assert error.code == "config_invalid"
    assert repr(mode) in str(error)
    assert "[dictate] rules" in str(error)
    # The guard runs before the file is stat()ed and before anything is sent.
    assert "does not exist" not in str(error)
    assert client.calls == []
    assert missing.exists() is False


@pytest.mark.parametrize("mode", DICTATE_MODES)
def test_every_dictate_mode_is_sent_verbatim(make_config, wav, mode):
    client = FakeClient()
    DictateTranscriber(make_config(mode=mode), client=client).transcribe(wav(1.0))
    assert client.calls[0].data["mode"] == mode


def test_a_missing_audio_file_maps_to_internal(make_config, tmp_path):
    client = FakeClient()
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(tmp_path / "gone.wav")
    assert excinfo.value.code == "internal"
    assert "does not exist" in str(excinfo.value)
    assert client.calls == []


# --------------------------------------------------------------------------
# one short clip
# --------------------------------------------------------------------------


def test_a_short_clip_makes_exactly_one_post_and_returns_its_text(make_config, wav):
    client = FakeClient([FakeResponse({"text": "नमस्ते दुनिया।", "language_code": "hi-IN"})])
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(2.0))

    assert len(client.calls) == 1
    assert transcript == Transcript(text="नमस्ते दुनिया।", language_code="hi-IN", segments=1)


def test_the_request_goes_to_the_v2_dictate_endpoint_with_the_key_header(
    make_config, wav, api_key
):
    client = FakeClient()
    DictateTranscriber(make_config(api_key=api_key), client=client).transcribe(wav(1.0))

    call = client.calls[0]
    assert call.url == "https://api.sarvam.ai/v2/dictate"
    assert call.url == DICTATE_CLIP_URL
    assert call.url.endswith("/v2/dictate")  # no trailing slash: it 307s
    assert call.headers["api-subscription-key"] == api_key


def test_the_audio_is_sent_as_a_wav_file_part(make_config, wav):
    client = FakeClient()
    DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))

    filename, content_type, content = client.calls[0].file
    assert filename.endswith(".wav")
    assert content_type == "audio/wav"
    assert content.startswith(b"RIFF")
    assert client.calls[0].file_handle.closed is True  # closed once the post returned


def test_every_post_waits_long_enough_for_the_finishing_step(make_config, wav):
    # The API's finishing call can take up to 90 s, so a 28 s clip needs more
    # than the usual short timeout; 120 s is the documented headroom.
    client = FakeClient()
    DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert client.calls[0].timeout == CLIP_TIMEOUT_S
    assert CLIP_TIMEOUT_S >= 90


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def test_a_clip_longer_than_the_chunk_limit_is_posted_in_parts_and_joined(
    make_config, wav, tmp_path
):
    source = wav(60.0)
    expected = _chunk_count(source, tmp_path)
    assert expected >= 3
    client = FakeClient(
        [
            FakeResponse({"text": "alpha"}),
            FakeResponse({"text": "beta"}),
            FakeResponse({"text": "gamma"}),
        ]
    )

    transcript = DictateTranscriber(make_config(), client=client).transcribe(source)

    assert len(client.calls) == expected
    assert transcript.segments == expected
    assert transcript.text == "alpha beta gamma"


def test_the_reported_language_is_the_first_one_a_chunk_reported(make_config, wav, tmp_path):
    source = wav(60.0)
    client = FakeClient(
        [
            FakeResponse({"text": "alpha", "language_code": None}),
            FakeResponse({"text": "beta", "language_code": "ta-IN"}),
            FakeResponse({"text": "gamma", "language_code": "hi-IN"}),
        ]
    )
    transcript = DictateTranscriber(make_config(), client=client).transcribe(source)
    assert transcript.language_code == "ta-IN"


def test_no_chunk_reporting_a_language_yields_none(make_config, wav):
    client = FakeClient([FakeResponse({"text": "hello"})])
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.language_code is None


def test_chunk_texts_are_stripped_and_empty_ones_dropped(make_config, wav):
    client = FakeClient(
        [
            FakeResponse({"text": "  alpha  "}),
            FakeResponse({"text": "   "}),
            FakeResponse({"text": " beta "}),
        ]
    )
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(60.0))
    assert transcript.text == "alpha beta"
    assert transcript.segments == 3


def test_the_chunk_directory_is_removed_after_transcription(make_config, wav):
    def chunk_dirs():
        return {path for path in Path(tempfile.gettempdir()).glob("sarvam-chunks-*")}

    before = chunk_dirs()
    DictateTranscriber(make_config(), client=FakeClient()).transcribe(wav(60.0))
    assert chunk_dirs() == before


# --------------------------------------------------------------------------
# the form parts
# --------------------------------------------------------------------------


def test_unset_settings_are_omitted_entirely_never_sent_as_empty_strings(
    make_config, wav
):
    client = FakeClient()
    DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))

    call = client.calls[0]
    # Only the mode is always there: everything else was left unset.
    assert set(call.data) == {"mode"}
    assert "" not in call.data.values()
    assert call.json_parts == {}
    assert "style" not in call.data
    assert "vocabulary" not in call.data
    assert "context" not in call.data
    assert "model" not in call.data
    assert "output_rules" not in call.data
    assert "language_code" not in call.data


def test_whitespace_only_settings_are_omitted_too(make_config, wav):
    config = make_config(
        dictate_style="   ",
        dictate_rules="\n",
        dictate_context="  ",
        dictate_reference="\t",
        dictate_output_rules="  ",
        dictate_model=" ",
        dictate_vocabulary=("", "   "),
    )
    client = FakeClient()
    DictateTranscriber(config, client=client).transcribe(wav(1.0))
    assert set(client.calls[0].data) == {"mode"}


def test_the_form_carries_exactly_the_configured_settings(make_config, wav):
    config = make_config(
        mode="codemix",
        language_code="hi-IN",
        dictate_style="clean",
        dictate_rules="Use Oxford commas.",
        dictate_vocabulary=("Velqora", "Sarvam"),
        dictate_context="A sales call about the Q3 numbers.",
        dictate_reference="Velqora, Velcore, Sarvam.",
        dictate_output_rules="Summarise in one line.",
        dictate_model="dictate-v1-2026-11-04",
    )
    client = FakeClient()
    DictateTranscriber(config, client=client).transcribe(wav(1.0))

    data = client.calls[0].data
    assert set(data) == {
        "mode",
        "language_code",
        "output_rules",
        "model",
        "style",
        "vocabulary",
        "context",
    }
    assert data["mode"] == "codemix"
    assert data["language_code"] == "hi-IN"
    assert data["output_rules"] == "Summarise in one line."
    assert data["model"] == "dictate-v1-2026-11-04"
    assert "" not in data.values()


def test_style_vocabulary_and_context_are_sent_as_json_strings(make_config, wav):
    config = make_config(
        dictate_style="light",
        dictate_rules="Keep Hinglish as spoken.",
        dictate_vocabulary=("Velqora",),
        dictate_context="A sales call.",
        dictate_reference="Velqora, Velcore.",
    )
    client = FakeClient()
    DictateTranscriber(config, client=client).transcribe(wav(1.0))

    data = client.calls[0].data
    for part in ("style", "vocabulary", "context"):
        assert isinstance(data[part], str), part
    assert client.calls[0].json_parts["style"] == {
        "preset": "light",
        "rules": "Keep Hinglish as spoken.",
    }
    assert client.calls[0].json_parts["vocabulary"] == [{"word": "Velqora"}]
    assert client.calls[0].json_parts["context"] == {
        "text": "A sales call.",
        "reference": "Velqora, Velcore.",
    }


def test_the_model_is_sent_only_when_pinned(make_config, wav):
    pinned = FakeClient()
    DictateTranscriber(
        make_config(dictate_model="dictate-v1-2026-11-04"), client=pinned
    ).transcribe(wav(1.0))
    assert pinned.calls[0].data["model"] == "dictate-v1-2026-11-04"

    unpinned = FakeClient()
    DictateTranscriber(make_config(dictate_model=""), client=unpinned).transcribe(wav(1.0))
    assert "model" not in unpinned.calls[0].data


# --------------------------------------------------------------------------
# response handling
# --------------------------------------------------------------------------


def test_a_degraded_response_still_returns_the_text(make_config, wav):
    # `text` already holds the raw transcript when finishing did not run, so a
    # degraded clip is usable text, not an error.
    client = FakeClient(
        [
            FakeResponse(
                {
                    "text": "kal meeting hai",
                    "transcript": "kal meeting hai",
                    "degraded": "llm_timeout",
                    "language_code": "hi-IN",
                }
            )
        ]
    )
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.text == "kal meeting hai"
    assert transcript.segments == 1


@pytest.mark.parametrize("degraded", ["llm_error", "asr_timeout", "llm_guard_failed"])
def test_every_degraded_reason_is_informational_only(make_config, wav, degraded):
    client = FakeClient([FakeResponse({"text": "text", "degraded": degraded})])
    assert DictateTranscriber(make_config(), client=client).transcribe(wav(1.0)).text == "text"


def test_an_empty_text_is_an_empty_transcript_not_an_error(make_config, wav):
    client = FakeClient([FakeResponse({"text": "", "language_code": None, "degraded": None})])
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.text == ""
    assert transcript.segments == 1


def test_the_output_object_is_ignored_because_we_type_the_text(make_config, wav):
    client = FakeClient(
        [
            FakeResponse(
                {
                    "text": "Please send the Q3 numbers by Friday.",
                    "output": {"text": "SUMMARY: Q3 numbers due Friday."},
                }
            )
        ]
    )
    config = make_config(dictate_output_rules="Summarise in one line.")
    transcript = DictateTranscriber(config, client=client).transcribe(wav(1.0))
    assert transcript.text == "Please send the Q3 numbers by Friday."
    assert "SUMMARY" not in transcript.text


def test_a_body_without_text_falls_back_to_the_raw_transcript(make_config, wav):
    client = FakeClient([FakeResponse({"transcript": "raw words"})])
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.text == "raw words"


def test_a_non_json_body_on_a_success_status_is_an_api_error(make_config, wav):
    """A 2xx that is not a JSON object is a proxy page or a truncated body, not
    an empty dictation: typing nothing would lose the audio without a word.
    """
    client = FakeClient([FakeResponse(ValueError("not json"))])

    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))

    assert excinfo.value.code == "api"
    assert "readable JSON" in str(excinfo.value)


# --------------------------------------------------------------------------
# error mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_maps_to_api_and_scrubs_the_key(make_config, wav, status):
    key = "super-secret-key-that-must-not-leak"
    client = FakeClient(
        [FakeResponse({"error": {"message": f"invalid key {key}", "code": "invalid_request_error"}}, status)]
    )

    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(api_key=key), client=client).transcribe(wav(1.0))

    error = excinfo.value
    assert error.code == "api"
    assert str(status) in str(error)
    assert key not in str(error)
    assert "***" in str(error)


def test_a_rate_limit_maps_to_api(make_config, wav):
    client = FakeClient([FakeResponse({"error": {"message": "slow down"}}, 429)])
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert excinfo.value.code == "api"
    assert "429" in str(excinfo.value)
    assert "rate limit" in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 413, 500, 502, 503])
def test_any_other_http_failure_maps_to_api_with_the_status_in_the_message(
    make_config, wav, status
):
    client = FakeClient([FakeResponse({"error": {"message": "nope"}}, status)])
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert excinfo.value.code == "api"
    assert f"HTTP {status}" in str(excinfo.value)
    assert "nope" in str(excinfo.value)


def test_an_error_body_without_a_message_still_names_the_status(make_config, wav):
    client = FakeClient([FakeResponse({"unexpected": True}, 500)])
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert excinfo.value.code == "api"
    assert "HTTP 500" in str(excinfo.value)


@pytest.mark.parametrize(
    "failure",
    [
        ReadTimeout("read timed out"),
        ConnectionError("connection reset by peer"),
        OSError(104, "connection reset by peer"),
    ],
)
def test_a_transport_failure_maps_to_network(make_config, wav, failure):
    client = FakeClient(error=failure)
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert excinfo.value.code == "network"


@pytest.mark.parametrize(
    "failure",
    [
        ReadTimeout("read timed out"),
        ConnectionError("connection reset by peer"),
        ValueError("weird payload"),
    ],
)
def test_errors_never_leak_the_api_key(make_config, wav, failure):
    key = "super-secret-key-that-must-not-leak"
    client = FakeClient(error=RuntimeError(f"{failure} while sending {key}"))
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(api_key=key), client=client).transcribe(wav(1.0))
    assert key not in str(excinfo.value)


def test_an_unexpected_failure_maps_to_internal(make_config, wav):
    client = FakeClient(error=ValueError("weird payload"))
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert excinfo.value.code == "internal"
    assert "weird payload" in str(excinfo.value)


def test_every_code_this_module_raises_is_from_the_documented_set():
    assert ALLOWED_CODES == {"no_api_key", "config_invalid", "network", "api", "internal"}
    assert TranscriptionError("x", code="config_invalid").code in ALLOWED_CODES


# --------------------------------------------------------------------------
# payload builders (shared with the live-socket unit)
# --------------------------------------------------------------------------


def test_style_payload_carries_preset_and_rules(make_config):
    config = make_config(dictate_style="clean", dictate_rules="Use Oxford commas.")
    assert style_payload(config) == {"preset": "clean", "rules": "Use Oxford commas."}


def test_style_payload_omits_the_unset_member(make_config):
    assert style_payload(make_config(dictate_style="light", dictate_rules="")) == {
        "preset": "light"
    }
    assert style_payload(make_config(dictate_style="", dictate_rules="Be terse.")) == {
        "rules": "Be terse."
    }


def test_style_payload_is_none_when_both_members_are_unset(make_config):
    assert style_payload(make_config(dictate_style="", dictate_rules="")) is None
    assert style_payload(make_config(dictate_style="  ", dictate_rules="\n")) is None


def test_vocabulary_payload_wraps_each_term_as_a_word_entry(make_config):
    config = make_config(dictate_vocabulary=("Velqora", "सर्वम"))
    assert vocabulary_payload(config) == [{"word": "Velqora"}, {"word": "सर्वम"}]


def test_vocabulary_payload_is_none_when_the_list_is_empty(make_config):
    assert vocabulary_payload(make_config(dictate_vocabulary=())) is None
    assert vocabulary_payload(make_config(dictate_vocabulary=("", " "))) is None


def test_context_payload_carries_text_and_reference(make_config):
    config = make_config(dictate_context="A sales call.", dictate_reference="Velqora, Velcore.")
    assert context_payload(config) == {
        "text": "A sales call.",
        "reference": "Velqora, Velcore.",
    }


def test_context_payload_omits_the_unset_member(make_config):
    assert context_payload(make_config(dictate_context="A sales call.", dictate_reference="")) == {
        "text": "A sales call."
    }
    assert context_payload(make_config(dictate_context="", dictate_reference="Velqora.")) == {
        "reference": "Velqora."
    }


def test_context_payload_is_none_when_both_members_are_unset(make_config):
    assert context_payload(make_config(dictate_context="", dictate_reference="")) is None


def test_the_builders_accept_a_config_with_every_dictate_field_left_unset():
    # The builders are imported by the live-socket unit and must not depend on
    # anything beyond the frozen configuration surface.
    assert style_payload(Config(api_key="k")) is None
    assert vocabulary_payload(Config(api_key="k")) is None
    assert context_payload(Config(api_key="k")) is None

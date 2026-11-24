"""Dictate clip tests: ``dictate.py`` at the ``sarvamai`` SDK boundary.

Every test injects a hand-written fake client whose shape mirrors the SDK —
``client.dictate.transcribe(**kwargs)`` — and which records the keyword
arguments it was handed, so nothing here touches the real API and nothing here
asserts on a hand-built wire. The one subprocess test blocks ``sarvamai`` at
import time, which is how the "the module imports on a machine without the SDK"
rule is actually proved rather than assumed; the file therefore never imports
``sarvamai`` itself and the suite runs without it installed.

Audio files are built with the stdlib ``wave`` module inside ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sarvam_dictation.config import DICTATE_MODES, Config
from sarvam_dictation.dictate import (
    CLIP_MAX_SECONDS,
    CLIP_TIMEOUT_S,
    DictateTranscriber,
    context_payload,
    style_payload,
    vocabulary_payload,
)
from sarvam_dictation.transcriber import MAX_CHUNK_SECONDS, TranscriptionError, Transcript, split_wav

ALLOWED_CODES = {"no_api_key", "config_invalid", "network", "api", "internal"}

#: The keys ``transcribe`` always receives: the mode and the per-request
#: options. Everything else is an optional setting and must be absent when unset.
ALWAYS_SENT = {"mode", "request_options"}


# --------------------------------------------------------------------------
# fakes: the SDK's shape, not the wire's
# --------------------------------------------------------------------------


@dataclass
class DictateResponseLike:
    """A stand-in for ``sarvamai.types.DictateResponse``.

    The field list is the SDK model's own — every one of them optional here so a
    test names only the fields it cares about. Deliberately a plain dataclass:
    importing the SDK to subclass its model would make the suite depend on it.
    """

    request_id: str = "req-1"
    model: str = "dictate-v1-2026-11-04"
    transcript: str | None = None
    text: str | None = None
    language_code: str | None = "hi-IN"
    corrections: list = field(default_factory=list)
    degraded: str | None = None
    usage: dict = field(default_factory=dict)
    output: object = None


class ApiError(Exception):
    """Shaped like ``sarvamai.core.api_error.ApiError`` (and its subclasses).

    Named ``ApiError`` and carrying ``status_code``/``body``/``headers`` because
    the module classifies by duck-typing: the real SDK class is never imported.
    """

    def __init__(self, *, status_code=None, body=None, headers=None):
        self.status_code = status_code
        self.body = body
        self.headers = {} if headers is None else headers
        super().__init__(f"status_code: {status_code}, body: {body}")


class ReadTimeout(Exception):
    """Named like an ``httpx`` timeout so the shared classifier sees it."""


class RecordedCall:
    """One ``dictate.transcribe`` as the fake client saw it, snapshotted at call time."""

    def __init__(self, kwargs):
        self.kwargs = dict(kwargs)
        self.handle = self.kwargs.get("file")
        # The SDK reads the handle during the request, so it must still be open
        # here; snapshot the bytes and the liveness before it is closed.
        self.handle_open_at_call = self.handle is not None and not self.handle.closed
        self.content = b""
        if self.handle is not None and not self.handle.closed:
            self.handle.seek(0)
            self.content = self.handle.read()

    @property
    def options(self):
        """The keyword arguments minus the file part."""
        return {name: value for name, value in self.kwargs.items() if name != "file"}

    @property
    def timeout_in_seconds(self):
        return (self.kwargs.get("request_options") or {}).get("timeout_in_seconds")


class FakeDictate:
    """The ``client.dictate`` namespace: one ``transcribe`` method."""

    def __init__(self, owner: "FakeClient"):
        self._owner = owner

    def transcribe(self, **kwargs):
        self._owner.calls.append(RecordedCall(kwargs))
        if self._owner.error is not None:
            raise self._owner.error
        index = min(len(self._owner.calls) - 1, len(self._owner.responses) - 1)
        return self._owner.responses[index]


class FakeClient:
    """A ``SarvamAI``-like client that records calls and replays canned responses.

    Hand-written rather than a ``MagicMock`` so the assertions read as "the SDK
    was asked for this", and so the file part is captured while the handle is
    still open.
    """

    def __init__(self, responses=None, error: BaseException | None = None):
        self.responses = (
            list(responses)
            if responses
            else [DictateResponseLike(text="नमस्ते", language_code="hi-IN")]
        )
        self.error = error
        self.calls: list[RecordedCall] = []
        self.dictate = FakeDictate(self)


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
# the lazy SDK import
# --------------------------------------------------------------------------

_SUBPROCESS_CHECK = r"""
import pathlib, sys, tempfile, wave

class _BlockSarvam:
    def find_spec(self, name, path=None, target=None):
        if name == "sarvamai" or name.startswith("sarvamai."):
            raise ModuleNotFoundError("No module named 'sarvamai'", name="sarvamai")
        return None

sys.meta_path.insert(0, _BlockSarvam())
for _name in [n for n in list(sys.modules) if n == "sarvamai" or n.startswith("sarvamai.")]:
    del sys.modules[_name]

import sarvam_dictation.dictate as module
assert "sarvamai" not in sys.modules, "dictate imported the SDK at import time"

from sarvam_dictation.config import Config

directory = pathlib.Path(tempfile.mkdtemp(prefix="dictate-no-sdk-"))
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
    assert "sarvamai" in str(exc), str(exc)
    assert "sarvamai>=0.1.49" in str(exc), str(exc)
    assert "pip install" in str(exc), str(exc)
else:
    raise AssertionError("a missing sarvamai SDK did not raise")

assert "sarvamai" not in sys.modules, "the failed call still imported the SDK"
print("ok")
"""


def test_the_module_imports_and_reports_a_missing_sdk_on_a_machine_without_it():
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


def test_an_injected_client_never_imports_the_sdk(make_config, wav):
    if "sarvamai" in sys.modules:
        pytest.skip("sarvamai is already imported in this interpreter")
    DictateTranscriber(make_config(), client=FakeClient()).transcribe(wav(1.0))
    assert "sarvamai" not in sys.modules


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
def test_every_dictate_mode_is_passed_to_the_sdk_verbatim(make_config, wav, mode):
    client = FakeClient()
    DictateTranscriber(make_config(mode=mode), client=client).transcribe(wav(1.0))
    assert client.calls[0].kwargs["mode"] == mode


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


def test_a_short_clip_makes_exactly_one_sdk_call_and_returns_its_text(make_config, wav):
    client = FakeClient([DictateResponseLike(text="नमस्ते दुनिया।", language_code="hi-IN")])
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(2.0))

    assert len(client.calls) == 1
    assert transcript == Transcript(text="नमस्ते दुनिया।", language_code="hi-IN", segments=1)


def test_the_sdk_is_called_once_per_chunk_with_the_file_as_a_keyword(make_config, wav):
    client = FakeClient()
    DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))

    call = client.calls[0]
    assert "file" in call.kwargs
    assert call.handle is not None
    assert call.handle_open_at_call is True  # the SDK reads it during the request


def test_the_audio_is_handed_over_as_the_wav_chunk_itself(make_config, wav):
    source = wav(1.0)
    client = FakeClient()
    DictateTranscriber(make_config(), client=client).transcribe(source)

    call = client.calls[0]
    assert call.handle.name == str(source)
    assert call.content.startswith(b"RIFF")
    assert call.handle.closed is True  # closed once the call returned


def test_every_call_allows_for_the_slow_finishing_step(make_config, wav):
    # The API's finishing call can take up to 90 s, and the SDK's own default is
    # 60 s, so the route asks for its own longer timeout on every request.
    client = FakeClient()
    DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))

    timeout = client.calls[0].timeout_in_seconds
    assert timeout == int(CLIP_TIMEOUT_S)
    assert timeout >= 90


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def test_a_clip_longer_than_the_chunk_limit_is_sent_in_parts_and_joined(
    make_config, wav, tmp_path
):
    source = wav(60.0)
    expected = _chunk_count(source, tmp_path)
    assert expected >= 3
    client = FakeClient(
        [
            DictateResponseLike(text="alpha"),
            DictateResponseLike(text="beta"),
            DictateResponseLike(text="gamma"),
        ]
    )

    transcript = DictateTranscriber(make_config(), client=client).transcribe(source)

    assert len(client.calls) == expected
    assert transcript.segments == expected
    assert transcript.text == "alpha beta gamma"


def test_a_clip_that_fits_the_endpoint_limit_is_one_call(make_config, wav):
    # 29 s fits the 30 s ceiling, so splitting it would buy nothing.
    assert CLIP_MAX_SECONDS == 30.0
    assert MAX_CHUNK_SECONDS < CLIP_MAX_SECONDS
    client = FakeClient()
    DictateTranscriber(make_config(), client=client).transcribe(wav(29.0))
    assert len(client.calls) == 1


def test_each_chunk_is_sent_as_its_own_file(make_config, wav):
    client = FakeClient()
    DictateTranscriber(make_config(), client=client).transcribe(wav(60.0))

    names = [call.handle.name for call in client.calls]
    assert len(set(names)) == len(names)
    assert all(name.endswith(".wav") for name in names)


def test_the_reported_language_is_the_first_one_a_chunk_reported(make_config, wav):
    source = wav(60.0)
    client = FakeClient(
        [
            DictateResponseLike(text="alpha", language_code=None),
            DictateResponseLike(text="beta", language_code="ta-IN"),
            DictateResponseLike(text="gamma", language_code="hi-IN"),
        ]
    )
    transcript = DictateTranscriber(make_config(), client=client).transcribe(source)
    assert transcript.language_code == "ta-IN"


def test_no_chunk_reporting_a_language_yields_none(make_config, wav):
    client = FakeClient([DictateResponseLike(text="hello", language_code=None)])
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.language_code is None


def test_chunk_texts_are_stripped_and_empty_ones_dropped(make_config, wav):
    client = FakeClient(
        [
            DictateResponseLike(text="  alpha  "),
            DictateResponseLike(text="   "),
            DictateResponseLike(text=" beta "),
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
# the options handed to the SDK
# --------------------------------------------------------------------------


def test_unset_settings_are_omitted_entirely_never_passed_as_none(make_config, wav):
    client = FakeClient()
    DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))

    options = client.calls[0].options
    # Only the mode and the per-request options are always there: everything
    # else was left unset, and None is not a legal value on this route.
    assert set(options) == ALWAYS_SENT
    assert None not in options.values()
    for name in ("style", "vocabulary", "context", "model", "output_rules", "language_code"):
        assert name not in options


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
    assert set(client.calls[0].options) == ALWAYS_SENT


def test_the_call_carries_exactly_the_configured_settings(make_config, wav):
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

    options = client.calls[0].options
    assert set(options) == ALWAYS_SENT | {
        "language_code",
        "output_rules",
        "model",
        "style",
        "vocabulary",
        "context",
    }
    assert options["mode"] == "codemix"
    assert options["language_code"] == "hi-IN"
    assert options["output_rules"] == "Summarise in one line."
    assert options["model"] == "dictate-v1-2026-11-04"
    assert "" not in options.values()


def test_style_vocabulary_and_context_are_the_payload_builders_output(make_config, wav):
    config = make_config(
        dictate_style="light",
        dictate_rules="Keep Hinglish as spoken.",
        dictate_vocabulary=("Velqora",),
        dictate_context="A sales call.",
        dictate_reference="Velqora, Velcore.",
    )
    client = FakeClient()
    DictateTranscriber(config, client=client).transcribe(wav(1.0))

    options = client.calls[0].options
    # The SDK serialises a dict to the JSON text part the contract documents, so
    # the builders' return value is handed over unchanged.
    assert options["style"] == {"preset": "light", "rules": "Keep Hinglish as spoken."}
    assert options["vocabulary"] == [{"word": "Velqora"}]
    assert options["context"] == {"text": "A sales call.", "reference": "Velqora, Velcore."}
    assert options["style"] == style_payload(config)
    assert options["vocabulary"] == vocabulary_payload(config)
    assert options["context"] == context_payload(config)


def test_the_model_is_sent_only_when_pinned(make_config, wav):
    pinned = FakeClient()
    DictateTranscriber(
        make_config(dictate_model="dictate-v1-2026-11-04"), client=pinned
    ).transcribe(wav(1.0))
    assert pinned.calls[0].kwargs["model"] == "dictate-v1-2026-11-04"

    unpinned = FakeClient()
    DictateTranscriber(make_config(dictate_model=""), client=unpinned).transcribe(wav(1.0))
    assert "model" not in unpinned.calls[0].kwargs


def test_an_empty_language_code_is_omitted_so_the_api_autodetects(make_config, wav):
    client = FakeClient()
    DictateTranscriber(make_config(language_code=""), client=client).transcribe(wav(1.0))
    assert "language_code" not in client.calls[0].kwargs


# --------------------------------------------------------------------------
# response handling
# --------------------------------------------------------------------------


def test_a_degraded_response_still_returns_the_text(make_config, wav):
    # `text` already holds the raw transcript when finishing did not run, so a
    # degraded clip is usable text, not an error.
    client = FakeClient(
        [
            DictateResponseLike(
                text="kal meeting hai",
                transcript="kal meeting hai",
                degraded="llm_timeout",
                language_code="hi-IN",
            )
        ]
    )
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.text == "kal meeting hai"
    assert transcript.segments == 1


@pytest.mark.parametrize("degraded", ["llm_error", "asr_timeout", "llm_guard_failed"])
def test_every_degraded_reason_is_informational_only(make_config, wav, degraded):
    client = FakeClient([DictateResponseLike(text="text", degraded=degraded)])
    assert DictateTranscriber(make_config(), client=client).transcribe(wav(1.0)).text == "text"


def test_an_empty_text_is_an_empty_transcript_not_an_error(make_config, wav):
    client = FakeClient([DictateResponseLike(text="", language_code=None, degraded=None)])
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.text == ""
    assert transcript.segments == 1


def test_an_empty_text_is_kept_even_when_a_transcript_is_present(make_config, wav):
    # An empty dictation is a real answer for a clip of background noise; the
    # raw transcript must not be substituted for it.
    client = FakeClient([DictateResponseLike(text="", transcript="raw words")])
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.text == ""


def test_the_output_object_is_ignored_because_we_type_the_text(make_config, wav):
    client = FakeClient(
        [
            DictateResponseLike(
                text="Please send the Q3 numbers by Friday.",
                output={"text": "SUMMARY: Q3 numbers due Friday."},
            )
        ]
    )
    config = make_config(dictate_output_rules="Summarise in one line.")
    transcript = DictateTranscriber(config, client=client).transcribe(wav(1.0))
    assert transcript.text == "Please send the Q3 numbers by Friday."
    assert "SUMMARY" not in transcript.text


def test_a_response_without_text_falls_back_to_the_raw_transcript(make_config, wav):
    client = FakeClient([DictateResponseLike(text=None, transcript="raw words")])
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.text == "raw words"


def test_a_response_with_neither_text_nor_transcript_is_empty(make_config, wav):
    client = FakeClient([DictateResponseLike(text=None, transcript=None)])
    transcript = DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert transcript.text == ""


# --------------------------------------------------------------------------
# error mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_maps_to_api_and_scrubs_the_key(make_config, wav, status):
    key = "super-secret-key-that-must-not-leak"
    client = FakeClient(
        error=ApiError(
            status_code=status,
            body={"error": {"message": f"invalid key {key}", "code": "invalid_request_error"}},
        )
    )

    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(api_key=key), client=client).transcribe(wav(1.0))

    error = excinfo.value
    assert error.code == "api"
    assert str(status) in str(error)
    assert key not in str(error)
    assert "***" in str(error)


def test_a_rate_limit_maps_to_api(make_config, wav):
    client = FakeClient(error=ApiError(status_code=429, body={"error": {"message": "slow down"}}))
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert excinfo.value.code == "api"
    assert "429" in str(excinfo.value)
    assert "rate limit" in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 413, 500, 502, 503])
def test_any_other_http_failure_maps_to_api_with_the_status_in_the_message(
    make_config, wav, status
):
    client = FakeClient(error=ApiError(status_code=status, body={"error": {"message": "nope"}}))
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert excinfo.value.code == "api"
    assert f"HTTP {status}" in str(excinfo.value)
    assert "nope" in str(excinfo.value)


@pytest.mark.parametrize("body", [{"unexpected": True}, None, "not json at all", [1, 2]])
def test_an_error_body_without_a_message_still_names_the_status(make_config, wav, body):
    client = FakeClient(error=ApiError(status_code=500, body=body))
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert excinfo.value.code == "api"
    assert "HTTP 500" in str(excinfo.value)


def test_an_api_error_does_not_dump_the_response_headers_into_the_message(make_config, wav):
    # ApiError.__str__ prints the whole header dict, which is noise in a desktop
    # notification; the documented error.message is preferred when it is there.
    client = FakeClient(
        error=ApiError(
            status_code=400,
            body={"error": {"message": "mode not supported"}},
            headers={"x-request-id": "abc"},
        )
    )
    with pytest.raises(TranscriptionError) as excinfo:
        DictateTranscriber(make_config(), client=client).transcribe(wav(1.0))
    assert "mode not supported" in str(excinfo.value)
    assert "x-request-id" not in str(excinfo.value)


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

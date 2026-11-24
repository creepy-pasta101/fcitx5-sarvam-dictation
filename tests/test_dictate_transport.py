"""Transport-level tests for the Dictate clip route.

Every other test of :mod:`sarvam_dictation.dictate` injects a hand-written fake
client, which proves the bookkeeping but never exercises the real encoding: the
multipart body, the boundary, the JSON-text parts, the header, the timeout. This
module drives the real module through a real ``httpx`` client wired to a mock
transport, so a request that would be refused with a 400 for being malformed
fails here instead of in production.

It needs ``httpx``, which the test-suite does not otherwise require, so the whole
module skips when the package is absent. ``httpx`` is an optional runtime
dependency of the Dictate engine only.
"""

from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import HTTP

import pytest

httpx = pytest.importorskip("httpx")

from sarvam_dictation.dictate import DictateTranscriber  # noqa: E402


def _parts(request: httpx.Request) -> dict[str, list[tuple[str | None, bytes]]]:
    """Parse a multipart request body into ``{field name: [(filename, value)]}``."""
    content_type = request.headers["content-type"]
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
    )
    parts: dict[str, list[tuple[str | None, bytes]]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        parts.setdefault(name, []).append((filename, part.get_payload(decode=True)))
    return parts


def _client(seen: list, status: int = 200, body: dict | None = None, raw: bytes | None = None):
    """An ``httpx.Client`` whose transport records requests and replays a reply."""
    payload = body if body is not None else {
        "request_id": "req",
        "model": "dictate-v1-2026-11-04",
        "transcript": "raw words",
        "text": "Finished words.",
        "language_code": "en-IN",
        "corrections": [],
        "degraded": None,
        "usage": {"audio_duration_s": 1.0, "words": 2,
                  "llm_input_tokens": None, "llm_output_tokens": None},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if raw is not None:
            return httpx.Response(status, content=raw,
                                  headers={"content-type": "text/html"})
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_request_is_a_well_formed_multipart_post(make_config, write_wav, tmp_path):
    seen: list = []
    config = make_config(
        engine="dictate",
        dictate_style="clean",
        dictate_rules="Write rupees as ₹.",
        dictate_vocabulary=("Velqora", "Vyshnavi Raghunathan"),
        dictate_context="a support call about a fiber connection",
        dictate_reference="Velqora Fiber\nVyshnavi Raghunathan",
        dictate_output_rules="Return JSON.",
        dictate_model="dictate-v1-2026-11-04",
    )
    audio = write_wav(tmp_path / "clip.wav", duration_s=1.0)

    DictateTranscriber(config, client=_client(seen)).transcribe(audio)

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.sarvam.ai/v2/dictate"
    assert request.headers["api-subscription-key"] == config.api_key
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")

    parts = _parts(request)
    # The file part is a file; everything else is text, and the three structured
    # options arrive as JSON *strings* the API parses itself.
    filename, payload = parts["file"][0]
    assert filename == "clip.wav"
    assert payload.startswith(b"RIFF")

    def text_of(name: str) -> str:
        return parts[name][0][1].decode()

    assert text_of("mode") == "transcribe"
    assert json.loads(text_of("style")) == {
        "preset": "clean",
        "rules": "Write rupees as ₹.",
    }
    assert json.loads(text_of("vocabulary")) == [
        {"word": "Velqora"},
        {"word": "Vyshnavi Raghunathan"},
    ]
    assert json.loads(text_of("context")) == {
        "text": "a support call about a fiber connection",
        "reference": "Velqora Fiber\nVyshnavi Raghunathan",
    }
    assert text_of("output_rules") == "Return JSON."
    assert text_of("model") == "dictate-v1-2026-11-04"
    # `language_code` is empty in this config, and an empty part is a 400.
    assert "language_code" not in parts


def test_an_unset_option_is_absent_rather_than_sent_empty(make_config, write_wav, tmp_path):
    seen: list = []
    config = make_config(engine="dictate")
    audio = write_wav(tmp_path / "clip.wav", duration_s=1.0)

    DictateTranscriber(config, client=_client(seen)).transcribe(audio)

    parts = _parts(seen[0])
    for name in ("language_code", "style", "vocabulary", "context",
                 "output_rules", "model"):
        assert name not in parts, f"{name} was sent although it is unset"
    assert all(value != b"" for entries in parts.values() for _, value in entries)


def test_a_clip_that_fits_the_limit_is_one_request(make_config, write_wav, tmp_path):
    """29 s is under the API's 30 s ceiling, so splitting it would only cost a
    second finishing call."""
    seen: list = []
    audio = write_wav(tmp_path / "long.wav", duration_s=29.0)

    transcript = DictateTranscriber(make_config(engine="dictate"),
                                    client=_client(seen)).transcribe(audio)

    assert len(seen) == 1
    assert transcript.segments == 1


def test_a_clip_over_the_limit_is_split_and_joined(make_config, write_wav, tmp_path):
    seen: list = []
    audio = write_wav(tmp_path / "verylong.wav", duration_s=65.0)

    transcript = DictateTranscriber(make_config(engine="dictate"),
                                    client=_client(seen)).transcribe(audio)

    assert len(seen) == 3  # 28 + 28 + 9
    assert transcript.segments == 3
    assert transcript.text.count("Finished words.") == 3


def test_a_response_that_is_not_json_is_an_error_not_an_empty_transcript(
    make_config, write_wav, tmp_path
):
    """A proxy page or a truncated body with a 200 would otherwise be typed as
    nothing, losing the user's audio without a word."""
    from sarvam_dictation.transcriber import TranscriptionError

    audio = write_wav(tmp_path / "clip.wav", duration_s=1.0)
    client = _client([], status=200, raw=b"<html>gateway</html>")

    with pytest.raises(TranscriptionError) as raised:
        DictateTranscriber(make_config(engine="dictate"), client=client).transcribe(audio)

    assert raised.value.code == "api"
    assert "readable JSON" in str(raised.value)

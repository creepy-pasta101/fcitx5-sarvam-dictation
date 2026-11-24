"""What the SDK actually puts on the wire for a Dictate clip.

The unit tests inject a fake SDK client, which proves the arguments the daemon
chooses but not what the SDK does with them. That gap hid a real trap: this SDK
does **not** drop an explicit ``None``, so an option passed as ``None`` is
encoded into the multipart body — as the literal string ``null`` for the JSON
parts (a documented 400) and as a bare part for the text parts. ``dictate.py``
therefore omits unset options instead of passing ``None``, and this module pins
that down through the real SDK, a real ``httpx`` client and a mock transport.

Needs ``sarvamai`` and ``httpx``, so it skips on a machine without them.
"""

from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import HTTP

import pytest

pytest.importorskip("httpx")
pytest.importorskip("sarvamai")

import httpx  # noqa: E402
from sarvamai import SarvamAI  # noqa: E402

from sarvam_dictation.dictate import DictateTranscriber  # noqa: E402

RESPONSE = {
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


def _parts(request: httpx.Request) -> dict[str, list[tuple[str | None, bytes]]]:
    """Parse a multipart body into ``{field name: [(filename, value)]}``."""
    content_type = request.headers["content-type"]
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
    )
    parts: dict[str, list[tuple[str | None, bytes]]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts.setdefault(name, []).append(
            (part.get_filename(), part.get_payload(decode=True))
        )
    return parts


def _sdk_client(seen: list) -> SarvamAI:
    """A real SDK client whose HTTP transport records and replays."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=RESPONSE)

    return SarvamAI(
        api_subscription_key="test-key-not-real",
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _configured(make_config):
    return make_config(
        engine="dictate",
        mode="codemix",
        language_code="hi-IN",
        dictate_style="clean",
        dictate_rules="Write rupees as ₹.",
        dictate_vocabulary=("Velqora", "Vyshnavi Raghunathan"),
        dictate_context="a support call",
        dictate_reference="Velqora Fiber",
        dictate_output_rules="Return JSON.",
        dictate_model="dictate-v1-2026-11-04",
    )


def test_the_sdk_sends_a_well_formed_multipart_request(make_config, write_wav, tmp_path):
    seen: list = []
    audio = write_wav(tmp_path / "clip.wav", duration_s=1.0)

    transcript = DictateTranscriber(
        _configured(make_config), client=_sdk_client(seen)
    ).transcribe(audio)

    assert transcript.text == "Finished words."
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v2/dictate"
    assert request.headers["api-subscription-key"] == "test-key-not-real"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")

    parts = _parts(request)
    filename, payload = parts["file"][0]
    assert filename == "clip.wav"
    assert payload.startswith(b"RIFF")

    def text_of(name: str) -> str:
        return parts[name][0][1].decode()

    assert text_of("mode") == "codemix"
    assert text_of("language_code") == "hi-IN"
    # The three structured options travel as JSON *strings* the API parses.
    assert json.loads(text_of("style")) == {
        "preset": "clean",
        "rules": "Write rupees as ₹.",
    }
    assert json.loads(text_of("vocabulary")) == [
        {"word": "Velqora"},
        {"word": "Vyshnavi Raghunathan"},
    ]
    assert json.loads(text_of("context")) == {
        "text": "a support call",
        "reference": "Velqora Fiber",
    }
    assert text_of("output_rules") == "Return JSON."
    assert text_of("model") == "dictate-v1-2026-11-04"


def test_an_unset_option_never_reaches_the_wire_as_null(make_config, write_wav, tmp_path):
    """The trap this module exists for: ``None`` is not "omit" for this SDK."""
    seen: list = []
    audio = write_wav(tmp_path / "clip.wav", duration_s=1.0)

    DictateTranscriber(make_config(engine="dictate"), client=_sdk_client(seen)).transcribe(audio)

    request = seen[0]
    body = request.content
    parts = _parts(request)

    assert b"null" not in body, "an unset option was serialised as null"
    for name in ("language_code", "style", "vocabulary", "context",
                 "output_rules", "model"):
        assert name not in parts, f"{name} was sent although it is unset"
    assert parts["mode"][0][1] == b"transcribe"

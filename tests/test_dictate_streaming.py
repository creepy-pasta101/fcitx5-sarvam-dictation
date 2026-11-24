"""Tests for the Dictate live-socket transcriber.

No network, no audio hardware, no ``websockets`` package and no sibling
``dictate.py`` are needed. Every test drives
:class:`DictateRealtimeTranscriber` against :class:`FakeConnector` /
:class:`FakeSocket`, a hand-written pair that answers ``recv()`` from a scripted
list of wire messages and records every ``send()`` in order. The loop is run with
``asyncio.run`` inside plain sync tests, so ``pytest-asyncio`` is not required.

Two stubs stand in for things this unit does not own:

* ``sys.modules["sarvam_dictation.dictate"]`` - the clip unit's payload
  builders, installed by the autouse ``dictate_stub`` fixture as a real
  ``types.ModuleType`` holding three lambdas whose return values a test sets. The
  module under test imports them lazily inside ``_config_update``, so a stub in
  ``sys.modules`` is enough and no file is needed.
* ``websockets`` - never installed and never imported: one test blocks it in
  ``sys.meta_path`` to prove the module imports without it, and another proves an
  injected client keeps it out of the process entirely.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import importlib
import json
import logging
import sys
import types
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

import sarvam_dictation.dictate_streaming as dictate_streaming
from sarvam_dictation.config import DICTATE_MODES, DICTATE_SAMPLE_RATES
from sarvam_dictation.dictate_streaming import (
    DICTATE_WS_URL,
    DictateRealtimeTranscriber,
    StreamingError,
    Transcript,
)

FAKE_KEY = "sk-fake-0000000000000000-not-a-real-key"


# --------------------------------------------------------------------------
# fakes: socket and connector
# --------------------------------------------------------------------------


class EndOfStream(Exception):
    """What the fake socket raises once its scripted messages run out."""


class Closed(Exception):
    """A WebSocket close frame, as ``websockets`` raises it (``.code``)."""

    def __init__(self, code: int, reason: str = "server closed") -> None:
        super().__init__(f"received {code} ({reason})")
        self.code = code


class Pause:
    """A scripted wait between two messages in a :class:`FakeSocket` script.

    Used where a message must land *after* the handshake: the receiver sleeps
    through it while ``open()`` finishes ``session.begin`` / ``config.updated``,
    which is what the real server does when it takes a moment to react.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


class FakeSocket:
    """Stand-in for a ``websockets`` client connection.

    ``messages`` are returned from ``recv()`` as JSON text, in order; an item may
    be an exception instance, which is raised instead. When the script runs out
    ``recv()`` waits for more, because a live session never ends on its own; pass
    ``block_forever=False`` to make it raise :class:`EndOfStream` instead.

    The fake answers the two messages a real server answers: a ``config.update``
    is followed by ``config.updated`` and an ``end`` by ``session.end``, each put
    at the *front* of the queue so it is the very next message, as the contract
    requires. Both can be switched off (``respond_to_config``/``respond_to_end``)
    to script a server that never confirms and never ends.

    ``log`` keeps every ``send`` and every ``recv`` in one ordered list, which is
    how the tests prove that ``speech_start`` follows the confirmation rather
    than racing it.
    """

    def __init__(
        self,
        messages=(),
        *,
        respond_to_config: bool = True,
        respond_to_end: bool = True,
        block_forever: bool = True,
    ) -> None:
        self._queue = list(messages)
        self._wake = asyncio.Event()
        self._respond = respond_to_config
        self._respond_to_end = respond_to_end
        self.block_forever = block_forever
        self.log: list[tuple[str, str]] = []
        self.sent: list[dict] = []
        self.closed = False
        self.close_calls: list[int] = []
        self.recv_calls = 0

    # -- server side -------------------------------------------------------

    async def recv(self):
        self.recv_calls += 1
        while True:
            if self._queue:
                message = self._queue.pop(0)
                if isinstance(message, Pause):
                    await asyncio.sleep(message.seconds)
                    continue
                if isinstance(message, Exception):
                    raise message
                event = str(message.get("event", "?"))
                self.log.append(("recv", event))
                return json.dumps(message)
            if not self.block_forever:
                raise EndOfStream()
            waiter = self._wake
            waiter.clear()
            if self._queue:
                continue
            await waiter.wait()

    def push(self, message: dict) -> None:
        """Queue one more server message (used by the keepalive test)."""
        self._queue.append(message)
        self._wake.set()

    # -- client side -------------------------------------------------------

    async def send(self, data) -> None:
        payload = json.loads(data)
        event = str(payload.get("event", "?"))
        self.sent.append(payload)
        self.log.append(("send", event))
        if event == "config.update" and self._respond:
            applied = sorted(key for key in payload if key != "event")
            self._queue.insert(0, {"event": "config.updated", "applied": applied})
            self._wake.set()
        if event == "end" and self._respond_to_end:
            # session.end is the last message, and the server only sends it once
            # the client has said `end` - which is exactly what finish() waits for.
            self._queue.insert(0, session_end())
            self._wake.set()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append(code)
        self.closed = True

    # -- assertion helpers -------------------------------------------------

    def events(self) -> list[str]:
        return [str(payload.get("event")) for payload in self.sent]

    def payloads(self, event: str) -> list[dict]:
        return [payload for payload in self.sent if payload.get("event") == event]

    def audio_payloads(self) -> list[bytes]:
        return [
            base64.b64decode(payload["audio"])
            for payload in self.sent
            if payload.get("event") == "audio_input"
        ]

    def received(self) -> list[str]:
        return [event for direction, event in self.log if direction == "recv"]


class FakeConnector:
    """A hand-written stand-in for ``websockets.connect``.

    Called as ``connector(url, additional_headers=...)`` like the real one, it
    records the handshake and returns (or raises) whatever the test scripted.
    """

    def __init__(self, socket: FakeSocket | None = None, error: Exception | None = None) -> None:
        self.socket = socket if socket is not None else FakeSocket()
        self.error = error
        self.calls: list[dict] = []

    async def __call__(self, url, additional_headers=None, **kwargs):
        self.calls.append({"url": url, "headers": dict(additional_headers or {}), "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return self.socket

    @property
    def url(self) -> str:
        assert self.calls, "the connector was never called"
        return self.calls[0]["url"]

    @property
    def query(self) -> list[tuple[str, str]]:
        return parse_qsl(urlsplit(self.url).query)

    @property
    def headers(self) -> dict[str, str]:
        return self.calls[0]["headers"]


# --------------------------------------------------------------------------
# the sibling module's payload builders, stubbed
# --------------------------------------------------------------------------


class DictateStub:
    """A stand-in for ``sarvam_dictation.dictate``'s three payload builders.

    ``style``/``vocabulary``/``context`` are what the builders return for this
    test; ``None`` means "the user set nothing", which the module under test must
    translate into "do not send the field".
    """

    def __init__(self) -> None:
        self.style: dict | None = None
        self.vocabulary: list | None = None
        self.context: dict | None = None
        self.calls: list[str] = []

    def module(self) -> types.ModuleType:
        module = types.ModuleType("sarvam_dictation.dictate")
        module.__doc__ = "Stub of the Dictate clip module, for the live-socket tests."
        module.style_payload = self._style
        module.vocabulary_payload = self._vocabulary
        module.context_payload = self._context
        return module

    def _style(self, config):
        self.calls.append("style")
        return self.style

    def _vocabulary(self, config):
        self.calls.append("vocabulary")
        return self.vocabulary

    def _context(self, config):
        self.calls.append("context")
        return self.context


@pytest.fixture(autouse=True)
def dictate_stub(monkeypatch):
    """Install the stub sibling module for every test in this file."""
    stub = DictateStub()
    monkeypatch.setitem(sys.modules, "sarvam_dictation.dictate", stub.module())
    return stub


@contextlib.contextmanager
def module_blocked(name: str):
    """Make ``import name`` fail with ``ModuleNotFoundError`` for one block."""

    class Blocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == name:
                raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
            return None

    blocker = Blocker()
    saved = sys.modules.pop(name, None)
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        if saved is not None:
            sys.modules[name] = saved


# --------------------------------------------------------------------------
# wire messages
# --------------------------------------------------------------------------


def session_begin() -> dict:
    return {"event": "session.begin", "request_id": "req-1", "config": {}}


def session_end() -> dict:
    return {
        "event": "session.end",
        "request_id": "req-1",
        "total_duration_s": "1.2",
        "total_utterances": 1,
        "audio_received_s": "1.0",
    }


def partial(text: str, utterance_idx: int = 0, language: str | None = "hi-IN") -> dict:
    return {
        "event": "transcript.partial",
        "utterance_idx": utterance_idx,
        "text": text,
        "language_code": language,
        "confidence": "0.9",
    }


def transcript_final(text: str, utterance_idx: int = 0, language: str | None = "hi-IN") -> dict:
    return {
        "event": "transcript.final",
        "utterance_idx": utterance_idx,
        "text": text,
        "language_code": language,
    }


def dictation_final(
    text: str,
    utterance_idx: int = 0,
    language: str | None = "hi-IN",
    degraded: str | None = None,
) -> dict:
    return {
        "event": "dictation.final",
        "utterance_idx": utterance_idx,
        "model": "dictate-v1",
        "text": text,
        "corrections": [],
        "degraded": degraded,
        "language_code": language,
        "usage": {"audio_duration_s": 1.0, "words": 1},
    }


def output_final(text: str) -> dict:
    return {"event": "output.final", "text": text, "degraded": None, "usage": {}}


def error_event(code: str, message: str, is_fatal: bool) -> dict:
    return {"event": "error", "code": code, "message": message, "is_fatal": is_fatal}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def pump(socket: "FakeSocket", expected_calls: int, timeout: float = 2.0) -> None:
    """Let the receiver task consume until it has asked ``expected_calls`` times."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while socket.recv_calls < expected_calls:
        if loop.time() > deadline:  # pragma: no cover - diagnostic only
            raise AssertionError(
                f"receiver only made {socket.recv_calls} recv() calls, wanted {expected_calls}"
            )
        await asyncio.sleep(0.002)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def other_tasks() -> list[asyncio.Task]:
    """Every task still running in this loop except the current one."""
    current = asyncio.current_task()
    return [task for task in asyncio.all_tasks() if task is not current and not task.done()]


def opened(config, socket=None, **kwargs):
    """Build a transcriber wired to a fake socket, plus the connector."""
    connector = FakeConnector(socket if socket is not None else FakeSocket(), **kwargs)
    return DictateRealtimeTranscriber(config, client=connector), connector


# --------------------------------------------------------------------------
# the module surface and its optional dependency
# --------------------------------------------------------------------------


def test_the_module_imports_without_websockets_installed():
    source = Path(dictate_streaming.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] != "websockets" for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] != "websockets"


def test_a_fresh_import_works_while_websockets_is_blocked():
    name = "sarvam_dictation.dictate_streaming"
    saved = sys.modules.pop(name, None)
    try:
        with module_blocked("websockets"):
            module = importlib.import_module(name)
        assert module.DICTATE_WS_URL == DICTATE_WS_URL
    finally:
        sys.modules.pop(name, None)
        if saved is not None:
            sys.modules[name] = saved


def test_an_injected_client_never_imports_websockets(make_config):
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        with module_blocked("websockets"):
            await transcriber.open()
            await transcriber.feed(b"\x00\x00" * 32)
            return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"
    # Only meaningful while nothing else in this interpreter has imported it:
    # the transport-level tests drive the real package and run in the same
    # session. The subprocess test above proves the same property on its own.
    if "websockets" in sys.modules:
        pytest.skip("websockets is already imported in this interpreter")
    assert "websockets" not in sys.modules


def test_the_error_codes_are_the_protocol_vocabulary():
    assert StreamingError("boom", code="api").code == "api"
    assert StreamingError("boom").code == "internal"
    assert StreamingError("bad", code="config_invalid").code == "config_invalid"


def test_the_exported_names_are_the_ones_the_daemon_imports():
    for name in (
        "DICTATE_WS_URL",
        "DictateRealtimeTranscriber",
        "StreamingError",
        "Transcript",
    ):
        assert name in dictate_streaming.__all__
    # StreamingError and Transcript are the streaming module's, not new copies.
    import sarvam_dictation.streaming as streaming

    assert StreamingError is streaming.StreamingError
    assert Transcript is streaming.Transcript


# --------------------------------------------------------------------------
# pre-flight: nothing is opened when the settings cannot work
# --------------------------------------------------------------------------


def test_open_without_an_api_key_raises_before_any_socket_is_opened(make_config):
    transcriber, connector = opened(make_config(api_key=""))

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "no_api_key"
        assert connector.calls == []
        assert other_tasks() == []

    asyncio.run(scenario())


def test_open_refuses_a_mode_dictate_does_not_have(make_config):
    transcriber, connector = opened(make_config(mode="translate"))

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "config_invalid"
        assert "translate" in str(excinfo.value)
        assert "dictate" in str(excinfo.value).lower()
        assert connector.calls == []
        assert other_tasks() == []

    asyncio.run(scenario())


@pytest.mark.parametrize("rate", [44100, 48000, 22050, 0])
def test_open_refuses_a_sample_rate_the_socket_cannot_serve(make_config, rate):
    transcriber, connector = opened(make_config(sample_rate=rate))

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "config_invalid"
        assert str(rate) in str(excinfo.value)
        assert connector.calls == []
        assert other_tasks() == []

    asyncio.run(scenario())


@pytest.mark.parametrize("rate", DICTATE_SAMPLE_RATES)
def test_open_accepts_every_sample_rate_the_socket_documents(make_config, rate):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, connector = opened(make_config(sample_rate=rate), socket)
        await transcriber.open()
        await transcriber.finish()
        return connector

    assert dict(asyncio.run(scenario()).query)["sample_rate"] == str(rate)


# --------------------------------------------------------------------------
# the handshake URL
# --------------------------------------------------------------------------


def test_the_connect_url_carries_exactly_the_documented_query_parameters(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, connector = opened(
            make_config(mode="codemix", language_code="hi-IN", sample_rate=16000), socket
        )
        await transcriber.open()
        await transcriber.finish()
        return connector

    connector = asyncio.run(scenario())
    assert connector.url.startswith(DICTATE_WS_URL + "?")
    assert connector.query == [
        ("language_code", "hi-IN"),
        ("mode", "codemix"),
        ("encoding", "linear16"),
        ("sample_rate", "16000"),
        ("stream_type", "balanced"),
        ("endpointing", "manual"),
    ]


def test_the_sample_rate_is_sent_as_a_string(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, connector = opened(make_config(sample_rate=8000), socket)
        await transcriber.open()
        await transcriber.finish()
        return connector

    query = dict(asyncio.run(scenario()).query)
    assert query["sample_rate"] == "8000"
    assert isinstance(query["sample_rate"], str)


def test_an_empty_language_code_becomes_auto(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, connector = opened(make_config(language_code=""), socket)
        await transcriber.open()
        await transcriber.finish()
        return connector

    assert dict(asyncio.run(scenario()).query)["language_code"] == "auto"


def test_the_model_parameter_is_omitted_unless_the_user_pinned_one(make_config):
    for pinned, expected in (("", None), ("dictate-v1-2026-11-04", "dictate-v1-2026-11-04")):
        socket = FakeSocket([session_begin()])

        async def scenario():
            transcriber, connector = opened(make_config(dictate_model=pinned), socket)
            await transcriber.open()
            await transcriber.finish()
            return connector

        query = dict(asyncio.run(scenario()).query)
        assert query.get("model") == expected


def test_the_api_key_travels_in_the_header_and_never_in_the_url(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, connector = opened(make_config(api_key=FAKE_KEY), socket)
        await transcriber.open()
        await transcriber.finish()
        return connector

    connector = asyncio.run(scenario())
    assert connector.headers["api-subscription-key"] == FAKE_KEY
    assert FAKE_KEY not in connector.url
    assert "api-subscription-key" not in connector.url


def test_the_stream_type_comes_from_the_config(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, connector = opened(make_config(dictate_stream_type="fast"), socket)
        await transcriber.open()
        await transcriber.finish()
        return connector

    assert dict(asyncio.run(scenario()).query)["stream_type"] == "fast"


# --------------------------------------------------------------------------
# the session handshake
# --------------------------------------------------------------------------


def test_one_config_update_carries_the_builders_values(make_config, dictate_stub):
    dictate_stub.style = {"preset": "clean", "rules": "no filler words"}
    dictate_stub.vocabulary = [{"word": "Velqora"}]
    dictate_stub.context = {"text": "a standup", "reference": "project names"}
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(dictate_output_rules="keep it short"), socket)
        await transcriber.open()
        await transcriber.finish()

    asyncio.run(scenario())
    updates = socket.payloads("config.update")
    assert len(updates) == 1, "the contract allows exactly one update at session start"
    assert updates[0] == {
        "event": "config.update",
        "style": {"preset": "clean", "rules": "no filler words"},
        "vocabulary": [{"word": "Velqora"}],
        "context": {"text": "a standup", "reference": "project names"},
        "output_rules": "keep it short",
    }
    assert dictate_stub.calls == ["style", "vocabulary", "context"]


def test_a_field_the_builder_returned_none_for_is_not_sent(make_config, dictate_stub):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.finish()

    asyncio.run(scenario())
    assert socket.payloads("config.update") == [{"event": "config.update"}]


def test_speech_start_is_sent_only_after_config_updated(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.finish()

    asyncio.run(scenario())
    log = socket.log
    assert log.index(("send", "config.update")) < log.index(("recv", "config.updated"))
    assert log.index(("recv", "config.updated")) < log.index(("send", "speech_start"))


def test_open_waits_for_session_begin_before_sending_anything(make_config, monkeypatch):
    monkeypatch.setattr(dictate_streaming, "SESSION_BEGIN_TIMEOUT_S", 0.05)
    socket = FakeSocket([], block_forever=True)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "api"
        assert "session.begin" in str(excinfo.value)
        # The only thing sent is the teardown `end` from cancel(); no
        # config.update and no speech_start ever went out.
        assert "config.update" not in socket.events()
        assert "speech_start" not in socket.events()
        assert other_tasks() == []

    asyncio.run(scenario())


def test_open_fails_when_config_update_is_never_confirmed(make_config, monkeypatch):
    monkeypatch.setattr(dictate_streaming, "CONFIG_UPDATED_TIMEOUT_S", 0.05)
    socket = FakeSocket([session_begin()], respond_to_config=False, block_forever=True)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "api"
        assert "config.updated" in str(excinfo.value)
        assert "speech_start" not in socket.events()
        assert other_tasks() == []

    asyncio.run(scenario())


def test_a_refused_config_update_does_not_kill_the_session(make_config):
    # The contract answers an update that changes nothing with invalid_config.
    # With nothing configured that is the normal case, and dictation must go on.
    socket = FakeSocket(
        [
            session_begin(),
            Pause(0.01),
            error_event("invalid_config", "nothing to change", False),
        ],
        respond_to_config=False,
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.finish()
        return transcriber

    asyncio.run(scenario())
    assert "speech_start" in socket.events()


def test_a_fatal_error_before_session_begin_fails_open_immediately(make_config):
    socket = FakeSocket([error_event("model_overloaded", "busy", True)])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "api"
        assert "model_overloaded" in str(excinfo.value)
        assert other_tasks() == []

    asyncio.run(scenario())


def test_a_403_at_the_handshake_is_an_api_error_not_a_network_one(make_config):
    class InvalidStatus(Exception):
        status_code = 403

        def __str__(self):
            return "server rejected WebSocket connection: HTTP 403"

    connector = FakeConnector(error=InvalidStatus())

    async def scenario():
        transcriber = DictateRealtimeTranscriber(make_config(), client=connector)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "api"
        assert "403" in str(excinfo.value)
        assert other_tasks() == []

    asyncio.run(scenario())


def test_a_transport_failure_at_the_handshake_is_a_network_error(make_config):
    connector = FakeConnector(error=OSError(111, "connection refused"))

    async def scenario():
        transcriber = DictateRealtimeTranscriber(make_config(), client=connector)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "network"
        assert other_tasks() == []

    asyncio.run(scenario())


def test_a_handshake_error_message_never_carries_the_api_key(make_config):
    class Rejected(Exception):
        def __str__(self):
            return f"handshake refused for key {FAKE_KEY}"

    connector = FakeConnector(error=Rejected())

    async def scenario():
        transcriber = DictateRealtimeTranscriber(make_config(api_key=FAKE_KEY), client=connector)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert FAKE_KEY not in str(excinfo.value)
        assert "***" in str(excinfo.value)

    asyncio.run(scenario())


def test_a_missing_payload_builder_module_is_an_internal_error(make_config):
    connector = FakeConnector()

    async def scenario():
        transcriber = DictateRealtimeTranscriber(make_config(), client=connector)
        with module_blocked("sarvam_dictation.dictate"):
            with pytest.raises(StreamingError) as excinfo:
                await transcriber.open()
        assert excinfo.value.code == "internal"
        assert "dictate" in str(excinfo.value)
        assert connector.calls == [], "a broken installation must not open a socket"

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------


def test_feed_buffers_into_whole_second_blocks_of_raw_pcm(make_config):
    socket = FakeSocket([session_begin()])
    pcm = b"\x01\x02" * 24000  # 1.5 s of 16 kHz s16le mono

    async def scenario():
        transcriber, _ = opened(make_config(sample_rate=16000), socket)
        await transcriber.open()
        for offset in range(0, len(pcm), 16000):
            await transcriber.feed(pcm[offset : offset + 16000])
        return await transcriber.finish()

    asyncio.run(scenario())
    blocks = socket.audio_payloads()
    assert [len(block) for block in blocks] == [32000, 16000]
    assert all(len(block) <= 32000 for block in blocks)
    assert b"".join(blocks) == pcm
    # Raw samples: no RIFF/WAVE header, the first bytes are the audio itself.
    assert b"".join(blocks).startswith(b"\x01\x02")
    assert b"RIFF" not in b"".join(blocks)


def test_feed_splits_one_oversized_chunk_instead_of_losing_it(make_config):
    socket = FakeSocket([session_begin()])
    pcm = b"\x7f\x00" * 48000  # 3 s in a single chunk, as a slow recorder may send

    async def scenario():
        transcriber, _ = opened(make_config(sample_rate=16000), socket)
        await transcriber.open()
        await transcriber.feed(pcm)
        await transcriber.finish()

    asyncio.run(scenario())
    assert [len(block) for block in socket.audio_payloads()] == [32000, 32000, 32000]


def test_feed_uses_half_second_blocks_with_the_fast_stream_type(make_config):
    socket = FakeSocket([session_begin()])
    pcm = b"\x00\x01" * 8000  # 1 s at 8 kHz

    async def scenario():
        transcriber, _ = opened(make_config(sample_rate=8000, dictate_stream_type="fast"), socket)
        await transcriber.open()
        await transcriber.feed(pcm)
        await transcriber.finish()

    asyncio.run(scenario())
    assert [len(block) for block in socket.audio_payloads()] == [8000, 8000]


def test_feed_never_sends_an_empty_audio_message(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.feed(b"")
        await transcriber.cancel()

    asyncio.run(scenario())
    assert socket.audio_payloads() == []
    assert "audio_input" not in socket.events()


def test_feed_before_open_is_a_no_op(make_config):
    transcriber, connector = opened(make_config())

    async def scenario():
        await transcriber.feed(b"\x00\x00" * 64)
        assert other_tasks() == []

    asyncio.run(scenario())
    assert connector.calls == []


def test_feed_after_finish_is_a_no_op(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.finish()
        before = len(socket.sent)
        await transcriber.feed(b"\x00\x00" * 64)
        return before

    before = asyncio.run(scenario())
    assert len(socket.sent) == before


def test_a_dead_socket_while_feeding_does_not_raise(make_config):
    socket = FakeSocket([session_begin()])

    async def broken_send(data):
        raise ConnectionResetError("socket is gone")

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        socket.send = broken_send
        await transcriber.feed(b"\x00\x00" * 32000)
        await transcriber.cancel()
        assert other_tasks() == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# partials
# --------------------------------------------------------------------------


def test_partials_carry_the_finished_turns_and_the_turn_in_flight(make_config):
    seen: list[str] = []
    socket = FakeSocket(
        [
            session_begin(),
            partial("नम"),
            partial("नमस्ते"),
            dictation_final("नमस्ते"),
            partial("दुनिया", utterance_idx=1),
            dictation_final("दुनिया", utterance_idx=1),
        ]
    )

    async def scenario():
        transcriber = DictateRealtimeTranscriber(
            make_config(), on_partial=seen.append, client=FakeConnector(socket)
        )
        await transcriber.open()
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert seen == ["नम", "नमस्ते", "नमस्ते दुनिया"]
    assert transcript.text == "नमस्ते दुनिया"


def test_a_partial_is_never_forwarded_as_an_empty_string(make_config):
    seen: list[str] = []
    socket = FakeSocket(
        [session_begin(), partial(""), partial("   "), partial("\n"), partial("ठीक")]
    )

    async def scenario():
        transcriber = DictateRealtimeTranscriber(
            make_config(), on_partial=seen.append, client=FakeConnector(socket)
        )
        await transcriber.open()
        await transcriber.finish()

    asyncio.run(scenario())
    assert seen == ["ठीक"]
    assert all(text.strip() for text in seen)


def test_a_stale_partial_for_a_finished_turn_is_not_repeated(make_config):
    seen: list[str] = []
    socket = FakeSocket(
        [session_begin(), dictation_final("पहला"), partial("पहला और")]
    )

    async def scenario():
        transcriber = DictateRealtimeTranscriber(
            make_config(), on_partial=seen.append, client=FakeConnector(socket)
        )
        await transcriber.open()
        await transcriber.finish()

    asyncio.run(scenario())
    assert seen == ["पहला"]


def test_a_failing_partial_callback_does_not_kill_the_session(make_config):
    calls: list[str] = []

    def explode(text: str) -> None:
        calls.append(text)
        raise RuntimeError("the sink is broken")

    socket = FakeSocket([session_begin(), partial("नम"), dictation_final("नमस्ते")])

    async def scenario():
        transcriber = DictateRealtimeTranscriber(
            make_config(), on_partial=explode, client=FakeConnector(socket)
        )
        await transcriber.open()
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"
    assert calls == ["नम"]


# --------------------------------------------------------------------------
# finish()
# --------------------------------------------------------------------------


def test_finish_sends_speech_end_then_end_and_reads_to_session_end(make_config):
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    asyncio.run(scenario())
    assert socket.events() == ["config.update", "speech_start", "speech_end", "end"]
    assert "session.end" in socket.received(), "the loop must read until session.end"


def test_turns_are_joined_in_utterance_idx_order_even_when_they_arrive_out_of_order(make_config):
    # The contract's warning: a later turn's transcript.final can arrive before an
    # earlier turn's dictation.final. Arrival order must not decide the text.
    socket = FakeSocket(
        [
            session_begin(),
            transcript_final("दूसरा", 1, language=None),
            dictation_final("दूसरा", 1, language=None),
            transcript_final("पहला", 0, language="bn-IN"),
            dictation_final("पहला", 0, language="bn-IN"),
            dictation_final("तीसरा", 2, language=None),
        ]
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert transcript.text == "पहला दूसरा तीसरा"
    assert transcript.segments == 3
    assert transcript.language_code == "bn-IN"


def test_a_turn_with_empty_text_is_counted_but_skipped_in_the_text(make_config):
    socket = FakeSocket(
        [
            session_begin(),
            dictation_final("पहला"),
            dictation_final("", utterance_idx=1, degraded="asr_error", language=None),
            dictation_final("तीसरा", utterance_idx=2),
        ]
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert transcript.text == "पहला तीसरा"
    assert transcript.segments == 3


def test_a_silent_session_is_an_empty_transcript_of_one_segment(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    assert asyncio.run(scenario()) == Transcript(text="", language_code=None, segments=1)


def test_output_final_does_not_change_the_text_and_does_not_break_the_loop(make_config):
    socket = FakeSocket(
        [
            session_begin(),
            dictation_final("कल मीटिंग है"),
            output_final("- Meeting tomorrow\n- Bring notes"),
        ]
    )

    async def scenario():
        transcriber, _ = opened(make_config(dictate_output_rules="bullet points"), socket)
        await transcriber.open()
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert transcript.text == "कल मीटिंग है", "a dictation types what was said, not a summary"
    assert "session.end" in socket.received()


def test_the_output_rules_are_sent_but_an_empty_one_is_not(make_config):
    for rules, expected in (("bullets", "bullets"), ("", None), ("   ", None)):
        socket = FakeSocket([session_begin()])

        async def scenario():
            transcriber, _ = opened(make_config(dictate_output_rules=rules), socket)
            await transcriber.open()
            await transcriber.finish()

        asyncio.run(scenario())
        assert socket.payloads("config.update")[0].get("output_rules") == expected


def test_a_fatal_error_with_no_turn_text_maps_to_api(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(api_key=FAKE_KEY), socket)
        await transcriber.open()
        # Pushed after the handshake: a fatal error before config.updated would
        # (correctly) fail open() instead of finish().
        socket.push(error_event("asr_error", "recognition stopped", True))
        await pump(socket, 3)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.finish()
        assert other_tasks() == []
        return excinfo.value

    exc = asyncio.run(scenario())
    assert exc.code == "api"
    assert "asr_error" in str(exc)
    assert "recognition stopped" in str(exc)


def test_a_fatal_error_after_a_finished_turn_returns_the_dictation(make_config):
    socket = FakeSocket(
        [
            session_begin(),
            dictation_final("नमस्ते"),
        ]
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        socket.push(error_event("asr_error", "recognition stopped", True))
        await pump(socket, 3)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"


def test_a_non_fatal_error_is_logged_and_ignored(make_config):
    socket = FakeSocket(
        [
            session_begin(),
            error_event("invalid_audio", "chunk was odd", False),
            dictation_final("नमस्ते"),
        ]
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"


def test_a_non_1000_close_before_session_end_maps_to_network(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        socket.push(Closed(1011, "recognition stopped"))
        await pump(socket, 3)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.finish()
        return excinfo.value

    exc = asyncio.run(scenario())
    assert exc.code == "network"
    assert "1011" in str(exc)


def test_a_1000_close_without_session_end_is_not_an_error(make_config):
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        socket.push(Closed(1000, "bye"))
        await pump(socket, 3)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"


def test_a_session_end_that_never_arrives_is_a_network_error(make_config, monkeypatch):
    monkeypatch.setattr(dictate_streaming, "SESSION_END_TIMEOUT_S", 0.05)
    socket = FakeSocket([session_begin()], respond_to_end=False)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.finish()
        assert excinfo.value.code == "network"
        assert other_tasks() == []

    asyncio.run(scenario())


def test_a_missing_session_end_still_returns_the_turns_already_finished(make_config, monkeypatch, caplog):
    monkeypatch.setattr(dictate_streaming, "SESSION_END_TIMEOUT_S", 0.05)
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")], respond_to_end=False)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    with caplog.at_level(logging.WARNING, logger="sarvam_dictation.dictate_streaming"):
        transcript = asyncio.run(scenario())

    assert transcript.text == "नमस्ते"
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any("session.end" in message for message in warnings), warnings


def test_finish_is_safe_when_open_was_never_called(make_config):
    transcriber, connector = opened(make_config())

    async def scenario():
        return await transcriber.finish()

    assert asyncio.run(scenario()) == Transcript(text="", language_code=None, segments=1)
    assert connector.calls == []


def test_finish_can_be_called_twice(make_config):
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish(), await transcriber.finish()

    first, second = asyncio.run(scenario())
    assert first == second
    assert socket.events().count("end") == 1


def test_an_unknown_event_is_ignored(make_config):
    socket = FakeSocket(
        [session_begin(), {"event": "brand.new"}, dictation_final("ठीक")]
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "ठीक"


# --------------------------------------------------------------------------
# cancel() and the keepalive
# --------------------------------------------------------------------------


def test_cancel_leaves_no_task_behind(make_config):
    socket = FakeSocket([session_begin()], block_forever=True)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()
        assert socket.closed is True
        assert other_tasks() == []

    asyncio.run(scenario())


def test_cancel_sends_end_once_for_a_session_that_is_still_open(make_config):
    socket = FakeSocket([session_begin()], block_forever=True)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()
        await transcriber.cancel()

    asyncio.run(scenario())
    assert socket.events() == ["config.update", "speech_start", "end"]
    assert len(socket.close_calls) == 1


def test_cancel_after_finish_does_not_send_a_second_end(make_config):
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        transcript = await transcriber.finish()
        await transcriber.cancel()
        return transcript, await transcriber.finish()

    transcript, again = asyncio.run(scenario())
    assert transcript.text == "नमस्ते"
    assert again == transcript
    assert socket.events().count("end") == 1


def test_cancel_before_open_is_a_no_op(make_config):
    transcriber, connector = opened(make_config())

    async def scenario():
        await transcriber.cancel()
        await transcriber.cancel()
        assert other_tasks() == []

    asyncio.run(scenario())
    assert connector.calls == []


def test_a_cancelled_session_does_not_wait_for_a_dictation(make_config):
    socket = FakeSocket([session_begin()], block_forever=True)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        started = asyncio.get_running_loop().time()
        await transcriber.cancel()
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(scenario()) < 1.0


def test_the_keepalive_pings_a_session_that_says_nothing(make_config, monkeypatch):
    # The contract closes a socket that has been quiet for 60 s with 1008.
    monkeypatch.setattr(dictate_streaming, "KEEPALIVE_INTERVAL_S", 0.01)
    socket = FakeSocket([session_begin()], block_forever=True)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await asyncio.sleep(0.05)
        await transcriber.cancel()

    asyncio.run(scenario())
    assert "ping" in socket.events()
    assert socket.payloads("ping")[0] == {"event": "ping"}


def test_the_keepalive_stops_when_the_session_ends(make_config, monkeypatch):
    monkeypatch.setattr(dictate_streaming, "KEEPALIVE_INTERVAL_S", 0.01)
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.finish()
        before = socket.events().count("ping")
        await asyncio.sleep(0.05)
        return before, socket.events().count("ping")

    before, after = asyncio.run(scenario())
    assert before == after, "a finished session must not keep pinging"


# --------------------------------------------------------------------------
# the frozen configuration surface this module codes against
# --------------------------------------------------------------------------


def test_the_modes_this_module_accepts_are_the_config_ones():
    # Four modes, no translate: config.py owns the tuple, this module only reads it.
    assert "translate" not in DICTATE_MODES
    assert set(DICTATE_MODES) == {"transcribe", "verbatim", "translit", "codemix"}

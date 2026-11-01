"""Tests for the realtime streaming transcriber (brief §1a/§1c).

No network, no audio hardware and no ``sarvamai`` SDK are needed: every test
drives :class:`RealtimeTranscriber` against :class:`FakeClient`, whose socket
answers ``recv()`` from a scripted list of messages. The loop is run with
``asyncio.run`` inside plain sync tests, so ``pytest-asyncio`` is not required.

The one test that does care about the SDK asserts the opposite of needing it:
with a client injected, ``sarvamai`` must never be imported (a ``sys.meta_path``
blocker turns any such import into a failure).
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import logging
import sys
import time
from pathlib import Path

import pytest

import sarvam_dictation.streaming as streaming
from sarvam_dictation.config import REALTIME_MODEL, REST_MODELS, STREAMING_MODELS
from sarvam_dictation.streaming import RealtimeTranscriber, StreamingError, Transcript

FAKE_KEY = "sk-fake-0000000000000000-not-a-real-key"


# --------------------------------------------------------------------------
# fakes: client, async context manager, socket
# --------------------------------------------------------------------------


class EndOfStream(Exception):
    """What the fake socket raises once its scripted messages run out."""


class Pause:
    """A scripted wait between two messages in a :class:`FakeSocket` script.

    A real service on a slow link finalises, pauses while it chews through the
    upload backlog, then finalises again. A pause in the script models that
    without a test having to wait for real seconds.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


class FakeSocket:
    """Stand-in for ``AsyncSpeechToTextRealtimeStreamingSocketClient``.

    ``messages`` are returned from ``recv()`` in order; an item may be an
    exception instance, which is raised instead, or a :class:`Pause`, which
    waits before the next message is returned. When the list is exhausted the
    socket raises :class:`EndOfStream`, exactly as the real one raises when the
    server closes the connection. ``block_forever`` keeps ``recv()`` waiting
    instead, which is what a live session looks like.

    Every message actually handed to the caller is recorded in ``received``
    together with whether the session had already been closed, so a test can
    prove the session stayed open past the first final.
    """

    def __init__(self, messages=(), block_forever: bool = False) -> None:
        self._messages = list(messages)
        self._block = block_forever
        self._gate = asyncio.Event()
        self.recv_calls = 0
        self.sent: list[tuple[str, dict]] = []
        self.received: list = []
        self.closed_when: list[bool] = []
        self.closed = False

    async def recv(self):
        self.recv_calls += 1
        while self._messages:
            message = self._messages.pop(0)
            if isinstance(message, Pause):
                await asyncio.sleep(message.seconds)
                continue
            if isinstance(message, Exception):
                raise message
            self.received.append(message)
            self.closed_when.append(self.closed)
            return message
        if self._block:
            await self._gate.wait()
        raise EndOfStream()

    # -- send_realtime_* ---------------------------------------------------
    async def _record(self, message) -> None:
        payload = message.dict() if hasattr(message, "dict") else dict(message)
        self.sent.append((payload["event"], payload))

    async def send_realtime_audio_input(self, message) -> None:
        await self._record(message)

    async def send_realtime_speech_start(self, message) -> None:
        await self._record(message)

    async def send_realtime_speech_end(self, message) -> None:
        await self._record(message)

    async def send_realtime_flush(self, message) -> None:
        await self._record(message)

    async def send_realtime_config_update(self, message) -> None:
        await self._record(message)

    async def send_realtime_end(self, message) -> None:
        await self._record(message)

    async def send_realtime_ping(self, message) -> None:
        await self._record(message)

    # -- assertions helpers ------------------------------------------------
    def events(self) -> list[str]:
        return [event for event, _ in self.sent]

    def audio_payloads(self) -> list[bytes]:
        return [base64.b64decode(payload["audio"]) for event, payload in self.sent
                if event == "audio_input"]


class FakeContext:
    """The async context manager ``connect()`` returns."""

    def __init__(self, client: "FakeClient") -> None:
        self._client = client

    async def __aenter__(self):
        self._client.enters += 1
        if self._client.connect_error is not None:
            raise self._client.connect_error
        return self._client.socket

    async def __aexit__(self, *exc_info):
        self._client.exits += 1
        self._client.socket.closed = True
        return False


class FakeService:
    """Stand-in for ``client.speech_to_text_realtime_streaming``."""

    def __init__(self, client: "FakeClient") -> None:
        self._client = client

    def connect(self, **kwargs) -> FakeContext:
        self._client.connect_calls.append(kwargs)
        return FakeContext(self._client)


class FakeClient:
    """Stand-in for ``sarvamai.AsyncSarvamAI``."""

    def __init__(self, socket: FakeSocket | None = None, connect_error=None) -> None:
        self.socket = socket if socket is not None else FakeSocket()
        self.connect_error = connect_error
        self.connect_calls: list[dict] = []
        self.enters = 0
        self.exits = 0
        self.speech_to_text_realtime_streaming = FakeService(self)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def partial(text, utterance_idx: int = 0, language=None) -> dict:
    return {
        "event": "transcript.partial",
        "utterance_idx": utterance_idx,
        "text": text,
        "language": language,
    }


def final(text, utterance_idx: int = 0, language=None) -> dict:
    return {
        "event": "transcript.final",
        "utterance_idx": utterance_idx,
        "text": text,
        "language": language,
        "language_confidence": 0.9,
        "start_s": 0.0,
        "end_s": 1.0,
    }


def error_message(code: str, message: str, is_fatal: bool, status_code=None) -> dict:
    return {
        "event": "error",
        "code": code,
        "is_fatal": is_fatal,
        "message": message,
        "status_code": status_code,
    }


async def pump(socket: FakeSocket, expected_calls: int, timeout: float = 2.0) -> None:
    """Let the receiver task consume until it has asked ``expected_calls`` times."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while socket.recv_calls < expected_calls:
        if loop.time() > deadline:  # pragma: no cover - diagnostic only
            raise AssertionError(
                f"receiver only made {socket.recv_calls} recv() calls, wanted {expected_calls}"
            )
        await asyncio.sleep(0.002)
    # recv_calls is incremented before the message is handled: let the receiver
    # finish applying the last one before the test looks at the state.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def other_tasks() -> list[asyncio.Task]:
    """Every task still running in this loop except the current one."""
    current = asyncio.current_task()
    return [task for task in asyncio.all_tasks() if task is not current and not task.done()]


@pytest.fixture
def sdk_blocked():
    """Make any attempt to import ``sarvamai`` fail loudly for one test."""
    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if name == "sarvamai" or name.startswith("sarvamai.")
    }
    for name in saved:
        sys.modules.pop(name, None)

    class Blocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "sarvamai" or fullname.startswith("sarvamai."):
                raise AssertionError("streaming.py imported the sarvamai SDK")
            return None

    blocker = Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(saved)


# --------------------------------------------------------------------------
# module surface
# --------------------------------------------------------------------------


def test_module_does_not_import_the_sdk_at_import_time():
    source = Path(streaming.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] != "sarvamai" for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] != "sarvamai"


def test_streaming_error_codes_are_the_protocol_vocabulary():
    exc = StreamingError("boom", code="api")
    assert exc.code == "api"
    assert str(exc) == "boom"
    assert StreamingError("other").code == "internal"
    assert StreamingError("bad model", code="config_invalid").code == "config_invalid"


def test_the_realtime_models_are_the_ones_config_declares():
    # No second copy of the tuple in this module: config.py is the source.
    assert streaming.REALTIME_MODELS is STREAMING_MODELS


def test_transcript_is_a_plain_dataclass():
    transcript = Transcript(text="नमस्ते", language_code="hi-IN", segments=2)
    assert (transcript.text, transcript.language_code, transcript.segments) == (
        "नमस्ते", "hi-IN", 2,
    )


# --------------------------------------------------------------------------
# open()
# --------------------------------------------------------------------------


def test_open_without_an_api_key_raises_before_any_client_is_constructed(make_config):
    config = make_config(api_key="")
    client = FakeClient()

    async def scenario():
        transcriber = RealtimeTranscriber(config, client=client)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "no_api_key"
        assert client.connect_calls == []
        assert client.enters == 0
        assert client.socket.sent == []
        assert other_tasks() == []

    asyncio.run(scenario())


def test_open_uses_the_verified_connect_arguments(make_config):
    config = make_config(model="saaras:v3-realtime", mode="transcribe", sample_rate=16000)
    client = FakeClient()

    async def scenario():
        transcriber = RealtimeTranscriber(config, client=client)
        await transcriber.open()
        await transcriber.cancel()

    asyncio.run(scenario())

    assert len(client.connect_calls) == 1
    kwargs = client.connect_calls[0]
    assert kwargs["language_code"] == "auto"
    assert kwargs["model"] == "saaras:v3-realtime"
    assert kwargs["mode"] == "transcribe"
    assert kwargs["encoding"] == "linear16"
    assert kwargs["sample_rate"] == "16000"
    assert kwargs["endpointing"] == "manual"
    assert kwargs["keyterms"] is None


def test_open_passes_a_configured_language_and_json_keyterms_for_saaras_v4(make_config):
    config = make_config(model="saaras:v4", language_code="hi-IN", keyterms=("Sarvam", "नई दिल्ली"))
    client = FakeClient()

    async def scenario():
        transcriber = RealtimeTranscriber(config, client=client)
        await transcriber.open()
        await transcriber.cancel()

    asyncio.run(scenario())

    kwargs = client.connect_calls[0]
    assert kwargs["language_code"] == "hi-IN"
    assert kwargs["model"] == "saaras:v4"
    assert json.loads(kwargs["keyterms"]) == ["Sarvam", "नई दिल्ली"]


def test_open_refuses_a_rest_only_model_instead_of_substituting_one(make_config):
    # Brief 8 replaced the old silent fallback: a model the realtime endpoint
    # does not serve is a configuration mistake, not something to paper over.
    config = make_config(model="saaras:v3")
    client = FakeClient()

    async def scenario():
        transcriber = RealtimeTranscriber(config, client=client)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "config_invalid"
        assert client.connect_calls == []
        assert client.enters == 0
        assert client.socket.sent == []
        assert other_tasks() == []

    asyncio.run(scenario())


def test_open_passes_the_configured_streaming_model_verbatim(make_config):
    for model in STREAMING_MODELS:
        client = FakeClient()

        async def scenario():
            transcriber = RealtimeTranscriber(make_config(model=model), client=client)
            await transcriber.open()
            await transcriber.cancel()

        asyncio.run(scenario())
        assert client.connect_calls[0]["model"] == model


def test_open_sends_speech_start_for_manual_endpointing(make_config):
    client = FakeClient()

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        assert client.socket.events() == ["speech_start"]
        await transcriber.cancel()

    asyncio.run(scenario())


def test_open_maps_a_connect_failure_and_scrubs_the_key(make_config):
    config = make_config(api_key=FAKE_KEY)
    client = FakeClient(connect_error=RuntimeError(f"handshake rejected for {FAKE_KEY}"))

    async def scenario():
        transcriber = RealtimeTranscriber(config, client=client)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        message = str(excinfo.value)
        assert excinfo.value.code == "internal"
        assert FAKE_KEY not in message
        assert "handshake rejected" in message
        assert other_tasks() == []

    asyncio.run(scenario())


def test_open_maps_a_rejected_key_to_the_api_code(make_config):
    class ApiError(Exception):
        status_code = 401

        def __str__(self):
            return "Websocket initialized with invalid credentials."

    client = FakeClient(connect_error=ApiError())

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "api"
        assert "401" in str(excinfo.value)

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# partials
# --------------------------------------------------------------------------


def test_partials_are_cumulative_stripped_and_forwarded(make_config):
    seen: list[str] = []
    socket = FakeSocket([partial("  नम"), partial("नमस्ते "), partial("नमस्ते दुनिया")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), on_partial=seen.append, client=client)
        await transcriber.open()
        await pump(socket, 4)
        transcript = await transcriber.finish()
        return transcript

    transcript = asyncio.run(scenario())
    assert seen == ["नम", "नमस्ते", "नमस्ते दुनिया"]
    assert transcript.text == ""
    assert transcript.segments == 0


def test_empty_partials_are_never_forwarded(make_config):
    seen: list[str] = []
    socket = FakeSocket([partial(""), partial("   "), partial("\n"), partial("ठीक")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), on_partial=seen.append, client=client)
        await transcriber.open()
        await pump(socket, 5)
        await transcriber.cancel()

    asyncio.run(scenario())
    assert seen == ["ठीक"]


def test_a_failing_partial_callback_does_not_kill_the_session(make_config):
    calls: list[str] = []

    def explode(text: str) -> None:
        calls.append(text)
        raise RuntimeError("the sink is broken")

    socket = FakeSocket([partial("नम"), partial("नमस्ते"), final("नमस्ते")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), on_partial=explode, client=client)
        await transcriber.open()
        await pump(socket, 4)
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert calls == ["नम", "नमस्ते"]
    assert transcript.text == "नमस्ते"
    assert transcript.segments == 1


def test_no_partial_callback_is_fine(make_config):
    socket = FakeSocket([partial("नम"), final("नम")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 3)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नम"


# --------------------------------------------------------------------------
# finals
# --------------------------------------------------------------------------


def test_finals_are_joined_in_utterance_idx_order_and_counted(make_config):
    socket = FakeSocket([
        partial("तीन", utterance_idx=2),
        final("तीन", utterance_idx=2),
        final("एक", utterance_idx=0, language="hi-IN"),
        final("दो", utterance_idx=1),
    ])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 5)
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert transcript.text == "एक दो तीन"
    assert transcript.segments == 3
    assert transcript.language_code == "hi-IN"


def test_final_text_is_stripped_and_single_spaced(make_config):
    socket = FakeSocket([final("  नमस्ते  ", 0), final("  दुनिया ", 1)])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 3)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते दुनिया"


def test_no_finals_at_all_gives_an_empty_transcript(make_config):
    socket = FakeSocket([partial("नम"), partial("नमस्ते")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 3)
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert transcript == Transcript("", None, 0)


def test_finish_times_out_into_an_empty_transcript_when_no_final_ever_arrives(
    make_config, monkeypatch
):
    # Brief 16 replaced the old first-final-plus-FINAL_TIMEOUT_S behaviour: a
    # session that never finalises anything now ends on the hard deadline, and
    # that deadline is the only thing that may establish an empty result.
    monkeypatch.setattr(streaming, "FINAL_SETTLE_S", 0.01)
    monkeypatch.setattr(streaming, "FINAL_DEADLINE_S", 0.05)
    socket = FakeSocket([partial("नम")], block_forever=True)
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 2)
        started = time.monotonic()
        transcript = await transcriber.finish()
        return transcript, time.monotonic() - started

    transcript, elapsed = asyncio.run(scenario())
    assert transcript == Transcript("", None, 0)
    assert elapsed < 1.0
    assert client.exits == 1
    assert socket.closed is True


def test_finish_sends_speech_end_flush_and_end(make_config):
    socket = FakeSocket([final("नमस्ते")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 2)
        await transcriber.finish()

    asyncio.run(scenario())
    assert socket.events() == ["speech_start", "speech_end", "flush", "end"]
    assert client.exits == 1


def test_finish_is_safe_when_open_was_never_called(make_config):
    client = FakeClient()

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        return await transcriber.finish()

    assert asyncio.run(scenario()) == Transcript("", None, 0)
    assert client.connect_calls == []


def test_finish_can_be_called_twice(make_config):
    socket = FakeSocket([final("नमस्ते")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 2)
        first = await transcriber.finish()
        second = await transcriber.finish()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second == Transcript("नमस्ते", None, 1)
    assert client.exits == 1


# --------------------------------------------------------------------------
# errors from the server
# --------------------------------------------------------------------------


def test_a_fatal_error_becomes_a_streaming_error_without_the_key(make_config):
    config = make_config(api_key=FAKE_KEY)
    socket = FakeSocket([
        error_message("quota_exhausted", f"key {FAKE_KEY} has no credit", True, 403),
    ])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(config, client=client)
        await transcriber.open()
        # A fatal error ends the receive loop, so there is no second recv().
        await pump(socket, 1)
        started = time.monotonic()
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.finish()
        return excinfo.value, time.monotonic() - started

    exc, elapsed = asyncio.run(scenario())
    assert exc.code == "api"
    assert "quota_exhausted" in str(exc)
    assert "has no credit" in str(exc)
    assert "403" in str(exc)
    assert FAKE_KEY not in str(exc)
    assert elapsed < 1.0, "a fatal error must not wait out the final timeout"
    assert client.exits == 1


def test_a_non_fatal_error_is_ignored(make_config):
    socket = FakeSocket([
        error_message("invalid_config", "unknown field", False),
        final("नमस्ते"),
    ])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 3)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"


def test_an_unknown_message_type_is_ignored(make_config):
    socket = FakeSocket([{"event": "session.begin"}, {"event": "brand.new"}, final("ठीक")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 4)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "ठीक"


def test_a_transport_failure_while_receiving_still_returns_what_was_collected(make_config):
    socket = FakeSocket([final("नमस्ते"), ConnectionResetError("peer went away")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        # final, then the failure that ends the loop: two recv() calls.
        await pump(socket, 2)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"


# --------------------------------------------------------------------------
# feed()
# --------------------------------------------------------------------------


def test_feed_sends_base64_pcm(make_config):
    socket = FakeSocket([], block_forever=True)
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await transcriber.feed(b"\x01\x02\x03\x04")
        await transcriber.feed(b"\xff\xfe")
        await transcriber.cancel()

    asyncio.run(scenario())
    assert socket.audio_payloads() == [b"\x01\x02\x03\x04", b"\xff\xfe"]
    assert socket.events().count("audio_input") == 2


def test_feed_before_open_is_a_no_op(make_config):
    socket = FakeSocket()
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.feed(b"\x00\x00" * 64)
        assert other_tasks() == []

    asyncio.run(scenario())
    assert socket.sent == []
    assert client.connect_calls == []


def test_feed_after_finish_is_a_no_op(make_config):
    socket = FakeSocket([final("नमस्ते")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 2)
        await transcriber.finish()
        before = len(socket.sent)
        await transcriber.feed(b"\x00\x00" * 64)
        return before

    before = asyncio.run(scenario())
    assert len(socket.sent) == before


def test_feed_after_a_failed_open_is_a_no_op(make_config):
    client = FakeClient(connect_error=RuntimeError("no route to host"))

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        with pytest.raises(StreamingError):
            await transcriber.open()
        await transcriber.feed(b"\x00\x00" * 64)
        await transcriber.feed(b"")
        assert other_tasks() == []

    asyncio.run(scenario())


def test_feed_swallows_a_dead_socket(make_config):
    socket = FakeSocket([], block_forever=True)
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        client.socket.send_realtime_audio_input = _broken_send
        await transcriber.feed(b"\x01\x02")
        await transcriber.cancel()

    asyncio.run(scenario())


async def _broken_send(message):
    raise ConnectionResetError("socket is gone")


# --------------------------------------------------------------------------
# cancel()
# --------------------------------------------------------------------------


def test_cancel_closes_the_socket_and_leaves_no_task_running(make_config):
    socket = FakeSocket([], block_forever=True)
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 1)
        await transcriber.cancel()
        assert socket.closed is True
        assert client.exits == 1
        assert "end" not in socket.events()
        assert other_tasks() == []

    asyncio.run(scenario())


def test_cancel_is_idempotent(make_config):
    socket = FakeSocket([], block_forever=True)
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await transcriber.cancel()
        await transcriber.cancel()
        await transcriber.cancel()
        assert client.exits == 1
        assert other_tasks() == []

    asyncio.run(scenario())


def test_cancel_before_open_is_a_no_op(make_config):
    client = FakeClient()

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.cancel()
        await transcriber.cancel()
        assert client.connect_calls == []
        assert other_tasks() == []

    asyncio.run(scenario())


def test_cancel_after_finish_keeps_the_transcript(make_config):
    socket = FakeSocket([final("नमस्ते")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 2)
        transcript = await transcriber.finish()
        await transcriber.cancel()
        return transcript, await transcriber.finish()

    transcript, again = asyncio.run(scenario())
    assert transcript.text == "नमस्ते"
    assert again == transcript


def test_a_cancelled_session_does_not_wait_for_a_final(make_config):
    socket = FakeSocket([], block_forever=True)
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        started = time.monotonic()
        await transcriber.cancel()
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 1.0


# --------------------------------------------------------------------------
# the SDK stays optional
# --------------------------------------------------------------------------


def test_an_injected_client_never_imports_the_sdk(make_config, sdk_blocked):
    socket = FakeSocket([partial("नम"), final("नमस्ते")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), on_partial=lambda text: None, client=client)
        await transcriber.open()
        await transcriber.feed(b"\x00\x00" * 32)
        await pump(socket, 3)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"
    assert "sarvamai" not in sys.modules


# --------------------------------------------------------------------------
# the realtime model namespace (brief 8)
# --------------------------------------------------------------------------


REST_ONLY_MODELS = tuple(model for model in REST_MODELS if model not in STREAMING_MODELS)


@pytest.mark.parametrize("model", REST_ONLY_MODELS)
def test_open_refuses_every_rest_only_model_before_connecting(make_config, model):
    client = FakeClient()

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(model=model), client=client)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "config_invalid"
        assert model in str(excinfo.value)
        assert client.connect_calls == []
        assert client.enters == 0
        assert client.socket.sent == []
        assert other_tasks() == []

    asyncio.run(scenario())


def test_open_refuses_a_rest_only_model_without_constructing_a_client(make_config):
    # No injected client: reaching _resolve_client would try to import the SDK
    # (and fail with `internal`, since sarvamai is not installed here).
    # `config_invalid` proves the guard ran before any client work.
    async def scenario():
        transcriber = RealtimeTranscriber(make_config(model="saaras:v3"))
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "config_invalid"
        assert other_tasks() == []

    asyncio.run(scenario())


def test_the_config_invalid_message_names_the_model_and_the_alternatives(make_config):
    async def scenario():
        transcriber = RealtimeTranscriber(make_config(model="saaras:v3"))
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        return str(excinfo.value)

    message = asyncio.run(scenario())
    assert "saaras:v3" in message
    assert REALTIME_MODEL in message
    assert "saaras:v4" in message
    assert "streaming" in message
    assert "HTTP" not in message
    assert "400" not in message


def test_an_unknown_model_is_refused_rather_than_replaced(make_config):
    client = FakeClient()

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(model="whisper-large"), client=client)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "config_invalid"
        assert "whisper-large" in str(excinfo.value)
        assert client.connect_calls == []

    asyncio.run(scenario())


def test_the_guard_leaves_a_valid_session_working_end_to_end(make_config):
    for model in STREAMING_MODELS:
        socket = FakeSocket([partial("नम"), final("नमस्ते")])
        client = FakeClient(socket)

        async def scenario():
            transcriber = RealtimeTranscriber(make_config(model=model), client=client)
            await transcriber.open()
            await transcriber.feed(b"\x00\x00" * 32)
            await pump(socket, 3)
            return await transcriber.finish()

        transcript = asyncio.run(scenario())
        assert transcript.text == "नमस्ते"
        assert client.connect_calls[0]["model"] == model
        assert client.exits == 1


def test_the_guard_does_not_disturb_the_missing_key_check(make_config):
    # A model that is fine for realtime still reports the missing key first.
    client = FakeClient()

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(api_key="", model="saaras:v4"), client=client)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "no_api_key"
        assert client.connect_calls == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# settling on quiet, not on the first final (brief 16)
# --------------------------------------------------------------------------


def test_the_settle_constants_are_the_frozen_ones():
    # Brief 16 fixes these two and deletes the old single hard cap.
    assert streaming.FINAL_SETTLE_S == pytest.approx(2.5)
    assert streaming.FINAL_DEADLINE_S == pytest.approx(30.0)
    assert not hasattr(streaming, "FINAL_TIMEOUT_S"), (
        "the old FINAL_TIMEOUT_S cap is dead code and must be gone"
    )


def test_every_final_is_collected_and_the_session_is_not_closed_after_the_first(
    make_config, monkeypatch
):
    # The regression that matters: a slow server finalises, pauses while it
    # works through the backlog, then finalises twice more. All three must be
    # in the transcript, and the session must still be open while they arrive.
    monkeypatch.setattr(streaming, "FINAL_SETTLE_S", 0.3)
    monkeypatch.setattr(streaming, "FINAL_DEADLINE_S", 5.0)
    socket = FakeSocket(
        [
            final("एक", 0),
            Pause(0.02),
            final("दो", 1),
            Pause(0.02),
            final("तीन", 2),
        ],
        block_forever=True,
    )
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        started = time.monotonic()
        transcript = await transcriber.finish()
        return transcript, time.monotonic() - started

    transcript, elapsed = asyncio.run(scenario())
    assert transcript.text == "एक दो तीन"
    assert transcript.segments == 3
    assert len(socket.received) == 3
    assert socket.closed_when == [False, False, False], (
        "the session must not be closed after the first final"
    )
    assert client.exits == 1
    assert socket.closed is True
    # The quiet window, not the first final, ended the wait.
    assert elapsed >= 0.25


def test_a_final_just_before_the_window_expires_restarts_it(make_config, monkeypatch):
    # The second final lands inside the first quiet window, so the window must
    # restart: nothing is cut, and the wait lasts longer than one window.
    monkeypatch.setattr(streaming, "FINAL_SETTLE_S", 0.2)
    monkeypatch.setattr(streaming, "FINAL_DEADLINE_S", 5.0)
    socket = FakeSocket(
        [final("पहला", 0), Pause(0.12), final("दूसरा", 1)], block_forever=True
    )
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        started = time.monotonic()
        transcript = await transcriber.finish()
        return transcript, time.monotonic() - started

    transcript, elapsed = asyncio.run(scenario())
    assert transcript.text == "पहला दूसरा"
    assert transcript.segments == 2
    assert elapsed >= 0.28, "the quiet window must have been restarted, not cut short"
    assert socket.closed_when == [False, False]


def test_a_single_final_still_waits_out_the_quiet_window(make_config, monkeypatch):
    monkeypatch.setattr(streaming, "FINAL_SETTLE_S", 0.15)
    monkeypatch.setattr(streaming, "FINAL_DEADLINE_S", 5.0)
    socket = FakeSocket([final("नमस्ते")], block_forever=True)
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        started = time.monotonic()
        transcript = await transcriber.finish()
        return transcript, time.monotonic() - started

    transcript, elapsed = asyncio.run(scenario())
    assert transcript.text == "नमस्ते"
    assert transcript.segments == 1
    assert elapsed >= 0.13


def test_no_finals_at_all_takes_the_deadline_path_and_is_logged(
    make_config, monkeypatch, caplog
):
    # "No finals yet" is not "settled empty": the result is empty only because
    # the deadline passed, and that must be visible in the log.
    monkeypatch.setattr(streaming, "FINAL_SETTLE_S", 0.02)
    monkeypatch.setattr(streaming, "FINAL_DEADLINE_S", 0.05)
    socket = FakeSocket([partial("नम")], block_forever=True)
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 2)
        return await transcriber.finish()

    with caplog.at_level(logging.WARNING, logger="sarvam_dictation.streaming"):
        transcript = asyncio.run(scenario())

    assert transcript == Transcript("", None, 0)
    assert transcript.segments == 0
    assert client.exits == 1
    warnings = [record.getMessage() for record in caplog.records
                if record.levelno == logging.WARNING]
    assert any("deadline" in message for message in warnings), warnings
    assert any("0 final segment" in message for message in warnings), warnings


def test_a_server_that_never_goes_quiet_hits_the_deadline_and_is_logged(
    make_config, monkeypatch, caplog
):
    # Finals keep arriving faster than the quiet window, so only the hard cap
    # can end the wait: the tail may be missing, and the warning says how many
    # finals were collected before the cap stopped it.
    monkeypatch.setattr(streaming, "FINAL_SETTLE_S", 5.0)
    monkeypatch.setattr(streaming, "FINAL_DEADLINE_S", 0.15)
    socket = FakeSocket(
        [
            final("एक", 0),
            Pause(0.04),
            final("दो", 1),
            Pause(0.04),
            final("तीन", 2),
            Pause(0.04),
            final("चार", 3),
            Pause(0.04),
            final("पाँच", 4),
        ],
        block_forever=True,
    )
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        return await transcriber.finish()

    with caplog.at_level(logging.WARNING, logger="sarvam_dictation.streaming"):
        transcript = asyncio.run(scenario())

    assert transcript.segments >= 2
    warnings = [record.getMessage() for record in caplog.records
                if record.levelno == logging.WARNING]
    assert any(
        f"{transcript.segments} final segment" in message for message in warnings
    ), warnings
    assert any("may be missing" in message for message in warnings), warnings
    assert client.exits == 1


def test_the_receiver_ending_returns_promptly_instead_of_waiting_out_the_window(
    make_config, monkeypatch
):
    # The server closes the socket, so nothing more can arrive: the wait ends
    # at once even though the quiet window is far longer.
    monkeypatch.setattr(streaming, "FINAL_SETTLE_S", 5.0)
    monkeypatch.setattr(streaming, "FINAL_DEADLINE_S", 30.0)
    socket = FakeSocket([final("नमस्ते")])
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        started = time.monotonic()
        transcript = await transcriber.finish()
        return transcript, time.monotonic() - started

    transcript, elapsed = asyncio.run(scenario())
    assert transcript.text == "नमस्ते"
    assert elapsed < 1.0, "a closed socket must not cost the whole quiet window"
    assert client.exits == 1


def test_a_final_arriving_before_finish_is_not_lost(make_config, monkeypatch):
    # A final that arrived while the user was still speaking is already in
    # hand when finish() starts: it settles after the quiet window, not before.
    monkeypatch.setattr(streaming, "FINAL_SETTLE_S", 0.05)
    monkeypatch.setattr(streaming, "FINAL_DEADLINE_S", 5.0)
    socket = FakeSocket([final("नमस्ते"), Pause(0.01), final("दुनिया", 1)],
                        block_forever=True)
    client = FakeClient(socket)

    async def scenario():
        transcriber = RealtimeTranscriber(make_config(), client=client)
        await transcriber.open()
        await pump(socket, 2)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते दुनिया"

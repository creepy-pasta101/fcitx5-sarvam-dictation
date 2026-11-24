"""Tests for the Dictate live-socket transcriber, at the SDK boundary.

No network, no audio hardware, no ``sarvamai`` SDK and no sibling ``dictate.py``
are needed. Every test drives :class:`DictateRealtimeTranscriber` against
:class:`FakeClient` / :class:`FakeSocket`, a pair that models what the SDK
exposes - ``client.dictate_streaming.connect(**kwargs)`` as an async context
manager yielding a socket with ``update_config``, ``speech_start``,
``send_audio``, ``speech_end``, ``end``, ``ping`` and ``recv()`` - records every
request model handed to it, in order, and answers ``recv()`` from a scripted
list of event objects. The loop is run with ``asyncio.run`` inside plain sync
tests, so ``pytest-asyncio`` is not required.

Two stubs stand in for things this unit does not own:

* ``sys.modules["sarvam_dictation.dictate"]`` - the clip unit's payload
  builders, installed by the autouse ``dictate_stub`` fixture as a real
  ``types.ModuleType`` holding three callables whose return values a test sets.
  The module under test imports them lazily inside ``_config_fields``, so a stub
  in ``sys.modules`` is enough and no file is needed.
* the event objects themselves - :class:`Event` mirrors the SDK's
  ``Dictate*`` event classes field for field (``event``, ``utterance_idx``,
  ``text``, ``language_code``, ``is_fatal`` ...), which is all the module reads,
  because it accesses every message by attribute name and accepts plain dicts
  too.

The SDK is never imported here: one test blocks it in ``sys.meta_path`` to prove
the module imports without it, and another proves an injected client keeps it
out of the process entirely.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import importlib
import inspect
import sys
import types
from pathlib import Path

import pytest

import sarvam_dictation.dictate_streaming as dictate_streaming
from sarvam_dictation.config import DICTATE_MODES, DICTATE_SAMPLE_RATES
from sarvam_dictation.dictate_streaming import (
    DictateRealtimeTranscriber,
    StreamingError,
    Transcript,
)

FAKE_KEY = "sk-fake-0000000000000000-not-a-real-key"


# --------------------------------------------------------------------------
# fakes: socket, client and the events the socket hands back
# --------------------------------------------------------------------------


class Event:
    """One server event, shaped like the SDK's ``Dictate*`` event classes.

    The module reads messages by attribute name (``message.event``,
    ``message.text``, ``message.utterance_idx``), which is exactly what the SDK's
    dataclasses offer, so a small attribute bag is a faithful stand-in and keeps
    the SDK out of the test run.
    """

    def __init__(self, event: str, **fields) -> None:
        self.event = event
        for name, value in fields.items():
            setattr(self, name, value)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Event({self.event!r})"


class EndOfStream(Exception):
    """What the fake socket raises once its scripted events run out."""


class Closed(Exception):
    """A socket close frame, as the transport under the SDK raises it (``.code``)."""

    def __init__(self, code: int, reason: str = "server closed") -> None:
        super().__init__(f"received {code} ({reason})")
        self.code = code


class ApiError(Exception):
    """An ``sarvamai.core.api_error.ApiError``-shaped failure (``.status_code``)."""

    def __init__(self, status_code: int, message: str = "rejected") -> None:
        super().__init__(message)
        self.status_code = status_code


class Pause:
    """A scripted wait between two events in a :class:`FakeSocket` script.

    Used where an event must land *after* the handshake: the receiver sleeps
    through it while ``open()`` finishes ``session.begin`` / ``config.updated``,
    which is what the real server does when it takes a moment to react.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


def _body(request) -> dict:
    """The JSON body a recorded request model would be sent as."""
    dump = getattr(request, "dict", None)
    if callable(dump):
        return dump()
    return dict(request)


class FakeSocket:
    """Stand-in for the socket the SDK's ``dictate_streaming.connect`` yields.

    ``events`` are returned from ``recv()`` in order; an item may be an exception
    instance, which is raised instead. When the script runs out ``recv()`` waits
    for more, because a live session never ends on its own; pass
    ``block_forever=False`` to make it raise :class:`EndOfStream` instead.

    The fake answers the two messages a real server answers: an ``update_config``
    is followed by ``config.updated`` and an ``end`` by ``session.end``, each put
    at the *front* of the queue so it is the very next message, as the contract
    requires. Both can be switched off (``respond_to_config``/``respond_to_end``)
    to script a server that never confirms and never ends.

    ``log`` keeps every request and every ``recv`` in one ordered list, which is
    how the tests prove that ``speech_start`` follows the confirmation rather
    than racing it, and that the audio tail precedes ``speech_end``.
    """

    def __init__(
        self,
        events=(),
        *,
        respond_to_config: bool = True,
        respond_to_end: bool = True,
        block_forever: bool = True,
        fail_audio: bool = False,
    ) -> None:
        self._queue = list(events)
        self._wake = asyncio.Event()
        self._respond = respond_to_config
        self._respond_to_end = respond_to_end
        self.block_forever = block_forever
        self.fail_audio = fail_audio
        self.log: list[tuple[str, str]] = []
        self.calls: list[tuple[str, object]] = []
        self.exit_calls = 0
        self.recv_calls = 0

    # -- server side -------------------------------------------------------

    async def recv(self):
        self.recv_calls += 1
        while True:
            if self._queue:
                item = self._queue.pop(0)
                if isinstance(item, Pause):
                    await asyncio.sleep(item.seconds)
                    continue
                if isinstance(item, Exception):
                    raise item
                self.log.append(("recv", str(getattr(item, "event", "?"))))
                return item
            if not self.block_forever:
                raise EndOfStream()
            waiter = self._wake
            waiter.clear()
            if self._queue:
                continue
            await waiter.wait()

    def push(self, event) -> None:
        """Queue one more server event (used by the keepalive test)."""
        self._queue.append(event)
        self._wake.set()

    # -- client side -------------------------------------------------------

    def _record(self, name: str, request) -> None:
        self.calls.append((name, request))
        self.log.append(("send", name))

    async def update_config(self, message) -> None:
        self._record("config.update", message)
        if self._respond:
            applied = sorted(key for key in _body(message) if key != "event")
            self._queue.insert(0, config_updated(applied))
            self._wake.set()

    async def speech_start(self, message) -> None:
        self._record("speech_start", message)

    async def speech_end(self, message) -> None:
        self._record("speech_end", message)

    async def send_audio(self, message) -> None:
        if self.fail_audio:
            raise RuntimeError("the socket is gone")
        self._record("audio_input", message)

    async def end(self, message) -> None:
        self._record("end", message)
        if self._respond_to_end:
            # session.end is the last message, and the server only sends it once
            # the client has said `end` - which is exactly what finish() waits for.
            self._queue.insert(0, session_end())
            self._wake.set()

    async def ping(self, message) -> None:
        self._record("ping", message)

    # -- assertion helpers -------------------------------------------------

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def requests(self, name: str) -> list:
        return [request for call, request in self.calls if call == name]

    def request(self, name: str):
        requests = self.requests(name)
        assert requests, f"no {name} request was sent (sent: {self.names()})"
        return requests[0]

    def bodies(self, name: str) -> list[dict]:
        return [_body(request) for request in self.requests(name)]

    def audio_chunks(self) -> list[bytes]:
        return [base64.b64decode(body["audio"]) for body in self.bodies("audio_input")]

    def received(self) -> list[str]:
        return [event for direction, event in self.log if direction == "recv"]

    def order(self) -> list[str]:
        """Every send and every recv, in one ordered list."""
        return [f"{direction}:{event}" for direction, event in self.log]


class FakeConnect:
    """The async context manager ``client.dictate_streaming.connect(...)`` returns."""

    def __init__(self, client: "FakeClient", kwargs: dict) -> None:
        self._client = client
        self._kwargs = kwargs

    async def __aenter__(self):
        if self._client.error is not None:
            raise self._client.error
        return self._client.socket

    async def __aexit__(self, exc_type, exc, tb):
        self._client.socket.exit_calls += 1
        return False


class FakeService:
    """The ``client.dictate_streaming`` namespace: just ``connect``."""

    def __init__(self, client: "FakeClient") -> None:
        self._client = client

    def connect(self, **kwargs) -> FakeConnect:
        self._client.calls.append(kwargs)
        return FakeConnect(self._client, kwargs)


class FakeClient:
    """A stand-in for ``sarvamai.AsyncSarvamAI``.

    ``connect`` records the keyword arguments it was handed - that list is what
    the connect-argument tests assert on - and yields (or raises) whatever the
    test scripted.
    """

    def __init__(self, socket: FakeSocket | None = None, error: Exception | None = None) -> None:
        self.socket = socket if socket is not None else FakeSocket()
        self.error = error
        self.calls: list[dict] = []
        self.dictate_streaming = FakeService(self)

    @property
    def kwargs(self) -> dict:
        assert self.calls, "connect() was never called"
        return self.calls[0]


# --------------------------------------------------------------------------
# the sibling module's payload builders, stubbed
# --------------------------------------------------------------------------


class DictateStub:
    """A stand-in for ``sarvam_dictation.dictate``'s three payload builders.

    ``style``/``vocabulary``/``context`` are what the builders return for this
    test; ``None`` means "the user set nothing", which the module under test must
    translate into "do not send the field". Whatever is set here is handed to the
    SDK untouched, so a test may put a plain dict *or* an SDK-shaped object in it.
    """

    def __init__(self) -> None:
        self.style: object | None = None
        self.vocabulary: object | None = None
        self.context: object | None = None
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
# server events
# --------------------------------------------------------------------------


def session_begin() -> Event:
    return Event("session.begin", request_id="req-1", config={})


def session_end() -> Event:
    return Event(
        "session.end",
        request_id="req-1",
        total_duration_s=1.2,
        total_utterances=1,
        audio_received_s=1.0,
        usage={},
    )


def config_updated(applied=("style",)) -> Event:
    return Event("config.updated", applied=list(applied), config={})


def partial(text: str, utterance_idx: int = 0, language: str | None = "hi-IN") -> Event:
    return Event(
        "transcript.partial",
        utterance_idx=utterance_idx,
        text=text,
        language_code=language,
        confidence=0.9,
    )


def transcript_final(text: str, utterance_idx: int = 0, language: str | None = "hi-IN") -> Event:
    return Event(
        "transcript.final",
        utterance_idx=utterance_idx,
        text=text,
        language_code=language,
    )


def dictation_final(
    text: str,
    utterance_idx: int = 0,
    language: str | None = "hi-IN",
    degraded: str | None = None,
) -> Event:
    return Event(
        "dictation.final",
        utterance_idx=utterance_idx,
        model="dictate-v1",
        text=text,
        corrections=[],
        degraded=degraded,
        language_code=language,
        usage={"audio_duration_s": 1.0, "words": 1},
    )


def output_final(text: str) -> Event:
    return Event("output.final", text=text, degraded=None, usage={})


def error_event(code: str, message: str, is_fatal: bool) -> Event:
    return Event("error", code=code, message=message, is_fatal=is_fatal)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


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
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def other_tasks() -> list[asyncio.Task]:
    """Every task still running in this loop except the current one."""
    current = asyncio.current_task()
    return [task for task in asyncio.all_tasks() if task is not current and not task.done()]


def opened(config, socket=None, **kwargs):
    """Build a transcriber wired to a fake SDK client, plus that client."""
    client = FakeClient(socket if socket is not None else FakeSocket(), **kwargs)
    return DictateRealtimeTranscriber(config, client=client), client


# --------------------------------------------------------------------------
# the module surface and its optional dependency
# --------------------------------------------------------------------------


def test_the_module_imports_without_the_sdk():
    source = Path(dictate_streaming.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        assert "sarvamai" not in names, "the SDK must be imported lazily, inside a method"
        assert "websockets" not in names, "the SDK owns the transport"
        assert "httpx" not in names, "the SDK owns the transport"


def test_a_fresh_import_works_while_the_sdk_is_blocked():
    name = "sarvam_dictation.dictate_streaming"
    saved = sys.modules.pop(name, None)
    try:
        with module_blocked("sarvamai"):
            module = importlib.import_module(name)
        assert module.DictateRealtimeTranscriber is not None
    finally:
        sys.modules.pop(name, None)
        if saved is not None:
            sys.modules[name] = saved


def test_an_injected_client_never_imports_the_sdk(make_config):
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])
    was_loaded = "sarvamai" in sys.modules

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        with module_blocked("sarvamai"):
            await transcriber.open()
            await transcriber.feed(b"\x00\x00" * 32)
            return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"
    if was_loaded:
        pytest.skip("sarvamai is already imported in this interpreter")
    assert "sarvamai" not in sys.modules


def test_the_raw_transport_machinery_is_gone():
    # The SDK builds the URL, the query string and the headers now.
    for name in ("DICTATE_WS_URL", "_connect", "_decode", "_url"):
        assert not hasattr(dictate_streaming, name), f"{name} should not survive the rewrite"


def test_a_missing_sdk_is_an_internal_error_that_names_the_install_line(make_config):
    transcriber = DictateRealtimeTranscriber(make_config())

    async def scenario():
        with module_blocked("sarvamai"):
            with pytest.raises(StreamingError) as excinfo:
                await transcriber.open()
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.code == "internal"
    assert "pip install 'sarvamai>=0.1.49'" in str(error)


def test_the_error_codes_are_the_protocol_vocabulary():
    assert StreamingError("boom", code="api").code == "api"
    assert StreamingError("boom").code == "internal"
    assert StreamingError("bad", code="config_invalid").code == "config_invalid"
    assert StreamingError("gone", code="network").code == "network"
    assert StreamingError("key", code="no_api_key").code == "no_api_key"


def test_the_exported_names_are_the_ones_the_daemon_imports():
    for name in ("DictateRealtimeTranscriber", "StreamingError", "Transcript"):
        assert name in dictate_streaming.__all__
    # StreamingError and Transcript are the streaming module's, not new copies.
    import sarvam_dictation.streaming as streaming

    assert StreamingError is streaming.StreamingError
    assert Transcript is streaming.Transcript


def test_the_public_interface_is_unchanged():
    signature = inspect.signature(DictateRealtimeTranscriber.__init__)
    assert list(signature.parameters) == ["self", "config", "on_partial", "client"]
    assert signature.parameters["on_partial"].default is None
    assert signature.parameters["client"].default is None
    for name in ("open", "feed", "finish", "cancel"):
        assert inspect.iscoroutinefunction(getattr(DictateRealtimeTranscriber, name))


# --------------------------------------------------------------------------
# pre-flight: nothing is opened when the settings cannot work
# --------------------------------------------------------------------------


def test_open_without_an_api_key_raises_before_any_socket_is_opened(make_config):
    transcriber, client = opened(make_config(api_key=""))

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "no_api_key"

    asyncio.run(scenario())
    assert client.calls == []


def test_open_refuses_a_mode_dictate_does_not_have(make_config):
    transcriber, client = opened(make_config(mode="translate"))

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "config_invalid"
        assert "translate" in str(excinfo.value)

    asyncio.run(scenario())
    assert client.calls == []


@pytest.mark.parametrize("rate", [44100, 22050, 48000, 11025])
def test_open_refuses_a_sample_rate_the_socket_cannot_serve(make_config, rate):
    transcriber, client = opened(make_config(sample_rate=rate))

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "config_invalid"
        assert str(rate) in str(excinfo.value)

    asyncio.run(scenario())
    assert client.calls == []


@pytest.mark.parametrize("rate", list(DICTATE_SAMPLE_RATES))
def test_open_accepts_every_sample_rate_the_socket_documents(make_config, rate):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(sample_rate=rate), socket)
        await transcriber.open()
        await transcriber.cancel()

    asyncio.run(scenario())
    assert socket.names()[:1] == ["speech_start"]


def test_open_refuses_an_unknown_stream_type(make_config):
    transcriber, client = opened(make_config(dictate_stream_type="turbo"))

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "config_invalid"

    asyncio.run(scenario())
    assert client.calls == []


# --------------------------------------------------------------------------
# the connect() arguments
# --------------------------------------------------------------------------


def test_connect_is_asked_for_exactly_the_documented_settings(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, client = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()
        return client.kwargs

    kwargs = asyncio.run(scenario())
    assert kwargs == {
        "language_code": "auto",
        "mode": "transcribe",
        "model": None,
        "endpointing": "manual",
        "stream_type": "balanced",
        "encoding": "linear16",
        "sample_rate": "16000",
    }


def test_the_sample_rate_is_sent_as_a_string(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, client = opened(make_config(sample_rate=8000), socket)
        await transcriber.open()
        await transcriber.cancel()
        return client.kwargs["sample_rate"]

    assert asyncio.run(scenario()) == "8000"


def test_an_empty_language_code_becomes_auto(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, client = opened(make_config(language_code=""), socket)
        await transcriber.open()
        await transcriber.cancel()
        return client.kwargs["language_code"]

    # ``auto`` is a real value on this socket: an empty config means "detect".
    assert asyncio.run(scenario()) == "auto"


def test_a_configured_language_code_is_passed_through(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, client = opened(make_config(language_code="hi-IN"), socket)
        await transcriber.open()
        await transcriber.cancel()
        return client.kwargs["language_code"]

    assert asyncio.run(scenario()) == "hi-IN"


def test_the_model_is_not_pinned_unless_the_user_pinned_one(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, client = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()
        return client.kwargs["model"]

    assert asyncio.run(scenario()) is None


def test_a_pinned_model_reaches_the_connect_call(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, client = opened(
            make_config(dictate_model="dictate-v1-2026-11-04"), socket
        )
        await transcriber.open()
        await transcriber.cancel()
        return client.kwargs["model"]

    assert asyncio.run(scenario()) == "dictate-v1-2026-11-04"


def test_the_stream_type_comes_from_the_config(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, client = opened(make_config(dictate_stream_type="fast"), socket)
        await transcriber.open()
        await transcriber.cancel()
        return client.kwargs["stream_type"]

    assert asyncio.run(scenario()) == "fast"


def test_endpointing_is_manual_so_the_hotkey_owns_the_turn(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, client = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()
        return client.kwargs["endpointing"], client.kwargs["encoding"]

    assert asyncio.run(scenario()) == ("manual", "linear16")


def test_a_client_without_a_dictate_streaming_service_is_an_internal_error(make_config):
    class Bare:
        pass

    transcriber = DictateRealtimeTranscriber(make_config(), client=Bare())

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "internal"
        assert "dictate_streaming" in str(excinfo.value)

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# the session order: session.begin, one config.update, config.updated,
# speech_start
# --------------------------------------------------------------------------


def test_one_config_update_carries_the_builders_values(make_config, dictate_stub):
    dictate_stub.style = {"preset": "clean", "rules": "no emoji"}
    dictate_stub.vocabulary = [{"word": "Sarvam"}]
    dictate_stub.context = {"text": "a note to a colleague"}
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(dictate_output_rules="bullets"), socket)
        await transcriber.open()
        await transcriber.cancel()

    asyncio.run(scenario())
    assert socket.names().count("config.update") == 1
    assert _body(socket.request("config.update")) == {
        "event": "config.update",
        "style": {"preset": "clean", "rules": "no emoji"},
        "vocabulary": [{"word": "Sarvam"}],
        "context": {"text": "a note to a colleague"},
        "output_rules": "bullets",
    }


def test_the_builders_values_are_passed_straight_through(make_config, dictate_stub):
    # Whatever the sibling returns - a dict now, an SDK model later - is handed
    # to the SDK unchanged: this module does not know or care which it is.
    style = types.SimpleNamespace(preset="light")
    vocabulary = [types.SimpleNamespace(word="Velqora")]
    dictate_stub.style = style
    dictate_stub.vocabulary = vocabulary
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()

    asyncio.run(scenario())
    body = _body(socket.request("config.update"))
    assert body["style"] is style
    assert body["vocabulary"] is vocabulary


def test_a_builder_value_the_sdk_cannot_take_is_rehanded_as_plain_data():
    # The SDK's streaming models and its clip models are separate classes, and
    # pydantic refuses one where the other is declared; ``_plain`` is the escape
    # hatch that keeps either shape working.
    class ClipStyle:
        def dict(self, **kwargs):
            assert kwargs == {"exclude_none": True}
            return {"preset": "clean"}

    assert dictate_streaming._plain(ClipStyle()) == {"preset": "clean"}
    assert dictate_streaming._plain([ClipStyle()]) == [{"preset": "clean"}]
    assert dictate_streaming._plain("plain") == "plain"


def test_a_field_the_builder_returned_none_for_is_not_sent(make_config, dictate_stub):
    dictate_stub.style = {"preset": "clean"}
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()

    asyncio.run(scenario())
    body = _body(socket.request("config.update"))
    assert body == {"event": "config.update", "style": {"preset": "clean"}}
    assert "vocabulary" not in body
    assert "context" not in body


def test_no_config_update_is_sent_when_nothing_is_configured(make_config):
    # An update that changes nothing is refused by the service, so an empty
    # [dictate] table means "do not send one at all".
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()

    asyncio.run(scenario())
    assert socket.names() == ["speech_start", "end"]


def test_the_output_rules_are_sent_but_an_empty_one_is_not(make_config, dictate_stub):
    dictate_stub.style = {"preset": "clean"}
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(dictate_output_rules="   "), socket)
        await transcriber.open()
        await transcriber.cancel()

    asyncio.run(scenario())
    assert "output_rules" not in _body(socket.request("config.update"))


def test_speech_start_is_sent_only_after_config_updated(make_config, dictate_stub):
    dictate_stub.style = {"preset": "clean"}
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()

    asyncio.run(scenario())
    order = socket.order()
    assert order.index("send:config.update") < order.index("recv:config.updated")
    assert order.index("recv:config.updated") < order.index("send:speech_start")


def test_open_waits_for_session_begin_before_sending_anything(make_config):
    socket = FakeSocket([Pause(0.02), session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        opening = asyncio.ensure_future(transcriber.open())
        await asyncio.sleep(0.01)
        early = socket.names()
        await opening
        await transcriber.cancel()
        return early

    assert asyncio.run(scenario()) == []
    assert "speech_start" in socket.names()


def test_open_fails_when_config_updated_never_arrives(make_config, dictate_stub, monkeypatch):
    monkeypatch.setattr(dictate_streaming, "CONFIG_UPDATED_TIMEOUT_S", 0.05)
    dictate_stub.style = {"preset": "clean"}
    dictate_stub_socket = FakeSocket([session_begin()], respond_to_config=False)

    async def scenario():
        transcriber, _ = opened(make_config(), dictate_stub_socket)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.code == "api"
    assert "config.updated" in str(error)
    assert dictate_stub_socket.exit_calls == 1, "the session must be closed on failure"


def test_open_fails_when_session_begin_never_arrives(make_config, monkeypatch):
    monkeypatch.setattr(dictate_streaming, "SESSION_BEGIN_TIMEOUT_S", 0.05)
    socket = FakeSocket()

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.code == "api"
    assert "session.begin" in str(error)


def test_a_refused_config_update_does_not_kill_the_session(make_config, dictate_stub, caplog):
    # With nothing configured the service answers an update that changes nothing
    # with invalid_config. Refusing to dictate over that would be absurd.
    dictate_stub.style = {"preset": "clean"}
    socket = FakeSocket(
        [
            session_begin(),
            # After the handshake: the rejection only counts as the answer to the
            # update while the update is still unconfirmed.
            Pause(0.01),
            error_event("invalid_config", "nothing to change", False),
        ],
        respond_to_config=False,
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())
    assert "speech_start" in socket.names()
    assert "did not apply config.update" in caplog.text


def test_a_fatal_error_before_session_begin_fails_open_immediately(make_config):
    socket = FakeSocket([error_event("asr_error", "recognition stopped", True)])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.code == "api"
    assert "asr_error" in str(error)


def test_a_403_at_the_handshake_is_an_api_error_not_a_network_one(make_config):
    transcriber, client = opened(make_config(), error=ApiError(403, "forbidden"))

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.code == "api"
    assert "403" in str(error)


def test_a_transport_failure_at_the_handshake_is_a_network_error(make_config):
    transcriber, client = opened(make_config(), error=ConnectionError("connection refused"))

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.code == "network"


def test_a_handshake_error_message_never_carries_the_api_key(make_config):
    transcriber, client = opened(make_config(api_key=FAKE_KEY), error=ApiError(401, FAKE_KEY))

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        return str(excinfo.value)

    message = asyncio.run(scenario())
    assert FAKE_KEY not in message
    assert "***" in message


def test_a_missing_payload_builder_module_is_an_internal_error(make_config, monkeypatch):
    # ``None`` in ``sys.modules`` makes the import raise ImportError even though
    # the file exists, which is what "a broken install" actually looks like. A
    # plain delitem would let Python re-import the real module and the test
    # would pass while proving nothing.
    monkeypatch.setitem(sys.modules, "sarvam_dictation.dictate", None)
    transcriber, client = opened(make_config())

    async def scenario():
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.open()
        assert excinfo.value.code == "internal"
        assert "dictate" in str(excinfo.value)

    asyncio.run(scenario())
    assert client.calls == [], "a broken install must fail before the socket is opened"


# --------------------------------------------------------------------------
# feed(): buffering to the per-message limit
# --------------------------------------------------------------------------


def test_feed_buffers_into_whole_second_blocks_of_raw_pcm(make_config):
    # 1 s of 16 kHz s16le mono is 32000 bytes: three halves of a second must
    # produce one message, and the last half must stay buffered for finish().
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.feed(b"\x01\x02" * 8000)  # 0.5 s
        await transcriber.feed(b"\x03\x04" * 8000)  # 0.5 s
        sent = socket.audio_chunks()
        await transcriber.cancel()
        return sent

    chunks = asyncio.run(scenario())
    assert len(chunks) == 1
    assert len(chunks[0]) == 32000
    assert chunks[0] == b"\x01\x02" * 8000 + b"\x03\x04" * 8000


def test_feed_splits_one_oversized_chunk_instead_of_losing_it(make_config):
    socket = FakeSocket([session_begin()])
    pcm = bytes(range(256)) * 250  # 64000 bytes, i.e. two seconds

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.feed(pcm)
        sent = socket.audio_chunks()
        await transcriber.cancel()
        return sent

    chunks = asyncio.run(scenario())
    assert [len(chunk) for chunk in chunks] == [32000, 32000]
    assert b"".join(chunks) == pcm


def test_feed_uses_half_second_blocks_with_the_fast_stream_type(make_config):
    socket = FakeSocket([session_begin()])
    pcm = b"\x00\x01" * 16000  # one second

    async def scenario():
        transcriber, _ = opened(make_config(dictate_stream_type="fast"), socket)
        await transcriber.open()
        await transcriber.feed(pcm)
        sent = socket.audio_chunks()
        await transcriber.cancel()
        return sent

    chunks = asyncio.run(scenario())
    assert [len(chunk) for chunk in chunks] == [16000, 16000]


def test_feed_never_sends_an_empty_audio_message(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.feed(b"")
        await transcriber.feed(b"\x00\x00")
        await transcriber.cancel()

    asyncio.run(scenario())
    assert socket.requests("audio_input") == []
    assert socket.names() == ["speech_start", "end"]


def test_feed_before_open_is_a_no_op(make_config):
    socket = FakeSocket()

    async def scenario():
        transcriber, client = opened(make_config(), socket)
        await transcriber.feed(b"\x00\x00" * 32000)
        return client.calls, socket.names()

    calls, names = asyncio.run(scenario())
    assert calls == []
    assert names == []


def test_feed_after_finish_is_a_no_op(make_config):
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.finish()
        before = len(socket.requests("audio_input"))
        await transcriber.feed(b"\x00\x00" * 32000)
        return before, len(socket.requests("audio_input"))

    before, after = asyncio.run(scenario())
    assert before == after


def test_a_dead_socket_while_feeding_does_not_raise(make_config):
    socket = FakeSocket([session_begin()], fail_audio=True)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.feed(b"\x00\x00" * 32000)  # must not raise
        await transcriber.cancel()

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
            make_config(), on_partial=seen.append, client=FakeClient(socket)
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
            make_config(), on_partial=seen.append, client=FakeClient(socket)
        )
        await transcriber.open()
        await transcriber.finish()

    asyncio.run(scenario())
    assert seen == ["ठीक"]
    assert all(text.strip() for text in seen)


def test_a_stale_partial_for_a_finished_turn_is_not_repeated(make_config):
    seen: list[str] = []
    socket = FakeSocket([session_begin(), dictation_final("पहला"), partial("पहला और")])

    async def scenario():
        transcriber = DictateRealtimeTranscriber(
            make_config(), on_partial=seen.append, client=FakeClient(socket)
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
            make_config(), on_partial=explode, client=FakeClient(socket)
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

    transcript = asyncio.run(scenario())
    assert transcript.text == "नमस्ते"
    assert socket.names()[-2:] == ["speech_end", "end"]
    assert "recv:session.end" in socket.order()
    assert socket.exit_calls == 1


def test_the_audio_tail_is_flushed_before_speech_end(make_config):
    # The last word of every recording lives in the sub-second tail: it must be
    # sent before speech_end or it is never transcribed.
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.feed(b"\x05\x06" * 100)  # 400 bytes, well under 1 s
        await transcriber.finish()
        return socket.audio_chunks()

    chunks = asyncio.run(scenario())
    assert chunks == [b"\x05\x06" * 100]
    order = socket.order()
    assert order.index("send:audio_input") < order.index("send:speech_end")


def test_turns_are_joined_in_utterance_idx_order_even_when_they_arrive_out_of_order(
    make_config,
):
    # The contract allows a later turn's transcript.final to arrive before an
    # earlier turn's dictation.final: arrival order says nothing about speech.
    socket = FakeSocket(
        [
            session_begin(),
            transcript_final("दूसरा", utterance_idx=1),
            dictation_final("दूसरा", utterance_idx=1),
            transcript_final("पहला", utterance_idx=0),
            dictation_final("पहला", utterance_idx=0),
        ]
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert transcript.text == "पहला दूसरा"
    assert transcript.segments == 2


def test_a_turn_with_empty_text_is_counted_but_skipped_in_the_text(make_config):
    socket = FakeSocket(
        [
            session_begin(),
            dictation_final("पहला", utterance_idx=0),
            dictation_final("", utterance_idx=1, degraded="asr_error"),
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

    transcript = asyncio.run(scenario())
    assert transcript.text == ""
    assert transcript.segments == 1
    assert transcript.language_code is None


def test_the_language_comes_from_the_first_turn_that_reports_one(make_config):
    socket = FakeSocket(
        [
            session_begin(),
            dictation_final("पहला", utterance_idx=0, language=None),
            dictation_final("दूसरा", utterance_idx=1, language="mr-IN"),
        ]
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    assert asyncio.run(scenario()).language_code == "mr-IN"


def test_output_final_does_not_change_the_text_and_does_not_break_the_loop(make_config):
    # output.final is read and discarded, and consuming it is what lets the read
    # loop reach session.end instead of stalling.
    socket = FakeSocket(
        [session_begin(), dictation_final("नमस्ते"), output_final("• Greeting\n• नमस्ते")]
    )

    async def scenario():
        transcriber, _ = opened(make_config(dictate_output_rules="bullets"), socket)
        await transcriber.open()
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert transcript.text == "नमस्ते"
    assert "recv:output.final" in socket.order()
    assert "recv:session.end" in socket.order()


def test_a_fatal_error_with_no_turn_text_maps_to_api(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        # Pushed after the handshake: a fatal error before config.updated would
        # (correctly) fail open() instead of finish().
        socket.push(error_event("model_overloaded", "busy", True))
        await pump(socket, 2)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.finish()
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.code == "api"
    assert "model_overloaded" in str(error)


def test_a_fatal_error_after_a_finished_turn_returns_the_dictation(make_config):
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        socket.push(error_event("asr_error", "stopped", True))
        await pump(socket, 2)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"


def test_a_non_fatal_error_is_logged_and_ignored(make_config, caplog):
    socket = FakeSocket(
        [
            session_begin(),
            error_event("invalid_audio", "chunk dropped", False),
            dictation_final("नमस्ते"),
        ]
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    with caplog.at_level("WARNING"):
        transcript = asyncio.run(scenario())
    assert transcript.text == "नमस्ते"
    assert "non-fatal Dictate error" in caplog.text


def test_a_non_1000_close_before_session_end_maps_to_network(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        socket.push(Closed(1011, "recognition stopped"))
        await pump(socket, 2)
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.finish()
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.code == "network"
    assert "1011" in str(error)


def test_a_non_1000_close_after_a_turn_returns_the_dictation(make_config):
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        socket.push(Closed(1013, "overloaded"))
        await pump(socket, 2)
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"


def test_a_1000_close_without_session_end_is_not_an_error(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        socket.push(Closed(1000, "bye"))
        await pump(socket, 2)
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert transcript.text == ""
    assert transcript.segments == 1


def test_a_session_end_that_never_arrives_is_a_network_error(make_config, monkeypatch):
    monkeypatch.setattr(dictate_streaming, "SESSION_END_TIMEOUT_S", 0.05)
    socket = FakeSocket([session_begin()], respond_to_end=False)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        with pytest.raises(StreamingError) as excinfo:
            await transcriber.finish()
        return excinfo.value

    error = asyncio.run(scenario())
    assert error.code == "network"
    assert "session.end" in str(error)


def test_a_missing_session_end_still_returns_the_turns_already_finished(
    make_config, monkeypatch, caplog
):
    monkeypatch.setattr(dictate_streaming, "SESSION_END_TIMEOUT_S", 0.05)
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")], respond_to_end=False)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    with caplog.at_level("WARNING"):
        transcript = asyncio.run(scenario())
    assert transcript.text == "नमस्ते"
    assert "returning 1 finished turn" in caplog.text


def test_finish_is_safe_when_open_was_never_called(make_config):
    transcriber, client = opened(make_config())

    async def scenario():
        return await transcriber.finish()

    transcript = asyncio.run(scenario())
    assert transcript.text == ""
    assert transcript.segments == 1
    assert client.calls == []


def test_finish_can_be_called_twice(make_config):
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        first = await transcriber.finish()
        second = await transcriber.finish()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second
    assert socket.names().count("end") == 1


def test_an_unknown_event_is_ignored(make_config):
    socket = FakeSocket(
        [session_begin(), Event("brand.new.event", text="?"), dictation_final("नमस्ते")]
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"


def test_a_plain_dict_message_is_accepted_too(make_config):
    # The SDK hands back its event models, but its recv() also passes an
    # unparseable message through as a dict; both must be readable.
    socket = FakeSocket(
        [
            session_begin(),
            {"event": "dictation.final", "utterance_idx": 0, "text": "नमस्ते"},
        ]
    )

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        return await transcriber.finish()

    assert asyncio.run(scenario()).text == "नमस्ते"


# --------------------------------------------------------------------------
# cancel() and the keepalive
# --------------------------------------------------------------------------


def test_cancel_leaves_no_task_behind(make_config):
    socket = FakeSocket([session_begin()], block_forever=True)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.cancel()
        assert socket.exit_calls == 1, "the SDK context manager must be exited"
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
    assert socket.names() == ["speech_start", "end"]
    assert socket.exit_calls == 1


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
    assert socket.names().count("end") == 1


def test_cancel_before_open_is_a_no_op(make_config):
    transcriber, client = opened(make_config())

    async def scenario():
        await transcriber.cancel()
        await transcriber.cancel()
        assert other_tasks() == []

    asyncio.run(scenario())
    assert client.calls == []


def test_a_cancelled_session_does_not_wait_for_a_dictation(make_config):
    socket = FakeSocket([session_begin()], block_forever=True)

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        started = asyncio.get_running_loop().time()
        await transcriber.cancel()
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(scenario()) < 1.0


def test_a_cancelled_session_does_not_raise_for_a_dead_socket(make_config):
    socket = FakeSocket([session_begin()])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        socket.push(Closed(1011, "recognition stopped"))
        await pump(socket, 2)  # let the receiver see the close
        await transcriber.cancel()
        assert other_tasks() == []

    asyncio.run(scenario())


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
    assert "ping" in socket.names()
    assert _body(socket.request("ping")) == {"event": "ping"}


def test_the_keepalive_stops_when_the_session_ends(make_config, monkeypatch):
    monkeypatch.setattr(dictate_streaming, "KEEPALIVE_INTERVAL_S", 0.01)
    socket = FakeSocket([session_begin(), dictation_final("नमस्ते")])

    async def scenario():
        transcriber, _ = opened(make_config(), socket)
        await transcriber.open()
        await transcriber.finish()
        before = socket.names().count("ping")
        await asyncio.sleep(0.05)
        return before, socket.names().count("ping")

    before, after = asyncio.run(scenario())
    assert before == after, "a finished session must not keep pinging"


# --------------------------------------------------------------------------
# the frozen configuration surface this module codes against
# --------------------------------------------------------------------------


def test_the_modes_this_module_accepts_are_the_config_ones():
    # Four modes, no translate: config.py owns the tuple, this module only reads it.
    assert "translate" not in DICTATE_MODES
    assert set(DICTATE_MODES) == {"transcribe", "verbatim", "translit", "codemix"}

"""Server tests: the full §4 contract driven over a real Unix socket.

No pytest-asyncio: every scenario is an ``async def`` executed with
``asyncio.run`` from an ordinary test function. The server binds a socket
inside ``tmp_path``, real clients connect with ``asyncio.open_unix_connection``,
and the recorder/transcriber are the fakes from ``conftest`` injected through
``recorder_factory``/``transcriber_factory``.

Ordering is never assumed where the brief does not fix it: helpers read until a
predicate matches, and "nothing else arrived" is proved with a ping barrier
rather than a sleep.

The last two sections cover brief 2, section 6 (the ``reload`` command, its
``reloaded``/``config_invalid`` replies, and the guarantee that a reload never
touches work that is already recording or transcribing) and brief 7 (the live
streaming path: partials to the owner only, throttled, then a ``result``).

The streaming fakes mirror the frozen ``StreamingRecorder`` /
``RealtimeTranscriber`` contracts (asynchronous ``start``/``chunks``/``stop``/
``cancel`` and ``open``/``feed``/``finish``/``cancel``) and are injected through
``stream_recorder_factory`` and ``stream_transcriber_factory``. No audio
hardware, no socket to Sarvam, and no ``sarvamai`` SDK is touched.

The final section covers brief 15: the transport is chosen per dictation mode,
and the command name carries the mode. ``start`` is the push-to-talk shape and
reads ``[api] streaming_push_to_talk``; ``toggle`` is the record-and-send shape
and reads ``[api] streaming_toggle``. No protocol message changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket as socket_module
import stat
import threading
from pathlib import Path

import pytest

import sarvam_dictation.server as server_module
from sarvam_dictation.config import Config, load_config
from sarvam_dictation.server import PARTIAL_INTERVAL, DictationServer

READ_TIMEOUT = 2.0
QUIET_WINDOW = 0.15
GATE_TIMEOUT = 5.0
PLACEHOLDER_KEY = "YOUR_SARVAM_API_KEY"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class ClientClosed(Exception):
    """The server closed the connection."""


class Client:
    """A tiny newline-delimited-JSON client for the daemon socket."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.received = []

    @classmethod
    async def connect(cls, path):
        reader, writer = await asyncio.open_unix_connection(str(path))
        return cls(reader, writer)

    async def send(self, message):
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        await self.send_raw(payload)

    async def send_raw(self, data: bytes):
        self.writer.write(data)
        await self.writer.drain()

    async def read(self, timeout=READ_TIMEOUT):
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        if not line:
            raise ClientClosed("server closed the connection")
        message = json.loads(line.decode("utf-8"))
        self.received.append(message)
        return message

    async def read_until(self, predicate, timeout=READ_TIMEOUT):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(
                    f"no matching message within {timeout}s; got {self.received!r}"
                )
            message = await self.read(remaining)
            if predicate(message):
                return message

    async def read_until_all(self, predicates, timeout=READ_TIMEOUT):
        """Read until every predicate has matched at least once."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        found = {}
        while len(found) < len(predicates):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(
                    f"not all of {len(predicates)} messages arrived; got {self.received!r}"
                )
            message = await self.read(remaining)
            for index, predicate in enumerate(predicates):
                if index not in found and predicate(message):
                    found[index] = message
        return [found[index] for index in range(len(predicates))]

    async def drain_quiet(self, window=QUIET_WINDOW):
        """Collect whatever is already in flight, then give up quickly."""
        collected = []
        while True:
            try:
                collected.append(await self.read(window))
            except asyncio.TimeoutError:
                return collected

    async def barrier(self):
        """Round-trip a ping so everything ordered before it has been read."""
        await self.send({"v": 1, "cmd": "ping"})
        return await self.read_until(is_type("pong"))

    async def close(self):
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


def is_type(kind):
    return lambda message: message.get("type") == kind


def is_state(state):
    return lambda message: message.get("type") == "state" and message.get("state") == state


def is_error(code=None):
    if code is None:
        return lambda message: message.get("type") == "error"
    return lambda message: message.get("type") == "error" and message.get("code") == code


@contextlib.asynccontextmanager
async def running(server):
    """Start the server, serve in the background, and always shut it down."""
    await server.start()
    task = asyncio.create_task(server.serve_forever())
    try:
        yield server
    finally:
        with contextlib.suppress(Exception):
            await server.shutdown()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def make_server(config, recorder_factory, transcriber_factory, socket_path, config_path=None):
    return DictationServer(
        config,
        recorder_factory=recorder_factory,
        transcriber_factory=transcriber_factory,
        socket_path=str(socket_path),
        config_path=None if config_path is None else str(config_path),
    )


def make_default_server(config, socket_path, config_path=None):
    """A server that builds its recorder/transcriber through the real defaults."""
    return DictationServer(
        config,
        socket_path=str(socket_path),
        config_path=None if config_path is None else str(config_path),
    )


# --------------------------------------------------------------------------
# binding
# --------------------------------------------------------------------------


def test_socket_file_is_created_with_owner_only_permissions(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            assert os.path.realpath(server.socket_path) == os.path.realpath(str(sock))
            assert sock.exists()
            assert stat.S_IMODE(sock.stat().st_mode) == 0o600

    asyncio.run(scenario())


def test_start_replaces_a_stale_socket_file(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "stale.sock"
    stale = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    stale.bind(str(sock))
    stale.close()
    assert sock.exists()

    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            assert (await client.barrier())["type"] == "pong"
            await client.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# ping / pong
# --------------------------------------------------------------------------


def test_ping_replies_with_a_bare_pong(tmp_path, config, recorder_factory, transcriber_factory):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "ping"})
            assert await client.read_until(is_type("pong")) == {"v": 1, "type": "pong"}
            await client.close()

    asyncio.run(scenario())


def test_ping_is_answered_while_recording(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "id-1"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "ping"})
            assert (await client.read_until(is_type("pong")))["type"] == "pong"
            await client.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# start / stop / result
# --------------------------------------------------------------------------


def test_start_then_stop_delivers_recording_transcribing_result_and_idle(
    tmp_path, config, recorder_factory, transcriber_factory, fake_transcriber
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)
    fake_transcriber.text = "नमस्ते दुनिया"
    fake_transcriber.language_code = "hi-IN"
    fake_transcriber.segments = 1

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)

            await client.send({"v": 1, "cmd": "start", "id": "start-id"})
            recording = await client.read_until(is_state("recording"))
            assert recording["id"] == "start-id"

            await client.send({"v": 1, "cmd": "stop", "id": "stop-id"})
            transcribing = await client.read_until(is_state("transcribing"))
            assert transcribing["id"] == "stop-id"

            result, idle = await client.read_until_all([is_type("result"), is_state("idle")])
            assert result["id"] == "stop-id"
            assert result["text"] == "नमस्ते दुनिया"
            assert result["language_code"] == "hi-IN"
            assert result["segments"] == 1
            assert result["duration_ms"] == recorder_factory.recorder.duration_ms
            assert isinstance(result["duration_ms"], int)
            assert idle["id"] == "stop-id"

            await client.close()

    asyncio.run(scenario())


def test_stop_calls_the_recorder_and_the_transcriber(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "a"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "b"})
            await client.read_until(is_type("result"))
            await client.close()

    asyncio.run(scenario())
    assert recorder_factory.recorder.start_calls == 1
    assert recorder_factory.recorder.stop_calls == 1
    assert len(transcriber_factory.transcriber.calls) == 1


def test_empty_transcript_is_still_delivered_as_a_result(
    tmp_path, config, recorder_factory, transcriber_factory, fake_transcriber
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)
    fake_transcriber.text = ""

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "a"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "b"})
            result = await client.read_until(is_type("result"))
            assert result["text"] == ""
            assert result["id"] == "b"
            await client.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# toggle
# --------------------------------------------------------------------------


def test_toggle_starts_when_idle_and_stops_when_recording(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)

            await client.send({"v": 1, "cmd": "toggle", "id": "t1"})
            recording = await client.read_until(is_state("recording"))
            assert recording["id"] == "t1"

            await client.send({"v": 1, "cmd": "toggle", "id": "t2"})
            await client.read_until(is_state("transcribing"))
            result = await client.read_until(is_type("result"))
            assert result["id"] == "t2"
            await client.read_until(is_state("idle"))

            await client.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# errors: busy and bad_request
# --------------------------------------------------------------------------


def test_second_start_while_recording_reports_busy(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            owner = await Client.connect(sock)
            other = await Client.connect(sock)

            await owner.send({"v": 1, "cmd": "start", "id": "owner-start"})
            await owner.read_until(is_state("recording"))

            await other.send({"v": 1, "cmd": "start", "id": "other-start"})
            error = await other.read_until(is_error("busy"))
            assert error["id"] == "other-start"
            assert error["v"] == 1

            await owner.close()
            await other.close()

    asyncio.run(scenario())


def test_stop_when_idle_reports_busy(tmp_path, config, recorder_factory, transcriber_factory):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "stop", "id": "stop-1"})
            error = await client.read_until(is_error("busy"))
            assert error["id"] == "stop-1"
            await client.close()

    asyncio.run(scenario())


def test_stop_from_a_non_owner_reports_busy_with_the_documented_message(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            owner = await Client.connect(sock)
            other = await Client.connect(sock)

            await owner.send({"v": 1, "cmd": "start", "id": "o1"})
            await owner.read_until(is_state("recording"))
            await other.read_until(is_state("recording"))

            await other.send({"v": 1, "cmd": "stop", "id": "o2"})
            error = await other.read_until(is_error("busy"))
            assert error["id"] == "o2"
            assert "another client owns the recording" in error["message"]

            await owner.close()
            await other.close()

    asyncio.run(scenario())


def test_unknown_command_reports_bad_request_with_null_id(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "frobnicate", "id": "whatever"})
            error = await client.read_until(is_error("bad_request"))
            assert error["id"] is None
            await client.barrier()
            await client.close()

    asyncio.run(scenario())


def test_unparseable_line_reports_bad_request_and_keeps_the_connection(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send_raw(b"this is not json\n")
            error = await client.read_until(is_error("bad_request"))
            assert error["id"] is None

            # The daemon must survive garbage and still answer a ping.
            await client.send({"v": 1, "cmd": "ping"})
            assert (await client.read_until(is_type("pong")))["type"] == "pong"
            await client.close()

    asyncio.run(scenario())


def test_command_without_an_id_reports_bad_request(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start"})
            error = await client.read_until(is_error("bad_request"))
            assert error["id"] is None
            await client.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# broadcast vs owner-only delivery
# --------------------------------------------------------------------------


def test_state_changes_reach_every_client_but_the_result_reaches_only_the_owner(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            owner = await Client.connect(sock)
            watcher = await Client.connect(sock)

            await owner.send({"v": 1, "cmd": "start", "id": "b1"})
            owner_recording = await owner.read_until(is_state("recording"))
            watcher_recording = await watcher.read_until(is_state("recording"))
            assert owner_recording["id"] == "b1"
            assert watcher_recording["id"] == "b1"

            await owner.send({"v": 1, "cmd": "stop", "id": "b2"})
            await owner.read_until(is_type("result"))
            watcher_transcribing = await watcher.read_until(is_state("transcribing"))
            assert watcher_transcribing["id"] == "b2"
            await watcher.read_until(is_state("idle"))

            leftovers = await watcher.drain_quiet()
            assert not any(message.get("type") == "result" for message in watcher.received)
            assert not any(message.get("type") == "result" for message in leftovers)

            await owner.close()
            await watcher.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# cancellation and ownership loss
# --------------------------------------------------------------------------


def test_cancel_while_recording_discards_and_returns_to_idle(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)

            await client.send({"v": 1, "cmd": "start", "id": "c1"})
            await client.read_until(is_state("recording"))

            await client.send({"v": 1, "cmd": "cancel", "id": "c2"})
            idle = await client.read_until(is_state("idle"))
            assert idle["id"] == "c2"

            await client.barrier()
            assert not any(message.get("type") == "result" for message in client.received)

            # A fresh recording must still be possible afterwards.
            await client.send({"v": 1, "cmd": "start", "id": "c3"})
            assert (await client.read_until(is_state("recording")))["id"] == "c3"

            await client.close()

    asyncio.run(scenario())
    assert recorder_factory.recorder.cancel_calls >= 1
    assert transcriber_factory.transcriber.calls == []


def test_cancel_while_idle_is_not_an_error(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "cancel", "id": "x1"})
            await client.barrier()
            assert not any(message.get("type") == "error" for message in client.received)
            await client.close()

    asyncio.run(scenario())


def test_owner_disconnect_mid_recording_cancels_back_to_idle(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    server = make_server(config, recorder_factory, transcriber_factory, sock)

    async def scenario():
        async with running(server):
            owner = await Client.connect(sock)
            watcher = await Client.connect(sock)

            await owner.send({"v": 1, "cmd": "start", "id": "d1"})
            await owner.read_until(is_state("recording"))
            await watcher.read_until(is_state("recording"))

            await owner.close()

            idle = await watcher.read_until(is_state("idle"))
            assert idle["id"] is None or isinstance(idle["id"], str)

            # The recording is gone, so a new start from another client works.
            await watcher.send({"v": 1, "cmd": "start", "id": "d2"})
            assert (await watcher.read_until(is_state("recording")))["id"] == "d2"

            await watcher.close()

    asyncio.run(scenario())
    assert recorder_factory.recorder.cancel_calls >= 1
    assert transcriber_factory.transcriber.calls == []


# --------------------------------------------------------------------------
# reload (brief 2, section 6)
# --------------------------------------------------------------------------


class CapturingRecorder:
    """A ``Recorder`` stand-in that remembers the settings it was built with."""

    def __init__(self, cfg, sink, duration_ms=1234):
        self.config = cfg
        self.duration_ms = duration_ms
        self.wav_path = Path("/nonexistent/fake.wav")
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.cancel_calls = 0
        sink.append(self)

    @property
    def is_recording(self):
        return self.started

    def start(self):
        self.start_calls += 1
        self.started = True

    def stop(self):
        from sarvam_dictation.recorder import Recording

        self.stop_calls += 1
        self.started = False
        return Recording(path=self.wav_path, duration_ms=self.duration_ms)

    def cancel(self):
        self.cancel_calls += 1
        self.started = False


class CapturingTranscriber:
    """A ``SarvamTranscriber`` stand-in that remembers its settings.

    ``gate`` (a ``threading.Event``) holds the worker thread inside
    ``transcribe`` so a test can act while the daemon is transcribing.
    """

    def __init__(self, cfg, sink, gate=None):
        self.config = cfg
        self.gate = gate
        self.calls = []
        sink.append(self)

    def transcribe(self, path):
        from sarvam_dictation.transcriber import Transcript

        if self.gate is not None:
            self.gate.wait(GATE_TIMEOUT)
        self.calls.append(path)
        return Transcript(text="नमस्ते", language_code="hi-IN", segments=1)


@pytest.fixture
def capture(monkeypatch):
    """Patch the real recorder/transcriber so tests see the settings applied.

    ``DictationServer``'s default factories build ``Recorder``/``SarvamTranscriber``
    from the module globals at call time, so patching those globals proves which
    settings a recording and its transcription actually got - not just what the
    server holds internally.
    """
    state = {"recorders": [], "transcribers": [], "gate": None}
    monkeypatch.setattr(
        server_module, "Recorder", lambda cfg: CapturingRecorder(cfg, state["recorders"])
    )
    monkeypatch.setattr(
        server_module,
        "SarvamTranscriber",
        lambda cfg: CapturingTranscriber(cfg, state["transcribers"], state["gate"]),
    )
    return state


def write_config_file(path, model="saaras:v3", mode="translate", backend="pulse", key=PLACEHOLDER_KEY):
    """Write a config.toml the daemon can load, and return its path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[api]\n"
        f'key = "{key}"\n'
        f'model = "{model}"\n'
        f'mode = "{mode}"\n'
        "\n"
        "[audio]\n"
        f'backend = "{backend}"\n'
        "\n"
        "[server]\n"
        "max_duration_s = 300\n",
        encoding="utf-8",
    )
    return path


def test_reload_applies_the_new_model_mode_and_backend_to_the_next_recording(
    tmp_path, config, capture
):
    sock = tmp_path / "dictation.sock"
    config_file = write_config_file(tmp_path / "config.toml")
    server = make_default_server(config, sock, config_file)

    async def scenario():
        async with running(server):
            assert server.config_path == config_file
            assert server.config.model == "saaras:v4"
            client = await Client.connect(sock)

            await client.send({"v": 1, "cmd": "reload", "id": "reload-1"})
            message = await client.read_until(is_type("reloaded"))
            assert message == {"v": 1, "type": "reloaded", "id": "reload-1"}

            assert server.config.model == "saaras:v3"
            assert server.config.mode == "translate"
            assert server.config.backend == "pulse"
            assert server.config.api_key == PLACEHOLDER_KEY

            await client.send({"v": 1, "cmd": "start", "id": "s1"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "s2"})
            result = await client.read_until(is_type("result"))
            assert result["id"] == "s2"
            await client.close()

    asyncio.run(scenario())

    assert len(capture["recorders"]) == 1
    applied = capture["recorders"][0].config
    assert (applied.model, applied.mode, applied.backend) == ("saaras:v3", "translate", "pulse")
    assert len(capture["transcribers"]) == 1
    assert capture["transcribers"][0].config.model == "saaras:v3"
    assert capture["transcribers"][0].config.mode == "translate"


def test_reload_follows_the_path_the_config_was_loaded_from(tmp_path, capture):
    sock = tmp_path / "dictation.sock"
    config_file = write_config_file(tmp_path / "config.toml")
    config = load_config(config_file)
    server = DictationServer(config, socket_path=str(sock))

    async def scenario():
        async with running(server):
            assert server.config_path == config_file
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "reload", "id": "r1"})
            assert (await client.read_until(is_type("reloaded")))["id"] == "r1"
            assert server.config.model == "saaras:v3"
            await client.close()

    asyncio.run(scenario())


def test_reload_with_an_invalid_value_reports_config_invalid_and_keeps_the_old_settings(
    tmp_path, config, capture
):
    sock = tmp_path / "dictation.sock"
    config_file = write_config_file(tmp_path / "config.toml")
    server = make_default_server(config, sock, config_file)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "reload", "id": "good-1"})
            await client.read_until(is_type("reloaded"))
            assert server.config.mode == "translate"

            write_config_file(config_file, model="saaras:v3", mode="shout")
            await client.send({"v": 1, "cmd": "reload", "id": "bad-1"})
            error = await client.read_until(is_error("config_invalid"))
            assert error["id"] == "bad-1"
            assert error["v"] == 1
            assert "[api] mode" in error["message"]
            assert "shout" in error["message"]

            # Previous settings survive and the connection was not dropped.
            assert server.config.mode == "translate"
            assert server.config.model == "saaras:v3"
            assert (await client.barrier())["type"] == "pong"

            # The old settings still work for real work.
            await client.send({"v": 1, "cmd": "start", "id": "s1"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "s2"})
            assert (await client.read_until(is_type("result")))["id"] == "s2"

            # Fixing the file makes the next reload succeed.
            write_config_file(config_file, model="saaras:v3", mode="verbatim")
            await client.send({"v": 1, "cmd": "reload", "id": "good-2"})
            assert (await client.read_until(is_type("reloaded")))["id"] == "good-2"
            assert server.config.mode == "verbatim"
            await client.close()

    asyncio.run(scenario())

    assert [recorder.config.mode for recorder in capture["recorders"]] == ["translate"]


def test_reload_with_a_malformed_file_reports_config_invalid_and_keeps_the_connection(
    tmp_path, config, capture
):
    sock = tmp_path / "dictation.sock"
    config_file = write_config_file(tmp_path / "config.toml")
    server = make_default_server(config, sock, config_file)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            config_file.write_text("this is not toml at all\n", encoding="utf-8")

            await client.send({"v": 1, "cmd": "reload", "id": "bad-1"})
            error = await client.read_until(is_error("config_invalid"))
            assert error["id"] == "bad-1"
            assert error["message"]

            assert server.config == config
            assert (await client.barrier())["type"] == "pong"
            assert server.state == "idle"
            await client.close()

    asyncio.run(scenario())


def test_reload_with_a_missing_file_falls_back_to_the_defaults(tmp_path, config, capture):
    sock = tmp_path / "dictation.sock"
    config_file = write_config_file(tmp_path / "config.toml")
    server = make_default_server(config, sock, config_file)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "reload", "id": "r1"})
            await client.read_until(is_type("reloaded"))
            assert server.config.model == "saaras:v3"

            config_file.unlink()
            await client.send({"v": 1, "cmd": "reload", "id": "r2"})
            assert (await client.read_until(is_type("reloaded")))["id"] == "r2"

            assert server.config == Config()
            assert server.config.model == "saaras:v4"
            assert server.config.mode == "transcribe"
            assert server.config.backend == "auto"
            assert server.config.api_key == ""
            await client.close()

    asyncio.run(scenario())


def test_reload_while_recording_does_not_disturb_it_and_applies_to_the_next_one(
    tmp_path, config, capture
):
    sock = tmp_path / "dictation.sock"
    config_file = write_config_file(tmp_path / "config.toml")
    server = make_default_server(config, sock, config_file)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)

            await client.send({"v": 1, "cmd": "start", "id": "s1"})
            await client.read_until(is_state("recording"))
            assert server.state == "recording"

            await client.send({"v": 1, "cmd": "reload", "id": "r1"})
            assert (await client.read_until(is_type("reloaded")))["id"] == "r1"
            assert server.state == "recording"

            await client.send({"v": 1, "cmd": "stop", "id": "s2"})
            await client.read_until(is_state("transcribing"))
            result = await client.read_until(is_type("result"))
            assert result["id"] == "s2"
            assert result["text"] == "नमस्ते"
            await client.read_until(is_state("idle"))

            # The next recording picks the reloaded settings up.
            await client.send({"v": 1, "cmd": "start", "id": "s3"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "s4"})
            assert (await client.read_until(is_type("result")))["id"] == "s4"
            await client.close()

    asyncio.run(scenario())

    first_recording, second_recording = capture["recorders"]
    assert first_recording.config.model == "saaras:v4"
    assert second_recording.config.model == "saaras:v3"
    assert first_recording.stop_calls == 1
    assert first_recording.cancel_calls == 0

    first_transcription, second_transcription = capture["transcribers"]
    assert first_transcription.config.model == "saaras:v4"
    assert second_transcription.config.model == "saaras:v3"


def test_reload_while_transcribing_lets_the_transcription_finish_with_its_own_settings(
    tmp_path, config, capture
):
    sock = tmp_path / "dictation.sock"
    config_file = write_config_file(tmp_path / "config.toml")
    server = make_default_server(config, sock, config_file)
    gate = threading.Event()
    capture["gate"] = gate

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)

            await client.send({"v": 1, "cmd": "start", "id": "s1"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "s2"})
            await client.read_until(is_state("transcribing"))
            assert server.state == "transcribing"

            await client.send({"v": 1, "cmd": "reload", "id": "r1"})
            assert (await client.read_until(is_type("reloaded")))["id"] == "r1"
            assert server.state == "transcribing"
            assert server.config.model == "saaras:v3"

            gate.set()
            result = await client.read_until(is_type("result"))
            assert result["id"] == "s2"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())

    assert capture["transcribers"][0].config.model == "saaras:v4"


def test_reload_does_not_broadcast_a_state_message(tmp_path, config, capture):
    sock = tmp_path / "dictation.sock"
    config_file = write_config_file(tmp_path / "config.toml")
    server = make_default_server(config, sock, config_file)

    async def scenario():
        async with running(server):
            owner = await Client.connect(sock)
            watcher = await Client.connect(sock)

            await owner.send({"v": 1, "cmd": "reload", "id": "r1"})
            assert (await owner.read_until(is_type("reloaded")))["id"] == "r1"

            # Ping barrier: every message the daemon queued before the pong has
            # been read, so an empty tail really means "nothing else arrived".
            await owner.barrier()
            await watcher.barrier()

            assert not any(message.get("type") == "state" for message in owner.received)
            assert not any(message.get("type") == "state" for message in watcher.received)
            assert [message.get("type") for message in watcher.received] == ["pong"]

            await owner.close()
            await watcher.close()

    asyncio.run(scenario())


def test_reload_without_an_id_is_bad_request_and_changes_nothing(tmp_path, config, capture):
    sock = tmp_path / "dictation.sock"
    config_file = write_config_file(tmp_path / "config.toml")
    server = make_default_server(config, sock, config_file)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "reload"})
            error = await client.read_until(is_error("bad_request"))
            assert error["id"] is None

            await client.barrier()
            assert not any(message.get("type") == "reloaded" for message in client.received)
            assert server.config == config
            await client.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# streaming (brief 7)
# --------------------------------------------------------------------------


class FakeStreamingError(Exception):
    """A ``StreamingError`` stand-in.

    The daemon reads only ``.code`` from it, exactly as the frozen
    ``streaming.StreamingError`` contract specifies, so the real class - which
    lives in a module this unit does not own - is not needed here.
    """

    def __init__(self, message="streaming failed", code="api"):
        super().__init__(message)
        self.code = code


class FakeTranscript:
    """The ``streaming.Transcript`` shape the ``result`` path reads."""

    def __init__(self, text, language_code, segments):
        self.text = text
        self.language_code = language_code
        self.segments = segments


class FakeStreamingRecorder:
    """A ``StreamingRecorder`` stand-in: yields canned PCM, then blocks.

    ``chunks()`` ends when the child "exits", which the fake models with an
    event that ``stop()`` and ``cancel()`` set.
    """

    def __init__(self, duration_ms=1234, chunks=(b"\x00\x01" * 160, b"\x02\x03" * 160)):
        self.duration_ms = duration_ms
        self.chunks_offered = list(chunks)
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.cancel_calls = 0
        self.ended = asyncio.Event()

    async def start(self):
        self.start_calls += 1
        self.started = True

    async def chunks(self):
        for chunk in self.chunks_offered:
            yield chunk
        await self.ended.wait()

    async def stop(self):
        self.stop_calls += 1
        self.started = False
        self.ended.set()
        return self.duration_ms

    async def cancel(self):
        self.cancel_calls += 1
        self.started = False
        self.ended.set()


class FakeStreamingTranscriber:
    """A ``RealtimeTranscriber`` stand-in driven entirely from the test.

    ``partials`` are handed out one per ``feed`` call, so a partial can be tied
    to audio arriving; ``emit`` pushes one by hand. ``open_error``,
    ``feed_error`` and ``finish_error`` make the matching stage fail.
    """

    def __init__(
        self,
        on_partial=None,
        text="नमस्ते दुनिया",
        language_code="hi-IN",
        segments=2,
        partials=(),
        open_error=None,
        feed_error=None,
        finish_error=None,
    ):
        self.on_partial = on_partial
        self.text = text
        self.language_code = language_code
        self.segments = segments
        self.pending_partials = list(partials)
        self.open_error = open_error
        self.feed_error = feed_error
        self.finish_error = finish_error
        self.fed = []
        self.open_calls = 0
        self.finish_calls = 0
        self.cancel_calls = 0

    async def open(self):
        self.open_calls += 1
        if self.open_error is not None:
            raise self.open_error

    async def feed(self, pcm):
        if self.feed_error is not None:
            raise self.feed_error
        self.fed.append(pcm)
        if self.pending_partials:
            self.emit(self.pending_partials.pop(0))

    def emit(self, text):
        """Deliver one partial, exactly as the receiver task would."""
        if self.on_partial is not None:
            self.on_partial(text)

    async def finish(self):
        self.finish_calls += 1
        if self.finish_error is not None:
            raise self.finish_error
        return FakeTranscript(self.text, self.language_code, self.segments)

    async def cancel(self):
        self.cancel_calls += 1


class StreamRecorderFactory:
    """Callable ``stream_recorder_factory`` that exposes its instance."""

    def __init__(self, recorder):
        self.recorder = recorder
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.recorder


class StreamTranscriberFactory:
    """Callable ``stream_transcriber_factory``, called with ``on_partial``."""

    def __init__(self, transcriber):
        self.transcriber = transcriber
        self.calls = 0
        self.on_partials = []

    def __call__(self, on_partial):
        self.calls += 1
        self.on_partials.append(on_partial)
        self.transcriber.on_partial = on_partial
        return self.transcriber


class StreamKit:
    """The fakes plus their factories, so a test can assert on both."""

    def __init__(self, recorder=None, transcriber=None):
        self.recorder = recorder if recorder is not None else FakeStreamingRecorder()
        self.transcriber = (
            transcriber if transcriber is not None else FakeStreamingTranscriber()
        )
        self.recorder_factory = StreamRecorderFactory(self.recorder)
        self.transcriber_factory = StreamTranscriberFactory(self.transcriber)


def make_streaming_server(config, socket_path, kit=None, **kwargs):
    """A server whose live path is wired to the fakes in ``kit``."""
    kit = kit or StreamKit()
    server = DictationServer(
        config,
        stream_recorder_factory=kit.recorder_factory,
        stream_transcriber_factory=kit.transcriber_factory,
        socket_path=str(socket_path),
        **kwargs,
    )
    return server, kit


def partial_texts(client):
    return [m["text"] for m in client.received if m.get("type") == "partial"]


def is_partial(text=None):
    if text is None:
        return is_type("partial")
    return lambda message: message.get("type") == "partial" and message.get("text") == text


def test_the_partial_interval_is_a_tenth_of_a_second():
    assert PARTIAL_INTERVAL == pytest.approx(0.1)


@pytest.fixture
def streaming_config(make_config):
    """A ``Config`` with the live path on for both modes, model permitting.

    Brief 15 splits the switch per dictation mode, so the brief 7 scenarios -
    which drive ``start`` and ``toggle`` interchangeably - need both on.
    """
    return make_config(streaming_push_to_talk=True, streaming_toggle=True, model="saaras:v4")


def test_streaming_start_sends_a_partial_and_stop_sends_the_result(
    tmp_path, streaming_config
):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(
        recorder=FakeStreamingRecorder(duration_ms=2345),
        transcriber=FakeStreamingTranscriber(
            text="नमस्ते दुनिया", language_code="hi-IN", segments=2, partials=["नमस्ते"]
        ),
    )
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)

            await client.send({"v": 1, "cmd": "start", "id": "start-id"})
            assert (await client.read_until(is_state("recording")))["id"] == "start-id"

            first = await client.read_until(is_partial())
            assert first == {
                "v": 1,
                "type": "partial",
                "id": "start-id",
                "text": "नमस्ते",
            }

            kit.transcriber.emit("नमस्ते दुनिया")
            second = await client.read_until(is_partial("नमस्ते दुनिया"))
            assert second["id"] == "start-id"

            await client.send({"v": 1, "cmd": "stop", "id": "stop-id"})
            assert (await client.read_until(is_state("transcribing")))["id"] == "stop-id"

            result, idle = await client.read_until_all([is_type("result"), is_state("idle")])
            assert result["id"] == "stop-id"
            assert result["text"] == "नमस्ते दुनिया"
            assert result["language_code"] == "hi-IN"
            assert result["segments"] == 2
            assert result["duration_ms"] == 2345
            assert isinstance(result["duration_ms"], int)
            assert idle["id"] == "stop-id"
            await client.close()

    asyncio.run(scenario())
    assert kit.recorder.start_calls == 1
    assert kit.recorder.stop_calls == 1
    assert kit.transcriber.open_calls == 1
    assert kit.transcriber.finish_calls == 1
    assert kit.transcriber.fed, "the drain task must have fed the captured PCM"
    assert kit.transcriber_factory.calls == 1


def test_streaming_partials_carry_the_start_id_and_never_replace_the_result(
    tmp_path, streaming_config
):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(
        transcriber=FakeStreamingTranscriber(text="final text", partials=["a", "b", "c"])
    )
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "S"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "T"})
            result = await client.read_until(is_type("result"))
            await client.read_until(is_state("idle"))
            await client.close()

            assert result["id"] == "T"
            assert result["text"] == "final text"
            partials = [m for m in client.received if m.get("type") == "partial"]
            assert partials, "at least one partial must have been delivered"
            assert all(m["id"] == "S" for m in partials)
            assert all(m["type"] == "partial" for m in partials)

    asyncio.run(scenario())


def test_streaming_partials_reach_the_owner_only(tmp_path, streaming_config):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            owner = await Client.connect(sock)
            watcher = await Client.connect(sock)

            await owner.send({"v": 1, "cmd": "start", "id": "s1"})
            await owner.read_until(is_state("recording"))
            await watcher.read_until(is_state("recording"))

            await owner.read_until(is_partial("नमस्ते"))
            kit.transcriber.emit("नमस्ते दुनिया")
            await owner.read_until(is_partial("नमस्ते दुनिया"))

            # A ping barrier proves nothing else is still in flight.
            await watcher.barrier()
            assert not any(m.get("type") == "partial" for m in watcher.received)
            assert not any(m.get("type") == "result" for m in watcher.received)

            await owner.send({"v": 1, "cmd": "stop", "id": "s2"})
            await owner.read_until(is_type("result"))
            await watcher.read_until(is_state("idle"))
            assert not any(m.get("type") == "partial" for m in watcher.received)
            assert not any(m.get("type") == "result" for m in watcher.received)

            await owner.close()
            await watcher.close()

    asyncio.run(scenario())


def test_streaming_partials_are_throttled_to_about_ten_a_second(
    tmp_path, streaming_config
):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["one"]))
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            loop = asyncio.get_running_loop()
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "s1"})
            await client.read_until(is_state("recording"))
            await client.read_until(is_partial("one"))

            started = loop.time()
            kit.transcriber.emit("two")
            second = await client.read_until(is_partial("two"))
            assert second["text"] == "two"
            # At most one message per 100 ms, so the second partial had to wait
            # for the window opened by the first. The floor is written out
            # rather than derived from the constant, so a broken throttle cannot
            # make this assertion vacuous.
            assert loop.time() - started >= 0.09
            await client.close()

    asyncio.run(scenario())


def test_streaming_coalesces_a_burst_and_still_delivers_the_last_partial(
    tmp_path, streaming_config
):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(transcriber=FakeStreamingTranscriber())
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "s1"})
            await client.read_until(is_state("recording"))

            for index in range(30):
                kit.transcriber.emit(f"p{index}")

            await client.read_until(is_partial("p29"))
            texts = partial_texts(client)
            assert texts[-1] == "p29"
            assert len(texts) < 30, "a burst must be coalesced, not queued"

            # A repeat of the text already shown is not resent.
            kit.transcriber.emit("p29")
            await asyncio.sleep(PARTIAL_INTERVAL * 2)
            await client.barrier()
            assert partial_texts(client).count("p29") == 1

            await client.close()

    asyncio.run(scenario())


def test_streaming_flushes_the_newest_partial_before_the_result(
    tmp_path, streaming_config
):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["first"]))
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "s1"})
            await client.read_until(is_state("recording"))
            await client.read_until(is_partial("first"))

            # Emitted inside the throttle window: only the stop path can
            # deliver it, and it must be delivered before the result.
            kit.transcriber.emit("second")
            await client.send({"v": 1, "cmd": "stop", "id": "s2"})

            await client.read_until(is_partial("second"))
            result = await client.read_until(is_type("result"))
            assert result["id"] == "s2"
            assert result["text"] == "नमस्ते दुनिया"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())


def test_streaming_cancel_mid_stream_produces_no_result(tmp_path, streaming_config):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "c1"})
            await client.read_until(is_state("recording"))
            await client.read_until(is_partial("नमस्ते"))

            await client.send({"v": 1, "cmd": "cancel", "id": "c2"})
            assert (await client.read_until(is_state("idle")))["id"] == "c2"
            # By the time idle is on the wire both sides of the live job have
            # been cancelled - checked here, before any later recording could
            # bump the counters.
            assert kit.recorder.cancel_calls == 1
            assert kit.transcriber.cancel_calls == 1
            assert kit.transcriber.finish_calls == 0
            await client.barrier()
            assert not any(m.get("type") == "result" for m in client.received)

            # A fresh streaming recording must still be possible.
            await client.send({"v": 1, "cmd": "start", "id": "c3"})
            assert (await client.read_until(is_state("recording")))["id"] == "c3"
            await client.send({"v": 1, "cmd": "cancel", "id": "c4"})
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    assert kit.recorder.cancel_calls >= 1
    assert kit.transcriber.cancel_calls >= 1
    assert kit.transcriber.finish_calls == 0


def test_streaming_error_at_open_becomes_an_error_message_with_its_code(
    tmp_path, streaming_config
):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(
        transcriber=FakeStreamingTranscriber(
            open_error=FakeStreamingError("no key configured", code="no_api_key")
        )
    )
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "s1"})
            error = await client.read_until(is_error("no_api_key"))
            assert error["id"] == "s1"
            assert error["v"] == 1
            assert "no key configured" in error["message"]

            # Nothing is recording, so the daemon is idle and a new start from
            # another client is accepted rather than reported busy.
            assert server.state == "idle"
            await client.barrier()
            assert not any(m.get("type") == "state" for m in client.received)
            await client.close()

    asyncio.run(scenario())
    assert kit.recorder.cancel_calls == 1
    assert kit.transcriber.cancel_calls == 1


def test_streaming_error_mid_feed_ends_the_recording(tmp_path, streaming_config):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(
        transcriber=FakeStreamingTranscriber(
            feed_error=FakeStreamingError("socket closed", code="network")
        )
    )
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            owner = await Client.connect(sock)
            watcher = await Client.connect(sock)

            await owner.send({"v": 1, "cmd": "start", "id": "s1"})
            await owner.read_until(is_state("recording"))
            await watcher.read_until(is_state("recording"))

            error = await owner.read_until(is_error("network"))
            assert error["id"] == "s1"
            assert "socket closed" in error["message"]
            await watcher.read_until(is_state("idle"))
            await owner.read_until(is_state("idle"))
            assert server.state == "idle"

            # The daemon is idle again, so the watcher can start its own.
            await watcher.send({"v": 1, "cmd": "start", "id": "w1"})
            assert (await watcher.read_until(is_state("recording")))["id"] == "w1"

            await owner.close()
            await watcher.close()

    asyncio.run(scenario())
    assert kit.recorder.cancel_calls >= 1
    assert kit.transcriber.cancel_calls >= 1


def test_streaming_error_at_finish_is_reported_with_the_stop_id(
    tmp_path, streaming_config
):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(
        transcriber=FakeStreamingTranscriber(
            finish_error=FakeStreamingError("upstream said no", code="api")
        )
    )
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "s1"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "s2"})

            error = await client.read_until(is_error("api"))
            assert error["id"] == "s2"
            await client.read_until(is_state("idle"))
            assert server.state == "idle"
            assert not any(m.get("type") == "result" for m in client.received)
            # No API key may ever leave the daemon in a message.
            assert not any(
                streaming_config.api_key in (m.get("message") or "") for m in client.received
            )
            await client.close()

    asyncio.run(scenario())


def test_streaming_keeps_the_busy_and_ownership_rules(tmp_path, streaming_config):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            owner = await Client.connect(sock)
            other = await Client.connect(sock)

            await owner.send({"v": 1, "cmd": "start", "id": "o1"})
            await owner.read_until(is_state("recording"))
            await other.read_until(is_state("recording"))

            await other.send({"v": 1, "cmd": "start", "id": "o2"})
            busy = await other.read_until(is_error("busy"))
            assert busy["id"] == "o2"

            await other.send({"v": 1, "cmd": "stop", "id": "o3"})
            busy = await other.read_until(is_error("busy"))
            assert busy["id"] == "o3"
            assert "another client owns the recording" in busy["message"]

            await other.send({"v": 1, "cmd": "cancel", "id": "o4"})
            busy = await other.read_until(is_error("busy"))
            assert busy["id"] == "o4"

            # The owner is still recording and can still finish.
            await owner.send({"v": 1, "cmd": "stop", "id": "o5"})
            assert (await owner.read_until(is_type("result")))["id"] == "o5"
            await owner.read_until(is_state("idle"))

            await owner.close()
            await other.close()

    asyncio.run(scenario())


def test_streaming_owner_disconnect_mid_recording_cancels_both(tmp_path, streaming_config):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            owner = await Client.connect(sock)
            watcher = await Client.connect(sock)

            await owner.send({"v": 1, "cmd": "start", "id": "d1"})
            await owner.read_until(is_state("recording"))
            await watcher.read_until(is_state("recording"))

            await owner.close()
            await watcher.read_until(is_state("idle"))
            assert not any(m.get("type") == "result" for m in watcher.received)
            assert kit.recorder.cancel_calls == 1
            assert kit.transcriber.cancel_calls == 1
            assert kit.transcriber.finish_calls == 0

            await watcher.send({"v": 1, "cmd": "start", "id": "d2"})
            assert (await watcher.read_until(is_state("recording")))["id"] == "d2"
            await watcher.send({"v": 1, "cmd": "cancel", "id": "d3"})
            await watcher.read_until(is_state("idle"))
            await watcher.close()

    asyncio.run(scenario())
    assert kit.recorder.cancel_calls >= 1
    assert kit.transcriber.cancel_calls >= 1
    assert kit.transcriber.finish_calls == 0


def test_streaming_toggle_starts_and_stops_the_live_path(tmp_path, streaming_config):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "toggle", "id": "t1"})
            assert (await client.read_until(is_state("recording")))["id"] == "t1"
            await client.read_until(is_partial("नमस्ते"))

            await client.send({"v": 1, "cmd": "toggle", "id": "t2"})
            result = await client.read_until(is_type("result"))
            assert result["id"] == "t2"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    assert kit.transcriber.finish_calls == 1


def test_streaming_auto_stop_uses_the_same_timer(tmp_path, make_config):
    sock = tmp_path / "dictation.sock"
    config = make_config(
        streaming_push_to_talk=True, streaming_toggle=True, model="saaras:v4", max_duration_s=1
    )
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server, kit = make_streaming_server(config, sock, kit)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "a1"})
            await client.read_until(is_state("recording"))
            result = await client.read_until(is_type("result"), timeout=GATE_TIMEOUT)
            assert result["id"] == "a1"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    assert kit.recorder.stop_calls == 1


def test_the_file_path_is_untouched_when_streaming_is_off(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit()
    server = DictationServer(
        config,
        recorder_factory=recorder_factory,
        transcriber_factory=transcriber_factory,
        stream_recorder_factory=kit.recorder_factory,
        stream_transcriber_factory=kit.transcriber_factory,
        socket_path=str(sock),
    )

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "f1"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "f2"})
            result = await client.read_until(is_type("result"))
            assert result["id"] == "f2"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    # The streaming factories were never consulted.
    assert kit.recorder_factory.calls == 0
    assert kit.transcriber_factory.calls == 0
    assert kit.recorder.start_calls == 0
    assert kit.transcriber.open_calls == 0
    # Meanwhile the file-based fakes did all the work, as before.
    assert recorder_factory.calls == 1
    assert recorder_factory.recorder.stop_calls == 1
    assert len(transcriber_factory.transcriber.calls) == 1


def test_the_realtime_model_from_a_config_file_selects_the_live_path(tmp_path):
    """End to end: ``model = saaras:v3-realtime`` forces streaming on."""
    sock = tmp_path / "dictation.sock"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[api]\nkey = "fake-key-not-real"\nmodel = "saaras:v3-realtime"\n',
        encoding="utf-8",
    )
    config = load_config(config_file, env={})
    assert config.streaming_push_to_talk is True
    assert config.streaming_toggle is True

    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server, kit = make_streaming_server(config, sock, kit, config_path=config_file)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "r1"})
            await client.read_until(is_state("recording"))
            await client.read_until(is_partial("नमस्ते"))
            await client.send({"v": 1, "cmd": "stop", "id": "r2"})
            assert (await client.read_until(is_type("result")))["id"] == "r2"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    assert kit.transcriber_factory.calls == 1
    assert kit.recorder_factory.calls == 1


def test_streaming_leaves_no_task_behind_after_shutdown(tmp_path, streaming_config):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server, kit = make_streaming_server(streaming_config, sock, kit)

    async def scenario():
        await server.start()
        task = asyncio.create_task(server.serve_forever())
        client = await Client.connect(sock)
        await client.send({"v": 1, "cmd": "start", "id": "z1"})
        await client.read_until(is_state("recording"))
        await client.read_until(is_partial("नमस्ते"))

        await server.shutdown()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await client.close()

        # Only this test's own task should remain.
        pending = [t for t in asyncio.all_tasks() if not t.done()]
        assert pending == [asyncio.current_task()]

    asyncio.run(scenario())
    assert kit.recorder.cancel_calls >= 1
    assert kit.transcriber.cancel_calls >= 1


# --------------------------------------------------------------------------
# choosing the transport per dictation mode (brief 15)
# --------------------------------------------------------------------------


def make_per_mode_server(
    config, socket_path, kit, recorder_factory, transcriber_factory
):
    """A server wired to both the file-based fakes and the streaming kit.

    Which one a job uses is then purely the daemon's decision, so a test can
    assert that exactly one of them was consulted.
    """
    return DictationServer(
        config,
        recorder_factory=recorder_factory,
        transcriber_factory=transcriber_factory,
        stream_recorder_factory=kit.recorder_factory,
        stream_transcriber_factory=kit.transcriber_factory,
        socket_path=str(socket_path),
    )


def test_start_streams_when_only_push_to_talk_is_on(
    tmp_path, make_config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    config = make_config(
        model="saaras:v4", streaming_push_to_talk=True, streaming_toggle=False
    )
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server = make_per_mode_server(config, sock, kit, recorder_factory, transcriber_factory)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "p1"})
            await client.read_until(is_state("recording"))
            await client.read_until(is_partial("नमस्ते"))
            await client.send({"v": 1, "cmd": "stop", "id": "p2"})
            assert (await client.read_until(is_type("result")))["id"] == "p2"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    assert kit.recorder_factory.calls == 1
    assert kit.transcriber_factory.calls == 1
    assert kit.transcriber.finish_calls == 1
    assert recorder_factory.calls == 0
    assert transcriber_factory.calls == 0


def test_toggle_does_not_stream_when_only_push_to_talk_is_on(
    tmp_path, make_config, recorder_factory, transcriber_factory
):
    # The same configuration as the test above: ``start`` streams, ``toggle``
    # must not, because only ``streaming_push_to_talk`` is on.
    sock = tmp_path / "dictation.sock"
    config = make_config(
        model="saaras:v4", streaming_push_to_talk=True, streaming_toggle=False
    )
    kit = StreamKit()
    server = make_per_mode_server(config, sock, kit, recorder_factory, transcriber_factory)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "toggle", "id": "t1"})
            assert (await client.read_until(is_state("recording")))["id"] == "t1"
            await client.send({"v": 1, "cmd": "toggle", "id": "t2"})
            assert (await client.read_until(is_type("result")))["id"] == "t2"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    assert kit.recorder_factory.calls == 0
    assert kit.transcriber_factory.calls == 0
    assert kit.recorder.start_calls == 0
    assert kit.transcriber.open_calls == 0
    assert recorder_factory.calls == 1
    assert recorder_factory.recorder.start_calls == 1
    assert recorder_factory.recorder.stop_calls == 1
    assert len(transcriber_factory.transcriber.calls) == 1


def test_toggle_streams_when_only_toggle_is_on(
    tmp_path, make_config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    config = make_config(
        model="saaras:v4", streaming_push_to_talk=False, streaming_toggle=True
    )
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server = make_per_mode_server(config, sock, kit, recorder_factory, transcriber_factory)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "toggle", "id": "m1"})
            assert (await client.read_until(is_state("recording")))["id"] == "m1"
            await client.read_until(is_partial("नमस्ते"))
            await client.send({"v": 1, "cmd": "toggle", "id": "m2"})
            assert (await client.read_until(is_type("result")))["id"] == "m2"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    assert kit.recorder_factory.calls == 1
    assert kit.transcriber_factory.calls == 1
    assert recorder_factory.calls == 0
    assert transcriber_factory.calls == 0


def test_start_does_not_stream_when_only_toggle_is_on(
    tmp_path, make_config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    config = make_config(
        model="saaras:v4", streaming_push_to_talk=False, streaming_toggle=True
    )
    kit = StreamKit()
    server = make_per_mode_server(config, sock, kit, recorder_factory, transcriber_factory)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "start", "id": "s1"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "s2"})
            assert (await client.read_until(is_type("result")))["id"] == "s2"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    assert kit.recorder_factory.calls == 0
    assert kit.transcriber_factory.calls == 0
    assert recorder_factory.calls == 1
    assert recorder_factory.recorder.start_calls == 1
    assert len(transcriber_factory.transcriber.calls) == 1


def test_toggle_stops_a_streaming_recording_rather_than_starting_a_second(
    tmp_path, make_config
):
    sock = tmp_path / "dictation.sock"
    config = make_config(
        model="saaras:v4", streaming_push_to_talk=False, streaming_toggle=True
    )
    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server, kit = make_streaming_server(config, sock, kit)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "toggle", "id": "x1"})
            assert (await client.read_until(is_state("recording")))["id"] == "x1"
            await client.read_until(is_partial("नमस्ते"))
            await client.send({"v": 1, "cmd": "toggle", "id": "x2"})
            assert (await client.read_until(is_state("transcribing")))["id"] == "x2"
            assert (await client.read_until(is_type("result")))["id"] == "x2"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    # One recording, ended by the second toggle: never a second start.
    assert kit.recorder.start_calls == 1
    assert kit.recorder.stop_calls == 1
    assert kit.transcriber.open_calls == 1
    assert kit.transcriber.finish_calls == 1
    assert kit.transcriber.cancel_calls == 0


def test_toggle_stops_a_batched_recording_rather_than_starting_a_second(
    tmp_path, config, recorder_factory, transcriber_factory
):
    sock = tmp_path / "dictation.sock"
    kit = StreamKit()
    server = make_per_mode_server(config, sock, kit, recorder_factory, transcriber_factory)

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            await client.send({"v": 1, "cmd": "toggle", "id": "b1"})
            assert (await client.read_until(is_state("recording")))["id"] == "b1"
            await client.send({"v": 1, "cmd": "toggle", "id": "b2"})
            assert (await client.read_until(is_state("transcribing")))["id"] == "b2"
            assert (await client.read_until(is_type("result")))["id"] == "b2"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    assert recorder_factory.calls == 1
    assert recorder_factory.recorder.start_calls == 1
    assert recorder_factory.recorder.stop_calls == 1
    assert kit.recorder.start_calls == 0


def test_a_config_file_with_only_streaming_toggle_streams_toggle_only(
    tmp_path, recorder_factory, transcriber_factory
):
    """End to end: the per-mode key from ``config.toml`` drives the dispatch."""
    sock = tmp_path / "dictation.sock"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[api]\nkey = "fake-key-not-real"\nmodel = "saaras:v4"\n'
        "streaming_toggle = true\n",
        encoding="utf-8",
    )
    config = load_config(config_file, env={})
    assert config.streaming_toggle is True
    assert config.streaming_push_to_talk is False

    kit = StreamKit(transcriber=FakeStreamingTranscriber(partials=["नमस्ते"]))
    server = DictationServer(
        config,
        recorder_factory=recorder_factory,
        transcriber_factory=transcriber_factory,
        stream_recorder_factory=kit.recorder_factory,
        stream_transcriber_factory=kit.transcriber_factory,
        socket_path=str(sock),
        config_path=str(config_file),
    )

    async def scenario():
        async with running(server):
            client = await Client.connect(sock)
            # ``start`` is push-to-talk, which this file left on batch.
            await client.send({"v": 1, "cmd": "start", "id": "c1"})
            await client.read_until(is_state("recording"))
            await client.send({"v": 1, "cmd": "stop", "id": "c2"})
            assert (await client.read_until(is_type("result")))["id"] == "c2"
            await client.read_until(is_state("idle"))
            # ``toggle`` is record-and-send, which this file turned on.
            await client.send({"v": 1, "cmd": "toggle", "id": "c3"})
            await client.read_until(is_state("recording"))
            await client.read_until(is_partial("नमस्ते"))
            await client.send({"v": 1, "cmd": "toggle", "id": "c4"})
            assert (await client.read_until(is_type("result")))["id"] == "c4"
            await client.read_until(is_state("idle"))
            await client.close()

    asyncio.run(scenario())
    assert recorder_factory.calls == 1
    assert kit.recorder_factory.calls == 1
    assert kit.recorder.start_calls == 1
    assert kit.transcriber.finish_calls == 1

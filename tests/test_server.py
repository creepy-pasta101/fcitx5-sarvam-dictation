"""Server tests: the full §4 contract driven over a real Unix socket.

No pytest-asyncio: every scenario is an ``async def`` executed with
``asyncio.run`` from an ordinary test function. The server binds a socket
inside ``tmp_path``, real clients connect with ``asyncio.open_unix_connection``,
and the recorder/transcriber are the fakes from ``conftest`` injected through
``recorder_factory``/``transcriber_factory``.

Ordering is never assumed where the brief does not fix it: helpers read until a
predicate matches, and "nothing else arrived" is proved with a ping barrier
rather than a sleep.

The last section covers brief 2, section 6: the ``reload`` command, its
``reloaded``/``config_invalid`` replies, and the guarantee that a reload never
touches work that is already recording or transcribing.
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
from sarvam_dictation.server import DictationServer

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

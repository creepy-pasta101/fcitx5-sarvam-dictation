"""Server tests for the ``[api] engine`` switch (brief section 3).

``DictationServer`` has two real transcriber factories - one for the file path
and one for the live path - and both of them now choose their route from the
job's engine: ``DictateTranscriber``/``DictateRealtimeTranscriber`` under
``engine = "dictate"``, ``SarvamTranscriber``/``RealtimeTranscriber`` under
``engine = "saaras"``. This module covers exactly that decision and nothing
else; the protocol, the state machine, the partial delivery and the
discard/cancel rules are covered by ``tests/test_server.py``, which this change
must leave untouched and green.

Three things are worth testing beyond "which class comes back":

* the *injection* contract. ``transcriber_factory`` and
  ``stream_transcriber_factory`` are how every other test - and any embedder -
  replaces the real route, so an injected factory must win over the engine
  switch, not be silently replaced by it.
* the *snapshot*. A ``reload`` may switch engines between two dictations, but a
  job in flight must keep the engine it started with, and ``_job_config()`` is
  where that promise lives. The tests set ``_active_config`` and ``_config`` to
  opposite engines on purpose.
* the *import*. ``sarvam_dictation.dictate`` and
  ``sarvam_dictation.dictate_streaming`` are imported inside the factories, so
  the daemon still imports on a machine with neither transport package; that is
  asserted in a subprocess, because a test session that has already built a
  dictate route would have the module in ``sys.modules`` for reasons that have
  nothing to do with import-time behaviour.

No pytest plugin is used: every scenario is a sync test function and the async
ones are driven with ``asyncio.run``. No audio hardware, no network, no
``sarvamai``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import sarvam_dictation.dictate as dictate_module
import sarvam_dictation.dictate_streaming as dictate_streaming_module
import sarvam_dictation.server as server_module
import sarvam_dictation.streaming as streaming_module
from sarvam_dictation.config import ENGINE_DICTATE, ENGINE_SAARAS
from sarvam_dictation.dictate import DictateTranscriber
from sarvam_dictation.dictate_streaming import DictateRealtimeTranscriber
from sarvam_dictation.server import DictationServer
from sarvam_dictation.streaming import RealtimeTranscriber
from sarvam_dictation.transcriber import SarvamTranscriber, Transcript

READ_TIMEOUT = 2.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def build_server(config, **kwargs):
    """A server with every real factory in place unless a test injects one."""
    return DictationServer(config, socket_path=kwargs.pop("socket_path", ""), **kwargs)


def recording_route(instances):
    """A route class that records the settings it was constructed with.

    Used in place of ``DictateTranscriber``/``DictateRealtimeTranscriber``/
    ``RealtimeTranscriber`` so a test can see the config and the keyword
    arguments the factory actually passed, without depending on any private
    attribute of a module this unit does not own.
    """

    class _Route:
        def __init__(self, config, **kwargs):
            self.config = config
            self.kwargs = kwargs
            instances.append(self)

    return _Route


class FakeFileRecorder:
    """A ``Recorder`` stand-in that never touches audio hardware."""

    def __init__(self, wav_path, duration_ms=1500):
        self.wav_path = Path(wav_path)
        self.duration_ms = duration_ms
        self.start_calls = 0
        self.stop_calls = 0
        self.cancel_calls = 0

    @property
    def is_recording(self):
        return self.stop_calls == 0

    def start(self):
        self.start_calls += 1

    def stop(self):
        from sarvam_dictation.recorder import Recording

        self.stop_calls += 1
        return Recording(path=self.wav_path, duration_ms=self.duration_ms)

    def cancel(self):
        self.cancel_calls += 1


class FakeDictateTranscriber:
    """A ``DictateTranscriber`` stand-in returning canned dictate text."""

    def __init__(self, config, client=None, text="नमस्ते दुनिया", language_code="hi-IN"):
        self.config = config
        self.client = client
        self.text = text
        self.language_code = language_code
        self.calls = []

    def transcribe(self, path):
        self.calls.append(path)
        return Transcript(text=self.text, language_code=self.language_code, segments=1)


class AddonClient:
    """A minimal addon-side client: newline-delimited JSON over the socket."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.received = []

    @classmethod
    async def connect(cls, path):
        reader, writer = await asyncio.open_unix_connection(str(path))
        return cls(reader, writer)

    async def send(self, message):
        self.writer.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
        await self.writer.drain()

    async def read_until(self, predicate, timeout=READ_TIMEOUT):
        """Read messages until one matches ``predicate``; return it."""
        while True:
            line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
            if not line:
                raise AssertionError(f"the daemon closed the connection; saw {self.received}")
            message = json.loads(line.decode("utf-8"))
            self.received.append(message)
            if predicate(message):
                return message

    async def close(self):
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()


def is_type(kind):
    return lambda message: message.get("type") == kind


def is_state(state):
    return lambda message: message.get("type") == "state" and message.get("state") == state


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


# --------------------------------------------------------------------------
# the file-based factory
# --------------------------------------------------------------------------


def test_the_file_based_factory_builds_the_dictate_transcriber_under_the_dictate_engine(
    make_config,
):
    server = build_server(make_config(engine=ENGINE_DICTATE))

    transcriber = server._new_transcriber()

    assert isinstance(transcriber, DictateTranscriber)


def test_the_file_based_factory_builds_the_saaras_transcriber_under_the_saaras_engine(
    make_config,
):
    server = build_server(make_config(engine=ENGINE_SAARAS))

    transcriber = server._new_transcriber()

    assert isinstance(transcriber, SarvamTranscriber)
    assert not isinstance(transcriber, DictateTranscriber)


def test_the_file_based_factory_gives_the_transcriber_the_job_settings(
    make_config, monkeypatch
):
    # The engine decides *which* route; the settings handed to it are the job
    # snapshot, unchanged from what the saaras-only code passed.
    instances = []
    monkeypatch.setattr(dictate_module, "DictateTranscriber", recording_route(instances))
    job_config = make_config(engine=ENGINE_DICTATE, dictate_style="clean")
    server = build_server(job_config)

    server._new_transcriber()

    assert len(instances) == 1
    assert instances[0].config is job_config
    assert instances[0].config.dictate_style == "clean"


# --------------------------------------------------------------------------
# the live factory
# --------------------------------------------------------------------------


def test_the_live_factory_builds_the_dictate_transcriber_under_the_dictate_engine(
    make_config,
):
    server = build_server(make_config(engine=ENGINE_DICTATE))

    transcriber = server._new_stream_transcriber(lambda text: None)

    assert isinstance(transcriber, DictateRealtimeTranscriber)


def test_the_live_factory_builds_the_realtime_transcriber_under_the_saaras_engine(
    make_config,
):
    server = build_server(make_config(engine=ENGINE_SAARAS))

    transcriber = server._new_stream_transcriber(lambda text: None)

    assert isinstance(transcriber, RealtimeTranscriber)
    assert not isinstance(transcriber, DictateRealtimeTranscriber)


def test_the_live_factory_hands_the_partial_callback_to_the_dictate_transcriber(
    make_config, monkeypatch
):
    instances = []
    monkeypatch.setattr(
        dictate_streaming_module,
        "DictateRealtimeTranscriber",
        recording_route(instances),
    )
    server = build_server(make_config(engine=ENGINE_DICTATE))
    on_partial = lambda text: None  # noqa: E731 - identity is what matters

    server._new_stream_transcriber(on_partial)

    assert len(instances) == 1
    assert instances[0].kwargs["on_partial"] is on_partial


def test_the_live_factory_hands_the_partial_callback_to_the_realtime_transcriber(
    make_config, monkeypatch
):
    instances = []
    monkeypatch.setattr(streaming_module, "RealtimeTranscriber", recording_route(instances))
    server = build_server(make_config(engine=ENGINE_SAARAS))
    on_partial = lambda text: None  # noqa: E731

    server._new_stream_transcriber(on_partial)

    assert len(instances) == 1
    assert instances[0].kwargs["on_partial"] is on_partial


# --------------------------------------------------------------------------
# the injection contract
# --------------------------------------------------------------------------


def test_an_injected_transcriber_factory_still_wins_over_the_dictate_engine(
    make_config, transcriber_factory, fake_transcriber
):
    # An injected factory is the whole point of the constructor argument; the
    # engine switch must not reach around it.
    server = build_server(
        make_config(engine=ENGINE_DICTATE), transcriber_factory=transcriber_factory
    )

    built = server._transcriber_factory()

    assert built is fake_transcriber
    assert transcriber_factory.calls == 1


def test_an_injected_stream_transcriber_factory_still_wins_over_the_dictate_engine(
    make_config,
):
    seen = []

    def factory(on_partial):
        seen.append(on_partial)
        return "the injected live transcriber"

    server = build_server(
        make_config(engine=ENGINE_DICTATE), stream_transcriber_factory=factory
    )
    on_partial = lambda text: None  # noqa: E731

    built = server._stream_transcriber_factory(on_partial)

    assert built == "the injected live transcriber"
    assert seen == [on_partial]


# --------------------------------------------------------------------------
# the engine is read from the job snapshot
# --------------------------------------------------------------------------


def test_the_file_factory_follows_a_dictate_snapshot_over_a_saaras_config(make_config):
    # The state a reload can produce mid-job: the live config says saaras, but
    # the job in flight started under dictate and must finish under dictate.
    server = build_server(make_config(engine=ENGINE_SAARAS))
    server._active_config = make_config(engine=ENGINE_DICTATE)

    assert isinstance(server._new_transcriber(), DictateTranscriber)


def test_the_file_factory_follows_a_saaras_snapshot_over_a_dictate_config(make_config):
    # And the reverse: a reload to dictate must not hijack work that began on
    # saaras.
    server = build_server(make_config(engine=ENGINE_DICTATE))
    server._active_config = make_config(engine=ENGINE_SAARAS)

    assert isinstance(server._new_transcriber(), SarvamTranscriber)


def test_the_live_factory_follows_a_dictate_snapshot_over_a_saaras_config(make_config):
    server = build_server(make_config(engine=ENGINE_SAARAS))
    server._active_config = make_config(engine=ENGINE_DICTATE)

    transcriber = server._new_stream_transcriber(lambda text: None)

    assert isinstance(transcriber, DictateRealtimeTranscriber)


def test_the_live_factory_follows_a_saaras_snapshot_over_a_dictate_config(make_config):
    server = build_server(make_config(engine=ENGINE_DICTATE))
    server._active_config = make_config(engine=ENGINE_SAARAS)

    transcriber = server._new_stream_transcriber(lambda text: None)

    assert isinstance(transcriber, RealtimeTranscriber)


# --------------------------------------------------------------------------
# the file-based path end to end
# --------------------------------------------------------------------------


def test_a_dictate_round_trip_still_delivers_the_result_to_the_addon(
    tmp_path, make_config, write_wav, monkeypatch
):
    # The engine switch must not disturb the protocol: the addon still gets
    # ``recording``, ``transcribing``, one ``result`` carrying the text, and
    # ``idle``. The transcriber here is built by the server's own default
    # factory, so the dictate route really is the one chosen.
    sock = tmp_path / "dictation.sock"
    wav = write_wav(tmp_path / "clip.wav", duration_s=1.0)

    recorder = FakeFileRecorder(wav, duration_ms=1500)
    dictate_transcriber = FakeDictateTranscriber(config=None, text="नमस्ते दुनिया")

    def build_dictate(config, client=None):
        dictate_transcriber.config = config
        return dictate_transcriber

    saaras_built = []
    monkeypatch.setattr(dictate_module, "DictateTranscriber", build_dictate)
    monkeypatch.setattr(
        server_module, "SarvamTranscriber", recording_route(saaras_built)
    )

    server = build_server(
        make_config(engine=ENGINE_DICTATE),
        recorder_factory=lambda: recorder,
        socket_path=str(sock),
    )

    async def scenario():
        async with running(server):
            addon = await AddonClient.connect(sock)
            await addon.send({"v": 1, "cmd": "start", "id": "start-1"})
            assert (await addon.read_until(is_state("recording")))["id"] == "start-1"

            await addon.send({"v": 1, "cmd": "stop", "id": "stop-1"})
            assert (await addon.read_until(is_state("transcribing")))["id"] == "stop-1"

            result = await addon.read_until(is_type("result"))
            assert result["id"] == "stop-1"
            assert result["text"] == "नमस्ते दुनिया"
            assert result["language_code"] == "hi-IN"
            assert result["duration_ms"] == 1500

            idle = await addon.read_until(is_state("idle"))
            assert idle["id"] == "stop-1"
            await addon.close()

    asyncio.run(scenario())

    assert recorder.start_calls == 1
    assert recorder.stop_calls == 1
    assert dictate_transcriber.calls == [Path(wav)]
    assert saaras_built == [], "the saaras route must not be built under the dictate engine"


# --------------------------------------------------------------------------
# the import stays lazy
# --------------------------------------------------------------------------


def test_importing_the_server_does_not_import_the_dictate_routes():
    # The hard rule of brief section 2: importing the daemon must not require
    # any optional transport package. A subprocess is used because this test
    # session may already have built a dictate route for another test, which
    # would put the module in ``sys.modules`` for unrelated reasons.
    daemon_dir = Path(server_module.__file__).resolve().parent.parent
    script = textwrap.dedent(
        """
        import sys

        import sarvam_dictation.server  # noqa: F401

        watched = (
            "sarvam_dictation.dictate",
            "sarvam_dictation.dictate_streaming",
            "sarvamai",
            "websockets",
            "httpx",
        )
        print(",".join(name for name in watched if name in sys.modules))
        """
    )
    environment = dict(os.environ, PYTHONPATH=str(daemon_dir))

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "", (
        f"importing the server must not import {completed.stdout.strip()}"
    )

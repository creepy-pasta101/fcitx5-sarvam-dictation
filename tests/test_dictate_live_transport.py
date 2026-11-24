"""Transport-level tests for the Dictate live socket.

The unit tests for :mod:`sarvam_dictation.dictate_streaming` inject a hand-
written fake socket, which proves the bookkeeping but never exercises the real
transport: the URL the query parameters are built into, the auth header, the
JSON framing, the close handshake and the read-until-``session.end`` loop. This
module drives the real client against a real loopback WebSocket that speaks the
documented protocol, so a message the API would refuse fails here.

It needs ``websockets``, which the test-suite does not otherwise require, so the
whole module skips when the package is absent. ``websockets`` is an optional
runtime dependency of the Dictate engine only.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

websockets = pytest.importorskip("websockets")

from websockets.asyncio.server import serve  # noqa: E402

from sarvam_dictation import dictate_streaming  # noqa: E402


class FakeSocket:
    """A one-session stand-in for ``wss://api.sarvam.ai/v2/dictate/ws``.

    ``turns`` are the finished texts to send, one per turn. The flags cover the
    cases the contract warns about: out-of-order finals, a turn that failed, an
    ``output.final`` nobody should type, and a socket that dies before
    ``session.end``.
    """

    def __init__(self, turns=("hello world",), *, out_of_order=False,
                 empty_first_turn=False, send_output=False,
                 die_without_session_end=False):
        self.turns = list(turns)
        self.out_of_order = out_of_order
        self.empty_first_turn = empty_first_turn
        self.send_output = send_output
        self.die_without_session_end = die_without_session_end
        self.seen: dict = {}
        self._server = None

    async def __aenter__(self):
        self.seen = {"messages": [], "audio_sizes": [], "path": None, "query": {},
                     "headers": {}, "close_code": None}
        self._server = await serve(self._session, "127.0.0.1", 0)
        self.seen["port"] = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info):
        self._server.close()
        await self._server.wait_closed()

    def url(self) -> str:
        return f"ws://127.0.0.1:{self.seen['port']}/v2/dictate/ws"

    async def _session(self, ws):
        from urllib.parse import parse_qsl

        raw = ws.request.path
        self.seen["path"] = raw.split("?")[0]
        self.seen["query"] = dict(parse_qsl(raw.split("?")[1] if "?" in raw else ""))
        self.seen["headers"] = {k.lower(): v for k, v in ws.request.headers.items()}

        await ws.send(json.dumps({
            "event": "session.begin", "request_id": "req", "config": {"model": "dictate-v1"}
        }))
        idx = 0
        finished = False
        while True:
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            except Exception:
                return
            self.seen["messages"].append(message)
            event = message.get("event")
            if event == "config.update":
                await ws.send(json.dumps({
                    "event": "config.updated",
                    "applied": sorted(k for k in message if k != "event"),
                    "config": {"model": "dictate-v1"},
                }))
            elif event == "audio_input":
                self.seen["audio_sizes"].append(len(base64.b64decode(message["audio"])))
                await ws.send(json.dumps({
                    "event": "transcript.partial", "utterance_idx": idx,
                    "text": "partial words",
                }))
            elif event == "ping":
                await ws.send(json.dumps({"event": "pong"}))
            elif event == "speech_end":
                await self._finish(ws)
                finished = True
            elif event == "end":
                if not finished:
                    await self._finish(ws)
                if self.send_output:
                    await ws.send(json.dumps({
                        "event": "output.final", "text": "a summary nobody types",
                        "degraded": None, "usage": {},
                    }))
                if self.die_without_session_end:
                    self.seen["close_code"] = 1011
                    await ws.close(1011, "recognition stopped")
                    return
                await ws.send(json.dumps({
                    "event": "session.end", "request_id": "req",
                    "total_duration_s": "1.0", "total_utterances": len(self.turns),
                    "audio_received_s": "1.0",
                    "usage": {"audio_duration_s": 1.0, "words": 2},
                }))
                self.seen["close_code"] = 1000
                await ws.close(1000, "normal")
                return

    async def _finish(self, ws):
        finals = []
        for i, text in enumerate(self.turns):
            if i == 0 and self.empty_first_turn:
                text = ""
            finals.append({
                "event": "dictation.final", "utterance_idx": i, "model": "dictate-v1",
                "text": text, "corrections": [],
                "degraded": "asr_error" if not text else None,
                "language_code": "en-IN", "usage": {"audio_duration_s": 1.0},
            })
        transcripts = [
            {"event": "transcript.final", "utterance_idx": i, "text": t}
            for i, t in enumerate(self.turns)
        ]
        if self.out_of_order:
            await ws.send(json.dumps(transcripts[-1]))
            for final in reversed(finals):
                await ws.send(json.dumps(final))
            await ws.send(json.dumps(transcripts[0]))
        else:
            for transcript, final in zip(transcripts, finals):
                await ws.send(json.dumps(transcript))
                await ws.send(json.dumps(final))


def _point_at(monkeypatch, url: str) -> None:
    """Point the module at the fake socket instead of the real service."""
    names = [name for name, value in vars(dictate_streaming).items()
             if isinstance(value, str) and value.startswith(("wss://", "ws://"))]
    assert names, "the module exposes no module-level socket URL to redirect"
    for name in names:
        monkeypatch.setattr(dictate_streaming, name, url)


def _is_ours(task: asyncio.Task) -> bool:
    """True when ``task`` runs code from the live-socket module."""
    coro = task.get_coro()
    code = getattr(coro, "cr_code", None) or getattr(coro, "__code__", None)
    return code is not None and "dictate_streaming.py" in code.co_filename


def _pcm(seconds: float, sample_rate: int = 16000) -> bytes:
    """Silence as raw 16-bit mono PCM, the shape the recorder feeds."""
    return b"\x00\x00" * int(seconds * sample_rate)


async def _transcribe(config, audio: bytes, partials: list | None = None):
    transcriber = dictate_streaming.DictateRealtimeTranscriber(
        config, on_partial=(partials.append if partials is not None else None)
    )
    await transcriber.open()
    for start in range(0, len(audio), 8000):
        await transcriber.feed(audio[start:start + 8000])
    return await transcriber.finish()


def test_the_session_connects_with_the_documented_settings(make_config, monkeypatch):
    async def scenario():
        async with FakeSocket() as server:
            _point_at(monkeypatch, server.url())
            transcript = await _transcribe(make_config(engine="dictate"), _pcm(0.5))
            return server.seen, transcript

    seen, transcript = asyncio.run(scenario())

    assert seen["path"] == "/v2/dictate/ws"
    assert seen["query"] == {
        "language_code": "auto",   # empty config means detect, and "auto" is legal here
        "mode": "transcribe",
        "encoding": "linear16",
        "sample_rate": "16000",    # a string, as the socket requires
        "stream_type": "balanced",
        "endpointing": "manual",   # one key press = one turn
    }
    assert "model" not in seen["query"]  # unset: omitted, never blank
    assert seen["headers"]["api-subscription-key"] == make_config(engine="dictate").api_key
    assert "api-subscription-key" not in seen["query"]
    assert transcript.text == "hello world"
    assert transcript.language_code == "en-IN"
    assert seen["close_code"] == 1000


def test_one_config_update_carries_every_setting_before_speech_start(make_config, monkeypatch):
    async def scenario():
        async with FakeSocket() as server:
            _point_at(monkeypatch, server.url())
            config = make_config(
                engine="dictate",
                dictate_style="clean",
                dictate_rules="Write rupees as ₹.",
                dictate_vocabulary=("Velqora",),
                dictate_context="a support call",
                dictate_output_rules="Return JSON.",
            )
            await _transcribe(config, _pcm(0.3))
            return server.seen["messages"]

    messages = asyncio.run(scenario())
    events = [message["event"] for message in messages]

    update = next(m for m in messages if m["event"] == "config.update")
    assert update["style"] == {"preset": "clean", "rules": "Write rupees as ₹."}
    assert update["vocabulary"] == [{"word": "Velqora"}]
    assert update["context"] == {"text": "a support call"}
    assert update["output_rules"] == "Return JSON."
    # Order matters: the update is applied before any audio or turn control.
    assert events.index("config.update") < events.index("speech_start")
    assert events.index("speech_start") < events.index("audio_input")
    assert events[-1] == "end"
    assert events.index("speech_end") < events.index("end")


def test_audio_is_split_into_messages_the_socket_accepts(make_config, monkeypatch):
    """One feed call may carry more than the 1 s the socket allows per message."""

    async def scenario():
        async with FakeSocket() as server:
            _point_at(monkeypatch, server.url())
            await _transcribe(make_config(engine="dictate"), _pcm(3.0))
            return server.seen["audio_sizes"]

    sizes = asyncio.run(scenario())
    assert sum(sizes) == 3.0 * 16000 * 2
    assert max(sizes) <= 16000 * 2, "a message carried more than one second of audio"


def test_turns_are_joined_in_index_order_even_when_they_arrive_reversed(
    make_config, monkeypatch
):
    """The contract warns a later turn's transcript can precede an earlier
    turn's finished text; appending in arrival order would scramble the field."""

    async def scenario():
        async with FakeSocket(("first turn", "second turn"), out_of_order=True) as server:
            _point_at(monkeypatch, server.url())
            return await _transcribe(make_config(engine="dictate"), _pcm(0.3))

    transcript = asyncio.run(scenario())
    assert transcript.text == "first turn second turn"
    assert transcript.segments == 2


def test_a_failed_turn_contributes_nothing_but_is_still_counted(make_config, monkeypatch):
    async def scenario():
        async with FakeSocket(("", "the real words"), empty_first_turn=True) as server:
            _point_at(monkeypatch, server.url())
            return await _transcribe(make_config(engine="dictate"), _pcm(0.3))

    transcript = asyncio.run(scenario())
    assert transcript.text == "the real words"
    assert transcript.segments == 2


def test_output_final_is_read_but_never_typed(make_config, monkeypatch):
    async def scenario():
        async with FakeSocket(("spoken words",), send_output=True) as server:
            _point_at(monkeypatch, server.url())
            return await _transcribe(
                make_config(engine="dictate", dictate_output_rules="Summarise."), _pcm(0.3)
            )

    transcript = asyncio.run(scenario())
    assert transcript.text == "spoken words"
    assert "summary" not in transcript.text


def test_partials_reach_the_callback_and_are_never_empty(make_config, monkeypatch):
    partials: list[str] = []

    async def scenario():
        async with FakeSocket() as server:
            _point_at(monkeypatch, server.url())
            await _transcribe(make_config(engine="dictate"), _pcm(0.3), partials)

    asyncio.run(scenario())
    assert partials, "no partial ever reached the callback"
    assert all(text.strip() for text in partials)


def test_a_socket_that_dies_before_session_end_still_returns_what_it_got(
    make_config, monkeypatch
):
    """A fatal close after the turns arrived is a partial success: the user's
    words are worth more than an error about the missing end of the session."""

    async def scenario():
        async with FakeSocket(("dictated words",), die_without_session_end=True) as server:
            _point_at(monkeypatch, server.url())
            return await _transcribe(make_config(engine="dictate"), _pcm(0.3))

    transcript = asyncio.run(scenario())
    assert transcript.text == "dictated words"


def test_cancel_leaves_no_task_behind(make_config, monkeypatch):
    """The receiver and the keepalive must be joined, not merely cancelled.

    The fake server's own handler task legitimately outlives the connection, so
    this looks only for tasks running this module's code.
    """

    async def scenario():
        async with FakeSocket() as server:
            _point_at(monkeypatch, server.url())
            transcriber = dictate_streaming.DictateRealtimeTranscriber(
                make_config(engine="dictate")
            )
            await transcriber.open()
            await transcriber.feed(_pcm(0.2))
            await transcriber.cancel()
            await asyncio.sleep(0)
            return [
                task for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and _is_ours(task)
            ]

    leftovers = asyncio.run(scenario())
    assert leftovers == [], f"cancel left {leftovers!r} running"

"""Async Unix-socket server implementing the frozen protocol of brief section 4.

One recording at a time; the connection that starts it owns it; state changes are
broadcast to every client while results and errors go only to the owner.
Transcription runs in a worker thread so the socket stays responsive to ``ping``
and ``cancel``.

Brief 2 adds one client command, ``reload`` (section 6): re-read ``config.toml``
from disk and apply it to *future* work. The settings of a job that is already
recording or transcribing are snapshotted when it starts, so a reload can never
change the audio settings or the API call of work in flight.

Brief 7 adds a second recording path, chosen per job by ``config.streaming``.
When it is on, audio is fed to Sarvam's realtime WebSocket API as it is
captured and interim ``partial`` messages reach the owner while the user is
still speaking; the final transcript still arrives as ``result``. The two
paths share one state machine: the same owner, the same ``busy`` replies, the
same cancel semantics, and the same ``result``/``error`` delivery. Only the
work behind ``start``, ``stop`` and ``cancel`` differs.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import inspect
import logging
import os
import socket
from pathlib import Path

from . import protocol
from .config import Config, ConfigError, default_config_path, default_socket_path, reload_config
from .protocol import (
    MAX_LINE,
    STATE_IDLE,
    STATE_RECORDING,
    STATE_TRANSCRIBING,
    LineReader,
    ProtocolError,
    encode,
    error_msg,
    partial_msg,
    result_msg,
    state_msg,
)
from .recorder import Recorder, RecorderError
from .transcriber import SarvamTranscriber, TranscriptionError

logger = logging.getLogger(__name__)

READ_CHUNK = 64 * 1024
DRAIN_TIMEOUT = 5.0
PROBE_TIMEOUT = 1.0
SHUTDOWN_TIMEOUT = 2.0

#: How often a streaming recording may push a ``partial`` to its owner: at most
#: one message per interval (brief 7 asks for about ten a second). Newer
#: partials coalesce rather than queue, so this is a rate limit and not a
#: backlog.
PARTIAL_INTERVAL = 0.1

#: The brief 2 client command (section 6). ``protocol.py`` is frozen, so the
#: daemon accepts it through its own tuple rather than editing the protocol.
RELOAD_COMMAND = "reload"

#: Every command this server accepts: the frozen protocol set plus ``reload``.
COMMANDS = tuple(dict.fromkeys(protocol.COMMANDS + (RELOAD_COMMAND,)))

#: The brief 2 error code (section 6). It is not part of the frozen
#: ``protocol.ERROR_CODES`` tuple, so the builder for it lives here.
CONFIG_INVALID_CODE = "config_invalid"


def reloaded_msg(msg_id: str) -> dict:
    """Build the ``reloaded`` reply (brief section 6).

    ``{"v":1,"type":"reloaded","id":"<uuid>"}`` and nothing else.
    """
    return {"v": protocol.VERSION, "type": "reloaded", "id": msg_id}


def config_invalid_msg(message: str, msg_id: str | None) -> dict:
    """Build the ``config_invalid`` error reply (brief section 6).

    Same key set and order as :func:`sarvam_dictation.protocol.error_msg`, with
    the new ``config_invalid`` code; the message names the offending key.
    """
    return {
        "v": protocol.VERSION,
        "type": "error",
        "id": msg_id,
        "code": CONFIG_INVALID_CODE,
        "message": message,
    }


class _Client:
    """Per-connection state: the socket, its framer, and a closed flag."""

    __slots__ = ("reader", "writer", "lines", "closed")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.lines = LineReader()
        self.closed = False


class _StreamJob:
    """Everything one live (streaming) recording owns.

    Held in :attr:`DictationServer._stream_job` while the job is in flight so
    that stop, cancel, owner-gone and shutdown can all tear the same object
    down, whatever it has reached. The partial state lives here rather than on
    the server so a new recording can never inherit a previous one's throttle
    window or last-sent text.
    """

    __slots__ = (
        "owner",
        "msg_id",
        "loop",
        "recorder",
        "transcriber",
        "drain_task",
        "partial_task",
        "event",
        "latest_partial",
        "sent_partial",
        "last_sent_at",
        "failure",
    )

    def __init__(self, owner: "_Client", msg_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self.owner = owner
        self.msg_id = msg_id
        self.loop = loop
        self.recorder = None
        self.transcriber = None
        self.drain_task: asyncio.Task | None = None
        self.partial_task: asyncio.Task | None = None
        self.event = asyncio.Event()
        self.latest_partial = ""
        self.sent_partial = ""
        self.last_sent_at: float | None = None
        #: ``(code, message)`` of the first failure seen, if any.
        self.failure: tuple[str, str] | None = None


class _StreamedRecording:
    """The duration carrier the shared ``result`` path reads.

    A streaming recording has no WAV file, so the ``Recording`` the file-based
    path builds is not available; ``_finish`` only ever reads ``duration_ms``
    and this is the smallest thing that satisfies it.
    """

    __slots__ = ("duration_ms",)

    def __init__(self, duration_ms: int) -> None:
        self.duration_ms = int(duration_ms)


class DictationServer:
    """The daemon's protocol server.

    The recorder and transcriber factories are injectable so tests can supply
    fakes without touching audio hardware or the network. The streaming path
    has its own pair, ``stream_recorder_factory`` and
    ``stream_transcriber_factory``, so a test can drive the live path with fakes
    too; the transcriber factory is called with the ``on_partial`` callback the
    server wants its partials delivered through.

    ``config_path`` is the file ``reload`` re-reads. When it is not given the
    server uses the path its :class:`~sarvam_dictation.config.Config` was
    loaded from (so ``--config`` is honoured) and otherwise the built-in XDG
    location, which is the same file the daemon loads at startup.
    """

    def __init__(
        self,
        config: Config,
        *,
        recorder_factory=None,
        transcriber_factory=None,
        stream_recorder_factory=None,
        stream_transcriber_factory=None,
        socket_path: str | None = None,
        config_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._config = config
        self._config_path = self._resolve_config_path(config, config_path)
        self._recorder_factory = recorder_factory or (lambda: Recorder(self._job_config()))
        self._transcriber_factory = transcriber_factory or (
            lambda: SarvamTranscriber(self._job_config())
        )
        self._stream_recorder_factory = stream_recorder_factory or self._new_stream_recorder
        self._stream_transcriber_factory = (
            stream_transcriber_factory or self._new_stream_transcriber
        )
        self._socket_path = socket_path or config.socket_path or default_socket_path()

        self._server: asyncio.AbstractServer | None = None
        self._clients: set[_Client] = set()
        self._state = STATE_IDLE
        self._owner: _Client | None = None
        self._owner_id: str | None = None
        self._recorder = None
        #: The live job while a streaming recording runs (brief 7); ``None``
        #: on the file-based path and while idle.
        self._stream_job: _StreamJob | None = None
        self._transcribe_task: asyncio.Task | None = None
        self._auto_stop_task: asyncio.Task | None = None
        self._discard_result = False
        self._cancel_id: str | None = None
        self._lock = asyncio.Lock()
        # Settings of the job currently recording/transcribing, captured when it
        # started. ``None`` while idle. A reload replaces ``_config`` only, so
        # work in flight always finishes with the settings it began with.
        self._active_config: Config | None = None
        # Every API key this process has seen, so a reload cannot leave the old
        # key unscrubbed in a message that is still on its way out.
        self._secrets: set[str] = {config.api_key} if config.api_key else set()

    @property
    def socket_path(self) -> str:
        """The path this server binds (or was asked to bind)."""
        return self._socket_path

    @property
    def state(self) -> str:
        """Current protocol state, for diagnostics and tests."""
        return self._state

    @property
    def config(self) -> Config:
        """The settings applied to the next recording (updated by ``reload``)."""
        return self._config

    @property
    def config_path(self) -> Path:
        """The file ``reload`` re-reads."""
        return self._config_path

    @staticmethod
    def _resolve_config_path(
        config: Config, config_path: str | os.PathLike[str] | None
    ) -> Path:
        """Pick the file to re-read: explicit argument, else the Config's own."""
        if config_path is not None:
            return Path(config_path)
        loaded_from = getattr(config, "config_path", "")
        if loaded_from:
            return Path(loaded_from)
        return default_config_path()

    async def start(self) -> None:
        """Bind the socket, removing a stale file only when nobody is listening."""
        if self._server is not None:
            return
        path = self._socket_path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.lexists(path):
            if await self._is_socket_live(path):
                raise RuntimeError(
                    f"another sarvam-dictation daemon is already listening on {path}"
                )
            logger.info("removing stale socket %s", path)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)

        self._server = await asyncio.start_unix_server(self._handle_client, path=path)
        try:
            os.chmod(path, 0o600)
        except OSError as exc:  # pragma: no cover - odd filesystems only
            logger.warning("cannot chmod %s to 0600: %s", path, exc)
        logger.info("listening on %s", path)

    async def serve_forever(self) -> None:
        """Serve until :meth:`shutdown` (or cancellation) closes the server."""
        server = self._server
        if server is None:
            raise RuntimeError("start() must be awaited before serve_forever()")
        async with server:
            await server.serve_forever()

    async def shutdown(self) -> None:
        """Close the listener, drop clients, stop capture and unlink the socket."""
        server = self._server
        self._server = None

        await self._cancel_task(self._auto_stop_task)
        self._auto_stop_task = None

        recorder = self._recorder
        self._recorder = None
        if recorder is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(recorder.cancel)

        # A live streaming recording holds no file recorder, so tear its own
        # tasks and socket down as well.
        await self._abort_stream_job()

        # Close the listener first, then drop every client: wait_closed() waits for
        # the connection handlers, so it must not be awaited before they can end.
        if server is not None:
            server.close()
        for client in list(self._clients):
            self._close_writer(client)
        self._clients.clear()
        if server is not None:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=SHUTDOWN_TIMEOUT)

        task = self._transcribe_task
        self._transcribe_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        self._state = STATE_IDLE
        self._owner = None
        self._owner_id = None
        self._active_config = None

        with contextlib.suppress(FileNotFoundError, NotADirectoryError, PermissionError):
            os.unlink(self._socket_path)
        logger.info("stopped; socket %s removed", self._socket_path)

    # ----------------------------------------------------------------- clients

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Read framed commands from one connection until EOF or protocol abuse."""
        client = _Client(reader, writer)
        self._clients.add(client)
        logger.debug("client connected (%d total)", len(self._clients))
        try:
            while True:
                data = await reader.read(READ_CHUNK)
                if not data:
                    break
                try:
                    messages = client.lines.feed(data)
                except ProtocolError as exc:
                    if client.lines.buffered_bytes > MAX_LINE:
                        logger.warning("client exceeded the %d byte line limit", MAX_LINE)
                        break
                    await self._send(client, error_msg("bad_request", str(exc), None))
                    continue
                for message in messages:
                    await self._dispatch(client, message)
                    if client.closed:
                        break
                if client.closed:
                    break
        except (ConnectionResetError, BrokenPipeError):
            logger.debug("client connection reset")
        except asyncio.CancelledError:
            raise
        finally:
            await self._drop_client(client)

    async def _drop_client(self, client: _Client) -> None:
        """Forget a connection, releasing ownership and cancelling if it owned work."""
        self._clients.discard(client)
        if self._owner is client:
            await self._owner_gone()
        self._close_writer(client)
        with contextlib.suppress(Exception):
            await client.writer.wait_closed()
        logger.debug("client disconnected (%d left)", len(self._clients))

    async def _owner_gone(self) -> None:
        """Handle the owner vanishing: discard the recording, or suppress its result."""
        async with self._lock:
            if self._state == STATE_RECORDING:
                logger.info("owner disconnected while recording; discarding")
                recorder = self._recorder
                self._recorder = None
                self._owner = None
                self._owner_id = None
                self._state = STATE_IDLE
                self._active_config = None
                await self._stop_auto_stop()
                if recorder is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(recorder.cancel)
                await self._abort_stream_job()
                await self._broadcast_state(STATE_IDLE, None)
            elif self._state == STATE_TRANSCRIBING:
                logger.info("owner disconnected while transcribing; result will be dropped")
                self._discard_result = True
                self._cancel_id = None

    def _close_writer(self, client: _Client) -> None:
        """Close a client's transport exactly once."""
        if client.closed:
            return
        client.closed = True
        with contextlib.suppress(Exception):
            client.writer.close()

    async def _send(self, client: _Client, message: dict) -> bool:
        """Write one message; returns False when the client is gone or too slow."""
        if client.closed or client.writer.is_closing():
            return False
        try:
            client.writer.write(encode(message))
            await asyncio.wait_for(client.writer.drain(), timeout=DRAIN_TIMEOUT)
            return True
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.debug("write to client failed: %s", exc)
            self._close_writer(client)
            return False
        except asyncio.TimeoutError:
            logger.warning("client is not draining its socket; dropping it")
            self._close_writer(client)
            return False

    async def _broadcast_state(self, state: str, msg_id: str | None) -> None:
        """Send a ``state`` message to every connected client."""
        message = state_msg(state, msg_id)
        for client in list(self._clients):
            await self._send(client, message)

    # ---------------------------------------------------------------- commands

    async def _dispatch(self, client: _Client, message: dict) -> None:
        """Validate and route one decoded command message."""
        version = message.get("v", protocol.VERSION)
        if version != protocol.VERSION:
            await self._send(
                client,
                error_msg("bad_request", f"unsupported protocol version {version!r}", None),
            )
            return
        command = message.get("cmd")
        if not isinstance(command, str) or command not in COMMANDS:
            await self._send(client, error_msg("bad_request", f"unknown command {command!r}", None))
            return
        if command == "ping":
            await self._send(client, protocol.pong_msg())
            return

        msg_id = message.get("id")
        if not isinstance(msg_id, str) or not msg_id:
            await self._send(
                client, error_msg("bad_request", f"'{command}' requires a string 'id'", None)
            )
            return

        if command == "start":
            await self._cmd_start(client, msg_id)
        elif command == "stop":
            await self._cmd_stop(client, msg_id)
        elif command == "toggle":
            if self._state == STATE_IDLE:
                await self._cmd_start(client, msg_id)
            else:
                await self._cmd_stop(client, msg_id)
        elif command == RELOAD_COMMAND:
            await self._cmd_reload(client, msg_id)
        else:  # cancel
            await self._cmd_cancel(client, msg_id)

    async def _cmd_start(self, client: _Client, msg_id: str) -> None:
        """Begin recording, making ``client`` the owner of the new recording."""
        async with self._lock:
            if self._state != STATE_IDLE:
                await self._send(client, error_msg("busy", f"daemon is {self._state}", msg_id))
                return
            if self._config.streaming:
                # The live path owns its own bookkeeping (snapshot, owner,
                # state and broadcast) and the same auto-stop timer.
                if await self._start_streaming(client, msg_id):
                    self._schedule_auto_stop(client, msg_id)
                return
            # Snapshot the settings this recording (and its transcription) will
            # use, before a reload could replace them.
            self._active_config = self._config
            recorder = self._recorder_factory()
            try:
                await asyncio.to_thread(recorder.start)
            except RecorderError as exc:
                logger.warning("cannot start recording: %s", exc)
                self._active_config = None
                await self._send(client, error_msg(_code_of(exc, "audio_failed"), str(exc), msg_id))
                return
            except Exception as exc:
                logger.exception("unexpected failure starting the recorder")
                self._active_config = None
                await self._send(
                    client, error_msg("internal", f"internal error: {exc.__class__.__name__}", msg_id)
                )
                return

            self._recorder = recorder
            self._owner = client
            self._owner_id = msg_id
            self._state = STATE_RECORDING
            await self._broadcast_state(STATE_RECORDING, msg_id)

        self._schedule_auto_stop(client, msg_id)

    async def _cmd_stop(self, client: _Client, msg_id: str) -> None:
        """Stop recording and start transcription, owner-only."""
        async with self._lock:
            if self._state == STATE_IDLE:
                await self._send(client, error_msg("busy", "not recording", msg_id))
                return
            if self._owner is not client:
                await self._send(
                    client, error_msg("busy", "another client owns the recording", msg_id)
                )
                return
            if self._state != STATE_RECORDING:
                await self._send(client, error_msg("busy", f"daemon is {self._state}", msg_id))
                return

            recorder = self._recorder
            self._recorder = None
            stream_job = self._stream_job
            self._stream_job = None
            self._state = STATE_TRANSCRIBING
            self._owner_id = msg_id
            self._discard_result = False
            self._cancel_id = None
            await self._stop_auto_stop()
            await self._broadcast_state(STATE_TRANSCRIBING, msg_id)
            if stream_job is not None:
                stream_job.owner = client
                self._transcribe_task = asyncio.create_task(
                    self._run_streaming_transcription(client, stream_job, msg_id)
                )
            else:
                self._transcribe_task = asyncio.create_task(
                    self._run_transcription(client, recorder, msg_id)
                )

    async def _cmd_cancel(self, client: _Client, msg_id: str) -> None:
        """Discard the current recording; legal in every state, owner-only when busy."""
        async with self._lock:
            if self._state == STATE_IDLE:
                await self._send(client, state_msg(STATE_IDLE, msg_id))
                return
            if self._owner is not client:
                await self._send(
                    client, error_msg("busy", "another client owns the recording", msg_id)
                )
                return
            if self._state == STATE_TRANSCRIBING:
                # The worker thread cannot be aborted; drop its result instead.
                self._discard_result = True
                self._cancel_id = msg_id
                return

            recorder = self._recorder
            self._recorder = None
            self._owner = None
            self._owner_id = None
            self._state = STATE_IDLE
            self._active_config = None
            await self._stop_auto_stop()
            if recorder is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(recorder.cancel)
            await self._abort_stream_job()
            await self._broadcast_state(STATE_IDLE, msg_id)

    async def _cmd_reload(self, client: _Client, msg_id: str) -> None:
        """Re-read the config file and apply it to future work (brief section 6).

        Legal in every state: the file is read off the event loop, the new
        settings are swapped in under the lock, and nothing else is touched -
        no state change, no broadcast, no effect on the current recording or
        transcription (those hold their own snapshot).
        """
        path = self._config_path
        try:
            new_config = await asyncio.to_thread(reload_config, path)
        except ConfigError as exc:
            message = self._scrub(str(exc))
            logger.warning("reload rejected: %s", message)
            await self._send(client, config_invalid_msg(message, msg_id))
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("unexpected failure while reloading the configuration")
            await self._send(
                client, error_msg("internal", f"internal error: {exc.__class__.__name__}", msg_id)
            )
            return

        async with self._lock:
            previous = self._config
            self._config = new_config
            if new_config.api_key:
                self._secrets.add(new_config.api_key)
            if new_config.socket_path != previous.socket_path:
                logger.info(
                    "socket_path is now %r; the listener stays on %s until a restart",
                    new_config.socket_path,
                    self._socket_path,
                )
        logger.info("configuration reloaded from %s", path)
        await self._send(client, reloaded_msg(msg_id))

    # --------------------------------------------------------------- streaming

    def _new_stream_recorder(self):
        """Build the real streaming recorder, imported lazily (brief 7)."""
        from .recorder import StreamingRecorder

        return StreamingRecorder(self._job_config())

    def _new_stream_transcriber(self, on_partial):
        """Build the real realtime transcriber, imported lazily (brief 7)."""
        from .streaming import RealtimeTranscriber

        return RealtimeTranscriber(self._job_config(), on_partial=on_partial)

    async def _start_streaming(self, client: _Client, msg_id: str) -> bool:
        """Open a live recording for ``client``; called with the lock held.

        Returns True once the daemon is recording, False when the job could not
        be started - in which case the owner has been sent the ``error`` and
        nothing is left running.
        """
        # Snapshot the settings this recording will use, exactly as the
        # file-based path does, so a reload cannot change a job in flight.
        self._active_config = self._config
        job = _StreamJob(client, msg_id, asyncio.get_running_loop())
        try:
            job.recorder = self._stream_recorder_factory()
            job.transcriber = self._stream_transcriber_factory(
                lambda text: self._deliver_partial(job, text)
            )
            await self._maybe_await(job.recorder.start())
            await self._maybe_await(job.transcriber.open())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cannot start streaming transcription: %s", exc)
            self._active_config = None
            await self._teardown_job(job)
            await self._send(
                client, error_msg(_code_of(exc, "internal"), self._scrub(str(exc)), msg_id)
            )
            return False

        self._stream_job = job
        self._owner = client
        self._owner_id = msg_id
        self._state = STATE_RECORDING
        await self._broadcast_state(STATE_RECORDING, msg_id)
        # Start the two tasks only once every client has seen the transition,
        # so a partial or a failure can never overtake it on the wire.
        job.drain_task = asyncio.create_task(self._drain_stream_chunks(job))
        job.partial_task = asyncio.create_task(self._consume_partials(job))
        return True

    @staticmethod
    def _deliver_partial(job: _StreamJob, text: str) -> None:
        """Accept one partial from the transcriber's receiver task.

        This runs where the transcriber calls it, so it only records the latest
        text and wakes the delivery task; the throttling and the socket write
        happen there. ``call_soon_threadsafe`` keeps that safe even if a
        transcriber calls back from a worker thread.
        """
        text = (text or "").strip()
        if not text:
            return
        job.latest_partial = text
        with contextlib.suppress(RuntimeError):
            job.loop.call_soon_threadsafe(job.event.set)

    async def _drain_stream_chunks(self, job: _StreamJob) -> None:
        """Push captured PCM into the transcriber until capture ends."""
        try:
            async for chunk in job.recorder.chunks():
                if not chunk:
                    continue
                await self._maybe_await(job.transcriber.feed(chunk))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = _code_of(exc, "internal")
            logger.warning("streaming capture failed (%s): %s", code, exc)
            if job.failure is None:
                job.failure = (code, self._scrub(str(exc)))
            await self._stream_failed(job)

    async def _consume_partials(self, job: _StreamJob) -> None:
        """Send ``partial`` to the owner, at most one per :data:`PARTIAL_INTERVAL`.

        The interval is a rate limit, not a queue: after waiting it out the task
        re-reads the newest text, so intermediate partials are dropped instead
        of delivered late. A partial identical to the one already sent is not
        resent, and the newest unseen partial is always delivered - immediately
        on the stop path, via :meth:`_flush_partial`.
        """
        loop = asyncio.get_running_loop()
        while True:
            await job.event.wait()
            job.event.clear()
            if job.last_sent_at is not None:
                delay = job.last_sent_at + PARTIAL_INTERVAL - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
            text = job.latest_partial
            if not text or text == job.sent_partial:
                continue
            await self._send_partial(job, text)

    async def _send_partial(self, job: _StreamJob, text: str) -> bool:
        """Write one ``partial`` to the owner only and remember it."""
        job.sent_partial = text
        job.last_sent_at = asyncio.get_running_loop().time()
        if job.owner is None:
            return False
        return await self._send(job.owner, partial_msg(job.msg_id, text))

    async def _flush_partial(self, job: _StreamJob) -> None:
        """Deliver the newest unseen partial at once, ignoring the interval."""
        text = job.latest_partial
        if not text or text == job.sent_partial:
            return
        await self._send_partial(job, text)

    async def _run_streaming_transcription(
        self, owner: _Client, job: _StreamJob, msg_id: str
    ) -> None:
        """Stop capture, finish the session, then report through ``_finish``."""
        duration_ms = 0
        transcript = None
        failure = job.failure
        try:
            try:
                duration_ms = int(await self._maybe_await(job.recorder.stop()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if failure is None:
                    failure = (_code_of(exc, "audio_failed"), self._scrub(str(exc)))
            # Capture has ended, so the drain task ends too; let it hand over
            # every remaining byte before the session is finished.
            await self._finish_drain(job)
            # The tail is in now, so the duration is only final at this point:
            # stop() no longer drains the pipe, and the reader that did counts
            # its bytes into the same recorder.
            duration_ms = int(getattr(job.recorder, "duration_ms", duration_ms))
            # The drain task records its own failure; read it again now that it
            # has ended, so a failure raised while capture was stopping is not
            # lost.
            if failure is None:
                failure = job.failure
            await self._cancel_task(job.partial_task)
            job.partial_task = None
            if failure is None:
                await self._flush_partial(job)
                try:
                    transcript = await self._maybe_await(job.transcriber.finish())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failure = (_code_of(exc, "internal"), self._scrub(str(exc)))
            if failure is not None:
                with contextlib.suppress(Exception):
                    await self._maybe_await(job.transcriber.cancel())
        finally:
            self._stream_job = None
            if job.recorder is not None:
                with contextlib.suppress(Exception):
                    await self._maybe_await(job.recorder.cancel())
            if failure is not None:
                logger.warning("streaming transcription failed (%s): %s", failure[0], failure[1])
            await self._finish(
                owner, msg_id, _StreamedRecording(duration_ms), transcript, failure
            )

    async def _finish_drain(self, job: _StreamJob) -> None:
        """Wait for the drain task to finish, cancelling it if it will not."""
        task = job.drain_task
        job.drain_task = None
        if task is None or task is asyncio.current_task():
            return
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(task, timeout=DRAIN_TIMEOUT)

    async def _stream_failed(self, job: _StreamJob) -> None:
        """End a live recording that failed mid-stream (brief 7).

        The state machine is the same one the file path uses: the owner is
        told, the daemon returns to idle and every client sees the transition.
        """
        async with self._lock:
            if self._stream_job is not job or self._state != STATE_RECORDING:
                # Stop or cancel already took the job over; its own path will
                # deliver the failure.
                return
            self._stream_job = None
            owner = job.owner
            msg_id = job.msg_id
            self._owner = None
            self._owner_id = None
            self._state = STATE_IDLE
            self._active_config = None
            await self._stop_auto_stop()
        await self._teardown_job(job)
        code, message = job.failure or ("internal", "streaming transcription failed")
        if owner is not None:
            await self._send(owner, error_msg(code, self._scrub(message), msg_id))
        await self._broadcast_state(STATE_IDLE, None)

    async def _teardown_job(self, job: _StreamJob) -> None:
        """Cancel a job's tasks and close both sides; never touches state."""
        current = asyncio.current_task()
        for name in ("partial_task", "drain_task"):
            task = getattr(job, name)
            setattr(job, name, None)
            if task is not None and task is not current:
                await self._cancel_task(task)
        if job.recorder is not None:
            with contextlib.suppress(Exception):
                await self._maybe_await(job.recorder.cancel())
        if job.transcriber is not None:
            with contextlib.suppress(Exception):
                await self._maybe_await(job.transcriber.cancel())

    async def _abort_stream_job(self) -> None:
        """Tear down the live job if there is one: cancel, owner-gone, shutdown."""
        job = self._stream_job
        self._stream_job = None
        if job is None:
            return
        await self._teardown_job(job)

    @staticmethod
    async def _maybe_await(value):
        """Return ``value``, awaiting it first when it is awaitable.

        The streaming recorder and transcriber are asynchronous by contract;
        this keeps the server working with a synchronous stand-in too, which is
        what a test fake is most likely to be.
        """
        if inspect.isawaitable(value):
            return await value
        return value

    # ------------------------------------------------------------ transcription

    async def _run_transcription(self, owner: _Client, recorder, msg_id: str) -> None:
        """Stop the recorder, transcribe off-loop, then report to the owner."""
        recording = None
        transcript = None
        failure: tuple[str, str] | None = None
        try:
            try:
                recording = await asyncio.to_thread(recorder.stop)
            except RecorderError as exc:
                failure = (_code_of(exc, "audio_failed"), str(exc))
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(recorder.cancel)
                return
            transcriber = self._transcriber_factory()
            transcript = await asyncio.to_thread(transcriber.transcribe, recording.path)
        except TranscriptionError as exc:
            failure = (_code_of(exc, "internal"), str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("unexpected failure during transcription")
            failure = ("internal", f"internal error: {exc.__class__.__name__}")
        finally:
            if recording is not None:
                with contextlib.suppress(OSError):
                    Path(recording.path).unlink(missing_ok=True)
            if failure is not None:
                logger.warning("transcription failed (%s): %s", failure[0], failure[1])
            await self._finish(owner, msg_id, recording, transcript, failure)

    async def _finish(
        self,
        owner: _Client,
        msg_id: str,
        recording,
        transcript,
        failure: tuple[str, str] | None,
    ) -> None:
        """Deliver result/error to the owner (unless discarded) and return to idle."""
        async with self._lock:
            discard = self._discard_result
            cancel_id = self._cancel_id
            self._discard_result = False
            self._cancel_id = None
            self._state = STATE_IDLE
            self._owner = None
            self._owner_id = None
            self._transcribe_task = None
            self._active_config = None

        if discard:
            await self._broadcast_state(STATE_IDLE, cancel_id)
            return

        if failure is not None:
            code, message = failure
            await self._send(owner, error_msg(code, self._scrub(message), msg_id))
        elif transcript is not None and recording is not None:
            await self._send(
                owner,
                result_msg(
                    msg_id,
                    (transcript.text or "").strip(),
                    transcript.language_code,
                    recording.duration_ms,
                    transcript.segments,
                ),
            )
        else:  # pragma: no cover - defensive
            await self._send(owner, error_msg("internal", "transcription produced no result", msg_id))
        await self._broadcast_state(STATE_IDLE, msg_id)

    def _schedule_auto_stop(self, client: _Client, msg_id: str) -> None:
        """Arm the ``max_duration_s`` auto-stop for the current recording."""
        limit = self._job_config().max_duration_s
        if limit <= 0:  # pragma: no cover - validation forbids this
            return
        self._auto_stop_task = asyncio.create_task(self._auto_stop(client, msg_id, float(limit)))

    async def _auto_stop(self, client: _Client, msg_id: str, delay: float) -> None:
        """Sleep for ``delay`` then behave exactly like an owner ``stop``."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        self._auto_stop_task = None
        if self._state != STATE_RECORDING or self._owner is not client:
            return
        logger.info("max_duration_s reached; stopping automatically")
        await self._cmd_stop(client, msg_id)

    async def _stop_auto_stop(self) -> None:
        """Cancel the auto-stop timer unless it is the task currently running."""
        task = self._auto_stop_task
        self._auto_stop_task = None
        if task is None or task is asyncio.current_task():
            return
        await self._cancel_task(task)

    @staticmethod
    async def _cancel_task(task: asyncio.Task | None) -> None:
        """Cancel and await a task, swallowing its cancellation."""
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # ------------------------------------------------------------------ helpers

    def _job_config(self) -> Config:
        """Settings for the job in flight, else the ones for the next job.

        Called when a recorder or transcriber is constructed, so an in-flight
        recording and its transcription both see the settings it started with
        even if a ``reload`` arrived in between.
        """
        active = self._active_config
        return active if active is not None else self._config

    def _scrub(self, text: str) -> str:
        """Strip every API key this daemon has seen from a message leaving it."""
        for key in self._secrets:
            if key and len(key) >= 4:
                text = text.replace(key, "***")
        return text

    async def _is_socket_live(self, path: str) -> bool:
        """True when something is accepting connections on ``path``."""

        def _probe() -> bool:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(PROBE_TIMEOUT)
            try:
                probe.connect(path)
            except OSError as exc:
                if exc.errno in (errno.ECONNREFUSED, errno.ENOENT, errno.ENOTSOCK):
                    return False
                logger.warning("cannot probe existing socket %s: %s", path, exc)
                return False
            finally:
                probe.close()
            return True

        return await asyncio.to_thread(_probe)


def _code_of(exc: BaseException, default: str) -> str:
    """Return the exception's ``code`` when it is a legal protocol error code."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in protocol.ERROR_CODES:
        return code
    return default

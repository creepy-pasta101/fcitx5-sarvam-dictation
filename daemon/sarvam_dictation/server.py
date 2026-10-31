"""Async Unix-socket server implementing the frozen protocol of brief section 4.

One recording at a time; the connection that starts it owns it; state changes are
broadcast to every client while results and errors go only to the owner.
Transcription runs in a worker thread so the socket stays responsive to ``ping``
and ``cancel``.

Brief 2 adds one client command, ``reload`` (section 6): re-read ``config.toml``
from disk and apply it to *future* work. The settings of a job that is already
recording or transcribing are snapshotted when it starts, so a reload can never
change the audio settings or the API call of work in flight.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
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


class DictationServer:
    """The daemon's protocol server.

    The recorder and transcriber factories are injectable so tests can supply
    fakes without touching audio hardware or the network.

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
        socket_path: str | None = None,
        config_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._config = config
        self._config_path = self._resolve_config_path(config, config_path)
        self._recorder_factory = recorder_factory or (lambda: Recorder(self._job_config()))
        self._transcriber_factory = transcriber_factory or (
            lambda: SarvamTranscriber(self._job_config())
        )
        self._socket_path = socket_path or config.socket_path or default_socket_path()

        self._server: asyncio.AbstractServer | None = None
        self._clients: set[_Client] = set()
        self._state = STATE_IDLE
        self._owner: _Client | None = None
        self._owner_id: str | None = None
        self._recorder = None
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
            self._state = STATE_TRANSCRIBING
            self._owner_id = msg_id
            self._discard_result = False
            self._cancel_id = None
            await self._stop_auto_stop()
            await self._broadcast_state(STATE_TRANSCRIBING, msg_id)
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

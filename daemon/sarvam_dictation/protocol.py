"""Wire protocol for the fcitx5-sarvam-dictation daemon.

Frozen contract (brief section 4): AF_UNIX / SOCK_STREAM, one JSON object per
line, UTF-8, ``\\n`` terminated, every message carries ``"v": 1``.

This module is deliberately free of I/O and of asyncio so that both the daemon
and the test-suite can exercise framing in isolation.
"""

from __future__ import annotations

import json

VERSION = 1
MAX_LINE = 1024 * 1024

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"

STATES = (STATE_IDLE, STATE_RECORDING, STATE_TRANSCRIBING)

COMMANDS = ("start", "stop", "toggle", "cancel", "ping")

#: Every error code the daemon may emit (brief section 4).
ERROR_CODES = (
    "busy",
    "bad_request",
    "no_api_key",
    "no_audio_device",
    "audio_failed",
    "network",
    "api",
    "too_long",
    "internal",
)


class ProtocolError(Exception):
    """Raised when a line cannot be decoded into a protocol message.

    The server turns this into an ``error`` message with code ``bad_request``
    (or, when the line exceeded :data:`MAX_LINE`, into a disconnect).
    """


def encode(message: dict) -> bytes:
    """Serialise ``message`` as one newline-terminated UTF-8 JSON line.

    Compact separators keep the framing byte-exact and small; ``ensure_ascii``
    is off so Devanagari transcripts travel as real UTF-8 rather than escapes.
    """
    if not isinstance(message, dict):
        raise ProtocolError(f"message must be a dict, got {type(message).__name__}")
    text = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def decode_line(line: bytes) -> dict:
    """Parse one line (with or without its trailing newline) into a dict.

    Raises :class:`ProtocolError` for invalid UTF-8, invalid JSON, or JSON that
    is not an object.
    """
    if isinstance(line, bytearray):
        raw = bytes(line)
    elif isinstance(line, bytes):
        raw = line
    else:
        raise ProtocolError(f"line must be bytes, got {type(line).__name__}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"line is not valid UTF-8: {exc}") from exc
    text = text.strip()
    if not text:
        raise ProtocolError("empty line")
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError(f"message must be a JSON object, got {type(message).__name__}")
    return message


def state_msg(state: str, msg_id: str | None) -> dict:
    """Build a ``state`` message (broadcast to every client on a transition)."""
    if state not in STATES:
        raise ProtocolError(f"unknown state {state!r}")
    return {"v": VERSION, "type": "state", "state": state, "id": msg_id}


def error_msg(code: str, message: str, msg_id: str | None) -> dict:
    """Build an ``error`` message addressed to the owner of the command."""
    if code not in ERROR_CODES:
        raise ProtocolError(f"unknown error code {code!r}")
    return {"v": VERSION, "type": "error", "id": msg_id, "code": code, "message": message}


def result_msg(
    msg_id: str,
    text: str,
    language_code: str | None,
    duration_ms: int,
    segments: int,
) -> dict:
    """Build a ``result`` message carrying the final transcript."""
    return {
        "v": VERSION,
        "type": "result",
        "id": msg_id,
        "text": text,
        "language_code": language_code,
        "duration_ms": int(duration_ms),
        "segments": int(segments),
    }


def pong_msg() -> dict:
    """Build the reply to ``ping``."""
    return {"v": VERSION, "type": "pong"}


class LineReader:
    """Incremental newline framer for one connection.

    ``feed`` may be called with arbitrary fragments; complete lines are decoded
    and returned in order. A trailing partial line stays buffered. The buffer is
    never allowed to grow without bound: exceeding ``limit`` raises
    :class:`ProtocolError` and leaves the offending bytes in place so the caller
    can see (via :attr:`buffered_bytes`) that it must drop the connection.
    """

    def __init__(self, limit: int = MAX_LINE) -> None:
        self._limit = limit
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        """Number of bytes currently held for an incomplete line."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[dict]:
        """Consume ``data`` and return every message it completed.

        Blank lines (a bare newline) are ignored: they carry no message and
        disconnecting a client over a keep-alive newline would be hostile.
        """
        if not data:
            return []
        self._buffer.extend(data)
        messages: list[dict] = []
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                break
            line = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            if not line.strip():
                continue
            messages.append(decode_line(line))
        if len(self._buffer) > self._limit:
            raise ProtocolError(f"line exceeds the {self._limit} byte limit")
        return messages

"""The wire vocabulary must cover every code the daemon can actually raise.

This module exists because it did not, and the failure was invisible: the
transcribers raised ``config_invalid`` for a setting that cannot work on the
route about to run, the addon had a notification written for that code, and
``server._code_of`` silently downgraded it to ``internal`` because the code was
missing from ``protocol.ERROR_CODES``. The user was told "the dictation daemon
hit an internal error" when the real answer was "this model only works on the
realtime endpoint".

Three guards, each catching a different way of getting that wrong again:

* the vocabulary covers every literal code in the daemon's source;
* a code the daemon raises survives the trip through the server to the client;
* the addon's own list of codes it has a sentence for is a subset of the
  vocabulary, so a code cannot be added here that the addon would render as the
  generic "Dictation failed."
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import wave

import pytest

from sarvam_dictation import protocol
from sarvam_dictation.config import Config
from sarvam_dictation.server import DictationServer
from sarvam_dictation.transcriber import TranscriptionError

DAEMON_DIR = pathlib.Path(__file__).resolve().parent.parent / "daemon" / "sarvam_dictation"

#: The codes ``src/sarvamdictation.cpp`` has a sentence for, in
#: ``friendlyErrorText``. Kept here as a literal because the C++ cannot be
#: introspected from a Python test; it is the addon's half of the contract.
ADDON_CODES = (
    "busy",
    "bad_request",
    "no_api_key",
    "no_audio_device",
    "audio_failed",
    "network",
    "api",
    "too_long",
    "internal",
    "config_invalid",
)


def _codes_raised_by_the_daemon() -> dict[str, set[str]]:
    """Every literal error code in the daemon's source, and where it appears."""
    found: dict[str, set[str]] = {}
    for path in DAEMON_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        # ``(?<![A-Za-z_])`` keeps ``status_code="..."`` and friends out: they
        # end in "code" and would otherwise look like an error code.
        patterns = (
            r'(?<![A-Za-z_])code\s*=\s*"([a-z_]+)"',
            r'error_msg\(\s*"([a-z_]+)"',
            r'_code_of\([^,]+,\s*"([a-z_]+)"',
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                found.setdefault(match.group(1), set()).add(path.name)
    return found


def test_every_code_the_daemon_raises_is_a_legal_wire_code():
    raised = _codes_raised_by_the_daemon()
    assert raised, "the scan found no error codes at all, so it is broken"
    missing = {
        code: sorted(where)
        for code, where in raised.items()
        if code not in protocol.ERROR_CODES
    }
    assert not missing, (
        "these codes are raised but cannot be sent, so the user sees a "
        f"downgraded error instead: {missing}"
    )


def test_the_addon_has_a_sentence_for_every_wire_code():
    """A code the addon does not know renders as "Dictation failed." """
    unknown = [code for code in protocol.ERROR_CODES if code not in ADDON_CODES]
    assert not unknown, f"the addon has no message for {unknown}"


def test_a_config_mistake_reaches_the_client_as_config_invalid(tmp_path):
    """The regression this module was written for, end to end."""

    class Recorder:
        def __init__(self, path):
            self.path = path
            self.duration_ms = 1000

        def start(self):
            return None

        def stop(self):
            with wave.open(str(self.path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x00\x00" * 16000)
            return self

        def cancel(self):
            return None

    class RefusingTranscriber:
        def transcribe(self, path):
            raise TranscriptionError(
                "model 'saaras:v3-realtime' only works on the realtime endpoint",
                code="config_invalid",
            )

    async def scenario():
        server = DictationServer(
            Config(api_key="test-key-not-real"),
            recorder_factory=lambda: Recorder(tmp_path / "clip.wav"),
            transcriber_factory=lambda: RefusingTranscriber(),
        )
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(server.socket_path)
            for command in ("toggle", "toggle"):
                writer.write(protocol.encode({"cmd": command, "id": "1"}))
                await writer.drain()
                if command == "toggle":
                    # Let the first toggle reach the recording state.
                    await asyncio.wait_for(reader.readline(), 5)
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                assert remaining > 0, "the daemon never reported the failure"
                line = await asyncio.wait_for(reader.readline(), remaining)
                assert line, "the daemon closed the socket without an error"
                message = json.loads(line)
                if message.get("type") == "error":
                    writer.close()
                    return message
        finally:
            await server.shutdown()

    message = asyncio.run(scenario())
    assert message["code"] == "config_invalid", message
    assert "realtime endpoint" in message["message"]


def test_error_msg_accepts_config_invalid():
    message = protocol.error_msg("config_invalid", "[api] mode 'translate' ...", "abc")
    assert message["code"] == "config_invalid"
    assert message["type"] == "error"


def test_an_unknown_code_is_still_refused():
    """The vocabulary is a guard, not a rubber stamp."""
    with pytest.raises(protocol.ProtocolError):
        protocol.error_msg("no_such_code", "x", "abc")

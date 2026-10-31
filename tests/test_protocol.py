"""Wire-protocol tests: brief §4, exercised through ``sarvam_dictation.protocol``.

The protocol is the frozen contract between the C++ addon and this daemon, so
these tests are deliberately pedantic: exact key sets, compact encoding, UTF-8
survival, and every documented rejection path of ``decode_line``/``LineReader``.
"""

from __future__ import annotations

import json

import pytest

from sarvam_dictation.protocol import (
    COMMANDS as PROTOCOL_COMMANDS,
    MAX_LINE,
    STATE_IDLE,
    STATE_RECORDING,
    STATE_TRANSCRIBING,
    VERSION,
    LineReader,
    ProtocolError,
    decode_line,
    encode,
    error_msg,
    result_msg,
    state_msg,
)
from sarvam_dictation.server import (
    COMMANDS as SERVER_COMMANDS,
    RELOAD_COMMAND,
    config_invalid_msg,
    reloaded_msg,
)


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------


def test_version_is_one():
    assert VERSION == 1


def test_max_line_is_one_mebibyte():
    assert MAX_LINE == 1024 * 1024


def test_state_constants_match_wire_strings():
    assert (STATE_IDLE, STATE_RECORDING, STATE_TRANSCRIBING) == (
        "idle",
        "recording",
        "transcribing",
    )


def test_protocol_error_is_an_exception():
    assert issubclass(ProtocolError, Exception)


# --------------------------------------------------------------------------
# encode
# --------------------------------------------------------------------------


def test_encode_is_newline_terminated_utf8():
    data = encode({"v": 1, "cmd": "ping"})
    assert isinstance(data, bytes)
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1
    assert data.decode("utf-8").strip() == '{"v":1,"cmd":"ping"}'


def test_encode_uses_compact_separators():
    data = encode({"v": 1, "type": "state", "state": "idle", "id": None})
    assert b'": ' not in data
    assert b", " not in data


def test_encode_keeps_non_ascii_readable_or_escaped_but_decodable():
    data = encode({"v": 1, "type": "result", "text": "नमस्ते"})
    assert json.loads(data.decode("utf-8"))["text"] == "नमस्ते"


# --------------------------------------------------------------------------
# decode_line
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        {"v": 1, "cmd": "start", "id": "abc"},
        {"v": 1, "type": "pong"},
        {"v": 1, "type": "result", "id": "x", "text": "नमस्ते दुनिया", "language_code": "hi-IN"},
        {"v": 1, "type": "result", "id": "x", "text": "मैं ठीक हूँ", "language_code": "hi-IN"},
        {"v": 1, "type": "error", "id": None, "code": "busy", "message": "already recording"},
    ],
)
def test_encode_decode_round_trip(message):
    assert decode_line(encode(message)) == message


def test_decode_line_preserves_json_escapes():
    original = {"v": 1, "text": 'quote " backslash \\ tab \t newline \n end', "id": "a"}
    assert decode_line(encode(original)) == original


def test_decode_line_preserves_control_and_unicode_edge_cases():
    original = {"v": 1, "text": "ज़िन्दगी ١٢٣ 🎤 \u0001"}
    assert decode_line(encode(original)) == original


def test_decode_line_accepts_a_trailing_newline():
    assert decode_line(b'{"v":1,"type":"pong"}\n') == {"v": 1, "type": "pong"}


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"\n",
        b"not json at all",
        b"{unquoted: 1}",
        b'{"v": 1',  # truncated object
        b'{"v":1}{"v":1}',  # two objects on one "line"
    ],
)
def test_decode_line_rejects_bad_json(line):
    with pytest.raises(ProtocolError):
        decode_line(line)


@pytest.mark.parametrize(
    "line",
    [
        b"[]",
        b"[1, 2, 3]",
        b'[{"v": 1}]',
        b'"a string"',
        b"42",
        b"true",
        b"null",
    ],
)
def test_decode_line_rejects_non_object_json(line):
    with pytest.raises(ProtocolError):
        decode_line(line)


def test_decode_line_rejects_invalid_utf8():
    with pytest.raises(ProtocolError):
        decode_line(b"\xff\xfe\x00garbage")


# --------------------------------------------------------------------------
# LineReader
# --------------------------------------------------------------------------


def test_linereader_yields_one_message_from_one_chunk():
    reader = LineReader()
    messages = reader.feed(encode({"v": 1, "cmd": "ping"}))
    assert messages == [{"v": 1, "cmd": "ping"}]
    assert reader.buffered_bytes == 0


def test_linereader_reassembles_a_line_split_across_chunks():
    reader = LineReader()
    data = encode({"v": 1, "cmd": "start", "id": "0123456789abcdef"})
    first, second = data[:10], data[10:]

    assert reader.feed(first) == []
    assert reader.buffered_bytes == len(first)
    assert reader.feed(second) == [{"v": 1, "cmd": "start", "id": "0123456789abcdef"}]
    assert reader.buffered_bytes == 0


def test_linereader_yields_several_messages_from_one_chunk():
    reader = LineReader()
    data = encode({"v": 1, "cmd": "ping"}) + encode({"v": 1, "cmd": "cancel", "id": "a"})
    assert reader.feed(data) == [
        {"v": 1, "cmd": "ping"},
        {"v": 1, "cmd": "cancel", "id": "a"},
    ]
    assert reader.buffered_bytes == 0


def test_linereader_buffers_a_trailing_partial_line():
    reader = LineReader()
    complete = encode({"v": 1, "cmd": "ping"})
    partial = b'{"v":1,"cmd":"sto'
    messages = reader.feed(complete + partial)
    assert messages == [{"v": 1, "cmd": "ping"}]
    assert reader.buffered_bytes == len(partial)

    assert reader.feed(b'p","id":"z"}\n') == [{"v": 1, "cmd": "stop", "id": "z"}]
    assert reader.buffered_bytes == 0


def test_linereader_reassembles_byte_by_byte():
    reader = LineReader()
    data = encode({"v": 1, "type": "result", "text": "नमस्ते", "id": "q"})
    collected = []
    for index in range(len(data)):
        collected.extend(reader.feed(data[index : index + 1]))
    assert collected == [{"v": 1, "type": "result", "text": "नमस्ते", "id": "q"}]


def test_linereader_raises_when_buffer_exceeds_max_line():
    reader = LineReader()
    with pytest.raises(ProtocolError):
        reader.feed(b"x" * (MAX_LINE + 1))


def test_linereader_raises_when_oversized_line_arrives_in_pieces():
    reader = LineReader()
    chunk = b"x" * (MAX_LINE // 2 + 1)
    with pytest.raises(ProtocolError):
        reader.feed(chunk)
        reader.feed(chunk)


def test_linereader_rejects_a_complete_but_invalid_line():
    reader = LineReader()
    with pytest.raises(ProtocolError):
        reader.feed(b"definitely not json\n")


# --------------------------------------------------------------------------
# message builders
# --------------------------------------------------------------------------


def test_state_msg_has_exactly_the_documented_keys():
    message = state_msg(STATE_RECORDING, "abc123")
    assert set(message) == {"v", "type", "state", "id"}
    assert message["v"] == 1
    assert message["type"] == "state"
    assert message["state"] == "recording"
    assert message["id"] == "abc123"


def test_state_msg_allows_a_null_id():
    message = state_msg(STATE_IDLE, None)
    assert message["id"] is None
    assert message["state"] == "idle"


def test_error_msg_has_exactly_the_documented_keys():
    message = error_msg("busy", "already recording", "abc123")
    assert set(message) == {"v", "type", "id", "code", "message"}
    assert message["v"] == 1
    assert message["type"] == "error"
    assert message["code"] == "busy"
    assert message["message"] == "already recording"
    assert message["id"] == "abc123"


def test_error_msg_allows_a_null_id():
    assert error_msg("bad_request", "unparseable line", None)["id"] is None


def test_result_msg_has_exactly_the_documented_keys():
    message = result_msg("abc123", "नमस्ते", "hi-IN", 2140, 1)
    assert set(message) == {
        "v",
        "type",
        "id",
        "text",
        "language_code",
        "duration_ms",
        "segments",
    }
    assert message["v"] == 1
    assert message["type"] == "result"
    assert message["id"] == "abc123"
    assert message["text"] == "नमस्ते"
    assert message["language_code"] == "hi-IN"
    assert message["duration_ms"] == 2140
    assert message["segments"] == 1


def test_result_msg_allows_empty_text_and_null_language():
    message = result_msg("abc123", "", None, 0, 0)
    assert message["text"] == ""
    assert message["language_code"] is None


@pytest.mark.parametrize("builder_call", [state_msg, error_msg, result_msg])
def test_builders_produce_encodable_compact_messages(builder_call):
    if builder_call is state_msg:
        message = builder_call(STATE_IDLE, None)
    elif builder_call is error_msg:
        message = builder_call("internal", "boom", None)
    else:
        message = builder_call("id", "text", None, 1, 1)
    assert decode_line(encode(message)) == message


# --------------------------------------------------------------------------
# brief 2, section 6: reload / reloaded / config_invalid
#
# protocol.py is frozen, so these two builders live in server.py. They are
# checked here, next to the other wire shapes, because the addon parses them.
# --------------------------------------------------------------------------


def test_reload_is_a_command_the_server_accepts():
    assert RELOAD_COMMAND == "reload"
    assert "reload" in SERVER_COMMANDS
    # Everything the frozen protocol already defined must still be accepted.
    assert set(PROTOCOL_COMMANDS) <= set(SERVER_COMMANDS)


def test_reloaded_msg_has_exactly_the_documented_keys():
    message = reloaded_msg("abc123")
    assert set(message) == {"v", "type", "id"}
    assert list(message) == ["v", "type", "id"]
    assert message["v"] == 1
    assert message["type"] == "reloaded"
    assert message["id"] == "abc123"


def test_config_invalid_msg_has_exactly_the_documented_keys():
    message = config_invalid_msg(
        "[api] mode 'shout' is not one of transcribe, translate", "abc123"
    )
    assert set(message) == {"v", "type", "id", "code", "message"}
    assert list(message) == ["v", "type", "id", "code", "message"]
    assert message["v"] == 1
    assert message["type"] == "error"
    assert message["code"] == "config_invalid"
    assert message["id"] == "abc123"
    assert "[api] mode" in message["message"]


def test_config_invalid_msg_allows_a_null_id():
    message = config_invalid_msg("malformed TOML", None)
    assert message["id"] is None
    assert message["code"] == "config_invalid"


def test_reload_builders_produce_encodable_compact_messages():
    for message in (
        reloaded_msg("abc123"),
        config_invalid_msg("[audio] backend 'x' is invalid", "abc123"),
    ):
        data = encode(message)
        assert data.endswith(b"\n")
        assert b'": ' not in data
        assert b", " not in data
        assert decode_line(data) == message


def test_reload_messages_round_trip_through_the_line_reader():
    reader = LineReader()
    data = encode(reloaded_msg("a1")) + encode(config_invalid_msg("[api] model", "b2"))
    assert reader.feed(data) == [
        reloaded_msg("a1"),
        config_invalid_msg("[api] model", "b2"),
    ]

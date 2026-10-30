# Wire protocol

This document, `src/sarvamdictation.cpp` and `daemon/sarvam_dictation/` are three descriptions of one contract. The C++ addon and the Python daemon are the two implementations of it, and this document is the reference they are checked against. Any change to a message, a field, a state name or an error code requires changing all three in the same commit; a change that lands on one side only produces a client and a server that disagree about what a line means, and the failure mode is silent (a message is ignored, or a recording is cancelled at the wrong moment) rather than loud.

## Transport

The daemon listens on an `AF_UNIX`, `SOCK_STREAM` socket. One connection is one client; the daemon accepts many connections at once and broadcasts state to all of them. The socket file is created with mode `0600`, so only the user who started the daemon can connect. There is no authentication beyond that file permission, and no TLS: the socket never leaves the machine.

## Socket path

Both sides compute the default path with the same rule, byte for byte:

```
if $XDG_RUNTIME_DIR is set and non-empty:  <XDG_RUNTIME_DIR>/sarvam-dictation.sock
else:                                      /tmp/sarvam-dictation-<uid>.sock
```

`<uid>` is the numeric real uid (`os.getuid()` in Python, `getuid()` in C++), written without padding. The C++ side reads `getenv("XDG_RUNTIME_DIR")` directly rather than going through fcitx5's `StandardPaths`, precisely so that the two processes cannot disagree about which directory the runtime socket belongs in.

Either side may override the path: the daemon through `[server] socket_path` in its config or `--socket` on its command line, the addon through its `SocketPath` option. Both default to the rule above, so an override is only needed when you deliberately want a non-default location, and it must be set on both sides or they will not find each other.

## Framing

Every message is one JSON object on one line, encoded as UTF-8 and terminated by a single `\n`. There are no length prefixes and no continuation lines. Encoders emit compact JSON (no spaces after `:` or `,`), though a receiver must accept any valid JSON whitespace, since it parses lines rather than matching text.

The maximum accepted line length is 1 MiB (1048576 bytes). A client that sends a line longer than that — measured on the buffered bytes before a newline is found — is disconnected. Receivers must tolerate partial lines (a read that ends mid-object) and must buffer until a newline arrives; a final line without a trailing newline at EOF is not a message.

An unparseable line, or a line that parses to something other than a JSON object, produces an `error` with code `bad_request` and `"id": null`. The connection stays open: a bad line is a client bug, not a protocol violation worth disconnecting for.

## Versioning

Every message carries `"v": 1`. The value is present in both directions and on every message type, including `pong`. A receiver that sees a different major version should treat the line as `bad_request` rather than guessing at field meanings; version 1 is the only version defined here. Additive changes (a new optional field, a new message type a client may ignore) keep `v` at 1; anything that changes the meaning of an existing field or the ownership rules bumps it.

## Client to server

Five commands. `id` is a client-generated UUIDv4 in hex form and is required on every command except `ping`.

```json
{"v":1,"cmd":"start","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31"}
{"v":1,"cmd":"stop","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
{"v":1,"cmd":"toggle","id":"b41e7d2c9a3f40518c6b2e7d0f4a931c"}
{"v":1,"cmd":"cancel","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
{"v":1,"cmd":"ping"}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `v` | integer | yes | Protocol version, `1`. |
| `cmd` | string | yes | One of `start`, `stop`, `toggle`, `cancel`, `ping`. |
| `id` | string | all but `ping` | Client-chosen identifier for this command. Echoed on every message that results from the command. |

`start` begins recording. If a recording is already in progress or a transcription is in flight, the reply is `error` with code `busy`.

`stop` ends the recording and starts transcription. If nothing is being recorded, the reply is `error` with code `busy`.

`toggle` is the convenience form used by the hotkey: it behaves as `start` when the daemon is idle and as `stop` when it is recording. Because a hotkey press does not know what state the daemon is in, this is the command the addon normally sends, and it is the command that makes the connection the owner of the recording when it starts one.

`cancel` stops the current recording and discards it without transcribing and without deleting anything the caller needs. It is legal in any state; when nothing is recording it is a no-op that still produces the current `state`. During `transcribing` it does not abort the in-flight HTTP request — the daemon replies with the `idle` state when that request finishes.

`ping` replies `{"v":1,"type":"pong"}` and carries no `id`. It exists so a client can check liveness without disturbing a recording, and it is what keeps the socket demonstrably responsive while transcription runs in a worker thread.

## Server to client

Four message types.

### `state`

```json
{"v":1,"type":"state","state":"recording","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31"}
```

| Field | Type | Meaning |
|---|---|---|
| `v` | integer | Protocol version, `1`. |
| `type` | string | `state`. |
| `state` | string | `idle`, `recording` or `transcribing`. |
| `id` | string or null | The `id` of the command that caused the change; `null` when the change was not caused by a command (the daemon starting, or recovering after an owner disconnect). |

State messages are broadcast to every connected client whenever the daemon's state changes, not only to the connection that caused the change. A second client therefore sees that someone else is recording, even though it will never see that recording's result.

### `result`

```json
{"v":1,"type":"result","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60","text":"नमस्ते, मैं ठीक हूँ।","language_code":"hi-IN","duration_ms":2140,"segments":1}
```

| Field | Type | Meaning |
|---|---|---|
| `v` | integer | Protocol version, `1`. |
| `type` | string | `result`. |
| `id` | string | The `id` of the `stop` (or `toggle`-that-stopped) command that produced this transcript. |
| `text` | string | The final transcript, already whitespace-stripped. May be empty. |
| `language_code` | string or null | The language the service reported, e.g. `hi-IN`; `null` when the service did not report one. |
| `duration_ms` | integer | Duration of the recorded audio in milliseconds, measured on the normalised file. |
| `segments` | integer | Number of chunks the audio was split into before transcription: `1` for anything up to 28 seconds, more for longer audio. |

An empty transcript is a legal, successful outcome — silence, a muted microphone, or noise the model declines to transcribe all produce `result` with `"text": ""`. The client decides what to do with it; the addon commits nothing in that case. A result is never replaced by an error once it has been sent.

`result` is delivered only to the owner of the recording (see below), never broadcast.

### `error`

```json
{"v":1,"type":"error","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60","code":"no_api_key","message":"No Sarvam API key configured"}
```

| Field | Type | Meaning |
|---|---|---|
| `v` | integer | Protocol version, `1`. |
| `type` | string | `error`. |
| `id` | string or null | The `id` of the command that failed; `null` when the failing line could not be attributed to a command (unparseable JSON, unknown `cmd`, missing `id`). |
| `code` | string | One of the codes in the table below. Clients switch on this, not on the message text. |
| `message` | string | Human-readable detail. May be shown to the user, so it must never contain the API key. |

`error` is delivered only to the owner of the recording, except for `bad_request` errors caused by a line the daemon cannot attribute, which go back on the connection that sent the line.

### `pong`

```json
{"v":1,"type":"pong"}
```

The only message with no `id` field and no `type`-specific payload.

## Ownership

The connection that sends `start`, or a `toggle` that starts a recording, becomes the **owner** of that recording. `result` and `error` for that recording go to the owner connection only, even when several clients are connected and even though the state changes were broadcast to all of them.

Ownership ends the moment the result or error is delivered. After that, any client may start a new recording.

If the owner's connection closes while it owns a recording, the daemon cancels the recording, discards the audio and returns to `idle`. This is what stops an abandoned recording from continuing to capture after the window that started it has gone. A `stop` or `cancel` arriving from a non-owner connection while a recording is owned is answered with `error` code `busy` and the message `another client owns the recording`.

The rule exists because a recording is a scarce, exclusive resource and its output belongs to whoever asked for it. Broadcasting results would type one person's dictation into another client's field; broadcasting nothing at all and letting any client stop any recording would let a stray connection silently destroy someone else's audio.

## State machine

```
idle --start/toggle--> recording --stop--> transcribing --> idle (result delivered)
recording --cancel--> idle
recording --owner disconnect--> idle (discarded)
transcribing --cancel--> (no effect on the in-flight request; reply state idle when it finishes)
```

There is one recording at a time. A `start` or `toggle`-that-starts arriving while the daemon is `recording` or `transcribing` produces `error` code `busy` and leaves the state unchanged. A `stop` arriving while `idle` produces `error` code `busy` as well.

The daemon is a single asyncio loop. Transcription runs in a worker thread (`asyncio.to_thread`), so the loop keeps reading the socket throughout: `ping` is answered and `cancel` is accepted while the HTTP request is outstanding. Anything that would block the loop — reading audio, waiting on the network — happens off it.

## Error codes

| Code | Meaning | Typical trigger |
|---|---|---|
| `busy` | The command is not legal in the current state, or the recording belongs to someone else. | `start` while recording or transcribing; `stop` while idle; `stop` or `cancel` from a non-owner connection. |
| `bad_request` | The line could not be understood. | Unparseable JSON, a JSON value that is not an object, an unknown `cmd`, or a missing `id` on a command that requires one. |
| `no_api_key` | No API key is available, so no request was attempted. | Neither `[api] key` in the config file nor `SARVAM_API_KEY` in the environment is set. |
| `no_audio_device` | No usable capture backend, or the configured device is unusable. | None of `pw-record`, `parec`, `arecord` on `PATH`; an `[audio] device` that does not exist. |
| `audio_failed` | Capture started but produced nothing usable. | Zero-length or unreadable WAV; the capture process died before it was asked to stop; `stop` called when no recording is running. |
| `network` | The API could not be reached or the connection failed. | DNS failure, no route, TLS failure, timeout. |
| `api` | The API answered with an error. | An invalid or expired key (HTTP 401), a rejected model or mode, rate limiting, a server error. |
| `too_long` | The audio exceeds what a single request may carry. | The daemon avoids this by splitting at 28 seconds and by stopping automatically at `max_duration_s`; the code exists so that both sides agree if it ever occurs. |
| `internal` | An unexpected daemon-side failure. | A bug, an unreadable temporary file, a missing SDK. |

Codes are part of the protocol: a client maps each one to its own user-facing sentence and must tolerate an unfamiliar code by falling back to the `message` text.

## Worked session

One client, one dictation, from an idle daemon. `C:` is the addon, `S:` is the daemon; every line is a complete frame.

```
C: {"v":1,"cmd":"ping"}
S: {"v":1,"type":"pong"}
C: {"v":1,"cmd":"toggle","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31"}
S: {"v":1,"type":"state","state":"recording","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31"}
C: {"v":1,"cmd":"toggle","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
S: {"v":1,"type":"state","state":"transcribing","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
S: {"v":1,"type":"result","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60","text":"नमस्ते, मैं ठीक हूँ।","language_code":"hi-IN","duration_ms":2140,"segments":1}
S: {"v":1,"type":"state","state":"idle","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
```

The first `toggle` starts the recording and makes this connection its owner; the resulting `state` carries that command's `id`. The second `toggle` stops it, producing a `transcribing` state, then the `result`, then the `idle` state as the daemon returns to rest. The addon commits `text` to the input context it captured at the first press, appends a space, and closes the "Transcribing…" notification when the `idle` arrives.

A failure looks like this instead — the recording ran, the key was missing:

```
C: {"v":1,"cmd":"toggle","id":"b41e7d2c9a3f40518c6b2e7d0f4a931c"}
S: {"v":1,"type":"state","state":"recording","id":"b41e7d2c9a3f40518c6b2e7d0f4a931c"}
C: {"v":1,"cmd":"toggle","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
S: {"v":1,"type":"state","state":"transcribing","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
S: {"v":1,"type":"error","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873","code":"no_api_key","message":"No Sarvam API key configured"}
S: {"v":1,"type":"state","state":"idle","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
```

A second connection watching the same daemon sees both `state` lines and neither the `result` nor the `error`. If it tries `{"v":1,"cmd":"stop","id":"5d3e8f1a7c2b49068e1f3a5c7d9b2e40"}` while the first connection owns the recording, it receives `{"v":1,"type":"error","id":"5d3e8f1a7c2b49068e1f3a5c7d9b2e40","code":"busy","message":"another client owns the recording"}`, and the first connection's recording continues undisturbed.

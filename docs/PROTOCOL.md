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

Either side may override the path, and both read the override from the same place: `[server] socket_path` in the daemon config file, or `--socket` on the daemon's command line. The addon takes `[server] socket_path` from `config.toml` too, so an override is set once rather than twice, and the two processes cannot end up looking in different places because one of them was missed. Both default to the rule above, so an override is only needed when you deliberately want a non-default location.

## Framing

Every message is one JSON object on one line, encoded as UTF-8 and terminated by a single `\n`. There are no length prefixes and no continuation lines. Encoders emit compact JSON (no spaces after `:` or `,`), though a receiver must accept any valid JSON whitespace, since it parses lines rather than matching text.

The maximum accepted line length is 1 MiB (1048576 bytes). A client that sends a line longer than that — measured on the buffered bytes before a newline is found — is disconnected. Receivers must tolerate partial lines (a read that ends mid-object) and must buffer until a newline arrives; a final line without a trailing newline at EOF is not a message.

An unparseable line, or a line that parses to something other than a JSON object, produces an `error` with code `bad_request` and `"id": null`. The connection stays open: a bad line is a client bug, not a protocol violation worth disconnecting for.

## Versioning

Every message carries `"v": 1`. The value is present in both directions and on every message type, including `pong`. A receiver that sees a different major version should treat the line as `bad_request` rather than guessing at field meanings; version 1 is the only version defined here. Additive changes (a new optional field, a new message type a client may ignore) keep `v` at 1; anything that changes the meaning of an existing field or the ownership rules bumps it.

## Client to server

Six commands. `id` is a client-generated UUIDv4 in hex form and is required on every command except `ping`.

```json
{"v":1,"cmd":"start","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31"}
{"v":1,"cmd":"stop","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
{"v":1,"cmd":"toggle","id":"b41e7d2c9a3f40518c6b2e7d0f4a931c"}
{"v":1,"cmd":"cancel","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
{"v":1,"cmd":"reload","id":"a6f4b2d80c9e41f7b3d5a1e6c8f02b94"}
{"v":1,"cmd":"ping"}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `v` | integer | yes | Protocol version, `1`. |
| `cmd` | string | yes | One of `start`, `stop`, `toggle`, `cancel`, `reload`, `ping`. |
| `id` | string | all but `ping` | Client-chosen identifier for this command. Echoed on every message that results from the command. |

`start` begins recording. If a recording is already in progress or a transcription is in flight, the reply is `error` with code `busy`.

`stop` ends the recording and starts transcription. If nothing is being recorded, the reply is `error` with code `busy`.

`toggle` is the convenience form used by the hotkey: it behaves as `start` when the daemon is idle and as `stop` when it is recording. Because a hotkey press does not know what state the daemon is in, this is the command the addon normally sends, and it is the command that makes the connection the owner of the recording when it starts one.

`cancel` stops the current recording and discards it without transcribing and without deleting anything the caller needs. It is legal in any state; when nothing is recording it is a no-op that still produces the current `state`. During `transcribing` it does not abort the in-flight HTTP request — the daemon replies with the `idle` state when that request finishes.

`reload` re-reads the daemon config file from disk and applies it to future recordings. It is legal in any state and is answered with `reloaded` or with an `error` carrying code `config_invalid`. It does not carry a config path: the daemon always reads the file it was started with, which is `~/.config/sarvam-dictation/config.toml` (or `$XDG_CONFIG_HOME/sarvam-dictation/config.toml`) unless `--config` said otherwise. The full semantics are in [Configuration reload](#configuration-reload).

`ping` replies `{"v":1,"type":"pong"}` and carries no `id`. It exists so a client can check liveness without disturbing a recording, and it is what keeps the socket demonstrably responsive while transcription runs in a worker thread.

### The command name carries the mode

A recording can be started in one of two ways, and each way streams according to a setting of its own. The mapping is load-bearing rather than incidental: the command name is what tells the daemon which mode it is in, and therefore which streaming setting applies.

| Command that starts the recording | Interaction | Streaming setting used |
|---|---|---|
| `start`, ended by `stop` | Push-to-talk: the key is held down, and its release ends the recording. | `[api] streaming_push_to_talk` |
| `toggle` | Record and send: the trigger key starts the recording and the same key ends it. | `[api] streaming_toggle` |

The daemon decides when the recording starts, from the command that started it. A `start` is the push-to-talk shape and reads `[api] streaming_push_to_talk`; a `toggle` that starts a recording is the record-and-send shape and reads `[api] streaming_toggle`. Nothing else about the two paths differs: both go through the same start path, the same busy check and the same auto-stop scheduling, and both end in the same `result`. A `stop` carries no setting of its own — it ends the recording that `start` began, and that recording streams or does not according to how it was started. A `toggle` that arrives while a recording is in progress is a stop, not a second start: it ends the recording in progress under the setting that recording began with, and it does not re-read the config file.

No new field was added to the protocol for this, and none is needed. The two shapes were already distinct commands before either of them could stream, so the command name carries the mode. There is no `mode` field on a command, no extra argument on `start`, and no new message type; the `v` of every message stays at `1`, because nothing about the meaning of an existing field changed. A client that predates the split needs no change at all: it sends the same two commands it always did, and the daemon resolves the setting for it.

How a setting is resolved from the config file — the per-mode key, then the legacy `[api] streaming`, then false — is described in [ARCHITECTURE.md](ARCHITECTURE.md). The protocol carries the consequence of that resolution and not the resolution itself: a streaming recording is started, stopped and cancelled with `start`, `stop`, `toggle` and `cancel`, exactly as a file-based one is.

## Server to client

Six message types.

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

### `partial`

```json
{"v":1,"type":"partial","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31","text":"नमस्ते दुनिया"}
```

| Field | Type | Meaning |
|---|---|---|
| `v` | integer | Protocol version, `1`. |
| `type` | string | `partial`. |
| `id` | string | The `id` of the `start` (or `toggle`-that-started) command that began the recording. |
| `text` | string | The latest partial transcript for the whole recording so far, already whitespace-stripped. Never empty. |

`partial` exists only while a streaming recording is in progress — that is, when the streaming setting for the mode that started it is on, `[api] streaming_push_to_talk` for a `start` and `[api] streaming_toggle` for a `toggle` — and carries the interim text the realtime service has recognised so far. The addon shows it through the recording indicator, which is what makes push-to-talk feel live: the words appear while the user is still speaking. The `text` is cumulative, not incremental: it is the whole recording up to that moment, so a client replaces what it is displaying rather than appending to it, and a client that misses one `partial` loses nothing but a frame. It carries the `id` of the `start` that began the recording, not of the `stop` that ends it, so a client with more than one session in its history can match a partial to the session it belongs to.

It is delivered only to the owner of the recording, never broadcast, and it is throttled: the daemon sends at most one `partial` per 100 ms, about ten per second, and always delivers the last text it has seen, so the client's view lags the service by no more than that interval. A partial identical to the one last sent is not sent again, which keeps a long pause from producing a run of identical frames; a client may therefore assume that each `partial` it receives differs from the previous one, and it must not assume any particular interval between them beyond that minimum.

`partial` never replaces `result`. The final transcript still arrives as `result` with the `id` of the `stop` that ended the recording, carrying the `text`, `language_code`, `duration_ms` and `segments` fields it always carries, and it is sent once. A client that ignores `partial` entirely sees exactly the protocol it saw before streaming existed, which is why no new command was added: `start`, `stop`, `toggle` and `cancel` mean what they meant, and a streaming recording is started, stopped and cancelled with the same commands as a file-based one.

### `reloaded`

```json
{"v":1,"type":"reloaded","id":"a6f4b2d80c9e41f7b3d5a1e6c8f02b94"}
```

| Field | Type | Meaning |
|---|---|---|
| `v` | integer | Protocol version, `1`. |
| `type` | string | `reloaded`. |
| `id` | string | The `id` of the `reload` command that this message answers. |

`reloaded` says that the config file was read and applied. It is the acknowledgement of one `reload` command and carries nothing else: no copy of the settings, no summary of what changed. It is delivered to the connection that sent the `reload`, never broadcast, and it never accompanies a `state` message.

A missing config file also produces `reloaded`, because falling back to the built-in defaults is a successful reload and is what the daemon does at startup too.

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

`error` is delivered only to the owner of the recording, except for `bad_request` errors caused by a line the daemon cannot attribute, which go back on the connection that sent the line, and for errors answering a command that is not tied to a recording — `reload`, whose `config_invalid` reply goes back to the connection that asked.

### `pong`

```json
{"v":1,"type":"pong"}
```

The only message with no `id` field and no `type`-specific payload.

## Ownership

The connection that sends `start`, or a `toggle` that starts a recording, becomes the **owner** of that recording. `result`, `partial` and `error` for that recording go to the owner connection only, even when several clients are connected and even though the state changes were broadcast to all of them.

Ownership ends the moment the result or error is delivered. After that, any client may start a new recording.

If the owner's connection closes while it owns a recording, the daemon cancels the recording, discards the audio and returns to `idle`. This is what stops an abandoned recording from continuing to capture after the window that started it has gone. A `stop` or `cancel` arriving from a non-owner connection while a recording is owned is answered with `error` code `busy` and the message `another client owns the recording`.

The rule exists because a recording is a scarce, exclusive resource and its output belongs to whoever asked for it. Broadcasting results would type one person's dictation into another client's field; broadcasting nothing at all and letting any client stop any recording would let a stray connection silently destroy someone else's audio.

`reload` is outside this rule: it belongs to no recording, so any connection may send it at any time, and its reply goes to the sender.

## State machine

```
idle --start/toggle--> recording --stop--> transcribing --> idle (result delivered)
recording --cancel--> idle
recording --owner disconnect--> idle (discarded)
transcribing --cancel--> (no effect on the in-flight request; reply state idle when it finishes)
```

There is one recording at a time. A `start` or `toggle`-that-starts arriving while the daemon is `recording` or `transcribing` produces `error` code `busy` and leaves the state unchanged. A `stop` arriving while `idle` produces `error` code `busy` as well.

`reload` appears nowhere in that diagram on purpose: it is not a transition. It leaves `idle`, `recording` and `transcribing` exactly as they were.

The daemon is a single asyncio loop. Transcription runs in a worker thread (`asyncio.to_thread`), so the loop keeps reading the socket throughout: `ping` is answered and `cancel` is accepted while the HTTP request is outstanding. Anything that would block the loop — reading audio, waiting on the network — happens off it.

## Configuration reload

`reload` is how a running daemon is told that its config file changed. The file itself is the only channel for settings; nothing about configuration is pushed over the socket.

The rules, in full:

* **Future operations only.** The daemon re-reads `~/.config/sarvam-dictation/config.toml` and applies the result to the next recording. A recording in progress, and a transcription already in flight, complete with the settings they started with. Nothing is retried or restarted.
* **A missing file succeeds.** The daemon falls back to its built-in defaults, exactly as it does at startup, and answers `reloaded`.
* **An invalid file fails without consequences.** If the file cannot be parsed or a value is out of range, the reply is `error` with code `config_invalid` and a `message` that names the offending key, for example `[api] mode 'dictate' is not one of transcribe, translate, verbatim, translit, codemix`. The daemon keeps running with the settings it already had and does **not** drop the socket: a bad config is a user mistake to be corrected, not a reason to break the client's connection.
* **Legal in any state.** `idle`, `recording` and `transcribing` all accept `reload`. It never disturbs the state machine, never touches ownership, and never interrupts the current recording.
* **Never broadcast.** No `state` message is emitted and no other connection is told that a reload happened. A reload is not a state change, and clients must not wait for one.
* **`id` is required**, like every command but `ping`. Without it the reply is `bad_request`.

The addon sends `reload` immediately after it writes `config.toml` from the fcitx5 configuration page, and once more right after a successful socket connect, so a daemon that was started long ago — by systemd, say — catches up with a file that changed while it was not connected.

## Error codes

| Code | Meaning | Typical trigger |
|---|---|---|
| `busy` | The command is not legal in the current state, or the recording belongs to someone else. | `start` while recording or transcribing; `stop` while idle; `stop` or `cancel` from a non-owner connection. |
| `bad_request` | The line could not be understood. | Unparseable JSON, a JSON value that is not an object, an unknown `cmd`, or a missing `id` on a command that requires one. |
| `config_invalid` | The config file could not be read as valid settings, so nothing was applied and the daemon kept its previous settings. | A `reload` after the file was hand-edited into something unparseable, or a value the daemon rejects: an unknown `[api] mode`, an unknown `[audio] backend`, an empty `[api] model`, a non-positive `[audio] sample_rate` or `[server] max_duration_s`. The `message` names the key. |
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
C: {"v":1,"cmd":"reload","id":"a6f4b2d80c9e41f7b3d5a1e6c8f02b94"}
S: {"v":1,"type":"reloaded","id":"a6f4b2d80c9e41f7b3d5a1e6c8f02b94"}
C: {"v":1,"cmd":"toggle","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31"}
S: {"v":1,"type":"state","state":"recording","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31"}
C: {"v":1,"cmd":"toggle","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
S: {"v":1,"type":"state","state":"transcribing","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
S: {"v":1,"type":"result","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60","text":"नमस्ते, मैं ठीक हूँ।","language_code":"hi-IN","duration_ms":2140,"segments":1}
S: {"v":1,"type":"state","state":"idle","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
```

The `reload` right after the connect is the addon catching the daemon up with `config.toml`: the addon had read the file, and the daemon may have been running since before the last change. The answer is `reloaded`, with no `state` line in between, because a reload is not a transition. The first `toggle` then starts the recording and makes this connection its owner; the resulting `state` carries that command's `id`. The second `toggle` stops it, producing a `transcribing` state, then the `result`, then the `idle` state as the daemon returns to rest. The addon commits `text` to the input context it captured at the first press, appends a space, and closes the "Transcribing…" notification when the `idle` arrives.

A streaming recording is the same session with `partial` frames in the middle. With the streaming setting for the mode in use on — `[api] streaming_toggle` for this `toggle` session — the daemon transcribes while the audio arrives and forwards the interim text to the owner:

```
C: {"v":1,"cmd":"toggle","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31"}
S: {"v":1,"type":"state","state":"recording","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31"}
S: {"v":1,"type":"partial","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31","text":"नमस्ते"}
S: {"v":1,"type":"partial","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31","text":"नमस्ते, मैं"}
S: {"v":1,"type":"partial","id":"3f2a9c1e8b4d47a6b0e5c2d19f7a6b31","text":"नमस्ते, मैं ठीक"}
C: {"v":1,"cmd":"toggle","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
S: {"v":1,"type":"state","state":"transcribing","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
S: {"v":1,"type":"result","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60","text":"नमस्ते, मैं ठीक हूँ।","language_code":"hi-IN","duration_ms":2140,"segments":1}
S: {"v":1,"type":"state","state":"idle","id":"7c1d0a4f2e6b48c39a5d8e3f1b2c7a60"}
```

The three `partial` frames carry the `id` of the `toggle` that started the recording, which is how the addon knows they belong to the session it opened, and each `text` is longer than the one before because the text is cumulative. The stop is an ordinary `toggle` and produces the same three messages as the non-streaming session — `transcribing`, `result`, `idle` — so nothing about the end of a recording differs between the two paths. The final `text` is longer than the last `partial` because the closing words are recognised when the utterance ends, and they arrive with the `result`. A second connection watching the same daemon sees the two `state` lines and none of the `partial` lines.

A reload can also arrive in the middle of a recording, which is what happens when the user presses *Apply* in the configuration page while dictating. Nothing about the recording changes:

```
C: {"v":1,"cmd":"toggle","id":"b41e7d2c9a3f40518c6b2e7d0f4a931c"}
S: {"v":1,"type":"state","state":"recording","id":"b41e7d2c9a3f40518c6b2e7d0f4a931c"}
C: {"v":1,"cmd":"reload","id":"c58a1f3b7d2e46098b4c6a1f3e7d205b"}
S: {"v":1,"type":"reloaded","id":"c58a1f3b7d2e46098b4c6a1f3e7d205b"}
C: {"v":1,"cmd":"toggle","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
S: {"v":1,"type":"state","state":"transcribing","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
S: {"v":1,"type":"result","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873","text":"अगली बैठक सोमवार को है।","language_code":"hi-IN","duration_ms":3120,"segments":1}
S: {"v":1,"type":"state","state":"idle","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
```

The `reloaded` in the middle is the only line the reload produces. The recording started with the old settings, so it is transcribed with them; the settings written by that *Apply* take effect from the next recording.

A reload of a file the daemon cannot accept answers with an error and changes nothing:

```
C: {"v":1,"cmd":"reload","id":"9b7c2e4a1f6d43058c2b9e7a4d1f6308"}
S: {"v":1,"type":"error","id":"9b7c2e4a1f6d43058c2b9e7a4d1f6308","code":"config_invalid","message":"[api] mode 'dictate' is not one of transcribe, translate, verbatim, translit, codemix"}
```

The connection stays open and the daemon keeps transcribing with the settings it had.

A failure during dictation looks like this instead — the recording ran, the key was missing:

```
C: {"v":1,"cmd":"toggle","id":"b41e7d2c9a3f40518c6b2e7d0f4a931c"}
S: {"v":1,"type":"state","state":"recording","id":"b41e7d2c9a3f40518c6b2e7d0f4a931c"}
C: {"v":1,"cmd":"toggle","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
S: {"v":1,"type":"state","state":"transcribing","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
S: {"v":1,"type":"error","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873","code":"no_api_key","message":"No Sarvam API key configured"}
S: {"v":1,"type":"state","state":"idle","id":"e09b3c5a1d7f42e8b6a4c9d2f0e1b873"}
```

A second connection watching the same daemon sees both `state` lines and neither the `result` nor the `error`. If it tries `{"v":1,"cmd":"stop","id":"5d3e8f1a7c2b49068e1f3a5c7d9b2e40"}` while the first connection owns the recording, it receives `{"v":1,"type":"error","id":"5d3e8f1a7c2b49068e1f3a5c7d9b2e40","code":"busy","message":"another client owns the recording"}`, and the first connection's recording continues undisturbed. If that second connection sends `reload` instead, it is answered with `reloaded` — and the first connection sees nothing at all.

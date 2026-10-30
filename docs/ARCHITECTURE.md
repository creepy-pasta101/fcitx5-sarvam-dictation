# Architecture

This document explains why the project is built the way it is. The message format itself is in [PROTOCOL.md](PROTOCOL.md); this file is about the shape around it.

## Two processes

Dictation needs three things that do not live comfortably in one place: a global hotkey inside the desktop's input framework, a microphone stream that has to be spawned, held open for as long as the user talks and then shut down cleanly, and an HTTPS request that can take seconds and can fail in a dozen ways. The first belongs inside fcitx5, because that is the only process that can see key events from every application and commit text into the focused field. The second and third belong anywhere but inside fcitx5, because fcitx5 runs a single event loop that also drives keyboard input for the whole desktop: a blocking read, a blocking `waitpid`, or a synchronous network call in an addon freezes typing in every window, not just the one being dictated into.

So the work is split. The C++ addon is a thin client — hotkey, socket, notifications, `commitString` — and is written so that nothing it does can block. The Python daemon is a separate process with its own asyncio loop, its own failure modes, and the freedom to spawn subprocesses and wait on the network. If the daemon crashes or hangs, typing keeps working; if fcitx5 restarts, the daemon keeps running and the next hotkey press reconnects to it. The two communicate over a Unix stream socket using newline-delimited JSON, and that protocol is the only thing they share.

The split also keeps the dependency budget honest. The addon links against fcitx5 and the C++ standard library and nothing else — no HTTP client, no JSON library, no TLS. All of that lives in Python, where the `sarvamai` SDK already exists.

## The addon in the input path

The addon is a **Module** addon, not an input method engine. That distinction is the single most important decision in the design. An input method engine is only fed key events while it is the *active* input method for an input context; when the user has switched to a different method, or when an application is using a plain keyboard layout, the engine sees nothing. Dictation has to work in whatever field the user is in, with whatever input method that field happens to be using, so an engine would defeat the entire purpose — you would have to switch to the dictation method before you could dictate, which is exactly the friction the tool is meant to remove. A Module addon has no such restriction: it is loaded once by the instance and can observe events globally.

The hotkey is therefore registered by watching key events on the instance, before the input method gets them:

```cpp
eventHandlers_.emplace_back(
    instance_->watchEvent<EventType::InputContextKeyEvent>(
        EventWatcherPhase::PreInputMethod, [this](KeyEvent &keyEvent) {
            // match the configured TriggerKey against keyEvent
        }));
```

`EventWatcherPhase::PreInputMethod` means the handler runs before the active input method sees the key, so a configured hotkey is ours to take regardless of what else is listening. When the key matches, the handler calls `filterAndAccept()` on the event — which stops it from reaching the application as ordinary typing — and starts the work. `KeyEvent::isRelease()` is checked first and releases are ignored; without that check the same physical press would fire the handler twice, starting and immediately stopping the recording.

Handlers registered this way are owned by an `eventHandlers_` member of the addon, so they unregister when the addon is destroyed and cannot outlive the object that owns them. The trigger key itself is a `KeyListOption`, so the user can bind several combinations and can change them from `fcitx5-configtool` without restarting anything.

## Delivering the transcript

When the hotkey fires, the addon remembers the `InputContext` that had focus at that moment. It stores it as a `TrackableObjectReference<InputContext>` rather than a raw pointer, because transcription takes seconds and the user may close the window, switch workspaces, or quit the application while it runs — the input context can be destroyed before the answer arrives, and a raw pointer would then be dangling. A trackable reference knows when the object is gone.

When the `result` message arrives, the addon commits into the remembered context if it is still alive:

```cpp
if (inputContext) { inputContext->commitString(text); }
```

If it is not — the window closed, the application exited — the text goes to `instance_->mostRecentInputContext()` instead, which is the best available guess at where the user is now. This is a deliberate compromise: it can put the transcript in a window the user did not intend, but the alternative is silently discarding several seconds of speech. If `AppendSpace` is enabled and the text is non-empty and does not already end in whitespace, one space is appended so that consecutive dictations do not run together. An empty transcript commits nothing at all.

Notifications run through the fcitx5 notifications module, declared as an *optional* dependency: every call is guarded by `if (auto *n = notifications())`, so the addon still works on a desktop where that module is not installed, just without the "Listening…" and "Transcribing…" tips. `ShowNotifications` turns them off entirely for users who find them noisy.

## Reaching the daemon

The addon connects lazily, on the first hotkey press, and keeps the socket open afterwards. If the connect fails with `ENOENT` or `ECONNREFUSED` and `AutoStartDaemon` is enabled, it forks and execs `DaemonCommand` — detached with `setsid`, so the daemon survives an fcitx5 restart instead of dying with its parent — and then retries the connect every 200 ms for up to five seconds. The command that triggered all this is queued and flushed as soon as a connection succeeds, so the user's first press still starts a recording; if the retries run out, the pending command is dropped and a notification says the daemon is not reachable. At most one daemon is spawned per trigger, never one per retry.

Reads on the socket go through fcitx5's own event loop: `instance_->eventLoop().addIOEvent(fd, IOEventFlag::In, cb)` returns an event source owned by an addon member, and the callback reads whatever is available, appends it to a line buffer, and dispatches every complete line it finds. The socket is non-blocking, partial lines are kept until the newline arrives, and the buffer is capped at 1 MiB, matching the protocol's limit. A malformed line is logged and skipped — it never propagates as an exception into fcitx5's loop. After a disconnect the addon simply tries again on the next hotkey press.

## The daemon's audio pipeline

Recording is a sequence of small, individually boring steps, and the order matters.

Capture is a subprocess: `pw-record`, `parec` or `arecord`, chosen by `detect_backend` according to `[audio] backend`, defaulting to whichever is first on `PATH`. The daemon does not link an audio library; a subprocess is one less thing to build against, and all three tools write a WAV the rest of the pipeline can read.

Stopping the capture is the delicate part. A WAV header written at the start of a file contains sizes that are only correct once recording ends, so a capture process that is killed outright leaves a header claiming zero or nonsense length — the audio is all there and none of it can be read. The daemon therefore shuts the child down gracefully: `SIGINT` first, wait up to three seconds, then `SIGTERM`, wait two, and only then `SIGKILL`. Every one of the three tools finalises its header on a polite signal; none of them does on `SIGKILL`. `SIGKILL` is the last resort, not the first.

The finished file is then normalised to 16 kHz mono 16-bit PCM, which is what the service expects and what keeps the chunk arithmetic predictable. The duration is read back from the normalised file rather than estimated from the process's run time, so `duration_ms` in the result is a measurement.

If the recording is longer than 28 seconds, it is split into ordered chunks with the standard library's `wave` module, each chunk transcribed separately, and the pieces joined: each part stripped, empty parts dropped, and the rest joined with single spaces. 28 seconds rather than 30 leaves headroom under the REST endpoint's roughly 30-second limit, so a chunk that is a few milliseconds over is still accepted. `segments` in the result reports how many chunks were used. `max_duration_s` (default 300) is a separate safety net: it stops the recording automatically, so a session the user forgot about cannot run until the disk fills.

The transcription call itself uses the `sarvamai` SDK, imported inside the method that needs it. That keeps the daemon importable, and its tests runnable, on a machine where the SDK is not installed; and it means a missing SDK surfaces as a transcription error at the moment of use rather than as a startup failure. The API key is read from `[api] key` or `SARVAM_API_KEY` before any network call is attempted, so a missing key produces `no_api_key` immediately rather than after a failed request. The key never appears in an error message or a log line.

## Threading model

The daemon is one asyncio loop, one task per client connection, and one worker thread at a time. Everything that touches the socket is a coroutine on the loop, so message handling is naturally serialised and the state machine needs no locks. Everything that blocks — spawning the recorder, waiting for it to exit, normalising the WAV, splitting it, calling the API — runs inside `asyncio.to_thread`. That is what keeps the loop responsive while a transcription is in flight: `ping` is answered and `cancel` is accepted throughout, which is what the protocol promises.

The consequence is that the daemon does one transcription at a time. That is intentional, not a limitation of the threading: one recording at a time is the protocol's rule, and serialising the API calls keeps a burst of dictation from exhausting rate limits.

## Failure modes

| Failure | Handling |
|---|---|
| Daemon not running | Addon autostarts it (if enabled), retries the connect 25 times at 200 ms, then notifies the user. |
| Daemon not found on the addon's socket path | Both sides derive the path from the same rule; an override on one side without the other is the usual cause, and the symptom is "daemon not reachable" while the daemon is visibly running. |
| Stale socket file after a daemon crash | The daemon unlinks a stale socket only after a connect attempt proves nobody is listening, so starting a second daemon cannot steal a live one's socket. |
| No capture backend / wrong device | `no_audio_device` reaches the client, which shows a message pointing at the audio configuration. |
| Capture child dies or writes nothing | Zero-length or unreadable output becomes `audio_failed`; the file is discarded and the daemon returns to idle. |
| Owner disconnects mid-recording | The daemon cancels and discards the recording and returns to `idle`, so an abandoned session does not keep the microphone open. |
| Window closes while transcribing | The trackable reference reports the input context dead; the text goes to the most recent input context instead. |
| API rejects the key or the request | The SDK exception is mapped to `api` (or `network` for transport failures) with a message that includes the underlying error but never the key. |
| Malformed line from a client | Answered with `bad_request`; the connection stays open. The addon's own parser skips bad lines and never throws into fcitx5's loop. |
| Line longer than 1 MiB | The client is disconnected; both sides enforce the same cap. |
| Empty transcript | A legal `result` with empty text; the addon commits nothing and closes the notification. |

## Known limitations

The service is used over its REST endpoint only, so there is no live interim transcript — nothing is typed until you stop recording and the whole clip has been transcribed. A streaming mode would need a different transport and a different protocol.

There is no command vocabulary. You cannot say "comma" or "new line" and get punctuation or a line break; whatever the model returns, including its own punctuation, is what gets committed. Punctuation quality therefore depends on the model and the mode.

One recording is in flight at a time, for the whole daemon, not just per client. A second client that tries to start while a recording is owned gets `busy`.

A working audio capture backend is required: one of `pw-record`, `parec` or `arecord` must be installed and must be able to open a source. The daemon does not fall back to reading the device directly.

Transcription requires network access to Sarvam's API and a valid key. Nothing about the tool works offline.

## Fcitx 5 version compatibility

The addon targets the fcitx5 **5.1.x** series, which is what Debian 12, Ubuntu 24.04 and current Fedora releases ship, and it also builds against 5.2. Three places in the code exist only because of that target:

The hotkey is registered through the runtime-typed `Instance::watchEvent(EventType, EventWatcherPhase, EventHandler)` overload and the event is then cast to `KeyEvent`. The templated `watchEvent<EventType::InputContextKeyEvent>(phase, callback)` form only appeared in 5.2.

Key matching walks the configured `KeyList` and calls `Key::check` rather than `KeyEvent::checkKeyList` with `KeyEventMatchingMode::MatchAllNormalizedKeys`, which is also 5.2-only.

`fcitx/addonmanager.h` is included from the addon header because `FCITX_ADDON_DEPENDENCY_LOADER` expands to a call on `AddonManager`, so the type has to be complete there rather than merely forward declared.

Everything else — `EventSourceIO`, `addIOEvent`, `addTimeEvent`, `EventSourceTime::setTime`, `TrackableObjectReference`, `InputContext::commitString`, the notifications module's `showTip` — is stable across both series. The addon is compiled warning-free under `-Wall -Wextra` with C++17.

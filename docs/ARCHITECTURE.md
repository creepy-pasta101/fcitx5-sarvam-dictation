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

`EventWatcherPhase::PreInputMethod` means the handler runs before the active input method sees the key, so a configured hotkey is ours to take regardless of what else is listening. When the key matches, the handler calls `filterAndAccept()` on the event — which stops it from reaching the application as ordinary typing — and starts the work. `KeyEvent::isRelease()` is checked first, because a release is not a toggle press: treating one as a press would fire the handler twice for a single physical key press, starting and immediately stopping the recording. Push-to-talk is the exception to that rule — its release is the event that ends the recording — and the section on the two interaction modes below explains what each kind of release does.

Handlers registered this way are owned by an `eventHandlers_` member of the addon, so they unregister when the addon is destroyed and cannot outlive the object that owns them. The trigger key itself is a `KeyListOption`, so the user can bind several combinations and can change them from `fcitx5-configtool` without restarting anything.

## Two interaction modes

One hotkey has two possible meanings, and the addon supports both: a key that toggles a recording on and off, and a key that records only while it is held. The wire protocol is unchanged between them. Push-to-talk is a `start` on the press and a `stop` or a `cancel` on the release, and all three commands already existed; the difference is entirely in how the addon reads key events.

**The toggle mode needs only presses.** A press starts a session, the next press ends it, and the addon keeps no more state than "a session is running". Presses are all it looks at, which is why the handler ignored releases for as long as this was the only mode.

**Push-to-talk needs the release event.** The duration of the clip is decided by the user's finger, and the only event that carries that information is the key-up. There is no timer the addon could run instead: the hold has no configured length, so the press can start a recording but nothing else can end it correctly. The handler therefore branches on `isRelease()` rather than returning early: a release of the push-to-talk key ends an active session, and a release of any other key is still ignored exactly as before.

**Auto-repeat has to be swallowed.** Both X11 and Wayland deliver a stream of press events while a key is held down, at the desktop's repeat rate. In toggle mode that is harmless, because a second press is a user action that means "stop". In push-to-talk it is destructive: every repeated press would arrive while a session is already active, and each one would send a fresh `start` with a new request id — dozens of times a second — restarting the recording under the user's finger and, in the worst case, having the daemon reject the extra starts as `busy`. So a press of the push-to-talk key that arrives while a session is already active is accepted and discarded: `filterAndAccept()` with no command sent. The event is still swallowed, because a held hotkey must not leak into the application as ordinary typing.

**A short tap becomes `cancel`, not `stop`.** Holding the key for a few tens of milliseconds is almost always a mis-keyed combination rather than speech. Sending `stop` would upload that fragment, ask the service to transcribe a fifth of a second of room tone, and commit whatever came back — possibly an empty string, possibly a hallucinated word — into the user's document. The addon therefore measures the hold against a 250 ms minimum and sends `cancel` when it falls short. `cancel` is the protocol's "discard this recording" command, so nothing is uploaded, nothing is committed, and the user sees nothing, which is the right outcome for an accident.

**Losing focus is turned into a release.** A key-up is delivered to whichever window has focus at that moment. If the user alt-tabs, clicks another window, or switches workspace mid-hold, the release goes somewhere the addon never sees, and the session would stay open until `max_duration_s` stopped it — several minutes of microphone with no way for the user to end it except another press. The addon therefore watches `InputContextFocusOut` and, when a push-to-talk session is active, treats the focus loss as the release it is never going to receive. The recording finishes with the same rule as a real release, so the audio captured up to that point is transcribed rather than thrown away.

**`cancel` is preferred whenever the daemon is not known to be recording.** On a release that passes the minimum hold, the addon does not send `stop` unconditionally: it sends `stop` only if the last `state` message the daemon broadcast said `recording`. If the state was anything else — the `start` failed outright, or the daemon already auto-stopped at `max_duration_s` — it sends `cancel` instead. The reason is that `cancel` is legal in every state, while `stop` against a daemon that is not recording produces a `busy` error. That error would arrive on top of the real failure the user has already been told about, and a second, misleading notification is worse than no second notification: it points at the wrong problem. Tracking the last broadcast state costs one member variable, set from every `state` message the handler already receives, and it is what makes that choice possible.

The two modes are also kept from interleaving. A press of the toggle key while a push-to-talk session is active is ignored — not filtered, just returned from — so a session cannot be started by one key and ended by the other, and the `stop`/`cancel` decision always belongs to the mode that opened the recording.

## Delivering the transcript

When the hotkey fires, the addon remembers the `InputContext` that had focus at that moment. It stores it as a `TrackableObjectReference<InputContext>` rather than a raw pointer, because transcription takes seconds and the user may close the window, switch workspaces, or quit the application while it runs — the input context can be destroyed before the answer arrives, and a raw pointer would then be dangling. A trackable reference knows when the object is gone.

When the `result` message arrives, the addon commits into the remembered context if it is still alive:

```cpp
if (inputContext) { inputContext->commitString(text); }
```

If it is not — the window closed, the application exited — the text goes to `instance_->mostRecentInputContext()` instead, which is the best available guess at where the user is now. This is a deliberate compromise: it can put the transcript in a window the user did not intend, but the alternative is silently discarding several seconds of speech. If the trailing-space option is enabled and the text is non-empty and does not already end in whitespace, one space is appended so that consecutive dictations do not run together. An empty transcript commits nothing at all.

Tips run through the fcitx5 notifications module, declared as an *optional* dependency: every call is guarded by `if (auto *n = notifications())`, so the addon still works on a desktop where that module is not installed, just without the "Listening…" and "Transcribing…" tips. Those tips are off unless *Show a tip while recording* is enabled, and failures do not travel this way at all — they go to `notify-send`, for the reasons set out under the notification policy below.

## The notification policy

The addon used to announce itself: a tip on *Listening…*, a tip on *Transcribing…*, a tip closing when the daemon went idle. That is now off by default, and the reasoning is that dictation is used in bursts while the user is typing. A window that appears and disappears for every utterance competes for attention with the text being produced, and it carries no information the user does not already have — they know they pressed the key, and they can see whether text arrived. So the default is quiet: nothing is shown while a recording is in progress, and the *Show a tip while recording* option exists only for users who want the old behaviour, in which case the existing tip helpers are used exactly as before, gated on that option instead of on the old notification option.

**A failed dictation is the one event that must reach the user.** Everything else in the addon's life is self-evident from the screen; a failure is not. The user has just spoken a sentence, released the key, and expects text. If nothing appears, the possibilities — the hotkey did not register, the daemon is not running, the API key is wrong, the microphone was muted, the transcript was empty — are indistinguishable from each other, and every one of them has a different fix. Silence here does not mean "nothing to report", it means the user is left guessing. That is why errors, and only errors, take a path that cannot be switched off by the tips option.

**`notify-send` rather than an fcitx5 tip.** Tips are delivered by fcitx5's notifications module, which the addon declares as an optional dependency precisely because it may not be installed or loaded. The case that matters most — something is broken — is also the case where the module is most likely to be unavailable or unhealthy, and a tip shown through a module that is not running is a tip nobody sees. So an error is handed to `notify-send`, a separate program that talks to the desktop's own notification service directly. The addon does not link libnotify and does not use its headers: it resolves the `notify-send` binary on `PATH` and executes it, which keeps the dependency budget at fcitx5 plus the standard library and means a missing libnotify is a missing program rather than a build failure.

The spawn is detached — a double fork with `setsid`, the same pattern used to start the daemon — with the three standard descriptors on `/dev/null`, so a notification daemon that hangs cannot block the fcitx5 event loop and the child cannot outlive its usefulness as a zombie. The addon never waits for the notification's exit status and never treats a failed delivery as an error of its own: a notification that does not appear is lost, not retried. The body is never written to a log, because it can carry an API error message and logs are shared more freely than screens.

**The fcitx5 tip is the fallback.** If `notify-send` is not on `PATH`, or the detached spawn fails, the error is shown as an fcitx5 tip instead — regardless of *Show a tip while recording*. That asymmetry is deliberate: the tips option is about how much the addon says while things work, and an error must never be dropped because the user asked for less chatter. The same rule makes the option ordering safe in every combination: desktop notification when the binary exists and *Notify on errors* is on, tip otherwise, and a warning in the log when both routes are unavailable.

## Reaching the daemon

The addon connects lazily, on the first hotkey press, and keeps the socket open afterwards. If the connect fails with `ENOENT` or `ECONNREFUSED` and autostart is enabled, it forks and execs the daemon command — detached with `setsid`, so the daemon survives an fcitx5 restart instead of dying with its parent — and then retries the connect every 200 ms for up to five seconds. The command that triggered all this is queued and flushed as soon as a connection succeeds, so the user's first press still starts a recording; if the retries run out, the pending command is dropped and a notification says the daemon is not reachable. At most one daemon is spawned per trigger, never one per retry.

Reads on the socket go through fcitx5's own event loop: `instance_->eventLoop().addIOEvent(fd, IOEventFlag::In, cb)` returns an event source owned by an addon member, and the callback reads whatever is available, appends it to a line buffer, and dispatches every complete line it finds. The socket is non-blocking, partial lines are kept until the newline arrives, and the buffer is capped at 1 MiB, matching the protocol's limit. A malformed line is logged and skipped — it never propagates as an exception into fcitx5's loop. After a disconnect the addon simply tries again on the next hotkey press.

A connection that has just succeeded is also sent a `reload`, so a daemon that has been running since long before the last configuration change picks that change up at the moment the addon first talks to it. That is the whole reason `reload` exists as a protocol command rather than the addon pushing values over the socket; the next section explains the reasoning.

## Configuration ownership

There are two processes and two kinds of setting, and the design rule is that each setting has exactly one file and that file has exactly one owner.

The daemon's settings — API key, model, mode, language, keyterms, audio device, sample rate, backend, socket path, maximum recording length — live in `~/.config/sarvam-dictation/config.toml`. The addon's own settings — trigger key, push-to-talk key, autostart, daemon command, the notification options, trailing space — live in `~/.config/fcitx5/conf/sarvamdictation.conf`, which is an ordinary `FCITX_CONFIGURATION` block and therefore an ordinary fcitx5 addon page. `fcitx5-configtool` shows both groups on the one *Sarvam Dictation* page, so the user sees a single screen, but the values are persisted to the two files the two processes actually read.

The obvious alternative was to let the page push values to the daemon over the socket, and it was rejected for four reasons:

* **One file, one owner.** The daemon reads `config.toml`; the page writes `config.toml`. There is no third store of daemon settings to keep in step, and no code path where a setting exists in memory but nowhere on disk.
* **No divergence after a restart.** A daemon started by systemd, or restarted after a crash, reads `config.toml` and nothing else. If the page had pushed values over the socket, a restart would silently revert the daemon to whatever the file said — settings that appeared to be applied and were not. Writing the file makes the running daemon and the next daemon agree by construction.
* **No second copy of the API key.** A socket-pushed configuration would either carry the key over the wire into the daemon's memory and a second persistence path, or need a separate mechanism for the one value that matters most. Keeping the key in the 0600 `config.toml` and nowhere else means there is exactly one file to protect and one file to audit.
* **The daemon keeps working standalone.** The daemon has no dependency on fcitx5, on the addon, or on the socket being up in order to be configured. A user who installed only `pip install ./daemon` and runs it as a service edits `config.toml` and sends `reload`; the same loader serves both worlds, unchanged.

### The flow

1. `fcitx5-configtool` edits the addon's `Configuration` object. Both groups of options are in it: the daemon options are ordinary fcitx5 options that happen to be persisted elsewhere.
2. When the addon loads its configuration, it fills the daemon options from `config.toml`. A missing file leaves them at their defaults and is not an error; a file that cannot be parsed leaves the options untouched and logs the reason.
3. On *Apply*, the addon writes `config.toml` from the daemon options: parent directories created, a temporary file in the same directory, `fsync`, rename, mode `0600`. The atomic write matters because a half-written config file is a file the daemon may read.
4. The addon then sends `reload` on the socket. The daemon re-reads the file and applies it to the next recording. A recording already in progress finishes with the settings it started with, so pressing *Apply* mid-dictation never truncates or corrupts a transcript. If the file the addon just wrote is rejected, the daemon answers `config_invalid` and keeps its previous settings rather than failing.

The order in that list is the point: the file is written first and the notification comes second. The file is the state; `reload` is only a hint that it changed. A daemon that missed the hint, or was not running when it was sent, is still correct, because the next `reload` — or the next daemon start — reads the same file.

### The TOML layer

`src/tomlfile.{h,cpp}` is a small reader and writer for the subset of TOML this project needs: sections, bare and quoted keys, basic and literal strings with the standard escapes, booleans, integers and single-line or multi-line arrays of strings. It includes nothing but the C++ standard library — no third-party TOML library, in keeping with the addon's dependency budget.

Two properties of it matter for configuration ownership. It preserves keys it does not manage: a `TomlDocument` is parsed from the existing file, the ten managed keys are set on it, and the document is serialised back, so a user's own comments-free extra keys, or a key a newer version understands and this one does not, survive an *Apply*. And it never fails to load: a line it cannot understand becomes a warning and is skipped, so a file written by a future version still opens in an older addon instead of being rejected wholesale.

### The value mapping

The addon does not hand-roll the conversion between TOML values and fcitx5 option values. It builds a `RawConfig` keyed by the option names and calls `config.load(raw, /*partial=*/true)` to read the file in, and `config.save(raw)` to get the values back out. Enums, booleans, integers and string lists are therefore marshalled by fcitx5's own rules rather than by a second implementation of them that could drift — an enum's stored name stays the same string the TOML file holds, and a `std::vector<std::string>` keeps being marshalled as indexed children (`Keyterms/0`, `Keyterms/1`, …) rather than a comma-joined string that would break on a keyterm containing a comma. Where the two formats genuinely differ — `True`/`False` in a `RawConfig` against `true`/`false` in TOML — the conversion is explicit and in one place.

## The daemon's audio pipeline

Recording is a sequence of small, individually boring steps, and the order matters.

Capture is a subprocess: `pw-record`, `parec` or `arecord`, chosen by `detect_backend` according to `[audio] backend`, defaulting to whichever is first on `PATH`. The daemon does not link an audio library; a subprocess is one less thing to build against, and all three tools write a WAV the rest of the pipeline can read.

Stopping the capture is the delicate part. A WAV header written at the start of a file contains sizes that are only correct once recording ends, so a capture process that is killed outright leaves a header claiming zero or nonsense length — the audio is all there and none of it can be read. The daemon therefore shuts the child down gracefully: `SIGINT` first, wait up to three seconds, then `SIGTERM`, wait two, and only then `SIGKILL`. Every one of the three tools finalises its header on a polite signal; none of them does on `SIGKILL`. `SIGKILL` is the last resort, not the first.

The finished file is then normalised to 16 kHz mono 16-bit PCM, which is what the service expects and what keeps the chunk arithmetic predictable. The duration is read back from the normalised file rather than estimated from the process's run time, so `duration_ms` in the result is a measurement.

If the recording is longer than 28 seconds, it is split into ordered chunks with the standard library's `wave` module, each chunk transcribed separately, and the pieces joined: each part stripped, empty parts dropped, and the rest joined with single spaces. 28 seconds rather than 30 leaves headroom under the REST endpoint's roughly 30-second limit, so a chunk that is a few milliseconds over is still accepted. `segments` in the result reports how many chunks were used. `max_duration_s` (default 300) is a separate safety net: it stops the recording automatically, so a session the user forgot about cannot run until the disk fills.

The transcription call itself uses the `sarvamai` SDK, imported inside the method that needs it. That keeps the daemon importable, and its tests runnable, on a machine where the SDK is not installed; and it means a missing SDK surfaces as a transcription error at the moment of use rather than as a startup failure. The API key is read from `[api] key` or `SARVAM_API_KEY` before any network call is attempted, so a missing key produces `no_api_key` immediately rather than after a failed request. The key never appears in an error message or a log line.

## Threading model

The daemon is one asyncio loop, one task per client connection, and one worker thread at a time. Everything that touches the socket is a coroutine on the loop, so message handling is naturally serialised and the state machine needs no locks. Everything that blocks — spawning the recorder, waiting for it to exit, normalising the WAV, splitting it, calling the API — runs inside `asyncio.to_thread`. That is what keeps the loop responsive while a transcription is in flight: `ping` is answered and `cancel` is accepted throughout, which is what the protocol promises.

Reading the config file on `reload` is small and does not go near the audio or the network, so it happens on the loop like any other message handler; the settings it produces are held for the next recording rather than swapped into a running one, which is what keeps a reload from racing the worker thread.

The consequence is that the daemon does one transcription at a time. That is intentional, not a limitation of the threading: one recording at a time is the protocol's rule, and serialising the API calls keeps a burst of dictation from exhausting rate limits.

## Failure modes

| Failure | Handling |
|---|---|
| Daemon not running | Addon autostarts it (if enabled), retries the connect 25 times at 200 ms, then notifies the user. |
| Daemon not found on the addon's socket path | Both sides derive the path from the same rule and read the same `[server] socket_path` from `config.toml`; a stale override left in an old fcitx5 conf is the usual cause, and the symptom is "daemon not reachable" while the daemon is visibly running. |
| Stale socket file after a daemon crash | The daemon unlinks a stale socket only after a connect attempt proves nobody is listening, so starting a second daemon cannot steal a live one's socket. |
| `config.toml` missing | Every setting falls back to its default, both at startup and on `reload`; the addon shows the defaults on the page. |
| `config.toml` unparseable or a value out of range | The daemon answers `reload` with `config_invalid` naming the key, keeps its previous settings and stays connected; the addon logs the reason and leaves its options untouched. |
| Config write fails (permissions, full disk) | `saveDaemonConfig` returns false with a reason, the addon reports it, and no `reload` is sent — the daemon keeps running on the settings it had. |
| No capture backend / wrong device | `no_audio_device` reaches the client, which shows a message pointing at the audio configuration. |
| Capture child dies or writes nothing | Zero-length or unreadable output becomes `audio_failed`; the file is discarded and the daemon returns to idle. |
| Owner disconnects mid-recording | The daemon cancels and discards the recording and returns to `idle`, so an abandoned session does not keep the microphone open. |
| Focus leaves the window mid-hold | Treated as the release that will never arrive, so the recording is finished and transcribed instead of holding the microphone open. |
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

`config.toml` is the store for the daemon's settings and the addon's configuration page is the editor for them. The page does not merge a hand edit made while fcitx5 is running: pressing *Apply* writes the keys it manages from the values currently on the page. A hand edit made outside `fcitx5-configtool` is therefore best followed by `sarvam-dictation-ctl reload` rather than by *Apply*.

## Fcitx 5 version compatibility

The addon targets the fcitx5 **5.1.x** series, which is what Debian 12, Ubuntu 24.04 and current Fedora releases ship, and it also builds against 5.2. Three places in the code exist only because of that target:

The hotkey is registered through the runtime-typed `Instance::watchEvent(EventType, EventWatcherPhase, EventHandler)` overload and the event is then cast to `KeyEvent`. The templated `watchEvent<EventType::InputContextKeyEvent>(phase, callback)` form only appeared in 5.2.

Key matching walks the configured `KeyList` and calls `Key::check` rather than `KeyEvent::checkKeyList` with `KeyEventMatchingMode::MatchAllNormalizedKeys`, which is also 5.2-only.

`fcitx/addonmanager.h` is included from the addon header because `FCITX_ADDON_DEPENDENCY_LOADER` expands to a call on `AddonManager`, so the type has to be complete there rather than merely forward declared.

Everything else — `EventSourceIO`, `addIOEvent`, `addTimeEvent`, `EventSourceTime::setTime`, `TrackableObjectReference`, `InputContext::commitString`, the notifications module's `showTip`, and the `FCITX_CONFIGURATION`/`RawConfig` marshalling the config layer relies on — is stable across both series. The addon is compiled warning-free under `-Wall -Wextra` with C++17.

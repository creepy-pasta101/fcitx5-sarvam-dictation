# fcitx5-sarvam-dictation

A dictation input method for Linux desktops. Press a hotkey anywhere — in a browser, a terminal, a chat window, an editor — speak in any of the 22 Indian languages Sarvam AI supports (or in English), press the hotkey again to stop, and the transcript is typed into whatever field currently has focus. If you would rather not press twice, a second hotkey works as push-to-talk: hold it down for as long as you speak and the release ends the recording. There is no separate dictation window and no copy-paste step: the text arrives as if you had typed it. The project is two pieces that work together: a small C++ addon for **Fcitx 5**, the input method framework, which owns the hotkey, the desktop notifications and the text insertion, and a Python daemon that owns audio capture and calls **Sarvam AI's Saaras speech-to-text model** over HTTPS — or, when streaming is turned on, over a WebSocket that returns text while you are still speaking. The addon never speaks HTTP and the daemon never touches fcitx5.

## How it works

```
  [ any app, focused ]  <-- commits text --  [ fcitx5 addon (C++, in-process) ]
                                                      |  Unix socket, newline-delimited JSON
                                                      v
                                          [ Python daemon (sarvamai SDK) ]
                                                      |
                                        audio capture (pw-record/parec/arecord)
                                                      |
                                        HTTPS -> api.sarvam.ai /speech-to-text
                                        WSS   -> api.sarvam.ai /speech-to-text-realtime/ws
```

The split exists because the addon lives inside fcitx5's event loop: any blocking call in it stalls keyboard input for every application on the desktop, so it must only do non-blocking socket reads and a string commit. The daemon, which is free to block, owns the messy parts — spawning and reaping an audio capture process, rewriting WAV headers, chunking long recordings, and waiting on a network request that can take seconds.

The wire protocol between the two is a frozen contract and is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md). The reasoning behind the design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

You need Fcitx 5 installed and running, a C++17 compiler with CMake 3.19 or newer, and the fcitx5 development headers so the addon can be built. On Debian and Ubuntu the headers come from `libfcitx5core-dev`, `libfcitx5config-dev`, `libfcitx5utils-dev` and `fcitx5-modules-dev`; on Fedora it is `fcitx5-devel`; on Arch the headers ship with the `fcitx5` package. Distribution package names vary between releases, so if one of these does not exist on your system, search your package manager for `fcitx5` development files — the build only needs `Fcitx5Core` and the `Notifications` module of `Fcitx5Module`.

The daemon needs Python 3.10 or newer. It uses only the standard library, plus the official `sarvamai` SDK, which it imports lazily so that the daemon starts and its test suite runs even when the SDK is not installed. For audio you need PipeWire, PulseAudio or ALSA with one of `pw-record`, `parec` or `arecord` on your `PATH`; the daemon picks whichever it finds first. Finally you need a Sarvam AI API key, which you can create at [dashboard.sarvam.ai](https://dashboard.sarvam.ai). Dictation over the REST endpoint works with any valid key; the optional streaming mode additionally needs a subscription that includes realtime access.

## Install

Clone the repository and run the installer from the top of the checkout:

```bash
git clone https://github.com/your-account/fcitx5-sarvam-dictation.git
cd fcitx5-sarvam-dictation
bash scripts/install.sh
```

The script builds the addon, installs it into the fcitx5 addon directory, and installs the daemon and the `sarvam-dictation-ctl` helper. Run it with `--help` to see the flags it accepts.

Next, register the addon. Open `fcitx5-configtool`, go to the *Addons* tab, find **Sarvam Dictation** in the list and enable it. Then restart fcitx5 so the addon is loaded:

```bash
fcitx5 -rd
```

If you would rather build it yourself, the manual path is plain CMake. The install prefix must match the prefix fcitx5 itself was installed under, which is `/usr` on nearly every distribution:

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr
cmake --build build -j"$(nproc)"
sudo cmake --install build
```

The Python daemon can also be installed on its own, without the C++ side, from the top of the checkout:

```bash
pip install ./daemon
```

That gives you the `sarvam-dictation-daemon` console script. The control helper is a plain script in the checkout at `scripts/sarvam-dictation-ctl`; run it from there, or copy it to somewhere on your `PATH`. If your environment did not pull in the speech SDK as a dependency, install it with `pip install sarvamai`; without it the daemon starts and answers `ping`, but any transcription attempt fails with a `no_api_key` or `internal` error rather than a transcript.

To keep the daemon running as a user service instead of letting the addon start it on demand, install the unit shipped in `packaging/`:

```bash
mkdir -p ~/.config/systemd/user
cp packaging/sarvam-dictation.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sarvam-dictation
```

The addon starts the daemon by itself when it cannot reach one, but a service gives you a stable process with logs in the journal, which is easier to debug. If you run the service, you can turn the addon's *Start the daemon automatically* option off.

### Packages

On Fedora and openSUSE the project also builds an RPM from the same source tree. Configure with the system prefix: a package build must use `-DCMAKE_INSTALL_PREFIX=/usr`, because fcitx5 looks for addons only in its own prefix, and an addon installed anywhere else is simply not found. Then build and run CPack:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr
cmake --build build
(cd build && cpack -G RPM)
```

The file that produces is named `fcitx5-sarvam-dictation-0.1.0-1.<arch>.rpm`, for example `fcitx5-sarvam-dictation-0.1.0-1.x86_64.rpm`. CI builds the same package on every push and publishes it as a build artifact on the workflow run, so if you would rather not build it yourself you can download it from there.

Install it with `dnf`, which pulls in the `fcitx5` and `python3` dependencies the package declares:

```bash
sudo dnf install ./fcitx5-sarvam-dictation-*.rpm
```

The package installs both halves of the project, because neither is useful without the other: the addon library and its registration file under fcitx5's own directories, the `sarvam_dictation` Python package, the `sarvam-dictation-daemon` launcher the addon starts when it needs a daemon, the `sarvam-dictation-ctl` helper, and the systemd user unit at `/usr/lib/systemd/user/sarvam-dictation.service`. That unit is already in place after the install, so there is nothing to copy into your home directory; enable it directly:

```bash
systemctl --user daemon-reload
systemctl --user enable --now sarvam-dictation
```

What the package deliberately does not do is install the `sarvamai` Python SDK. That SDK is distributed on PyPI only and is not in Fedora's or openSUSE's repositories, so a hard dependency on it would make the package uninstallable. Install it yourself, for the interpreter the daemon runs under:

```bash
pip install sarvamai
```

Until you do, the daemon starts and answers `ping`, but a transcription attempt fails instead of returning a transcript. This is also why the API key setup step is unchanged: the key still goes into `~/.config/sarvam-dictation/config.toml`, which the configuration page writes for you, or into `SARVAM_API_KEY`. A package install changes where the binaries come from, not how they are configured. Once the package is installed, register the addon in `fcitx5-configtool` and restart fcitx5 exactly as described above.

## Configure

Every setting is edited in `fcitx5-configtool`, on the **Sarvam Dictation** addon page: open `fcitx5-configtool`, go to the *Addons* tab, select **Sarvam Dictation** and click *Configure*. The page holds the addon's own settings and the daemon's settings side by side, so you do not have to edit any file by hand.

The page writes two files, because the two processes own different things:

* `~/.config/sarvam-dictation/config.toml` (or `$XDG_CONFIG_HOME/sarvam-dictation/config.toml` when that variable is set) holds the API, audio and server settings — everything the daemon reads. This file holds your API key and is written with mode `0600`, so only you can read it.
* `~/.config/fcitx5/conf/sarvamdictation.conf` holds the addon-only settings: the trigger key, the push-to-talk key, the daemon command, the notification and indicator options and the trailing space. The daemon has no interest in any of these, so they never enter `config.toml`.

When you press *Apply*, the addon writes `config.toml` from the values on the page, then sends a `reload` command to the daemon over the socket. A running daemon re-reads the file and applies the new settings to the next recording — no restart of either process is needed.

### Daemon settings

These are the settings the daemon reads from `~/.config/sarvam-dictation/config.toml`. Each one appears on the configuration page with the label in the first column.

| Setting | `config.toml` key | Default | Meaning |
|---|---|---|---|
| Sarvam API key | `[api] key` | empty | Your Sarvam AI key. Empty means the daemon falls back to the `SARVAM_API_KEY` environment variable. |
| Model | `[api] model` | `saaras:v4` | Speech-to-text model: `saaras:v4` (recommended), `saaras:v3`, or `saaras:v3-realtime`, which transcribes as you speak and has no REST equivalent. Picking `saaras:v3-realtime` turns streaming on by itself, and `config.toml` will show `streaming = true` after *Apply*, whatever the *Streaming* toggle says. |
| Streaming | `[api] streaming` | `false` | Send audio to the realtime endpoint while you speak and show the partial text as it arrives, instead of transcribing the finished clip. Off by default. Needs a model that supports it (`saaras:v3-realtime` or `saaras:v4`) and a subscription that includes realtime access. |
| Output mode | `[api] mode` | `transcribe` | What Saaras returns: `transcribe`, `translate`, `verbatim`, `translit` or `codemix`. |
| Language | `[api] language_code` | empty | Spoken language. Empty means auto-detect, which is what the config page's *Auto-detect* entry writes; otherwise a code such as `hi-IN`, `bn-IN` or `ta-IN`. A hand-written `auto` is accepted too and treated as empty. |
| Keyterms | `[api] keyterms` | `[]` | List of words and phrases to bias the model towards. `saaras:v4` only, at most 50 entries. |
| Audio device | `[audio] device` | empty | Capture source, picked from a dropdown of the devices the daemon found. Empty means the system default source. |
| Sample rate | `[audio] sample_rate` | `16000` | Recording sample rate: `16000` Hz (recommended) or `8000` Hz (telephony). |
| Capture backend | `[audio] backend` | `auto` | Which capture program to use: `auto`, `pipewire` (`pw-record`), `pulse` (`parec`) or `alsa` (`arecord`). |
| Socket path | `[server] socket_path` | empty | Path to the daemon socket. Empty means the default rule (`$XDG_RUNTIME_DIR/sarvam-dictation.sock`, else `/tmp/sarvam-dictation-<uid>.sock`). |
| Max recording length (seconds) | `[server] max_duration_s` | `300` | Auto-stop and transcribe at this length. Accepts 5 to 3600. |

### Choosing an audio device

The *Audio device* setting is a dropdown rather than a text field, so a non-default microphone can be chosen without knowing a PulseAudio source name or an ALSA PCM string. The list holds the devices the daemon found on this machine. *System default (follow the desktop)* comes first: that entry stores the empty string, which is the default and means whatever the desktop is currently using. Below it comes one entry per device, labelled with a name you can recognise and storing the exact string the capture tool wants. Which devices can appear follows the capture backend the daemon resolves: PulseAudio and PipeWire sources when `pactl` is available, ALSA capture devices when it falls back to `arecord`.

Sources whose names end in `.monitor` are labelled as the monitor of an output, because recording from one captures what you play rather than what you say. They are listed rather than hidden — capturing the desktop's own audio is occasionally what someone wants — but the label says plainly that the entry is not a microphone.

A device that is no longer present is not silently dropped. When `config.toml` names a device the enumeration did not return, the page adds an entry for it, labelled with that value followed by *(not currently available)*, so the setting you chose stays visible for what it is — a device that is absent right now — instead of disappearing from the list and being replaced the next time you press *Apply*. The stored value stays a plain string, so hand-editing `[audio] device` in `config.toml` still works.

The same list is available to scripts. `sarvam-dictation-daemon --list-audio-devices` prints it to standard output, one device per line, as the device id, a tab, then the label, and exits 0 even when it found no devices. It does not read `config.toml`, does not check for an API key and does not start the daemon, so it works on a machine that has never been configured and inside an install script.

One caveat is worth knowing: the list is built when the configuration page opens. A headset plugged in while the page is already open does not appear in it; close the page and open it again, and the new device is there.

### Model namespaces

The three endpoints the SDK can reach do not share a model list. Each one has its own namespace, and a name that is valid on one is not valid on the others.

| SDK client | Transport | Accepted models |
|---|---|---|
| `client.speech_to_text` | REST `POST /speech-to-text` | `saarika:v2.5`, `saaras:v3`, `saaras:v4` |
| `client.speech_to_text_streaming` | WebSocket `/speech-to-text/ws` (legacy) | `saaras:v3`, `saaras:v4` |
| `client.speech_to_text_realtime_streaming` | WebSocket `/speech-to-text-realtime/ws` | `saaras:v3-realtime`, `saaras:v4` |

The SDK exposes these as three separate clients, with three separate model lists, and the lists are not interchangeable. `saaras:v3-realtime` exists only on the realtime endpoint and is not a REST model at all, so a request that carries it to `POST /speech-to-text` is refused by the provider with a 400. `saaras:v4` is the one model that works everywhere: it is the only name all three endpoints accept, which is why it is the default. The daemon uses two of the three clients — the REST client for the ordinary path, the realtime client when streaming is on — and checks the configured model against the list of the endpoint it is about to use before any request is made.

The configuration page offers the three Saaras models. `saarika:v2.5` is accepted by the REST endpoint but is not one of the choices on the page.

### Addon settings

These live in `~/.config/fcitx5/conf/sarvamdictation.conf` and are shown on the same page.

| Setting | Default | Meaning |
|---|---|---|
| Trigger key | `Control+Alt+d` | Key combination that starts and stops dictation. Several combinations may be bound. |
| Push-to-talk key | `Control+Alt+space` | Key combination held down to record; releasing it stops and transcribes. Several combinations may be bound. |
| Start the daemon automatically | `true` | Start the daemon command when the daemon cannot be reached. |
| Daemon command | `sarvam-dictation-daemon` | Program the addon launches when autostarting. |
| Notify on errors | `true` | Deliver a failure as a desktop notification through `notify-send`, with an fcitx5 tip as the fallback. |
| Show a tip while recording | `false` | Show "Listening…" and "Transcribing…" tips while dictating. Off by default. Not used on KDE Plasma, where the on-screen display reports recording state instead. |
| Show an on-screen display on KDE Plasma | `true` | Show recording start and stop as a Plasma on-screen display instead of an fcitx5 tip. KDE Plasma only; on every other desktop the option has no effect. |
| Show the transcript in the indicator | `true` | While streaming, show the partial transcript in the recording indicator as it is recognised. Off keeps the *Listening* and *Transcribing* states but drops the text itself. |
| Show the transcript in the text field while streaming | `true` | While streaming, compose the partial transcript into the focused text field as IME preedit, so the words appear where they will be typed and are replaced by the final transcript when the recording ends. With this on the indicator shows only *Listening* and *Transcribing*. Off restores the text in the indicator, subject to *Show the transcript in the indicator*. |
| Copy the transcript to the clipboard when nothing is focused to type into | `true` | When the transcript arrives and no input context has focus, put it on the clipboard and say so through the recording indicator instead of dropping it. See the clipboard paragraphs under Usage. |
| Always copy the transcript to the clipboard as well | `false` | Copy every transcript to the clipboard before typing it, for applications that accept committed text and silently discard it. Off by default. |
| Append a trailing space | `true` | Append a space after the committed transcript, so consecutive dictations do not run together. |

The socket path used to be an addon setting; it is now `[server] socket_path` in `config.toml`. Both processes read it from there, so there is only one place to set it and the two sides cannot disagree.

### The config file

The file the page writes looks like this. A missing file is not an error — every setting falls back to its default.

```toml
# Written by fcitx5-configtool via the fcitx5-sarvam-dictation addon.
# The dictation daemon reads this file; edit it here or in fcitx5-configtool.

[api]
key = "YOUR_SARVAM_API_KEY"
model = "saaras:v4"
mode = "transcribe"
language_code = ""          # empty = auto-detect
keyterms = []
streaming = false

[audio]
device = ""
sample_rate = 16000
backend = "auto"

[server]
socket_path = ""
max_duration_s = 300
```

### Hand-editing and precedence

Hand-editing `config.toml` still works, and it is what standalone and systemd users do: the daemon reads the file at startup and on every `reload`, and it does not care who wrote it. Any key in the file that the addon does not manage — your own comments and extra keys — is preserved when the addon saves, so a hand edit outside the eleven settings above survives.

The precedence rule is worth being plain about: pressing *Apply* in `fcitx5-configtool` rewrites the keys the page manages, using the values currently on the page. The page fills itself from `config.toml` when the addon loads its configuration, so a hand edit made while fcitx5 is running is not reflected on the page and is overwritten by the next *Apply*. If you edit the file by hand while fcitx5 is running, make the edit in `fcitx5-configtool` as well, or reload and check the page before you press *Apply*.

After a hand edit, tell the daemon to pick it up:

```bash
sarvam-dictation-ctl reload
```

The same command is useful when you want to confirm that the daemon accepts the file: it answers with a `reloaded` message, or with an error naming the offending key.

### The API key

The key is stored in plain text in `config.toml`, which is why that file is mode `0600` and why the key never goes into `~/.config/fcitx5/conf/sarvamdictation.conf` — a second copy in a second file would be one more thing to leak and one more thing to keep in step. Keep the key out of version control: the file lives in your home directory, and if you keep dotfiles in git, add `sarvam-dictation/config.toml` to `.gitignore` rather than committing a real key. Never put a key in a repository, an issue, or a screenshot; in anything you share, use the placeholder `YOUR_SARVAM_API_KEY`.

The key can also come from the `SARVAM_API_KEY` environment variable, which fills in only when the file's value is empty; an explicit key in the file is never shadowed by the environment.

On the configuration page the key field does not show your key. When a key is stored the field shows `********` — eight asterisks — and when none is stored it is empty; the key itself stays in `config.toml` and is never put on the page. The field is write-only, and it behaves in the three ways that matter. Typing a new key into it replaces the stored one when you press *Apply*. Leaving `********` alone keeps the stored key exactly as it was, so opening the page and pressing *Apply* to change some other setting does not wipe your key. Clearing the field removes the key, after which the daemon falls back to `SARVAM_API_KEY` if that variable is set and otherwise reports `no_api_key` on the next recording.

It is worth being plain about why the field works this way. `fcitx5-configtool` renders a string option as a plain `QLineEdit`, and it has no password widget at all: the option hints it understands are `Enum`, `EnumI18n`, `IntMin`, `IntMax` and `ListConstrain`, and none of them makes a text field echo dots instead of characters. A genuinely masked input therefore cannot be obtained from the addon side. What the addon does instead is keep the real key in `config.toml`, store the fixed sentinel `********` in the option so the page shows that rather than the key, and put the real value back when you save. The sentinel is never written to `config.toml`. The cost is that the field cannot reveal what is stored, only replace it: a key you have forgotten has to be replaced rather than looked up, and the field looks populated whenever a key exists even though it tells you nothing about which one.

## Usage

There are two ways to dictate, and they use two different keys.

The toggle key starts and stops. Press `Control+Alt+d` (or whatever you bound to the trigger key) and the daemon starts recording from your default source; press the same hotkey again and the recording is finalised and sent to Sarvam. This is the mode to use for a long thought, because you do not have to keep a finger on the keyboard.

The push-to-talk key records while it is held. Press `Control+Alt+space` (or whatever you bound to the push-to-talk key), keep it down and speak; release it and the recording ends and is transcribed. Nothing has to be pressed twice: the release of the key is what stops the recording, so the length of the hold is the length of the clip. A hold has to last at least 250 ms. A shorter tap is discarded rather than uploaded — the recording is cancelled, nothing is transcribed and nothing is committed — so a mistyped key combination cannot paste a fraction of a second of silence into your document. Losing window focus also ends a push-to-talk recording: if you switch windows while holding the key, the key-up goes to the other window, so the addon finishes the recording itself instead of leaving the microphone open.

Either way, the recording is sent to Sarvam, and when the answer comes back the text is committed into the field that had focus when you started, followed by a space if *Append a trailing space* is on.

Typing is not always possible when the answer comes back. You may have switched windows while it was being transcribed, the field you started in may be gone because the window was closed, or the window you are now in may have no input field at all. In that case the transcript is not lost: it goes to the clipboard, and the recording indicator says *Copied to clipboard* with a line explaining that nothing was focused to type into. Paste it wherever you want it. This is what *Copy the transcript to the clipboard when nothing is focused to type into* controls, and it is on by default. With the option off, the addon still tries the field it remembers — an unfocused field may accept a commit — and falls back to the clipboard when there is nothing at all to type into, so a transcript is never dropped silently in either setting.

The clipboard is written with whatever the system provides, and nothing needs to be set up: `wl-copy` on Wayland, `xclip` or `xsel` on X11, and Klipper over D-Bus, which every Plasma installation has even when no clipboard utility is installed. The addon tries them in that order and uses the first one present. The transcript is copied exactly as the model returned it, without the trailing space: the space exists to separate consecutive dictations that are typed, and a space at the end of a paste is only noise. If no clipboard tool can be found at all, the copy fails and you get an error notification saying so, because at that point the text really is lost.

There is one case the addon cannot see. An application can hold a focused input context and still discard whatever is committed into it — a browser page with no field focused is the usual example, and a file manager behaves the same way. Fcitx 5 gives an addon no way to tell whether a focused window will actually accept typed text, so this cannot be detected, and without help the transcript disappears exactly as it did before. *Always copy the transcript to the clipboard as well* covers it: with that option on, every transcript is put on the clipboard just before it is typed, so there is always something to paste whether the application keeps the text or throws it away. It is off by default, because it puts every dictation on the clipboard whether or not you wanted it there.

By default nothing is shown on screen while you dictate, with one exception: on **KDE Plasma**, starting a recording and stopping it each show a small on-screen display — the same centred indicator Plasma itself uses for volume and brightness — so you can see at a glance that the microphone is open and that it has closed again. It is delivered through Plasma's own on-screen display service, so there is nothing to configure and nothing extra to install, and it is on out of the box; turn off *Show an on-screen display on KDE Plasma* if you would rather not have it. That indicator is Plasma-only. On GNOME, XFCE, sway or any other desktop it does not exist, and nothing is shown unless you turn on *Show a tip while recording*. On Plasma there is also no *Listening…* or *Transcribing…* fcitx5 tip: the on-screen display takes its place, so the tip option is not used for recording state there. Failures are the exception and are never silent: when the daemon cannot be reached, when the API rejects the key, or when the recording produced no audio, you get a desktop notification delivered by `notify-send`, so the message is seen even when the fcitx5 notification daemon is not running. `notify-send` comes from the `libnotify-bin` package on Debian and Ubuntu, or from `libnotify` on Fedora and openSUSE; if it is not installed, the addon falls back to an fcitx5 notification tip instead. Errors are reported regardless of *Show a tip while recording* and regardless of the on-screen display option, and they are never shown as an on-screen display: an error has to be readable and stay on screen until you dismiss it, which is what a notification does and a small centred indicator does not. Turning off *Notify on errors* leaves them in the log only.

The output mode chooses what Saaras does with your speech. `transcribe` is the everyday choice: speech in, text in the same language's own script. `translate` returns the transcript in English, which is useful when you speak an Indian language but need English text. `verbatim` keeps the disfluencies and false starts rather than tidying them away, which is what you want for interview or meeting notes where the exact wording matters. `translit` returns romanised text instead of native script, useful for names, addresses and anything you will read back over a phone line. `codemix` is for speech that mixes languages inside a sentence, such as Hindi with English technical words, and preserves that mixture instead of forcing one language. Leave the language on *Auto-detect* to let the service detect it; set a code (for example `hi-IN`) when you know it, since detection on a short utterance is the most common source of a wrong-language transcript.

Streaming is the other way to get the same text. With *Streaming* on — the `[api] streaming` key, off by default — the daemon sends your audio to Sarvam's realtime WebSocket endpoint while you are still speaking, and the partial transcript arrives as it is recognised, in the text field itself. That is what *Show the transcript in the text field while streaming* does, and it is on by default: the words so far are composed into the field as IME preedit, so you see them where they will be typed and can tell at a glance that the microphone is open and roughly what has been heard. It is the same dictation, delivered sooner, and it is most useful with push-to-talk: you hold the key, watch the words appear, and let go when the sentence is done. The toggle key works with streaming too, but there is less to watch while you think, so the gain is smaller.

The text in the field is composition text, not typed text. It is provisional, it is underlined wherever the application renders its own preedit, and it is replaced by the final transcript when the recording ends, so nothing is typed into the document until it is final. An error, a cancelled recording or a lost window takes the composition away rather than leaving a half-heard sentence in the field. Because the field already carries the words, the recording indicator shows only *Listening* and *Transcribing* while this is on, and no transcript of its own. A client that does not support preedit gets the text in fcitx5's own composing window instead of inline in the field: it is still composition, still replaced by the final transcript, just drawn by fcitx5 rather than by the application. Losing focus cancels the composition, so switching windows mid-recording does not leave the field in composition state.

Turning *Show the transcript in the text field while streaming* off restores the earlier behaviour: the partial transcript appears in the recording indicator instead, subject to *Show the transcript in the indicator*, and the field is left alone until the final transcript is committed. The indicator is refreshed a few times a second rather than on every event the service sends, which is faster than anyone reads and keeps it steady instead of flickering, and what it shows is always the newest partial; a partial long enough to overflow the small centred indicator shows its most recent words rather than its opening ones, since those are the words being spoken. Switching the option off while a recording is running clears what is already composed in the field, so the last partial does not stay behind until you release the key.

The partial text in the indicator has a switch of its own. *Show the transcript in the indicator*, on by default, decides whether that text is shown there at all — which is the switch that matters once *Show the transcript in the text field while streaming* is off, since with it on the field carries the words and the indicator carries only the state. With *Show the transcript in the indicator* off, the indicator still opens on the key press and still reports *Listening* and *Transcribing*, it simply carries no transcript. That is what to reach for when the text moving under your eyes pulls your attention away from what you are writing, or when the screen is being shared and you would rather not have your words on it. Turning it off while a recording is running clears the text that is already showing, so the last partial does not stay up until you release the key.

Two conditions come with it. Streaming needs a subscription that includes realtime access, so a key that transcribes fine over REST can still be refused by the realtime endpoint. It also needs a model that supports it: `saaras:v3-realtime` or `saaras:v4`. Choosing `saaras:v3-realtime` turns streaming on by itself, because that model has no REST equivalent, and `config.toml` will show `streaming = true` after you press *Apply*: the addon writes the state the daemon will actually be in rather than the position of the *Streaming* toggle, and it turns the toggle on when it loads such a file, so the page and the file agree. Asking for streaming with a model outside those two is a configuration error, and the daemon names both the option and the model when it rejects the file. Otherwise the REST path stays the default: with *Streaming* off, which is how the tool ships, nothing about the behaviour described here changes.

The REST endpoint accepts audio up to about 30 seconds per request, so the daemon splits anything longer into 28-second chunks, transcribes them in order and joins the pieces with single spaces. You do not have to think about this: hold the hotkey session open for a two-minute thought and it works. The max recording length setting (default 300 seconds) stops the recording automatically at that length so a forgotten session cannot run forever.

To check the daemon without involving fcitx5 at all, record from a terminal:

```bash
sarvam-dictation-ctl record out.wav
```

Speak, press Enter, and you get a WAV file plus the transcript the daemon produced. This is the fastest way to tell whether a problem is in the audio path, in the API call, or in the addon. From a checkout that has not been installed, the same command is `./scripts/sarvam-dictation-ctl record out.wav`.

The helper speaks the same protocol as the addon and has one command per client message: `status`, `start`, `stop`, `toggle`, `cancel`, `reload` and `record`. `sarvam-dictation-ctl reload` asks a running daemon to re-read `config.toml` and apply it to the next recording, which is what you want after editing the file outside `fcitx5-configtool`.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| "Sarvam dictation: daemon not reachable" | The addon could not connect and gave up after retrying for five seconds. Start the daemon in the foreground (`sarvam-dictation-daemon --log-level debug`) and watch its output. If it starts fine, the two sides disagree about the socket path: check `[server] socket_path` on the addon's configuration page, which is the one place both processes read it from. |
| `no_audio_device` error | No capture program was found, or the configured device does not exist. Install PipeWire or PulseAudio utilities so `pw-record` or `parec` is on `PATH`, or pick a real source from the *Audio device* dropdown. `sarvam-dictation-daemon --check` reports which backend it resolved. |
| A device I just plugged in is missing from the *Audio device* dropdown | The list is built when the configuration page opens, so a device attached while the page is open is not in it. Close the page and open it again. A device that was configured and has since been unplugged is still listed, labelled with its value followed by *(not currently available)*, and keeps its value. |
| `audio_failed` error | The recorder produced zero-length or unreadable audio — usually a wrong audio device, or the capture process died immediately. Confirm the source works with `pw-record`/`parec`/`arecord` on the command line first. |
| `401` or `api` error | The API key is missing, wrong, or out of quota. Check the Sarvam API key field on the configuration page (that is `[api] key` in `~/.config/sarvam-dictation/config.toml`) or `SARVAM_API_KEY`, and that the key is active on the dashboard. A key that was pasted with surrounding whitespace counts as wrong. |
| Streaming fails while the REST path works | The realtime endpoint is a different service and needs a subscription that includes realtime access, so a key that transcribes over REST can still be refused there; the error code that comes back is `api`. Check the subscription on the dashboard, or turn *Streaming* off and keep using the REST path. A model other than `saaras:v3-realtime` or `saaras:v4` cannot stream at all, and `config_invalid` names both the option and the model if you try it. |
| `400` saying `Invalid model 'saaras:v3-realtime'` | The daemon in use is older than the streaming support, or was installed separately from the addon, so it sends the realtime model to the REST endpoint, which never accepts it. Reinstall so that both halves match, and run `sarvam-dictation-daemon --check`, which now prints the endpoint that will be used and flags a model that does not belong to it. |
| `no_api_key` error | Neither the config file nor the environment has a key. `sarvam-dictation-daemon --check` states plainly whether a key is present. |
| `config_invalid` error | The daemon rejected the config file and kept its previous settings, so dictation still works. The message names the offending key; fix that value in `fcitx5-configtool` and press *Apply*, or correct the file and run `sarvam-dictation-ctl reload`. The same code is returned at recording time when the chosen model cannot be used with the endpoint the daemon is about to use; the message then names the model and says what to change. |
| Settings on the page revert after you edit the file by hand | The page is filled from `config.toml` when fcitx5 loads the addon, and *Apply* rewrites the keys it manages. Make the change on the page, or reload fcitx5 before pressing *Apply*. |
| Empty transcript, nothing inserted | The daemon transcribed silence or noise and returned an empty string, which is a legal result and commits nothing. Check the microphone is not muted and that the audio device points at it; raise your input level and try a longer utterance. |
| Text is transcribed but does not appear in the application | The application may not be using fcitx5 at all. Electron and some GTK applications only get text committed through fcitx5's own frontend: make sure the app is running under Wayland with `GTK_IM_MODULE=fcitx`/`QT_IM_MODULE=fcitx`, or under X11 with `XMODIFIERS=@im=fcitx`, and that fcitx5's Wayland or X11 frontend is the one the app connects to. Also check whether the window that had focus was closed while transcribing — the transcript then goes to the clipboard and the recording indicator says so, rather than being typed into another window. |
| The transcript went to the clipboard instead of my window | Nothing was focused to type into when the transcript came back: you had switched windows, the field had gone, or the window you were in has no input field. That is the fallback doing its job — paste with your usual shortcut. If it happens in an application where a text field really was focused, the application is accepting committed text and discarding it, which the addon cannot detect: turn on *Always copy the transcript to the clipboard as well*. If nothing at all arrives on the clipboard, no clipboard tool was found — install `wl-clipboard` on Wayland, or `xclip` or `xsel` on X11; Klipper covers a Plasma install on its own. |
| Hotkey does nothing, or does the wrong thing | Another binding owns `Control+Alt+d` or `Control+Alt+space`. Change the trigger key or the push-to-talk key in the addon's configuration page, or free the key in your desktop's keyboard settings. Bindings grabbed by the compositor never reach fcitx5. |
| A short push-to-talk tap records nothing | A hold shorter than 250 ms is treated as an accidental tap and the recording is cancelled, so nothing is transcribed. Hold the key for longer. Losing window focus during the hold also ends the recording early. |
| Need to see what happened | Daemon logs go to the journal when it runs as a service: `journalctl --user -u sarvam-dictation -f`. For a foreground daemon, raise the level with `sarvam-dictation-daemon --log-level debug`. For the addon, restart fcitx5 in verbose mode with `fcitx5 -rd --verbose`. |

`sarvam-dictation-daemon --check` is the single most useful diagnostic: it validates the config file, reports the resolved audio backend and device, says whether an API key is present, prints the endpoint the daemon will use, and exits non-zero if something is wrong — including a model that does not belong to that endpoint. `sarvam-dictation-daemon --list-audio-devices` is its companion for the audio side: it prints the devices the daemon can see, in the same list the configuration page offers, and needs no configuration of its own.

## Development

Build with the CMake commands in the install section; there is no separate development configuration. The Python test suite runs from the top of the checkout and needs no API key and no audio device:

```bash
pytest -q tests
```

The protocol lives in two places, and they must stay in step: `daemon/sarvam_dictation/protocol.py` on the Python side and `src/sarvamdictation.cpp` on the C++ side. [docs/PROTOCOL.md](docs/PROTOCOL.md) describes the same contract in prose, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains why the pieces are arranged the way they are.

The config file is read and written by a dependency-free TOML subset reader in `src/tomlfile.cpp`, which preserves keys it does not manage. The addon's option definitions are in `src/sarvamdictationconfig.cpp`: that is where a new setting is declared, and where the mapping between an fcitx5 option and its `config.toml` key is kept.

## Privacy

Your audio is uploaded to Sarvam AI's API to be transcribed. That is the whole point of the tool, and it means the audio leaves your machine and is handled under Sarvam's terms. Nothing else is retained: the daemon writes a temporary WAV in a temporary directory, transcribes it, and deletes it. With streaming on there is no temporary WAV at all — the audio is sent as it is captured, to the same service, and nothing is written to disk. There is no history, no database, no analytics, and no storage of transcripts by this project. The API key sits in one file, `~/.config/sarvam-dictation/config.toml`, readable only by you. If you would rather not send a given piece of audio to a remote service, do not dictate it.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Suhan G Paradkar.

This project is not affiliated with, endorsed by, or supported by Sarvam AI or the Fcitx project. Sarvam and Saaras are products of Sarvam AI; Fcitx 5 is a project of the Fcitx community. Issues with this addon belong in this repository, not in theirs.

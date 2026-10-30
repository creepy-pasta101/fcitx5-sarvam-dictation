# fcitx5-sarvam-dictation

A dictation input method for Linux desktops. Press a hotkey anywhere — in a browser, a terminal, a chat window, an editor — speak in any of the 22 Indian languages Sarvam AI supports (or in English), press the hotkey again, and the transcript is typed into whatever field currently has focus. There is no separate dictation window and no copy-paste step: the text arrives as if you had typed it. The project is two pieces that work together: a small C++ addon for **Fcitx 5**, the input method framework, which owns the hotkey, the desktop notifications and the text insertion, and a Python daemon that owns audio capture and calls **Sarvam AI's Saaras speech-to-text model** over HTTPS. The addon never speaks HTTP and the daemon never touches fcitx5.

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
```

The split exists because the addon lives inside fcitx5's event loop: any blocking call in it stalls keyboard input for every application on the desktop, so it must only do non-blocking socket reads and a string commit. The daemon, which is free to block, owns the messy parts — spawning and reaping an audio capture process, rewriting WAV headers, chunking long recordings, and waiting on a network request that can take seconds.

The wire protocol between the two is a frozen contract and is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md). The reasoning behind the design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

You need Fcitx 5 installed and running, a C++17 compiler with CMake 3.19 or newer, and the fcitx5 development headers so the addon can be built. On Debian and Ubuntu the headers come from `libfcitx5core-dev`, `libfcitx5config-dev`, `libfcitx5utils-dev` and `fcitx5-modules-dev`; on Fedora it is `fcitx5-devel`; on Arch the headers ship with the `fcitx5` package. Distribution package names vary between releases, so if one of these does not exist on your system, search your package manager for `fcitx5` development files — the build only needs `Fcitx5Core` and the `Notifications` module of `Fcitx5Module`.

The daemon needs Python 3.10 or newer. It uses only the standard library, plus the official `sarvamai` SDK, which it imports lazily so that the daemon starts and its test suite runs even when the SDK is not installed. For audio you need PipeWire, PulseAudio or ALSA with one of `pw-record`, `parec` or `arecord` on your `PATH`; the daemon picks whichever it finds first. Finally you need a Sarvam AI API key, which you can create at [dashboard.sarvam.ai](https://dashboard.sarvam.ai).

## Install

Clone the repository and run the installer from the top of the checkout:

```bash
git clone https://github.com/your-account/fcitx5-sarvam-dictation.git
cd fcitx5-sarvam-dictation
./scripts/install.sh
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

The addon starts the daemon by itself when it cannot reach one, but a service gives you a stable process with logs in the journal, which is easier to debug. If you run the service, you can turn the addon's `AutoStartDaemon` option off.

## Configure

The daemon reads `~/.config/sarvam-dictation/config.toml` (or `$XDG_CONFIG_HOME/sarvam-dictation/config.toml` when that variable is set). A missing file is not an error — every setting falls back to its default. A complete example:

```toml
[api]
key = ""                # empty -> read env SARVAM_API_KEY
model = "saaras:v4"     # or "saaras:v3"
mode = "transcribe"     # transcribe | translate | verbatim | translit | codemix
language_code = ""      # "" = auto-detect; else e.g. "hi-IN"
keyterms = []           # list[str], saaras:v4 only, <=50 entries

[audio]
device = ""             # "" = system default source
sample_rate = 16000
backend = "auto"        # auto | pipewire | pulse | alsa

[server]
socket_path = ""        # "" = the default socket path rule
max_duration_s = 300    # auto-stop and transcribe at this length
```

The addon has its own options, which you edit in `fcitx5-configtool` under the addon's configuration page:

| Option | Default | Meaning |
|---|---|---|
| `TriggerKey` | `Control+Alt+d` | Key combination that starts and stops dictation. |
| `SocketPath` | `""` | Path to the daemon socket; empty means the default rule (`$XDG_RUNTIME_DIR/sarvam-dictation.sock`, else `/tmp/sarvam-dictation-<uid>.sock`). Set this only if you override the daemon's `socket_path`. |
| `AutoStartDaemon` | `true` | Start `DaemonCommand` when the daemon cannot be reached. |
| `DaemonCommand` | `sarvam-dictation-daemon` | Program the addon launches when autostarting. |
| `ShowNotifications` | `true` | Show "Listening…" and "Transcribing…" notification tips. |
| `AppendSpace` | `true` | Append a space after the committed transcript, so consecutive dictations do not run together. |

The API key can come from either the `key` field of `[api]` or the `SARVAM_API_KEY` environment variable. To keep the key in the file instead, replace the empty value with your own key, so the `[api]` section reads `key = "YOUR_SARVAM_API_KEY"` — the placeholder is exactly what belongs in anything you share or commit. The environment fills in only when the config file value is empty; an explicit key in the file is never shadowed by the environment. Keep the key out of version control — the file lives in your home directory, and if you keep dotfiles in git, add `sarvam-dictation/config.toml` to `.gitignore` rather than committing a real key. Never put a key in a repository, an issue, or a screenshot.

## Usage

Press `Control+Alt+d` (or whatever you bound to `TriggerKey`). A notification appears saying *Listening…* and the daemon starts recording from your default source. Press the same hotkey again: the notification changes to *Transcribing…*, the recording is finalised and sent to Sarvam, and when the answer comes back the text is committed into the field that had focus when you first pressed the hotkey, followed by a space if `AppendSpace` is on. The notification closes when the daemon returns to idle.

The `mode` setting chooses what Saaras does with your speech. `transcribe` is the everyday choice: speech in, text in the same language's own script. `translate` returns the transcript in English, which is useful when you speak an Indian language but need English text. `verbatim` keeps the disfluencies and false starts rather than tidying them away, which is what you want for interview or meeting notes where the exact wording matters. `translit` returns romanised text instead of native script, useful for names, addresses and anything you will read back over a phone line. `codemix` is for speech that mixes languages inside a sentence, such as Hindi with English technical words, and preserves that mixture instead of forcing one language. Leave `language_code` empty to let the service detect the language; set it (for example `hi-IN`) when you know it, since detection on a short utterance is the most common source of a wrong-language transcript.

The REST endpoint accepts audio up to about 30 seconds per request, so the daemon splits anything longer into 28-second chunks, transcribes them in order and joins the pieces with single spaces. You do not have to think about this: hold the hotkey session open for a two-minute thought and it works. The `max_duration_s` setting (default 300) stops the recording automatically at that length so a forgotten session cannot run forever.

To check the daemon without involving fcitx5 at all, record from a terminal:

```bash
sarvam-dictation-ctl record out.wav
```

Speak, press Enter, and you get a WAV file plus the transcript the daemon produced. This is the fastest way to tell whether a problem is in the audio path, in the API call, or in the addon. From a checkout that has not been installed, the same command is `./scripts/sarvam-dictation-ctl record out.wav`.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| "Sarvam dictation: daemon not reachable" | The addon could not connect and gave up after retrying for five seconds. Start the daemon in the foreground (`sarvam-dictation-daemon --log-level debug`) and watch its output. If it starts fine, the two sides disagree about the socket path: check `socket_path` in the daemon config and `SocketPath` in the addon options. |
| `no_audio_device` error | No capture program was found, or the configured device does not exist. Install PipeWire or PulseAudio utilities so `pw-record` or `parec` is on `PATH`, or set `[audio] device` to a real source. `sarvam-dictation-daemon --check` reports which backend it resolved. |
| `audio_failed` error | The recorder produced zero-length or unreadable audio — usually a wrong `device`, or the capture process died immediately. Confirm the source works with `pw-record`/`parec`/`arecord` on the command line first. |
| `401` or `api` error | The API key is missing, wrong, or out of quota. Check `key` in `~/.config/sarvam-dictation/config.toml` or `SARVAM_API_KEY`, and that the key is active on the dashboard. A key that was pasted with surrounding whitespace counts as wrong. |
| `no_api_key` error | Neither the config file nor the environment has a key. `sarvam-dictation-daemon --check` states plainly whether a key is present. |
| Empty transcript, nothing inserted | The daemon transcribed silence or noise and returned an empty string, which is a legal result and commits nothing. Check the microphone is not muted and that `[audio] device` points at it; raise your input level and try a longer utterance. |
| Text is transcribed but does not appear in the application | The application may not be using fcitx5 at all. Electron and some GTK applications only get text committed through fcitx5's own frontend: make sure the app is running under Wayland with `GTK_IM_MODULE=fcitx`/`QT_IM_MODULE=fcitx`, or under X11 with `XMODIFIERS=@im=fcitx`, and that fcitx5's Wayland or X11 frontend is the one the app connects to. Also check whether the window that had focus was closed while transcribing — the text then goes to the most recent input context, which may be a different window. |
| Hotkey does nothing, or does the wrong thing | Another binding owns `Control+Alt+d`. Change `TriggerKey` in the addon's configuration page, or free the key in your desktop's keyboard settings. Bindings grabbed by the compositor never reach fcitx5. |
| Need to see what happened | Daemon logs go to the journal when it runs as a service: `journalctl --user -u sarvam-dictation -f`. For a foreground daemon, raise the level with `sarvam-dictation-daemon --log-level debug`. For the addon, restart fcitx5 in verbose mode with `fcitx5 -rd --verbose`. |

`sarvam-dictation-daemon --check` is the single most useful diagnostic: it validates the config file, reports the resolved audio backend and device, says whether an API key is present, and exits non-zero if something is wrong.

## Development

Build with the CMake commands in the install section; there is no separate development configuration. The Python test suite runs from the top of the checkout and needs no API key and no audio device:

```bash
pytest -q tests
```

The protocol lives in two places, and they must stay in step: `daemon/sarvam_dictation/protocol.py` on the Python side and `src/sarvamdictation.cpp` on the C++ side. [docs/PROTOCOL.md](docs/PROTOCOL.md) describes the same contract in prose, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains why the pieces are arranged the way they are.

## Privacy

Your audio is uploaded to Sarvam AI's API to be transcribed. That is the whole point of the tool, and it means the audio leaves your machine and is handled under Sarvam's terms. Nothing else is retained: the daemon writes a temporary WAV in a temporary directory, transcribes it, and deletes it. There is no history, no database, no analytics, and no storage of transcripts by this project. If you would rather not send a given piece of audio to a remote service, do not dictate it.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Suhan G Paradkar.

This project is not affiliated with, endorsed by, or supported by Sarvam AI or the Fcitx project. Sarvam and Saaras are products of Sarvam AI; Fcitx 5 is a project of the Fcitx community. Issues with this addon belong in this repository, not in theirs.

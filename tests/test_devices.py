"""Tests for brief 9's device enumeration (``devices.py`` and the CLI flag).

No ``pactl`` or ``arecord`` is needed: every enumeration case goes through the
injectable ``runner``, which returns canned ``(returncode, stdout)`` pairs.  The
two cases that exercise the real subprocess call use an interpreter that is
always present, so they check the timeout plumbing rather than a sound stack.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

import sarvam_dictation.__main__ as daemon_main
import sarvam_dictation.devices as devices_mod
from sarvam_dictation.devices import AudioDevice, format_device_list, list_devices

# --------------------------------------------------------------------------
# realistic tool output
# --------------------------------------------------------------------------

#: `pactl list short sources` on a PipeWire box with a built-in mic, a USB mic
#: and two monitor sources. Fields are tab-separated; field 2 is the name.
PACTL_OUTPUT = (
    "52\talsa_output.pci-0000_00_1f.3.analog-stereo.monitor\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
    "53\talsa_input.pci-0000_00_1f.3.analog-stereo\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
    "60\talsa_input.usb-Generic_USB_Audio-00.mono-fallback\tPipeWire\ts16le 1ch 48000Hz\tSUSPENDED\n"
    "61\talsa_output.usb-Generic_USB_Audio-00.analog-stereo.monitor\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
)

#: `arecord -l` on a laptop: one HDA codec, one HDMI output-only card that still
#: reports a capture device, plus the non-device lines arecord always prints.
ARECORD_OUTPUT = """**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC257 Analog [ALC257 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: HDMI [HDA ATI HDMI], device 3: HDMI 0 [HDMI 0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


def make_runner(table):
    """Return a runner answering from ``table`` and recording every argv."""
    calls = []

    def runner(argv):
        calls.append(list(argv))
        key = " ".join(argv)
        if key in table:
            return table[key]
        return (1, "")

    runner.calls = calls
    return runner


def pulse_runner(output=PACTL_OUTPUT, returncode=0):
    return make_runner({"pactl list short sources": (returncode, output)})


def alsa_runner(output=ARECORD_OUTPUT, returncode=0):
    return make_runner({"arecord -l": (returncode, output)})


# --------------------------------------------------------------------------
# pulse
# --------------------------------------------------------------------------


def test_pulse_listing_is_parsed_in_tool_order():
    found = list_devices("pulse", pulse_runner())

    assert [device.id for device in found] == [
        "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
        "alsa_input.pci-0000_00_1f.3.analog-stereo",
        "alsa_input.usb-Generic_USB_Audio-00.mono-fallback",
        "alsa_output.usb-Generic_USB_Audio-00.analog-stereo.monitor",
    ]
    assert {device.backend for device in found} == {"pulse"}


def test_pulse_uses_the_short_sources_command():
    runner = pulse_runner()

    list_devices("pulse", runner)

    assert runner.calls == [["pactl", "list", "short", "sources"]]


def test_ordinary_sources_are_labelled_with_their_name():
    found = list_devices("pulse", pulse_runner())

    assert found[1] == AudioDevice(
        id="alsa_input.pci-0000_00_1f.3.analog-stereo",
        label="alsa_input.pci-0000_00_1f.3.analog-stereo",
        backend="pulse",
    )


def test_monitor_sources_say_they_record_playback_not_the_microphone():
    found = list_devices("pulse", pulse_runner())

    monitors = [device for device in found if device.id.endswith(".monitor")]
    assert len(monitors) == 2
    for device in monitors:
        assert device.label == device.id + " (monitor of an output)"
        assert "monitor of an output" in device.label


def test_pulse_keeps_lines_whose_fields_are_space_padded():
    output = "0 alsa_input.pci-0000_00_1f.3.analog-stereo PipeWire s16le 2ch 48000Hz RUNNING\n"

    found = list_devices("pulse", pulse_runner(output))

    assert [device.id for device in found] == ["alsa_input.pci-0000_00_1f.3.analog-stereo"]


def test_pulse_skips_blank_and_single_field_lines():
    output = "\n   \nnot-a-source\n0\talsa_input.mic\tPipeWire\ts16le 1ch 48000Hz\tRUNNING\n"

    found = list_devices("pulse", pulse_runner(output))

    assert [device.id for device in found] == ["alsa_input.mic"]


def test_pulse_with_no_sources_is_an_empty_list():
    assert list_devices("pulse", pulse_runner("")) == []


# --------------------------------------------------------------------------
# pipewire
# --------------------------------------------------------------------------


def test_pipewire_backend_uses_the_pulse_command_and_labels_pipewire():
    runner = pulse_runner()

    found = list_devices("pipewire", runner)

    assert runner.calls == [["pactl", "list", "short", "sources"]]
    assert [device.backend for device in found] == ["pipewire"] * 4
    assert found[0].id == "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"
    assert found[0].label.endswith("(monitor of an output)")


# --------------------------------------------------------------------------
# alsa
# --------------------------------------------------------------------------


def test_arecord_listing_is_parsed_into_plughw_ids():
    found = list_devices("alsa", alsa_runner())

    assert [device.id for device in found] == ["plughw:0,0", "plughw:2,3"]
    assert {device.backend for device in found} == {"alsa"}


def test_arecord_labels_are_built_from_the_card_and_device_names():
    found = list_devices("alsa", alsa_runner())

    assert found[0].label == "HDA Intel PCH: ALC257 Analog"
    assert found[1].label == "HDA ATI HDMI: HDMI 0"


def test_arecord_uses_the_dash_l_command():
    runner = alsa_runner()

    list_devices("alsa", runner)

    assert runner.calls == [["arecord", "-l"]]


def test_arecord_output_that_does_not_match_is_skipped_not_fatal():
    output = (
        "**** List of CAPTURE Hardware Devices ****\n"
        "this line is not a device\n"
        "card 1: PCH [HDA Intel PCH], device 0: ALC257 Analog [ALC257 Analog]\n"
        "card x: broken [unclosed\n"
        "card 3: Device [USB Audio], device 0: USB Audio [USB Audio]\n"
    )

    found = list_devices("alsa", alsa_runner(output))

    assert [device.id for device in found] == ["plughw:1,0", "plughw:3,0"]


def test_arecord_with_no_devices_is_an_empty_list():
    output = "**** List of CAPTURE Hardware Devices ****\n"

    assert list_devices("alsa", alsa_runner(output)) == []


# --------------------------------------------------------------------------
# auto ordering
# --------------------------------------------------------------------------


def test_auto_prefers_pulse_over_alsa():
    runner = make_runner(
        {
            "pactl list short sources": (0, PACTL_OUTPUT),
            "arecord -l": (0, ARECORD_OUTPUT),
        }
    )

    found = list_devices("auto", runner)

    assert {device.backend for device in found} == {"pulse"}
    assert runner.calls == [["pactl", "list", "short", "sources"]]


def test_auto_falls_back_to_alsa_when_pulse_has_no_sources():
    runner = make_runner(
        {
            "pactl list short sources": (0, ""),
            "arecord -l": (0, ARECORD_OUTPUT),
        }
    )

    found = list_devices("auto", runner)

    assert [device.id for device in found] == ["plughw:0,0", "plughw:2,3"]
    assert [call for call in runner.calls] == [
        ["pactl", "list", "short", "sources"],
        ["arecord", "-l"],
    ]


def test_auto_is_the_default_backend():
    runner = make_runner({"pactl list short sources": (0, PACTL_OUTPUT)})

    assert [device.id for device in list_devices(runner=runner)] == [
        device.id for device in list_devices("pulse", pulse_runner())
    ]


def test_auto_with_neither_tool_yields_nothing():
    runner = make_runner({})

    assert list_devices("auto", runner) == []
    assert runner.calls == [["pactl", "list", "short", "sources"], ["arecord", "-l"]]


def test_an_unknown_backend_yields_nothing_and_runs_nothing():
    runner = make_runner({})

    assert list_devices("oss", runner) == []
    assert runner.calls == []


def test_backend_name_is_case_and_whitespace_insensitive():
    assert list_devices("  PULSE ", pulse_runner())[0].backend == "pulse"


# --------------------------------------------------------------------------
# failures never raise
# --------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["auto", "pulse", "pipewire", "alsa"])
def test_a_non_zero_exit_yields_an_empty_list(backend):
    runner = make_runner(
        {
            "pactl list short sources": (1, "Connection failure: Connection refused\n"),
            "arecord -l": (2, "arecord: device_list:274: no soundcards found...\n"),
        }
    )

    assert list_devices(backend, runner) == []


def test_a_missing_tool_yields_an_empty_list():
    def runner(argv):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    assert list_devices("auto", runner) == []


def test_a_timeout_yields_an_empty_list():
    def runner(argv):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=devices_mod.TOOL_TIMEOUT_S)

    assert list_devices("auto", runner) == []
    assert list_devices("alsa", runner) == []


def test_a_runner_that_raises_anything_yields_an_empty_list():
    def runner(argv):
        raise RuntimeError("boom")

    assert list_devices("auto", runner) == []


def test_a_runner_returning_nonsense_yields_an_empty_list():
    for rubbish in (None, "text", (0,), (0, None), ("zero", ""), object()):
        assert list_devices("pulse", lambda argv, value=rubbish: value) == []


def test_bytes_output_is_decoded():
    found = list_devices("pulse", lambda argv: (0, PACTL_OUTPUT.encode()))

    assert len(found) == 4


def test_unparseable_output_yields_an_empty_list():
    assert list_devices("pulse", pulse_runner("\x00\x01 garbage \xff\n")) == []
    assert list_devices("alsa", alsa_runner("\x00\x01 garbage \xff\n")) == []


def test_the_default_runner_gives_up_after_its_timeout(monkeypatch):
    """A hung tool must not block: the default runner kills it and returns empty."""
    monkeypatch.setattr(devices_mod, "TOOL_TIMEOUT_S", 0.25)
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]

    started = time.monotonic()
    returncode, output = devices_mod._default_runner(argv)
    elapsed = time.monotonic() - started

    assert (returncode, output) == (127, "")
    assert elapsed < 5.0


def test_the_default_runner_reports_a_missing_binary_as_no_output():
    returncode, output = devices_mod._default_runner(
        ["sarvam-dictation-no-such-binary-anywhere"]
    )

    assert (returncode, output) == (127, "")


def test_the_default_runner_captures_stdout_and_ignores_stderr():
    returncode, output = devices_mod._default_runner(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('card 0: ok\\n'); sys.stderr.write('noise\\n')",
        ]
    )

    assert returncode == 0
    assert output == "card 0: ok\n"


def test_the_default_runner_passes_a_short_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "0\talsa_input.mic\tPipeWire\n")

    monkeypatch.setattr(devices_mod.subprocess, "run", fake_run)

    found = list_devices("pulse")

    assert [device.id for device in found] == ["alsa_input.mic"]
    assert seen["timeout"] == devices_mod.TOOL_TIMEOUT_S
    assert seen["stderr"] is subprocess.DEVNULL
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["argv"] == ["pactl", "list", "short", "sources"]


def test_list_devices_never_raises_when_the_parsers_blow_up(monkeypatch):
    def explode(runner, label_backend="pulse"):
        raise AssertionError("parser exploded")

    monkeypatch.setattr(devices_mod, "_pulse_devices", explode)
    monkeypatch.setattr(devices_mod, "_alsa_devices", explode)

    assert list_devices("auto", pulse_runner()) == []
    assert list_devices("pulse", pulse_runner()) == []
    assert list_devices("alsa", pulse_runner()) == []


# --------------------------------------------------------------------------
# format_device_list
# --------------------------------------------------------------------------


def test_format_is_id_tab_label_per_line():
    found = list_devices("alsa", alsa_runner())

    assert format_device_list(found) == (
        "plughw:0,0\tHDA Intel PCH: ALC257 Analog\n"
        "plughw:2,3\tHDA ATI HDMI: HDMI 0\n"
    )


def test_format_ends_with_exactly_one_newline():
    text = format_device_list(list_devices("pulse", pulse_runner()))

    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert text.count("\n") == 4


def test_format_of_no_devices_is_the_empty_string():
    assert format_device_list([]) == ""


def test_labels_may_contain_spaces_and_are_split_on_the_first_tab_only():
    devices = [
        AudioDevice(
            id="alsa_input.usb-Generic_USB_Audio-00.mono-fallback",
            label="USB Audio mono fallback (monitor of an output)",
            backend="pulse",
        )
    ]

    line = format_device_list(devices).rstrip("\n")

    assert line.count("\t") == 1
    device_id, label = line.split("\t", 1)
    assert device_id == "alsa_input.usb-Generic_USB_Audio-00.mono-fallback"
    assert label == "USB Audio mono fallback (monitor of an output)"


def test_format_flattens_a_label_that_contains_a_newline_or_tab():
    devices = [AudioDevice(id="plughw:0,0", label="Line\none\ttwo", backend="alsa")]

    text = format_device_list(devices)

    assert text == "plughw:0,0\tLine one two\n"
    assert text.count("\n") == 1


def test_format_skips_a_device_with_an_empty_id():
    devices = [
        AudioDevice(id="", label="nameless", backend="pulse"),
        AudioDevice(id="plughw:0,0", label="ok", backend="alsa"),
    ]

    assert format_device_list(devices) == "plughw:0,0\tok\n"


def test_format_accepts_any_sequence():
    found = list_devices("alsa", alsa_runner())

    assert format_device_list(tuple(found)) == format_device_list(found)


# --------------------------------------------------------------------------
# --list-audio-devices
# --------------------------------------------------------------------------


@pytest.fixture
def no_config(monkeypatch):
    """Fail loudly if the flag reads the config, the key or starts a server."""
    called = []

    def boom(*args, **kwargs):
        called.append(args)
        raise AssertionError("--list-audio-devices must not read the config")

    monkeypatch.setattr(daemon_main, "load_config", boom)
    monkeypatch.setattr(daemon_main, "_run_check", boom)

    async def serve_boom(*args, **kwargs):
        raise AssertionError("--list-audio-devices must not start the server")

    monkeypatch.setattr(daemon_main, "_serve", serve_boom)
    return called


def test_the_flag_is_documented_in_help(capsys):
    with pytest.raises(SystemExit) as excinfo:
        daemon_main.build_parser().parse_args(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--list-audio-devices" in out
    assert "id<TAB>label" in out


def test_the_flag_prints_the_devices_and_exits_zero(monkeypatch, capsys, no_config):
    monkeypatch.setattr(
        daemon_main,
        "list_devices",
        lambda backend="auto", runner=None: list_devices("alsa", alsa_runner()),
    )

    assert daemon_main.main(["--list-audio-devices"]) == 0

    out = capsys.readouterr()
    assert out.out == (
        "plughw:0,0\tHDA Intel PCH: ALC257 Analog\n"
        "plughw:2,3\tHDA ATI HDMI: HDMI 0\n"
    )
    assert out.err == ""
    assert no_config == []


def test_the_flag_exits_zero_with_an_empty_list(monkeypatch, capsys, no_config):
    monkeypatch.setattr(daemon_main, "list_devices", lambda backend="auto", runner=None: [])

    assert daemon_main.main(["--list-audio-devices"]) == 0
    assert capsys.readouterr().out == ""


def test_the_flag_exits_zero_with_no_environment_at_all(monkeypatch, capsys):
    """No config, no HOME/XDG, no API key, and no audio tool installed."""
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "SARVAM_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def no_tools(argv):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(devices_mod, "_default_runner", no_tools)

    assert daemon_main.main(["--list-audio-devices"]) == 0
    assert capsys.readouterr().out == ""


def test_the_flag_output_round_trips_through_the_dropdown_parser(monkeypatch, capsys):
    monkeypatch.setattr(
        daemon_main,
        "list_devices",
        lambda backend="auto", runner=None: list_devices("pulse", pulse_runner()),
    )

    assert daemon_main.main(["--list-audio-devices"]) == 0

    lines = capsys.readouterr().out.splitlines()
    parsed = [line.split("\t", 1) for line in lines]
    assert [pair[0] for pair in parsed] == [
        "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
        "alsa_input.pci-0000_00_1f.3.analog-stereo",
        "alsa_input.usb-Generic_USB_Audio-00.mono-fallback",
        "alsa_output.usb-Generic_USB_Audio-00.analog-stereo.monitor",
    ]
    assert all(len(pair) == 2 and pair[1] for pair in parsed)


def test_the_flag_does_not_swallow_a_broken_stdout(monkeypatch, no_config):
    monkeypatch.setattr(daemon_main, "list_devices", lambda backend="auto", runner=None: [])

    class ClosedStdout:
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(daemon_main.sys, "stdout", ClosedStdout())

    assert daemon_main.main(["--list-audio-devices"]) == 0

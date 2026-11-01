"""Enumerate the capture devices this machine actually has.

The configuration page offers ``[audio] device`` as a dropdown, so it needs the
list of devices that exist *right now*.  The enumeration lives here, in the
daemon, because the daemon is the part of the project that already knows about
the audio stack: the addon reaches it through
``sarvam-dictation-daemon --list-audio-devices`` and parses
``id<TAB>label`` lines, so no audio library has to be linked into fcitx5.

Two rules shape this module:

* :func:`list_devices` **never raises**.  A missing tool, a non-zero exit, a
  timeout or unparseable output all mean "no devices", because the caller is a
  configuration dialog that must still open on a machine with no sound card.
* The ``id`` is exactly what the recorder passes to the capture tool
  (``--device=`` for ``parec``, ``-D`` for ``arecord``, ``--target`` for
  ``pw-record``), so a value taken from this list can be stored verbatim.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

__all__ = ["AudioDevice", "format_device_list", "list_devices", "TOOL_TIMEOUT_S"]

#: How long a single enumeration command may take before it is abandoned.
TOOL_TIMEOUT_S = 2.0

#: Appended to the label of a PulseAudio/PipeWire monitor source.
MONITOR_SUFFIX = " (monitor of an output)"

#: ``runner(argv) -> (returncode, stdout_text)``; injectable for tests.
Runner = Callable[[Sequence[str]], "tuple[int, str]"]

#: ``arecord -l`` prints one line per capture device, e.g.
#: ``card 1: PCH [HDA Intel PCH], device 0: ALC257 Analog [ALC257 Analog]``
_ALSA_LINE = re.compile(
    r"^card\s+(?P<card>\d+):\s+(?P<card_id>\S+)\s+\[(?P<card_name>[^\]]*)\]\s*,"
    r"\s*device\s+(?P<device>\d+):\s*(?P<dev_name>[^\[]*?)\s*\[(?P<sub_name>[^\]]*)\]"
)


@dataclass(frozen=True)
class AudioDevice:
    """One capture device, as the daemon and the configuration page see it."""

    id: str       # exactly what the capture tool wants
    label: str    # what a human should see
    backend: str  # "pulse", "pipewire" or "alsa"


def list_devices(backend: str = "auto", runner: Runner | None = None) -> list[AudioDevice]:
    """Enumerate capture devices. Never raises: a failing tool yields ``[]``.

    ``backend`` is ``auto`` (pulse, then alsa, first non-empty wins),
    ``pulse``, ``pipewire`` or ``alsa``.  ``pipewire`` runs the same command as
    ``pulse`` -- ``pw-record --target`` accepts pipewire-pulse source names --
    and labels the results ``pipewire``.  ``runner`` replaces the subprocess
    call: it takes an argv sequence and returns ``(returncode, stdout_text)``.
    """
    try:
        name = (backend or "auto").strip().lower()
        run = runner if runner is not None else _default_runner

        if name == "auto":
            for candidate in ("pulse", "alsa"):
                devices = _pulse_devices(run) if candidate == "pulse" else _alsa_devices(run)
                if devices:
                    return devices
            return []
        if name in ("pulse", "pipewire"):
            return _pulse_devices(run, label_backend=name)
        if name == "alsa":
            return _alsa_devices(run)
        return []
    except Exception:  # noqa: BLE001 - the caller must never see an exception
        return []


def format_device_list(devices: Sequence[AudioDevice]) -> str:
    """One device per line, as ``id<TAB>label``, ending with a newline.

    Empty string for no devices.  The label is last and may contain spaces;
    ids never contain a tab, so consumers split on the first tab only.
    """
    lines = []
    for device in devices:
        device_id = _single_line(device.id)
        if not device_id:
            continue
        lines.append(f"{device_id}\t{_single_line(device.label)}")
    if not lines:
        return ""
    return "".join(line + "\n" for line in lines)


# --------------------------------------------------------------------------
# the default runner
# --------------------------------------------------------------------------


def _default_runner(argv: Sequence[str]) -> tuple[int, str]:
    """Run ``argv`` with a short timeout and return ``(returncode, stdout)``.

    ``stderr`` is discarded: these tools chatter about missing servers and the
    daemon must not relay that.  Any failure -- the binary is not installed, it
    exits non-zero, it hangs past :data:`TOOL_TIMEOUT_S` -- is reported as "no
    output" rather than raised.
    """
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=TOOL_TIMEOUT_S,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return (127, "")
    return (completed.returncode, completed.stdout or "")


def _run(runner: Runner, argv: Sequence[str]) -> tuple[int, str]:
    """Call ``runner`` defensively and normalise whatever it returned."""
    try:
        result = runner(list(argv))
    except Exception:  # noqa: BLE001 - a broken runner is "no output"
        return (1, "")
    try:
        returncode, output = result  # type: ignore[misc]
    except (TypeError, ValueError):
        return (1, "")
    try:
        code = int(returncode)
    except (TypeError, ValueError):
        return (1, "")
    if isinstance(output, bytes):
        try:
            output = output.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - defensive only
            return (1, "")
    if not isinstance(output, str):
        return (1, "")
    return (code, output)


# --------------------------------------------------------------------------
# PulseAudio / PipeWire (through pipewire-pulse)
# --------------------------------------------------------------------------


def _pulse_devices(runner: Runner, label_backend: str = "pulse") -> list[AudioDevice]:
    """Parse ``pactl list short sources``, keeping the tool's order."""
    returncode, output = _run(runner, ["pactl", "list", "short", "sources"])
    if returncode != 0 or not output:
        return []
    devices: list[AudioDevice] = []
    for line in output.splitlines():
        name = _pactl_source_name(line)
        if not name:
            continue
        label = name + MONITOR_SUFFIX if name.endswith(".monitor") else name
        devices.append(AudioDevice(id=name, label=label, backend=label_backend))
    return devices


def _pactl_source_name(line: str) -> str:
    """Return the source name (second field) of one ``pactl`` line, or ``""``."""
    line = line.rstrip("\r")
    if not line.strip():
        return ""
    fields = line.split("\t")
    if len(fields) < 2:
        # ``pactl list short`` is tab-separated, but a few builds pad with
        # spaces. Such a line still starts with the numeric index and carries at
        # least index, name and driver, so both are required before the padded
        # form is trusted -- that keeps stray noise lines out.
        padded = line.split()
        if len(padded) < 3 or not padded[0].isdigit():
            return ""
        fields = padded
    name = fields[1].strip()
    if any(char < " " or char == "\x7f" for char in name):
        return ""
    return name


# --------------------------------------------------------------------------
# ALSA
# --------------------------------------------------------------------------


def _alsa_devices(runner: Runner) -> list[AudioDevice]:
    """Parse ``arecord -l``, skipping every line that is not a device."""
    returncode, output = _run(runner, ["arecord", "-l"])
    if returncode != 0 or not output:
        return []
    devices: list[AudioDevice] = []
    for line in output.splitlines():
        match = _ALSA_LINE.match(line.strip())
        if match is None:
            continue
        card = match.group("card")
        number = match.group("device")
        card_name = (match.group("card_name") or "").strip()
        device_name = (match.group("dev_name") or "").strip()
        subdevice_name = (match.group("sub_name") or "").strip()
        card_id = (match.group("card_id") or "").strip()
        devices.append(
            AudioDevice(
                id=f"plughw:{card},{number}",
                label=_alsa_label(card_name, device_name, subdevice_name, card_id),
                backend="alsa",
            )
        )
    return devices


def _alsa_label(card_name: str, device_name: str, subdevice_name: str, card_id: str) -> str:
    """Build a readable ``<card>: <device>`` label from an ``arecord -l`` line."""
    card = card_name or card_id or "card"
    detail = device_name or subdevice_name
    if detail and detail != card:
        return f"{card}: {detail}"
    return card


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _single_line(value: str) -> str:
    """Flatten ``value`` so it cannot break the ``id<TAB>label`` framing."""
    return " ".join(str(value).split())

"""Command-line entry point for the sarvam-dictation daemon.

Installed as the ``sarvam-dictation-daemon`` console script; the package is
also runnable with the interpreter's ``-m`` switch. Logs go to stderr, and
SIGINT/SIGTERM shut the socket down cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from . import __version__
from .config import (
    API_KEY_ENV,
    REST_MODELS,
    STREAMING_MODELS,
    Config,
    ConfigError,
    default_config_path,
    default_socket_path,
    load_config,
)
from .recorder import RecorderError, detect_backend
from .server import DictationServer

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_LEVELS = ("debug", "info", "warning", "error")


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the daemon CLI."""
    parser = argparse.ArgumentParser(
        prog="sarvam-dictation-daemon",
        description="Dictation daemon for the fcitx5 Sarvam addon "
        "(audio capture + Sarvam AI speech-to-text over a Unix socket).",
    )
    parser.add_argument("--socket", metavar="PATH", default=None, help="Unix socket path to bind")
    parser.add_argument("--config", metavar="PATH", default=None, help="path to config.toml")
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default="info",
        help="stderr log verbosity (default: info)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config, audio backend and API key, print a report and exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"sarvam-dictation-daemon {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the daemon; returns a process exit status."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format=LOG_FORMAT,
        stream=sys.stderr,
    )

    config_path = Path(args.config) if args.config else None
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        logging.error("configuration error: %s", exc)
        return 2

    if args.check:
        return _run_check(config, config_path, args.socket)

    try:
        asyncio.run(_serve(config, args.socket))
    except KeyboardInterrupt:  # pragma: no cover - terminal Ctrl-C without handlers
        logging.info("interrupted; exiting")
        return 0
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1
    except OSError as exc:
        logging.error("cannot start the daemon: %s", exc)
        return 1
    return 0


async def _serve(config: Config, socket_path: str | None) -> None:
    """Bind, serve until a signal arrives, then shut down cleanly."""
    server = DictationServer(config, socket_path=socket_path)
    await server.start()
    logging.info("sarvam-dictation-daemon %s ready", __version__)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signum, stop_event.set)
            installed.append(signum)

    serve_task = asyncio.create_task(server.serve_forever())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for signum in installed:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signum)
        for task in (serve_task, stop_task):
            task.cancel()
        await asyncio.gather(serve_task, stop_task, return_exceptions=True)
        await server.shutdown()


def _run_check(config: Config, config_path: Path | None, socket_override: str | None) -> int:
    """Print a diagnostic report for ``--check`` and return 0 (ok) or 1 (problem)."""
    path = config_path if config_path is not None else default_config_path()
    problems: list[str] = []

    exists = path.exists()
    print(f"config file:  {path} ({'present' if exists else 'not present, using defaults'})")

    if config.api_key:
        file_only = _file_api_key(path)
        source = "config file" if file_only else API_KEY_ENV
        print(f"api key:      set (from {source})")
    else:
        print(f"api key:      MISSING (set [api] key or export {API_KEY_ENV})")
        problems.append("no API key")

    print(f"model:        {config.model}")
    print(f"streaming:    {'on (realtime websocket)' if config.streaming else 'off (one request per recording)'}")

    # Brief 8: name the endpoint this configuration will actually talk to and
    # check the model against that endpoint's namespace, so a mismatch is one
    # line here instead of the provider's HTTP 400 later.
    if config.streaming:
        endpoint = "realtime websocket"
        allowed = STREAMING_MODELS
    else:
        endpoint = "REST /speech-to-text"
        allowed = REST_MODELS
    print(f"endpoint:     {endpoint}")
    if config.model not in allowed:
        if config.streaming:
            problems.append(
                f"model {config.model!r} cannot be used with streaming; "
                f"use {STREAMING_MODELS[0]} or {STREAMING_MODELS[1]}"
            )
        else:
            problems.append(
                f"model {config.model!r} cannot be used with REST /speech-to-text; "
                f"use {_or_list(REST_MODELS)}"
            )

    print(f"mode:         {config.mode}")
    print(f"language:     {config.language_code or 'auto-detect'}")
    print(f"keyterms:     {len(config.keyterms)}")
    print(f"sample rate:  {config.sample_rate} Hz")
    print(f"max length:   {config.max_duration_s} s")

    try:
        backend = detect_backend(config.backend)
        print(f"audio:        {backend} (backend={config.backend})")
    except RecorderError as exc:
        print(f"audio:        UNAVAILABLE ({exc})")
        problems.append("no audio backend")

    socket_path = socket_override or config.socket_path or default_socket_path()
    print(f"socket:       {socket_path}")

    if problems:
        print(f"result:       FAILED ({', '.join(problems)})")
        return 1
    print("result:       OK")
    return 0


def _or_list(names: tuple[str, ...]) -> str:
    """Join ``names`` as ``a, b or c`` for the ``--check`` messages."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} or {names[-1]}"


def _file_api_key(path: Path) -> str:
    """Return the API key as configured in the file alone (ignoring the env var)."""
    try:
        return load_config(path, env={}).api_key
    except ConfigError:  # pragma: no cover - already validated by the caller
        return ""


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())

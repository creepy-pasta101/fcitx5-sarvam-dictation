"""Python daemon for fcitx5-sarvam-dictation.

Deliberately light: importing this package must not pull in asyncio, the audio
stack or the Sarvam SDK. Import the submodules you need explicitly.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]

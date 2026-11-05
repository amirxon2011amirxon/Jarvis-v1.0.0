"""Local web interface for Jarvis.

The original desktop shell lives in ``ui.py``.  This package is a presentation
adapter around the existing application: ``main.JarvisLive`` still owns the
conversation, ``actions.tool_calling`` still owns the tools, and ``config`` is
still the only place settings are read and written.
"""
from __future__ import annotations

from pathlib import Path

__version__ = "1.0.0"


def frontend_root() -> Path:
    """The frontend package directory, independent of the process CWD."""
    try:
        from importlib.resources import files

        return Path(str(files("frontend")))
    except Exception:  # noqa: BLE001 - source checkout fallback
        return Path(__file__).resolve().parent.parent / "frontend"


def frontend_dist() -> Path:
    """The packaged production assets (``frontend/dist``)."""
    return frontend_root() / "dist"

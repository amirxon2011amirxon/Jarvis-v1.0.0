"""Explicit, one-shot screenshots for Jarvis.

This tool exists for the narrow request "take a screenshot".  It captures the
PRIMARY display once and writes a PNG.  It never runs in the background, never
starts a timer or a watcher, and never transmits the image anywhere.

The capture backend is ``mss``, the same dependency the screen-understanding
tool already uses, and Pillow is its existing PNG encoder.  No new dependency
is introduced.
"""
from __future__ import annotations

import io
import os
import re
import time
from pathlib import Path
from typing import Any, Dict

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_FILENAME_ATTEMPTS = 10_000


def _default_screenshot_dir() -> Path:
    """The user's shell Pictures folder, with a safe fallback."""
    pictures = ""
    try:
        from actions import open_file_tool

        pictures = open_file_tool._home_folder("pictures")
    except Exception:  # noqa: BLE001 - fall back to the profile path
        pictures = ""
    if not pictures:
        try:
            pictures = str(Path.home() / "Pictures")
        except Exception:  # noqa: BLE001 - a broken profile is reported later
            pictures = ""
    return Path(pictures) / "Screenshots"


def _known_folder_path(raw: str) -> Path | None:
    """Resolve ``Downloads`` / ``Downloads\\Shots`` through shell folders."""
    known = ("desktop", "documents", "downloads", "pictures",
             "music", "videos", "screenshots")

    def base_for(key: str) -> str:
        try:
            from actions import open_file_tool

            return open_file_tool._home_folder(key)
        except Exception:  # noqa: BLE001 - treated as an invalid relative path
            return ""

    bare = str(raw or "").strip().rstrip("\\/").casefold()
    if bare in known:
        base = base_for(bare)
        return Path(base) if base else None
    match = re.match(r"^([A-Za-z ]+)[\\/]+(.+)$", str(raw or ""))
    if not match:
        return None
    key = match.group(1).strip().casefold()
    if key not in known:
        return None
    base = base_for(key)
    if not base:
        return None
    return Path(base) / match.group(2)


def _resolve_save_dir(save_path: Any) -> Path:
    """Return the destination folder, or raise ValueError for a bad path."""
    raw = str(save_path or "").strip()
    if not raw:
        return _default_screenshot_dir()
    if "\x00" in raw or any(ord(ch) < 0x20 for ch in raw):
        raise ValueError("The save path contains control characters.")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        known = _known_folder_path(expanded)
        if known is None:
            raise ValueError(
                "save_path must be an absolute folder or a known folder "
                "such as Downloads or Pictures.")
        candidate = known
    if candidate.exists() and not candidate.is_dir():
        raise ValueError(
            f"save_path '{candidate}' is a file, not a folder.")
    return candidate


def _timestamp_slug() -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())


def _capture_primary_png() -> bytes:
    """Capture the primary display once and return PNG bytes.

    Kept as one seam so tests can exercise the file/validation behaviour
    without a physical display.  The geometry is read on every call, so a
    resolution or monitor change needs no reset.
    """
    import mss
    from PIL import Image

    with mss.mss() as sct:
        monitors = sct.monitors
        index = 1 if len(monitors) > 1 else 0  # monitors[1] is primary
        shot = sct.grab(monitors[index])
        image = Image.frombytes("RGB", shot.size, shot.rgb)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def _write_exclusive(path: Path, data: bytes) -> None:
    """Create ``path`` exactly once; never overwrite an existing file."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _verify_png(path: Path, data: bytes) -> bool:
    try:
        if path.stat().st_size != len(data):
            return False
        with open(path, "rb") as handle:
            return handle.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE
    except OSError:
        return False


def screenshot(save_path: str = "") -> Dict[str, Any]:
    """Capture the primary display once and save it as a PNG.

    ``save_path`` is an optional destination FOLDER.  When omitted the file
    goes to the user's Pictures/Screenshots directory.  Filenames are
    timestamped and a numeric suffix is added rather than overwriting an
    existing screenshot.
    """
    try:
        directory = _resolve_save_dir(save_path)
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise ValueError(f"'{directory}' is not a folder.")
    except PermissionError as exc:
        return {
            "success": False,
            "ok": False,
            "error": f"Windows denied permission to create '{save_path}': {exc}",
        }
    except (ValueError, OSError) as exc:
        return {
            "success": False,
            "ok": False,
            "error": f"The screenshot folder is invalid: {exc}",
        }

    try:
        data = _capture_primary_png()
    except ImportError as exc:
        return {
            "success": False,
            "ok": False,
            "error": (
                "Screenshot capture is unavailable because mss or Pillow is "
                f"not installed: {exc}"),
        }
    except Exception as exc:  # noqa: BLE001 - report the real backend fault
        return {
            "success": False,
            "ok": False,
            "error": f"Screenshot capture failed: {type(exc).__name__}: {exc}",
        }

    if not isinstance(data, (bytes, bytearray)) or not data:
        return {
            "success": False,
            "ok": False,
            "error": "Screenshot capture returned no image data.",
        }
    data = bytes(data)
    if not data.startswith(PNG_SIGNATURE):
        return {
            "success": False,
            "ok": False,
            "error": "Screenshot capture returned an invalid PNG image.",
        }

    stamp = _timestamp_slug()
    for attempt in range(1, MAX_FILENAME_ATTEMPTS + 1):
        suffix = "" if attempt == 1 else f"_{attempt}"
        name = f"Screenshot_{stamp}{suffix}.png"
        path = directory / name
        try:
            _write_exclusive(path, data)
        except FileExistsError:
            continue
        except PermissionError as exc:
            return {
                "success": False,
                "ok": False,
                "error": f"Windows denied permission to save '{path}': {exc}",
            }
        except OSError as exc:
            return {
                "success": False,
                "ok": False,
                "error": f"Could not save the screenshot: {exc}",
            }
        if not _verify_png(path, data):
            try:
                os.unlink(path)
            except OSError:
                pass
            return {
                "success": False,
                "ok": False,
                "error": (
                    f"The screenshot was written but did not verify at "
                    f"'{path}', so it was removed."),
            }
        try:
            absolute = str(path.resolve())
        except OSError:
            absolute = str(path.absolute())
        return {
            "success": True,
            "ok": True,
            "saved_path": absolute,
            "filename": name,
            "message": f"Screenshot saved to {absolute}",
        }

    return {
        "success": False,
        "ok": False,
        "error": "Could not choose a unique screenshot filename.",
    }


__all__ = ["screenshot"]

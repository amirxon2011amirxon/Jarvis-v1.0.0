# actions/web/open_web_tool.py
"""Open a validated web address in Brave.

The assistant resolves what the user means — a site name, product, organisation
or an explicit address — and passes a complete URL here. This module deliberately
does **not** recognise website names, keep a site directory or consult another
model. It validates the address, finds Brave and launches it.

Design
------
``validate_url(url)``
    Accepts ``http://`` and ``https://`` URLs with any valid hostname and TLD,
    including paths, queries and fragments. A bare ``example.com/docs`` is
    treated as ``https://example.com/docs``. Unsupported schemes, embedded
    credentials, whitespace, malformed hosts and bad ports are rejected.
    Nothing is hard-coded about which TLDs exist.

``open_web(url)``
    Launch the validated URL in Brave. The browser is started with an argument
    vector and ``shell=False``; LLM-supplied text is never interpreted by a
    shell. A normal Brave invocation hands the URL to an already-running Brave
    process, so the existing browser session is reused and the page opens in a
    new tab.

Public API
----------
``validate_url(url)`` -> ``(normalised_url, error)``
``open_web(url)``      -> ``{"success", "url", "opened", "via", "message"}``
                          or ``{"success": False, ..., "error"}``
``get_brave_path()``   -> detected/configured Brave executable, or ``""``

Configuration
-------------
Brave is auto-detected from PATH and the usual Windows install locations. To
use a custom install, set ``brave_path`` in ``config/api_keys.json`` or the
``BRAVE_PATH`` environment variable. Both may be a full path or a command name.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

try:
    from config import get_brave_path as _configured_brave_path
except Exception:  # pragma: no cover - keeps the tool usable as a standalone file
    def _configured_brave_path() -> str:
        return ""


_ALLOWED_SCHEMES = ("http", "https")
_BROWSER_NAME = "Brave"

# A hostname label: 1–63 ASCII letters, digits or hyphens, not starting/ending
# with a hyphen. The TLD is just the last label, so no extension list exists.
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

# A URL with no scheme, e.g. "example.com", "localhost:3080" or
# "example.com/docs?q=1". Only shapes that clearly start with a host are given
# an https:// prefix; anything else is parsed as-is so "javascript:..." and
# "mailto:..." are rejected as schemes rather than mangled into hostnames.
_BARE_URL_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])"
    r"(?::\d{1,5})?(?:[/?#].*)?$",
    re.DOTALL,
)


# ─────────────────────────────────────────────────────────────────────────────
#  URL validation
# ─────────────────────────────────────────────────────────────────────────────
def _valid_host(host: str) -> bool:
    """True for an IP literal, localhost, or a syntactically valid DNS name."""
    name = host.strip()
    if name.endswith("."):                 # a single trailing root dot is valid
        name = name[:-1]
    if not name:
        return False

    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass

    if name.lower() == "localhost":
        return True

    try:
        ascii_name = name.encode("idna").decode("ascii")
    except UnicodeError:
        return False

    if len(ascii_name) > 253:
        return False
    labels = ascii_name.split(".")
    if len(labels) < 2:                    # a public name needs at least one dot
        return False
    return all(_HOST_LABEL_RE.match(label) for label in labels)


def validate_url(url: Any) -> Tuple[str, str]:
    """Return ``(url, "")`` when valid, otherwise ``("", reason)``.

    The returned URL is the original address with surrounding whitespace
    removed; a scheme-less host gets ``https://``. Paths, query strings and
    fragments are never rewritten.
    """
    raw = str(url or "").strip()
    if not raw:
        return "", "No URL was provided."
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in raw):
        return "", "The URL contains whitespace or control characters."
    if "\\" in raw:
        return "", "The URL contains a backslash."

    candidate = raw
    if "://" not in candidate and _BARE_URL_RE.match(candidate):
        candidate = "https://" + candidate

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        return "", f"The URL is malformed: {exc}"

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        shown = parts.scheme or "none"
        return "", f"Only http:// and https:// are supported (got '{shown}')."
    if not parts.netloc:
        return "", "The URL has no host."
    if parts.username is not None or parts.password is not None:
        return "", "URLs containing credentials are not accepted."

    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        return "", f"The URL has an invalid host or port: {exc}"

    if port is not None and not 1 <= port <= 65535:
        return "", "The URL port must be between 1 and 65535."
    if not host or not _valid_host(host):
        return "", f"'{host or parts.netloc}' is not a valid host."

    return candidate, ""


# ─────────────────────────────────────────────────────────────────────────────
#  Brave discovery
# ─────────────────────────────────────────────────────────────────────────────
def _candidate_paths() -> List[str]:
    """Explicit configuration first, then PATH, then standard installs."""
    candidates: List[str] = []

    env_path = (os.getenv("BRAVE_PATH") or "").strip()
    if env_path:
        candidates.append(env_path)

    try:
        configured = (_configured_brave_path() or "").strip()
    except Exception:
        configured = ""
    if configured:
        candidates.append(configured)

    for name in ("brave.exe", "brave", "brave-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    if os.name == "nt":
        program_files = os.getenv("PROGRAMFILES") or r"C:\Program Files"
        program_files_x86 = os.getenv("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"
        local_appdata = os.getenv("LOCALAPPDATA") or ""
        bases = [program_files, program_files_x86]
        if local_appdata:
            bases.append(local_appdata)
        for base in bases:
            candidates.append(os.path.join(
                base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"))

    return candidates


def get_brave_path() -> str:
    """Return the Brave executable to use, or ``""`` when none is found."""
    for candidate in _candidate_paths():
        expanded = os.path.expandvars(os.path.expanduser(
            str(candidate).strip().strip('"').strip("'")))
        if not expanded:
            continue
        try:
            if os.path.isfile(expanded):
                return str(Path(expanded).resolve())
            found = shutil.which(expanded)
            if found:
                return str(Path(found).resolve())
        except OSError:
            continue
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Launching
# ─────────────────────────────────────────────────────────────────────────────
def _launch_brave(executable: str, url: str) -> None:
    """Start Brave with the URL as one argument; never through a shell.

    A plain ``brave.exe <url>`` invocation is handed to an existing Brave
    process by Chromium's single-instance mechanism, which opens the page in a
    new tab and reuses the current profile/session. If Brave is not running it
    opens as usual.
    """
    kwargs: Dict[str, Any] = {"shell": False}
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            kwargs["creationflags"] = flags

    process = subprocess.Popen([executable, url], **kwargs)

    # A process that has already failed before returning from Popen is a launch
    # failure. A normal browser process is still running (None) or has handed
    # the URL to an existing session and exited cleanly (0).
    try:
        code = process.poll()
    except Exception:
        code = None
    if code not in (None, 0):
        raise RuntimeError(f"Brave exited immediately with code {code}.")


# ─────────────────────────────────────────────────────────────────────────────
#  Public tool
# ─────────────────────────────────────────────────────────────────────────────
def open_web(url: str) -> Dict[str, Any]:
    """Open ``url`` in Brave. Never raises; failures are structured results."""
    raw = str(url or "").strip()
    normalised, error = validate_url(raw)
    if error:
        return {
            "success": False,
            "url": raw or "invalid-url",
            "message": "Invalid or unsupported URL.",
            "error": f"Invalid or unsupported URL: {error}",
        }

    try:
        executable = get_brave_path()
    except Exception as exc:
        return {
            "success": False,
            "url": normalised,
            "message": "Website could not be opened.",
            "error": f"Could not locate {_BROWSER_NAME}: {type(exc).__name__}: {exc}",
        }
    if not executable:
        return {
            "success": False,
            "url": normalised,
            "message": "Website could not be opened.",
            "error": (
                "Brave executable was not found. Install Brave, or set "
                "'brave_path' in config/api_keys.json (or the BRAVE_PATH "
                "environment variable) to the full path of brave.exe."
            ),
        }

    try:
        _launch_brave(executable, normalised)
    except Exception as exc:
        return {
            "success": False,
            "url": normalised,
            "message": "Website could not be opened.",
            "error": f"Could not launch {_BROWSER_NAME}: {type(exc).__name__}: {exc}",
        }

    return {
        "success": True,
        "url": normalised,
        "opened": normalised,
        "via": _BROWSER_NAME.lower(),
        "message": "Website opened successfully.",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        _usage = f"python -m {__spec__.name}" if __spec__ else "python <tool file>"
        print(f"Usage: {_usage} <url>")
        print(f'Example: {_usage} "https://claude.ai"')
        raise SystemExit(1)
    print(json.dumps(open_web(" ".join(args)), indent=2, ensure_ascii=False))

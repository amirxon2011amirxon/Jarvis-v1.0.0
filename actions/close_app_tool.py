# actions/computer_control/close_app_tool.py
"""Discover running applications and close one by its exact name.

Design mirrors ``open_app_tool.py`` deliberately:

``list_running_apps()``
    Return the applications that currently have a visible window. This is what a
    person means by "running apps" — Telegram, Chrome, Notepad — not the hundred
    background services Windows runs. Each entry carries its name and window
    titles so the assistant can decide which one the user meant.

``close_app(app_name)``
    Close a name returned by ``list_running_apps``. Matching is exact (case,
    spacing and punctuation insensitive). If the name is not a running app,
    nothing is closed and the assistant is told to list again.

WHY THIS DOES NOT FORCE-KILL
    ``taskkill /PID`` without ``/F`` asks the app to close through its normal
    window message, so an unsaved document can still prompt to save. For
    Explorer and UWP frame windows the tool posts ``WM_CLOSE`` to the specific
    window instead, because killing the shell or the shared frame host would
    take other windows with it. If the app stays open (a save prompt, or an app
    that hides in the tray), the result says so instead of pretending it closed.

The assistant's own process and its ancestors are never listed, so asking to
close "the editor" cannot take down the conversation that runs the tool.

No aliases, no hard-coded process names, no fuzzy matching.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

_IS_WIN = sys.platform.startswith("win")

# Running processes come and go; a short cache lets the two-call sequence
# (list, then close) work without re-enumerating every time, while still being
# fresh enough that a window the user just closed is not listed.
_INDEX_TTL = 5.0
_INDEX: List[Dict[str, Any]] = []
_INDEX_AT = 0.0
_INDEX_LOCK = threading.Lock()

# Shell/input furniture that technically owns a window but is not an app the
# user means when asking to close something.
_SKIP_PROCESSES = frozenset({
    "textinputhost",
    "searchhost",
    "shellexperiencehost",
    "startmenuexperiencehost",
    "windowsinputexperience",
    "inputapp",
    "lockapp",
    "logonui",
    "dwm",
    "sihost",
    "ctfmon",
})

_SKIP_TITLES = frozenset({
    "default ime",
    "msctfime ui",
    "hidden window",
    "program manager",
    "windows input experience",
    "task switching",
})

_NO_WINDOW = 0x08000000 if _IS_WIN else 0
_WM_CLOSE = 0x0010
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

# Never list the process running the assistant, or the terminal/editor it was
# launched from. Otherwise "close the editor" can take down the conversation
# that is executing the command.
_PROTECTED_PIDS: Optional[set[int]] = None


def _protected_pids() -> set[int]:
    global _PROTECTED_PIDS
    if _PROTECTED_PIDS is not None:
        return _PROTECTED_PIDS
    pids = {os.getpid()}
    try:
        import psutil
        for parent in psutil.Process(os.getpid()).parents():
            try:
                name = str(parent.name() or "").casefold()
            except Exception:
                name = ""
            # Explorer is commonly the parent of a double-clicked app. It is
            # the user's file-manager and shell, not Aze's terminal: filtering
            # it out made every File Explorer window disappear from the
            # running-app list and from close_file. Explorer windows are closed
            # with WM_CLOSE, never taskkill, so the shell stays intact.
            if name in ("explorer", "explorer.exe"):
                break
            pids.add(parent.pid)
            if len(pids) >= 8:
                break
    except Exception:
        pass
    _PROTECTED_PIDS = pids
    return pids


# ─────────────────────────────────────────────────────────────────────────────
#  Windows helpers
# ─────────────────────────────────────────────────────────────────────────────
def _norm(text: str) -> str:
    """Case/punctuation-insensitive form used for exact matching."""
    s = (text or "").lower().strip()
    s = re.sub(r"\.(exe|lnk|msc|bat|cmd)$", "", s)
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def _run_powershell_json(command: str, timeout: int = 20) -> Any:
    if not _IS_WIN:
        return None
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, encoding="utf-8-sig",
            errors="replace", timeout=timeout, creationflags=_NO_WINDOW,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return None
        data = json.loads(raw)
        if isinstance(data, dict):
            return [data]
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _collect_windows_powershell() -> List[Dict[str, Any]]:
    """Every visible top-level application window, with its process.

    This is the fallback path: it costs a PowerShell process start (~0.7 s on
    the reference machine). ``_collect_windows_native`` is preferred.
    """
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and "
        "$_.MainWindowTitle } | Select-Object ProcessName, Id, "
        "MainWindowTitle, MainWindowHandle | ConvertTo-Json -Compress"
    )
    data = _run_powershell_json(command)
    if not data:
        return []

    windows: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        process = str(item.get("ProcessName") or "").strip()
        title = str(item.get("MainWindowTitle") or "").strip()
        try:
            pid = int(item.get("Id") or 0)
            hwnd = int(item.get("MainWindowHandle") or 0)
        except (TypeError, ValueError):
            continue
        if not process or not title or pid <= 0 or hwnd <= 0:
            continue
        if pid in _protected_pids():
            continue
        if process.lower() in _SKIP_PROCESSES:
            continue
        if title.lower() in _SKIP_TITLES:
            continue

        # A UWP app is hosted by ApplicationFrameHost; the title is the app's
        # name, while the process name is meaningless to the user. Everywhere
        # else the executable name is the honest, stable name.
        name = title if process.lower() == "applicationframehost" else process
        windows.append({
            "name": name,
            "process": process,
            "pid": pid,
            "hwnd": hwnd,
            "title": title,
        })
    return windows


def _collect_windows_native() -> Optional[List[Dict[str, Any]]]:
    """Enumerate windows through EnumWindows instead of starting PowerShell.

    ``close_file_tool`` already owns the ctypes enumeration. It returns
    ``None`` only when that module cannot be imported/used, so a genuine
    "no visible windows" result stays a cheap empty list rather than paying for
    the fallback process.
    """
    if not _IS_WIN:
        return None
    try:
        from actions import get_tool_module
        native_windows = get_tool_module("close_file")._windows
    except Exception:
        return None
    try:
        raw = native_windows()
    except Exception:
        return None

    out: List[Dict[str, Any]] = []
    for window in raw:
        try:
            process = str(window.get("process") or "").strip()
            title = str(window.get("title") or "").strip()
            pid = int(window.get("pid") or 0)
            hwnd = int(window.get("hwnd") or 0)
        except (TypeError, ValueError):
            continue
        if not process or not title or pid <= 0 or hwnd <= 0:
            continue
        name = title if process.casefold() == "applicationframehost" else process
        out.append({
            "name": name,
            "process": process,
            "pid": pid,
            "hwnd": hwnd,
            "title": title,
        })
    return out


def _collect_windows() -> List[Dict[str, Any]]:
    native = _collect_windows_native()
    if native is not None:
        return native
    return _collect_windows_powershell()


def _group(windows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for win in windows:
        key = _norm(win["name"])
        if not key:
            continue
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "name": win["name"],
                "items": [],
                "titles": [],
                "processes": [],
            }
        if win["title"] not in group["titles"]:
            group["titles"].append(win["title"])
        if win["process"] not in group["processes"]:
            group["processes"].append(win["process"])
        if not any(i["pid"] == win["pid"] and i["hwnd"] == win["hwnd"]
                   for i in group["items"]):
            group["items"].append({
                "pid": win["pid"],
                "hwnd": win["hwnd"],
                "process": win["process"],
            })
    return list(groups.values())


def _index(refresh: bool = False) -> List[Dict[str, Any]]:
    global _INDEX, _INDEX_AT
    with _INDEX_LOCK:
        stale = (time.time() - _INDEX_AT) > _INDEX_TTL
        if refresh or not _INDEX or stale:
            _INDEX = _group(_collect_windows())
            _INDEX_AT = time.time()
        # A shallow copy is enough: callers only read the nested lists.
        return [dict(entry, items=list(entry.get("items", [])),
                     titles=list(entry.get("titles", [])),
                     processes=list(entry.get("processes", [])))
                for entry in _INDEX]


# ─────────────────────────────────────────────────────────────────────────────
#  Discovery
# ─────────────────────────────────────────────────────────────────────────────
def list_running_apps() -> Dict[str, Any]:
    """Return applications with a visible window, for the assistant to choose."""
    if not _IS_WIN:
        return {"count": 0, "apps": [],
                "note": "Closing applications is only implemented on Windows."}

    # This is the discovery call, so it always asks Windows afresh. The short
    # cache is for close_app, which runs immediately after this list.
    apps = _index(refresh=True)
    items = []
    for app in sorted(apps, key=lambda a: a["name"].lower()):
        titles = [_clip(t, 120) for t in app.get("titles", [])[:3]]
        items.append({"name": app["name"], "titles": titles})
    return {"count": len(items), "apps": items}


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def get_running_programs() -> List[Dict[str, Any]]:
    """Detailed catalogue, for callers that need PIDs and window handles."""
    if not _IS_WIN:
        return []
    out: List[Dict[str, Any]] = []
    for app in _index():
        out.append({
            "name": app["name"],
            "titles": list(app.get("titles", [])),
            "processes": list(app.get("processes", [])),
            "pids": [i["pid"] for i in app.get("items", [])],
            "windows": [i["hwnd"] for i in app.get("items", [])],
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Closing
# ─────────────────────────────────────────────────────────────────────────────
def _find_running(app_name: str, refresh: bool = False) -> Optional[Dict[str, Any]]:
    key = _norm(app_name)
    if not key:
        return None
    apps = _index(refresh)
    for app in apps:
        if _norm(app["name"]) == key:
            return app
    # A model may copy the executable name instead of the display name when a
    # UWP title is shown; accepting that exact field is not an alias.
    for app in apps:
        if any(_norm(process) == key for process in app.get("processes", [])):
            return app
    for app in apps:
        if any(_norm(title) == key for title in app.get("titles", [])):
            return app
    return None


def _post_close(hwnd: int) -> bool:
    if not _IS_WIN or not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.PostMessageW(
            int(hwnd), _WM_CLOSE, 0, 0))
    except Exception:
        return False


def _process_alive(pid: int) -> bool:
    if not _IS_WIN or not pid:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        # OpenProcess fails both for a dead PID and for a process this user may
        # not inspect. Reporting "closed" for an access-denied process was a
        # false success; only ERROR_INVALID_PARAMETER proves the PID is gone.
        try:
            error = int(ctypes.windll.kernel32.GetLastError())
        except Exception:
            error = 0
        return error != 87       # ERROR_INVALID_PARAMETER
    try:
        code = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(code)):
            return code.value == _STILL_ACTIVE
        return True
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _window_alive(hwnd: int) -> bool:
    if not _IS_WIN or not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.IsWindow(int(hwnd)))
    except Exception:
        # An unanswerable window query is not proof that the window closed.
        return True


def _request_close(item: Dict[str, Any]) -> None:
    """Ask one window/process to close, gracefully."""
    process = str(item.get("process") or "").lower()
    hwnd = int(item.get("hwnd") or 0)
    pid = int(item.get("pid") or 0)

    # Explorer owns the shell. ApplicationFrameHost owns every hosted UWP app.
    # Sending WM_CLOSE to the one window closes only that window; taskkill on
    # their PID would take unrelated things with it.
    if process in ("explorer", "applicationframehost"):
        _post_close(hwnd)
        return

    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid)],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
        # ``check=False`` means a refused taskkill is a return code, not an
        # exception. The window message is the fallback for that case too,
        # otherwise an app that taskkill cannot reach is only reported as
        # "closing" without ever receiving the graceful request.
        if getattr(result, "returncode", 0) not in (0, None):
            _post_close(hwnd)
    except Exception:
        _post_close(hwnd)


def _still_open(item: Dict[str, Any]) -> bool:
    process = str(item.get("process") or "").lower()
    if process in ("explorer", "applicationframehost"):
        return _window_alive(int(item.get("hwnd") or 0))
    return _process_alive(int(item.get("pid") or 0))


def close_app(app_name: str) -> Dict[str, Any]:
    """Close a running application by exact name. Never raises.

    Returns ``{"closed": name}`` when the window/process is gone, or a
    ``{"closing": ...}`` result when the app was asked to close but is still
    running (usually a save prompt or a tray app).
    """
    try:
        if not _IS_WIN:
            return {"error": "Closing applications is only implemented on Windows."}

        wanted = str(app_name or "").strip()
        if not wanted:
            return {"error": "No application name was provided."}

        entry = _find_running(wanted)
        if entry is None:
            # The cache is deliberately short, but a model can take a while to
            # answer; one forced refresh avoids a false "not running".
            entry = _find_running(wanted, refresh=True)
        if entry is None:
            return {
                "error": (
                    f"No running app is named '{wanted}'. Call "
                    "list_running_apps to see every open app, choose the best "
                    "match yourself, then call close_app with that exact name."
                )
            }

        name = entry["name"]
        items = entry.get("items", [])
        seen = set()
        unique = []
        for item in items:
            key = (item.get("pid"), item.get("hwnd"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)

        for item in unique:
            _request_close(item)

        # Give the app a moment to close or to show its save prompt.
        deadline = time.time() + 2.0
        remaining = []
        while time.time() < deadline:
            remaining = [item for item in unique if _still_open(item)]
            if not remaining:
                break
            time.sleep(0.15)

        if remaining:
            processes = sorted({str(i.get("process") or "") for i in remaining})
            return {
                "closing": name,
                "still_running": processes,
                "note": (
                    "The app was asked to close but is still running. It may be "
                    "waiting for confirmation, saving a document, or hiding in "
                    "the notification area."
                ),
            }

        return {"closed": name}
    except Exception as exc:  # pragma: no cover - defensive, never crash a call
        return {"error": str(exc)}
    finally:
        # The running-app snapshot is no longer trustworthy after a close.
        global _INDEX_AT
        _INDEX_AT = 0.0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args and args[0].lower() in ("list", "list_running", "discover"):
        print(json.dumps(list_running_apps(), indent=2, ensure_ascii=False))
    elif args:
        print(json.dumps(close_app(" ".join(args)), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(get_running_programs(), indent=2, ensure_ascii=False))

# actions/computer_control/close_file_tool.py
"""Close one open file or folder window without closing unrelated work.

What this tool does
-------------------
``close_file(path="", name="", app="", all_matches=False)``
    Find the open window that belongs to the requested file or folder and ask
    that **one window** to close through the normal window message
    (``WM_CLOSE``). Windows then does what clicking the X does: the application
    keeps its save prompt, so unsaved work is never discarded silently. The
    result says whether the window actually disappeared or only whether the
    close was initiated.

How the target is identified
----------------------------
* ``path`` — the exact file or folder. File Explorer windows are matched by
  their real folder path through the shell's own window collection, so two
  folders with the same name in different places are told apart. Applications
  are matched by the file name appearing as a whole name in their window title
  ("report.xlsx - Excel"), never by a substring of a longer file name.
* ``name`` — a literal file name, folder name or window-title fragment.
* ``app`` — narrows the candidates by application ("Excel", "Notepad",
  "File Explorer", "VLC", ...). Unknown names match the process name.
* No arguments — the most recently opened file/folder, as recorded by
  ``open_file_tool`` when Aze itself opened it.

There is no fuzzy matching, no similarity scoring and no typo correction: a
name either appears in the window title or it does not.

What is deliberately not done
-----------------------------
* **No process termination.** ``taskkill``/``TerminateProcess`` never run
  here. Killing the process behind a document would take every other document
  in that application with it and could discard unsaved work.
* **No closing a whole multi-document window.** Editors and browsers keep
  several documents in one window (VS Code tabs, browser tabs, Acrobat tabs,
  Notepad++). Closing such a window would close unrelated work, so the tool
  refuses and says why instead of guessing.
* **No foreground-window guessing.** The focused window is not assumed to be
  the target; if several windows match and the request does not say "all",
  the result lists them and closes nothing.

Limitations
-----------
Windows has no universal "close this document" API. Office documents, classic
editors, media players, archive managers, image viewers and Explorer windows
each own one top-level window per open item and close normally through
``WM_CLOSE``. Applications that multiplex documents into one window expose no
safe external per-document close and are reported as unsupported.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from typing import Any, Dict, List

_IS_WIN = sys.platform.startswith("win")

_WM_CLOSE = 0x0010
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Reuse the app-closing tool's furniture filters and the assistant's own
# process protection: closing Aze's window must never be possible from here.
# Resolved lazily by stable tool name, because the tool's category folder is
# user-renamable and must not be baked into an import path.
def _close_app_helpers():
    try:
        from actions import get_tool_module
        module = get_tool_module("close_app")
    except Exception:  # pragma: no cover - defensive, standalone use
        module = None
    if module is not None:
        return (module._SKIP_PROCESSES, module._SKIP_TITLES,
                module._protected_pids)
    return (frozenset(), frozenset(), lambda: {os.getpid()})

# Shell surfaces that own windows but are not open documents.
_SKIP_CLASSES = frozenset({
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "TrayNotifyWnd",
    "Progman",
    "WorkerW",
    "Windows.UI.Core.CoreWindow",
    "ApplicationManager_DesktopShellWindow",
    "DV2ControlHost",
    "MsgrIMEWindowClass",
    "MSCTFIME UI",
})

# Applications that keep several documents in ONE top-level window. Closing
# that window would close documents the user did not ask about, and Windows
# exposes no safe external per-document close, so those are refused with an
# explanation (value) rather than closed.
_MULTI_DOCUMENT_PROCESSES = {
    "code": "Visual Studio Code keeps several files in one window",
    "notepad++": "Notepad++ keeps several files in one window",
    "sublime_text": "Sublime Text keeps several files in one window",
    "chrome": "the browser keeps pages in tabs",
    "msedge": "the browser keeps pages in tabs",
    "brave": "the browser keeps pages in tabs",
    "firefox": "the browser keeps pages in tabs",
    "opera": "the browser keeps pages in tabs",
    "vivaldi": "the browser keeps pages in tabs",
    "acrobat": "Adobe Acrobat can keep several PDFs in one window",
    "acrord32": "Adobe Acrobat Reader can keep several PDFs in one window",
}

# Application words the assistant may pass in ``app`` mapped to the executable
# name Windows reports. Every other value is compared to the process name
# literally.
_APP_ALIASES = {
    "excel": ("excel",),
    "microsoft excel": ("excel",),
    "word": ("winword",),
    "microsoft word": ("winword",),
    "powerpoint": ("powerpnt",),
    "microsoft powerpoint": ("powerpnt",),
    "outlook": ("outlook",),
    "onenote": ("onenote",),
    "notepad": ("notepad",),
    "paint": ("mspaint", "paintapp"),
    "explorer": ("explorer",),
    "file explorer": ("explorer",),
    "visual studio code": ("code",),
    "vs code": ("code",),
    "vscode": ("code",),
    "code": ("code",),
    "acrobat": ("acrobat", "acrord32"),
    "adobe acrobat": ("acrobat", "acrord32"),
    "acrobat reader": ("acrobat", "acrord32"),
    "sumatra": ("sumatrapdf",),
    "sumatra pdf": ("sumatrapdf",),
    "vlc": ("vlc",),
    "winrar": ("winrar",),
    "7-zip": ("7zfm",),
    "7zip": ("7zfm",),
    "bandizip": ("bandizip",),
    "peazip": ("peazip",),
    "windows media player": ("wmplayer",),
    "media player": ("wmplayer", "applicationframehost"),
    "photos": ("applicationframehost", "photos"),
}

# Give a closing window enough time to disappear before calling it initiated.
_CLOSE_TIMEOUT = 2.5
_POLL_INTERVAL = 0.12


# ─────────────────────────────────────────────────────────────────────────────
#  Windows discovery
# ─────────────────────────────────────────────────────────────────────────────
def _process_name(pid: int) -> str:
    """The executable name for a PID, lower-case and without ``.exe``."""
    if not _IS_WIN or not pid:
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.splitext(os.path.basename(buffer.value))[0].lower()
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _window_title(hwnd: int) -> str:
    try:
        length = ctypes.windll.user32.GetWindowTextLengthW(int(hwnd))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(int(hwnd), buffer, length + 1)
        return str(buffer.value or "").strip()
    except Exception:
        return ""


def _windows() -> List[Dict[str, Any]]:
    """Every visible, titled top-level window with its process.

    Deliberately not ``Get-Process``/``MainWindowHandle``: that reports only
    one window per process, and a second Excel workbook or Explorer window is
    exactly what this tool has to tell apart.
    """
    if not _IS_WIN:
        return []

    found: List[Dict[str, Any]] = []
    skip_processes, skip_titles, protected_pids = _close_app_helpers()
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return []

    prototype = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _lparam):
        try:
            hwnd = int(hwnd)
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _window_title(hwnd)
            if not title:
                return True
            if title.casefold() in skip_titles:
                return True

            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value in _SKIP_CLASSES:
                return True

            try:
                if user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
                    return True
            except Exception:
                pass

            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pid = int(pid.value)
            if not pid or pid in protected_pids():
                return True

            process = _process_name(pid)
            if not process or process in skip_processes:
                return True

            found.append({
                "hwnd": hwnd,
                "title": title,
                "process": process,
                "pid": pid,
            })
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(prototype(visit), 0)
    except Exception:
        return []
    return found


def _explorer_windows() -> List[Dict[str, Any]]:
    """Every open File Explorer window with its real folder path.

    The address bar's ``LocationURL`` is used rather than the window title:
    Windows shows a *localized* display name for shell folders, so the title
    cannot be matched against the English name the user and the model use.
    The URL always carries the real path. Failure to reach COM is not fatal — callers fall back to display-name
    matching.
    """
    if not _IS_WIN:
        return []
    try:
        import comtypes
        import comtypes.client

        try:
            comtypes.CoInitializeEx()
        except Exception:
            pass

        shell = comtypes.client.CreateObject("Shell.Application")
        collection = shell.Windows()
        count = int(collection.Count)
        out: List[Dict[str, Any]] = []
        for index in range(count):
            try:
                window = collection.Item(index)
                hwnd = int(window.HWND)
                path = _path_from_url(str(window.LocationURL or ""))
            except Exception:
                continue
            if hwnd and path:
                out.append({"hwnd": hwnd, "path": path})
        return out
    except Exception:
        return []


def _path_from_url(url: str) -> str:
    """The filesystem path behind a ``file:///`` address-bar URL."""
    if not url or not url.lower().startswith("file:"):
        return ""
    try:
        from urllib.parse import unquote, urlparse
        from urllib.request import url2pathname
        parsed = urlparse(url)
        return os.path.normpath(url2pathname(unquote(parsed.path)))
    except Exception:
        return ""


def _localized_name(path: str) -> str:
    """The name Windows displays for a folder, in the system language.

    Shared with the open tool's shell-folder resolution so both tools agree on
    what a folder is called in the system language.
    """
    try:
        from actions import get_tool_module
        _display_name_for = get_tool_module("open_file")._display_name_for
        return _display_name_for(path)
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Matching — literal only
# ─────────────────────────────────────────────────────────────────────────────
def _norm(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _as_bool(value: Any, default: bool = False) -> bool:
    """Strict boolean for a model-supplied argument.

    A truthy string such as ``"false"`` used to enable ``all_matches`` and
    could close every matching window instead of asking which one was meant.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
    return default


def _same_path(first: str, second: str) -> bool:
    try:
        return os.path.normcase(os.path.normpath(first)) == \
            os.path.normcase(os.path.normpath(second))
    except Exception:
        return False


def _title_contains(title: str, text: str) -> bool:
    """A plain literal substring test (case-insensitive)."""
    needle = str(text or "").strip().casefold()
    return bool(needle) and needle in str(title or "").casefold()


def _title_has_name(title: str, name: str) -> bool:
    """Whether ``name`` appears as a whole file name in a window title.

    ``report.xlsx`` matches ``report.xlsx - Excel`` and ``report.xlsx* - ...``
    but not ``report.xlsx.bak - Notepad``, which a plain substring test would
    wrongly accept.
    """
    needle = str(name or "").strip().casefold()
    if not needle:
        return False
    haystack = str(title or "").casefold()
    separators = set(" \t\\/()[]{}<>\"'*|,")

    def _boundary(char: str) -> bool:
        # A title separator, never a name character: "report.xlsx - Excel"
        # and "(report.xlsx)" match, "my-report.xlsx" and
        # "report.xlsx.bak" do not.
        return not char or char.isspace() or char in separators

    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        before = haystack[index - 1] if index else ""
        after_index = index + len(needle)
        after = haystack[after_index] if after_index < len(haystack) else ""

        if _boundary(before) and _boundary(after):
            return True
        start = index + 1


def _app_matches(window: Dict[str, Any], app: str) -> bool:
    wanted = _norm(app)
    if not wanted:
        return True
    process = _norm(window.get("process"))
    candidates = _APP_ALIASES.get(wanted)
    if candidates is not None:
        # A known application name is authoritative: matching its word inside
        # an unrelated title ("Password manager - Chrome" for "Word") used to
        # be able to close the wrong window.
        return process in candidates
    if process == wanted:
        return True
    # UWP apps all share ApplicationFrameHost; their only usable identity is
    # the window title (which is the app name).
    if process == "applicationframehost":
        return wanted in _norm(window.get("title"))
    return False


def _multi_document_reason(process: str) -> str:
    return _MULTI_DOCUMENT_PROCESSES.get(str(process or "").lower(), "")


def _known_folder_path(text: str) -> str:
    """A shell-folder word ("Music", "Downloads") as its real folder path."""
    try:
        from actions import get_tool_module
        _known_location = get_tool_module("open_file")._known_location
        return _known_location(text)
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Closing
# ─────────────────────────────────────────────────────────────────────────────
def _post_close(hwnd: int) -> bool:
    """Ask one window to close normally. Returns whether Windows accepted it."""
    if not _IS_WIN or not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.PostMessageW(
            int(hwnd), _WM_CLOSE, 0, 0))
    except Exception:
        return False


def _window_alive(hwnd: int) -> bool:
    if not _IS_WIN or not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.IsWindow(int(hwnd)))
    except Exception:
        # If the question cannot be answered, do not claim the close worked.
        return True


def _await_closed(hwnd: int, timeout: float = _CLOSE_TIMEOUT) -> bool:
    """Wait for one window to disappear; True when it did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _window_alive(hwnd):
            return True
        time.sleep(_POLL_INTERVAL)
    return not _window_alive(hwnd)


def _display(hwnd: int, title: str = "") -> str:
    title = title or _window_title(hwnd)
    return title or f"window {hwnd}"


def _candidates(path: str, name: str, app: str) -> List[Dict[str, Any]]:
    """Open windows that match the requested target, exactly.

    For a folder path the shell's folder collection is authoritative; the
    title is only a fallback when COM is unavailable. For a file path the
    application window must carry the exact file name as a whole name.
    """
    windows = _windows()
    explorer = _explorer_windows() if path else []
    matches: List[Dict[str, Any]] = []
    seen: set = set()

    def add(window: Dict[str, Any], kind: str) -> None:
        if window["hwnd"] in seen:
            return
        seen.add(window["hwnd"])
        matches.append({
            "hwnd": window["hwnd"],
            "title": window.get("title") or "",
            "process": window.get("process") or "",
            "pid": int(window.get("pid") or 0),
            "kind": kind,
        })

    if path:
        base = os.path.basename(str(path).rstrip("\\/"))
        is_folder = os.path.isdir(path) or not os.path.splitext(base)[1]
        if is_folder:
            for entry in explorer:
                if _same_path(entry["path"], path):
                    window = next(
                        (w for w in windows if w["hwnd"] == entry["hwnd"]),
                        {"hwnd": entry["hwnd"], "title": base,
                         "process": "explorer", "pid": 0},
                    )
                    if _app_matches(window, app):
                        add(window, "folder")
        else:
            # A file is never open as an Explorer window; a similarly named
            # folder window must not be mistaken for it.
            windows = [w for w in windows if w["process"] != "explorer"]
        # Shell folders are displayed under a localized name, while the
        # request uses the English/real name. Both count for the fallback when
        # the address-bar path was unavailable.
        names = [base]
        if is_folder:
            localized = _localized_name(path)
            if localized and localized.casefold() != base.casefold():
                names.append(localized)
        for window in windows:
            if window["hwnd"] in seen:
                continue
            # When the shell's exact-path collection worked, title matching is
            # not needed for folder windows — and would risk the wrong folder.
            if is_folder and window["process"] == "explorer" and explorer:
                continue
            if any(name and _title_has_name(window["title"], name)
                   for name in names) and _app_matches(window, app):
                add(window, "folder" if is_folder else "file")
    elif name:
        for window in windows:
            if _title_contains(window["title"], name) and \
                    _app_matches(window, app):
                add(window, "unknown")
    elif app:
        # "Close File Explorer" with no name: that application's windows are
        # the candidates, and the usual one-match/ambiguous rules apply.
        for window in windows:
            if _app_matches(window, app):
                add(window, "unknown")

    return matches


def _failure(operation: str, message: str, reason: str,
             **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "operation": operation,
        "reason": reason,
        "message": message,
        "error": message,
    }
    result.update(extra)
    return result


def _close_one(candidate: Dict[str, Any]) -> str:
    """Close one candidate. Returns ``closed``, ``initiated`` or ``failed``."""
    if not _post_close(candidate["hwnd"]):
        return "failed"
    return "closed" if _await_closed(candidate["hwnd"]) else "initiated"


def close_file(path: Any = "", name: Any = "", app: Any = "",
               all_matches: bool = False) -> Dict[str, Any]:
    """Close the open window for one file or folder, or every match if asked.

    Never terminates a process, never closes a window that hosts several
    documents, and never claims a close that did not happen. Unsaved work
    stays protected by the application's own save prompt.
    """
    operation = "close_file"
    if not _IS_WIN:
        return _failure(operation,
                        "Closing files and folders is only implemented on "
                        "Windows.", "unsupported_platform")

    all_matches = _as_bool(all_matches, False)
    target_path = str(path or "").strip().strip('"\'')
    target_name = str(name or "").strip().strip('"\'')
    target_app = str(app or "").strip()

    # A shell-folder word means its real folder ("Music" -> C:\Users\...\Music).
    # Windows displays a localized title, so the name alone could never match
    # that window; the path can.
    if not target_path and target_name:
        known = _known_folder_path(target_name)
        if known:
            target_path = known
            target_name = ""

    # Nothing specified at all: fall back to what Aze itself opened most
    # recently. With only an app given, that app's windows are the target.
    used_recent = False
    if not target_path and not target_name and not target_app:
        try:
            from actions import get_tool_module
            recently_opened = get_tool_module("open_file").recently_opened
            for entry in recently_opened(5):
                candidate_path = str(entry.get("path") or "")
                if not candidate_path:
                    continue
                if _candidates(candidate_path, "", target_app):
                    target_path = candidate_path
                    used_recent = True
                    break
        except Exception:
            pass
        if not target_path:
            return _failure(
                operation,
                "Tell me which file or folder to close — its name or exact "
                "path. Any open item works: for example 'report.xlsx', "
                "'main.py', 'Projects', or 'Music'. I could not find a "
                "recently opened item to close.",
                "no_target")

    candidates = _candidates(target_path, target_name, target_app)
    label = target_path or target_name or target_app or "that item"

    if not candidates:
        return _failure(
            operation,
            f"'{label}' does not appear to be open in any window right now.",
            "not_found",
            path=target_path,
            name=target_name,
            used_recent=used_recent)

    # One match is the normal case; several need either an explicit "all"
    # request or a more precise target.
    if len(candidates) > 1 and not all_matches:
        return _failure(
            operation,
            f"More than one open window matches '{label}'. Ask the user which "
            "one they mean, or pass the exact file path, then call again. "
            "Nothing was closed.",
            "ambiguous",
            path=target_path,
            name=target_name,
            candidates=[{"title": item["title"],
                         "app": item["process"],
                         "kind": item["kind"]} for item in candidates[:8]])

    # Refuse windows that host several documents: closing one would close
    # unrelated work.
    eligible: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    for candidate in candidates:
        reason = _multi_document_reason(candidate["process"])
        if reason:
            refused.append({"title": candidate["title"],
                            "app": candidate["process"],
                            "reason": reason})
        else:
            eligible.append(candidate)

    if not eligible:
        first = refused[0]
        return _failure(
            operation,
            f"'{first['title']}' cannot be closed on its own: "
            f"{first['reason']}, so closing the window would close documents "
            "the user did not ask about. Nothing was closed.",
            "unsupported",
            path=target_path,
            name=target_name,
            refused=refused)

    closed: List[Dict[str, Any]] = []
    initiated: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for candidate in eligible:
        outcome = _close_one(candidate)
        record = {
            "title": candidate["title"] or _display(candidate["hwnd"]),
            "app": candidate["process"],
            "kind": candidate["kind"],
        }
        if outcome == "closed":
            closed.append(record)
        elif outcome == "initiated":
            initiated.append(record)
        else:
            failed.append(record)

    names_closed = [item["title"] for item in closed]
    names_initiated = [item["title"] for item in initiated]

    if not closed and not initiated:
        return _failure(
            operation,
            "Windows refused the close request for "
            + ", ".join(item['title'] for item in failed) + ".",
            "close_failed",
            path=target_path,
            name=target_name,
            failed=failed)

    if initiated and not closed and not failed:
        # The window is still there: almost always the application's save or
        # confirmation prompt. Report honestly that the close is not confirmed.
        return _failure(
            operation,
            f"Asked {', '.join(names_initiated)} to close. The window is "
            "still open, most likely waiting for the user to save or confirm. "
            "Do not say it is closed yet.",
            "close_initiated",
            path=target_path,
            name=target_name,
            closing=(names_initiated[0] if len(names_initiated) == 1
                     else names_initiated),
            used_recent=used_recent)

    single = len(closed) + len(initiated) + len(failed) == 1
    message = (
        f"Closed {', '.join(names_closed)}." if names_closed
        else "The close was requested.")
    if names_initiated:
        message += (f" Asked {', '.join(names_initiated)} to close, but the "
                    "window is still open (it may be waiting to save).")
    if failed:
        message += (" Could not close "
                    + ", ".join(item['title'] for item in failed) + ".")
    if refused:
        message += (" Left open: "
                    + "; ".join(item["title"] + " (" + item["reason"] + ")"
                                for item in refused) + ".")

    result: Dict[str, Any] = {
        "success": bool(closed) and not initiated and not failed and not refused,
        "operation": operation,
        "path": target_path,
        "name": target_name,
        "closed": (names_closed[0] if single and names_closed
                   else names_closed),
        "item_type": eligible[0]["kind"] if single else "mixed",
        "via": "window close",
        "used_recent": used_recent,
        "message": message.strip(),
    }
    if not result["success"]:
        result["error"] = message.strip()
    if names_initiated:
        result["closing"] = (names_initiated[0] if len(names_initiated) == 1
                             else names_initiated)
    if failed:
        result["failed"] = failed
    if refused:
        result["refused"] = refused
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _usage = (f"python -m {__spec__.name}" if __spec__ else "python <tool file>")
    arguments = [part for part in sys.argv[1:] if not part.startswith("--")]
    if not arguments:
        print(f"Usage: {_usage} <file-or-folder> [--all]")
        raise SystemExit(1)
    command = " ".join(arguments)
    outcome = close_file(
        path=command if os.path.exists(command) else "",
        name="" if os.path.exists(command) else command,
        all_matches="--all" in sys.argv)
    print(json.dumps(outcome, indent=2, ensure_ascii=False))

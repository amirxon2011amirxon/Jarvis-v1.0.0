# actions/computer_control/open_file_tool.py
"""Find, open and locate local files and folders on Windows.

The assistant resolves what the user means — a file name, a kind of document, a
folder, or a full path — and calls one of the operations below. This module does
**not** guess filenames, keep a database, call another model or consult a list of
file types. It searches the folders that actually exist, validates what it
found, and hands the path to Windows.

Public API
----------
``search_files(query, ...)``
    Search the user's usual folders (or one explicit location) by name, part of
    a name, extension, category, kind and modification time — widening once to
    every drive's top levels when the usual folders hold nothing. Every
    criterion is optional: a location and item type is a complete request, and
    a category word ("videos", "documents", "notebooks") stands in for a list
    of extensions. Returns candidate paths with deterministic match labels
    (exact, partial, similar, kind) so the assistant can choose, pick randomly,
    take the newest, or ask. Names are matched case-insensitively and with a
    conservative similarity pass, so typos and separator/case differences are
    shown as candidates instead of being silently missed. Nothing is opened.

``open_file(path, app="", confirm=False)``
    Open one existing file with its registered Windows application, or with an
    application the user named. Executables, installers, scripts and shortcuts
    return ``approval_required`` until the local dashboard approves that exact
    path; the old ``confirm`` argument is ignored.

``open_folder(path, app="")``
    Open one existing folder in File Explorer, or in a named application (a
    project directory in VS Code, for example).

``open_containing_folder(path)``
    Open the folder that contains a file and select the file in Explorer. For a
    folder, the folder itself is selected in its parent — stated in the result,
    never an accident.

``validate_path(path, expect)``
    The shared check behind all three: resolve, exist, type, access. It is a
    helper, not an LLM tool, and it never promises that the later operation will
    succeed — launch failures are handled where they happen.

Why it is built this way
------------------------
* **Windows decides the application — with an installed fallback behind it.**
  Opening a document uses the registered association through ``os.startfile``.
  When Windows has no association for the type, or the associated application
  will not launch, an installed application that fits the file's category is
  tried: pictures to an image app, video and audio to a media player,
  archives to an archive manager, PDF, SVG and web files to a browser, text
  and source files to an editor. Candidates are discovered through the
  registry's ``App Paths`` key, known install locations, the browsers
  ``open_web`` already finds, and the application catalogue ``open_app``
  maintains — existence is always checked, never assumed, and results are
  cached until ``refresh_app_cache()``. There is no shell string built from
  user text, and an unfamiliar extension is not rejected merely for being
  unfamiliar.
* **Search stays bounded.** The user's real folders are searched first and,
  only when they hold nothing, the search widens once to the top levels of the
  fixed drives; symlinks and junctions are not followed; hidden and machine
  folders are skipped unless asked for; a time budget and a result limit keep
  the assistant responsive.
* **Nothing destructive.** This module only opens and reveals. It never deletes,
  moves, renames, edits, extracts or installs, and it never runs a command built
  from a filename.
* **Same conventions as the other tools.** Results are plain dictionaries with
  ``success``/``operation``/``path``/``message``/``error`` fields; every failure
  is structured and nothing raises out of a tool call.
"""

from __future__ import annotations

import ctypes
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

try:                                       # stdlib on Windows only
    import winreg
except ImportError:                        # pragma: no cover - non-Windows
    winreg = None

_IS_WIN = sys.platform.startswith("win")

# ``os.startfile`` only exists on Windows. Keeping the reference at module level
# means the launch path has exactly one seam, which the tests replace.
_startfile = getattr(os, "startfile", None)

try:                                       # the app's own settings module
    from config import BASE_DIR, load_api_keys
except Exception:                          # pragma: no cover - standalone use
    BASE_DIR = Path(__file__).resolve().parent.parent

    def load_api_keys() -> Dict[str, Any]:
        return {}

from core import approvals as _approvals

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ─────────────────────────────────────────────────────────────────────────────
#  Search tuning
# ─────────────────────────────────────────────────────────────────────────────
# A search is a tool call inside a conversation, so it has to answer quickly.
# These bounds are what keep "find my project" from walking an entire disk.
_SEARCH_BUDGET = 6.0            # seconds of filesystem work per call
_MAX_DEPTH = 6                  # folder levels below each root
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100

# Wider second pass. When the user's usual folders have nothing, the search is
# widened once to the root of every fixed and removable drive — levels below the
# drive root, not a full-disk walk. That is what finds a file or folder created
# somewhere new ("D:\\Code\\snippets") without paying for a whole-disk scan on
# every request; a named ``location`` searches exactly where the user pointed.
_EXPAND_BUDGET = 6.0
_EXPAND_DEPTH = 4

# Folders that never contain the user's own documents but do contain hundreds of
# thousands of files. Hidden folders (``.git``, ``.venv``) are handled by the
# hidden-attribute rule instead of by name.
_SKIP_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", "site-packages", "dist-packages",
    "windows", "winsxs", "windowssystemapps", "windowsapps",
    "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
})

_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# Nothing here is an allowlist of what can be opened; it only separates ordinary
# documents from the files that *execute* something when Windows opens them.
_INSTALLER_EXTS = frozenset({
    ".msi", ".msix", ".msixbundle", ".appx", ".appxbundle",
})
_EXECUTABLE_EXTS = frozenset({
    ".exe", ".com", ".scr", ".pif", ".cpl", ".bat", ".cmd",
    ".msc", ".scf", ".jar", ".application", ".appref-ms", ".xbap",
    ".jnlp", ".gadget", ".diagcab",
})
_SCRIPT_EXTS = frozenset({
    ".ps1", ".ps2", ".psd1", ".psm1", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".reg", ".hta", ".py", ".pyw",
    ".sh", ".bash", ".zsh", ".fish", ".command",
    ".msh", ".msh1", ".msh2", ".mshxml",
})
_SHORTCUT_EXTS = frozenset({".lnk", ".url", ".website"})
# Macro-enabled Office documents execute VBA when the editor opens them.
_MACRO_EXTS = frozenset({
    ".docm", ".dotm", ".xlsm", ".xltm", ".xlam", ".pptm", ".potm",
    ".ppam", ".sldm",
})

# A tool call can be interrupted; ``request_cancel()`` is the seam for that.
_CANCEL = threading.Event()


class _SearchTimeout(Exception):
    """The search budget ran out before every folder was visited."""


# ─────────────────────────────────────────────────────────────────────────────
#  Windows shells and known folders
# ─────────────────────────────────────────────────────────────────────────────
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# Shell folder ids. OneDrive redirects Desktop, Documents and Downloads, so the
# shell's answer is the only reliable one; guessing ``%USERPROFILE%\\Documents``
# would search the wrong place on a redirected account.
#
# Each entry is either a CSIDL for ``SHGetFolderPathW`` or a FOLDERID GUID for
# ``SHGetKnownFolderPath`` — Downloads arrived with Vista and has no CSIDL. The
# CSIDL route is the older but more portable one: it answers even when the
# family of GUID constants is unavailable, which is what a machine without the
# newer shell helpers would hit.
_KNOWN_FOLDERS = {
    "desktop": ("csidl", 0x10, "Desktop"),          # CSIDL_DESKTOPDIRECTORY
    "documents": ("csidl", 0x05, "Documents"),      # CSIDL_PERSONAL
    "downloads": ("guid", "374DE290-123F-4565-9164-39C4925E467B", "Downloads"),
    "pictures": ("csidl", 0x27, "Pictures"),        # CSIDL_MYPICTURES
    "music": ("csidl", 0x0D, "Music"),              # CSIDL_MYMUSIC
    "videos": ("csidl", 0x0E, "Videos"),            # CSIDL_MYVIDEO
}

# Known-folder resolution goes through the shell and never changes during a
# process unless the OS profile is redirected on the fly. Cache successful
# answers; misses are retried so a folder created later can appear.
_HOME_FOLDER_CACHE: Dict[str, str] = {}
_HOME_FOLDER_LOCK = threading.RLock()
_LOCAL_DRIVES_CACHE: List[str] = []
_LOCAL_DRIVES_AT = 0.0
_LOCAL_DRIVES_LOCK = threading.Lock()
_DRIVES_TTL = 30.0


def _known_folder(guid_text: str) -> str:
    """Resolve a Windows known folder by GUID, or ``""`` when the shell cannot."""
    if not _IS_WIN:
        return ""
    try:
        parsed = uuid.UUID(guid_text)
        guid = _GUID(
            parsed.fields[0], parsed.fields[1], parsed.fields[2],
            (ctypes.c_ubyte * 8)(*parsed.bytes[8:]))
        pointer = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(pointer))
        if result != 0 or not pointer.value:
            return ""
        value = str(pointer.value)
        ctypes.windll.ole32.CoTaskMemFree(
            ctypes.cast(pointer, ctypes.c_void_p))
        return value
    except Exception:
        return ""


def _csidl_folder(csidl: int) -> str:
    """Resolve a shell folder by CSIDL, or ``""`` when the shell cannot."""
    if not _IS_WIN:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(260)
        result = ctypes.windll.shell32.SHGetFolderPathW(
            None, csidl, None, 0, buffer)
        if result != 0 or not buffer.value:
            return ""
        return str(buffer.value)
    except Exception:
        return ""


def _home_folder(key: str) -> str:
    """The shell's folder, with an environment fallback for odd setups."""
    # Screenshots is derived from Pictures, which callers/tests can patch. Do
    # not cache the derived path; the underlying Pictures lookup is cached.
    if key == "screenshots":
        return _resolve_home_folder(key)

    with _HOME_FOLDER_LOCK:
        cached = _HOME_FOLDER_CACHE.get(key)
    if cached is not None:
        return cached

    result = _resolve_home_folder(key)
    if result:
        with _HOME_FOLDER_LOCK:
            _HOME_FOLDER_CACHE[key] = result
    return result


def _resolve_home_folder(key: str) -> str:
    if key == "screenshots":
        # Windows has no reliably registered Screenshots known folder on every
        # build, so derive it from the resolved Pictures folder: that is where
        # Windows puts it by default and where a OneDrive redirect carries it.
        pictures = _home_folder("pictures")
        candidates = []
        if pictures:
            candidates.append(os.path.join(pictures, "Screenshots"))
        try:
            candidates.append(str(Path.home() / "Pictures" / "Screenshots"))
            candidates.append(
                str(Path.home() / "OneDrive" / "Pictures" / "Screenshots"))
        except Exception:
            pass
        for candidate in candidates:
            if os.path.isdir(candidate):
                return candidate
        return ""

    kind, value, folder_name = _KNOWN_FOLDERS[key]
    known = _csidl_folder(value) if kind == "csidl" else _known_folder(value)
    if known and os.path.isdir(known):
        return known
    try:
        candidate = Path.home() / folder_name
        if candidate.is_dir():
            return str(candidate)
    except Exception:
        pass
    return ""


def _configured_roots() -> List[str]:
    """Extra search roots from ``config/api_keys.json`` (``file_search_roots``).

    The app's settings file already holds every user preference; a project
    folder the user wants searched belongs there too. A missing or malformed
    value simply means "no extra roots".
    """
    try:
        value = (load_api_keys() or {}).get("file_search_roots")
    except Exception:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    roots: List[str] = []
    for item in value:
        text = str(item or "").strip().strip('"\'')
        if not text:
            continue
        expanded = os.path.expandvars(os.path.expanduser(text))
        if os.path.isdir(expanded):
            roots.append(expanded)
    return roots


# ─────────────────────────────────────────────────────────────────────────────
#  Path validation
# ─────────────────────────────────────────────────────────────────────────────
def _as_text(value: Any) -> str:
    """String form of a tool argument, tolerating a list/tuple of values.

    The model is declared a string, but an occasional client sends a one-item
    array; joining is more faithful than ``str(["pdf"])``.
    """
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(item) for item in value if item is not None)
    return str(value or "")


def _strip_quotes(text: Any) -> str:
    value = _as_text(text).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    """Strict boolean for a model-supplied argument.

    ``bool("false")`` is True.  A model that emits the string instead of the
    declared BOOLEAN must not thereby turn a confirmation gate off.
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


def _problem(reason: str, error: str, path: str = "",
             item_type: str = "") -> Dict[str, Any]:
    return {
        "ok": False,
        "path": path,
        "item_type": item_type,
        "reason": reason,
        "error": error,
    }


def _can_access(path: Path, is_dir: bool) -> bool:
    """Whether this process may actually reach the path.

    A file held open by another program is still a fine target for Windows, so
    only a clear permission or disappearance problem counts as inaccessible.
    """
    try:
        if is_dir:
            with os.scandir(str(path)) as entries:
                next(entries, None)
        else:
            with open(str(path), "rb"):
                pass
        return True
    except PermissionError:
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


def validate_path(path: Any, expect: str = "any") -> Dict[str, Any]:
    """Check that a concrete path exists, is the right type and is usable.

    ``expect`` is ``"file"``, ``"folder"`` or ``"any"``. Returns
    ``{"ok": True, "path": absolute_path, "item_type": "file"|"folder"}`` on
    success, otherwise ``{"ok": False, "reason": ..., "error": ...}`` with a
    reason the caller can act on: ``empty``, ``invalid``, ``missing``,
    ``ambiguous``, ``wrong_type`` or ``denied``.

    A relative path is rejected here: user-facing relative paths are resolved
    by ``resolve_open_target`` against the real shell folders and the drives,
    never against the process working directory or the application folder.
    """
    raw = _strip_quotes(path)
    if not raw:
        return _problem("empty", "No path was provided.")
    if "\x00" in raw or any(ord(ch) < 0x20 and ch not in "\t" for ch in raw):
        return _problem(
            "invalid", "That path contains control characters.", raw)
    if len(raw) > 32767:
        return _problem("invalid", "That path is too long.", raw)

    expanded = os.path.expandvars(os.path.expanduser(raw))

    # "\folder" is rooted on the *current* drive, which belongs to the process
    # working directory — the assumption this tool must not make. UNC paths
    # ("\\server\share") do carry their target and are kept.
    if expanded[:1] in ("\\", "/") and not expanded.startswith(("\\\\", "//")):
        return _problem(
            "invalid",
            f"'{raw}' does not include a drive. Give the full path, for "
            f"example 'C:{expanded}'.",
            raw)

    candidate = Path(expanded)

    if not candidate.is_absolute():
        return _problem(
            "missing",
            f"'{raw}' is not an absolute path. Give the full path, or a "
            "folder name such as 'Downloads'.",
            raw)
    # ``.``/``..`` and forward slashes are resolved here so the returned path
    # names the actual entry, without relying on the caller to normalise it.
    resolved = Path(os.path.normpath(str(candidate)))

    try:
        exists = resolved.exists()
    except OSError:
        exists = False
    if not exists:
        return _problem(
            "missing",
            f"'{raw}' does not exist."
            + (" The drive may not be connected."
               if len(str(resolved)) > 1 and str(resolved)[1:2] == ":" else ""),
            raw)

    is_dir = resolved.is_dir()
    is_file = resolved.is_file()
    item_type = "folder" if is_dir else "file" if is_file else "other"

    if expect == "file" and not is_file:
        detail = ("That is a folder, not a file." if is_dir
                  else "That path is not a regular file.")
        return _problem("wrong_type", f"{detail} ({resolved})", str(resolved),
                        item_type)
    if expect == "folder" and not is_dir:
        detail = ("That is a file, not a folder." if is_file
                  else "That path is not a folder.")
        return _problem("wrong_type", f"{detail} ({resolved})", str(resolved),
                        item_type)

    if not _can_access(resolved, is_dir):
        return _problem(
            "denied",
            f"'{resolved}' exists but cannot be accessed. Windows is "
            "refusing permission.",
            str(resolved), item_type)

    return {"ok": True, "path": str(resolved), "item_type": item_type,
            "reason": "", "error": ""}


# ─────────────────────────────────────────────────────────────────────────────
#  Search
# ─────────────────────────────────────────────────────────────────────────────
def request_cancel() -> None:
    """Ask the search running in another thread to stop at the next folder."""
    _CANCEL.set()


def _item_type(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text in ("file", "files"):
        return "file"
    if text in ("folder", "folders", "directory", "directories", "dir"):
        return "folder"
    return "any"


def _extension_filter(value: Any) -> Set[str]:
    """``"pdf"``, ``".pdf"``, ``"*.pdf"``, ``"pptx, pptm"`` and category words
    such as ``"videos"``, ``"documents"`` or ``"notebooks"`` all work.

    A token is expanded only when it names a known category; every other token
    is taken as a literal extension. Nothing here interprets language: the
    alias table is fixed, there is no spelling correction and no synonym
    guessing beyond it.
    """
    text = _as_text(value).strip()
    if not text:
        return set()
    parts = re.split(r"[,;\s]+", text)
    found: Set[str] = set()
    for part in parts:
        token = part.strip().strip("*").lstrip(".").casefold()
        if not token:
            continue
        expansion = _category_extensions(token)
        found.update(expansion or {token})
    return found


def _filter_categories(value: Any) -> List[str]:
    """The category names an extension filter mentioned, for reporting."""
    names: Set[str] = set()
    for part in re.split(r"[,;\s]+", _as_text(value).strip()):
        token = part.strip().strip("*").lstrip(".").casefold()
        category = _SEARCH_CATEGORY_ALIASES.get(token) if token else None
        if category:
            names.add(category)
    return sorted(names)


def _attributes_hidden(name: str, attributes: int) -> bool:
    if name.startswith("."):
        return True
    return bool(attributes & _FILE_ATTRIBUTE_HIDDEN
                or attributes & _FILE_ATTRIBUTE_SYSTEM)


def _walk(roots: List[str], include_hidden: bool, deadline: float,
          cancel: threading.Event,
          max_depth: int = _MAX_DEPTH) -> Iterator[Tuple[str, bool, float]]:
    """Yield ``(path, is_dir, mtime)`` for every reachable entry.

    Directories are visited depth-first with a stack, so a deep tree cannot
    exhaust the Python stack. Symlinks and junctions are reported with the
    folder type they point at but are never descended into: a junction inside
    a user folder is how a bounded search becomes a full-disk search.
    Unreadable folders are skipped, not fatal.
    """
    stack: List[Tuple[str, int]] = [(root, 0) for root in reversed(roots)]
    while stack:
        if cancel.is_set():
            return
        if time.monotonic() > deadline:
            raise _SearchTimeout()
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    # Checked per entry, not per folder: one folder can hold
                    # thousands of files, and an interrupted search has to stop
                    # without finishing it.
                    if cancel.is_set():
                        return
                    if time.monotonic() > deadline:
                        raise _SearchTimeout()

                    # One stat per entry supplies the hidden/system attributes
                    # and the modification time; a file that cannot be stat'ed
                    # is still yielded (it may be openable) with no mtime.
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        info = None
                    attributes = (getattr(info, "st_file_attributes", 0)
                                  if info is not None else 0)
                    if not include_hidden and _attributes_hidden(
                            entry.name, attributes):
                        continue

                    try:
                        real_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    is_reparse = bool(
                        attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
                    is_dir = real_dir
                    if not is_dir and is_reparse:
                        if attributes & _FILE_ATTRIBUTE_DIRECTORY:
                            is_dir = True
                        else:
                            try:
                                is_dir = bool(
                                    entry.is_symlink()
                                    and os.path.isdir(entry.path))
                            except OSError:
                                is_dir = False

                    yield (entry.path, is_dir,
                           info.st_mtime if info is not None else 0.0)
                    if (real_dir and not is_reparse and depth < max_depth
                            and entry.name.casefold() not in _SKIP_DIR_NAMES):
                        stack.append((entry.path, depth + 1))
        except _SearchTimeout:
            raise
        except OSError:
            continue


def _tokens(text: str) -> List[str]:
    return re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)


# Fuzzy candidates are deliberately conservative: a typo or a separator/case
# difference should surface, while unrelated names from a whole-drive scan must
# not flood the assistant with noise.
_FUZZY_MIN_RATIO = 0.74
_FUZZY_STRONG_RATIO = 0.84
_FUZZY_MIN_LENGTH = 4


def _normal_name(text: str) -> str:
    """Case-, accent- and separator-insensitive form used for comparison."""
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    value = "".join(char for char in value
                    if not unicodedata.combining(char))
    value = re.sub(r"[\s._\-()\[\]{},]+", " ", value)
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


def _fuzzy_ratio(query: str, name: str) -> float:
    """A bounded similarity score between the typed name and a real entry.

    Literal relations are scored before this runs. This only catches differences
    humans consider obvious: case, accents, separators, missing/extra letters
    and small transpositions. It is not used to silently open anything.
    """
    wanted = _normal_name(query)
    actual = _normal_name(name)
    actual_stem = _normal_name(os.path.splitext(name)[0])
    if not wanted or not actual:
        return 0.0
    if len(wanted) < _FUZZY_MIN_LENGTH:
        return 0.0
    closest_length = min(
        abs(len(wanted) - len(actual)),
        abs(len(wanted) - len(actual_stem)),
    )
    if closest_length > max(4, len(wanted) // 2):
        return 0.0

    def ratio(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        return difflib.SequenceMatcher(None, left, right).ratio()

    full = ratio(wanted, actual)
    stem = ratio(wanted, _normal_name(os.path.splitext(name)[0]))
    best = max(full, stem)

    # Word-order and "extra word" changes: compare the query tokens with their
    # best counterpart in the real name rather than only the whole string.
    query_tokens = _tokens(wanted)
    name_tokens = _tokens(name)
    if query_tokens and name_tokens:
        token_scores = []
        for token in query_tokens:
            token_scores.append(max(ratio(token, other)
                                    for other in name_tokens))
        token_score = sum(token_scores) / len(token_scores)
        if token_score >= _FUZZY_MIN_RATIO:
            best = max(best, token_score * 0.96)
    return best


def _score(name: str, path: str, query: str) -> float:
    """How well one entry answers the query, deterministically.

    Literal relations rank first: case-insensitive equality, separator-
    normalized equality, prefix, substring, path fragment and whole-word
    membership. A conservative similarity pass then reports near names
    (``homewrok`` -> ``homework``) as low-confidence candidates. Nothing is
    corrected or opened automatically; the assistant sees every candidate and
    asks the user when the result is not exact.
    """
    if not query:
        return 1.0
    wanted = query.casefold().strip()
    lowered = name.casefold().strip()
    stem = lowered.rsplit(".", 1)[0] if "." in lowered.strip(".") else lowered

    if lowered == wanted or stem == wanted:
        return 100.0
    # "My_Report" and "my report" refer to the same name.
    wanted_normal = _normal_name(wanted)
    if (_normal_name(lowered) == wanted_normal
            or _normal_name(stem) == wanted_normal):
        return 100.0
    if lowered.startswith(wanted) or stem.startswith(wanted):
        return 80.0
    if wanted in lowered:
        return 65.0

    # A query that contains a separator is a path fragment ("Aze/actions").
    if "/" in wanted or "\\" in wanted:
        normalised = wanted.replace("/", "\\").strip("\\")
        if normalised and normalised in path.casefold().replace("/", "\\"):
            return 55.0

    wanted_tokens = _tokens(wanted)
    name_tokens = _tokens(name)
    if wanted_tokens and name_tokens:
        if all(token in name_tokens for token in wanted_tokens):
            return 55.0
        if all(token in lowered for token in wanted_tokens):
            return 45.0

    ratio = _fuzzy_ratio(wanted, name)
    if ratio >= _FUZZY_STRONG_RATIO:
        return 40.0
    if ratio >= _FUZZY_MIN_RATIO:
        return 28.0
    return 0.0


def _local_drives() -> List[str]:
    """Fixed and removable drive roots that can be searched.

    Network drives and optical media are left out on purpose: they are slow or
    ejectable, and an explicit ``location`` still reaches them. The type is
    asked before any path is touched, so an unavailable drive letter costs one
    API call and no filesystem stall. Results are cached briefly: a drive can
    be plugged in at any time, but a single search calls this repeatedly for
    widening and relative-path resolution.
    """
    global _LOCAL_DRIVES_AT
    if not _IS_WIN:
        return []
    now = time.monotonic()
    with _LOCAL_DRIVES_LOCK:
        if _LOCAL_DRIVES_AT and now - _LOCAL_DRIVES_AT < _DRIVES_TTL:
            return list(_LOCAL_DRIVES_CACHE)
    try:
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
    except Exception:
        return []
    drives: List[str] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        # 2 removable, 3 fixed, 6 ramdisk; 1 no root, 4 network, 5 cdrom.
        if get_drive_type(ctypes.c_wchar_p(root)) not in (2, 3, 6):
            continue
        if not os.path.isdir(root):
            continue
        drives.append(root)
    with _LOCAL_DRIVES_LOCK:
        _LOCAL_DRIVES_CACHE[:] = drives
        _LOCAL_DRIVES_AT = now
    return list(drives)


# Bare shell-folder names a location may use ("in Downloads"). Deterministic
# name mapping; anything else is a real path and goes through validation.
_KNOWN_LOCATION_NAMES = {
    "desktop": "desktop", "documents": "documents", "document": "documents",
    "downloads": "downloads", "download": "downloads",
    "pictures": "pictures", "picture": "pictures",
    "music": "music", "videos": "videos", "video": "videos",
    "screenshots": "screenshots", "screenshot": "screenshots",
}


def _known_location(raw: str) -> str:
    """A bare shell-folder name resolved to its real path, or ``""``.

    English names are mapped directly. Any other name is compared with the
    *display name Windows gives the folder in the system language*, so a
    localized name resolves to the same real folder as its English name,
    without a hard-coded language table.
    """
    key = raw.strip().strip("\\/").casefold()
    folder_key = _KNOWN_LOCATION_NAMES.get(key)
    if folder_key:
        folder = _home_folder(folder_key)
        return folder if folder and os.path.isdir(folder) else ""

    for candidate_key in set(_KNOWN_LOCATION_NAMES.values()):
        folder = _home_folder(candidate_key)
        if not folder:
            continue
        display = _display_name_for(folder)
        if display and display.casefold() == key:
            return folder
    return ""


_DISPLAY_NAMES: Dict[str, str] = {}


def _display_name_for(path: str) -> str:
    """The name Windows displays for a folder, in the system language."""
    if not _IS_WIN or not path:
        return ""
    key = os.path.normcase(os.path.abspath(path))
    if key in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[key]
    name = ""
    try:
        from ctypes import wintypes

        class _SHFILEINFOW(ctypes.Structure):
            _fields_ = [
                ("hIcon", wintypes.HICON),
                ("iIcon", ctypes.c_int),
                ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", ctypes.c_wchar * 260),
                ("szTypeName", ctypes.c_wchar * 80),
            ]

        info = _SHFILEINFOW()
        ok = ctypes.windll.shell32.SHGetFileInfoW(
            ctypes.c_wchar_p(path), 0, ctypes.byref(info),
            ctypes.sizeof(info), 0x000000200)      # SHGFI_DISPLAYNAME
        if ok:
            name = str(info.szDisplayName or "").strip()
    except Exception:
        name = ""
    _DISPLAY_NAMES[key] = name
    return name


def _search_roots(location: Any) -> Tuple[List[str], str]:
    """Where to search: one explicit location, or the user's usual folders.

    An explicit location is resolved by the same user-path resolver as the
    open tools: absolute paths as given, known-folder-relative paths
    ("Pictures\\Screenshots") under the real folder, and anything else on the
    drives — never relative to the process working directory.
    """
    raw = _strip_quotes(location)
    if raw:
        resolved = resolve_open_target(raw, expect="folder")
        if not resolved["ok"]:
            message = resolved["error"]
            candidates = resolved.get("candidates") or []
            if candidates:
                message += " Candidates: " + ", ".join(candidates)
            return [], message
        return [resolved["path"]], ""

    roots: List[str] = []
    roots.extend(_configured_roots())
    for key in ("desktop", "documents", "downloads",
                "pictures", "music", "videos"):
        folder = _home_folder(key)
        if folder:
            roots.append(folder)
    # The project directory is NOT a default root: it contains code and test
    # fixtures, and including it made an unrelated project file look like the
    # user's file before the drive search ever ran. It can still be searched by
    # naming it as the location, or by adding it to file_search_roots.

    unique: List[str] = []
    seen: Set[str] = set()
    for root in roots:
        try:
            absolute = os.path.abspath(root)
        except (OSError, ValueError):
            continue
        key = os.path.normcase(absolute)
        if key in seen or not os.path.isdir(absolute):
            continue
        seen.add(key)
        unique.append(absolute)
    return unique, ""


def _bare_name(text: str) -> bool:
    """A single file/folder name, not a path: no separators and no drive."""
    return bool(text) and not any(mark in text for mark in ("/", "\\", ":"))


def _profile_roots() -> List[str]:
    """The user's real shell folders, for a targeted lookup before disks."""
    roots: List[str] = []
    seen: Set[str] = set()
    for key in ("desktop", "documents", "downloads", "pictures", "music",
                "videos", "screenshots"):
        folder = _home_folder(key)
        if not folder:
            continue
        try:
            absolute = os.path.abspath(folder)
            marker = os.path.normcase(absolute)
        except (OSError, ValueError):
            continue
        if marker in seen or not os.path.isdir(absolute):
            continue
        seen.add(marker)
        roots.append(absolute)
    return roots


def _scan_roots(roots: List[str], classify, expect: str,
                max_depth: int = _EXPAND_DEPTH
                ) -> Tuple[List[Tuple[str, str]], bool]:
    """Collect matching entries under ``roots``.

    ``classify(path)`` returns a rank ("full" beats "name"/"leaf"), or an
    empty string for no match. Returns ``(matches, timed_out)`` so a scan cut
    short by the time limit is never reported as proof that the item does not
    exist.
    """
    matches: List[Tuple[str, str]] = []
    if not roots:
        return matches, False
    timed_out = False
    deadline = time.monotonic() + _EXPAND_BUDGET
    try:
        for path, is_dir, _mtime in _walk(roots, False, deadline, _CANCEL,
                                          max_depth=max_depth):
            if expect == "file" and is_dir:
                continue
            if expect == "folder" and not is_dir:
                continue
            rank = classify(path)
            if rank:
                matches.append((path, rank))
    except _SearchTimeout:
        timed_out = True
    return matches, timed_out


_RANK_ORDER = {"full": 0, "name": 1, "leaf": 1, "similar": 2}


def _has_exact_matches(matches: List[Tuple[str, str]]) -> bool:
    return any(rank != "similar" for _path, rank in matches)


def _resolve_matches(raw: str, matches: List[Tuple[str, str]],
                     timed_out: bool, searched: List[str],
                     expect: str) -> Dict[str, Any]:
    """Turn scan matches into one validated path, candidates, or not found."""
    if matches:
        best_rank = min(_RANK_ORDER.get(rank, 9) for _path, rank in matches)
        best = [path for path, rank in matches
                if _RANK_ORDER.get(rank, 9) == best_rank]
        if best_rank >= _RANK_ORDER["similar"]:
            problem = _problem(
                "ambiguous",
                (f"I found {len(best)} similar item"
                 f"{'' if len(best) == 1 else 's'} for '{raw}'. Ask the user "
                 "which one they meant, then call again with that exact path."),
                raw)
            problem["candidates"] = best[:10]
            problem["clarification_required"] = True
            problem["similar"] = True
            return problem
        if len(best) > 1:
            problem = _problem(
                "ambiguous",
                (f"More than one item matches '{raw}'. Ask the user which one "
                 "they meant, then call again with that exact path."),
                raw)
            problem["candidates"] = best[:10]
            problem["clarification_required"] = True
            return problem
        return validate_path(best[0], expect)

    scope = (", ".join(searched) if searched
             else "the usual folders or any drive")
    message = f"'{raw}' was not found in {scope}."
    if timed_out:
        message += (" Some folders were not checked because the search reached "
                    "its time limit, so the item may still be there.")
    message += " Give the full path, or name the folder to look in."
    return _problem("missing", message, raw)


def _locate_on_drives(name: str, expect: str) -> Dict[str, Any]:
    """Locate a bare file/folder name, profile first, then C: then D:.

    Exact (case-insensitive) name equality wins. When no exact name exists, a
    conservative similarity pass reports close candidates instead of pretending
    the item does not exist; unique similar matches are still returned as
    candidates so the assistant can ask before opening anything.
    The user's shell folders are checked before whole drives, so a name in
    Downloads is found quickly and a drive scan is only the fallback.
    """
    def classify(path: str) -> str:
        leaf = os.path.basename(path.rstrip("\\/"))
        score = _score(leaf, path, name)
        if score >= 100.0:
            return "name"
        if score > 1.0:
            return "similar"
        return ""

    _CANCEL.clear()
    searched: List[str] = []
    profile = _profile_roots()
    matches, timed_out = _scan_roots(profile, classify, expect,
                                     max_depth=_MAX_DEPTH)
    searched.extend(profile)

    # A fuzzy hit in the profile must not stop the drive pass: an exact file
    # with that name may live on another drive. Keep the first fuzzy set as a
    # fallback, but continue until an exact match is found.
    if not _has_exact_matches(matches):
        for drive in _local_drives():
            searched.append(drive)
            hits, timeout = _scan_roots([drive], classify, expect)
            timed_out = timed_out or timeout
            if _has_exact_matches(hits):
                matches = hits
                break
            if hits and not matches:
                matches = hits

    if not matches and not searched:
        return _problem(
            "missing",
            f"'{name}' is not an absolute path, and no local drive could be "
            "searched. Give the full path.",
            name)
    return _resolve_matches(name, matches, timed_out, searched, expect)


def _split_known_prefix(raw: str) -> Tuple[str, str]:
    """Split ``Downloads\\internova`` into (real Downloads, "internova").

    The first component is matched against the English shell-folder names and
    against the display name Windows shows for each shell folder, so
    ``Pictures\\Screenshots`` resolves under the user's own Pictures folder in
    any system language. Returns ``("", "")`` when the first component is not
    a known shell folder.
    """
    parts = [part for part in re.split(r"[\\/]+", raw.strip().strip("\\/"))
             if part]
    if len(parts) < 2:
        return "", ""
    first = parts[0].casefold()

    folder_key = _KNOWN_LOCATION_NAMES.get(first)
    if folder_key:
        base = _home_folder(folder_key)
    else:
        base = ""
        for candidate_key in set(_KNOWN_LOCATION_NAMES.values()):
            folder = _home_folder(candidate_key)
            if folder and _display_name_for(folder).casefold() == first:
                base = folder
                break

    if not base:
        return "", ""
    return base, os.path.join(*parts[1:])


def _locate_relative(raw: str, expect: str,
                     drives: Optional[List[str]] = None) -> Dict[str, Any]:
    """Find a relative path (``Pictures\\Screenshots``) for the user.

    A path that ends with the requested components wins over one whose leaf
    merely has the same name. The user's shell folders are checked first, then
    the drives in order — C: first, D: next — unless ``drives`` names the one
    drive the user specified. Several equally good matches are reported as
    ambiguous rather than picked.
    """
    parts = [part for part in re.split(r"[\\/]+", raw.strip().strip("\\/"))
             if part]
    if not parts:
        return _problem("invalid", "That path is empty.", raw)
    if any(part in (".", "..") for part in parts):
        return _problem(
            "invalid",
            f"'{raw}' contains '.' or '..', which only means something "
            "relative to a working directory. Give the full path.",
            raw)
    tail = [part.casefold() for part in parts]

    def classify(path: str) -> str:
        components = [part.casefold() for part in Path(path).parts]
        if (len(components) >= len(tail)
                and components[-len(tail):] == tail):
            return "full"
        if components and components[-1] == tail[-1]:
            return "leaf"
        leaf = Path(path).name
        if _score(leaf, path, parts[-1]) > 1.0:
            return "similar"
        return ""

    _CANCEL.clear()
    searched: List[str] = []
    matches: List[Tuple[str, str]] = []
    timed_out = False

    if drives is None:
        profile = _profile_roots()
        matches, timed_out = _scan_roots(profile, classify, expect,
                                         max_depth=_MAX_DEPTH)
        searched.extend(profile)

    if not matches:
        drive_list = list(drives) if drives is not None else _local_drives()
        for drive in drive_list:
            searched.append(drive)
            hits, timeout = _scan_roots([drive], classify, expect)
            timed_out = timed_out or timeout
            if _has_exact_matches(hits):
                matches = hits
                break
            if hits and not matches:
                matches = hits
    elif not _has_exact_matches(matches):
        # Fuzzy profile hits are only a fallback; an exact relative path may
        # still be on another drive.
        drive_list = list(drives) if drives is not None else _local_drives()
        for drive in drive_list:
            searched.append(drive)
            hits, timeout = _scan_roots([drive], classify, expect)
            timed_out = timed_out or timeout
            if _has_exact_matches(hits):
                matches = hits
                break

    if not matches and not searched:
        return _problem(
            "missing",
            f"'{raw}' is a relative path, and no local drive could be "
            "searched. Give the full path.",
            raw)
    return _resolve_matches(raw, matches, timed_out, searched, expect)


def resolve_open_target(path: Any, expect: str = "any") -> Dict[str, Any]:
    """Resolve a user-supplied path without assuming where files live.

    Priority:

    1. an absolute path is validated exactly as given;
    2. a drive on its own ("C:") is that drive's root, and "C:folder" keeps
       the named drive — it never falls back to another one;
    3. a known shell-folder name ("Downloads", its system-language equivalent,
       or "Screenshots") resolves to the real folder;
    4. a relative path starting with a known folder
       ("Downloads\\internova", "Pictures\\Screenshots") is resolved under the
       real folder, and when it is not there the relative path is located in
       the user's folders and then on the drives;
    5. any other relative path is located the same way, C: before D:.

    A rooted path without a drive ("\\folder") is rejected rather than tied to
    the drive of the process working directory. The working directory and the
    application's own folder are never used as roots for user paths.
    """
    raw = _strip_quotes(path)
    if not raw:
        return _problem("empty", "No path was provided.")

    expanded = os.path.expandvars(os.path.expanduser(raw))

    if expanded[:1] in ("\\", "/") and not expanded.startswith(("\\\\", "//")):
        return _problem(
            "invalid",
            f"'{raw}' does not include a drive. Give the full path, for "
            f"example 'C:{expanded}'.",
            raw)

    drive_match = re.match(r"^([A-Za-z]):(.*)$", expanded)
    if drive_match and not os.path.isabs(expanded):
        drive_root = drive_match.group(1).upper() + ":\\"
        rest = drive_match.group(2).lstrip("\\/")
        if not rest:
            return validate_path(drive_root, expect)
        candidate = os.path.join(drive_root, rest)
        try:
            if os.path.exists(candidate):
                return validate_path(candidate, expect)
        except OSError:
            pass
        if not os.path.isdir(drive_root):
            return _problem(
                "missing",
                f"The {drive_root} drive is not available.",
                raw)
        return _locate_relative(rest, expect, drives=[drive_root])

    if os.path.isabs(expanded):
        check = validate_path(expanded, expect)
        if check.get("ok"):
            return check
        if check.get("reason") == "missing":
            similar = _similar_in_parent(expanded, expect)
            if similar:
                return _similar_problem(raw, similar)
        return check

    if expect in ("any", "folder"):
        known = _known_location(raw)
        if known:
            return {"ok": True, "path": known, "item_type": "folder",
                    "reason": "", "error": ""}

    base, rest = _split_known_prefix(raw)
    if base:
        candidate = os.path.join(base, rest)
        try:
            if os.path.exists(candidate):
                return validate_path(candidate, expect)
        except OSError:
            pass
        return _locate_relative(raw, expect)

    if _bare_name(raw):
        return _locate_on_drives(raw, expect)

    return _locate_relative(raw, expect)


def _public_result(path: str, is_dir: bool, mtime: float,
                   score: float) -> Dict[str, Any]:
    name = os.path.basename(path.rstrip("\\/")) or path
    item: Dict[str, Any] = {
        "path": path,
        "name": name,
        "type": "folder" if is_dir else "file",
        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        if mtime else "",
    }
    if not is_dir:
        item["extension"] = os.path.splitext(name)[1].lower()
        try:
            item["size"] = os.path.getsize(path)
        except OSError:
            pass
    item["_score"] = score
    item["_mtime"] = mtime
    return item


def _match_label(score: float) -> str:
    """How the result matched, in one word the assistant can reason about.

    ``exact`` is a full-name hit (the whole name or its stem equals the query),
    ``partial`` a prefix, substring or whole-word relation, ``similar`` a
    conservative fuzzy near-name (typo, case/separator difference), and
    ``kind`` an entry that matches only because of the extension filter — the
    newest of its kind, not a named file. The score ``1.0`` is the no-query
    sentinel.
    """
    if score >= 100.0:
        return "exact"
    if score >= 45.0:
        return "partial"
    if score > 1.0:
        return "similar"
    return "kind"


def search_files(query: Any = "", extension: Any = "", item_type: Any = "any",
                 location: Any = "", modified_within_days: Any = 0,
                 include_hidden: bool = False,
                 limit: Any = _DEFAULT_LIMIT) -> Dict[str, Any]:
    """Search for files and folders by name, extension, category and mtime.

    Every criterion is optional: a location plus an item type is a complete
    request ("any file in Downloads"), and a category word in ``extension``
    covers a whole kind of file ("videos", "documents", "notebooks") without
    naming an extension. Returns candidates, newest first within equally good
    matches, each with a ``match`` label (exact, partial, similar, kind) for the
    assistant to judge. Names are compared case-insensitively and with a
    conservative similarity pass, so a typo, different spacing or a nearby
    spelling is returned as a candidate rather than hidden. Nothing is selected
    or opened automatically: the assistant must ask the user when the result is
    not exact. The usual folders are searched first; when they hold nothing and
    ``location`` does not name a folder, the search widens once to the top
    levels of every fixed and removable drive. It never opens anything.
    """
    operation = "search_files"
    wanted = _strip_quotes(query)
    extensions = _extension_filter(extension)
    categories = _filter_categories(extension)
    include_hidden = _as_bool(include_hidden, False)
    raw_kind = _as_text(item_type).strip().casefold()
    kind = _item_type(raw_kind)
    if raw_kind and kind == "any" and raw_kind not in ("any", "all", "both"):
        return _failure(
            operation, "",
            f"'{item_type}' is not a valid item type. Use 'file', 'folder' "
            "or 'any'.",
            reason="bad_item_type")
    described_by_kind = bool(extensions)

    roots, problem = _search_roots(location)
    if problem:
        return _failure(operation, _strip_quotes(location),
                        f"That search location could not be used: {problem}",
                        reason="bad_location")
    explicit_location = bool(_strip_quotes(location))
    # Drives not already covered by the first pass, kept for widening later.
    widen_to = [] if explicit_location else [
        drive for drive in _local_drives()
        if os.path.normcase(drive) not in
        {os.path.normcase(root) for root in roots}]
    if not roots and not widen_to:
        return _failure(
            operation, "",
            "None of the usual folders could be searched. Give the folder to "
            "look in.",
            reason="no_roots")

    try:
        days = float(modified_within_days or 0)
    except (TypeError, ValueError):
        days = 0.0
    cutoff = time.time() - days * 86400.0 if days > 0 else 0.0

    try:
        maximum = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    except (TypeError, ValueError):
        maximum = _DEFAULT_LIMIT

    _CANCEL.clear()
    found: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    state = {"timed_out": False}
    matched_by_name = False

    def run_pass(pass_roots: List[str], max_depth: int, budget: float) -> None:
        """One bounded walk. Fills ``found``; ``state`` reports the timeout."""
        nonlocal matched_by_name
        deadline = time.monotonic() + budget
        try:
            for path, is_dir, mtime in _walk(pass_roots, bool(include_hidden),
                                             deadline, _CANCEL,
                                             max_depth=max_depth):
                key = os.path.normcase(os.path.abspath(path))
                if key in seen:
                    continue
                if kind == "file" and is_dir:
                    continue
                if kind == "folder" and not is_dir:
                    continue
                if extensions and (is_dir
                                   or os.path.splitext(path)[1].lstrip(".")
                                   .casefold() not in extensions):
                    continue
                if cutoff and mtime and mtime < cutoff:
                    continue
                score = _score(os.path.basename(path.rstrip("\\/")),
                               path.replace("\\", "/"), wanted)
                # With an extension filter the user described a *kind* of file
                # ("the latest PowerPoint presentation"), so words that appear
                # in no filename must not hide the files of that kind; they
                # rank last and the newest of them come first.
                if wanted and score <= 0 and not described_by_kind:
                    continue
                if score > 0:
                    matched_by_name = True
                seen.add(key)
                found.append(_public_result(path, is_dir, mtime, score))
        except _SearchTimeout:
            state["timed_out"] = True

    if roots:
        run_pass(roots, _MAX_DEPTH, _SEARCH_BUDGET)
    first_pass_timed_out = state["timed_out"]
    cancelled = _CANCEL.is_set()

    # Nothing in the usual folders and no location was named: widen to the
    # drives ONE AT A TIME, in drive order (C: before D:), each with its own
    # budget, and stop at the first drive that yields a match. A single shared
    # pass used to search the last drive first and let a huge C: consume the
    # whole budget before D: was ever reached.
    widened = False
    widened_drives: List[str] = []
    if (not found and not explicit_location and not first_pass_timed_out
            and not cancelled and widen_to):
        for drive in widen_to:
            if _CANCEL.is_set():
                break
            widened = True
            widened_drives.append(drive)
            run_pass([drive], _EXPAND_DEPTH, _EXPAND_BUDGET)
            if found:
                break
    timed_out = state["timed_out"]
    cancelled = _CANCEL.is_set()

    found.sort(key=lambda item: (-item["_score"], -item["_mtime"],
                                 item["path"].casefold()))
    truncated = len(found) > maximum
    results = []
    for item in found[:maximum]:
        clean = {key: value for key, value in item.items()
                 if not key.startswith("_")}
        clean["match"] = _match_label(item["_score"])
        results.append(clean)

    if results:
        message = (f"Found {len(found)} matching item"
                   + ("" if len(found) == 1 else "s") + ".")
        best_label = results[0].get("match")
        if wanted and best_label == "similar":
            message = (f"No exact name matched '{wanted}'. These are the "
                       "closest similar items; read them and ask the user "
                       "which one they meant before opening anything.")
        elif wanted and described_by_kind and not matched_by_name:
            message = (f"No name matched '{wanted}', so these are the "
                       f"'{extension}' items found, newest first.")
        if widened:
            message += (" The usual folders had nothing, so the search was "
                        "widened to the top folders of every drive.")
        if truncated:
            message += f" Showing the first {maximum}."
        if timed_out:
            message += " The search stopped at its time limit, so some folders " \
                       "were not checked."
        elif cancelled:
            message += " The search was interrupted, so it was not complete."
    else:
        scope = ", ".join(roots or widened_drives or widen_to
                          or ["the usual folders"])
        label = wanted or extension or "any file"
        message = (f"No files or folders matching '{label}' were "
                   f"found in {scope}.")
        if widened:
            message += (" The usual folders were widened to the top folders "
                        "of every drive, and still nothing matched.")
        if timed_out:
            message += " The search hit its time limit before it finished."
        message += " Try a different name, or give the folder to search."

    result: Dict[str, Any] = {
        "success": True,
        "operation": operation,
        "query": wanted,
        "results": results,
        "count": len(found),
        "searched": roots or widened_drives or widen_to,
        "truncated": truncated,
        "timed_out": timed_out,
        "message": message,
    }
    if widened:
        result["widened_to"] = widened_drives
    if categories:
        result["categories"] = categories
    if extensions:
        result["extensions"] = sorted(extensions)
    if not results:
        result["hint"] = ("Give a location to search, or a broader part of the "
                          "name.")
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Opening
# ─────────────────────────────────────────────────────────────────────────────
def _failure(operation: str, path: Any, error: str, reason: str = "",
             item_type: str = "") -> Dict[str, Any]:
    return {
        "success": False,
        "operation": operation,
        "path": str(path or ""),
        "item_type": item_type,
        "reason": reason,
        "message": error,
        "error": error,
    }


def _validate_failure(operation: str, check: Dict[str, Any]) -> Dict[str, Any]:
    result = _failure(operation, check.get("path", ""), check["error"],
                      check.get("reason", ""), check.get("item_type", ""))
    if check.get("candidates"):
        result["candidates"] = list(check["candidates"])
    if check.get("clarification_required"):
        result["clarification_required"] = True
        result["similar"] = bool(check.get("similar"))
    return result


def _similar_problem(raw: str, candidates: List[str]) -> Dict[str, Any]:
    problem = _problem(
        "ambiguous",
        (f"I found {len(candidates)} similar item"
         f"{'' if len(candidates) == 1 else 's'} for '{raw}'. Ask the user "
         "which one they meant, then call again with that exact path."),
        raw)
    problem["candidates"] = candidates[:10]
    problem["clarification_required"] = True
    problem["similar"] = True
    return problem


def _similar_in_parent(path: str, expect: str) -> List[str]:
    """Close names in the given path's existing parent folder."""
    parent = os.path.dirname(path)
    leaf = os.path.basename(path.rstrip("\\/"))
    if not parent or not leaf or not os.path.isdir(parent):
        return []
    scored: List[Tuple[float, str]] = []
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if expect == "file" and is_dir:
                    continue
                if expect == "folder" and not is_dir:
                    continue
                score = _score(entry.name, entry.path, leaf)
                if score > 1.0:
                    scored.append((score, entry.path))
    except OSError:
        return []
    scored.sort(key=lambda item: (-item[0], item[1].casefold()))
    return [item for _score_value, item in scored[:10]]


def _launch_risk(path: str) -> str:
    """The kind of action Windows would take, when it is more than displaying.

    ``""`` means an ordinary document or data file: opening it is safe and needs
    no confirmation. Anything else executes, installs or follows a shortcut,
    and the assistant has to ask first.
    """
    suffix = os.path.splitext(path)[1].lower()
    if suffix in _INSTALLER_EXTS:
        return "installer"
    if suffix in _EXECUTABLE_EXTS:
        return "executable"
    if suffix in _SCRIPT_EXTS:
        return "script"
    if suffix in _SHORTCUT_EXTS:
        return "shortcut"
    if suffix in _MACRO_EXTS:
        return "macro"
    return ""


_RISK_ACTION = {
    "installer": "install software on your computer",
    "executable": "run a program",
    "script": "run a script",
    "shortcut": "launch whatever the shortcut points to",
    "macro": "run macros embedded in the document",
}
_RISK_LABEL = {
    "installer": "an installer",
    "executable": "an executable",
    "script": "a script",
    "shortcut": "a shortcut",
    "macro": "a macro-enabled document",
}

# A named application normally receives the path as data, which is how a
# script is displayed instead of executed. That guarantee belongs to viewers
# and editors; a named interpreter or launcher would run it. These are the
# application names that may open a high-risk file without a second question.
_SAFE_VIEWER_APP_KEYS = frozenset({
    "notepad", "notepad++", "visual studio code", "vs code", "vscode",
    "sublime text", "atom", "gedit", "kate", "notepadqq", "neovim",
    "vim", "emacs", "pycharm", "intellij idea", "textedit",
})


def _display_name(path: str) -> str:
    """The file or folder name, falling back to the path for a drive root."""
    return os.path.basename(str(path).rstrip("\\/")) or str(path)


# Recently opened targets, newest first. "Close this file" / "close the folder
# I just opened" need the context of what Aze itself opened; a short in-memory
# record is enough and never touches the disk.
_RECENT_OPENED: List[Dict[str, Any]] = []
_RECENT_OPENED_LOCK = threading.Lock()
_RECENT_OPENED_LIMIT = 8


def _remember_opened(path: str, item_type: str = "") -> None:
    """Record a successful open so close_file can resolve 'this'/'that one'."""
    target = str(path or "").strip()
    if not target:
        return
    kind = item_type or ("folder" if os.path.isdir(target) else "file")
    with _RECENT_OPENED_LOCK:
        _RECENT_OPENED.insert(0, {
            "path": target,
            "item_type": kind,
            "at": time.time(),
        })
        del _RECENT_OPENED[_RECENT_OPENED_LIMIT:]


def recently_opened(limit: int = 5) -> List[Dict[str, Any]]:
    """The last successfully opened files and folders, newest first."""
    try:
        count = max(1, int(limit))
    except (TypeError, ValueError):
        count = 5
    with _RECENT_OPENED_LOCK:
        return [dict(entry) for entry in _RECENT_OPENED[:count]]


def _app_key(text: Any) -> str:
    """The same exact-name rule ``open_app_tool`` uses for installed apps."""
    value = str(text or "").lower().strip()
    value = re.sub(r"\.(exe|lnk|msc|bat|cmd)$", "", value)
    value = re.sub(r"[^\w\s]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _installed_apps() -> List[Dict[str, Any]]:
    """The catalogue ``open_app_tool`` already built, or an empty list."""
    try:
        from actions import get_tool_module
        get_installed_programs = get_tool_module("open_app").get_installed_programs
        return list(get_installed_programs() or [])
    except Exception:
        return []


def _resolve_app(name: str) -> Optional[Dict[str, Any]]:
    key = _app_key(name)
    if not key:
        return None
    for entry in _installed_apps():
        if _app_key(entry.get("name")) == key:
            return entry
    return None


def _open_with_app(operation: str, target: str, app: str) -> Dict[str, Any]:
    """Hand ``target`` to an installed application the user named.

    The application comes from the same catalogue ``open_app`` uses, so an
    uninstalled name fails here exactly as it does there — no executable path is
    invented, and Windows' default association is never substituted silently.
    """
    entry = _resolve_app(app)
    if entry is None:
        return _failure(
            operation, target,
            f"No installed app is named '{app}'. Call list_apps to see every "
            "installed app, choose the exact name yourself, then try again.",
            reason="app_not_installed")

    name = str(entry.get("name") or app)
    shortcut = str(entry.get("shortcut_path") or "")

    if shortcut and os.path.isfile(shortcut):
        try:
            _startfile(shortcut, "open", f'"{target}"')
        except OSError as exc:
            return _failure(
                operation, target,
                f"'{name}' was found, but handing it the file failed: {exc}",
                reason="launch_failed")
        _remember_opened(target)
        return {
            "success": True,
            "operation": operation,
            "path": target,
            "item_type": "folder" if os.path.isdir(target) else "file",
            "opened": target,
            "via": name,
            "message": f"Opened '{_display_name(target)}' in {name}.",
        }

    launch = str(entry.get("launch") or "")
    if launch and os.path.isfile(launch):
        # A desktop app registered through the Apps folder can be given a path.
        try:
            subprocess.Popen([launch, target], shell=False,
                             creationflags=_NO_WINDOW)
        except OSError as exc:
            return _failure(
                operation, target,
                f"'{name}' was found, but launching it failed: {exc}",
                reason="launch_failed")
        _remember_opened(target)
        return {
            "success": True,
            "operation": operation,
            "path": target,
            "item_type": "folder" if os.path.isdir(target) else "file",
            "opened": target,
            "via": name,
            "message": f"Opened '{_display_name(target)}' in {name}.",
        }

    return _failure(
        operation, target,
        f"'{name}' is a Microsoft Store app, and Windows cannot be told to "
        "open a specific file in it. Open the file with its default "
        "application, or open the app by itself with open_app.",
        reason="app_argument_unsupported")


# ``AssocQueryStringW`` asks the shell which handler it would use. It is the
# same data Explorer uses, so there is no table of extensions to fall stale.
_ASSOCSTR_COMMAND = 1
_ASSOCSTR_EXECUTABLE = 2
_ASSOCSTR_PROGID = 20


def _assoc_string(string_type: int, extension: str) -> str:
    """One shell association string for an extension, or ``""``."""
    if not _IS_WIN or not extension:
        return ""
    try:
        from ctypes import wintypes

        shlwapi = ctypes.WinDLL("shlwapi")
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        result = shlwapi.AssocQueryStringW(
            0, string_type, extension, None, buffer, ctypes.byref(size))
        if result != 0:
            return ""
        return str(buffer.value or "").strip()
    except Exception:
        return ""


def _associated_handler(path: str) -> str:
    """What Windows has registered for this file, or ``""`` for nothing.

    Windows resolves associations by extension, so an empty answer (including the
    answer for an extensionless ``Makefile``) means it would show the "How do you
    want to open this file?" picker. That picker is not an open: ``os.startfile``
    reports success after showing it, so reporting success would be a lie.
    """
    if not _IS_WIN:
        return ""

    suffix = os.path.splitext(path)[1].lower()
    executable = _assoc_string(_ASSOCSTR_EXECUTABLE, suffix)
    command = _assoc_string(_ASSOCSTR_COMMAND, suffix)
    progid = _assoc_string(_ASSOCSTR_PROGID, suffix)

    if executable and not executable.casefold().endswith("openwith.exe"):
        return executable
    if command and "openwith.exe" not in command.casefold():
        return command

    # Store-packaged handlers (Photos, Media Player) register a ProgID without
    # exposing a command or executable; the shell still opens them, so a known
    # ProgID is a real handler even when those strings are unavailable. Windows
    # answers "Unknown" for an extension nothing is registered for.
    if progid and progid.casefold() != "unknown":
        return progid
    return ""


def _shell_open(path: str) -> None:
    """Ask Windows to open the path with its registered application."""
    if not _IS_WIN or _startfile is None:
        raise OSError("Opening files is only implemented on Windows.")
    _startfile(path)


def _launch_error_message(path: str, exc: OSError) -> Tuple[str, str]:
    """Turn a Win32 launch error into an answer the assistant can act on."""
    # ``os.startfile`` reports the Win32 code in ``winerror``; a plain OSError
    # raised by an intermediate layer may only carry it in ``errno``.
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    name = os.path.basename(path.rstrip("\\/")) or path
    if code == 1155:                       # ERROR_NO_ASSOCIATION
        return (
            f"Windows has no application registered for '{name}'. Name the app "
            "the user wants instead, for example app='Notepad'.",
            "no_association")
    if code == 5:                          # ERROR_ACCESS_DENIED
        return (f"Windows refused access to '{name}'.", "denied")
    if code == 2:                          # ERROR_FILE_NOT_FOUND
        return (f"'{name}' disappeared before it could be opened.", "missing")
    return (f"'{name}' could not be opened: {exc}", "launch_failed")


# ─────────────────────────────────────────────────────────────────────────────
#  Fallback applications — the layer behind the default association
# ─────────────────────────────────────────────────────────────────────────────
# The default association is always tried first. When Windows has none, or the
# associated application refuses to launch, an installed application that fits
# the file's *category* is chosen instead. This is a suggestion mechanism, not
# a whitelist: an extension listed here is still opened by whatever Windows has
# registered for it when that exists, and an unknown extension is never forced
# into an unrelated application — it fails honestly instead.
_CATEGORIES = {
    "image": (".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp", ".webp",
              ".tif", ".tiff", ".ico", ".heic", ".heif", ".avif"),
    "svg": (".svg",),
    "webdoc": (".html", ".htm", ".mht", ".mhtml"),
    "video": (".mp4", ".m4v", ".mkv", ".avi", ".mov", ".wmv", ".webm",
              ".mpg", ".mpeg", ".ts", ".3gp", ".flv", ".ogv"),
    "audio": (".mp3", ".wav", ".flac", ".ogg", ".oga", ".m4a", ".aac",
              ".wma", ".opus", ".mid", ".midi", ".aiff", ".aif"),
    "archive": (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2",
                ".xz", ".cab", ".iso"),
    "pdf": (".pdf",),
    "ebook": (".epub", ".mobi", ".azw3", ".djvu"),
    "code": (".py", ".js", ".mjs", ".ts", ".jsx", ".tsx", ".c", ".h", ".cpp",
             ".hpp", ".cs", ".java", ".kt", ".rs", ".go", ".rb", ".php",
             ".swift", ".m", ".mm", ".sql", ".sh", ".lua", ".r", ".dart",
             ".vue", ".svelte", ".asm", ".ino", ".gradle", ".cmake",
             ".ipynb"),
    "text": (".txt", ".log", ".md", ".rst", ".csv", ".tsv", ".ini", ".cfg",
             ".conf", ".json", ".yaml", ".yml", ".toml", ".xml", ".env",
             ".diff", ".patch", ".srt", ".vtt", ".nfo", ".properties"),
    "font": (".ttf", ".otf", ".ttc", ".fon"),
    "document": (".doc", ".docx", ".odt", ".rtf", ".pages"),
    "spreadsheet": (".xls", ".xlsx", ".ods", ".xlt", ".xltx"),
    "slides": (".ppt", ".pptx", ".odp", ".pps", ".ppsx"),
}
_EXTENSION_CATEGORY = {extension: category
                       for category, extensions in _CATEGORIES.items()
                       for extension in extensions}

# Category words the assistant may pass as search criteria instead of listing
# extensions ("open any video", "find a spreadsheet"). Expansion is fixed and
# deterministic: a token is either one of these words or a literal extension.
_SEARCH_CATEGORY_ALIASES = {
    "image": "image", "images": "image", "picture": "image",
    "pictures": "image", "photo": "image", "photos": "image",
    "video": "video", "videos": "video", "movie": "video",
    "movies": "video",
    "audio": "audio", "music": "audio", "song": "audio", "songs": "audio",
    "sound": "audio", "sounds": "audio",
    "document": "document", "documents": "document", "docs": "document",
    "word": "document",
    "pdf": "pdf", "pdfs": "pdf",
    "spreadsheet": "spreadsheet", "spreadsheets": "spreadsheet",
    "excel": "spreadsheet", "sheet": "spreadsheet", "sheets": "spreadsheet",
    "presentation": "presentation", "presentations": "presentation",
    "slide": "presentation", "slides": "presentation",
    "powerpoint": "presentation", "powerpoints": "presentation",
    "text": "text",
    "code": "code", "source": "code",
    "archive": "archive", "archives": "archive",
    "font": "font", "fonts": "font",
    "notebook": "notebook", "notebooks": "notebook",
}

# Extensions behind each category word. ``groups`` name entries of
# ``_CATEGORIES``; ``extra`` adds formats that are searchable but not
# openable by a specific fallback application (web fonts, spreadsheets' CSV).
_SEARCH_CATEGORY_GROUPS = {
    "image": (("image", "svg"), ()),
    "video": (("video",), ()),
    "audio": (("audio",), ()),
    "document": (("document", "pdf"), ()),
    "pdf": (("pdf",), ()),
    "spreadsheet": (("spreadsheet",), (".csv",)),
    "presentation": (("slides",), ()),
    "text": (("text",), ()),
    "code": (("code", "webdoc"), ()),
    "archive": (("archive",), ()),
    "font": (("font",), (".woff", ".woff2")),
    "notebook": ((), (".ipynb",)),
}


def _category_extensions(token: str) -> Set[str]:
    """Extensions for a category word, or an empty set for anything else."""
    category = _SEARCH_CATEGORY_ALIASES.get(token)
    if not category:
        return set()
    groups, extra = _SEARCH_CATEGORY_GROUPS[category]
    extensions = {extension.lstrip(".").casefold()
                  for group in groups for extension in _CATEGORIES[group]}
    extensions.update(value.lstrip(".").casefold() for value in extra)
    return extensions

# Exe candidates. ``app_paths`` names are looked up in the registry's
# ``App Paths`` key — the mechanism Windows itself uses to locate an
# application by name — and ``paths`` are known install locations. Both are
# existence checks, never assumptions: an entry that is not installed simply
# does not resolve. ``mode`` is how the file is passed: as a plain path
# argument, or as a ``file:///`` URI for browsers.
_FALLBACK_APPS: Dict[str, Dict[str, Any]] = {
    "notepad": {"name": "Notepad", "mode": "path",
                "paths": ("%windir%\\System32\\notepad.exe",
                          "%windir%\\notepad.exe")},
    "notepadpp": {"name": "Notepad++", "mode": "path",
                  "app_paths": ("notepad++.exe",),
                  "paths": ("C:\\Program Files\\Notepad++\\notepad++.exe",
                            "C:\\Program Files (x86)\\Notepad++\\notepad++.exe")},
    "vscode": {"name": "Visual Studio Code", "mode": "path",
               "paths": ("%LocalAppData%\\Programs\\Microsoft VS Code\\Code.exe",
                         "C:\\Program Files\\Microsoft VS Code\\Code.exe",
                         "%LocalAppData%\\Microsoft\\WindowsApps\\code.exe")},
    "paint": {"name": "Paint", "mode": "path",
              "app_paths": ("mspaint.exe",),
              "paths": ("%windir%\\System32\\mspaint.exe",)},
    "fontview": {"name": "Windows Font Viewer", "mode": "path",
                 "paths": ("%windir%\\System32\\fontview.exe",)},
    "brave": {"name": "Brave", "mode": "uri", "brave": True,
              "app_paths": ("brave.exe",)},
    "msedge": {"name": "Microsoft Edge", "mode": "uri",
               "app_paths": ("msedge.exe",),
               "paths": ("C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
                         "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe")},
    "chrome": {"name": "Google Chrome", "mode": "uri",
               "app_paths": ("chrome.exe",),
               "paths": ("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                         "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe")},
    "firefox": {"name": "Firefox", "mode": "uri",
                "app_paths": ("firefox.exe",),
                "paths": ("C:\\Program Files\\Mozilla Firefox\\firefox.exe",
                          "C:\\Program Files (x86)\\Mozilla Firefox\\firefox.exe")},
    "vlc": {"name": "VLC media player", "mode": "path",
            "app_paths": ("vlc.exe",),
            "paths": ("C:\\Program Files\\VideoLAN\\VLC\\vlc.exe",
                      "C:\\Program Files (x86)\\VideoLAN\\VLC\\vlc.exe"),
            "catalogue": ("VLC media player", "VLC")},
    "wmp": {"name": "Windows Media Player", "mode": "path",
            "app_paths": ("wmplayer.exe",),
            "paths": ("C:\\Program Files (x86)\\Windows Media Player\\wmplayer.exe",
                      "C:\\Program Files\\Windows Media Player\\wmplayer.exe")},
    "mpc": {"name": "Media Player Classic", "mode": "path",
            "app_paths": ("mpc-hc64.exe", "mpc-hc.exe", "mpc-be64.exe",
                          "mpc-be.exe"),
            "paths": ("C:\\Program Files\\MPC-HC\\mpc-hc64.exe",
                      "C:\\Program Files (x86)\\MPC-HC\\mpc-hc.exe",
                      "C:\\Program Files\\MPC-BE x64\\MPC-BE64.exe")},
    "sevenzip": {"name": "7-Zip", "mode": "path",
                 "paths": ("C:\\Program Files\\7-Zip\\7zFM.exe",
                           "C:\\Program Files (x86)\\7-Zip\\7zFM.exe"),
                 "catalogue": ("7-Zip", "7-Zip File Manager")},
    "winrar": {"name": "WinRAR", "mode": "path",
               "paths": ("C:\\Program Files\\WinRAR\\WinRAR.exe",
                         "C:\\Program Files (x86)\\WinRAR\\WinRAR.exe"),
               "catalogue": ("WinRAR",)},
    "bandizip": {"name": "Bandizip", "mode": "path",
                 "paths": ("C:\\Program Files\\Bandizip\\Bandizip.exe",)},
    "peazip": {"name": "PeaZip", "mode": "path",
               "paths": ("C:\\Program Files\\PeaZip\\peazip.exe",)},
    "sumatra": {"name": "Sumatra PDF", "mode": "path",
                "paths": ("%LocalAppData%\\SumatraPDF\\SumatraPDF.exe",
                          "C:\\Program Files\\SumatraPDF\\SumatraPDF.exe")},
    "acrobat": {"name": "Adobe Acrobat Reader", "mode": "path",
                "app_paths": ("AcroRd32.exe", "acrobat.exe")},
    "winword": {"name": "Microsoft Word", "mode": "path",
                "app_paths": ("winword.exe",)},
    "excel": {"name": "Microsoft Excel", "mode": "path",
              "app_paths": ("excel.exe",)},
    "powerpoint": {"name": "Microsoft PowerPoint", "mode": "path",
                   "app_paths": ("powerpnt.exe",)},
}

# Which applications fit which category, in priority order. ``"browser"``
# expands to the browser entries above (Brave — already discovered by
# open_web — then Edge, Chrome, Firefox). A ``("catalogue", names)`` entry is
# resolved through the application catalogue open_app already maintains.
_BROWSERS = ("brave", "msedge", "chrome", "firefox")
_FALLBACK_ORDER: Dict[str, tuple] = {
    "image": ("paint", "browser", ("catalogue", ("GIMP", "Inkscape"))),
    "svg": ("browser", "paint"),
    "webdoc": ("browser", "vscode", "notepad"),
    "video": ("vlc", "mpc", "browser"),
    "audio": ("vlc", "wmp", ("catalogue", ("AIMP", "MusicBee",
                                           "foobar2000")), "browser"),
    "archive": ("sevenzip", "winrar", "bandizip", "peazip"),
    "pdf": ("browser", "sumatra", "acrobat"),
    "ebook": ("sumatra", ("catalogue", ("Calibre",))),
    "code": ("vscode", "notepadpp", "notepad",
             ("catalogue", ("Sublime Text",))),
    "text": ("notepad", "notepadpp", "vscode"),
    "font": ("fontview",),
    "document": ("winword", ("catalogue", ("LibreOffice Writer",
                                           "LibreOffice"))),
    "spreadsheet": ("excel", ("catalogue", ("LibreOffice Calc",
                                            "LibreOffice"))),
    "slides": ("powerpoint", ("catalogue", ("LibreOffice Impress",
                                            "LibreOffice"))),
}

# Resolved executable paths, cached so a request costs at most one lookup per
# candidate. ``refresh_app_cache()`` clears it.
_resolved_apps: Dict[str, Optional[str]] = {}


def refresh_app_cache() -> None:
    """Forget cached fallback-application paths so they resolve again."""
    _resolved_apps.clear()


def _extract_exe_path(value: Any) -> str:
    """The executable path inside an ``App Paths`` default value, if it exists.

    The value is usually a plain path or a quoted command line; taking the
    quoted segment when quotes are present keeps arguments out of the path.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 0:
            text = text[1:end]
    elif '"' in text:
        text = text.split('"', 1)[1]
    text = os.path.expandvars(os.path.expanduser(text.strip()))
    return text if os.path.isfile(text) else ""


def _app_paths_lookup(exe_names: Tuple[str, ...]) -> str:
    """Find an application through its ``App Paths`` registration."""
    if not _IS_WIN or winreg is None:
        return ""
    for exe_name in exe_names:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                        hive,
                        rf"Software\Microsoft\Windows\CurrentVersion"
                        rf"\App Paths\{exe_name}") as key:
                    value = winreg.QueryValue(key, None)
            except OSError:
                continue
            path = _extract_exe_path(value)
            if path:
                return path
    return ""


def _resolve_candidate(key: str) -> Optional[str]:
    """The executable for one fallback candidate, or ``None`` when absent.

    Brave reuses the discovery ``open_web`` already does. Everything else is a
    registry or known-location existence check, cached per session.
    """
    if key in _resolved_apps:
        return _resolved_apps[key]
    candidate = _FALLBACK_APPS.get(key) or {}
    exe = _app_paths_lookup(candidate.get("app_paths", ()))
    if not exe:
        for template in candidate.get("paths", ()):
            try:
                path = os.path.expandvars(os.path.expanduser(template))
            except Exception:
                continue
            if os.path.isfile(path):
                exe = path
                break
    if not exe and candidate.get("brave"):
        try:
            from actions import get_tool_module
            get_brave_path = get_tool_module("open_web").get_brave_path
            found = get_brave_path()
            if found and os.path.isfile(found):
                exe = found
        except Exception:
            pass
    _resolved_apps[key] = exe or None
    return _resolved_apps[key]


def _categories_for(path: str) -> List[str]:
    """The file's fallback categories; extensionless files count as text.

    An editor displays anything without interpreting it, so a bare ``Makefile``
    or ``LICENSE`` is safe to hand to a text editor — and strictly safer than
    executing it, whatever it turns out to be.
    """
    suffix = os.path.splitext(path)[1].lower()
    if not suffix:
        return ["text"]
    category = _EXTENSION_CATEGORY.get(suffix)
    return [category] if category else []


def _category_steps(categories: List[str]) -> List[Any]:
    """The candidate steps for these categories, deduplicated and ordered."""
    steps: List[Any] = []
    seen: Set[str] = set()
    for category in categories:
        for entry in _FALLBACK_ORDER.get(category, ()):
            if isinstance(entry, str):
                keys = _BROWSERS if entry == "browser" else (entry,)
                for key in keys:
                    if key not in seen:
                        seen.add(key)
                        steps.append(key)
            else:
                label = "catalogue:" + ",".join(entry[1])
                if label not in seen:
                    seen.add(label)
                    steps.append(entry)
    return steps


def _popen_launched(exe: str, arg: str) -> Tuple[bool, str]:
    """Start one application with the file as its argument.

    The argument list form never touches a shell, so spaces, parentheses and
    Unicode in the path need no escaping. The app is asked to open the file;
    when the started process exits immediately with a failure code the launch
    is treated as refused, so the next candidate can be tried.
    """
    try:
        process = subprocess.Popen([exe, arg], shell=False,
                                   creationflags=_NO_WINDOW)
    except OSError as exc:
        return False, str(exc)
    try:
        time.sleep(0.5)
    except Exception:
        pass
    if process.poll() is not None and process.returncode != 0:
        return False, f"the application exited at once (code {process.returncode})"
    return True, ""


def _candidate_arg(candidate: Dict[str, Any], target: str) -> str:
    """How this candidate wants the file passed: a path or a file URI."""
    if candidate.get("mode") == "uri":
        return Path(target).as_uri()
    return target


def _open_with_fallback(operation: str, target: str, why: str) -> Dict[str, Any]:
    """Open ``target`` with an installed application that fits its category.

    ``why`` explains to the user why the default association was not used.
    Every attempt is recorded; the first that starts is reported with its
    application name and the fallback flag.
    """
    name = _display_name(target)
    steps = _category_steps(_categories_for(target))
    if not steps:
        # No category fits, so there is nothing safe to suggest: report the
        # missing association rather than guessing.
        return _failure(
            operation, target,
            f"Windows has no application registered for '{name}'. Name the app "
            "the user wants with app=..., or ask them which one to use.",
            reason="no_association", item_type="file")

    attempts: List[Dict[str, Any]] = []

    for step in steps:
        if isinstance(step, str):
            candidate = _FALLBACK_APPS[step]
            exe = _resolve_candidate(step)
            if not exe:
                attempts.append({"app": candidate["name"],
                                 "error": "not installed"})
                continue
            ok, detail = _popen_launched(exe, _candidate_arg(candidate, target))
            if ok:
                _remember_opened(target)
                return {
                    "success": True,
                    "operation": operation,
                    "path": target,
                    "item_type": "file",
                    "opened": target,
                    "via": candidate["name"],
                    "fallback": True,
                    "attempts": attempts,
                    "message": (
                        f"Opened '{name}' with {candidate['name']} — "
                        f"fallback, because {why}. The application was "
                        "started with the file."
                    ),
                }
            attempts.append({"app": candidate["name"], "error": detail})
            continue

        # A catalogue step: the same launcher open_app uses, so Store apps and
        # shortcuts are handled by existing code.
        for app_name in step[1]:
            result = _open_with_app(operation, target, app_name)
            if result.get("success"):
                result["fallback"] = True
                result["attempts"] = attempts
                result["message"] = (
                    f"{result.get('message', '').rstrip('.')} — fallback, "
                    f"because {why}."
                )
                return result
            attempts.append({"app": app_name,
                             "error": result.get("error", "not installed")})

    tried = ", ".join(attempt["app"] for attempt in attempts) or "no candidate"
    failure = _failure(
        operation, target,
        f"Windows has no default application for '{name}', and no installed "
        f"application that can open it was found (tried: {tried}). Name an "
        "application with app=..., or ask the user which one to use.",
        reason="no_usable_application", item_type="file")
    failure["attempts"] = attempts
    return failure



def _approval_required(operation: str, target: str, risk: str,
                       request: Dict[str, Any]) -> Dict[str, Any]:
    """The structured answer asking for a trusted UI decision."""
    action = _RISK_ACTION[risk]
    name = _display_name(target)
    summary = (
        f"Open '{name}': this is {_RISK_LABEL[risk]} file and opening it "
        f"would {action}.")
    return {
        "success": False,
        "ok": False,
        "approval_required": True,
        "operation": operation,
        "path": target,
        "item_type": "file",
        "risk": risk,
        "reason": "approval_required",
        "approval_summary": summary,
        "approval_expires_at": str(request.get("expires_at") or ""),
        "_approval_id": str(request.get("approval_id") or ""),
        "message": f"{summary} The user must approve it in the Jarvis interface.",
        "error": (
            "Approval required in the Jarvis interface. Do not retry until "
            "the user approves, and never supply confirmation flags."),
    }


def _authorize_risky_open(target: str, named_app: str,
                          risk: str) -> Tuple[bool, Dict[str, Any]]:
    summary = (
        f"Open '{_display_name(target)}'"
        + (f" with {named_app}" if named_app else "")
        + f": this is {_RISK_LABEL[risk]} file and opening it would "
        + _RISK_ACTION[risk])
    try:
        allowed, request, _reason = _approvals.get_service().authorize(
            tool="open_file",
            operation="open_file",
            target=target,
            parameters={
                "path": os.path.normcase(os.path.abspath(target)),
                "app": _app_key(named_app) if named_app else "",
                "risk": risk,
            },
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        return False, {
            "success": False,
            "ok": False,
            "operation": "open_file",
            "path": target,
            "item_type": "file",
            "approval_unavailable": True,
            "reason": "approval_unavailable",
            "error": (
                "Approval is unavailable, so the file was not opened: "
                f"{type(exc).__name__}: {exc}"),
        }
    if not allowed:
        return False, _approval_required("open_file", target, risk, request)
    return True, {}


def open_file(path: Any, app: Any = "", confirm: bool = False) -> Dict[str, Any]:
    """Open one existing file with Windows, or with a named application.

    Layered selection: the Windows default association first, then — when
    there is no association or the associated application will not launch —
    an installed application that fits the file's category (an image app for
    pictures, a media player for video, an archive manager for archives, a
    text or code editor for configuration and source files). Executables,
    installers and shortcuts return ``approval_required`` until the local
    dashboard approves that exact path. ``confirm`` is accepted only for old
    callers and never authorizes anything. A script keeps the gate when its
    default association would execute it; with no association at all, an editor
    is used to display it, which executes nothing.
    """
    operation = "open_file"
    raw = _strip_quotes(path)
    check = resolve_open_target(raw, expect="file")
    if not check["ok"]:
        return _validate_failure(operation, check)
    target = check["path"]
    # Kept for call compatibility only. Authorization is never derived from it.
    _ignored_confirm = _as_bool(confirm, False)

    named_app = str(app or "").strip()
    risk = _launch_risk(target)
    if named_app:
        # A named application normally receives the file as data, which is
        # safe for an editor. But the same path handed to an interpreter or a
        # launcher runs it; a model-generated app name must not bypass approval.
        if (risk and _app_key(named_app) not in _SAFE_VIEWER_APP_KEYS):
            allowed, result = _authorize_risky_open(target, named_app, risk)
            if not allowed:
                return result
        return _open_with_app(operation, target, named_app)

    gate = False
    if risk == "script":
        # Opening a script with its default association executes it; opening
        # it in an editor (no association, so the fallback editor below) does
        # not. Gate only the executing path.
        gate = bool(_associated_handler(target))
    elif risk:
        gate = True
    if gate:
        allowed, result = _authorize_risky_open(target, "", risk)
        if not allowed:
            return result

    if _associated_handler(target):
        try:
            _shell_open(target)
        except OSError as exc:
            message, reason = _launch_error_message(target, exc)
            if reason in ("denied", "missing"):
                # Windows refused the path itself; another application would
                # hit the same wall.
                return _failure(operation, target, message, reason=reason,
                                item_type="file")
            return _open_with_fallback(
                operation, target,
                f"the default application could not launch it ({message})")
        _remember_opened(target)
        return {
            "success": True,
            "operation": operation,
            "path": target,
            "item_type": "file",
            "opened": target,
            "via": "default application",
            "message": f"Opened '{_display_name(target)}' with its default "
                       "application.",
        }

    return _open_with_fallback(
        operation, target,
        "Windows has no default application for this file type")


def open_folder(path: Any, app: Any = "") -> Dict[str, Any]:
    """Open one existing folder in File Explorer, or in a named application."""
    operation = "open_folder"
    raw = _strip_quotes(path)
    check = resolve_open_target(raw, expect="folder")
    if not check["ok"]:
        return _validate_failure(operation, check)
    target = check["path"]

    named_app = str(app or "").strip()
    if named_app:
        result = _open_with_app(operation, target, named_app)
        result["item_type"] = "folder"
        return result

    try:
        _shell_open(target)
    except OSError as exc:
        message, reason = _launch_error_message(target, exc)
        return _failure(operation, target, message, reason=reason,
                        item_type="folder")

    _remember_opened(target, "folder")
    return {
        "success": True,
        "operation": operation,
        "path": target,
        "item_type": "folder",
        "opened": target,
        "via": "File Explorer",
        "message": f"Opened the '{_display_name(target)}' folder.",
    }


def _reveal(path: str) -> None:
    """Open the folder containing ``path`` with it selected in Explorer.

    ``explorer.exe /select,"<path>"`` is the documented reveal command, passed as
    one command line because Explorer parses that line itself. The list form
    (``["explorer.exe", "/select," + path]``) silently does nothing on Windows
    11, which is why this is not a subprocess argument list. A Windows file name
    cannot contain a double quote, so quoting the path cannot be broken out of,
    and no shell is involved.
    """
    if not _IS_WIN:
        raise OSError("Revealing files is only implemented on Windows.")
    if '"' in path:
        raise OSError("That path contains a quote, so it cannot be selected.")
    subprocess.Popen(f'explorer.exe /select,"{path}"', shell=False,
                     creationflags=_NO_WINDOW)


def open_containing_folder(path: Any) -> Dict[str, Any]:
    """Open the folder holding ``path`` and select it in Explorer.

    A folder is selected in *its parent*, which the result states explicitly, so
    "show me where this is" never silently opens the wrong location.
    """
    operation = "open_containing_folder"
    raw = _strip_quotes(path)
    check = resolve_open_target(raw, expect="any")
    if not check["ok"]:
        return _validate_failure(operation, check)
    target = check["path"]
    item_type = check["item_type"]

    if item_type not in ("file", "folder"):
        return _failure(
            operation, target,
            f"'{target}' is not a file or folder, so there is nothing to show.",
            reason="unsupported",
            item_type=item_type)

    parent = Path(target).parent
    if str(parent) == str(Path(target)):
        # A drive root has no parent; opening it is the only sensible reveal.
        try:
            _shell_open(target)
        except OSError as exc:
            message, reason = _launch_error_message(target, exc)
            return _failure(operation, target, message, reason=reason,
                            item_type=item_type)
        return {
            "success": True,
            "operation": operation,
            "path": target,
            "item_type": item_type,
            "folder": target,
            "opened": target,
            "via": "File Explorer",
            "message": f"Opened '{target}', which is a drive root.",
        }

    try:
        _reveal(str(Path(target)))
    except OSError as exc:
        return _failure(
            operation, target,
            f"File Explorer could not be asked to show '{target}': {exc}",
            reason="launch_failed", item_type=item_type)

    name = _display_name(str(Path(target)))
    if item_type == "folder":
        message = (f"'{name}' is a folder, so its location ({parent}) was opened "
                   f"with the folder selected.")
    else:
        message = f"Opened {parent} with '{name}' selected."
    _remember_opened(str(parent), "folder")
    return {
        "success": True,
        "operation": operation,
        "path": target,
        "item_type": item_type,
        "folder": str(parent),
        "revealed": name,
        "via": "File Explorer",
        "message": message,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _usage = (f"python -m {__spec__.name}" if __spec__ else "python <tool file>")
    argv = [part for part in sys.argv[1:] if not part.startswith("--")]
    if not argv:
        print(f"Usage: {_usage} <command> [argument]")
        print("  search <name>          find files and folders by name")
        print("  open <path>            open a file with its default application")
        print("  folder <path>          open a folder in File Explorer")
        print("  reveal <path>          show a file in its folder")
        print("  check <path>           validate a path without opening it")
        raise SystemExit(1)

    command = argv[0].lower()
    argument = " ".join(argv[1:])
    if command == "search":
        outcome = search_files(argument)
    elif command == "open":
        outcome = open_file(argument)
    elif command == "folder":
        outcome = open_folder(argument)
    elif command == "reveal":
        outcome = open_containing_folder(argument)
    elif command == "check":
        outcome = validate_path(argument)
    else:
        outcome = {"error": f"Unknown command '{command}'."}
    print(json.dumps(outcome, indent=2, ensure_ascii=False))

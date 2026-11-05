"""Filesystem management for Jarvis: create, rename, move, copy, delete, find, list.

One tool, one file.  The LLM calls ``file_control(action=...)``; every
operation is implemented here, on top of :mod:`os`, :mod:`shutil` and (for the
Recycle Bin) the Windows shell through :mod:`ctypes`.  Nothing outside this
file is specific to filesystem management.

Design rules that are enforced in code, not in the prompt:

* **No silent overwrite.**  Every destination is checked first; a conflict is
  a structured error, never a replace.
* **No silent permanent deletion.**  Deletion prefers the Windows Recycle Bin
  and never falls back to ``os.remove``/``shutil.rmtree`` when the shell
  refuses.  Permanent deletion needs a server-side, UI-granted approval.
* **Approval is bound.**  Every deletion returns ``approval_required`` and the
  exact operation is authorized only by the local dashboard.  Requests are
  single-use and tied to the resolved paths and flags; model-supplied tokens
  or confirmation flags are ignored.
* **Protected locations.**  Drive roots, the Windows directory, Program
  Files, ProgramData, the user-profile root, the shell folders themselves and
  this application's own folder cannot be renamed, moved or deleted.  The
  check compares resolved real paths with ``commonpath`` — never string
  prefixes.
* **No link recursion.**  Symbolic links and junctions are never followed
  while searching or copying; a top-level link is refused as a source.
* **Honest results.**  Each mutating operation re-checks the filesystem
  afterwards and reports what actually happened.

The known-folder names (Desktop, Documents, Downloads, …) are resolved by the
existing ``open_file`` tool, which asks the Windows shell (OneDrive redirects
these folders, so ``%USERPROFILE%\\Documents`` is not reliable).  Extension
category words ("videos", "documents", …) reuse that tool's fixed alias table.
"""
from __future__ import annotations

import ctypes
import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core import approvals as _approvals

# ── limits ───────────────────────────────────────────────────────────────────
MAX_LIST_LIMIT = 1000
MAX_FIND_LIMIT = 500
MAX_BATCH_ITEMS = 200
FIND_SECONDS = 12.0          # a voice turn must not wait for a full-disk crawl
MAX_WALK_DEPTH = 64

_ACTIONS = ("create", "rename", "move", "copy", "delete", "find", "list",
            "write", "append", "replace_content", "remove_content",
            "clear_content")

# Content editing is format-aware.  Binary Office formats never receive plain
# text; formats not listed here are read-only through read_document.
_PLAIN_EDIT_FORMATS = frozenset({
    "text", "markdown", "log", "html", "xml",
})
_STRUCTURED_EDIT_FORMATS = frozenset({
    "docx", "xlsx", "pptx", "csv", "tsv", "json", "yaml",
})
_READ_ONLY_EDIT_FORMATS = frozenset({
    "pdf", "epub", "rtf", "doc", "xls", "ppt", "odt", "ods", "odp",
})
MAX_EDIT_BYTES = 64 * 1024 * 1024
MAX_EDIT_CELLS = 200_000
PREVIEW_CHARS = 110

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_INVALID_NAME_CHARS = set('<>:"/\\|?*')

# Path aliases the assistant may speak: the existing open_file tool resolves
# these through the shell, so redirected (OneDrive) folders stay correct.
_FOLDER_ALIASES = {
    "desktop": "desktop",
    "documents": "documents",
    "document": "documents",
    "docs": "documents",
    "downloads": "downloads",
    "download": "downloads",
    "pictures": "pictures",
    "picture": "pictures",
    "photos": "pictures",
    "music": "music",
    "videos": "videos",
    "video": "videos",
}

_ALIAS_RE = re.compile(
    r"^(?P<alias>desktop|documents?|docs|downloads?|pictures?|photos|"
    r"music|videos?|home|profile|user\s+profile)"
    r"(?:\s*(?:folder|directory))?\s*(?:[\\/]\s*(?P<rest>.*))?$",
    re.IGNORECASE,
)
_DRIVE_RE = re.compile(r"^(?P<drive>[A-Za-z])(?:\s*drive|\s*:)?\s*(?:[\\/]\s*(?P<rest>.*))?$",
                       re.IGNORECASE)


# ── small helpers ────────────────────────────────────────────────────────────
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _clean(value: Any) -> str:
    """Alias used by the content-editing helpers."""
    return _text(value)


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).casefold()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off", ""):
        return False if text else default
    return default


def _int(value: Any, default: int, low: int, high: int) -> int:
    """Parse an optional limit; unset/invalid uses ``default`` (never 1)."""
    text = _text(value)
    if text in ("", "0", "none", "null"):
        return default
    try:
        number = int(float(text))
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return max(low, min(high, number))


def _strip_quotes(value: Any) -> str:
    text = _text(value)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].strip()
    return text


def _now_text(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp)) \
        if timestamp else ""


def _open_file_tool():
    """The existing open_file tool: shell-folder + extension-category source.

    Imported lazily so tool discovery order can never create a cycle.
    """
    try:
        from actions import get_tool_module
        return get_tool_module("open_file")
    except Exception:  # noqa: BLE001 - the fallbacks below stay safe
        return None


def _known_folder(key: str) -> str:
    module = _open_file_tool()
    if module is None:
        return ""
    try:
        return str(module._home_folder(key))  # noqa: SLF001 - shared shell source
    except Exception:  # noqa: BLE001
        return ""


def _extension_set(value: Any) -> set[str]:
    module = _open_file_tool()
    if module is not None:
        try:
            return set(module._extension_filter(value))  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
    text = _text(value)
    return {
        part.strip().strip("*").lstrip(".").casefold()
        for part in re.split(r"[,;\s]+", text) if part.strip()
    }


def _item_kind(value: Any) -> str:
    text = _text(value).casefold()
    if text in ("file", "files"):
        return "file"
    if text in ("folder", "folders", "directory", "directories", "dir"):
        return "folder"
    return "any"


def _kind_of(path: str) -> str:
    try:
        if os.path.isdir(path):
            return "folder"
        if os.path.isfile(path):
            return "file"
    except OSError:
        pass
    return "other"


def _is_link(path: str) -> bool:
    """True for symbolic links and Windows junctions/reparse points."""
    try:
        if os.path.islink(path):
            return True
    except OSError:
        pass
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _same_path(left: str, right: str) -> bool:
    return (os.path.normcase(os.path.abspath(left))
            == os.path.normcase(os.path.abspath(right)))


def _within(child: str, parent: str) -> bool:
    """``child`` is ``parent`` or lives under it (real paths, no prefixes)."""
    try:
        left = os.path.normcase(os.path.realpath(os.path.abspath(child)))
        right = os.path.normcase(os.path.realpath(os.path.abspath(parent)))
        return os.path.commonpath([left, right]) == right
    except (ValueError, OSError):
        return False


# ── result construction ──────────────────────────────────────────────────────
def _ok(operation: str, message: str, **fields: Any) -> Dict[str, Any]:
    result = {"success": True, "ok": True, "operation": operation,
              "message": message}
    result.update(fields)
    return result


def _fail(operation: str, reason: str, error: str, **fields: Any
          ) -> Dict[str, Any]:
    print(f"[FileControl] {operation} {reason}: {error}")
    result = {"success": False, "ok": False, "operation": operation,
              "reason": reason, "error": error}
    result.update(fields)
    return result


def _clarify(operation: str, message: str, **fields: Any) -> Dict[str, Any]:
    result = {
        "success": False, "ok": False, "operation": operation,
        "clarification_required": True, "reason": "ambiguous",
        "message": message, "error": message,
    }
    result.update(fields)
    return result


def _resolution_failure(operation: str, error: Dict[str, Any]
                        ) -> Dict[str, Any]:
    """Turn a resolution error into the right result shape."""
    if error.get("reason") == "ambiguous":
        return _clarify(operation, str(error.get("error") or "Ambiguous path."))
    return _fail(operation, str(error.get("reason") or "invalid"),
                 str(error.get("error") or "Invalid path."))


# ── path resolution ──────────────────────────────────────────────────────────
def _resolve_path(raw: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Resolve one user path to an absolute, normalised path.

    Accepts full paths, shell-folder aliases ("Downloads\\report.pdf",
    "Documents\\AI Projects"), drive references ("D:\\", "D drive") and
    absolute UNC paths.  A bare relative name has no unambiguous base, so it
    is refused with a clarification instead of guessing a folder.
    """
    text = _strip_quotes(raw)
    if not text:
        return "", {"reason": "empty", "error": "No path was provided."}
    if "\x00" in text or any(ord(ch) < 0x20 for ch in text):
        return "", {"reason": "invalid",
                    "error": "That path contains control characters."}
    if len(text) > 32000:
        return "", {"reason": "invalid", "error": "That path is too long."}

    expanded = os.path.expandvars(os.path.expanduser(text))

    match = _ALIAS_RE.match(expanded)
    if match:
        alias = match.group("alias").casefold()
        rest = (match.group("rest") or "").strip()
        if alias in ("home", "profile", "user profile"):
            base = os.path.expanduser("~")
        else:
            base = _known_folder(_FOLDER_ALIASES[alias])
            if not base:
                return "", {
                    "reason": "ambiguous",
                    "error": (f"I could not locate the {alias.title()} folder "
                              "on this computer. Give the full path instead."),
                }
        candidate = os.path.join(base, rest) if rest else base
        return os.path.normpath(candidate), None

    # "D drive" / "D:" / "D:\folder" — a drive reference is unambiguous.
    # A bare single letter is not treated as a drive; it is ambiguous.
    drive = _DRIVE_RE.match(expanded)
    looks_like_drive = bool(
        drive) and (expanded[1:2] == ":"
                    or re.match(r"^[A-Za-z]\s+drive\b", expanded,
                                re.IGNORECASE) is not None)
    if drive and looks_like_drive:
        letter = drive.group("drive").upper()
        rest = (drive.group("rest") or "").strip()
        root = f"{letter}:\\"
        candidate = os.path.join(root, rest) if rest else root
        return os.path.normpath(candidate), None

    if os.path.isabs(expanded):
        return os.path.normpath(expanded), None

    return "", {
        "reason": "ambiguous",
        "error": (f"'{text}' is not an absolute path. Say which folder it is "
                  "in (Desktop, Documents, Downloads, Pictures, Music, Videos "
                  "or a drive such as D:)."),
    }


def _require_action(action: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
    value = _text(action).casefold()
    if value in _ACTIONS:
        return value, None
    if not value:
        return "", _fail("unknown", "invalid",
                         "An action is required: " + ", ".join(_ACTIONS) + ".")
    return "", _fail(
        "unknown", "unsupported",
        f"Unknown action '{_text(action)}'. Use one of: "
        + ", ".join(_ACTIONS) + ".")


def _validate_name(name: Any) -> Optional[str]:
    """Return an error string for an invalid single filename, else ``""``."""
    text = _strip_quotes(name)
    if not text:
        return "A new name is required."
    if any(char in _INVALID_NAME_CHARS for char in text):
        return ("The new name contains characters Windows does not allow "
                "(< > : \" / \\ | ? *).")
    if text.rstrip(" .") != text:
        return "Windows names cannot end with a space or a dot."
    stem = text.split(".")[0].casefold()
    if stem in _WINDOWS_RESERVED:
        return f"'{text}' is a reserved Windows device name."
    return ""


# ── protection ───────────────────────────────────────────────────────────────
def _system_roots() -> List[str]:
    """Locations where creating, renaming or deleting is never allowed."""
    roots: List[str] = []
    if os.name == "nt":
        drive = os.environ.get("SystemDrive", "C:")
        roots.extend(filter(None, [
            os.environ.get("WINDIR") or os.environ.get("SystemRoot"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramData"),
            f"{drive}\\",
        ]))
    else:
        roots.extend(["/etc", "/bin", "/sbin", "/usr", "/boot", "/dev",
                      "/proc", "/sys"])
    return roots


def _immutable_roots() -> List[str]:
    """Folders that may not be renamed, moved or deleted themselves.

    Their *contents* remain ordinary files, so work inside Downloads still
    works — only the shell-folder root, the profile root and the Jarvis
    application folder itself are off limits.
    """
    roots = [os.path.expanduser("~")]
    for key in ("desktop", "documents", "downloads", "pictures", "music",
                "videos"):
        folder = _known_folder(key)
        if folder:
            roots.append(folder)
    try:
        roots.append(str(Path(__file__).resolve().parents[2]))
    except (OSError, IndexError):
        pass
    return roots


def _is_drive_root(path: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]:[\\/]?", path.strip()))


def _write_blocked_reason(path: str) -> str:
    """Why writing here is forbidden (Windows/Program Files/ProgramData).

    Drive roots are *not* blocked for writing: creating a new folder on ``D:\\``
    is an ordinary request.  Only the system trees are off limits.
    """
    real = os.path.realpath(path)
    for root in _system_roots():
        if not root:
            continue
        trimmed = root.rstrip("\\/")
        if len(trimmed) == 2 and trimmed[1:2] == ":":  # a bare drive root
            continue
        if _within(real, root):
            return f"it is inside the protected system location '{root}'"
    return ""


def _protected_reason(path: str) -> str:
    """Why this path may not be destroyed, or ``""`` when it is ordinary."""
    real = os.path.realpath(path)
    if _is_drive_root(real):
        # Deleting a volume root is never a legitimate file-management action,
        # on the system drive or any other fixed/removable/network volume.
        drive = os.path.splitdrive(real)[0] or real
        return f"it is the root of drive '{drive.rstrip(chr(92))}'"
    for root in _system_roots():
        if not root:
            continue
        if _is_drive_root(root):
            if _same_path(real, root):
                return f"it is the root of drive '{root.rstrip(chr(92))}'"
            continue
        if _within(real, root):
            return f"it is inside the protected system location '{root}'"
    for root in _immutable_roots():
        if root and _same_path(real, root):
            return ("it is a top-level user or application folder; only its "
                    "contents can be managed")
    return ""


# ── approval gate ────────────────────────────────────────────────────────────
def _delete_summary(paths: List[str], permanent: bool,
                    recursive: bool) -> str:
    shown = ", ".join(paths[:3])
    if len(paths) > 3:
        shown += f" and {len(paths) - 3} more"
    method = "permanently" if permanent else "to the Recycle Bin"
    return (f"Delete {shown} {method}"
            + (" including folder contents" if recursive else ""))


def _approval_required(operation: str, summary: str,
                       request: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": False,
        "ok": False,
        "operation": operation,
        "approval_required": True,
        "reason": "approval_required",
        "approval_summary": summary,
        "approval_expires_at": str(request.get("expires_at") or ""),
        "_approval_id": str(request.get("approval_id") or ""),
        "message": (
            f"{summary}. The user must approve this in the Jarvis interface."),
        "error": (
            "Approval required in the Jarvis interface. Do not retry until "
            "the user approves, and never supply confirmation flags or "
            "tokens."),
    }


def _authorize_delete(paths: List[str], permanent: bool,
                      recursive: bool) -> Tuple[bool, Dict[str, Any]]:
    """Return ``(allowed, result)``; result is the tool response when blocked."""
    canonical = sorted(
        os.path.normcase(os.path.abspath(path)) for path in paths)
    summary = _delete_summary(paths, permanent, recursive)
    try:
        allowed, request, _reason = _approvals.get_service().authorize(
            tool="file_control",
            operation="delete",
            target=summary,
            parameters={
                "paths": canonical,
                "permanent": bool(permanent),
                "recursive": bool(recursive),
            },
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        return False, {
            "success": False,
            "ok": False,
            "operation": "delete",
            "approval_unavailable": True,
            "reason": "approval_unavailable",
            "error": (
                "Approval is unavailable, so nothing was deleted: "
                f"{type(exc).__name__}: {exc}"),
        }
    if not allowed:
        return False, _approval_required("delete", summary, request)
    return True, {}


# ── target computation ───────────────────────────────────────────────────────
def _target_path(source: str, destination: str) -> str:
    """Where ``source`` lands when moved/copied to ``destination``.

    An existing directory (or a path ending in a separator) is a container:
    the item keeps its name inside it.  Anything else is the final path.
    """
    if os.path.isdir(destination) or destination.endswith(("\\", "/")):
        return os.path.join(destination, os.path.basename(source.rstrip("\\/")))
    return destination


def _conflict(target: str, source: str = "") -> bool:
    if not os.path.exists(target) and not os.path.lexists(target):
        return False
    # A case-only rename of the same item is not a conflict on Windows.
    return not (source and _same_path(target, source))


def _validate_transfer(source: str, destination: str,
                       operation: str) -> Optional[Dict[str, Any]]:
    if not os.path.lexists(source):
        return _fail(operation, "not_found", f"'{source}' does not exist.",
                     path=source)
    if _is_link(source):
        return _fail(operation, "unsupported",
                     "Symbolic links and junctions are not moved or copied by "
                     "this tool.", path=source)
    reason = _protected_reason(source)
    if reason:
        return _fail(operation, "protected",
                     f"'{source}' cannot be changed because {reason}.",
                     path=source)
    if os.path.isdir(source) and _within(destination, source):
        return _fail(operation, "invalid_destination",
                     "A folder cannot be moved or copied inside itself.",
                     path=source, destination=destination)
    if _conflict(destination, source):
        return _fail(operation, "conflict",
                     f"'{destination}' already exists; nothing was overwritten.",
                     path=source, destination=destination)
    blocked = _write_blocked_reason(destination)
    if blocked:
        return _fail(operation, "protected",
                     f"The destination cannot be used because {blocked}.",
                     path=source, destination=destination)
    parent = os.path.dirname(destination.rstrip("\\/")) or destination
    if not os.path.isdir(parent):
        return _fail(operation, "not_found",
                     f"The destination folder '{parent}' does not exist.",
                     destination=destination)
    return None


# ── operations ───────────────────────────────────────────────────────────────
def _op_create(args: Dict[str, Any]) -> Dict[str, Any]:
    path, error = _resolve_path(args.get("source_path"))
    if error:
        return _resolution_failure("create", error)
    blocked = _write_blocked_reason(path)
    if blocked:
        return _fail("create", "protected",
                     f"'{path}' cannot be created because {blocked}.",
                     path=path)
    kind = _item_kind(args.get("item_type"))
    if kind == "any":
        kind = "folder" if (path.endswith(("\\", "/"))
                            or not os.path.splitext(path)[1]) else "file"
    name_error = _validate_name(os.path.basename(path.rstrip("\\/")))
    if name_error:
        return _fail("create", "invalid", name_error, path=path)
    if os.path.lexists(path):
        return _fail("create", "conflict",
                     f"'{path}' already exists; nothing was overwritten.",
                     path=path)
    recursive = _bool(args.get("recursive"))
    parent = os.path.dirname(path.rstrip("\\/"))
    if kind == "folder" and parent and not os.path.isdir(parent):
        if not recursive:
            return _fail("create", "not_found",
                         f"The folder '{parent}' does not exist. Set "
                         "recursive=true to create it as well.", path=path)
    try:
        if kind == "folder":
            if recursive:
                os.makedirs(path, exist_ok=False)
            else:
                os.mkdir(path)
        else:
            if parent and not os.path.isdir(parent):
                if recursive:
                    os.makedirs(parent, exist_ok=True)
                else:
                    return _fail("create", "not_found",
                                 f"The folder '{parent}' does not exist.",
                                 path=path)
            # "create a Word file with text" must actually write the text. The
            # create action previously made an empty raw file and silently
            # ignored content, and an empty .docx/.xlsx/.pptx created that way
            # was not even a valid Office document.
            content_text = str(args.get("content") or "")
            if content_text:
                write_args = dict(args)
                write_args["source_path"] = path
                return _op_edit_content(write_args, "write")
            fmt = _edit_format(path)
            empty_args = dict(args)
            empty_args["source_path"] = path
            empty_args["content"] = ""
            if fmt in _PLAIN_EDIT_FORMATS:
                return _edit_plain_text(path, "write", empty_args, fmt)
            if fmt == "docx":
                return _edit_docx(path, "write", empty_args)
            if fmt == "xlsx":
                return _edit_xlsx(path, "write", empty_args)
            if fmt == "pptx":
                return _edit_pptx(path, "write", empty_args)
            if fmt in ("csv", "tsv"):
                return _edit_csv(path, "write", empty_args, fmt)
            if fmt in ("json", "yaml"):
                return _edit_json_yaml(path, "write", empty_args, fmt)
            with open(path, "x", encoding="utf-8"):
                pass
    except FileExistsError:
        return _fail("create", "conflict",
                     f"'{path}' already exists; nothing was overwritten.",
                     path=path)
    except PermissionError:
        return _fail("create", "denied",
                     f"Windows denied permission to create '{path}'.", path=path)
    except OSError as exc:
        return _fail("create", "failed",
                     f"Could not create '{path}': {exc.strerror or exc}.",
                     path=path)
    if not os.path.lexists(path):
        return _fail("create", "failed",
                     f"'{path}' was not created.", path=path)
    return _ok("create", f"Created {kind} '{path}'.", path=path,
               item_type="folder" if kind == "folder" else "file")


def _op_rename(args: Dict[str, Any]) -> Dict[str, Any]:
    source, error = _resolve_path(args.get("source_path"))
    if error:
        return _resolution_failure("rename", error)
    if not os.path.lexists(source):
        return _fail("rename", "not_found", f"'{source}' does not exist.",
                     path=source)
    reason = _protected_reason(source)
    if reason:
        return _fail("rename", "protected",
                     f"'{source}' cannot be renamed because {reason}.",
                     path=source)
    new_name = _strip_quotes(args.get("new_name"))
    name_error = _validate_name(new_name)
    if name_error:
        return _fail("rename", "invalid", name_error, path=source)
    if any(sep in new_name for sep in ("\\", "/")):
        return _fail("rename", "invalid",
                     "Give only the new name — use move to put the item in "
                     "another folder.", path=source)
    destination = os.path.join(os.path.dirname(source.rstrip("\\/")), new_name)
    if _conflict(destination, source):
        return _fail("rename", "conflict",
                     f"'{destination}' already exists; nothing was overwritten.",
                     path=source, destination=destination)
    try:
        os.rename(source, destination)
    except PermissionError:
        return _fail("rename", "denied",
                     f"Windows denied permission to rename '{source}'.",
                     path=source)
    except OSError as exc:
        return _fail("rename", "failed",
                     f"Could not rename '{source}': {exc.strerror or exc}.",
                     path=source)
    if not os.path.lexists(destination):
        return _fail("rename", "failed",
                     f"'{destination}' was not created.", path=source)
    return _ok("rename", f"Renamed to '{destination}'.", path=source,
               destination=destination)


def _first_link(root: str) -> str:
    """Path of the first link/junction inside a tree, or an empty string."""
    for current, dirs, files in os.walk(os.path.abspath(root)):
        for name in dirs + files:
            candidate = os.path.join(current, name)
            if _is_link(candidate):
                return candidate
    return ""


def _transfer_one(source: str, destination: str, operation: str,
                  recursive: bool) -> Dict[str, Any]:
    if _same_path(source, destination):
        return _fail(operation, "conflict",
                     f"'{source}' is already at that location; nothing to do.",
                     path=source, destination=destination)
    problem = _validate_transfer(source, destination, operation)
    if problem:
        return problem
    skipped: List[str] = []
    if operation == "move" and os.path.isdir(source) and not _is_link(source):
        # shutil.move falls back to copytree across volumes and CPython's
        # copytree follows junctions. Refuse rather than duplicate another
        # drive's tree or delete the source links.
        link = _first_link(source)
        if link:
            return _fail(
                operation, "unsafe_transfer",
                f"'{source}' contains a link or junction ('{link}'); moving a "
                "linked tree is not supported. Remove the link first.",
                path=source, destination=destination)
    try:
        if operation == "move":
            shutil.move(source, destination)
        elif os.path.isdir(source):
            skipped = _copy_tree(source, destination)
        else:
            shutil.copy2(source, destination)
    except PermissionError:
        return _fail(operation, "denied",
                     f"Windows denied access to '{source}' or "
                     f"'{destination}'.", path=source,
                     destination=destination)
    except OSError as exc:
        return _fail(operation, "failed",
                     f"Could not {operation} '{source}': "
                     f"{exc.strerror or exc}.", path=source,
                     destination=destination)
    expected = destination
    if operation == "move" and not os.path.lexists(expected):
        return _fail(operation, "failed",
                     "The move did not complete; the item is not at the "
                     "destination.", path=source, destination=destination)
    if operation == "copy" and not os.path.lexists(expected):
        return _fail(operation, "failed",
                     "The copy did not create the destination.",
                     path=source, destination=destination)
    fields: Dict[str, Any] = {"source": source, "destination": destination}
    if skipped:
        fields["skipped_links"] = len(skipped)
    verb = "Moved" if operation == "move" else "Copied"
    return _ok(operation, f"{verb} to '{destination}'.", **fields)


def _copy_tree(source: str, destination: str) -> List[str]:
    """Recursive copy that never follows links or junctions.

    Links inside the tree are skipped and returned, which is what keeps a
    junction from duplicating a whole other drive.
    """
    skipped: List[str] = []
    root = os.path.abspath(source)
    for current, dirs, files in os.walk(root):
        relative = os.path.relpath(current, root)
        target_dir = destination if relative == "." \
            else os.path.join(destination, relative)
        os.makedirs(target_dir, exist_ok=True)
        keep: List[str] = []
        for name in dirs:
            full = os.path.join(current, name)
            if _is_link(full):
                skipped.append(full)
                continue
            keep.append(name)
        dirs[:] = keep
        for name in files:
            full = os.path.join(current, name)
            if _is_link(full):
                skipped.append(full)
                continue
            shutil.copy2(full, os.path.join(target_dir, name))
    if skipped:
        print(f"[FileControl] copy skipped {len(skipped)} link(s) under "
              f"'{source}'.")
    return skipped


def _transfer(args: Dict[str, Any], operation: str) -> Dict[str, Any]:
    sources, error = _collect_sources(args, operation)
    if error:
        return error
    destination, error = _resolve_path(args.get("destination_path"))
    if error:
        return _resolution_failure(operation, error)
    if operation == "copy" and _is_link(destination):
        return _fail(operation, "unsupported",
                     "The destination is a link; give a real folder.",
                     destination=destination)
    items: List[Dict[str, Any]] = []
    for source in sources:
        target = _target_path(source, destination)
        items.append(_transfer_one(source, target, operation,
                                   _bool(args.get("recursive"))))
    return _batch_result(operation, items)


def _batch_result(operation: str, items: List[Dict[str, Any]],
                  message: Optional[str] = None) -> Dict[str, Any]:
    succeeded = [item for item in items if item.get("success")]
    failed = [item for item in items if not item.get("success")]
    if len(items) == 1:
        return items[0]
    summary = message or (
        f"{operation.title()}: {len(succeeded)} succeeded, "
        f"{len(failed)} failed.")
    result: Dict[str, Any] = {
        "success": bool(succeeded) and not failed,
        "ok": bool(succeeded) and not failed,
        "operation": operation,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "items": items,
        "message": summary,
    }
    if failed and not succeeded:
        result["error"] = summary
        result["reason"] = "failed"
    elif failed:
        result["error"] = summary
        result["reason"] = "partial"
    return result


# ── delete ───────────────────────────────────────────────────────────────────
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", ctypes.c_bool),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def _recycle_unavailable_reason(path: str) -> str:
    """Refuse media where Windows would silently delete instead of recycle."""
    if os.name != "nt":
        return "The Recycle Bin is only available on Windows."
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive or drive.startswith("\\\\"):
        return "Network locations cannot use the Recycle Bin."
    try:
        kind = int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\"))
    except Exception:
        # If Windows cannot identify the volume, do not risk a permanent
        # delete while reporting a recycle-bin operation.
        return "The volume type could not be verified."
    # DRIVE_REMOVABLE=2, DRIVE_REMOTE=4, DRIVE_CDROM=5.
    if kind in (2, 4, 5):
        label = {2: "Removable", 4: "Network", 5: "Optical"}.get(
            kind, "This")
        return f"{label} volumes cannot use the Recycle Bin."
    return ""


def _send_to_recycle_bin(path: str) -> Tuple[bool, str]:
    """Move one path to the Windows Recycle Bin when the volume supports it."""
    blocked = _recycle_unavailable_reason(path)
    if blocked:
        return False, blocked
    try:
        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOERRORUI = 0x0400
        operation = _SHFILEOPSTRUCTW(
            None, FO_DELETE, path + "\0", None,
            FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
            False, None, None)
        code = ctypes.windll.shell32.SHFileOperationW(
            ctypes.byref(operation))
        if code != 0:
            return False, (f"Windows refused to move it to the Recycle Bin "
                           f"(code {code}).")
        if operation.fAnyOperationsAborted:
            return False, "The Recycle Bin operation was cancelled."
        return True, ""
    except Exception as exc:  # noqa: BLE001 - the shell API is best-effort
        return False, f"Recycle Bin unavailable: {type(exc).__name__}."


def _rmtree_onerror(function, path, _info):
    try:
        os.chmod(path, 0o700)
        function(path)
    except OSError:
        pass


def _delete_permanent(path: str) -> None:
    if _is_link(path):
        if os.path.isdir(path):
            os.rmdir(path)   # removes the junction itself, not its target
        else:
            os.remove(path)
        return
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=_rmtree_onerror)
    else:
        os.remove(path)


def _delete_one(path: str, permanent: bool, recursive: bool,
                prechecked: bool = False) -> Dict[str, Any]:
    if not os.path.lexists(path):
        return _fail("delete", "not_found", f"'{path}' does not exist.",
                     path=path)
    reason = _protected_reason(path)
    if reason:
        return _fail("delete", "protected",
                     f"'{path}' cannot be deleted because {reason}.",
                     path=path)
    kind = _kind_of(path)
    if kind == "folder" and not _is_link(path):
        try:
            empty = not any(True for _ in os.scandir(path))
        except PermissionError:
            return _fail("delete", "denied",
                         f"Windows denied access to '{path}'.", path=path)
        except OSError as exc:
            return _fail("delete", "failed",
                         f"Could not inspect '{path}': {exc}.", path=path)
        if not empty and not recursive:
            return _fail("delete", "folder_not_empty",
                         f"'{path}' is not empty. Ask for a recursive delete "
                         "if that is what the user wants.", path=path)
    if permanent:
        try:
            _delete_permanent(path)
        except PermissionError:
            return _fail("delete", "denied",
                         f"Windows denied permission to delete '{path}'.",
                         path=path)
        except OSError as exc:
            return _fail("delete", "failed",
                         f"Could not delete '{path}': {exc.strerror or exc}.",
                         path=path)
        method = "permanent"
    else:
        ok, why = _send_to_recycle_bin(path)
        if not ok:
            return _fail("delete", "recycle_failed",
                         f"'{path}' was not deleted: {why}", path=path)
        method = "recycle_bin"
    if os.path.lexists(path):
        return _fail("delete", "failed",
                     f"'{path}' still exists after the delete.", path=path,
                     method=method)
    return _ok("delete", f"Deleted '{path}' ({method}).", path=path,
               item_type=kind, method=method)


def _op_delete(args: Dict[str, Any]) -> Dict[str, Any]:
    sources, error = _collect_sources(args, "delete")
    if error:
        return error
    permanent = _bool(args.get("permanent"))
    recursive = _bool(args.get("recursive"))
    # Refuse early when a non-empty folder is targeted without recursion.
    for path in sources:
        if not os.path.lexists(path):
            return _fail("delete", "not_found", f"'{path}' does not exist.",
                         path=path)
        reason = _protected_reason(path)
        if reason:
            return _fail("delete", "protected",
                         f"'{path}' cannot be deleted because {reason}.",
                         path=path)
        if (os.path.isdir(path) and not _is_link(path) and not recursive):
            try:
                if any(True for _ in os.scandir(path)):
                    return _fail(
                        "delete", "folder_not_empty",
                        f"'{path}' is not empty. Ask the user before deleting "
                        "a folder and its contents (recursive=true).",
                        path=path)
            except OSError as exc:
                return _fail("delete", "failed",
                             f"Could not inspect '{path}': {exc}.", path=path)

    allowed, approval_result = _authorize_delete(
        sources, permanent, recursive)
    if not allowed:
        return approval_result

    # Revalidate right before the destructive calls.
    for path in sources:
        if not os.path.lexists(path):
            return _fail("delete", "not_found",
                         f"'{path}' disappeared before it could be deleted.",
                         path=path)
        reason = _protected_reason(path)
        if reason:
            return _fail("delete", "protected",
                         f"'{path}' cannot be deleted because {reason}.",
                         path=path)

    items = [_delete_one(path, permanent, recursive) for path in sources]
    return _batch_result("delete", items)


# ── find / list ──────────────────────────────────────────────────────────────
def _metadata(path: str) -> Dict[str, Any]:
    kind = _kind_of(path)
    entry: Dict[str, Any] = {
        "path": path,
        "name": os.path.basename(path.rstrip("\\/")) or path,
        "type": kind,
    }
    try:
        stat = os.stat(path)
        entry["modified"] = _now_text(stat.st_mtime)
        if kind == "file":
            entry["size"] = stat.st_size
            entry["size_mb"] = round(stat.st_size / (1024 * 1024), 3)
    except OSError:
        pass
    return entry


def _matches(entry: Dict[str, Any], query: str, extensions: set[str],
             kind: str, min_size_mb: float) -> bool:
    name = entry["name"]
    if kind != "any" and entry["type"] != kind:
        return False
    if extensions:
        suffix = os.path.splitext(name)[1].lstrip(".").casefold()
        if entry["type"] == "file" and suffix not in extensions:
            return False
        if entry["type"] == "folder":
            return False
    if query and query.casefold() not in name.casefold():
        return False
    if min_size_mb > 0:
        # A folder has no meaningful byte size: size filters only match files.
        if entry["type"] != "file":
            return False
        size_mb = float(entry.get("size_mb") or 0.0)
        if size_mb < min_size_mb:
            return False
    return True


def _candidate_dir(raw: Any, operation: str, default_home: bool = False
                   ) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not _text(raw) and default_home:
        return os.path.expanduser("~"), None
    path, error = _resolve_path(raw)
    if error:
        return "", _resolution_failure(operation, error)
    if not os.path.isdir(path):
        return "", _fail(operation, "not_found",
                         f"'{path}' is not an existing folder.", path=path)
    return path, None


def _walk_entries(root: str, recursive: bool, deadline: float,
                  exclude: str = "") -> Iterable[Dict[str, Any]]:
    """Yield entry metadata below ``root``; never follows links."""
    root = os.path.abspath(root)
    stack: List[Tuple[str, int]] = [(root, 0)]
    while stack:
        if time.monotonic() > deadline:
            return
        current, depth = stack.pop()
        try:
            with os.scandir(current) as rows:
                for row in rows:
                    if time.monotonic() > deadline:
                        return
                    path = os.path.join(current, row.name)
                    if exclude and _within(path, exclude):
                        continue
                    try:
                        is_dir = row.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir and _is_link(path):
                        continue
                    entry = _metadata(path)
                    yield entry
                    if recursive and is_dir and depth < MAX_WALK_DEPTH:
                        stack.append((path, depth + 1))
        except (PermissionError, OSError):
            continue


def _op_find(args: Dict[str, Any]) -> Dict[str, Any]:
    root, error = _candidate_dir(args.get("source_path"), "find")
    if error:
        return error
    recursive = _bool(args.get("recursive"), default=True)
    limit = _int(args.get("limit"), 50, 1, MAX_FIND_LIMIT)
    query = _strip_quotes(args.get("query"))
    extensions = _extension_set(args.get("extension"))
    kind = _item_kind(args.get("item_type"))
    try:
        min_size = float(args.get("min_size_mb") or 0)
    except (TypeError, ValueError):
        min_size = 0.0
    matches: List[Dict[str, Any]] = []
    deadline = time.monotonic() + FIND_SECONDS
    for entry in _walk_entries(root, recursive, deadline):
        if _matches(entry, query, extensions, kind, min_size):
            matches.append(entry)
            if len(matches) >= limit:
                break
    matches.sort(key=lambda item: (item["type"] != "folder",
                                   item.get("modified", "")),
                 reverse=False)
    return _ok("find",
               f"Found {len(matches)} matching item(s) in '{root}'.",
               source=root, count=len(matches), results=matches,
               truncated=len(matches) >= limit)


def _op_list(args: Dict[str, Any]) -> Dict[str, Any]:
    root, error = _candidate_dir(args.get("source_path"), "list",
                                 default_home=True)
    if error:
        return error
    limit = _int(args.get("limit"), 100, 1, MAX_LIST_LIMIT)
    query = _strip_quotes(args.get("query"))
    extensions = _extension_set(args.get("extension"))
    kind = _item_kind(args.get("item_type"))
    entries: List[Dict[str, Any]] = []
    try:
        with os.scandir(root) as rows:
            for row in rows:
                path = os.path.join(root, row.name)
                entry = _metadata(path)
                if _matches(entry, query, extensions, kind, 0.0):
                    entries.append(entry)
    except PermissionError:
        return _fail("list", "denied",
                     f"Windows denied access to '{root}'.", path=root)
    except OSError as exc:
        return _fail("list", "failed",
                     f"Could not read '{root}': {exc}.", path=root)
    entries.sort(key=lambda item: (item["type"] != "folder",
                                   item["name"].casefold()))
    truncated = len(entries) > limit
    entries = entries[:limit]
    return _ok("list", f"Listed {len(entries)} item(s) in '{root}'.",
               source=root, count=len(entries), items=entries,
               truncated=truncated)


# ── source collection (batch) ────────────────────────────────────────────────
def _collect_sources(args: Dict[str, Any], operation: str
                     ) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """Resolve the sources, expanding a filtered folder into its matches."""
    raw_list = args.get("source_paths")
    if isinstance(raw_list, list) and raw_list:
        filters = [key for key in ("query", "extension", "item_type")
                   if _text(args.get(key))]
        if filters:
            return [], _fail(
                operation, "invalid",
                "source_paths cannot be combined with "
                + ", ".join(filters)
                + "; list the exact items or give one folder to filter.")
        if len(raw_list) > MAX_BATCH_ITEMS:
            return [], _fail(operation, "invalid",
                             f"At most {MAX_BATCH_ITEMS} items per batch.")
        sources: List[str] = []
        for raw in raw_list:
            path, error = _resolve_path(raw)
            if error:
                return [], _fail(operation, error["reason"],
                                 f"{error['error']} (batch item: {_text(raw)})")
            sources.append(path)
        return sources, None

    source, error = _resolve_path(args.get("source_path"))
    if error:
        return [], _resolution_failure(operation, error)

    filters_given = bool(_text(args.get("extension"))
                         or _text(args.get("query"))
                         or _text(args.get("item_type")))
    if (filters_given and os.path.isdir(source) and not _is_link(source)):
        destination, dest_error = _resolve_path(args.get("destination_path"))
        exclude = destination if operation in ("move", "copy") \
            and not dest_error else ""
        extensions = _extension_set(args.get("extension"))
        kind = _item_kind(args.get("item_type"))
        query = _strip_quotes(args.get("query"))
        recursive = _bool(args.get("recursive"))
        matches: List[str] = []
        for entry in _walk_entries(source, recursive,
                                   time.monotonic() + FIND_SECONDS,
                                   exclude=exclude):
            if entry["type"] == "folder" and kind != "folder":
                continue  # extension/query batches act on files
            if _matches(entry, query, extensions, kind, 0.0):
                matches.append(entry["path"])
                if len(matches) >= MAX_BATCH_ITEMS:
                    break
        if not matches:
            return [], _fail(
                operation, "not_found",
                f"No items in '{source}' matched the given filters.",
                path=source)
        return matches, None

    return [source], None


def _document_module():
    """The read_document tool, used only for shared format/encoding helpers."""
    try:
        from actions import get_tool_module
        return get_tool_module("read_document")
    except Exception:  # noqa: BLE001 - editing still has its own fallbacks
        return None


def _edit_format(path: str) -> str:
    module = _document_module()
    if module is None:
        return ""
    try:
        fmt = module.format_from_extension(path)
        if fmt:
            return fmt
        if os.path.exists(path):
            fmt, _source = module.detect_format(path)
            return fmt
    except Exception:  # noqa: BLE001
        pass
    return ""


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(text: str, encoding: str = "utf-8") -> str:
    try:
        data = text.encode(encoding)
    except Exception:  # noqa: BLE001
        data = text.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def _preview(value: Any, limit: int = PREVIEW_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _detect_newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text and "\n" not in text:
        return "\r"
    return "\n"


def _normalize_newlines(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline) if newline != "\n" else normalized


def _temp_for(path: str) -> str:
    folder = os.path.dirname(path) or "."
    suffix = os.path.splitext(path)[1] or ".tmp"
    descriptor, temp = tempfile.mkstemp(prefix=".jarvis-edit-",
                                        suffix=suffix, dir=folder)
    os.close(descriptor)
    return temp


def _commit_temp(path: str, temp: str) -> None:
    try:
        if os.path.exists(path):
            try:
                shutil.copymode(path, temp)
            except OSError:
                pass
        os.replace(temp, path)
    finally:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass


def _atomic_write_bytes(path: str, data: bytes) -> None:
    temp = _temp_for(path)
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _commit_temp(path, temp)
    finally:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass


def _create_bytes(path: str, data: bytes) -> None:
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


def _read_text_for_edit(path: str, encoding: str = ""
                        ) -> Tuple[str, str, List[str]]:
    module = _document_module()
    with open(path, "rb") as handle:
        data = handle.read(MAX_EDIT_BYTES)
    if module is not None:
        text, used, warnings = module._decode_text(data, encoding)
        return text, used, warnings
    used = encoding or "utf-8"
    return data.decode(used), used, []


def _authorize_content_edit(operation: str, path: str,
                            parameters: Dict[str, Any],
                            summary: str) -> Optional[Dict[str, Any]]:
    """Return a pending/unavailable result, or ``None`` when allowed."""
    try:
        allowed, request, _reason = _approvals.get_service().authorize(
            tool="file_control",
            operation=operation,
            target=path,
            parameters=parameters,
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        return {
            "success": False,
            "ok": False,
            "operation": operation,
            "approval_unavailable": True,
            "reason": "approval_unavailable",
            "error": ("Approval is unavailable, so the file was not changed: "
                      f"{type(exc).__name__}: {exc}"),
            "path": path,
        }
    if not allowed:
        return _approval_required(operation, summary, request)
    return None


def _edit_base_result(operation: str, path: str, fmt: str,
                      message: str, **fields: Any) -> Dict[str, Any]:
    result = _ok(operation, message, path=path, format=fmt, **fields)
    return result


def _edit_check_target(path: str, operation: str,
                       allow_new: bool = False
                       ) -> Optional[Dict[str, Any]]:
    blocked = _write_blocked_reason(path)
    if blocked:
        return _fail(operation, "protected",
                     f"'{path}' cannot be changed because {blocked}.",
                     path=path)
    if os.path.lexists(path):
        if _is_link(path):
            return _fail(operation, "unsupported",
                         "Symbolic links and junctions are not edited by this "
                         "tool; edit the real file instead.", path=path)
        if not os.path.isfile(path):
            return _fail(operation, "not_a_file",
                         f"'{path}' is not a regular file.", path=path)
        try:
            if os.path.getsize(path) > MAX_EDIT_BYTES:
                return _fail(
                    operation, "file_too_large",
                    f"'{path}' is larger than the "
                    f"{MAX_EDIT_BYTES // (1024 * 1024)} MB editing limit.",
                    path=path)
        except OSError as exc:
            return _fail(operation, "unreadable",
                         f"Could not inspect '{path}': {exc}.", path=path)
        return None
    if not allow_new:
        return _fail(operation, "not_found", f"'{path}' does not exist.",
                     path=path)
    name_error = _validate_name(os.path.basename(path.rstrip("\\/")))
    if name_error:
        return _fail(operation, "invalid", name_error, path=path)
    parent = os.path.dirname(path.rstrip("\\/"))
    if parent and not os.path.isdir(parent):
        return _fail(operation, "not_found",
                     f"The folder '{parent}' does not exist. Create it first.",
                     path=path)
    return None


def _edit_conflict_result(operation: str, path: str) -> Dict[str, Any]:
    return _fail(
        operation, "conflict",
        f"'{path}' changed while the approval was pending; nothing was "
        "written. Ask again so the new content is reviewed.", path=path)


def _encode_for_write(text: str, encoding: str) -> Tuple[Optional[bytes],
                                                          Optional[Dict[str, Any]]]:
    try:
        return text.encode(encoding or "utf-8"), None
    except LookupError:
        return None, _fail("write", "invalid_encoding",
                           f"Unknown text encoding '{encoding}'.")
    except UnicodeEncodeError as exc:
        return None, _fail(
            "write", "encoding_error",
            f"The content cannot be represented in {encoding}: {exc.reason}.")


# ── plain text editing ───────────────────────────────────────────────────────
def _edit_plain_text(path: str, action: str, args: Dict[str, Any],
                     fmt: str) -> Dict[str, Any]:
    content = str(args.get("content") or "")
    find_text = str(args.get("find_text") or "")
    replacement = str(args.get("replacement_text") or "")
    requested_encoding = _clean(args.get("encoding"))
    exists = os.path.lexists(path)

    if action == "write" and not exists:
        problem = _edit_check_target(path, action, allow_new=True)
        if problem:
            return problem
        encoding = requested_encoding or "utf-8"
        data, error = _encode_for_write(content, encoding)
        if error:
            return error
        print(f"[FileControl] write path={path} format={fmt} "
              f"content_supplied={content != ''} content_length={len(content)}")
        try:
            _create_bytes(path, data or b"")
        except FileExistsError:
            return _fail(action, "conflict",
                         f"'{path}' already exists; nothing was overwritten.",
                         path=path)
        except OSError as exc:
            return _fail(action, "failed",
                         f"Could not create '{path}': {exc}.", path=path)
        verify_text, _verify_encoding, _warnings = _read_text_for_edit(
            path, encoding)
        if verify_text != content:
            print(f"[FileControl] write verify FAILED path={path} "
                  f"expected={len(content)} actual={len(verify_text)}")
            try:
                os.unlink(path)
            except OSError:
                pass
            return _fail(action, "verify_failed",
                         f"'{path}' was created but its content did not match "
                         "the requested text, so it was removed.", path=path)
        print(f"[FileControl] write verified path={path} chars={len(verify_text)}")
        return _edit_base_result(
            action, path, fmt, f"Created '{path}' ({len(content)} characters).",
            encoding=encoding, characters_written=len(content),
            content_length=len(content), verified=True)

    problem = _edit_check_target(path, action)
    if problem:
        return problem

    old_text, used_encoding, warnings = _read_text_for_edit(
        path, requested_encoding)
    encoding = requested_encoding or used_encoding or "utf-8"
    newline = _detect_newline(old_text)
    old_digest = _file_sha256(path)
    replacements = 0

    if action == "write":
        new_text = content
        summary = (f"Replace all content of '{os.path.basename(path)}' "
                   f"with {len(content)} characters")
    elif action == "append":
        addition = _normalize_newlines(content, newline)
        if (addition and old_text and not old_text.endswith(("\n", "\r"))
                and not addition.startswith(("\n", "\r"))):
            addition = newline + addition
        new_text = old_text + addition
        summary = (f"Append {len(addition)} characters to "
                   f"'{os.path.basename(path)}'")
    elif action in ("replace_content", "remove_content"):
        if not find_text:
            return _fail(action, "invalid_argument",
                         "find_text is required and cannot be empty.",
                         path=path)
        replacements = old_text.count(find_text)
        if replacements == 0:
            return _fail(action, "not_found",
                         f"'{_preview(find_text)}' was not found in "
                         f"'{path}'; the file was not changed.", path=path)
        replacement_text = replacement if action == "replace_content" else ""
        new_text = old_text.replace(find_text, replacement_text)
        verb = "Replace" if action == "replace_content" else "Remove"
        summary = (f"{verb} {replacements} occurrence(s) of "
                   f"'{_preview(find_text)}' in "
                   f"'{os.path.basename(path)}'")
    elif action == "clear_content":
        if not old_text:
            return _edit_base_result(
                action, path, fmt, f"'{path}' was already empty.",
                characters_written=0)
        new_text = ""
        summary = f"Clear all content of '{os.path.basename(path)}'"
    else:  # pragma: no cover - dispatch only routes known actions
        return _fail(action, "unsupported", f"Unsupported action '{action}'.")

    if new_text == old_text:
        return _edit_base_result(
            action, path, fmt, f"'{path}' already has that content; nothing "
            "was changed.", characters_written=0)

    parameters = {
        "path": os.path.normcase(os.path.abspath(path)),
        "format": fmt,
        "encoding": encoding,
        "current_sha256": old_digest,
        "new_sha256": _text_sha256(new_text, encoding),
        "content_chars": len(new_text),
        "find_sha256": (_text_sha256(find_text) if find_text else ""),
        "replacement_sha256": (
            _text_sha256(replacement) if replacement else ""),
        "replacements": replacements,
    }
    blocked = _authorize_content_edit(action, path, parameters, summary)
    if blocked:
        return blocked
    if _file_sha256(path) != old_digest:
        return _edit_conflict_result(action, path)

    data, error = _encode_for_write(new_text, encoding)
    if error:
        return error
    try:
        _atomic_write_bytes(path, data or b"")
    except PermissionError:
        return _fail(action, "denied",
                     f"Windows denied permission to write '{path}'.",
                     path=path)
    except OSError as exc:
        return _fail(action, "failed",
                     f"Could not write '{path}': {exc}.", path=path)

    verify_text, verify_encoding, _verify_warnings = _read_text_for_edit(
        path, encoding)
    if verify_text != new_text:
        print(f"[FileControl] {action} verify FAILED path={path} "
              f"expected={len(new_text)} actual={len(verify_text)}")
        return _fail(action, "verify_failed",
                     f"'{path}' did not contain the expected content after "
                     "writing.", path=path)
    print(f"[FileControl] {action} verified path={path} "
          f"chars={len(verify_text)}")
    fields: Dict[str, Any] = {
        "encoding": verify_encoding,
        "characters_written": len(content) if action in
        ("write", "append") else len(new_text),
        "replacements": replacements,
        "count": replacements,
    }
    if warnings:
        fields["warnings"] = warnings
    verb = {
        "write": "Wrote", "append": "Appended to",
        "replace_content": "Replaced content in",
        "remove_content": "Removed content from",
        "clear_content": "Cleared",
    }[action]
    return _edit_base_result(
        action, path, fmt,
        f"{verb} '{path}'"
        + (f" ({replacements} occurrence(s))." if replacements else "."),
        **fields)


# ── DOCX ─────────────────────────────────────────────────────────────────────
def _docx_add_lines(document: Any, content: str) -> int:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    added = 0
    for line in lines:
        document.add_paragraph(line)
        added += 1
    return added


def _docx_clear(document: Any) -> None:
    body = document.element.body
    for child in list(body):
        if str(getattr(child, "tag", "")).endswith("}sectPr"):
            continue
        body.remove(child)


def _docx_replace(document: Any, find_text: str,
                  replacement: str) -> int:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    count = 0

    def paragraph_replace(paragraph: Any) -> None:
        nonlocal count
        text = paragraph.text
        if find_text not in text:
            return
        count += text.count(find_text)
        paragraph.text = text.replace(find_text, replacement)

    try:
        blocks = list(document.iter_inner_content())
    except AttributeError:  # pragma: no cover
        blocks = list(document.paragraphs) + list(document.tables)
    for block in blocks:
        if isinstance(block, Paragraph):
            paragraph_replace(block)
        elif isinstance(block, Table):
            for row in block.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        paragraph_replace(paragraph)

    # Headers and footers are part of the document the user sees.
    for section in document.sections:
        for container in (section.header, section.footer):
            for paragraph in container.paragraphs:
                paragraph_replace(paragraph)
    return count


def _docx_text(path: str) -> str:
    from docx import Document

    document = Document(path)
    values: List[str] = []
    for paragraph in document.paragraphs:
        values.append(paragraph.text)
    for table in document.tables:
        for row in table.rows:
            values.extend(cell.text for cell in row.cells)
    return "\n".join(values)


def _edit_docx(path: str, action: str, args: Dict[str, Any]
               ) -> Dict[str, Any]:
    try:
        from docx import Document
    except Exception:  # noqa: BLE001
        return _fail(action, "missing_dependency",
                     "Editing DOCX needs the optional 'python-docx' package. "
                     "Install it with: pip install python-docx", path=path)
    content = str(args.get("content") or "")
    find_text = str(args.get("find_text") or "")
    replacement = str(args.get("replacement_text") or "")
    exists = os.path.lexists(path)

    if action == "write" and not exists:
        problem = _edit_check_target(path, action, allow_new=True)
        if problem:
            return problem
        temp = ""
        try:
            document = Document()
            _docx_add_lines(document, content)
            temp = _temp_for(path)
            document.save(temp)
            if os.path.exists(path):
                try:
                    os.remove(temp)
                except OSError:
                    pass
                return _fail(action, "conflict",
                             f"'{path}' appeared before it could be written.",
                             path=path)
            _commit_temp(path, temp)
        except OSError as exc:
            if temp:
                try:
                    os.remove(temp)
                except OSError:
                    pass
            return _fail(action, "failed",
                         f"Could not create '{path}': {exc}.", path=path)
        return _edit_base_result(
            action, path, "docx",
            f"Created '{path}' with {len(content.splitlines())} paragraph(s).",
            paragraphs_written=len(content.splitlines()))

    problem = _edit_check_target(path, action)
    if problem:
        return problem
    old_digest = _file_sha256(path)
    try:
        document = Document(path)
    except Exception as exc:  # noqa: BLE001
        if action == "write":
            # A full-content write replaces every paragraph anyway. This also
            # repairs files created empty by older builds, which are not valid
            # DOCX packages and cannot be opened by python-docx.
            document = Document()
        else:
            return _fail(action, "corrupted",
                         f"The DOCX document could not be opened: "
                         f"{type(exc).__name__}.", path=path)

    replacements = 0
    if action == "write":
        summary = (f"Replace all content of '{os.path.basename(path)}' "
                   f"with {len(content)} characters (formatting of removed "
                   "content is lost)")
    elif action == "append":
        summary = (f"Append {len(content)} characters to "
                   f"'{os.path.basename(path)}'")
    elif action in ("replace_content", "remove_content"):
        if not find_text:
            return _fail(action, "invalid_argument",
                         "find_text is required and cannot be empty.",
                         path=path)
        # Count without mutating a disposable copy.
        replacements = _docx_count(document, find_text)
        if replacements == 0:
            return _fail(action, "not_found",
                         f"'{_preview(find_text)}' was not found in "
                         f"'{path}'; the file was not changed.", path=path)
        verb = "Replace" if action == "replace_content" else "Remove"
        summary = (f"{verb} {replacements} occurrence(s) of "
                   f"'{_preview(find_text)}' in "
                   f"'{os.path.basename(path)}'")
    elif action == "clear_content":
        summary = f"Clear all content of '{os.path.basename(path)}'"
        replacements = 0
    else:  # pragma: no cover
        return _fail(action, "unsupported", f"Unsupported action '{action}'.")

    parameters = {
        "path": os.path.normcase(os.path.abspath(path)),
        "format": "docx",
        "current_sha256": old_digest,
        "content_sha256": _text_sha256(content) if content else "",
        "find_sha256": _text_sha256(find_text) if find_text else "",
        "replacement_sha256": (
            _text_sha256(replacement) if replacement else ""),
        "replacements": replacements,
    }
    blocked = _authorize_content_edit(action, path, parameters, summary)
    if blocked:
        return blocked
    if _file_sha256(path) != old_digest:
        return _edit_conflict_result(action, path)

    # Reload after approval so the edit applies to the reviewed bytes.
    try:
        document = Document(path)
    except Exception as exc:  # noqa: BLE001
        if action == "write":
            document = Document()
        else:
            return _fail(action, "corrupted",
                         f"The DOCX document could not be reopened: "
                         f"{type(exc).__name__}.", path=path)
    temp = ""
    try:
        if action == "write":
            _docx_clear(document)
            _docx_add_lines(document, content)
        elif action == "append":
            _docx_add_lines(document, content)
        elif action == "replace_content":
            _docx_replace(document, find_text, replacement)
        elif action == "remove_content":
            _docx_replace(document, find_text, "")
        elif action == "clear_content":
            _docx_clear(document)
        temp = _temp_for(path)
        document.save(temp)
        # Validate the saved document before replacing the original.
        if action in ("replace_content", "remove_content"):
            remaining = _docx_count(Document(temp), find_text)
            if action == "remove_content" and remaining:
                os.remove(temp)
                return _fail(action, "verify_failed",
                             "Some matches remain after the edit; the "
                             "original file was not changed.", path=path)
        if action in ("write", "append") and content.strip():
            probe = _docx_text(temp)
            first_line = next((line.strip() for line in content.splitlines()
                               if line.strip()), "")
            if first_line and first_line not in probe:
                os.remove(temp)
                return _fail(action, "verify_failed",
                             "The written paragraph was not found after "
                             "saving; the original file was not changed.",
                             path=path)
        _commit_temp(path, temp)
    except PermissionError:
        if temp:
            try:
                os.remove(temp)
            except OSError:
                pass
        return _fail(action, "denied",
                     f"Windows denied permission to write '{path}'.",
                     path=path)
    except Exception as exc:  # noqa: BLE001
        if temp:
            try:
                os.remove(temp)
            except OSError:
                pass
        return _fail(action, "failed",
                     f"Could not edit '{path}': {type(exc).__name__}: {exc}.",
                     path=path)
    return _edit_base_result(
        action, path, "docx",
        f"Updated '{path}'"
        + (f" ({replacements} occurrence(s))." if replacements else "."),
        replacements=replacements, count=replacements,
        warnings=["Edited paragraphs may lose character-level formatting; "
                  "paragraph styles and document structure are preserved."])


def _docx_count(document: Any, find_text: str) -> int:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    count = 0

    def paragraph_count(paragraph: Any) -> None:
        nonlocal count
        count += paragraph.text.count(find_text)

    try:
        blocks = list(document.iter_inner_content())
    except AttributeError:  # pragma: no cover
        blocks = list(document.paragraphs) + list(document.tables)
    for block in blocks:
        if isinstance(block, Paragraph):
            paragraph_count(block)
        elif isinstance(block, Table):
            for row in block.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        paragraph_count(paragraph)
    for section in document.sections:
        for container in (section.header, section.footer):
            for paragraph in container.paragraphs:
                paragraph_count(paragraph)
    return count


# ── XLSX ─────────────────────────────────────────────────────────────────────
def _xlsx_value(text: str) -> Any:
    stripped = text.strip()
    if stripped == "":
        return None
    lowered = stripped.casefold()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        if re.fullmatch(r"-?\d+", stripped):
            return int(stripped)
        if re.fullmatch(r"-?\d+\.\d+", stripped):
            return float(stripped)
    except ValueError:
        pass
    return text


def _xlsx_rows(content: str) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        delimiter = "\t" if "\t" in line else ","
        try:
            cells = next(csv.reader([line], delimiter=delimiter))
        except csv.Error:
            cells = line.split(delimiter)
        rows.append([_xlsx_value(cell) for cell in cells])
    return rows


def _xlsx_cell_matches(value: Any, wanted: str) -> bool:
    return str(value if value is not None else "") == wanted


def _xlsx_apply(workbook: Any, action: str, *, content: str,
                find_text: str, replacement: str, sheet: str,
                cell: str) -> Tuple[int, str]:
    from openpyxl.utils import column_index_from_string

    if sheet:
        matches = [ws for ws in workbook.worksheets
                   if ws.title.casefold() == sheet.casefold()]
        if not matches:
            raise ValueError(
                f"Worksheet '{sheet}' was not found. Available sheets: "
                + ", ".join(ws.title for ws in workbook.worksheets[:20]))
        worksheets = matches
    else:
        worksheets = list(workbook.worksheets)
    replacements = 0
    processed = 0
    message = ""
    if action == "write":
        if not cell:
            raise ValueError(
                "Writing an existing XLSX requires `cell` (for example A1) "
                "so worksheets and existing structure stay intact. Use "
                "append to add rows.")
        worksheet = worksheets[0]
        address = str(cell).upper()
        column_index_from_string(re.sub(r"\d", "", address))
        worksheet[address] = _xlsx_value(content)
        replacements = 1
        message = f"Set {worksheet.title}!{address}."
    elif action == "append":
        worksheet = worksheets[0]
        rows = _xlsx_rows(content)
        if not rows:
            raise ValueError("content must contain at least one row.")
        # openpyxl reports max_row == 1 for a brand-new empty worksheet.
        # Starting at 2 would leave a blank first row in a newly created file.
        empty = (worksheet.max_row == 1 and worksheet.max_column == 1
                 and worksheet["A1"].value is None)
        start = 1 if empty else worksheet.max_row + 1
        for row_index, row in enumerate(rows, start=start):
            for column, value in enumerate(row, start=1):
                worksheet.cell(row=row_index, column=column, value=value)
        replacements = len(rows)
        message = f"Appended {len(rows)} row(s) to '{worksheet.title}'."
    elif action in ("replace_content", "remove_content"):
        if not find_text:
            raise ValueError("find_text is required and cannot be empty.")
        target = str(cell).upper() if cell else ""
        for worksheet in worksheets:
            for row in worksheet.iter_rows():
                for item in row:
                    processed += 1
                    if processed > MAX_EDIT_CELLS:
                        raise ValueError(
                            "The workbook is too large to edit safely in one "
                            "operation; convert the sheet to CSV first.")
                    if target and item.coordinate != target:
                        continue
                    if not _xlsx_cell_matches(item.value, find_text):
                        continue
                    item.value = (None if action == "remove_content"
                                  else _xlsx_value(replacement))
                    replacements += 1
        message = (f"Updated {replacements} cell(s) across "
                   f"{len(worksheets)} worksheet(s).")
    elif action == "clear_content":
        for worksheet in worksheets:
            for row in worksheet.iter_rows():
                for item in row:
                    processed += 1
                    if processed > MAX_EDIT_CELLS:
                        raise ValueError(
                            "The workbook is too large to clear safely in one "
                            "operation; convert the sheet to CSV first.")
                    if item.value is not None:
                        item.value = None
                        replacements += 1
        message = f"Cleared {replacements} cell(s) while keeping the workbook."
    return replacements, message


def _edit_xlsx(path: str, action: str, args: Dict[str, Any]
               ) -> Dict[str, Any]:
    try:
        import openpyxl
        from openpyxl.utils import column_index_from_string
    except Exception:  # noqa: BLE001
        return _fail(action, "missing_dependency",
                     "Editing XLSX needs the optional 'openpyxl' package. "
                     "Install it with: pip install openpyxl", path=path)
    content = str(args.get("content") or "")
    find_text = str(args.get("find_text") or "")
    replacement = str(args.get("replacement_text") or "")
    sheet = _clean(args.get("sheet"))
    cell = _clean(args.get("cell"))
    exists = os.path.lexists(path)

    if action == "write" and not exists:
        problem = _edit_check_target(path, action, allow_new=True)
        if problem:
            return problem
        temp = ""
        try:
            workbook = openpyxl.Workbook()
            worksheet = workbook.active
            if sheet:
                worksheet.title = sheet[:31]
            if cell:
                address = str(cell).upper()
                column_index_from_string(re.sub(r"\d", "", address))
                worksheet[address] = _xlsx_value(content)
            elif content:
                worksheet["A1"] = content
            temp = _temp_for(path)
            workbook.save(temp)
            if os.path.exists(path):
                try:
                    os.remove(temp)
                except OSError:
                    pass
                return _fail(action, "conflict",
                             f"'{path}' appeared before it could be written.",
                             path=path)
            _commit_temp(path, temp)
        except Exception as exc:  # noqa: BLE001
            if temp:
                try:
                    os.remove(temp)
                except OSError:
                    pass
            return _fail(action, "failed",
                         f"Could not create '{path}': "
                         f"{type(exc).__name__}: {exc}.", path=path)
        return _edit_base_result(
            action, path, "xlsx", f"Created workbook '{path}'.",
            sheet_names=[sheet or "Sheet"])

    problem = _edit_check_target(path, action)
    if problem:
        return problem
    old_digest = _file_sha256(path)
    try:
        workbook = openpyxl.load_workbook(path, data_only=False)
    except Exception as exc:  # noqa: BLE001
        if action == "write":
            # Replacing content does not need the old workbook. Recreate a
            # valid file (also repairs empty placeholders from older builds).
            workbook = openpyxl.Workbook()
            if sheet:
                workbook.active.title = str(sheet)[:31]
        else:
            return _fail(action, "corrupted",
                         f"The XLSX workbook could not be opened: "
                         f"{type(exc).__name__}.", path=path)
    try:
        # Dry run on a copy-like inspection: compute the summary and count
        # before asking for approval.
        temp = ""
        if action in ("replace_content", "remove_content"):
            if not find_text:
                return _fail(action, "invalid_argument",
                             "find_text is required and cannot be empty.",
                             path=path)
            matches = 0
            for worksheet in workbook.worksheets:
                if sheet and worksheet.title.casefold() != sheet.casefold():
                    continue
                for row in worksheet.iter_rows():
                    for item in row:
                        if cell and item.coordinate != cell.upper():
                            continue
                        if _xlsx_cell_matches(item.value, find_text):
                            matches += 1
            if matches == 0:
                return _fail(action, "not_found",
                             f"'{_preview(find_text)}' was not found in "
                             f"'{path}'; the file was not changed.", path=path)
            replacements = matches
        elif action == "write":
            if not cell:
                return _fail(
                    action, "unsupported",
                    "Writing an existing XLSX requires `cell` (for example "
                    "A1). Use append to add rows, or replace_content to "
                    "change matching cells.", path=path)
            replacements = 1
        elif action == "append":
            if not content.strip():
                return _fail(action, "invalid_argument",
                             "content is required for append.", path=path)
            replacements = len(_xlsx_rows(content))
        else:
            replacements = 0
        if action == "write":
            summary = (f"Set cell {cell.upper()} in "
                       f"'{os.path.basename(path)}' to "
                       f"'{_preview(content)}'")
        elif action == "append":
            summary = (f"Append {replacements} row(s) to "
                       f"'{os.path.basename(path)}'")
        elif action == "replace_content":
            summary = (f"Replace {replacements} cell(s) matching "
                       f"'{_preview(find_text)}' in "
                       f"'{os.path.basename(path)}'")
        elif action == "remove_content":
            summary = (f"Clear {replacements} cell(s) matching "
                       f"'{_preview(find_text)}' in "
                       f"'{os.path.basename(path)}'")
        else:
            summary = f"Clear every cell value in '{os.path.basename(path)}'"
        parameters = {
            "path": os.path.normcase(os.path.abspath(path)),
            "format": "xlsx",
            "sheet": sheet,
            "cell": cell.upper(),
            "current_sha256": old_digest,
            "content_sha256": _text_sha256(content) if content else "",
            "find_sha256": _text_sha256(find_text) if find_text else "",
            "replacement_sha256": (
                _text_sha256(replacement) if replacement else ""),
            "replacements": replacements,
        }
        blocked = _authorize_content_edit(action, path, parameters, summary)
        if blocked:
            return blocked
        if _file_sha256(path) != old_digest:
            return _edit_conflict_result(action, path)

        try:
            workbook = openpyxl.load_workbook(path, data_only=False)
        except Exception:
            if action != "write":
                raise
            workbook = openpyxl.Workbook()
            if sheet:
                workbook.active.title = str(sheet)[:31]
        replacements, message = _xlsx_apply(
            workbook, action, content=content, find_text=find_text,
            replacement=replacement, sheet=sheet, cell=cell)
        temp = _temp_for(path)
        workbook.save(temp)
        # Validate the saved workbook before replacing the original.
        check = openpyxl.load_workbook(temp, data_only=False)
        if action == "write":
            worksheet = next(
                (ws for ws in check.worksheets
                 if sheet and ws.title.casefold() == sheet.casefold()),
                check.worksheets[0])
            expected = _xlsx_value(content)
            expected_text = "" if expected is None else str(expected)
            if not _xlsx_cell_matches(worksheet[cell.upper()].value,
                                      expected_text):
                check.close()
                os.remove(temp)
                return _fail(action, "verify_failed",
                             "The target cell did not contain the new value "
                             "after saving; the original file was not changed.",
                             path=path)
        elif action in ("replace_content", "remove_content"):
            remaining = 0
            for worksheet in check.worksheets:
                if sheet and worksheet.title.casefold() != sheet.casefold():
                    continue
                for row in worksheet.iter_rows():
                    for item in row:
                        if cell and item.coordinate != cell.upper():
                            continue
                        if _xlsx_cell_matches(item.value, find_text):
                            remaining += 1
            if remaining and not (action == "replace_content"
                                  and replacement == find_text):
                check.close()
                os.remove(temp)
                return _fail(action, "verify_failed",
                             "The saved workbook still contains the requested "
                             "match; the original file was not changed.",
                             path=path)
        elif action == "clear_content":
            leftover = any(
                item.value is not None
                for worksheet in check.worksheets
                if not sheet or worksheet.title.casefold() == sheet.casefold()
                for row in worksheet.iter_rows() for item in row)
            if leftover:
                check.close()
                os.remove(temp)
                return _fail(action, "verify_failed",
                             "Cell values remained after clearing; the "
                             "original file was not changed.", path=path)
        check.close()
        _commit_temp(path, temp)
    except PermissionError:
        if temp:
            try:
                os.remove(temp)
            except OSError:
                pass
        return _fail(action, "denied",
                     f"Windows denied permission to write '{path}'.",
                     path=path)
    except ValueError as exc:
        if temp:
            try:
                os.remove(temp)
            except OSError:
                pass
        return _fail(action, "unsupported", str(exc), path=path)
    except Exception as exc:  # noqa: BLE001
        if temp:
            try:
                os.remove(temp)
            except OSError:
                pass
        return _fail(action, "failed",
                     f"Could not edit '{path}': {type(exc).__name__}: {exc}.",
                     path=path)
    finally:
        try:
            workbook.close()
        except Exception:  # noqa: BLE001
            pass
    return _edit_base_result(
        action, path, "xlsx", f"Updated '{path}'. {message}",
        replacements=replacements, count=replacements,
        warnings=["openpyxl rewrites the workbook; charts, images and VBA "
                  "that openpyxl does not model may not be preserved."])


# ── PPTX ─────────────────────────────────────────────────────────────────────
def _pptx_text_paragraphs(presentation: Any) -> Iterable[Any]:
    for slide in presentation.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    yield paragraph
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        for paragraph in cell.text_frame.paragraphs:
                            yield paragraph


def _pptx_replace(presentation: Any, find_text: str,
                  replacement: str) -> int:
    count = 0
    for paragraph in _pptx_text_paragraphs(presentation):
        text = paragraph.text
        if find_text not in text:
            continue
        count += text.count(find_text)
        paragraph.text = text.replace(find_text, replacement)
    return count


def _pptx_add_slide(presentation: Any, content: str) -> Any:
    from pptx.util import Inches

    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = slide.shapes.add_textbox(
        Inches(1), Inches(1), Inches(8), Inches(5))
    box.text_frame.word_wrap = True
    box.text_frame.text = content
    return slide


def _pptx_clear_slides(presentation: Any) -> None:
    from pptx.oxml.ns import qn

    slides = presentation.slides._sldIdLst  # noqa: SLF001 - python-pptx API
    for slide_id in list(slides):
        relationship_id = slide_id.get(qn("r:id"))
        if relationship_id:
            try:
                presentation.part.drop_rel(relationship_id)
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
        slides.remove(slide_id)


def _edit_pptx(path: str, action: str, args: Dict[str, Any]
               ) -> Dict[str, Any]:
    try:
        from pptx import Presentation
    except Exception:  # noqa: BLE001
        return _fail(action, "missing_dependency",
                     "Editing PPTX needs the optional 'python-pptx' package. "
                     "Install it with: pip install python-pptx", path=path)
    content = str(args.get("content") or "")
    find_text = str(args.get("find_text") or "")
    replacement = str(args.get("replacement_text") or "")
    exists = os.path.lexists(path)

    if action == "write" and not exists:
        problem = _edit_check_target(path, action, allow_new=True)
        if problem:
            return problem
        temp = ""
        try:
            presentation = Presentation()
            _pptx_add_slide(presentation, content)
            temp = _temp_for(path)
            presentation.save(temp)
            if os.path.exists(path):
                try:
                    os.remove(temp)
                except OSError:
                    pass
                return _fail(action, "conflict",
                             f"'{path}' appeared before it could be written.",
                             path=path)
            _commit_temp(path, temp)
        except Exception as exc:  # noqa: BLE001
            if temp:
                try:
                    os.remove(temp)
                except OSError:
                    pass
            return _fail(action, "failed",
                         f"Could not create '{path}': "
                         f"{type(exc).__name__}: {exc}.", path=path)
        return _edit_base_result(
            action, path, "pptx", f"Created presentation '{path}'.",
            slide_count=1)

    problem = _edit_check_target(path, action)
    if problem:
        return problem
    old_digest = _file_sha256(path)
    try:
        presentation = Presentation(path)
    except Exception as exc:  # noqa: BLE001
        if action == "write":
            # A full-content write rebuilds every slide, so a corrupt/empty
            # legacy placeholder can be repaired instead of blocking the user.
            presentation = Presentation()
        else:
            return _fail(action, "corrupted",
                         f"The PPTX presentation could not be opened: "
                         f"{type(exc).__name__}.", path=path)
    replacements = 0
    if action == "write":
        summary = (f"Replace every slide in '{os.path.basename(path)}' with "
                   f"the new content (slide formatting is lost)")
    elif action == "append":
        summary = (f"Append a slide with {len(content)} characters to "
                   f"'{os.path.basename(path)}'")
    elif action in ("replace_content", "remove_content"):
        if not find_text:
            return _fail(action, "invalid_argument",
                         "find_text is required and cannot be empty.",
                         path=path)
        for paragraph in _pptx_text_paragraphs(presentation):
            replacements += paragraph.text.count(find_text)
        if replacements == 0:
            return _fail(action, "not_found",
                         f"'{_preview(find_text)}' was not found in "
                         f"'{path}'; the file was not changed.", path=path)
        verb = "Replace" if action == "replace_content" else "Remove"
        summary = (f"{verb} {replacements} occurrence(s) of "
                   f"'{_preview(find_text)}' in "
                   f"'{os.path.basename(path)}'")
    elif action == "clear_content":
        summary = f"Clear all slide text in '{os.path.basename(path)}'"
    else:  # pragma: no cover
        return _fail(action, "unsupported", f"Unsupported action '{action}'.")

    parameters = {
        "path": os.path.normcase(os.path.abspath(path)),
        "format": "pptx",
        "current_sha256": old_digest,
        "content_sha256": _text_sha256(content) if content else "",
        "find_sha256": _text_sha256(find_text) if find_text else "",
        "replacement_sha256": (
            _text_sha256(replacement) if replacement else ""),
        "replacements": replacements,
    }
    blocked = _authorize_content_edit(action, path, parameters, summary)
    if blocked:
        return blocked
    if _file_sha256(path) != old_digest:
        return _edit_conflict_result(action, path)

    temp = ""
    try:
        try:
            presentation = Presentation(path)
        except Exception:
            if action != "write":
                raise
            presentation = Presentation()
        if action == "write":
            _pptx_clear_slides(presentation)
            _pptx_add_slide(presentation, content)
        elif action == "append":
            _pptx_add_slide(presentation, content)
        elif action == "replace_content":
            _pptx_replace(presentation, find_text, replacement)
        elif action == "remove_content":
            _pptx_replace(presentation, find_text, "")
        elif action == "clear_content":
            for paragraph in _pptx_text_paragraphs(presentation):
                paragraph.text = ""
        temp = _temp_for(path)
        presentation.save(temp)
        check = Presentation(temp)
        if action in ("write", "append"):
            probe = "\n".join(p.text for p in _pptx_text_paragraphs(check))
            first_line = next((line.strip() for line in content.splitlines()
                               if line.strip()), "")
            if first_line and first_line not in probe:
                os.remove(temp)
                return _fail(action, "verify_failed",
                             "The new slide text was not found after saving; "
                             "the original file was not changed.", path=path)
        elif action in ("replace_content", "remove_content"):
            remaining = sum(
                paragraph.text.count(find_text)
                for paragraph in _pptx_text_paragraphs(check))
            if remaining and not (action == "replace_content"
                                  and replacement == find_text):
                os.remove(temp)
                return _fail(action, "verify_failed",
                             "The saved presentation still contains the "
                             "requested text; the original file was not "
                             "changed.", path=path)
        elif action == "clear_content":
            if any(paragraph.text.strip()
                   for paragraph in _pptx_text_paragraphs(check)):
                os.remove(temp)
                return _fail(action, "verify_failed",
                             "Slide text remained after clearing; the "
                             "original file was not changed.", path=path)
        _commit_temp(path, temp)
    except PermissionError:
        if temp:
            try:
                os.remove(temp)
            except OSError:
                pass
        return _fail(action, "denied",
                     f"Windows denied permission to write '{path}'.",
                     path=path)
    except Exception as exc:  # noqa: BLE001
        if temp:
            try:
                os.remove(temp)
            except OSError:
                pass
        return _fail(action, "failed",
                     f"Could not edit '{path}': {type(exc).__name__}: {exc}.",
                     path=path)
    return _edit_base_result(
        action, path, "pptx",
        f"Updated '{path}'"
        + (f" ({replacements} occurrence(s))." if replacements else "."),
        replacements=replacements, count=replacements,
        warnings=["python-pptx may not preserve every custom theme, animation "
                  "or embedded object when the presentation is rewritten."])


# ── CSV / TSV ────────────────────────────────────────────────────────────────
def _csv_parse(text: str, fmt: str) -> List[List[str]]:
    delimiter = "\t" if fmt == "tsv" else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [list(row) for row in reader]


def _csv_serialize(rows: List[List[str]], fmt: str, newline: str) -> str:
    delimiter = "\t" if fmt == "tsv" else ","
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator=newline)
    writer.writerows(rows)
    return buffer.getvalue()


def _csv_column_index(rows: List[List[str]], column: Any) -> Optional[int]:
    text = _clean(column)
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        index = int(text) - 1
        if index < 0:
            raise ValueError("column must be 1 or greater.")
        return index
    if rows:
        for index, header in enumerate(rows[0]):
            if header.casefold() == text.casefold():
                return index
    raise ValueError(
        f"Column '{text}' was not found in the header row.")


def _csv_row_index(row: Any) -> int:
    text = _clean(row)
    if text.casefold() in ("", "0", "none", "null"):
        return 0
    if not re.fullmatch(r"\d+", text):
        raise ValueError("row must be a 1-based number.")
    value = int(text)
    if value < 1:
        raise ValueError("row must be 1 or greater.")
    return value


def _edit_csv(path: str, action: str, args: Dict[str, Any], fmt: str
              ) -> Dict[str, Any]:
    content = str(args.get("content") or "")
    find_text = str(args.get("find_text") or "")
    replacement = str(args.get("replacement_text") or "")
    requested_encoding = _clean(args.get("encoding"))
    exists = os.path.lexists(path)

    if action == "write" and not exists:
        problem = _edit_check_target(path, action, allow_new=True)
        if problem:
            return problem
        encoding = requested_encoding or "utf-8"
        try:
            rows = _csv_parse(content, fmt) if content else []
            data = _csv_serialize(rows, fmt, "\n").encode(encoding)
            _create_bytes(path, data)
        except FileExistsError:
            return _fail(action, "conflict",
                         f"'{path}' already exists; nothing was overwritten.",
                         path=path)
        except Exception as exc:  # noqa: BLE001
            return _fail(action, "failed",
                         f"Could not create '{path}': {exc}.", path=path)
        try:
            verify_rows = _csv_parse(_read_text_for_edit(path, encoding)[0], fmt)
        except Exception:  # noqa: BLE001
            verify_rows = None
        if verify_rows != rows:
            try:
                os.unlink(path)
            except OSError:
                pass
            return _fail(action, "verify_failed",
                         f"'{path}' was created but its rows did not read back "
                         "correctly, so it was removed.", path=path)
        return _edit_base_result(
            action, path, fmt, f"Created '{path}' with {len(rows)} row(s).",
            row_count=len(rows))

    problem = _edit_check_target(path, action)
    if problem:
        return problem
    text, used_encoding, warnings = _read_text_for_edit(
        path, requested_encoding)
    encoding = requested_encoding or used_encoding or "utf-8"
    newline = _detect_newline(text)
    old_digest = _file_sha256(path)
    try:
        rows = _csv_parse(text, fmt)
    except csv.Error as exc:
        return _fail(action, "corrupted",
                     f"The {fmt.upper()} file is not valid: {exc}.", path=path)
    replacements = 0
    row_number = 0
    column_index: Optional[int] = None
    try:
        row_number = _csv_row_index(args.get("row"))
        column_index = _csv_column_index(rows, args.get("column"))
    except ValueError as exc:
        return _fail(action, "invalid_argument", str(exc), path=path)

    if action == "write":
        if row_number:
            if column_index is None:
                return _fail(action, "invalid_argument",
                             "Writing one cell needs `column` as well as "
                             "`row`.", path=path)
            while len(rows) < row_number:
                rows.append([])
            row = rows[row_number - 1]
            while len(row) <= column_index:
                row.append("")
            row[column_index] = content
            replacements = 1
            summary = (f"Set row {row_number}, column "
                       f"{column_index + 1} of '{os.path.basename(path)}'")
        else:
            rows = _csv_parse(content, fmt) if content else []
            replacements = len(rows)
            summary = (f"Replace all rows of '{os.path.basename(path)}' with "
                       f"{len(rows)} row(s)")
    elif action == "append":
        additions = _csv_parse(content, fmt) if content else []
        if not additions:
            return _fail(action, "invalid_argument",
                         "content is required for append.", path=path)
        rows.extend(additions)
        replacements = len(additions)
        summary = (f"Append {len(additions)} row(s) to "
                   f"'{os.path.basename(path)}'")
    elif action in ("replace_content", "remove_content"):
        if not find_text:
            return _fail(action, "invalid_argument",
                         "find_text is required and cannot be empty.",
                         path=path)
        for row in rows:
            indexes = ([column_index] if column_index is not None
                       else range(len(row)))
            for index in indexes:
                if index < len(row) and row[index] == find_text:
                    row[index] = ("" if action == "remove_content"
                                  else replacement)
                    replacements += 1
        if replacements == 0:
            return _fail(action, "not_found",
                         f"'{_preview(find_text)}' was not found in "
                         f"'{path}'; the file was not changed.", path=path)
        verb = "Replace" if action == "replace_content" else "Remove"
        summary = (f"{verb} {replacements} cell(s) matching "
                   f"'{_preview(find_text)}' in "
                   f"'{os.path.basename(path)}'")
    elif action == "clear_content":
        rows = []
        summary = f"Clear all rows in '{os.path.basename(path)}'"
    else:  # pragma: no cover
        return _fail(action, "unsupported", f"Unsupported action '{action}'.")

    new_text = _csv_serialize(rows, fmt, newline)
    if new_text == text:
        return _edit_base_result(
            action, path, fmt, f"'{path}' already has that content; nothing "
            "was changed.", row_count=len(rows))
    parameters = {
        "path": os.path.normcase(os.path.abspath(path)),
        "format": fmt,
        "encoding": encoding,
        "current_sha256": old_digest,
        "new_sha256": _text_sha256(new_text, encoding),
        "content_sha256": _text_sha256(content) if content else "",
        "find_sha256": _text_sha256(find_text) if find_text else "",
        "replacement_sha256": (
            _text_sha256(replacement) if replacement else ""),
        "row": row_number,
        "column": column_index if column_index is not None else "",
        "replacements": replacements,
    }
    blocked = _authorize_content_edit(action, path, parameters, summary)
    if blocked:
        return blocked
    if _file_sha256(path) != old_digest:
        return _edit_conflict_result(action, path)
    try:
        data = new_text.encode(encoding)
        _atomic_write_bytes(path, data)
    except Exception as exc:  # noqa: BLE001
        return _fail(action, "failed",
                     f"Could not write '{path}': {exc}.", path=path)
    try:
        verify_rows = _csv_parse(_read_text_for_edit(path, encoding)[0], fmt)
    except Exception:  # noqa: BLE001
        verify_rows = None
    if verify_rows != rows:
        return _fail(action, "verify_failed",
                     "The saved rows did not match the requested change.",
                     path=path)
    fields: Dict[str, Any] = {
        "encoding": encoding, "row_count": len(rows),
        "replacements": replacements, "count": replacements,
    }
    if warnings:
        fields["warnings"] = warnings
    return _edit_base_result(
        action, path, fmt,
        f"Updated '{path}'"
        + (f" ({replacements} row/cell change(s))." if replacements else "."),
        **fields)


# ── JSON / YAML ──────────────────────────────────────────────────────────────
def _json_replace(value: Any, find_text: str,
                  replacement: str) -> Tuple[Any, int]:
    if isinstance(value, str):
        count = value.count(find_text)
        return (value.replace(find_text, replacement), count) if count else (
            value, 0)
    if isinstance(value, list):
        total = 0
        items = []
        for item in value:
            changed, count = _json_replace(item, find_text, replacement)
            items.append(changed)
            total += count
        return items, total
    if isinstance(value, dict):
        total = 0
        result: Dict[Any, Any] = {}
        for key, item in value.items():
            new_key, key_count = _json_replace(key, find_text, replacement) \
                if isinstance(key, str) else (key, 0)
            if new_key in result:
                raise ValueError(
                    f"Replacing key {key!r} would merge it with the existing "
                    f"key {new_key!r}; nothing was changed.")
            changed, value_count = _json_replace(
                item, find_text, replacement)
            result[new_key] = changed
            total += key_count + value_count
        return result, total
    return value, 0


def _json_remove(value: Any, find_text: str) -> Tuple[Any, int]:
    if isinstance(value, list):
        total = 0
        items = []
        for item in value:
            if isinstance(item, str) and item == find_text:
                total += 1
                continue
            changed, count = _json_remove(item, find_text)
            items.append(changed)
            total += count
        return items, total
    if isinstance(value, dict):
        total = 0
        result: Dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key == find_text:
                total += 1
                continue
            if isinstance(item, str) and item == find_text:
                total += 1
                continue
            changed, count = _json_remove(item, find_text)
            result[key] = changed
            total += count
        return result, total
    return value, 0


def _edit_json_yaml(path: str, action: str, args: Dict[str, Any], fmt: str
                    ) -> Dict[str, Any]:
    if fmt == "yaml":
        try:
            import yaml
        except Exception:  # noqa: BLE001
            return _fail(action, "missing_dependency",
                         "Editing YAML needs the optional 'PyYAML' package. "
                         "Install it with: pip install pyyaml", path=path)
    content = str(args.get("content") or "")
    find_text = str(args.get("find_text") or "")
    replacement = str(args.get("replacement_text") or "")
    requested_encoding = _clean(args.get("encoding"))
    exists = os.path.lexists(path)

    def load_text(value: str) -> Any:
        if fmt == "json":
            return json.loads(value)
        return yaml.safe_load(value)

    def dump_value(value: Any) -> str:
        if fmt == "json":
            return json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)

    if action == "write" and not exists:
        problem = _edit_check_target(path, action, allow_new=True)
        if problem:
            return problem
        try:
            value = load_text(content)
        except Exception as exc:  # noqa: BLE001
            return _fail(action, "invalid_argument",
                         f"content is not valid {fmt.upper()}: {exc}.",
                         path=path)
        encoding = requested_encoding or "utf-8"
        try:
            _create_bytes(path, dump_value(value).encode(encoding))
        except FileExistsError:
            return _fail(action, "conflict",
                         f"'{path}' already exists; nothing was overwritten.",
                         path=path)
        except Exception as exc:  # noqa: BLE001
            return _fail(action, "failed",
                         f"Could not create '{path}': {exc}.", path=path)
        try:
            verify_value = load_text(_read_text_for_edit(path, encoding)[0])
        except Exception:  # noqa: BLE001
            verify_value = object()
        if verify_value != value:
            try:
                os.unlink(path)
            except OSError:
                pass
            return _fail(action, "verify_failed",
                         f"'{path}' was created but did not read back "
                         "correctly, so it was removed.", path=path)
        return _edit_base_result(
            action, path, fmt, f"Created '{path}'.")

    problem = _edit_check_target(path, action)
    if problem:
        return problem
    text, used_encoding, warnings = _read_text_for_edit(
        path, requested_encoding)
    encoding = requested_encoding or used_encoding or "utf-8"
    newline = _detect_newline(text)
    old_digest = _file_sha256(path)
    try:
        current = load_text(text)
    except Exception as exc:  # noqa: BLE001
        return _fail(action, "corrupted",
                     f"The existing {fmt.upper()} is not valid: {exc}.",
                     path=path)
    replacements = 0
    if action == "write":
        try:
            value = load_text(content)
        except Exception as exc:  # noqa: BLE001
            return _fail(action, "invalid_argument",
                         f"content is not valid {fmt.upper()}: {exc}.",
                         path=path)
        summary = (f"Replace all content of '{os.path.basename(path)}'")
    elif action == "append":
        try:
            addition = load_text(content)
        except Exception as exc:  # noqa: BLE001
            return _fail(action, "invalid_argument",
                         f"content is not valid {fmt.upper()}: {exc}.",
                         path=path)
        if isinstance(current, list):
            if isinstance(addition, list):
                value = current + addition
                replacements = len(addition)
            else:
                value = current + [addition]
                replacements = 1
        elif isinstance(current, dict) and isinstance(addition, dict):
            value = dict(current)
            value.update(addition)
            replacements = len(addition)
        else:
            return _fail(action, "unsupported",
                         f"append supports list-to-list or object-to-object "
                         f"{fmt.upper()} values.", path=path)
        summary = (f"Append to '{os.path.basename(path)}'")
    elif action in ("replace_content", "remove_content"):
        if not find_text:
            return _fail(action, "invalid_argument",
                         "find_text is required and cannot be empty.",
                         path=path)
        if action == "replace_content":
            try:
                value, replacements = _json_replace(
                    current, find_text, replacement)
            except ValueError as exc:
                return _fail(action, "conflict", str(exc), path=path)
        else:
            value, replacements = _json_remove(current, find_text)
        if replacements == 0:
            return _fail(action, "not_found",
                         f"'{_preview(find_text)}' was not found in "
                         f"'{path}'; the file was not changed.", path=path)
        verb = "Replace" if action == "replace_content" else "Remove"
        summary = (f"{verb} {replacements} value(s) containing "
                   f"'{_preview(find_text)}' in "
                   f"'{os.path.basename(path)}'")
    elif action == "clear_content":
        value = [] if isinstance(current, list) else {}
        summary = f"Clear all content of '{os.path.basename(path)}'"
    else:  # pragma: no cover
        return _fail(action, "unsupported", f"Unsupported action '{action}'.")

    new_text = dump_value(value)
    new_text = _normalize_newlines(new_text, newline)
    try:
        canonical_before = json.dumps(current, sort_keys=True, default=str)
        canonical_after = json.dumps(value, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        canonical_before = canonical_after = ""
    if canonical_before == canonical_after and action != "write":
        return _edit_base_result(
            action, path, fmt, f"'{path}' already has that content; nothing "
            "was changed.")
    parameters = {
        "path": os.path.normcase(os.path.abspath(path)),
        "format": fmt,
        "encoding": encoding,
        "current_sha256": old_digest,
        "new_sha256": _text_sha256(new_text, encoding),
        "content_sha256": _text_sha256(content) if content else "",
        "find_sha256": _text_sha256(find_text) if find_text else "",
        "replacement_sha256": (
            _text_sha256(replacement) if replacement else ""),
        "replacements": replacements,
    }
    blocked = _authorize_content_edit(action, path, parameters, summary)
    if blocked:
        return blocked
    if _file_sha256(path) != old_digest:
        return _edit_conflict_result(action, path)
    try:
        _atomic_write_bytes(path, new_text.encode(encoding))
        verify_value = load_text(_read_text_for_edit(path, encoding)[0])
        if json.dumps(verify_value, sort_keys=True, default=str) != \
                json.dumps(value, sort_keys=True, default=str):
            return _fail(action, "verify_failed",
                         "The saved document did not match the requested "
                         "change.", path=path)
    except Exception as exc:  # noqa: BLE001
        return _fail(action, "failed",
                     f"Could not write '{path}': {exc}.", path=path)
    fields: Dict[str, Any] = {
        "encoding": encoding, "replacements": replacements,
        "count": replacements,
    }
    if warnings:
        fields["warnings"] = warnings
    if fmt == "yaml":
        fields.setdefault("warnings", []).append(
            "PyYAML rewrites the file; comments and original formatting may "
            "not be preserved.")
    return _edit_base_result(
        action, path, fmt,
        f"Updated '{path}'"
        + (f" ({replacements} change(s))." if replacements else "."),
        **fields)


# ── content-edit dispatch ────────────────────────────────────────────────────
def _op_edit_content(args: Dict[str, Any], action: str) -> Dict[str, Any]:
    path, error = _resolve_path(args.get("source_path"))
    if error:
        return _resolution_failure(action, error)
    fmt = _edit_format(path)
    if not fmt:
        return _fail(
            action, "unsupported",
            "That file format is not supported for editing. Supported text "
            "formats: TXT, MD, LOG, HTML, XML; structured formats: DOCX, "
            "XLSX, PPTX, CSV, TSV, JSON, YAML. Use read_document to inspect "
            "PDF, EPUB, RTF, legacy Office and OpenDocument files.",
            path=path)
    content_value = args.get("content")
    content_text = "" if content_value is None else str(content_value)
    if action in ("write", "append") and content_text == "":
        print(f"[FileControl] {action} REJECTED path={path} format={fmt} "
              "content_supplied=False content_length=0")
        return _fail(
            action, "empty_content",
            "No content was supplied. Use 'create' to make an intentionally "
            "empty file, or supply the full text in `content`.", path=path,
            content_supplied=False, content_length=0)
    if action in ("write", "append"):
        print(f"[FileControl] {action} path={path} format={fmt} "
              f"content_supplied=True content_length={len(content_text)}")
    if fmt in _PLAIN_EDIT_FORMATS:
        return _edit_plain_text(path, action, args, fmt)
    if fmt == "docx":
        return _edit_docx(path, action, args)
    if fmt == "xlsx":
        return _edit_xlsx(path, action, args)
    if fmt == "pptx":
        return _edit_pptx(path, action, args)
    if fmt in ("csv", "tsv"):
        return _edit_csv(path, action, args, fmt)
    if fmt in ("json", "yaml"):
        return _edit_json_yaml(path, action, args, fmt)
    return _fail(
        action, "unsupported",
        f"'{fmt}' is read-only in this build. Use read_document for reading; "
        "editing is supported for TXT, MD, LOG, HTML, XML, DOCX, XLSX, PPTX, "
        "CSV, TSV, JSON and YAML.", path=path, format=fmt)


# ── dispatch ─────────────────────────────────────────────────────────────────
def file_control(
    action: str,
    source_path: str = "",
    destination_path: str = "",
    source_paths: Optional[List[str]] = None,
    new_name: str = "",
    item_type: str = "",
    extension: str = "",
    query: str = "",
    recursive: Any = None,
    permanent: Any = False,
    limit: Any = 0,
    min_size_mb: Any = 0,
    content: str = "",
    find_text: str = "",
    replacement_text: str = "",
    encoding: str = "",
    sheet: str = "",
    cell: str = "",
    row: Any = 0,
    column: str = "",
    confirm_token: str = "",
) -> Dict[str, Any]:
    """Manage files and folders.  ``action`` selects the operation.

    Filesystem:
    * ``create`` — ``source_path`` (``item_type`` folder/file, ``recursive``
      creates missing parent folders)
    * ``rename`` — ``source_path`` and ``new_name`` (same folder)
    * ``move`` / ``copy`` — ``source_path`` (or ``source_paths``) and
      ``destination_path``; a folder destination keeps the item's name
    * ``delete`` — ``source_path`` (or ``source_paths``); ``recursive`` for a
      folder with contents; Recycle Bin unless ``permanent``; every delete
      requires a UI approval and returns ``approval_required`` first
    * ``find`` — ``source_path`` folder, optional ``query``, ``extension``,
      ``item_type``, ``min_size_mb``, ``recursive``, ``limit``
    * ``list`` — ``source_path`` folder (defaults to the user profile)

    Content editing (existing files require a single-use UI approval):
    * ``write`` — ``source_path`` and ``content``; a new file follows the
      creation policy, an existing file needs approval
    * ``append`` — add ``content`` to an existing file
    * ``replace_content`` — replace exact ``find_text`` with
      ``replacement_text``
    * ``remove_content`` — remove exact ``find_text``
    * ``clear_content`` — empty the file/structured container, keeping the
      file itself

    ``sheet``/``cell`` address XLSX cells; ``row``/``column`` address CSV/TSV
    cells.  ``encoding`` optionally overrides text encoding.  Plain text,
    DOCX, XLSX, PPTX, CSV/TSV, JSON and YAML are edited format-aware; binary
    Office files are never written as plain text.

    ``confirm_token`` is accepted only for old callers and is ignored:
    authorization comes exclusively from the server-side approval queue.
    Filtered batches: give a folder as ``source_path`` plus ``extension`` or
    ``query`` (and ``recursive`` when wanted) to move/copy/delete the matches.
    """
    args: Dict[str, Any] = {
        "source_path": source_path,
        "destination_path": destination_path,
        "source_paths": source_paths,
        "new_name": new_name,
        "item_type": item_type,
        "extension": extension,
        "query": query,
        "recursive": recursive,
        "permanent": permanent,
        "limit": limit,
        "min_size_mb": min_size_mb,
        "content": content,
        "find_text": find_text,
        "replacement_text": replacement_text,
        "encoding": encoding,
        "sheet": sheet,
        "cell": cell,
        "row": row,
        "column": column,
    }
    operation, error = _require_action(action)
    if error:
        return error
    handlers = {
        "create": _op_create,
        "rename": _op_rename,
        "move": lambda data: _transfer(data, "move"),
        "copy": lambda data: _transfer(data, "copy"),
        "delete": _op_delete,
        "find": _op_find,
        "list": _op_list,
        "write": lambda data: _op_edit_content(data, "write"),
        "append": lambda data: _op_edit_content(data, "append"),
        "replace_content": lambda data: _op_edit_content(
            data, "replace_content"),
        "remove_content": lambda data: _op_edit_content(
            data, "remove_content"),
        "clear_content": lambda data: _op_edit_content(
            data, "clear_content"),
    }
    try:
        return handlers[operation](args)
    except Exception as exc:  # noqa: BLE001 - never surface a traceback
        print(f"[FileControl] {operation} crashed: {type(exc).__name__}: {exc}")
        return _fail(operation, "failed",
                     f"The {operation} operation could not be completed "
                     f"({type(exc).__name__}).")

"""Safe ZIP archive management for Jarvis.

One registered action, ``zip_tool``, with the operations create, extract,
inspect, list, add, delete_entries and test.  Path resolution and protected
location checks are reused from ``file_control``; destructive/modifying
actions use the existing central approval service.

Security rules enforced here, not only in the prompt:

* every extraction target is normalised and checked to stay inside the
  requested destination (ZIP Slip, absolute/drive/UNC names, ``..``);
* symlink entries and symlinked destination ancestors are refused;
* duplicate and Windows case-insensitive entry collisions are rejected before
  any file is written;
* entry count, individual size and total uncompressed size limits guard
  against archive bombs;
* encrypted archives require an explicitly supplied password and passwords are
  never logged or returned;
* archive modification writes a temporary sibling and replaces the original
  only after the new archive passes verification.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core import approvals as _approvals

_OPERATIONS = ("create", "extract", "inspect", "list", "add",
               "delete_entries", "test")

_COMPRESSION = {
    "store": zipfile.ZIP_STORED,
    "stored": zipfile.ZIP_STORED,
    "deflate": zipfile.ZIP_DEFLATED,
    "bzip2": zipfile.ZIP_BZIP2,
    "lzma": zipfile.ZIP_LZMA,
}
_COMPRESSION_NAMES = {
    zipfile.ZIP_STORED: "store",
    zipfile.ZIP_DEFLATED: "deflate",
    zipfile.ZIP_BZIP2: "bzip2",
    zipfile.ZIP_LZMA: "lzma",
}

_DEFAULT_MAX_ENTRIES = 10_000
_DEFAULT_MAX_TOTAL = 4 * 1024 * 1024 * 1024       # 4 GiB
_DEFAULT_MAX_ENTRY = 1024 * 1024 * 1024           # 1 GiB
_DEFAULT_MAX_RATIO = 2_000
_MAX_PAGE_SIZE = 500


class ZipToolError(ValueError):
    """A user-facing archive error."""


def _limit(name: str, default: int) -> int:
    raw = os.environ.get(f"JARVIS_ZIP_{name}", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _max_entries() -> int:
    return _limit("MAX_ENTRIES", _DEFAULT_MAX_ENTRIES)


def _max_total() -> int:
    return _limit("MAX_TOTAL_BYTES", _DEFAULT_MAX_TOTAL)


def _max_entry() -> int:
    return _limit("MAX_ENTRY_BYTES", _DEFAULT_MAX_ENTRY)


def _max_ratio() -> int:
    return _limit("MAX_RATIO", _DEFAULT_MAX_RATIO)


def _fc():
    from actions import file_control_tool

    return file_control_tool


def _resolve(raw: Any, label: str = "path") -> str:
    text = str(raw or "").strip()
    if not text:
        raise ZipToolError(f"A {label} is required.")
    path, error = _fc()._resolve_path(text)
    if error:
        raise ZipToolError(
            f"{label.capitalize()} is invalid: {error.get('error') or error}")
    return path


def _ensure_writable(path: str, label: str) -> None:
    reason = _fc()._write_blocked_reason(path)
    if reason:
        raise ZipToolError(f"The {label} cannot be used because {reason}.")


def _ensure_protected_ok(path: str, label: str) -> None:
    reason = _fc()._protected_reason(path)
    if reason:
        raise ZipToolError(f"The {label} cannot be changed because {reason}.")


def _ok(operation: str, message: str, **fields: Any) -> Dict[str, Any]:
    result = {"success": True, "ok": True, "operation": operation,
              "message": message}
    result.update(fields)
    return result


def _fail(operation: str, reason: str, error: str, **fields: Any
          ) -> Dict[str, Any]:
    result = {"success": False, "ok": False, "operation": operation,
              "reason": reason, "error": error}
    result.update(fields)
    return result


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
        "message": f"{summary}. The user must approve this in the Jarvis interface.",
        "error": ("Approval required in the Jarvis interface. Do not retry "
                  "until the user approves."),
    }


def _authorize(operation: str, archive: str, *, destination: str = "",
               parameters: Optional[Dict[str, Any]] = None,
               summary: str = "") -> Optional[Dict[str, Any]]:
    params = dict(parameters or {})
    try:
        allowed, request, _reason = _approvals.get_service().authorize(
            tool="zip_tool",
            operation=operation,
            target=archive or destination,
            parameters=params,
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        return _fail(operation, "approval_unavailable",
                     f"Approval is unavailable, so nothing was changed: "
                     f"{type(exc).__name__}: {exc}")
    if not allowed:
        return _approval_required(operation, summary, request)
    return None


def _compression(value: Any) -> Tuple[int, str]:
    name = str(value or "deflate").strip().casefold()
    if name not in _COMPRESSION:
        raise ZipToolError(
            "compression must be store, deflate, bzip2 or lzma.")
    return _COMPRESSION[name], name


def _compression_level(value: Any, method: int) -> Optional[int]:
    if value in (None, "", 0) or str(value).strip() in ("", "0"):
        return None
    if method == zipfile.ZIP_STORED:
        return None
    try:
        level = int(value)
    except (TypeError, ValueError) as exc:
        raise ZipToolError("compression_level must be an integer.") from exc
    if not (0 <= level <= 9):
        raise ZipToolError("compression_level must be between 0 and 9.")
    return level


def _entry_name(name: Any, *, directory: bool = False) -> str:
    text = str(name or "").replace("\\", "/")
    if not text or "\x00" in text or any(ord(ch) < 0x20 for ch in text):
        raise ZipToolError(f"Archive entry name is invalid: {name!r}.")
    if text.startswith("/") or text.startswith("//"):
        raise ZipToolError(f"Archive entry is an absolute path: {name!r}.")
    if re.match(r"^[A-Za-z]:", text):
        raise ZipToolError(
            f"Archive entry is drive-qualified: {name!r}.")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ZipToolError(
            f"Archive entry would escape the destination: {name!r}.")
    for part in parts:
        name_error = _fc()._validate_name(part)
        if name_error:
            raise ZipToolError(f"Archive entry {name!r}: {name_error}")
    result = "/".join(parts)
    return result + "/" if directory else result


def _is_link(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _entry_size_limits(infos: Sequence[zipfile.ZipInfo]) -> Tuple[int, int]:
    if len(infos) > _max_entries():
        raise ZipToolError(
            f"Archive contains {len(infos)} entries, above the "
            f"{_max_entries()}-entry safety limit.")
    total = 0
    for info in infos:
        size = int(info.file_size)
        if size > _max_entry():
            raise ZipToolError(
                f"Archive entry '{info.filename}' is {size} bytes, above "
                f"the {_max_entry()}-byte per-entry limit.")
        total += size
        if total > _max_total():
            raise ZipToolError(
                f"Archive expands to more than {_max_total()} bytes, above "
                "the configured safety limit.")
    return total, len(infos)


def _open_archive(path: str, password: str = "") -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path, "r")
    except FileNotFoundError as exc:
        raise ZipToolError(f"Archive not found: {path}") from exc
    except zipfile.BadZipFile as exc:
        raise ZipToolError(f"Not a valid ZIP archive: {path}") from exc
    except OSError as exc:
        raise ZipToolError(f"Could not open archive '{path}': {exc}") from exc
    except RuntimeError as exc:
        raise ZipToolError(
            "The archive is encrypted or unreadable; supply the password if "
            "the user provided one.") from exc
    return archive


def _needs_password(infos: Iterable[zipfile.ZipInfo]) -> bool:
    return any(bool(info.flag_bits & 0x1) for info in infos)


def _pwd(password: str) -> Optional[bytes]:
    return password.encode("utf-8") if password else None


def _verify_archive(path: str, expected: Optional[Iterable[str]] = None,
                    password: str = "") -> Tuple[bool, str]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            bad = archive.testzip()
            if bad:
                return False, f"CRC verification failed for '{bad}'."
            if expected is not None:
                names = {info.filename.rstrip("/") for info in archive.infolist()}
                missing = sorted(set(expected) - names)
                if missing:
                    return False, ("Missing entries after writing: "
                                   + ", ".join(missing[:5]))
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return False, f"Archive verification failed: {type(exc).__name__}: {exc}"
    return True, ""


def _as_list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [item for item in value if str(item).strip()]
    return [value]


def _source_entries(sources: Sequence[Any], destination: str
                    ) -> List[Tuple[str, str, bool]]:
    """Return (archive_name, source_path, is_dir) for every source entry."""
    records: List[Tuple[str, str, bool]] = []
    seen: set[str] = set()
    destination_norm = os.path.normcase(os.path.abspath(destination))

    def add(name: str, source: str, directory: bool) -> None:
        key = name.rstrip("/").casefold()
        if key in seen:
            raise ZipToolError(f"Duplicate archive entry name: {name}")
        seen.add(key)
        records.append((name, source, directory))

    for raw in sources or []:
        source = _resolve(raw, "source")
        if not os.path.lexists(source):
            raise ZipToolError(f"Source does not exist: {source}")
        if _fc()._is_link(source):
            raise ZipToolError(
                f"Source '{source}' is a symbolic link or junction; links "
                "are not archived.")
        base = os.path.basename(os.path.normpath(source))
        if not base:
            raise ZipToolError(f"Source '{source}' has no usable name.")
        if not os.path.isdir(source):
            if os.path.normcase(os.path.abspath(source)) == destination_norm:
                raise ZipToolError(
                    "The destination archive cannot also be a source.")
            add(base, source, False)
            continue
        root_name = _entry_name(base, directory=True)
        def walk_error(error: OSError) -> None:
            raise ZipToolError(
                f"Could not read '{getattr(error, 'filename', source)}': {error}")

        for current, dirs, files in os.walk(source, followlinks=False,
                                           onerror=walk_error):
            for directory in dirs:
                full = os.path.join(current, directory)
                if _fc()._is_link(full):
                    raise ZipToolError(
                        f"Source '{full}' is a symbolic link or junction; "
                        "links are not archived.")
            dirs[:] = sorted(dirs)
            relative = os.path.relpath(current, source)
            arc_dir = (root_name if relative == "."
                       else root_name + relative.replace(os.sep, "/") + "/")
            add(arc_dir, current, True)
            for filename in sorted(files):
                full = os.path.join(current, filename)
                if _fc()._is_link(full):
                    raise ZipToolError(
                        f"Source '{full}' is a symbolic link or junction; "
                        "links are not archived.")
                if os.path.normcase(os.path.abspath(full)) == destination_norm:
                    continue
                add(arc_dir + filename, full, False)
    return records


def _write_entries(archive_path: str, records: Sequence[Tuple[str, str, bool]],
                   compression: int, level: Optional[int]) -> None:
    kwargs: Dict[str, Any] = {"compression": compression}
    if level is not None:
        kwargs["compresslevel"] = level
    with zipfile.ZipFile(archive_path, "w", **kwargs) as archive:
        for name, source, directory in records:
            if directory:
                info = zipfile.ZipInfo(name)
                info.external_attr = (0o40775 << 16) | 0x10
                archive.writestr(info, b"")
            else:
                archive.write(source, name)


def _temp_archive(directory: str, suffix: str = ".zip") -> str:
    descriptor, path = tempfile.mkstemp(
        prefix=".jarvis-zip-", suffix=suffix, dir=directory)
    os.close(descriptor)
    return path


def _safe_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _create(operation: str, source_values: Sequence[Any], destination: Any,
            compression_value: Any, level_value: Any) -> Dict[str, Any]:
    if not source_values:
        return _fail(operation, "invalid", "At least one source is required.")
    try:
        destination_path = _resolve(destination, "destination")
        if not destination_path.lower().endswith(".zip"):
            destination_path += ".zip"
        parent = os.path.dirname(destination_path)
        _ensure_writable(parent or destination_path, "destination")
        if os.path.isdir(destination_path):
            raise ZipToolError("The destination is a directory, not a ZIP file.")
        exists = os.path.lexists(destination_path)
        if exists:
            _ensure_protected_ok(destination_path, "archive")
        compression, _name = _compression(compression_value)
        level = _compression_level(level_value, compression)
        records = _source_entries(source_values, destination_path)
        if not records:
            raise ZipToolError("No files or folders were found to archive.")
    except ZipToolError as exc:
        return _fail(operation, "invalid", str(exc), archive_path=str(destination or ""))
    except OSError as exc:
        return _fail(operation, "failed", f"Could not prepare the archive: {exc}",
                     archive_path=str(destination or ""))

    if exists:
        summary = f"Replace the existing archive '{destination_path}'"
        blocked = _authorize(
            "create_overwrite", destination_path,
            parameters={"destination": os.path.normcase(os.path.abspath(destination_path))},
            summary=summary)
        if blocked:
            return blocked
    try:
        os.makedirs(parent or ".", exist_ok=True)
        temp = _temp_archive(parent or ".", ".zip")
    except OSError as exc:
        return _fail(operation, "failed", f"Could not create the archive: {exc}",
                     archive_path=destination_path)
    try:
        _write_entries(temp, records, compression, level)
        ok, why = _verify_archive(temp, [name.rstrip("/") for name, _s, _d in records])
        if not ok:
            return _fail(operation, "verify_failed", why,
                         archive_path=destination_path)
        if os.path.lexists(destination_path) and not exists:
            return _fail(operation, "conflict",
                         f"'{destination_path}' appeared before it could be written.",
                         archive_path=destination_path)
        os.replace(temp, destination_path)
    except Exception as exc:  # noqa: BLE001 - reported truthfully
        return _fail(operation, "failed",
                     f"Could not create '{destination_path}': "
                     f"{type(exc).__name__}: {exc}", archive_path=destination_path)
    finally:
        _safe_remove(temp)
    files = sum(1 for _n, _s, is_dir in records if not is_dir)
    dirs = len(records) - files
    return _ok(operation, f"Created '{destination_path}'.",
               archive_path=os.path.abspath(destination_path),
               entries_processed=len(records), files_created=files,
               directories_created=dirs, compression=_name,
               total_uncompressed_size=sum(os.path.getsize(s) for _n, s, d in records if not d),
               integrity_verified=True)


def _summary_entries(infos: Sequence[zipfile.ZipInfo]) -> List[Dict[str, Any]]:
    return [{
        "name": info.filename,
        "size": int(info.file_size),
        "compressed_size": int(info.compress_size),
        "is_dir": info.is_dir(),
        "method": _COMPRESSION_NAMES.get(info.compress_type, str(info.compress_type)),
        "encrypted": bool(info.flag_bits & 0x1),
        "crc": f"{int(info.CRC):08x}",
    } for info in infos]


def _bounded_entries(infos: Sequence[zipfile.ZipInfo], page: int,
                     page_size: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 100), _MAX_PAGE_SIZE))
    start = (page - 1) * page_size
    rows = _summary_entries(infos[start:start + page_size])
    return rows, {"page": page, "page_size": page_size,
                  "total_entries": len(infos),
                  "has_more": start + len(rows) < len(infos)}


def _inspect(operation: str, archive_value: Any, page: Any,
             page_size: Any, password: str) -> Dict[str, Any]:
    try:
        path = _resolve(archive_value, "archive path")
        with _open_archive(path, password) as archive:
            infos = archive.infolist()
            total, count = _entry_size_limits(infos)
            compressed = sum(int(info.compress_size) for info in infos)
            methods = sorted({_COMPRESSION_NAMES.get(info.compress_type,
                                                     str(info.compress_type))
                              for info in infos})
            ratio = (total / compressed) if compressed else 0.0
            rows, paging = _bounded_entries(infos, page, page_size)
            integrity = "structure_readable"
            if total <= 256 * 1024 * 1024:
                if _needs_password(infos):
                    integrity = "encrypted_not_checked"
                else:
                    bad = archive.testzip()
                    integrity = "ok" if bad is None else f"corrupt:{bad}"
            warnings = []
            if _needs_password(infos):
                warnings.append("Archive contains encrypted entries.")
            if ratio > _max_ratio():
                warnings.append(
                    f"Compression ratio {ratio:.1f}:1 exceeds the configured "
                    "warning limit.")
    except ZipToolError as exc:
        return _fail(operation, "invalid", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(operation, "failed",
                     f"Could not inspect the archive: {type(exc).__name__}: {exc}")
    return _ok(operation, f"Inspected '{os.path.basename(path)}'.",
               archive_path=os.path.abspath(path),
               archive_size=os.path.getsize(path),
               entries=rows,
               total_uncompressed_size=total,
               total_compressed_size=compressed,
               compression_methods=methods,
               compression_ratio=round(ratio, 2),
               integrity=integrity, warnings=warnings, **paging)


def _list(operation: str, archive_value: Any, prefix: Any, page: Any,
          page_size: Any, password: str) -> Dict[str, Any]:
    try:
        path = _resolve(archive_value, "archive path")
        prefix_norm = str(prefix or "").replace("\\", "/").strip("/")
        with _open_archive(path, password) as archive:
            infos = archive.infolist()
            if prefix_norm:
                key = prefix_norm.casefold() + "/"
                infos = [info for info in infos
                         if info.filename.casefold() == prefix_norm.casefold()
                         or info.filename.casefold().startswith(key)]
            rows, paging = _bounded_entries(infos, page, page_size)
    except ZipToolError as exc:
        return _fail(operation, "invalid", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(operation, "failed",
                     f"Could not list the archive: {type(exc).__name__}: {exc}")
    return _ok(operation, f"Listed {len(rows)} archive entries.",
               archive_path=os.path.abspath(path), entries=rows,
               path_prefix=prefix_norm, **paging)


def _safe_target(destination: str, entry: str) -> str:
    destination_abs = os.path.abspath(destination)
    parts = entry.rstrip("/").split("/")
    target = os.path.abspath(os.path.join(destination_abs, *parts))
    destination_key = os.path.normcase(destination_abs)
    target_key = os.path.normcase(target)
    try:
        if os.path.commonpath([destination_key, target_key]) != destination_key:
            raise ZipToolError(
                f"Archive entry '{entry}' escapes the extraction destination.")
    except ValueError as exc:
        raise ZipToolError(
            f"Archive entry '{entry}' escapes the extraction destination.") from exc
    if target_key == destination_key:
        raise ZipToolError(f"Archive entry '{entry}' targets the destination root.")
    relative = os.path.relpath(target, destination_abs)
    current = destination_abs
    for part in relative.split(os.sep)[:-1]:
        current = os.path.join(current, part)
        if os.path.lexists(current) and _fc()._is_link(current):
            raise ZipToolError(
                f"Extraction would pass through a link at '{current}'.")
    return target


def _extract(operation: str, archive_value: Any, destination_value: Any,
             overwrite: bool, selected: Optional[Sequence[Any]],
             password: str) -> Dict[str, Any]:
    try:
        archive_path = _resolve(archive_value, "archive path")
        destination = _resolve(destination_value, "destination")
        _ensure_writable(destination, "destination")
        if _fc()._is_drive_root(destination):
            raise ZipToolError("Refusing to extract directly into a drive root.")
        if os.path.lexists(destination):
            if _fc()._is_link(destination):
                raise ZipToolError("The extraction destination is a link.")
            if not os.path.isdir(destination):
                raise ZipToolError(
                    "The extraction destination is not a directory.")
        with _open_archive(archive_path, password) as archive:
            infos = archive.infolist()
            total, count = _entry_size_limits(infos)
            if _needs_password(infos) and not password:
                raise ZipToolError(
                    "This archive is encrypted. Supply the password explicitly "
                    "if the user gave one.")
            selected_keys = {
                _entry_name(item) for item in (selected or []) if str(item).strip()
            }
            chosen: List[zipfile.ZipInfo] = []
            selected_dirs = {
                _entry_name(info.filename, directory=True).casefold()
                for info in infos
                if info.is_dir()
                and _entry_name(info.filename, directory=info.is_dir()).rstrip("/")
                in {item.rstrip("/") for item in selected_keys}
            }
            # A folder may be selected before it has an explicit directory
            # entry (some ZIP writers only store its children).
            selected_dirs.update(
                item.rstrip("/").casefold() + "/" for item in selected_keys)
            for info in infos:
                name = _entry_name(info.filename, directory=info.is_dir())
                if selected_keys:
                    exact = name.rstrip("/") in {
                        item.rstrip("/") for item in selected_keys}
                    inside = any(
                        name.casefold().startswith(prefix)
                        for prefix in selected_dirs)
                    if not exact and not inside:
                        continue
                if _is_link(info):
                    raise ZipToolError(
                        f"Archive entry '{info.filename}' is a symbolic link; "
                        "links are never extracted.")
                chosen.append(info)
            if selected_keys:
                found = set()
                for info in chosen:
                    normalized = _entry_name(
                        info.filename, directory=info.is_dir()).rstrip("/")
                    for item in selected_keys:
                        key = item.rstrip("/")
                        if (normalized == key
                                or normalized.casefold().startswith(
                                    key.casefold() + "/")):
                            found.add(item)
                missing = sorted(set(selected_keys) - found)
                if missing:
                    raise ZipToolError(
                        "Selected entries were not found: " + ", ".join(missing[:10]))
            if not chosen:
                raise ZipToolError("No archive entries matched the request.")
            seen: set[str] = set()
            targets: List[Tuple[zipfile.ZipInfo, str]] = []
            for info in chosen:
                name = _entry_name(info.filename, directory=info.is_dir())
                key = name.rstrip("/").casefold()
                if key in seen:
                    raise ZipToolError(
                        f"Archive contains duplicate/case-insensitive entry "
                        f"name: {info.filename}")
                seen.add(key)
                targets.append((info, _safe_target(destination, name)))
            conflicts = [target for info, target in targets
                         if not info.is_dir() and os.path.lexists(target)]
            if conflicts and not overwrite:
                return _fail(
                    operation, "conflict",
                    f"{len(conflicts)} destination file(s) already exist; "
                    "nothing was extracted.",
                    archive_path=os.path.abspath(archive_path),
                    destination=os.path.abspath(destination),
                    files_skipped=len(conflicts),
                    conflicts=[os.path.relpath(p, destination)
                               for p in conflicts[:20]])
            if conflicts and overwrite:
                summary = (f"Extract and overwrite {len(conflicts)} existing "
                           f"file(s) into '{destination}'")
                blocked = _authorize(
                    "extract_overwrite", archive_path,
                    destination=destination,
                    parameters={
                        "archive_path": os.path.normcase(os.path.abspath(archive_path)),
                        "destination": os.path.normcase(os.path.abspath(destination)),
                        "entries": sorted(os.path.relpath(p, destination)
                                          for p in conflicts),
                    },
                    summary=summary)
                if blocked:
                    return blocked
        os.makedirs(destination, exist_ok=True)
        if _fc()._is_link(destination):
            raise ZipToolError("The extraction destination became a link.")
        created = 0
        processed = 0
        dirs = 0
        with _open_archive(archive_path, password) as archive:
            for info, target in targets:
                name = _entry_name(info.filename, directory=info.is_dir())
                if info.is_dir():
                    if os.path.lexists(target) and _fc()._is_link(target):
                        raise ZipToolError(
                            f"Extraction would replace a link at '{target}'.")
                    os.makedirs(target, exist_ok=True)
                    dirs += 1
                    processed += 1
                    continue
                parent = os.path.dirname(target)
                os.makedirs(parent, exist_ok=True)
                if os.path.lexists(target) and not overwrite:
                    return _fail(
                        operation, "conflict",
                        f"'{os.path.relpath(target, destination)}' appeared "
                        "during extraction; the operation was stopped.",
                        archive_path=os.path.abspath(archive_path),
                        destination=os.path.abspath(destination),
                        files_created=created,
                        entries_processed=processed)
                descriptor, temp = tempfile.mkstemp(
                    prefix=".jarvis-unzip-", dir=parent)
                try:
                    with os.fdopen(descriptor, "wb") as output, \
                            archive.open(info, pwd=_pwd(password)) as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    if os.path.getsize(temp) != info.file_size:
                        raise ZipToolError(
                            f"Extracted size mismatch for '{info.filename}'.")
                    os.replace(temp, target)
                except Exception:
                    _safe_remove(temp)
                    raise
                created += 1
                processed += 1
    except ZipToolError as exc:
        return _fail(operation, "invalid", str(exc))
    except (zipfile.BadZipFile, RuntimeError) as exc:
        return _fail(operation, "corrupt",
                     f"The archive could not be read (encrypted or corrupt): {exc}")
    except (PermissionError, OSError) as exc:
        return _fail(operation, "failed", f"Extraction failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _fail(operation, "failed",
                     f"Extraction failed: {type(exc).__name__}: {exc}")
    return _ok(operation, f"Extracted {created} file(s) to '{destination}'.",
               archive_path=os.path.abspath(archive_path),
               destination=os.path.abspath(destination),
               entries_processed=processed, files_created=created,
               directories_created=dirs,
               total_uncompressed_size=sum(info.file_size for info, _t in targets
                                           if not info.is_dir()),
               integrity_verified=True)


def _modify_common(operation: str, archive_value: Any,
                   password: str) -> Tuple[Optional[str], Optional[zipfile.ZipFile]]:
    archive_path = _resolve(archive_value, "archive path")
    if not os.path.isfile(archive_path):
        raise ZipToolError(f"Archive not found: {archive_path}")
    _ensure_protected_ok(archive_path, "archive")
    archive = _open_archive(archive_path, password)
    infos = archive.infolist()
    _entry_size_limits(infos)
    if _needs_password(infos):
        archive.close()
        raise ZipToolError(
            "Encrypted archives cannot be modified in place; extract with the "
            "password and create a new archive instead.")
    return archive_path, archive


def _add(operation: str, archive_value: Any, source_values: Sequence[Any],
         password: str) -> Dict[str, Any]:
    if not source_values:
        return _fail(operation, "invalid", "At least one source is required.")
    archive = None
    temp = ""
    try:
        archive_path, archive = _modify_common(operation, archive_value, password)
        existing = {_entry_name(info.filename, directory=info.is_dir())
                    .rstrip("/").casefold()
                    for info in archive.infolist()}
        records = _source_entries(source_values, archive_path)
        new_records = []
        skipped = []
        for name, source, directory in records:
            key = name.rstrip("/").casefold()
            if key in existing:
                skipped.append(name.rstrip("/"))
            else:
                existing.add(key)
                new_records.append((name, source, directory))
        if not new_records:
            return _ok(operation, "Those entries are already in the archive.",
                       archive_path=os.path.abspath(archive_path),
                       entries_processed=0, files_skipped=len(skipped),
                       warnings=["No new entries were added."])
        blocked = _authorize(
            "add", archive_path,
            parameters={"archive_path": os.path.normcase(os.path.abspath(archive_path)),
                        "entries": sorted(name.rstrip("/") for name, _s, _d in new_records)},
            summary=f"Add {len(new_records)} entr"
                    f"{'y' if len(new_records) == 1 else 'ies'} to "
                    f"'{archive_path}'")
        if blocked:
            return blocked
        parent = os.path.dirname(archive_path) or "."
        temp = _temp_archive(parent, ".zip")
        archive.close()
        archive = None
        shutil.copy2(archive_path, temp)
        with zipfile.ZipFile(temp, "a") as output:
            _write_into_open(output, new_records)
        ok, why = _verify_archive(
            temp, [name.rstrip("/") for name, _s, _d in new_records])
        if not ok:
            return _fail(operation, "verify_failed", why,
                         archive_path=os.path.abspath(archive_path))
        os.replace(temp, archive_path)
        temp = ""
        return _ok(operation, f"Added {len(new_records)} entr"
                   f"{'y' if len(new_records) == 1 else 'ies'} to the archive.",
                   archive_path=os.path.abspath(archive_path),
                   entries_processed=len(new_records),
                   files_created=sum(1 for _n, _s, d in new_records if not d),
                   files_skipped=len(skipped),
                   warnings=[f"Skipped existing: {', '.join(skipped[:10])}"]
                   if skipped else [],
                   integrity_verified=True)
    except ZipToolError as exc:
        return _fail(operation, "invalid", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(operation, "failed",
                     f"Could not add to the archive: {type(exc).__name__}: {exc}")
    finally:
        if archive is not None:
            archive.close()
        _safe_remove(temp)


def _write_into_open(output: zipfile.ZipFile,
                     records: Sequence[Tuple[str, str, bool]]) -> None:
    for name, source, directory in records:
        if directory:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o40775 << 16) | 0x10
            output.writestr(info, b"")
        else:
            output.write(source, name)


def _delete_entries(operation: str, archive_value: Any,
                    entry_values: Sequence[Any], password: str) -> Dict[str, Any]:
    if not entry_values:
        return _fail(operation, "invalid", "At least one entry is required.")
    archive = None
    temp = ""
    try:
        archive_path, archive = _modify_common(operation, archive_value, password)
        wanted = {_entry_name(item).rstrip("/") for item in entry_values
                  if str(item).strip()}
        infos = archive.infolist()
        known = {_entry_name(info.filename, directory=info.is_dir()).rstrip("/")
                 for info in infos}
        missing = sorted(wanted - known)
        if missing:
            return _fail(operation, "not_found",
                         "These entries were not found: " + ", ".join(missing[:10]),
                         archive_path=os.path.abspath(archive_path))
        blocked = _authorize(
            "delete_entries", archive_path,
            parameters={"archive_path": os.path.normcase(os.path.abspath(archive_path)),
                        "entries": sorted(wanted)},
            summary=f"Delete {len(wanted)} entr"
                    f"{'y' if len(wanted) == 1 else 'ies'} from '{archive_path}'")
        if blocked:
            return blocked
        parent = os.path.dirname(archive_path) or "."
        temp = _temp_archive(parent, ".zip")
        with zipfile.ZipFile(temp, "w") as output:
            output.comment = archive.comment
            for info in infos:
                name = _entry_name(info.filename, directory=info.is_dir()).rstrip("/")
                if name in wanted:
                    continue
                if info.is_dir():
                    output.writestr(info, b"")
                else:
                    with archive.open(info, pwd=_pwd(password)) as source, \
                            output.open(info, "w") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
        archive.close()
        archive = None
        remaining = [
            _entry_name(info.filename, directory=info.is_dir()).rstrip("/")
            for info in infos
            if _entry_name(info.filename,
                           directory=info.is_dir()).rstrip("/") not in wanted]
        ok, why = _verify_archive(temp, remaining)
        if not ok:
            return _fail(operation, "verify_failed", why,
                         archive_path=os.path.abspath(archive_path))
        os.replace(temp, archive_path)
        temp = ""
        return _ok(operation, f"Deleted {len(wanted)} entr"
                   f"{'y' if len(wanted) == 1 else 'ies'} from the archive.",
                   archive_path=os.path.abspath(archive_path),
                   entries_processed=len(wanted), files_deleted=len(wanted),
                   integrity_verified=True)
    except ZipToolError as exc:
        return _fail(operation, "invalid", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(operation, "failed",
                     f"Could not delete archive entries: {type(exc).__name__}: {exc}")
    finally:
        if archive is not None:
            archive.close()
        _safe_remove(temp)


def _test(operation: str, archive_value: Any, password: str) -> Dict[str, Any]:
    try:
        path = _resolve(archive_value, "archive path")
        with _open_archive(path, password) as archive:
            infos = archive.infolist()
            total, count = _entry_size_limits(infos)
            if _needs_password(infos) and not password:
                raise ZipToolError(
                    "This archive is encrypted. Supply the password explicitly "
                    "if the user gave one.")
            corrupt = []
            checked = 0
            for info in infos:
                if info.is_dir():
                    continue
                try:
                    with archive.open(info, pwd=_pwd(password)) as source:
                        while source.read(1024 * 1024):
                            pass
                    checked += 1
                except Exception as exc:  # noqa: BLE001 - collect all bad entries
                    corrupt.append({"name": info.filename,
                                    "error": f"{type(exc).__name__}: {exc}"})
    except ZipToolError as exc:
        return _fail(operation, "invalid", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(operation, "failed",
                     f"Could not test the archive: {type(exc).__name__}: {exc}")
    if corrupt:
        return _fail(operation, "corrupt",
                     f"{len(corrupt)} entr"
                     f"{'y' if len(corrupt) == 1 else 'ies'} failed verification.",
                     archive_path=os.path.abspath(path),
                     entries_processed=checked, total_entries=count,
                     total_uncompressed_size=total,
                     corrupt_entries=corrupt, integrity_verified=False)
    return _ok(operation, "Archive passed CRC/read checks.",
               archive_path=os.path.abspath(path),
               entries_processed=checked, total_entries=count,
               total_uncompressed_size=total,
               corrupt_entries=[], integrity_verified=True)


def zip_tool(
    operation: str,
    sources: Optional[List[str]] = None,
    destination: str = "",
    archive_path: str = "",
    compression: str = "deflate",
    compression_level: Any = 0,
    overwrite: Any = False,
    selected_entries: Optional[List[str]] = None,
    entries: Optional[List[str]] = None,
    path_prefix: str = "",
    page: Any = 1,
    page_size: Any = 100,
    password: str = "",
) -> Dict[str, Any]:
    """Create, inspect, extract and manage ZIP archives safely."""
    op = str(operation or "").strip().casefold()
    if op not in _OPERATIONS:
        return _fail("zip_tool", "invalid",
                     "operation must be one of: " + ", ".join(_OPERATIONS) + ".")
    overwrite_flag = _fc()._bool(overwrite)
    sources = _as_list(sources)
    selected_entries = _as_list(selected_entries)
    entries = _as_list(entries)
    try:
        if op == "create":
            return _create(op, sources, destination,
                           compression, compression_level)
        if op == "extract":
            return _extract(op, archive_path, destination, overwrite_flag,
                            selected_entries, password)
        if op == "inspect":
            return _inspect(op, archive_path, page, page_size, password)
        if op == "list":
            return _list(op, archive_path, path_prefix, page, page_size, password)
        if op == "add":
            return _add(op, archive_path, sources, password)
        if op == "delete_entries":
            return _delete_entries(op, archive_path, entries, password)
        if op == "test":
            return _test(op, archive_path, password)
    except ZipToolError as exc:
        return _fail(op, "invalid", str(exc))
    except Exception as exc:  # noqa: BLE001 - actions always answer
        return _fail(op, "failed", f"{type(exc).__name__}: {exc}")
    return _fail(op, "invalid", "Unsupported operation.")


__all__ = ["zip_tool"]

"""Aze's tool packages.

Every tool module lives directly in this directory::

    actions/
      open_app_tool, close_app_tool, open_file_tool, close_file_tool,
      file_control_tool, read_document_tool, reminders_tool,
      system_control_tool,
      open_web_tool, close_web_tool,
      web_fetch_tool, research_tool, screen_understanding_tool,
      screenshot_tool, zip_tool, youtube_player_tool, phone_call_tool

Discovery scans every ``*_tool.py`` file recursively and loads it by its real
path, so an optional category subfolder is still supported for external packs.
A module keeps its stable tool name (the file stem without ``_tool``), which is
what the registry and cross-tool helpers use.

Backward compatibility
----------------------
The first releases exposed every tool as ``actions.open_app_tool`` and so on.
Those import paths are still valid: every discovered module is also registered
under its flat file-stem name in ``sys.modules``. Existing imports, tests and
``mock.patch("actions.open_file_tool...")`` keep working, while there is still
exactly one module object per tool.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_TOOL_FILE_SUFFIX = "_tool.py"
_TOOL_ROOT = Path(__file__).resolve().parent

_TOOL_MODULES: dict[str, ModuleType] = {}
_TOOL_RECORDS_BY_NAME: dict[str, dict[str, Any]] = {}
_TOOL_RECORDS_BY_ID: dict[str, dict[str, Any]] = {}
_LOADING: set[str] = set()


def discover_tool_files() -> list[dict[str, Any]]:
    """Every ``*_tool.py`` file, wherever a category folder currently is.

    The folder path is reported but never used as an import requirement: it is
    the category label. This scan is uncached so a renamed folder or a newly
    added tool is visible on the next UI refresh.
    """
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(_TOOL_ROOT.rglob(f"*{_TOOL_FILE_SUFFIX}")):
        try:
            relative = path.relative_to(_TOOL_ROOT)
        except ValueError:
            continue
        parents = relative.parts[:-1]
        if any(part.startswith(".") or part == "__pycache__"
               for part in parents):
            continue
        if not path.is_file():
            continue
        category = "/".join(parents)
        module = path.stem
        name = module[: -len("_tool")]
        tool_id = f"{category}/{name}" if category else name
        if not name or tool_id in seen:
            continue
        seen.add(tool_id)
        records.append({
            "id": tool_id,
            "name": name,
            "module": module,
            "category": category,
            "path": str(path),
        })
    return records


def _canonical_module_name(record: dict[str, Any]) -> str:
    parts = [__name__]
    if record["category"]:
        parts.extend(record["category"].split("/"))
    parts.append(record["module"])
    return ".".join(parts)


def _load_from_file(record: dict[str, Any], module_name: str) -> ModuleType:
    """Load a tool even when its folder name is not a normal package name."""
    spec = importlib.util.spec_from_file_location(module_name, record["path"])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {record['path']}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_record(record: dict[str, Any]) -> ModuleType | None:
    """Import one tool once, under its path-derived and flat stable names."""
    name = record["name"]
    if name in _TOOL_MODULES:
        return _TOOL_MODULES[name]
    if name in _LOADING:
        # A cross-tool import asked for a module that is still initialising.
        return None
    _LOADING.add(name)
    module: ModuleType | None = None
    try:
        module_name = _canonical_module_name(record)
        importlib.invalidate_caches()
        try:
            module = importlib.import_module(module_name)
        except Exception:
            # Folder names with spaces/dots are valid on disk but not always as
            # a package path. Loading by file path makes the folder name purely
            # cosmetic.
            module = _load_from_file(record, module_name)
        if module is None:
            return None
        _TOOL_MODULES[name] = module
        # Flat, stable aliases: actions.open_app_tool and similar.
        setattr(sys.modules[__name__], record["module"], module)
        alias = f"{__name__}.{record['module']}"
        sys.modules.setdefault(alias, module)
        return module
    finally:
        _LOADING.discard(name)


def get_tool_module(name: str) -> ModuleType | None:
    """Return a loaded tool module by tool name, file stem or canonical id.

    Tool code uses this instead of a folder-specific import path, so moving a
    category folder cannot break cross-tool helpers. Newly added files are
    loaded on demand as well.
    """
    full = str(name or "").strip()
    if not full:
        return None
    record = _TOOL_RECORDS_BY_ID.get(full)
    if record is not None:
        key = record["name"]
    else:
        key = full
        if "/" in key:
            key = key.rsplit("/", 1)[-1]
        if key.endswith("_tool"):
            key = key[: -len("_tool")]
    module = _TOOL_MODULES.get(key)
    if module is not None:
        return module

    record = record or _TOOL_RECORDS_BY_NAME.get(key)
    if record is None:
        for item in discover_tool_files():
            if item["name"] == key:
                record = item
                _TOOL_RECORDS_BY_NAME.setdefault(key, item)
                _TOOL_RECORDS_BY_ID.setdefault(item["id"], item)
                break
    if record is None:
        return None
    try:
        _load_record(record)
    except Exception as exc:  # noqa: BLE001 - one bad tool must not hide others
        print(f"[Actions] Could not load {record['path']}: "
              f"{type(exc).__name__}: {exc}")
    return _TOOL_MODULES.get(key)


def _load_all_tools() -> None:
    for record in discover_tool_files():
        _TOOL_RECORDS_BY_NAME.setdefault(record["name"], record)
        _TOOL_RECORDS_BY_ID.setdefault(record["id"], record)
        try:
            _load_record(record)
        except Exception as exc:  # noqa: BLE001 - report, never kill startup
            print(f"[Actions] Could not load {record['path']}: "
                  f"{type(exc).__name__}: {exc}")


_load_all_tools()

"""Safe, cached file access for Skill packages.

The loader enforces the package boundary for every read. A path that escapes a
Skill directory through ``..``, an absolute path, a drive prefix or a symlink is
rejected before any file is opened. Only text-like files can be read as
instructions or references; binary resources are listed but never decoded.
"""
from __future__ import annotations

import os
import posixpath
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Iterable, List, Sequence

from .models import SkillPackage

# Instruction documents are deliberately bounded. A Skill is specialised
# guidance, not a book; an oversized file is more likely a packaging mistake
# than useful context.
MAX_INSTRUCTION_BYTES = 256 * 1024
MAX_TEXT_BYTES = 512 * 1024
MAX_ASSETS = 200
_ASSET_KINDS = ("references", "resources")
_TEXT_SUFFIXES = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml",
    ".css", ".scss", ".less", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm",
    ".svg", ".xml", ".csv", ".py", ".sh", ".ps1", ".sql", ".ini", ".cfg",
})


class SkillLoadError(ValueError):
    """A Skill file could not be loaded safely."""


def normalise_relative_path(value: object) -> str:
    """Return a safe POSIX relative path or raise ``ValueError``.

    Both separators are normalised, drive-qualified paths and URL-style
    absolute paths are rejected, and traversal segments are refused before a
    path is joined to anything.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("path is empty")
    if "\x00" in raw:
        raise ValueError("path contains a null byte")
    unified = raw.replace("\\", "/")
    if unified.startswith("/") or unified.startswith("//"):
        raise ValueError("absolute paths are not allowed")
    if posixpath.isabs(unified):
        raise ValueError("absolute paths are not allowed")
    # Windows drive-relative and drive-absolute forms such as C:\x and C:x.
    if len(unified) >= 2 and unified[1] == ":":
        raise ValueError("drive-qualified paths are not allowed")
    parts = [part for part in unified.split("/") if part not in ("", ".")]
    if not parts:
        raise ValueError("path is empty")
    if any(part == ".." for part in parts):
        raise ValueError("path traversal is not allowed")
    return "/".join(parts)


def _is_within(path: Path, root: Path) -> bool:
    try:
        resolved_path = str(path.resolve(strict=False))
        resolved_root = str(root.resolve(strict=False))
    except (OSError, RuntimeError):
        return False
    try:
        return os.path.commonpath([
            os.path.normcase(resolved_path),
            os.path.normcase(resolved_root),
        ]) == os.path.normcase(resolved_root)
    except ValueError:
        # Different drives on Windows.
        return False


def resolve_inside(root: Path, relative: object) -> Path:
    """Resolve a relative package path and prove it stays inside ``root``."""
    clean = normalise_relative_path(relative)
    candidate = (root / Path(clean)).resolve(strict=False)
    if not _is_within(candidate, root):
        raise ValueError("path escapes the Skill package")
    return candidate


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_SUFFIXES


class SkillLoader:
    """Loads instruction/reference text with a bounded mtime-aware cache."""

    def __init__(self, cache_size: int = 128) -> None:
        self._lock = threading.RLock()
        self._cache: "OrderedDict[tuple, tuple[int, str]]" = OrderedDict()
        self._cache_size = max(8, int(cache_size))

    # ── primitive reads ──────────────────────────────────────────────────────

    def read_text(self, path: Path, *, max_bytes: int = MAX_TEXT_BYTES) -> str:
        """Read one text file, caching by path, mtime and size."""
        path = Path(path)
        try:
            stat = path.stat()
        except OSError as exc:
            raise SkillLoadError(f"cannot read {path.name}: {exc}") from exc
        if not path.is_file():
            raise SkillLoadError(f"{path.name} is not a file")
        if stat.st_size > max_bytes:
            raise SkillLoadError(
                f"{path.name} is too large ({stat.st_size} bytes; limit "
                f"{max_bytes})")
        if not is_text_file(path):
            raise SkillLoadError(
                f"{path.name} is not a readable text resource")

        key = (str(path.resolve(strict=False)), stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached[1]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SkillLoadError(f"cannot read {path.name}: {exc}") from exc
        with self._lock:
            self._cache[key] = (stat.st_mtime_ns, text)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return text

    # ── package-level API ────────────────────────────────────────────────────

    def load_entrypoint(self, package: SkillPackage) -> str:
        """Load the manifest's main instruction document (Level 2)."""
        path = resolve_inside(package.path, package.metadata.entrypoint)
        if not path.is_file():
            raise SkillLoadError(
                f"entrypoint {package.metadata.entrypoint!r} is missing")
        return self.read_text(path, max_bytes=MAX_INSTRUCTION_BYTES)

    def load_asset(
        self,
        package: SkillPackage,
        relative: object,
        *,
        allowed_kinds: Sequence[str] = _ASSET_KINDS,
    ) -> dict:
        """Load one reference/resource on demand (Level 3)."""
        clean = normalise_relative_path(relative)
        first = clean.split("/", 1)[0]
        allowed = {str(kind) for kind in allowed_kinds}
        if first not in allowed:
            raise SkillLoadError(
                "only references/ and resources/ files can be read")
        path = resolve_inside(package.path, clean)
        if not path.is_file():
            raise SkillLoadError(f"{clean} is not a file")
        text = self.read_text(path, max_bytes=MAX_TEXT_BYTES)
        stat = path.stat()
        return {
            "skill_id": package.id,
            "path": clean,
            "kind": first,
            "size": stat.st_size,
            "content": text,
        }

    def list_assets(self, package: SkillPackage) -> List[dict]:
        """List optional package assets without reading their contents."""
        assets: List[dict] = []
        seen: set[str] = set()
        for kind in _ASSET_KINDS:
            directory = package.path / kind
            if not directory.is_dir():
                continue
            try:
                candidates: Iterable[Path] = sorted(directory.rglob("*"))
            except OSError:
                continue
            for path in candidates:
                if len(assets) >= MAX_ASSETS:
                    return assets
                if not path.is_file():
                    continue
                if not _is_within(path, package.path):
                    # A symlinked asset pointing out of the package is not a
                    # package asset at all.
                    continue
                try:
                    relative = path.relative_to(package.path).as_posix()
                    size = path.stat().st_size
                except (OSError, ValueError):
                    continue
                if relative in seen:
                    continue
                seen.add(relative)
                assets.append({
                    "path": relative,
                    "kind": kind,
                    "size": size,
                    "readable": is_text_file(path),
                })
        assets.sort(key=lambda item: (item["kind"], item["path"].casefold()))
        return assets

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


def safe_declared_path(value: object, kind: str) -> str:
    """Validate a manifest-declared reference/resource path."""
    clean = normalise_relative_path(value)
    prefix = f"{kind}/"
    if not clean.startswith(prefix):
        raise ValueError(f"must live under {prefix}")
    if clean == prefix.rstrip("/"):
        raise ValueError("must name a file or directory inside the package")
    return clean

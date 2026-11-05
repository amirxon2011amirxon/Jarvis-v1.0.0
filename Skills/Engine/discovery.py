"""Filesystem discovery for Skill packages.

Discovery only reads ``manifest.yaml`` files. It never opens ``SKILL.md`` or
any reference/resource, which is what keeps the first disclosure level cheap.
Malformed packages are reported in ``DiscoveryResult.errors`` and skipped; one
bad package can never stop the application or hide the valid packages beside
it.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict

from .models import DiscoveredSkill, DiscoveryError, DiscoveryResult

MAX_MANIFEST_BYTES = 256 * 1024
_SOURCE_DIRS = (("Builtin", "builtin"), ("Installed", "installed"))

try:  # PyYAML is a declared dependency; this keeps a partial install graceful.
    import yaml as _yaml
except Exception:  # pragma: no cover - exercised only without PyYAML
    _yaml = None


def app_root() -> Path:
    """The application root, never the process working directory."""
    return Path(__file__).resolve().parent.parent.parent


def skills_root() -> Path:
    """The writable Skills directory, overridable for tests/alternate installs.

    In a source checkout this remains the repository's ``Skills/`` directory.
    A pip-installed copy keeps built-ins as package resources and copies them
    into the user's data directory, so ``Installed`` stays writable and is not
    placed inside site-packages.
    """
    override = (os.environ.get("JARVIS_SKILLS_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    package_root = Path(__file__).resolve().parent.parent
    try:
        import config

        data_dir = Path(config.DATA_DIR).expanduser().resolve()
    except Exception:  # noqa: BLE001 - source/test fallback
        data_dir = package_root.parent
    if data_dir == package_root.parent:
        return package_root
    target = data_dir / "Skills"
    try:
        (target / "Installed").mkdir(parents=True, exist_ok=True)
        source = package_root / "Builtin"
        if source.is_dir():
            shutil.copytree(source, target / "Builtin", dirs_exist_ok=True)
    except OSError:
        # A read-only profile still gets a clear capability error later; the
        # engine must not crash at import time.
        pass
    return target


def _safe_dir(path: Path) -> bool:
    try:
        return path.is_dir() and not path.is_symlink()
    except OSError:
        return False


def _read_manifest(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"cannot read manifest: {exc}") from exc
    if stat.st_size > MAX_MANIFEST_BYTES:
        raise ValueError(
            f"manifest.yaml is too large ({stat.st_size} bytes; limit "
            f"{MAX_MANIFEST_BYTES})")
    if path.is_symlink():
        raise ValueError("manifest.yaml may not be a symbolic link")
    if _yaml is None:
        raise ValueError("PyYAML is not installed; manifest.yaml cannot be read")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read manifest: {exc}") from exc
    try:
        data = _yaml.safe_load(text)
    except Exception as exc:  # yaml.YAMLError subclasses vary by version
        raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("manifest.yaml must contain a mapping")
    return data


def discover_skills(root: Path | None = None) -> DiscoveryResult:
    """Scan Builtin/ and Installed/ and return parsed packages plus errors."""
    base = Path(root) if root is not None else skills_root()
    result = DiscoveryResult()

    for folder, source in _SOURCE_DIRS:
        source_dir = base / folder
        if not source_dir.exists():
            continue
        if not _safe_dir(source_dir):
            result.errors.append(DiscoveryError(
                path=str(source_dir), source=source,
                message=f"{folder}/ is not a readable directory",
                code="source_unavailable"))
            continue

        try:
            entries = sorted(source_dir.iterdir(), key=lambda p: p.name.casefold())
        except OSError as exc:
            result.errors.append(DiscoveryError(
                path=str(source_dir), source=source,
                message=f"cannot scan {folder}/: {exc}",
                code="source_unavailable"))
            continue

        for package_path in entries:
            if not _safe_dir(package_path):
                # Files such as .gitkeep are not packages. A symlinked package
                # is refused so a package can never escape the Skills root.
                if package_path.is_symlink() and package_path.is_dir():
                    result.errors.append(DiscoveryError(
                        path=str(package_path), source=source,
                        message="Skill packages may not be symbolic links",
                        code="unsafe_package"))
                continue

            manifest_path = package_path / "manifest.yaml"
            if not manifest_path.is_file():
                result.errors.append(DiscoveryError(
                    path=str(package_path), source=source,
                    message="package is missing manifest.yaml",
                    code="missing_manifest"))
                continue

            try:
                manifest = _read_manifest(manifest_path)
            except ValueError as exc:
                result.errors.append(DiscoveryError(
                    path=str(manifest_path), source=source,
                    message=str(exc), code="invalid_manifest"))
                continue

            result.packages.append(DiscoveredSkill(
                path=package_path,
                source=source,
                manifest_path=manifest_path,
                manifest=manifest,
            ))

    return result

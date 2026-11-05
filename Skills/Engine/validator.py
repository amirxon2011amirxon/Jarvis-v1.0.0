"""Manifest and package validation for the Skills Engine.

Validation is intentionally strict about identity and paths and permissive
about extra manifest metadata. A malformed package produces a validation
result; it never raises through discovery and never prevents other Skills from
loading.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .loader import (
    is_text_file,
    normalise_relative_path,
    resolve_inside,
    safe_declared_path,
)
from .models import (
    DiscoveredSkill,
    SkillMetadata,
    ValidationResult,
)

_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_MAX_NAME = 80
_MAX_DESCRIPTION = 2000
_MAX_TAGS = 20
_MAX_ASSET_DECLARATIONS = 100


def _text(
    manifest: Dict[str, Any],
    key: str,
    errors: List[str],
    warnings: List[str],
    *,
    required: bool = False,
    default: str = "",
    limit: int = 200,
) -> str:
    value = manifest.get(key)
    if value is None:
        if required:
            errors.append(f"manifest field '{key}' is required")
        return default
    if not isinstance(value, str):
        errors.append(f"manifest field '{key}' must be a string")
        return default
    cleaned = " ".join(value.split()).strip() if key == "description" else value.strip()
    if required and not cleaned:
        errors.append(f"manifest field '{key}' cannot be blank")
    if len(cleaned) > limit:
        errors.append(
            f"manifest field '{key}' is longer than {limit} characters")
        cleaned = cleaned[:limit]
    return cleaned


def _metadata_section(
    manifest: Dict[str, Any], errors: List[str]
) -> Dict[str, Any]:
    metadata = manifest.get("metadata")
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        errors.append("manifest field 'metadata' must be a mapping")
        return {}
    return metadata


def _string_list(
    value: Any,
    field: str,
    errors: List[str],
    *,
    limit: int = _MAX_TAGS,
    item_limit: int = 40,
) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        errors.append(f"manifest field '{field}' must be a list")
        return ()
    cleaned: List[str] = []
    for item in value[:limit]:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"manifest field '{field}' contains an invalid value")
            continue
        text = item.strip()
        if len(text) > item_limit:
            errors.append(
                f"manifest field '{field}' contains a value longer than "
                f"{item_limit} characters")
            text = text[:item_limit]
        if text not in cleaned:
            cleaned.append(text)
    if len(value) > limit:
        errors.append(
            f"manifest field '{field}' has more than {limit} entries")
    return tuple(cleaned)


def _validate_declared_assets(
    package_path: Path,
    values: Any,
    kind: str,
    errors: List[str],
    warnings: List[str],
) -> Tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list):
        errors.append(f"manifest field '{kind}' must be a list")
        return ()
    declared: List[str] = []
    for raw in values[:_MAX_ASSET_DECLARATIONS]:
        try:
            clean = safe_declared_path(raw, kind)
        except ValueError as exc:
            errors.append(f"manifest {kind} path {raw!r}: {exc}")
            continue
        try:
            target = resolve_inside(package_path, clean)
        except ValueError as exc:
            errors.append(f"manifest {kind} path {raw!r}: {exc}")
            continue
        if not target.exists():
            errors.append(f"manifest {kind} path {clean!r} does not exist")
            continue
        if target.is_dir():
            # A declared directory is valid: every file inside is discoverable
            # without being read.
            try:
                has_file = any(path.is_file() for path in target.rglob("*"))
            except OSError:
                has_file = False
            if not has_file:
                warnings.append(f"manifest {kind} directory {clean!r} is empty")
        elif not target.is_file():
            errors.append(f"manifest {kind} path {clean!r} is not a file")
            continue
        if clean not in declared:
            declared.append(clean)
    if len(values) > _MAX_ASSET_DECLARATIONS:
        errors.append(
            f"manifest field '{kind}' has more than "
            f"{_MAX_ASSET_DECLARATIONS} entries")
    return tuple(declared)


def validate_discovered(discovered: DiscoveredSkill) -> Tuple[
        Optional[SkillMetadata], ValidationResult]:
    """Validate one parsed manifest and build its metadata."""
    errors: List[str] = []
    warnings: List[str] = []
    manifest = discovered.manifest
    package_path = discovered.path

    if not isinstance(manifest, dict):
        return None, ValidationResult.failed(
            "manifest.yaml must contain a mapping")

    raw_id = _text(
        manifest, "id", errors, warnings, required=True, limit=64)
    skill_id = raw_id
    if skill_id and skill_id != skill_id.lower():
        errors.append("manifest field 'id' must be lower-case")
    if skill_id and not _ID_RE.fullmatch(skill_id):
        errors.append(
            "manifest field 'id' must be lower-case kebab-case "
            "(for example 'frontend-design')")
    if skill_id and len(skill_id) > 64:
        errors.append("manifest field 'id' is longer than 64 characters")

    name = _text(manifest, "name", errors, warnings, required=True,
                 limit=_MAX_NAME)
    description = _text(
        manifest, "description", errors, warnings, required=True,
        limit=_MAX_DESCRIPTION)
    version = _text(
        manifest, "version", errors, warnings, required=True, limit=32)
    if version and not _VERSION_RE.fullmatch(version):
        errors.append(
            "manifest field 'version' must be semantic versioning "
            "(for example '1.0.0')")

    metadata = _metadata_section(manifest, errors)
    category = ""
    raw_category = metadata.get("category", manifest.get("category"))
    if raw_category is None:
        category = "general"
    elif isinstance(raw_category, str) and raw_category.strip():
        category = raw_category.strip()
        if len(category) > 40:
            errors.append("manifest category is longer than 40 characters")
            category = category[:40]
    else:
        errors.append("manifest category must be a non-empty string")
        category = "general"

    raw_tags = metadata.get("tags", manifest.get("tags"))
    tags = _string_list(raw_tags, "metadata.tags", errors)

    author = _text(manifest, "author", errors, warnings, limit=200)
    license_name = _text(manifest, "license", errors, warnings, limit=200)

    requirements: Tuple[str, ...] = ()
    raw_requirements = manifest.get("requirements")
    if raw_requirements is not None:
        if not isinstance(raw_requirements, dict):
            errors.append("manifest field 'requirements' must be a mapping")
        else:
            requirements = _string_list(
                raw_requirements.get("tools"),
                "requirements.tools",
                errors,
                limit=32,
                item_limit=80,
            )

    entrypoint_raw = manifest.get("entrypoint", "SKILL.md")
    entrypoint = ""
    if not isinstance(entrypoint_raw, str) or not entrypoint_raw.strip():
        errors.append("manifest field 'entrypoint' must be a non-empty string")
    else:
        try:
            entrypoint = normalise_relative_path(entrypoint_raw)
        except ValueError as exc:
            errors.append(f"manifest entrypoint: {exc}")
    if entrypoint:
        try:
            entry_path = resolve_inside(package_path, entrypoint)
        except ValueError as exc:
            errors.append(f"manifest entrypoint: {exc}")
        else:
            if not entry_path.is_file():
                errors.append(
                    f"manifest entrypoint {entrypoint!r} does not exist")
            elif not is_text_file(entry_path):
                errors.append(
                    f"manifest entrypoint {entrypoint!r} is not a text document")

    # The open Agent Skills package contract requires SKILL.md. The entrypoint
    # may point at another Markdown document, but the canonical file must
    # exist so a package remains understandable and portable.
    skill_md = package_path / "SKILL.md"
    if not skill_md.is_file():
        errors.append("package is missing SKILL.md")

    references = _validate_declared_assets(
        package_path, manifest.get("references"), "references",
        errors, warnings)
    resources = _validate_declared_assets(
        package_path, manifest.get("resources"), "resources",
        errors, warnings)

    if skill_id and discovered.path.name != skill_id:
        warnings.append(
            f"package folder {discovered.path.name!r} does not match the "
            f"Skill id {skill_id!r}")

    if errors:
        return None, ValidationResult(errors=tuple(errors),
                                      warnings=tuple(warnings))

    metadata_model = SkillMetadata(
        id=skill_id,
        name=name,
        description=description,
        version=version,
        author=author,
        license=license_name,
        category=category,
        tags=tags,
        requirements=requirements,
        entrypoint=entrypoint or "SKILL.md",
        references=references,
        resources=resources,
    )
    return metadata_model, ValidationResult(warnings=tuple(warnings))


def validate_manifest_dict(
    manifest: Dict[str, Any],
    package_path: Path,
    source: str = "installed",
) -> Tuple[Optional[SkillMetadata], ValidationResult]:
    """Convenience entry point used by tests and future installers."""
    discovered = DiscoveredSkill(
        path=Path(package_path),
        source=source,
        manifest_path=Path(package_path) / "manifest.yaml",
        manifest=manifest,
    )
    return validate_discovered(discovered)

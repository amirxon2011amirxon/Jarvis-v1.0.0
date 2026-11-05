"""Shared data structures for the Jarvis Skills Engine.

These models deliberately contain no I/O. Discovery produces
``DiscoveredSkill`` objects, validation converts them into ``SkillPackage``
objects, and the registry stores ``SkillRecord`` entries. Keeping them small
and explicit makes the filesystem the single source of truth: no dataclass in
this module is itself persisted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SKILL_SOURCES = ("builtin", "installed")


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of validating one manifest and package."""

    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }

    @classmethod
    def failed(cls, *errors: str) -> "ValidationResult":
        return cls(errors=tuple(str(error) for error in errors if error))


@dataclass(frozen=True)
class SkillMetadata:
    """Lightweight manifest metadata. No instruction text lives here."""

    id: str
    name: str
    description: str
    version: str
    author: str = ""
    license: str = ""
    category: str = "general"
    tags: Tuple[str, ...] = ()
    requirements: Tuple[str, ...] = ()
    entrypoint: str = "SKILL.md"
    references: Tuple[str, ...] = ()
    resources: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "license": self.license,
            "category": self.category,
            "tags": list(self.tags),
            "requirements": {"tools": list(self.requirements)},
            "entrypoint": self.entrypoint,
            "references": list(self.references),
            "resources": list(self.resources),
        }


@dataclass
class DiscoveredSkill:
    """A package found on disk whose manifest parsed as a mapping."""

    path: Path
    source: str
    manifest_path: Path
    manifest: Dict[str, Any]


@dataclass(frozen=True)
class DiscoveryError:
    """One malformed package. It never prevents other packages loading."""

    path: str
    source: str
    message: str
    code: str = "invalid_package"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "source": self.source,
            "message": self.message,
            "code": self.code,
        }


@dataclass
class DiscoveryResult:
    packages: List[DiscoveredSkill] = field(default_factory=list)
    errors: List[DiscoveryError] = field(default_factory=list)


@dataclass
class SkillPackage:
    """A validated package rooted in the filesystem."""

    metadata: SkillMetadata
    path: Path
    manifest_path: Path
    source: str
    manifest: Dict[str, Any]
    validation: ValidationResult

    @property
    def id(self) -> str:
        return self.metadata.id


@dataclass
class SkillRecord:
    """One runtime registry entry."""

    package: SkillPackage
    enabled: bool = True
    active: bool = False
    activated_at: Optional[str] = None
    registered_at: str = field(default_factory=_iso_now)

    @property
    def id(self) -> str:
        return self.package.id

    @property
    def name(self) -> str:
        return self.package.metadata.name

    def base_dict(self) -> Dict[str, Any]:
        meta = self.package.metadata
        return {
            "id": meta.id,
            "name": meta.name,
            "description": meta.description,
            "version": meta.version,
            "author": meta.author,
            "license": meta.license,
            "category": meta.category,
            "tags": list(meta.tags),
            "source": self.package.source,
            "enabled": bool(self.enabled),
            "available": bool(self.package.validation.valid),
            "active": bool(self.active),
            "activated_at": self.activated_at,
            "entrypoint": meta.entrypoint,
            "requirements": {"tools": list(meta.requirements)},
            "references": list(meta.references),
            "resources": list(meta.resources),
            "validation": self.package.validation.to_dict(),
            "registered_at": self.registered_at,
        }


@dataclass(frozen=True)
class ActiveSkill:
    """An activation scoped to one conversation/task."""

    skill_id: str
    name: str
    version: str
    source: str
    scope_id: str
    activated_at: str
    instructions: str
    references: Tuple[str, ...] = ()
    resources: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.skill_id,
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "scope_id": self.scope_id,
            "activated_at": self.activated_at,
            "state": "active",
            "instruction_chars": len(self.instructions),
            "references": list(self.references),
            "resources": list(self.resources),
        }


@dataclass
class ActivationResult:
    ok: bool = False
    skill_id: str = ""
    name: str = ""
    version: str = ""
    source: str = ""
    already_active: bool = False
    activated_at: str = ""
    instructions: str = ""
    references: Tuple[str, ...] = ()
    resources: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    error: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": self.ok,
            "skill_id": self.skill_id,
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "already_active": self.already_active,
            "activated_at": self.activated_at,
            "references": list(self.references),
            "resources": list(self.resources),
            "warnings": list(self.warnings),
            "note": self.note,
        }
        if self.error:
            payload["error"] = self.error
        if self.instructions:
            payload["instructions"] = self.instructions
        return payload

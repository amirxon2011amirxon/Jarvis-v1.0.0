"""Jarvis Skills Engine v1.0.0.

Public entry points::

    from Skills.Engine import get_engine

    engine = get_engine()
    engine.list_for_llm()          # Level 1 metadata
    engine.activate("code-review") # Level 2 instructions
    engine.read_reference(...)     # Level 3 resource
"""
from .engine import ENGINE_VERSION, SkillsEngine, get_engine, reset_engine
from .models import (
    ActivationResult,
    ActiveSkill,
    DiscoveryError,
    SkillMetadata,
    SkillPackage,
    SkillRecord,
    ValidationResult,
)
from .registry import SkillRegistry

__all__ = [
    "ENGINE_VERSION",
    "ActivationResult",
    "ActiveSkill",
    "DiscoveryError",
    "SkillMetadata",
    "SkillPackage",
    "SkillRecord",
    "SkillRegistry",
    "SkillsEngine",
    "ValidationResult",
    "get_engine",
    "reset_engine",
]

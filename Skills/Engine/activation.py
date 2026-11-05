"""Activation policy for the Skills Engine.

Activation never guesses. The model asks for one specific Skill id; this module
validates that request, loads the instruction document once, records it for the
current scope and returns the text to the tool result (which the Live model
reads as context).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from .context_manager import SkillContextManager
from .loader import SkillLoadError, SkillLoader
from .models import ActiveSkill, ActivationResult, SkillRecord
from .registry import SkillRegistry

ToolAvailability = Callable[[str], Optional[bool]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ActivationManager:
    def __init__(
        self,
        registry: SkillRegistry,
        loader: SkillLoader,
        context: SkillContextManager,
        tool_availability: Optional[ToolAvailability] = None,
    ) -> None:
        self.registry = registry
        self.loader = loader
        self.context = context
        self.tool_availability = tool_availability

    def _missing_tools(self, record: SkillRecord) -> Tuple[str, ...]:
        if self.tool_availability is None:
            return ()
        missing = []
        for name in record.package.metadata.requirements:
            try:
                available = self.tool_availability(name)
            except Exception:
                available = None
            if available is False:
                missing.append(name)
        return tuple(missing)

    def activate(self, skill_id: str, scope_id: str) -> ActivationResult:
        wanted = str(skill_id or "").strip()
        if not wanted:
            return ActivationResult(
                error="A Skill id is required. Call list_skills to see ids.")

        record = self.registry.get(wanted)
        if record is None:
            return ActivationResult(
                error=(
                    f"No Skill named '{wanted}' is installed. Call list_skills "
                    "to inspect the available ids."))

        if not record.package.validation.valid:
            return ActivationResult(
                skill_id=record.id,
                name=record.name,
                error=(
                    f"Skill '{record.id}' did not pass validation and cannot "
                    "be activated."))

        if not record.enabled:
            return ActivationResult(
                skill_id=record.id,
                name=record.name,
                error=(
                    f"Skill '{record.id}' is disabled. Enable it in the Skills "
                    "panel before activating it."))

        if self.context.is_active(scope_id, record.id):
            existing = self.context.get(scope_id, record.id)
            return ActivationResult(
                ok=True,
                skill_id=record.id,
                name=record.name,
                version=record.package.metadata.version,
                source=record.package.source,
                already_active=True,
                activated_at=existing.activated_at if existing else "",
                instructions="",
                note=(
                    "This Skill is already active for the current conversation. "
                    "Its instructions are already in context; no duplicate copy "
                    "was injected."),
            )

        try:
            instructions = self.loader.load_entrypoint(record.package)
        except (SkillLoadError, ValueError, OSError) as exc:
            return ActivationResult(
                skill_id=record.id,
                name=record.name,
                error=f"Could not load SKILL.md: {exc}")

        if not instructions.strip():
            return ActivationResult(
                skill_id=record.id,
                name=record.name,
                error="SKILL.md is empty; there are no instructions to apply.")

        assets = self.loader.list_assets(record.package)
        references = tuple(
            item["path"] for item in assets if item["kind"] == "references")
        resources = tuple(
            item["path"] for item in assets if item["kind"] == "resources")

        activated_at = _now_iso()
        active = ActiveSkill(
            skill_id=record.id,
            name=record.name,
            version=record.package.metadata.version,
            source=record.package.source,
            scope_id=scope_id,
            activated_at=activated_at,
            instructions=instructions,
            references=references,
            resources=resources,
        )
        if not self.context.activate(active):
            # Another thread activated it between the check and this point.
            return ActivationResult(
                ok=True,
                skill_id=record.id,
                name=record.name,
                version=record.package.metadata.version,
                source=record.package.source,
                already_active=True,
                activated_at=activated_at,
                instructions="",
                note="This Skill was already active; no duplicate was added.",
            )

        self.registry.set_active(record.id, True, activated_at)
        warnings = self._missing_tools(record)
        warning_list = tuple(
            f"Required tool '{name}' is not currently available; use an "
            "available equivalent or tell the user what is missing."
            for name in warnings
        )
        note = (
            "The Skill instructions are now active for this conversation. "
            "Apply them to the current task and use read_skill_reference when "
            "you need one of the listed supporting documents.")
        if warnings:
            note += " Some declared tools are unavailable; follow the warnings."
        return ActivationResult(
            ok=True,
            skill_id=record.id,
            name=record.name,
            version=record.package.metadata.version,
            source=record.package.source,
            already_active=False,
            activated_at=activated_at,
            instructions=instructions,
            references=references,
            resources=resources,
            warnings=warning_list,
            note=note,
        )

"""Task/conversation-scoped Skill instruction context.

This is not a second conversation system. It stores only which Skills have
been activated for the current scope and the instruction text they contributed,
so Jarvis can restore them on a fresh Live connection and clean them up when the
conversation boundary changes.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional

from .models import ActiveSkill


def normalise_scope(scope_id: object) -> str:
    text = str(scope_id or "").strip()
    return text or "default"


class SkillContextManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._scopes: Dict[str, Dict[str, ActiveSkill]] = {}

    # ── activation state ─────────────────────────────────────────────────────

    def activate(self, active: ActiveSkill) -> bool:
        """Register one active Skill. False if it was already active."""
        scope = normalise_scope(active.scope_id)
        with self._lock:
            skills = self._scopes.setdefault(scope, {})
            if active.skill_id in skills:
                return False
            skills[active.skill_id] = active
            return True

    def is_active(self, scope_id: object, skill_id: str) -> bool:
        scope = normalise_scope(scope_id)
        with self._lock:
            return skill_id in self._scopes.get(scope, {})

    def get(self, scope_id: object, skill_id: str) -> Optional[ActiveSkill]:
        scope = normalise_scope(scope_id)
        with self._lock:
            return self._scopes.get(scope, {}).get(skill_id)

    def get_any(self, skill_id: str) -> Optional[ActiveSkill]:
        """Return the first active instance, across all scopes."""
        with self._lock:
            for skills in self._scopes.values():
                active = skills.get(skill_id)
                if active is not None:
                    return active
            return None

    def list_active(self, scope_id: object) -> List[ActiveSkill]:
        scope = normalise_scope(scope_id)
        with self._lock:
            return list(self._scopes.get(scope, {}).values())

    def active_skill_ids(self) -> List[str]:
        with self._lock:
            ids: set[str] = set()
            for skills in self._scopes.values():
                ids.update(skills)
            return sorted(ids)

    def active_count(self, scope_id: Optional[object] = None) -> int:
        with self._lock:
            if scope_id is None:
                return sum(len(skills) for skills in self._scopes.values())
            return len(self._scopes.get(normalise_scope(scope_id), {}))

    def remove(self, scope_id: object, skill_id: str) -> bool:
        scope = normalise_scope(scope_id)
        with self._lock:
            skills = self._scopes.get(scope)
            if not skills or skill_id not in skills:
                return False
            skills.pop(skill_id, None)
            if not skills:
                self._scopes.pop(scope, None)
            return True

    def remove_everywhere(self, skill_id: str) -> bool:
        removed = False
        with self._lock:
            for scope in list(self._scopes):
                if self._scopes[scope].pop(skill_id, None) is not None:
                    removed = True
                if not self._scopes[scope]:
                    self._scopes.pop(scope, None)
        return removed

    def clear_scope(self, scope_id: object) -> List[str]:
        scope = normalise_scope(scope_id)
        with self._lock:
            skills = self._scopes.pop(scope, {})
            return list(skills)

    def clear_all(self) -> List[str]:
        with self._lock:
            ids: set[str] = set()
            for skills in self._scopes.values():
                ids.update(skills)
            self._scopes.clear()
            return sorted(ids)

    def retain(self, predicate: Callable[[str], bool]) -> List[str]:
        """Drop active Skills that no longer satisfy ``predicate``."""
        dropped: List[str] = []
        with self._lock:
            for scope in list(self._scopes):
                for skill_id in list(self._scopes[scope]):
                    if not predicate(skill_id):
                        self._scopes[scope].pop(skill_id, None)
                        dropped.append(skill_id)
                if not self._scopes[scope]:
                    self._scopes.pop(scope, None)
        return dropped

    # ── prompt rendering ─────────────────────────────────────────────────────

    def render_context(
        self,
        scope_id: object,
        *,
        include_instructions: bool = True,
    ) -> str:
        """Render active guidance for a new Live connection.

        With ``include_instructions=False`` this returns only a compact index.
        A resumed connection already contains the activation tool results, so
        re-sending full instructions would duplicate context for no benefit.
        """
        active = self.list_active(scope_id)
        if not active:
            return ""
        if not include_instructions:
            names = ", ".join(
                f"{item.name} ({item.skill_id} v{item.version})"
                for item in active)
            return (
                "# ACTIVE SKILLS\n\n"
                f"Active Skills for this conversation: {names}. Their full "
                "instructions are already present in the conversation from "
                "activation. Use them where they materially improve the "
                "current task; do not apply them to unrelated requests."
            )

        blocks = [
            "# ACTIVE SKILL INSTRUCTIONS",
            "",
            "The Skills below were explicitly activated for this conversation.",
            "Their guidance is reference material for the current task. It is",
            "subordinate to Jarvis's core system instructions, safety rules and",
            "the user's current request. Apply it where it improves the work,",
            "and ignore it for unrelated requests.",
        ]
        for item in active:
            blocks.extend([
                "",
                f"## {item.name} ({item.skill_id}) v{item.version}",
                f"Source: {item.source}",
                "",
                item.instructions.strip(),
            ])
        return "\n".join(blocks).strip()

    def scope_summary(self, scope_id: object) -> List[dict]:
        return [item.to_dict() for item in self.list_active(scope_id)]

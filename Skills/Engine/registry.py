"""In-memory registry with one small persistent state file.

Skill definitions themselves are never stored here: they are rebuilt from the
filesystem on every discovery. The only persisted registry state is the set of
disabled Skill ids, kept in ``Skills/.state.json`` so a user's enable/disable
choice survives restarts without touching the package. Active state is runtime
only and reset with the process.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from .models import DiscoveryError, SkillRecord


class SkillRegistry:
    def __init__(self, state_path: Path) -> None:
        self.state_path = Path(state_path)
        self._lock = threading.RLock()
        self._records: Dict[str, SkillRecord] = {}
        self._errors: List[DiscoveryError] = []
        self._disabled: set[str] = set()
        self._load_state()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load_state(self) -> None:
        path = self.state_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        disabled = data.get("disabled")
        if isinstance(disabled, list):
            self._disabled = {
                str(item).strip() for item in disabled
                if isinstance(item, str) and item.strip()
            }

    def _write_state_locked(self) -> None:
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "disabled": sorted(self._disabled),
        }
        fd, temp_name = tempfile.mkstemp(
            prefix=".skills-state-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    # ── rebuild / lookup ─────────────────────────────────────────────────────

    def rebuild(
        self,
        records: Iterable[SkillRecord],
        errors: Iterable[DiscoveryError] = (),
    ) -> None:
        with self._lock:
            next_records: Dict[str, SkillRecord] = {}
            for record in records:
                record.enabled = record.id not in self._disabled
                next_records[record.id] = record
            self._records = next_records
            self._errors = list(errors)

    def get(self, skill_id: str) -> Optional[SkillRecord]:
        with self._lock:
            return self._records.get(str(skill_id or "").strip())

    def all(self) -> List[SkillRecord]:
        with self._lock:
            return list(self._records.values())

    def ids(self) -> List[str]:
        with self._lock:
            return list(self._records)

    def errors(self) -> List[DiscoveryError]:
        with self._lock:
            return list(self._errors)

    def is_enabled(self, skill_id: str) -> bool:
        with self._lock:
            return skill_id in self._records and skill_id not in self._disabled

    def disabled_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._disabled)

    # ── mutations ────────────────────────────────────────────────────────────

    def set_enabled(self, skill_id: str, enabled: bool) -> SkillRecord:
        with self._lock:
            record = self._records.get(skill_id)
            if record is None:
                raise KeyError(skill_id)
            was_disabled = skill_id in self._disabled
            if enabled:
                self._disabled.discard(skill_id)
            else:
                self._disabled.add(skill_id)
            try:
                self._write_state_locked()
            except Exception:
                if was_disabled:
                    self._disabled.add(skill_id)
                else:
                    self._disabled.discard(skill_id)
                raise
            record.enabled = bool(enabled)
            if not enabled:
                record.active = False
                record.activated_at = None
            return record

    def remove_disabled(self, skill_id: str) -> None:
        with self._lock:
            if skill_id in self._disabled:
                self._disabled.discard(skill_id)
                self._write_state_locked()

    def set_active(
        self,
        skill_id: str,
        active: bool,
        activated_at: Optional[str] = None,
    ) -> None:
        with self._lock:
            record = self._records.get(skill_id)
            if record is None:
                return
            record.active = bool(active)
            record.activated_at = (
                activated_at if active else None)

    def mark_all_inactive(self) -> None:
        with self._lock:
            for record in self._records.values():
                record.active = False
                record.activated_at = None

    def counts(self) -> Dict[str, int]:
        with self._lock:
            records = list(self._records.values())
            return {
                "total": len(records),
                "enabled": sum(1 for r in records if r.enabled),
                "active": sum(1 for r in records if r.active),
                "builtin": sum(1 for r in records
                                if r.package.source == "builtin"),
                "installed": sum(1 for r in records
                                  if r.package.source == "installed"),
                "disabled": sum(1 for r in records if not r.enabled),
                "issues": len(self._errors),
            }

    def state_snapshot(self) -> Mapping[str, object]:
        with self._lock:
            return {"disabled": sorted(self._disabled)}

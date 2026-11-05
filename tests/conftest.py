"""Test isolation for the long-term memory database and Skills state.

The environment overrides are read before the application modules import, so no
test can create or modify ``config/memory.db`` or ``Skills/.state.json`` in the
repository.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_ROOT = tempfile.mkdtemp(prefix="jarvis-memory-tests-")
os.environ.setdefault("JARVIS_MEMORY_DB", os.path.join(_ROOT, "memory.db"))
os.environ.setdefault(
    "JARVIS_MEMORY_BACKUPS", os.path.join(_ROOT, "backups"))
os.environ.setdefault(
    "JARVIS_REMINDERS_DB", os.path.join(_ROOT, "reminders.db"))
os.environ.setdefault("JARVIS_DESKTOP_NOTIFICATIONS", "0")
# Never let the test process load the developer's real .env (and therefore make
# real SIP registrations when a runtime is constructed).
_EMPTY_ENV = os.path.join(_ROOT, "empty.env")
with open(_EMPTY_ENV, "w", encoding="utf-8"):
    pass
os.environ.setdefault("JARVIS_ENV_FILE", _EMPTY_ENV)
# Enable/disable state must never be written into the repository by tests.
os.environ.setdefault(
    "JARVIS_SKILLS_STATE", os.path.join(_ROOT, "skills-state.json"))


def _cleanup() -> None:
    shutil.rmtree(_ROOT, ignore_errors=True)


atexit.register(_cleanup)

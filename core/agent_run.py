"""Lightweight objective-scoped run tracking for Jarvis.

This module is deliberately not an agent planner or a second execution loop.
The Gemini Live session in ``main.py`` remains the model and the tool transport;
this component records the state of a task that Gemini explicitly starts with
the ``task_status`` tool.  That gives the existing session, tool dispatcher and
WebSocket UI one shared run identity without changing how conversations work.

WHY A SEPARATE RUN OBJECT
    The Live session already keeps the conversation, but its context can be
    compressed or lost on reconnect.  A small, explicit record of the user's
    objective, its progress and its terminal state survives those changes and
    can be shown to the frontend.  Ordinary conversation never creates a run:
    only the model's ``task_status`` call does.

LIMITS
    The four limits are intentionally few.  They are enforced by ``AgentRun``
    methods so the Live integration, the tool and tests share one rule set.
    Environment overrides exist for operators; no settings-file surface is
    added in this foundational version.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# ── statuses ─────────────────────────────────────────────────────────────────
IN_PROGRESS = "in_progress"
NEEDS_USER = "needs_user"
COMPLETED = "completed"
BLOCKED = "blocked"
FAILED = "failed"
CANCELLED = "cancelled"

RUN_STATUSES = frozenset({
    IN_PROGRESS, NEEDS_USER, COMPLETED, BLOCKED, FAILED, CANCELLED,
})
TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, CANCELLED})
ACTIVE_STATUSES = frozenset({IN_PROGRESS, NEEDS_USER, BLOCKED})

_MAX_OBJECTIVE_CHARS = 2000
_MAX_STEP_CHARS = 300
_MAX_SUMMARY_CHARS = 500
_MAX_ARTIFACTS = 20
_MAX_STEPS_HISTORY = 12
_MAX_OUTCOMES = 20
_MAX_FAILURES = 10
_MAX_EVIDENCE = 12
_MAX_BLOCKERS = 5
_MAX_VERIFICATION_ATTEMPTS = 3

# These are intent hints, not a classifier for every sentence.  A simple action
# request (open/play/close) does not need separate verification; a request that
# asks for an outcome (read, analyse, research, write, check) does.
_VERIFICATION_WORDS = frozenset({
    "analyse", "analyze", "analysis", "assess", "audit", "build", "check",
    "compare", "compile", "confirm", "create", "debug", "determine",
    "diagnose", "draft", "edit", "ensure", "evaluate", "explain", "extract",
    "find", "fix", "gather", "inspect", "investigate", "learn", "list",
    "monitor", "organize", "organise", "plan", "read", "report", "research",
    "review", "search", "study", "summarise", "summarize", "summary", "test",
    "track", "understand", "update", "validate", "verify", "write",
})
_ACTION_ONLY_WORDS = frozenset({
    "answer", "call", "close", "end", "hang", "launch", "mute", "open",
    "pause", "play", "reject", "resume", "set", "show", "start", "stop",
    "turn", "unmute",
})
_WRITE_WORDS = frozenset({
    "create", "edit", "update", "write", "modify", "save", "delete", "move",
    "copy", "rename", "patch", "fix", "build",
})
_RESEARCH_WORDS = frozenset({
    "compare", "research", "investigate", "fact", "landscape",
    "evidence", "sources", "study",
})
_LAUNCH_WORDS = frozenset({
    "open", "launch", "start", "show", "display",
})

_OBSERVATION_TOOLS = frozenset({
    "list_apps", "list_running_apps", "search_files", "web_fetch",
    "research", "get_call_status", "get_call_history", "memory_search",
    "memory_list", "memory_stats", "list_skills", "read_skill_reference",
})

# Exact repeats of these successful calls are treated as duplicate completed
# operations while an AgentRun is active.  Read-only observations stay
# repeatable because checking current state again is often legitimate.
_SIDE_EFFECT_TOOLS = frozenset({
    "open_app", "close_app", "open_file", "close_file", "open_folder",
    "open_containing_folder", "open_web", "close_web", "file_control",
    "system_control", "youtube_player", "phone_call", "call", "answer_call",
    "reject_call", "hangup", "mute_call", "unmute_call", "research",
    "reminders", "screen_understanding", "memory_remember",
    "memory_update", "memory_forget", "shutdown_jarvis",
})

_FAILURE_MARKERS = {
    "timeout": (
        "timeout", "timed out", "deadline", "took too long",
        "timed-out",
    ),
    "permission": (
        "permission", "denied", "not authorized", "forbidden",
        "access denied", "requires permission", "not allowed",
    ),
    "unsupported": (
        "unsupported", "not supported", "not implemented",
        "does not support", "cannot be used on this platform",
    ),
    "invalid": (
        "bad arguments", "invalid", "unknown argument",
        "unexpected keyword", "missing required", "required positional",
        "expected:", "could not parse", "no path", "empty", "malformed",
        "not a valid", "invalid url", "invalid operation",
    ),
    "missing": (
        "not found", "no such", "does not exist", "not installed",
        "no installed app", "no matching", "could not find",
        "not connected", "no active", "missing",
    ),
    "temporary": (
        "temporarily", "try again", "busy", "rate limit", "429", "503",
        "network", "connection", "overloaded", "service unavailable",
        "server error",
    ),
    "blocker": (
        "blocked", "blocker", "cannot continue", "requires user",
        "needs user", "user input",
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _tokens(text: Any) -> set[str]:
    return set(re.findall(r"[a-z]+", str(text or "").lower()))


def _has_token(text: Any, words: frozenset[str]) -> bool:
    return bool(_tokens(text) & words)


def _as_mapping(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    try:
        return dict(value)
    except Exception:
        return {}


def verification_policy(objective: str) -> tuple[bool, str]:
    """Whether an objective needs evidence beyond the tool's own return value."""
    text = str(objective or "")
    if _has_token(text, _VERIFICATION_WORDS):
        return True, (
            "the request asks for an outcome that should be supported by "
            "observation or explicit evidence")
    lowered = text.lower()
    if any(phrase in lowered for phrase in (
            "make sure", "double check", "double-check", "verify that")):
        return True, "the request explicitly asks for verification"
    if _has_token(text, _ACTION_ONLY_WORDS):
        return False, "a direct action request does not need extra verification"
    return False, "no outcome verification was requested"


def is_observation_tool(tool_name: str, args: Any = None,
                        result: Any = None) -> bool:
    """Whether a successful result is observational evidence, not just an action."""
    name = str(tool_name or "")
    if name in _OBSERVATION_TOOLS:
        return True
    data = _as_mapping(args)
    operation = str(data.get("operation") or data.get("action") or "").strip().lower()
    if name == "system_control":
        return operation.startswith(("get_", "diagnose_", "list_"))
    if name == "file_control":
        return operation in ("find", "list")
    if name == "youtube_player":
        return operation == "get_status"
    if name == "reminders":
        return operation in ("list_reminders", "get_reminder",
                             "list_notifications")
    if name == "screen_understanding":
        return operation in ("one_time", "get_observation_status")
    return False


def is_side_effect_tool(tool_name: str, args: Any = None) -> bool:
    """Whether repeating this exact successful call would repeat a side effect."""
    name = str(tool_name or "")
    if name not in _SIDE_EFFECT_TOOLS:
        return False
    data = _as_mapping(args)
    operation = str(data.get("operation") or data.get("action") or "").strip().lower()
    if name == "system_control":
        return not operation.startswith(("get_", "diagnose_", "list_"))
    if name == "file_control":
        return operation not in ("find", "list")
    if name == "youtube_player":
        return operation != "get_status"
    if name == "reminders":
        return operation not in (
            "list_reminders", "get_reminder", "list_notifications")
    if name == "screen_understanding":
        return operation != "get_observation_status"
    return True


def classify_failure(result: Any, tool_name: str = "") -> Dict[str, Any]:
    """Classify a failed tool result without assuming it is recoverable."""
    data = result if isinstance(result, dict) else {}
    error_value = data.get("error") or data.get("message") or \
        data.get("reason") or data.get("summary") or ""
    if isinstance(error_value, dict):
        error_value = error_value.get("message") or error_value.get("code") or ""
    error = _clip(error_value, _MAX_SUMMARY_CHARS)
    if data.get("approval_required"):
        category = "approval_required"
    elif data.get("confirmation_required"):
        category = "confirmation_required"
    elif data.get("clarification_required"):
        category = "clarification_required"
    else:
        haystack = " ".join(str(value) for value in (
            error, data.get("error_code", ""), data.get("status", ""),
            data.get("reason", ""),
        )).lower()
        category = "unknown"
        for candidate in (
                "timeout", "permission", "unsupported", "invalid",
                "missing", "temporary", "blocker"):
            if any(marker in haystack for marker in _FAILURE_MARKERS[candidate]):
                category = candidate
                break
    if category == "approval_required":
        hint = ("The user must approve this exact operation in the Jarvis "
                "interface before it can run.")
        recoverable, retry, requires_user = False, False, True
    elif category == "confirmation_required":
        hint = "Ask the user for confirmation, then repeat only with confirmation."
        recoverable, retry, requires_user = False, False, True
    elif category == "clarification_required":
        hint = "Ask the user which option they mean, then use the chosen value."
        recoverable, retry, requires_user = False, False, True
    elif category == "permission":
        hint = "Explain the permission requirement and ask the user to resolve it."
        recoverable, retry, requires_user = False, False, True
    elif category == "unsupported":
        hint = "Use another available approach or explain that this is unsupported."
        recoverable, retry, requires_user = False, False, False
    elif category == "timeout":
        hint = "The operation timed out; retry once only if justified, or use another approach."
        recoverable, retry, requires_user = True, True, False
    elif category == "temporary":
        hint = "This looks temporary; retry once only if justified, or use another approach."
        recoverable, retry, requires_user = True, True, False
    elif category == "invalid":
        hint = "Correct the arguments; do not repeat them unchanged."
        recoverable, retry, requires_user = True, False, False
    elif category == "missing":
        hint = "The resource is missing; find another source or ask the user."
        recoverable, retry, requires_user = True, False, False
    elif category == "blocker":
        hint = "The task is blocked; ask the user or report the blocker honestly."
        recoverable, retry, requires_user = False, False, True
    else:
        hint = "Do not assume this is recoverable; inspect it or ask the user."
        recoverable, retry, requires_user = False, False, False
    return {
        "tool": str(tool_name or ""),
        "category": category,
        "recoverable": recoverable,
        "retry_same_call": retry,
        "requires_user": requires_user,
        "error": error,
        "hint": hint,
    }


def call_signature(call: Any) -> str:
    name = str(getattr(call, "name", "") or "")
    args = getattr(call, "args", None)
    data = _as_mapping(args)
    try:
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        payload = str(data)
    # Hash the whole argument object. A truncated prefix made two different
    # long writes to the same path share a signature, so the second was
    # wrongly refused as a duplicate.
    digest = hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()
    return f"{name}\x00{digest}"


def _env_number(name: str, default, cast, low, high):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = cast(str(raw).strip())
    except (TypeError, ValueError):
        return default
    try:
        return max(low, min(high, value))
    except TypeError:
        return default


class AgentRunError(Exception):
    """Base error for run state and limit violations."""


class AgentRunLimitError(AgentRunError):
    """A configured execution limit was reached."""

    def __init__(self, limit: str, message: str) -> None:
        super().__init__(message)
        self.limit = limit


class AgentRunStateError(AgentRunError):
    """The requested transition is not valid for the run's current state."""


class AgentRunCancelled(AgentRunError):
    """The run was cancelled and cannot continue."""


class AgentRunVerificationError(AgentRunError):
    """Completion was requested without sufficient objective evidence."""

    def __init__(self, message: str, *, missing: Optional[list] = None,
                 attempts: int = 0, exhausted: bool = False) -> None:
        super().__init__(message)
        self.missing = list(missing or [])
        self.attempts = int(attempts)
        self.exhausted = bool(exhausted)

    def payload(self) -> Dict[str, Any]:
        return {
            "status": ("verification_exhausted" if self.exhausted
                       else "verification_required"),
            "missing_evidence": list(self.missing),
            "verification_attempts": self.attempts,
            "verification_exhausted": self.exhausted,
            "exhausted": self.exhausted,
            "message": str(self),
        }


# ── limits ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AgentRunLimits:
    """The few execution limits a run needs.

    Defaults are deliberately modest: a voice assistant task should normally
    finish in a handful of tool rounds, and a stuck model must not be allowed to
    loop indefinitely.  ``from_environment`` keeps the defaults configurable
    without adding a second settings surface.
    """

    max_steps: int = 12
    max_tool_calls: int = 24
    max_consecutive_failures: int = 3
    max_duration_seconds: float = 600.0

    def __post_init__(self) -> None:
        if int(self.max_steps) < 1:
            raise ValueError("max_steps must be at least 1")
        if int(self.max_tool_calls) < 1:
            raise ValueError("max_tool_calls must be at least 1")
        if int(self.max_consecutive_failures) < 1:
            raise ValueError("max_consecutive_failures must be at least 1")
        if float(self.max_duration_seconds) <= 0:
            raise ValueError("max_duration_seconds must be positive")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_steps": int(self.max_steps),
            "max_tool_calls": int(self.max_tool_calls),
            "max_consecutive_failures": int(self.max_consecutive_failures),
            "max_duration_seconds": float(self.max_duration_seconds),
        }

    @classmethod
    def from_environment(cls) -> "AgentRunLimits":
        return cls(
            max_steps=_env_number(
                "JARVIS_AGENT_MAX_STEPS", 12, int, 1, 100),
            max_tool_calls=_env_number(
                "JARVIS_AGENT_MAX_TOOL_CALLS", 24, int, 1, 500),
            max_consecutive_failures=_env_number(
                "JARVIS_AGENT_MAX_CONSECUTIVE_FAILURES", 3, int, 1, 20),
            max_duration_seconds=_env_number(
                "JARVIS_AGENT_MAX_DURATION_SECONDS", 600.0, float, 1.0,
                86_400.0),
        )


# ── run ──────────────────────────────────────────────────────────────────────
@dataclass
class AgentRun:
    """One objective-scoped run.

    All mutations are guarded by the instance lock because tools execute in
    worker threads while the Live session and the web UI read snapshots.
    """

    objective: str
    conversation_id: str = ""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = IN_PROGRESS
    started_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    step_count: int = 0
    tool_call_count: int = 0
    successful_tool_calls: int = 0
    failed_tool_calls: int = 0
    consecutive_failures: int = 0
    cancel_requested: bool = False
    last_tool_summary: str = ""
    current_step: str = ""
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    tool_outcomes: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    failed_attempts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    completed_attempts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    verification_required: bool = False
    verification_reason: str = ""
    verification_evidence: List[Dict[str, Any]] = field(default_factory=list)
    verification_attempts: int = 0
    verification_exhausted: bool = False
    verification_missing: List[str] = field(default_factory=list)
    remaining_work: str = ""
    result_summary: str = ""
    last_error: str = ""
    finished_at: str = ""
    limits: AgentRunLimits = field(
        default_factory=AgentRunLimits.from_environment)
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False)
    _started_monotonic: float = field(
        default_factory=time.monotonic, repr=False, compare=False)
    _finished_monotonic: float = field(
        default=0.0, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.objective = _clip(self.objective, _MAX_OBJECTIVE_CHARS)
        if not self.objective:
            raise ValueError("an AgentRun needs a non-empty objective")
        self.conversation_id = _clip(self.conversation_id, 120)
        self.run_id = _clip(self.run_id, 120) or uuid.uuid4().hex
        if self.status not in RUN_STATUSES:
            raise ValueError(f"unknown AgentRun status: {self.status!r}")
        self.started_at = self.started_at or _now_iso()
        self.updated_at = self.updated_at or self.started_at
        required, reason = verification_policy(self.objective)
        if required:
            self.verification_required = True
        if not self.verification_reason:
            self.verification_reason = reason
        if self.status in TERMINAL_STATUSES:
            self.finished_at = self.finished_at or self.updated_at
            self._finished_monotonic = time.monotonic()

    # ── snapshots ────────────────────────────────────────────────────────────

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return self.status in TERMINAL_STATUSES

    def _elapsed_locked(self) -> float:
        end = self._finished_monotonic or time.monotonic()
        return max(0.0, end - self._started_monotonic)

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-safe snapshot for events, the UI and tool results."""
        with self._lock:
            return {
                "run_id": self.run_id,
                "conversation_id": self.conversation_id,
                "objective": self.objective,
                "status": self.status,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "finished_at": self.finished_at,
                "step_count": int(self.step_count),
                "tool_call_count": int(self.tool_call_count),
                "successful_tool_calls": int(self.successful_tool_calls),
                "failed_tool_calls": int(self.failed_tool_calls),
                "consecutive_failures": int(self.consecutive_failures),
                "cancel_requested": bool(self.cancel_requested),
                "last_tool_summary": self.last_tool_summary,
                "current_step": self.current_step,
                "artifacts": [dict(item) for item in self.artifacts],
                "steps": [dict(item) for item in self.steps],
                "tool_outcomes": [dict(item) for item in self.tool_outcomes],
                "failures": [dict(item) for item in self.failures],
                "blockers": list(self.blockers),
                "failed_operations": [
                    {"tool": item.get("tool", ""),
                     "category": item.get("category", ""),
                     "recoverable": bool(item.get("recoverable")),
                     "retry_same_call": bool(item.get("retry_same_call")),
                     "count": int(item.get("count", 0)),
                     "last_error": item.get("last_error", "")}
                    for item in self.failed_attempts.values()
                ],
                "completed_operations": [
                    {"tool": item.get("tool", ""),
                     "count": int(item.get("count", 0))}
                    for item in self.completed_attempts.values()
                ],
                "remaining_work": self.remaining_work,
                "verification": {
                    "required": bool(self.verification_required),
                    "reason": self.verification_reason,
                    "evidence": [dict(item)
                                 for item in self.verification_evidence],
                    "attempts": int(self.verification_attempts),
                    "exhausted": bool(self.verification_exhausted),
                    "missing": list(self.verification_missing),
                },
                "result_summary": self.result_summary,
                "last_error": self.last_error,
                "elapsed_seconds": round(self._elapsed_locked(), 2),
                "limits": self.limits.to_dict(),
                "is_terminal": self.status in TERMINAL_STATUSES,
            }

    # ── internal helpers ─────────────────────────────────────────────────────

    def _touch_locked(self) -> None:
        self.updated_at = _now_iso()

    def _check_open_locked(self) -> None:
        if self.status in TERMINAL_STATUSES:
            raise AgentRunStateError(
                f"run is already {self.status} and cannot be changed")
        if self.cancel_requested:
            raise AgentRunCancelled("the run was cancelled")

    def _check_duration_locked(self) -> None:
        if self._elapsed_locked() <= float(self.limits.max_duration_seconds):
            return
        message = (
            f"Run duration limit reached "
            f"({float(self.limits.max_duration_seconds):.0f}s).")
        self._fail_locked(message)
        raise AgentRunLimitError("max_duration_seconds", message)

    def _fail_locked(self, reason: str) -> None:
        self.status = FAILED
        self.last_error = _clip(reason, _MAX_SUMMARY_CHARS)
        self.finished_at = _now_iso()
        self._finished_monotonic = time.monotonic()
        self._touch_locked()

    # ── transitions ──────────────────────────────────────────────────────────

    def set_status(self, status: str, *, current_step: Optional[str] = None,
                   summary: str = "", error: str = "") -> Dict[str, Any]:
        """Move to a new status, refusing invalid terminal transitions."""
        wanted = str(status or "").strip().lower()
        if wanted not in RUN_STATUSES:
            raise AgentRunStateError(f"unknown run status: {status!r}")
        with self._lock:
            if self.status in TERMINAL_STATUSES and wanted != self.status:
                raise AgentRunStateError(
                    f"run is already {self.status} and cannot become {wanted}")
            if self.cancel_requested and wanted != CANCELLED:
                raise AgentRunCancelled("the run was cancelled")
            if wanted == CANCELLED:
                self.cancel_requested = True
            if wanted in (NEEDS_USER, BLOCKED) and not self.verification_required:
                self.verification_required = True
                if not self.verification_reason:
                    self.verification_reason = (
                        "the task needed user input or was blocked")
            self.status = wanted
            if current_step is not None:
                self.current_step = _clip(current_step, _MAX_STEP_CHARS)
            if summary:
                self.result_summary = _clip(summary, _MAX_SUMMARY_CHARS)
            if error:
                self.last_error = _clip(error, _MAX_SUMMARY_CHARS)
            if wanted in TERMINAL_STATUSES:
                self.finished_at = _now_iso()
                self._finished_monotonic = time.monotonic()
            else:
                self.finished_at = ""
                self._finished_monotonic = 0.0
            self._touch_locked()
            return self.to_dict()

    def set_progress(self, *, step: Optional[str] = None,
                     summary: str = "",
                     last_tool_summary: str = "",
                     remaining: Optional[str] = None) -> Dict[str, Any]:
        """Update human-readable progress without changing status."""
        with self._lock:
            self._check_open_locked()
            self._check_duration_locked()
            if step is not None:
                self.current_step = _clip(step, _MAX_STEP_CHARS)
            if summary:
                self.result_summary = _clip(summary, _MAX_SUMMARY_CHARS)
            if last_tool_summary:
                self.last_tool_summary = _clip(
                    last_tool_summary, _MAX_SUMMARY_CHARS)
            if remaining is not None:
                self.remaining_work = _clip(remaining, _MAX_SUMMARY_CHARS)
            self._touch_locked()
            return self.to_dict()

    def record_step(self, step: str = "") -> Dict[str, Any]:
        """Count one model/tool round and enforce the step budget."""
        with self._lock:
            self._check_open_locked()
            self._check_duration_locked()
            if self.step_count >= int(self.limits.max_steps):
                message = (
                    f"Step limit reached ({int(self.limits.max_steps)} steps).")
                self._fail_locked(message)
                raise AgentRunLimitError("max_steps", message)
            self.step_count += 1
            label = _clip(step or f"Step {self.step_count}", _MAX_STEP_CHARS)
            if step:
                self.current_step = label
            self.steps.append({
                "step": label,
                "at": _now_iso(),
            })
            if len(self.steps) > _MAX_STEPS_HISTORY:
                del self.steps[: len(self.steps) - _MAX_STEPS_HISTORY]
            self._touch_locked()
            return self.to_dict()

    def ensure_tool_capacity(self, count: int = 1) -> None:
        """Reject a batch that would exceed the tool-call budget.

        This is checked before dispatch so a batch never starts after the run
        has already exhausted its allowance.  The actual count is still
        incremented per result in ``record_tool_call``.
        """
        pending = max(0, int(count or 0))
        if not pending:
            return
        with self._lock:
            self._check_open_locked()
            self._check_duration_locked()
            if (self.tool_call_count + pending
                    > int(self.limits.max_tool_calls)):
                message = (
                    f"Tool call limit reached "
                    f"({int(self.limits.max_tool_calls)} calls).")
                self._fail_locked(message)
                raise AgentRunLimitError("max_tool_calls", message)

    def record_tool_call(self, *, tool_name: str = "", ok: bool = True,
                         summary: str = "",
                         failure: Optional[Dict[str, Any]] = None,
                         signature: str = "",
                         side_effect: Optional[bool] = None) -> Dict[str, Any]:
        """Count one tool result and enforce call/failure budgets.

        ``failure`` is the classified failure produced by ``classify_failure``.
        The original error remains in ``summary``/``failure`` and is never
        replaced by the classification.
        """
        with self._lock:
            self._check_open_locked()
            self._check_duration_locked()
            if self.tool_call_count >= int(self.limits.max_tool_calls):
                message = (
                    f"Tool call limit reached "
                    f"({int(self.limits.max_tool_calls)} calls).")
                self._fail_locked(message)
                raise AgentRunLimitError("max_tool_calls", message)
            self.tool_call_count += 1
            if ok:
                self.successful_tool_calls += 1
            else:
                self.failed_tool_calls += 1
            label = _clip(tool_name, 80)
            detail = _clip(summary, _MAX_SUMMARY_CHARS)
            self.last_tool_summary = (
                f"{label}: {detail}" if label and detail else detail or label)
            analysis = failure if isinstance(failure, dict) else None
            if analysis is None and not ok:
                analysis = classify_failure({"error": detail}, label)
            category = str((analysis or {}).get("category") or "")
            self.tool_outcomes.append({
                "tool": label,
                "ok": bool(ok),
                "summary": detail,
                "category": category,
                "at": _now_iso(),
            })
            if len(self.tool_outcomes) > _MAX_OUTCOMES:
                del self.tool_outcomes[: len(self.tool_outcomes) - _MAX_OUTCOMES]
            if ok:
                self.consecutive_failures = 0
                self.last_error = ""
                # An approval/confirmation pause is resolved by the successful
                # retry; leaving the run in NEEDS_USER would keep it stuck and
                # block every later task in the conversation.
                if self.status == NEEDS_USER:
                    self.status = IN_PROGRESS
                if signature:
                    self.failed_attempts.pop(signature, None)
                repeatable_side_effect = (
                    bool(side_effect) if side_effect is not None
                    else label in _SIDE_EFFECT_TOOLS)
                if signature and repeatable_side_effect:
                    previous = self.completed_attempts.get(signature, {})
                    self.completed_attempts[signature] = {
                        "tool": label,
                        "count": int(previous.get("count", 0)) + 1,
                    }
                    while len(self.completed_attempts) > 30:
                        self.completed_attempts.pop(
                            next(iter(self.completed_attempts)), None)
                if label:
                    prefix = f"{label}:"
                    self.blockers = [
                        item for item in self.blockers
                        if not item.startswith(prefix)
                    ]
            else:
                if analysis is None:
                    analysis = classify_failure({"error": detail}, label)
                self.verification_required = True
                if not self.verification_reason:
                    self.verification_reason = (
                        "a tool failure means the outcome should be verified")
                self.last_error = _clip(
                    analysis.get("error") or detail
                    or (f"{label} failed" if label else "tool failed"),
                    _MAX_SUMMARY_CHARS)
                self.failures.append({
                    "tool": label,
                    "category": category,
                    "recoverable": bool(analysis.get("recoverable")),
                    "requires_user": bool(analysis.get("requires_user")),
                    "error": self.last_error,
                    "hint": _clip(analysis.get("hint"), _MAX_SUMMARY_CHARS),
                    "at": _now_iso(),
                })
                if len(self.failures) > _MAX_FAILURES:
                    del self.failures[: len(self.failures) - _MAX_FAILURES]
                if category in ("blocker", "permission", "unsupported",
                                "missing"):
                    blocker = f"{label}: {detail or category}"
                    if blocker not in self.blockers:
                        self.blockers.append(_clip(blocker, _MAX_SUMMARY_CHARS))
                        if len(self.blockers) > _MAX_BLOCKERS:
                            del self.blockers[: len(self.blockers) - _MAX_BLOCKERS]
                if signature:
                    previous = self.failed_attempts.get(signature, {})
                    count = int(previous.get("count", 0)) + 1
                    self.failed_attempts[signature] = {
                        "tool": label,
                        "category": category,
                        "recoverable": bool(analysis.get("recoverable")),
                        "retry_same_call": bool(analysis.get("retry_same_call")),
                        "count": count,
                        "last_error": self.last_error,
                    }
                    while len(self.failed_attempts) > 20:
                        self.failed_attempts.pop(
                            next(iter(self.failed_attempts)), None)
                self.consecutive_failures += 1
                if (self.consecutive_failures
                        >= int(self.limits.max_consecutive_failures)):
                    message = (
                        "Consecutive failure limit reached "
                        f"({int(self.limits.max_consecutive_failures)}).")
                    self._fail_locked(message)
                    raise AgentRunLimitError(
                        "max_consecutive_failures", message)
            self._touch_locked()
            return self.to_dict()

    def record_pending(self, *, tool_name: str = "", signature: str = "",
                       category: str = "confirmation_required",
                       summary: str = "") -> Dict[str, Any]:
        """Remember a call that needs the user's decision, without counting it
        as a tool failure."""
        with self._lock:
            self._check_open_locked()
            label = _clip(tool_name, 80)
            detail = _clip(summary, _MAX_SUMMARY_CHARS)
            if signature:
                self.failed_attempts[signature] = {
                    "tool": label,
                    "category": category,
                    "recoverable": True,
                    "retry_same_call": False,
                    "count": 1,
                    "last_error": detail or category,
                }
                while len(self.failed_attempts) > 20:
                    self.failed_attempts.pop(
                        next(iter(self.failed_attempts)), None)
            blocker = f"{label}: {detail or category}"
            if blocker not in self.blockers:
                self.blockers.append(_clip(blocker, _MAX_SUMMARY_CHARS))
                if len(self.blockers) > _MAX_BLOCKERS:
                    del self.blockers[: len(self.blockers) - _MAX_BLOCKERS]
            self._touch_locked()
            return self.to_dict()

    def add_evidence(self, summary: Any, *, source: str = "model",
                     tool: str = "", kind: str = "") -> Dict[str, Any]:
        """Attach compact evidence supporting the objective."""
        text = _clip(summary, _MAX_SUMMARY_CHARS)
        if not text:
            return self.to_dict()
        with self._lock:
            self._check_open_locked()
            evidence_kind = kind or ("observation" if source.startswith("observation")
                                     else "explicit")
            for item in self.verification_evidence:
                if (item.get("summary") == text
                        and item.get("tool") == str(tool or "")
                        and item.get("source") == str(source or "")):
                    return self.to_dict()
            self.verification_evidence.append({
                "summary": text,
                "source": _clip(source, 40),
                "tool": _clip(tool, 80),
                "kind": evidence_kind,
                "at": _now_iso(),
            })
            if len(self.verification_evidence) > _MAX_EVIDENCE:
                del self.verification_evidence[
                    : len(self.verification_evidence) - _MAX_EVIDENCE]
            self.verification_missing = []
            self._touch_locked()
            return self.to_dict()

    def can_retry_signature(self, signature: str) -> bool:
        with self._lock:
            record = self.failed_attempts.get(signature or "")
            if not record:
                return True
            if (record.get("category") in ("temporary", "timeout")
                    and int(record.get("count", 0)) < 2):
                return True
            return False

    def missing_verification(self) -> List[str]:
        """What evidence is still needed before completion, if anything."""
        with self._lock:
            if not self.verification_required:
                return []
            evidence = list(self.verification_evidence)
            explicit = [item for item in evidence
                        if item.get("kind") == "explicit"]
            observations = [item for item in evidence
                            if item.get("kind") == "observation"]
            tools = {str(item.get("tool") or "") for item in observations}
            tokens = _tokens(self.objective)
            if tokens & _WRITE_WORDS:
                return ([] if explicit else
                        ["explicit evidence that the written or edited result "
                         "is correct"])
            if tokens & _RESEARCH_WORDS:
                if explicit or "research" in tools:
                    return []
                return ["a completed multi-source research result or explicit "
                        "evidence supporting the conclusion"]
            if tokens & _LAUNCH_WORDS:
                if explicit or "list_running_apps" in tools:
                    return []
                return ["an observation that the application or service is "
                        "actually running"]
            if explicit or observations:
                return []
            return ["explicit evidence from the completed work"]

    def note_verification_attempt(self) -> int:
        with self._lock:
            self.verification_attempts += 1
            self._touch_locked()
            return self.verification_attempts

    def set_verification_missing(self, missing: list) -> None:
        with self._lock:
            self.verification_missing = [
                _clip(item, _MAX_SUMMARY_CHARS) for item in (missing or [])
            ][:5]
            self._touch_locked()

    def set_verification_exhausted(self) -> None:
        with self._lock:
            self.verification_exhausted = True
            self._touch_locked()

    def context_summary(self, limit: int = 1200) -> str:
        """A compact execution context for Gemini or the task-status result."""
        with self._lock:
            lines = [
                f"Objective: {self.objective}",
                f"Status: {self.status}",
                f"Current step: {self.current_step or '(none)'}",
            ]
            remaining = (self.remaining_work
                         or ("; ".join(self.verification_missing)
                             if self.verification_missing else ""))
            if remaining:
                lines.append("Remaining: " + remaining)
            if self.steps:
                lines.append("Completed steps (recent):")
                for item in self.steps[-3:]:
                    lines.append(f"- {item.get('step', '')}")
            if self.tool_outcomes:
                lines.append("Relevant tool outcomes:")
                for item in self.tool_outcomes[-5:]:
                    state = "ok" if item.get("ok") else "failed"
                    lines.append(
                        f"- {item.get('tool', 'tool')} [{state}]: "
                        f"{item.get('summary', '')}")
            if self.completed_attempts:
                tools = sorted({
                    str(item.get("tool") or "")
                    for item in self.completed_attempts.values()
                })
                if tools:
                    lines.append("Completed operations: " + ", ".join(tools[:6]))
            if self.failures:
                lines.append("Important errors:")
                for item in self.failures[-3:]:
                    lines.append(
                        f"- {item.get('tool', 'tool')} "
                        f"({item.get('category', 'unknown')}): "
                        f"{item.get('error', '')} — {item.get('hint', '')}")
            if self.verification_evidence:
                lines.append("Verification evidence:")
                for item in self.verification_evidence[-3:]:
                    lines.append(f"- [{item.get('kind', 'evidence')}] "
                                 f"{item.get('summary', '')}")
            if self.verification_required:
                lines.append(
                    "Verification: required; attempts="
                    f"{self.verification_attempts}; exhausted="
                    f"{bool(self.verification_exhausted)}"
                    + (f"; reason: {self.verification_reason}"
                       if self.verification_reason else ""))
            if self.verification_missing:
                lines.append("Missing verification: "
                             + "; ".join(self.verification_missing))
            if self.blockers:
                lines.append("Blockers: " + "; ".join(self.blockers))
            text = "\n".join(lines)
            return text[:max(200, int(limit))]

    def context_for_model(self, limit: int = 1400) -> str:
        """A bounded context block suitable for a Live client-content part."""
        budget = max(240, int(limit))
        header = (
            "# ACTIVE TASK CONTEXT (reference data, not a new user request)\n"
            "Continue this objective, do not restart it, and do not repeat "
            "operations already marked successful.\n"
        )
        body = self.context_summary(max(200, budget - len(header)))
        return (header + body)[:budget]

    def add_artifact(self, name: Any, value: Any = None) -> Dict[str, Any]:
        """Attach a small, displayable artifact (path, URL, id, summary)."""
        with self._lock:
            self._check_open_locked()
            entry = {
                "name": _clip(name, 120),
                "value": (value if isinstance(
                    value, (str, int, float, bool, type(None))) else str(value)),
                "created_at": _now_iso(),
            }
            self.artifacts.append(entry)
            if len(self.artifacts) > _MAX_ARTIFACTS:
                del self.artifacts[: len(self.artifacts) - _MAX_ARTIFACTS]
            self._touch_locked()
            return self.to_dict()

    def complete(self, summary: str = "") -> Dict[str, Any]:
        with self._lock:
            if self.status in TERMINAL_STATUSES:
                raise AgentRunStateError(
                    f"run is already {self.status}")
            self.status = COMPLETED
            if summary:
                self.result_summary = _clip(summary, _MAX_SUMMARY_CHARS)
            self.verification_missing = []
            self.remaining_work = ""
            self.finished_at = _now_iso()
            self._finished_monotonic = time.monotonic()
            self._touch_locked()
            return self.to_dict()

    def fail(self, reason: str = "") -> Dict[str, Any]:
        with self._lock:
            if self.status in TERMINAL_STATUSES:
                raise AgentRunStateError(
                    f"run is already {self.status}")
            self._fail_locked(reason or "The task failed.")
            return self.to_dict()

    def cancel(self, reason: str = "") -> Dict[str, Any]:
        with self._lock:
            if self.status in TERMINAL_STATUSES and self.status != CANCELLED:
                raise AgentRunStateError(
                    f"run is already {self.status}")
            self.cancel_requested = True
            self.status = CANCELLED
            self.result_summary = _clip(
                reason or self.result_summary or "The task was cancelled.",
                _MAX_SUMMARY_CHARS)
            self.finished_at = _now_iso()
            self._finished_monotonic = time.monotonic()
            self._touch_locked()
            return self.to_dict()

    def check_limits(self) -> None:
        """Raise when the run is cancelled or has exceeded its duration."""
        with self._lock:
            self._check_open_locked()
            self._check_duration_locked()


# ── process-wide service ─────────────────────────────────────────────────────
class AgentRunService:
    """The one active run for the one Jarvis conversation.

    The service is process-wide for the same reason the tool registry and the
    memory service are: Jarvis owns one Live session.  Tests can replace or
    reset it, and a future multi-run design can extend it without changing the
    tool contract.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._run: Optional[AgentRun] = None
        self._listener: Optional[Callable[[Dict[str, Any]], None]] = None
        self._conversation_id = ""

    # ── wiring ───────────────────────────────────────────────────────────────

    def set_listener(
            self, callback: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        with self._lock:
            self._listener = callback

    def set_conversation_id(self, conversation_id: Any) -> None:
        with self._lock:
            self._conversation_id = _clip(conversation_id, 120)

    def current(self) -> Optional[AgentRun]:
        with self._lock:
            return self._run

    def current_snapshot(self) -> Optional[Dict[str, Any]]:
        run = self.current()
        return run.to_dict() if run is not None else None

    def has_active_run(self) -> bool:
        run = self.current()
        return bool(run is not None and run.status in ACTIVE_STATUSES)

    def can_continue(self, run_id: Optional[str] = None) -> bool:
        """Whether a dispatch may start or continue for this run.

        Checks identity, terminal/cancelled state, and the duration budget.
        Callers use this as a cheap guard before each tool execution.
        """
        run = self._resolve(run_id, required=False)
        if run is None or run.status in TERMINAL_STATUSES:
            return False
        if run.cancel_requested:
            self._emit("cancelled", run)
            return False
        try:
            self.check_duration(run.run_id)
        except AgentRunError:
            return False
        return True

    def begin_execution(self, run_id: Optional[str] = None, *,
                        step: str = "", tool_call_count: int = 0) -> bool:
        """Admit one tool-call batch, enforcing all pre-execution limits.

        Returns False when the run is terminal, cancelled, past its duration,
        or the batch would exceed the step/tool-call budget.  The failure is
        emitted and recorded on the run; the caller must not dispatch.
        """
        run = self._resolve(run_id, required=False)
        if run is None or run.status in TERMINAL_STATUSES:
            return False
        if run.cancel_requested:
            self._emit("cancelled", run)
            return False
        try:
            run.ensure_tool_capacity(tool_call_count)
            run.record_step(step)
        except AgentRunLimitError as exc:
            self._emit("limit", run, error=str(exc), limit=exc.limit)
            return False
        except AgentRunCancelled:
            self._emit("cancelled", run)
            return False
        except AgentRunStateError:
            return False
        self._emit("step", run)
        return True

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, objective: str, *,
              conversation_id: Optional[str] = None,
              limits: Optional[AgentRunLimits] = None) -> AgentRun:
        text = _clip(objective, _MAX_OBJECTIVE_CHARS)
        if not text:
            raise AgentRunStateError("an objective is required to start a task")
        with self._lock:
            if self._run is not None and self._run.status not in TERMINAL_STATUSES:
                raise AgentRunStateError(
                    "a task run is already active; complete or cancel it first")
            conversation = (conversation_id
                            if conversation_id is not None
                            else self._conversation_id)
            run = AgentRun(
                objective=text,
                conversation_id=conversation or "",
                limits=limits or AgentRunLimits.from_environment(),
            )
            self._run = run
        self._emit("started", run)
        return run

    def update(self, *, run_id: Optional[str] = None,
               status: str = "", step: Optional[str] = None,
               summary: str = "", error: str = "",
               remaining: Optional[str] = None) -> AgentRun:
        run = self._resolve(run_id)
        if status:
            run.set_status(status, current_step=step, summary=summary,
                           error=error)
            if remaining is not None and run.status not in TERMINAL_STATUSES:
                run.set_progress(remaining=remaining)
            self._emit("status", run)
        else:
            run.set_progress(step=step, summary=summary, remaining=remaining)
            self._emit("progress", run)
        return run

    def record_step(self, run_id: Optional[str] = None, step: str = "") -> AgentRun:
        run = self._resolve(run_id)
        try:
            run.record_step(step)
        except AgentRunLimitError as exc:
            self._emit("limit", run, error=str(exc), limit=exc.limit)
            raise
        except AgentRunCancelled:
            self._emit("cancelled", run)
            raise
        self._emit("step", run)
        return run

    def record_tool_result(self, *, run_id: Optional[str] = None,
                           tool_name: str = "", ok: bool = True,
                           summary: str = "",
                           failure: Optional[Dict[str, Any]] = None,
                           signature: str = "",
                           side_effect: Optional[bool] = None) -> AgentRun:
        run = self._resolve(run_id)
        try:
            run.record_tool_call(
                tool_name=tool_name, ok=ok, summary=summary,
                failure=failure, signature=signature,
                side_effect=side_effect)
        except AgentRunLimitError as exc:
            self._emit("limit", run, error=str(exc), limit=exc.limit)
            raise
        except AgentRunCancelled:
            self._emit("cancelled", run)
            raise
        self._emit("tool", run)
        return run

    def record_pending(self, *, run_id: Optional[str] = None,
                       tool_name: str = "", signature: str = "",
                       category: str = "confirmation_required",
                       summary: str = "") -> AgentRun:
        run = self._resolve(run_id)
        run.record_pending(
            tool_name=tool_name, signature=signature, category=category,
            summary=summary)
        self._emit("pending", run)
        return run

    def add_evidence(self, *, run_id: Optional[str] = None,
                     summary: str = "", source: str = "model",
                     tool: str = "", kind: str = "") -> AgentRun:
        run = self._resolve(run_id)
        run.add_evidence(summary, source=source, tool=tool, kind=kind)
        self._emit("evidence", run)
        return run

    def call_guard(self, run_id: str, call: Any) -> str:
        """Return a reason when a failed call must not be repeated unchanged."""
        run = self._resolve(run_id, required=False)
        if run is None or run.status in TERMINAL_STATUSES:
            return "the task run is no longer active"
        name = str(getattr(call, "name", "") or "")
        if name == "task_status":
            return ""
        signature = call_signature(call)
        record = run.failed_attempts.get(signature)
        if record and not run.can_retry_signature(signature):
            category = record.get("category", "unknown")
            previous = _clip(record.get("last_error"), 180)
            suffix = f" Last error: {previous}." if previous else ""
            return (
                f"Repeated failed operation blocked ({category}).{suffix} "
                "Change the arguments, use another approach, or ask the user.")
        completed = run.completed_attempts.get(signature)
        if completed:
            return (
                f"This {completed.get('tool', name)} operation already "
                "completed successfully. Do not repeat it unless the user "
                "explicitly asks for a new run.")
        return ""

    def mark_needs_user(self, *, run_id: Optional[str] = None,
                        step: str = "", summary: str = "") -> AgentRun:
        run = self._resolve(run_id)
        run.set_status(NEEDS_USER, current_step=step, summary=summary)
        self._emit("needs_user", run)
        return run

    def add_artifact(self, *, run_id: Optional[str] = None,
                     name: Any = "", value: Any = None) -> AgentRun:
        run = self._resolve(run_id)
        run.add_artifact(name, value)
        self._emit("artifact", run)
        return run

    def complete(self, *, run_id: Optional[str] = None,
                 summary: str = "") -> AgentRun:
        run = self._resolve(run_id)
        run.complete(summary)
        self._emit("completed", run)
        return run

    def request_completion(self, *, run_id: Optional[str] = None,
                           summary: str = "",
                           evidence: Optional[list] = None) -> AgentRun:
        """Complete only when the run's objective has enough evidence.

        This is the completion path used by ``task_status``.  The low-level
        ``complete`` remains available for internal state transitions and tests.
        """
        run = self._resolve(run_id)
        for item in _evidence_items(evidence):
            run.add_evidence(item, source="model")
        if run.verification_exhausted:
            raise AgentRunVerificationError(
                "Verification attempts are exhausted; report the task as "
                "blocked or failed rather than completed.",
                missing=list(run.verification_missing),
                attempts=run.verification_attempts,
                exhausted=True)
        missing = run.missing_verification()
        if not missing:
            run.set_verification_missing([])
            try:
                run.set_progress(remaining="")
            except AgentRunError:
                pass
            run.complete(summary)
            self._emit("completed", run)
            return run

        attempts = run.note_verification_attempt()
        run.set_verification_missing(missing)
        if attempts >= _MAX_VERIFICATION_ATTEMPTS:
            message = (
                "Verification could not be completed after "
                f"{attempts} attempts. Report the task as blocked or failed.")
            run.set_verification_exhausted()
            try:
                run.set_status(
                    BLOCKED, current_step="Verification required",
                    summary="; ".join(missing), error=message)
            except AgentRunError:
                pass
            self._emit("verification_exhausted", run,
                       missing=missing, error=message)
            raise AgentRunVerificationError(
                message, missing=missing, attempts=attempts, exhausted=True)
        try:
            run.set_progress(
                step="Verification required: " + "; ".join(missing),
                remaining="; ".join(missing))
        except AgentRunError:
            pass
        self._emit("verification_required", run, missing=missing)
        raise AgentRunVerificationError(
            "Completion needs more evidence: " + "; ".join(missing),
            missing=missing, attempts=attempts, exhausted=False)

    def fail(self, *, run_id: Optional[str] = None,
             reason: str = "") -> AgentRun:
        run = self._resolve(run_id)
        run.fail(reason)
        self._emit("failed", run)
        return run

    def cancel(self, *, run_id: Optional[str] = None,
               reason: str = "") -> Optional[AgentRun]:
        run = self._resolve(run_id, required=False)
        if run is None or run.status in TERMINAL_STATUSES:
            return run
        run.cancel(reason)
        self._emit("cancelled", run)
        return run

    def check_duration(self, run_id: Optional[str] = None) -> Optional[AgentRun]:
        run = self._resolve(run_id, required=False)
        if run is None or run.status in TERMINAL_STATUSES:
            return run
        try:
            run.check_limits()
        except AgentRunLimitError as exc:
            self._emit("limit", run, error=str(exc), limit=exc.limit)
            raise
        except AgentRunCancelled:
            self._emit("cancelled", run)
            raise
        return run

    def reset(self) -> None:
        """Test/maintenance hook: forget the current run."""
        with self._lock:
            self._run = None

    # ── helpers ──────────────────────────────────────────────────────────────

    def _resolve(self, run_id: Optional[str] = None,
                 *, required: bool = True) -> Optional[AgentRun]:
        with self._lock:
            run = self._run
        if run is None:
            if required:
                raise AgentRunStateError("no active task run")
            return None
        if run_id and str(run_id) != run.run_id:
            if required:
                raise AgentRunStateError(
                    f"no task run with id {run_id!r}")
            return None
        return run

    def _emit(self, event: str, run: AgentRun, **extra: Any) -> None:
        with self._lock:
            listener = self._listener
        if listener is None:
            return
        payload: Dict[str, Any] = {
            "event": str(event or "updated"),
            "run": run.to_dict(),
            "ts": _now_iso(),
        }
        payload.update({key: value for key, value in extra.items()
                        if value is not None})
        try:
            listener(payload)
        except Exception as exc:  # noqa: BLE001 - the UI must never break a run
            print(f"[AgentRun] event listener failed: {exc}")


# ── process-wide instance ────────────────────────────────────────────────────
_SERVICE: Optional[AgentRunService] = None
_SERVICE_LOCK = threading.RLock()


def get_service() -> AgentRunService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = AgentRunService()
        return _SERVICE


def reset_service(service: Optional[AgentRunService] = None) -> Optional[AgentRunService]:
    """Replace the process service (tests and isolated hosts)."""
    global _SERVICE
    with _SERVICE_LOCK:
        previous = _SERVICE
        _SERVICE = service
        return previous


# ── Gemini-facing structured status tool ─────────────────────────────────────
def task_status(operation: str, objective: str = "", status: str = "",
                step: str = "", summary: str = "", reason: str = "",
                artifacts: Optional[list] = None,
                evidence: Optional[list] = None,
                remaining: str = "") -> Dict[str, Any]:
    """Start, update, finish or inspect the current objective-driven task.

    This is the only way a run starts.  Ordinary conversation and one-shot
    requests never create one; Gemini calls this tool when the user's request is
    a multi-step objective that benefits from tracked state.
    """
    service = get_service()
    op = _clip(operation, 40).lower()
    try:
        if op == "start":
            run = service.start(objective)
            if step or remaining:
                service.update(step=step or None, remaining=remaining or None)
            for item in _artifact_items(artifacts):
                service.add_artifact(name=item)
            message = "Task run started."
        elif op == "update":
            if (not status and not step and not summary and not artifacts
                    and not evidence and not remaining):
                raise AgentRunStateError(
                    "update needs a status, step, summary, artifact, evidence "
                    "or remaining work")
            wanted = str(status or "").strip().lower()
            if wanted in TERMINAL_STATUSES:
                raise AgentRunStateError(
                    "use complete, fail or cancel for a terminal status")
            run = service.update(status=status, step=step, summary=summary,
                                 remaining=remaining or None)
            for item in _artifact_items(artifacts):
                service.add_artifact(name=item)
            for item in _evidence_items(evidence):
                service.add_evidence(summary=item, source="model")
            message = "Task run updated."
        elif op == "complete":
            for item in _artifact_items(artifacts):
                service.add_artifact(name=item)
            run = service.request_completion(
                summary=summary or step, evidence=evidence)
            message = "Task run completed."
        elif op == "fail":
            run = service.fail(reason=reason or summary or step)
            message = "Task run marked failed."
        elif op == "cancel":
            run = service.cancel(reason=reason or summary or step)
            if run is None:
                return {
                    "ok": True,
                    "operation": op,
                    "run": None,
                    "summary": "No active task run to cancel.",
                }
            if run.status != CANCELLED:
                raise AgentRunStateError(
                    f"run is already {run.status}")
            message = "Task run cancelled."
        elif op in ("status", "get", "get_status"):
            run = service.current()
            return {
                "ok": True,
                "operation": "status",
                "run": service.current_snapshot(),
                "context": run.context_summary() if run is not None else "",
                "summary": ("No active task run."
                            if run is None
                            else "Current task run."),
            }
        else:
            raise AgentRunStateError(
                f"unknown task operation: {operation!r}")
    except AgentRunVerificationError as exc:
        run = service.current()
        verification = exc.payload()
        return {
            "ok": False,
            "operation": op,
            "status": verification.get("status"),
            "run": run.to_dict() if run is not None else None,
            "context": run.context_summary() if run is not None else "",
            "error": str(exc),
            "summary": str(exc),
            "verification": verification,
            "missing_evidence": list(exc.missing),
        }
    except AgentRunError as exc:
        run = service.current()
        return {
            "ok": False,
            "operation": op,
            "run": run.to_dict() if run is not None else None,
            "context": run.context_summary() if run is not None else "",
            "error": str(exc),
            "summary": str(exc),
        }
    return {
        "ok": True,
        "operation": op,
        "run": run.to_dict(),
        "context": run.context_summary(),
        "summary": message,
    }


def _artifact_items(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    cleaned: List[str] = []
    for item in items[:10]:
        text = _clip(item, 300)
        if text:
            cleaned.append(text)
    return cleaned


def _evidence_items(value: Any) -> List[str]:
    """Normalise the evidence parameter of the task-status tool."""
    items = _artifact_items(value)
    return [item[:500] for item in items[:5]]

"""Server-side, single-use approval requests for Jarvis actions.

The model can *request* a protected action, but it can never authorize one.
Approval is granted only by the local dashboard through the HTTP approval
endpoints.  Requests are bound to the current conversation session, to an exact
operation fingerprint, and are consumed once.

This module has no dependency on the Gemini session or the web server so tools,
the runtime and tests can share the same rules.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
CONSUMED = "consumed"
REVOKED = "revoked"

ACTIVE_STATUSES = frozenset({PENDING, APPROVED})
TERMINAL_STATUSES = frozenset({REJECTED, EXPIRED, CONSUMED, REVOKED})

DEFAULT_TTL_SECONDS = 120.0
MIN_TTL_SECONDS = 15.0
MAX_TTL_SECONDS = 900.0
DEFAULT_SWEEP_INTERVAL = 0.5
MAX_STORED_REQUESTS = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_monotonic() -> float:
    return time.monotonic()


def _ttl_from_environment() -> float:
    raw = os.environ.get("JARVIS_APPROVAL_TTL", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, value))


def _fingerprint(tool: str, operation: str, target: str,
                 parameters: Dict[str, Any]) -> str:
    payload = {
        "tool": str(tool or ""),
        "operation": str(operation or ""),
        "target": str(target or ""),
        "parameters": parameters or {},
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ApprovalError(Exception):
    """An approval could not be granted or rejected in its current state."""

    def __init__(self, message: str, *, status: str = "") -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ApprovalRequest:
    approval_id: str
    tool: str
    operation: str
    target: str
    summary: str
    parameters: Dict[str, Any]
    session_id: str
    fingerprint: str
    created_at: str
    expires_at: str
    expires_monotonic: float
    status: str = PENDING
    decided_at: str = ""
    consumed_at: str = ""
    reason: str = ""

    def public(self) -> Dict[str, Any]:
        """The payload the browser may see.  No internal session/fingerprint."""
        return {
            "approval_id": self.approval_id,
            "tool": self.tool,
            "operation": self.operation,
            "target": self.target,
            "summary": self.summary,
            "parameters": dict(self.parameters),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "decided_at": self.decided_at,
            "consumed_at": self.consumed_at,
            "reason": self.reason,
        }


class ApprovalService:
    """Thread-safe process-wide approval queue.

    ``authorize`` is called by a protected tool before it executes.  It either
    consumes a matching approved request or returns a pending request for the
    UI.  ``approve``/``reject`` are called only by the local HTTP boundary.
    """

    def __init__(
        self,
        *,
        ttl_seconds: Optional[float] = None,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL,
        start_sweeper: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._listener: Optional[Callable[[Dict[str, Any]], None]] = None
        self._requests: Dict[str, ApprovalRequest] = {}
        self._order: List[str] = []
        self._session_id = ""
        self._ttl = (
            max(0.01, float(ttl_seconds))
            if ttl_seconds is not None
            else _ttl_from_environment()
        )
        self._sweep_interval = max(0.05, float(sweep_interval))
        self._stop = threading.Event()
        self._sweeper: Optional[threading.Thread] = None
        if start_sweeper:
            self._sweeper = threading.Thread(
                target=self._sweep_loop,
                name="jarvis-approval-expiry",
                daemon=True,
            )
            self._sweeper.start()

    # ── configuration / events ───────────────────────────────────────────────

    def set_listener(
        self, listener: Optional[Callable[[Dict[str, Any]], None]]
    ) -> None:
        with self._lock:
            self._listener = listener

    def set_session(self, session_id: Any) -> None:
        """Bind approvals to a conversation; a change revokes every active one."""
        new_id = str(session_id or "")
        events: List[Dict[str, Any]] = []
        with self._lock:
            if new_id == self._session_id:
                return
            for request in self._requests.values():
                if request.status in ACTIVE_STATUSES:
                    self._set_status_locked(
                        request, REVOKED, reason="session_changed")
                    events.append(self._event("revoked", request))
            self._session_id = new_id
        self._emit_many(events)

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    # ── tool-facing API ──────────────────────────────────────────────────────

    def authorize(
        self,
        *,
        tool: str,
        operation: str,
        target: str,
        parameters: Optional[Dict[str, Any]] = None,
        summary: str = "",
    ) -> Tuple[bool, Dict[str, Any], str]:
        """Check or create the approval for one exact operation.

        Returns ``(allowed, public_request, reason)``.  ``allowed`` is true only
        when a matching request was approved by the UI and has just been
        consumed.  Otherwise the caller must not execute the operation.
        """
        params = dict(parameters or {})
        fingerprint = _fingerprint(tool, operation, target, params)
        events: List[Dict[str, Any]] = []
        allowed = False
        reason = "pending"
        with self._lock:
            self._expire_locked(events)
            match = self._find_active_locked(fingerprint)
            if match is not None and match.status == APPROVED:
                self._set_status_locked(match, CONSUMED, consumed=True)
                events.append(self._event("consumed", match))
                public = match.public()
                allowed = True
                reason = "consumed"
            elif match is not None:
                public = match.public()
                reason = "pending"
            else:
                created = self._create_locked(
                    tool=tool,
                    operation=operation,
                    target=target,
                    summary=summary,
                    parameters=params,
                    fingerprint=fingerprint,
                )
                public = created.public()
                events.append(self._event("created", created))
                reason = "created"
        self._emit_many(events)
        return allowed, public, reason

    # ── UI-facing API ────────────────────────────────────────────────────────

    def approve(self, approval_id: str) -> Dict[str, Any]:
        events: List[Dict[str, Any]] = []
        with self._lock:
            self._expire_locked(events)
            request = self._resolve_locked(approval_id)
            if request.status == APPROVED:
                public = request.public()
            elif request.status != PENDING:
                raise ApprovalError(
                    f"Approval is {request.status} and cannot be granted.",
                    status=request.status,
                )
            else:
                self._set_status_locked(request, APPROVED)
                events.append(self._event("approved", request))
                public = request.public()
        self._emit_many(events)
        return public

    def reject(self, approval_id: str) -> Dict[str, Any]:
        events: List[Dict[str, Any]] = []
        with self._lock:
            self._expire_locked(events)
            request = self._resolve_locked(approval_id)
            if request.status == REJECTED:
                public = request.public()
            elif request.status not in (PENDING, APPROVED):
                raise ApprovalError(
                    f"Approval is {request.status} and cannot be rejected.",
                    status=request.status,
                )
            else:
                self._set_status_locked(request, REJECTED, reason="user_rejected")
                events.append(self._event("rejected", request))
                public = request.public()
        self._emit_many(events)
        return public

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._expire_locked([])
            request = self._requests.get(str(approval_id or ""))
            return request.public() if request is not None else None

    def active_requests(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._expire_locked([])
            rows = [
                request.public()
                for request in self._requests.values()
                if request.status in ACTIVE_STATUSES
            ]
        rows.sort(key=lambda item: (item.get("created_at", ""),
                                    item.get("approval_id", "")))
        return rows

    def public_requests(self) -> List[Dict[str, Any]]:
        """Alias used by bootstrap snapshots."""
        return self.active_requests()

    def all_requests(self) -> List[Dict[str, Any]]:
        """Test/diagnostic view, including terminal states."""
        with self._lock:
            self._expire_locked([])
            rows = [request.public() for request in self._requests.values()]
        rows.sort(key=lambda item: (item.get("created_at", ""),
                                    item.get("approval_id", "")))
        return rows

    def revoke_all(self, reason: str = "revoked") -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        with self._lock:
            for request in self._requests.values():
                if request.status in ACTIVE_STATUSES:
                    self._set_status_locked(request, REVOKED, reason=reason)
                    events.append(self._event("revoked", request))
        self._emit_many(events)
        return [event["approval"] for event in events]

    def expire(self) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        with self._lock:
            self._expire_locked(events)
        self._emit_many(events)
        return [event["approval"] for event in events]

    def close(self) -> None:
        self._stop.set()
        thread = self._sweeper
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)
        self._sweeper = None

    # ── internals ────────────────────────────────────────────────────────────

    def _find_active_locked(self, fingerprint: str) -> Optional[ApprovalRequest]:
        session = self._session_id
        for request in self._requests.values():
            if (request.status in ACTIVE_STATUSES
                    and request.session_id == session
                    and request.fingerprint == fingerprint):
                return request
        return None

    def _create_locked(
        self,
        *,
        tool: str,
        operation: str,
        target: str,
        summary: str,
        parameters: Dict[str, Any],
        fingerprint: str,
    ) -> ApprovalRequest:
        now_mono = _now_monotonic()
        expires_mono = now_mono + self._ttl
        request = ApprovalRequest(
            approval_id=uuid.uuid4().hex,
            tool=str(tool or ""),
            operation=str(operation or ""),
            target=str(target or ""),
            summary=str(summary or ""),
            parameters=dict(parameters or {}),
            session_id=self._session_id,
            fingerprint=fingerprint,
            created_at=_now_iso(),
            expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=self._ttl)
            ).isoformat(timespec="seconds"),
            expires_monotonic=expires_mono,
        )
        self._requests[request.approval_id] = request
        self._order.append(request.approval_id)
        self._trim_locked()
        return request

    def _resolve_locked(self, approval_id: str) -> ApprovalRequest:
        request = self._requests.get(str(approval_id or ""))
        if request is None:
            raise ApprovalError("Approval request not found.", status="not_found")
        if request.session_id != self._session_id:
            self._set_status_locked(request, REVOKED, reason="session_changed")
            raise ApprovalError(
                "Approval belongs to a different session.", status="revoked")
        return request

    def _set_status_locked(
        self,
        request: ApprovalRequest,
        status: str,
        *,
        reason: str = "",
        consumed: bool = False,
    ) -> None:
        request.status = status
        if reason:
            request.reason = reason
        stamp = _now_iso()
        if status in (APPROVED, REJECTED):
            request.decided_at = stamp
        if consumed or status == CONSUMED:
            request.consumed_at = stamp
            request.decided_at = request.decided_at or stamp
        if status == EXPIRED and not request.reason:
            request.reason = "expired"

    def _expire_locked(self, events: List[Dict[str, Any]]) -> None:
        now = _now_monotonic()
        for request in self._requests.values():
            if (request.status in ACTIVE_STATUSES
                    and request.expires_monotonic <= now):
                self._set_status_locked(request, EXPIRED, reason="expired")
                events.append(self._event("expired", request))

    def _trim_locked(self) -> None:
        while len(self._order) > MAX_STORED_REQUESTS:
            oldest = self._order.pop(0)
            request = self._requests.get(oldest)
            if request is None or request.status in ACTIVE_STATUSES:
                # Never silently drop an active request; put it back.
                self._order.append(oldest)
                break
            self._requests.pop(oldest, None)

    def _event(self, name: str, request: ApprovalRequest) -> Dict[str, Any]:
        return {
            "type": "approval.updated",
            "event": name,
            "approval": request.public(),
            "ts": _now_iso(),
        }

    def _emit_many(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        with self._lock:
            listener = self._listener
        for event in events:
            approval = event.get("approval") or {}
            print(
                f"[APPROVAL] {event.get('event')} {approval.get('tool', '')}"
                f" {approval.get('operation', '')}: "
                f"{approval.get('target', '')}"
            )
            if listener is None:
                continue
            try:
                listener(event)
            except Exception as exc:  # noqa: BLE001 - UI must not break the run
                print(f"[APPROVAL] listener failed: "
                      f"{type(exc).__name__}: {exc}")

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self._sweep_interval):
            try:
                self.expire()
            except Exception as exc:  # noqa: BLE001 - sweeper must keep running
                print(f"[APPROVAL] expiry sweep failed: "
                      f"{type(exc).__name__}: {exc}")


_SERVICE: Optional[ApprovalService] = None
_SERVICE_LOCK = threading.Lock()


def get_service() -> ApprovalService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = ApprovalService()
        return _SERVICE


def reset_service(service: Optional[ApprovalService] = None) -> ApprovalService:
    global _SERVICE
    with _SERVICE_LOCK:
        old = _SERVICE
        _SERVICE = service or ApprovalService()
        if old is not None and old is not _SERVICE:
            old.close()
        return _SERVICE


def shutdown_service() -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        old = _SERVICE
        _SERVICE = None
    if old is not None:
        old.close()

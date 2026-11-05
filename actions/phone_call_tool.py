"""Telephony actions for Jarvis.

The SIP/RTP engine lives in :mod:`core.telephony`; this file only exposes the
small, explicit action surface the model may call.  It is registered in
``actions/tool_calling.py`` like every other tool module, so the model decides
when to dial, answer, reject, hang up or mute from the user's natural language.

No action starts a call unless the model received an explicit user request.
Incoming calls are events, not automatic conversations, and auto-answer is off
unless ``JARVIS_SIP_AUTO_ANSWER`` is enabled.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from config import get_call_allowlist, validate_telephony_settings
from core import approvals as _approvals
from core import telephony

# Widely used emergency/service numbers.  Automated calls to these are blocked:
# the user must dial them directly.
_EMERGENCY_NUMBERS = frozenset({
    "000", "110", "111", "112", "118", "119", "911", "999",
})


def _service():
    return telephony.get_service()


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _sip_user(target: str) -> str:
    text = str(target or "")
    if "@" in text:
        text = text.rsplit("@", 1)[0]
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    return text


def _is_emergency(raw: str, target: str) -> bool:
    values = {_digits(raw), _digits(_sip_user(target))}
    return any(value in _EMERGENCY_NUMBERS for value in values)


def _is_allowlisted(raw: str, target: str, settings: Dict[str, Any]) -> bool:
    raw_key = str(raw or "").strip().casefold()
    target_key = str(target or "").strip().casefold()
    raw_digits = _digits(raw)
    for entry in get_call_allowlist():
        entry_key = entry.casefold()
        if entry_key in (raw_key, target_key):
            return True
        if raw_digits and _digits(entry) == raw_digits:
            return True
        try:
            resolved = telephony.resolve_target(entry, settings)
        except Exception:
            resolved = ""
        if resolved and resolved.casefold() == target_key:
            return True
    return False


def _approval_required(summary: str, request: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": False,
        "success": False,
        "operation": "call",
        "approval_required": True,
        "reason": "approval_required",
        "approval_summary": summary,
        "approval_expires_at": str(request.get("expires_at") or ""),
        "_approval_id": str(request.get("approval_id") or ""),
        "message": (
            f"{summary}. The user must approve this call in the Jarvis "
            "interface."),
        "error": (
            "Approval required in the Jarvis interface. Do not retry until "
            "the user approves, and never supply confirmation flags."),
    }


def call(number_or_sip_address: str, display_name: str = "") -> Dict[str, Any]:
    """Place an outgoing SIP call after trusted authorization.

    ``number_or_sip_address`` may be a full ``sip:user@domain`` URI, a bare SIP
    username on the configured domain, or a contact name from
    ``config/contacts.json``.  Allowlisted targets skip the UI gate; all other
    new calls require a single-use approval.  Emergency numbers are blocked.
    """
    raw = str(number_or_sip_address or "").strip()
    try:
        if _is_emergency(raw, raw):
            print(f"[APPROVAL] blocked emergency call to {_sip_user(raw)}")
            return {
                "ok": False,
                "success": False,
                "operation": "call",
                "error_code": "emergency_call_blocked",
                "emergency": True,
                "error": (
                    "Emergency numbers cannot be dialled by Jarvis. Ask the "
                    "user to dial the emergency number directly."),
            }
        settings = _service().settings()
        target = telephony.resolve_target(raw, settings)
        if _is_emergency(raw, target):
            print(f"[APPROVAL] blocked emergency call to {_sip_user(target)}")
            return {
                "ok": False,
                "success": False,
                "operation": "call",
                "error_code": "emergency_call_blocked",
                "emergency": True,
                "error": (
                    "Emergency numbers cannot be dialled by Jarvis. Ask the "
                    "user to dial the emergency number directly."),
            }
        if not _is_allowlisted(raw, target, settings):
            summary = f"Place a call to {target}"
            try:
                allowed, request, _reason = _approvals.get_service().authorize(
                    tool="phone_call",
                    operation="call",
                    target=target,
                    parameters={"target": target},
                    summary=summary,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed
                return {
                    "ok": False,
                    "success": False,
                    "operation": "call",
                    "approval_unavailable": True,
                    "reason": "approval_unavailable",
                    "error": (
                        "Approval is unavailable, so no call was placed: "
                        f"{type(exc).__name__}: {exc}"),
                }
            if not allowed:
                return _approval_required(summary, request)
        result = _service().call(target)
        result.setdefault("success", bool(result.get("ok")))
        if display_name and result.get("call"):
            result["call"]["requested_name"] = display_name
        return result
    except telephony.TelephonyError as exc:
        return {"ok": False, "success": False, "operation": "call",
                "state": "failed", "connected": False,
                "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - actions always answer the model
        return {"ok": False, "success": False, "operation": "call",
                "state": "failed", "connected": False,
                "error": f"{type(exc).__name__}: {exc}"}


def _failure(operation: str, exc: Exception) -> Dict[str, Any]:
    """Truthful failure shape shared by every phone action."""
    return {
        "ok": False,
        "success": False,
        "operation": operation,
        "state": "failed",
        "connected": False,
        "error": (str(exc) if isinstance(exc, telephony.TelephonyError)
                  else f"{type(exc).__name__}: {exc}"),
    }


def answer_call(call_id: str = "") -> Dict[str, Any]:
    """Answer a ringing incoming call."""
    try:
        return _service().answer(call_id)
    except telephony.TelephonyError as exc:
        return _failure("answer_call", exc)
    except Exception as exc:  # noqa: BLE001
        return _failure("answer_call", exc)


def reject_call(call_id: str = "", reason: str = "declined") -> Dict[str, Any]:
    """Reject a ringing incoming call without answering it."""
    try:
        return _service().reject(call_id, reason or "declined")
    except telephony.TelephonyError as exc:
        return _failure("reject_call", exc)
    except Exception as exc:  # noqa: BLE001
        return _failure("reject_call", exc)


def hangup(call_id: str = "") -> Dict[str, Any]:
    """End the active call, or cancel an outgoing call that is still ringing."""
    try:
        return _service().hangup(call_id)
    except telephony.TelephonyError as exc:
        return _failure("hangup", exc)
    except Exception as exc:  # noqa: BLE001
        return _failure("hangup", exc)


def _bool_request(value: Any, default: bool = True) -> bool:
    """Parse a boolean request without treating the string 'false' as true."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().casefold()
    if text in ("1", "true", "yes", "on", "mute", "muted"):
        return True
    if text in ("0", "false", "no", "off", "unmute", "unmuted", ""):
        return False
    return default


def mute_call(call_id: str = "", muted: bool = True) -> Dict[str, Any]:
    """Mute or unmute the outbound audio Jarvis sends to the caller.

    Incoming caller audio keeps flowing so Jarvis can still listen and answer;
    use ``muted=false`` to speak again.
    """
    try:
        return _service().mute(call_id, _bool_request(muted, True))
    except telephony.TelephonyError as exc:
        return _failure("mute_call", exc)
    except Exception as exc:  # noqa: BLE001
        return _failure("mute_call", exc)


def unmute_call(call_id: str = "") -> Dict[str, Any]:
    """Unmute the outbound audio Jarvis sends to the caller."""
    return mute_call(call_id, False)

def get_call_status(call_id: str = "") -> Dict[str, Any]:
    """Report SIP registration, active calls, mute state and current duration."""
    try:
        return _service().status(call_id)
    except Exception as exc:  # noqa: BLE001 - status must never crash the model
        return _failure("get_call_status", exc)


def get_call_history(limit: int = 20) -> Dict[str, Any]:
    """Return recent call history, newest first (no credentials or audio)."""
    try:
        return _service().history(limit)
    except Exception as exc:  # noqa: BLE001
        return _failure("get_call_history", exc)


def _redact(text: str) -> str:
    return re.sub(
        r"(?i)\b(password|passwd|secret|token|authorization)\b\s*[:=]\s*\S+",
        r"\1=[redacted]", str(text or ""))[:300]


def readiness() -> Dict[str, Any]:
    """Truthful capability status for the dashboard, never a secret.

    ``ready`` means the SIP engine reported a verified registration. A
    configured but untested account is reported as ``configured_unverified``;
    a value merely existing is never called valid.
    """
    import importlib.util

    result: Dict[str, Any] = {
        "capability": "phone",
        "label": "PHONE CALL",
        "status": "unavailable",
        "detail": "",
        "configured": False,
    }
    try:
        settings = _service().settings()
    except Exception as exc:  # noqa: BLE001 - a readiness check never raises
        result["detail"] = (
            f"Telephony settings could not be read ({type(exc).__name__}).")
        return result
    result["configured"] = bool(
        str(settings.get("username") or "").strip()
        and str(settings.get("password") or ""))
    problems = validate_telephony_settings(settings, require_password=True)
    if problems:
        missing = any("password is missing" in item for item in problems)
        result["status"] = ("missing_credentials" if missing
                            else "invalid_configuration")
        result["detail"] = "; ".join(problems)
        return result
    missing_dependencies = []
    if importlib.util.find_spec("pylibsrtp") is None:
        missing_dependencies.append("pylibsrtp")
    if (importlib.util.find_spec("audioop") is None
            and importlib.util.find_spec("audioop_lts") is None):
        missing_dependencies.append("audioop-lts")
    if missing_dependencies:
        result["status"] = "missing_dependency"
        result["detail"] = ("Missing optional SIP audio package(s): "
                            + ", ".join(missing_dependencies))
        return result
    try:
        registration = _service().registration()
    except Exception as exc:  # noqa: BLE001
        result["status"] = "service_unavailable"
        result["detail"] = (
            f"SIP registration could not be queried ({type(exc).__name__}).")
        return result
    state = str(registration.get("state") or "").strip().casefold()
    last_error = _redact(registration.get("last_error") or "")
    if state == "registered":
        result["status"] = "ready"
        result["detail"] = "SIP registration is active."
    elif state in ("connecting", "registering", "starting"):
        result["status"] = "starting"
        result["detail"] = "SIP registration is in progress."
    elif last_error:
        result["status"] = "service_unavailable"
        result["detail"] = last_error
    else:
        result["status"] = "configured_unverified"
        result["detail"] = ("SIP credentials are configured but no live "
                            "registration has been verified.")
    return result


__all__ = [
    "call",
    "answer_call",
    "reject_call",
    "hangup",
    "mute_call",
    "unmute_call",
    "get_call_status",
    "get_call_history",
    "readiness",
]

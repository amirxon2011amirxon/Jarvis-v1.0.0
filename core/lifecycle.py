"""Jarvis process lifecycle commands.

This is deliberately not an action-pack tool. Shutting down Jarvis is a core
control operation: the browser interface gets a shutdown notice, the existing
Brave tab is closed when possible, and the local FastAPI/uvicorn server exits
gracefully. The launcher registers the actual process handler, so this module
stays independent of the web layer and easy to test.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

Handler = Callable[[str], None]

_lock = threading.RLock()
_handlers: List[Handler] = []
_requested = threading.Event()
_worker: Optional[threading.Thread] = None


def register_shutdown_handler(handler: Handler) -> Callable[[], None]:
    """Register one process-level shutdown callback.

    The callback receives the human-readable reason. Registering the same
    callable twice is a no-op. Returns an unregister function so tests and
    short-lived launchers can clean up after themselves.
    """
    if not callable(handler):
        raise TypeError("shutdown handler must be callable")
    with _lock:
        if handler not in _handlers:
            _handlers.append(handler)

    def unregister() -> None:
        with _lock:
            try:
                _handlers.remove(handler)
            except ValueError:
                pass

    return unregister


def has_shutdown_handler() -> bool:
    with _lock:
        return bool(_handlers)


def shutdown_requested() -> bool:
    return _requested.is_set()


def request_shutdown(reason: str = "", delay: float = 0.6) -> bool:
    """Start one graceful shutdown. Returns False if one is already running.

    ``delay`` gives the current model turn time to receive the tool response and
    speak a short confirmation before the browser/server are torn down.
    """
    global _worker
    text = str(reason or "").strip() or "user request"
    with _lock:
        if _requested.is_set():
            return False
        handlers = list(_handlers)
        if not handlers:
            return False
        _requested.set()
        worker = threading.Thread(
            target=_run_handlers,
            args=(text, max(0.0, float(delay)), handlers),
            name="jarvis-shutdown",
            daemon=True,
        )
        _worker = worker
        worker.start()
        return True


def _run_handlers(reason: str, delay: float, handlers: List[Handler]) -> None:
    if delay:
        time.sleep(delay)
    for handler in handlers:
        try:
            handler(reason)
        except Exception:  # noqa: BLE001 - one handler must not stop the rest
            traceback.print_exc()


def shutdown_jarvis(reason: str = "") -> Dict[str, Any]:
    """Shut down Jarvis itself: close the UI tab and stop the local server.

    This is a CORE lifecycle command, not an action-pack tool. It may be called
    only when the user explicitly asks to shut down, close, quit, turn off or
    power down Jarvis itself. It must not be used for closing applications,
    browser tabs, files, or the YouTube player panel.
    """
    text = str(reason or "").strip()[:200] or "explicit user request"
    if shutdown_requested():
        return {
            "success": True,
            "result": "Jarvis is already shutting down.",
            "already_shutting_down": True,
        }
    if not has_shutdown_handler():
        return {
            "success": False,
            "error": ("This runtime has no shutdown handler, so the local "
                      "server cannot be stopped from here."),
            "error_code": "shutdown_unavailable",
        }
    started = request_shutdown(text)
    if not started:
        return {
            "success": True,
            "result": "Jarvis is already shutting down.",
            "already_shutting_down": True,
        }
    return {
        "success": True,
        "result": "Shutting down Jarvis. The interface and local server are closing.",
        "shutting_down": True,
        "reason": text,
    }


def _reset_for_tests() -> None:
    """Clear handler/request state between isolated tests."""
    global _worker
    with _lock:
        _handlers.clear()
        _requested.clear()
        _worker = None

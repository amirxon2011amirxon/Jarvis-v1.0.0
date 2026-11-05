"""Dashboard and desktop notifications for reminders, timers and AgentRun.

Notifications are persisted in the same SQLite database as reminders.  The
dashboard path is authoritative: the service always emits a WebSocket event
first.  A desktop attempt is made separately and its real outcome is stored;
an unavailable or failed desktop notification never hides or cancels the
dashboard one.

No notification can execute a tool, shell command or file operation.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from xml.sax.saxutils import escape as _xml_escape

from core.scheduler import default_db_path

UTC = timezone.utc
MAX_NOTIFICATIONS = 500
MAX_TITLE = 180
MAX_BODY = 4000
PRIORITIES = ("low", "normal", "high", "urgent")
STATUSES = ("unread", "read", "dismissed")
KINDS = ("reminder", "timer", "agent", "system")


class NotificationError(ValueError):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _json(value: Any) -> str:
    if value is None:
        value = {}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _apple_escape(value: str) -> str:
    """Escape a value for an AppleScript double-quoted string.

    Backslashes must be escaped before quotes; escaping only quotes lets a
    body containing a backslash terminate the string and inject AppleScript.
    """
    return (str(value or "").replace("\\", "\\\\").replace('"', '\\"')
            .replace("\r", " ").replace("\n", " "))


def _desktop_command(title: str, body: str) -> Optional[List[str]]:
    """Return a safe desktop-notification command, or ``None`` if unsupported."""
    if sys.platform.startswith("win"):
        title_xml = _xml_escape(title)
        body_xml = _xml_escape(body)
        toast_xml = (
            "<toast><visual><binding template=\"ToastGeneric\">"
            f"<text>{title_xml}</text><text>{body_xml}</text>"
            "</binding></visual></toast>")
        encoded = base64.b64encode(toast_xml.encode("utf-8")).decode("ascii")
        script = (
            "$ErrorActionPreference='Stop';"
            "[Windows.UI.Notifications.ToastNotificationManager,"
            "Windows.UI.Notifications,ContentType=WindowsRuntime] > $null;"
            "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,"
            "ContentType=WindowsRuntime] > $null;"
            "$xml=[Windows.Data.Xml.Dom.XmlDocument]::new();"
            f"$xml.LoadXml([Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{encoded}')));"
            "$toast=[Windows.UI.Notifications.ToastNotification]::new($xml);"
            "[Windows.UI.Notifications.ToastNotificationManager]::"
            "CreateToastNotifier('Jarvis').Show($toast)")
        return ["powershell.exe", "-NoProfile", "-NonInteractive",
                "-WindowStyle", "Hidden", "-Command", script]
    if sys.platform == "darwin" and shutil.which("osascript"):
        script = (f'display notification "{_apple_escape(body)}" '
                  f'with title "{_apple_escape(title)}"')
        return ["osascript", "-e", script]
    if shutil.which("notify-send"):
        return ["notify-send", "-a", "Jarvis", title, body]
    return None


def _attempt_desktop(title: str, body: str) -> Dict[str, Any]:
    if str(os.environ.get("JARVIS_DESKTOP_NOTIFICATIONS", "1")).strip() == "0":
        return {"desktop": "unsupported",
                "error": "desktop notifications disabled by environment"}
    command = _desktop_command(title, body)
    if command is None:
        return {"desktop": "unsupported", "error": ""}
    try:
        result = subprocess.run(
            command, timeout=5.0, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False)
    except subprocess.TimeoutExpired:
        return {"desktop": "failed", "error": "desktop notification timed out"}
    except Exception as exc:  # noqa: BLE001 - desktop is best effort
        return {"desktop": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:300]}
    if int(result.returncode) != 0:
        return {"desktop": "failed",
                "error": f"desktop notifier exited {result.returncode}"}
    # The OS accepted the request.  We deliberately do not claim the user saw
    # it; dashboard delivery is reported separately as "emitted".
    return {"desktop": "sent", "error": ""}


class NotificationService:
    """Thread-safe persisted notification queue and event publisher."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        self.path = Path(db_path) if db_path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._publish: Optional[Callable[[Dict[str, Any]], None]] = None
        self._log: Optional[Callable[[str], None]] = None
        self._desktop: Optional[Callable[[str, str], Dict[str, Any]]] = None
        self._last_delivery: Dict[str, Any] = {}
        self._ensure_schema()

    # ── wiring ───────────────────────────────────────────────────────────────

    def configure(self, *, publish: Optional[Callable[[Dict[str, Any]], None]] = None,
                  log: Optional[Callable[[str], None]] = None,
                  desktop_notifier: Optional[Callable[[str, str], Dict[str, Any]]] = None,
                  ) -> None:
        with self._lock:
            if publish is not None:
                self._publish = publish
            if log is not None:
                self._log = log
            if desktop_notifier is not None:
                self._desktop = desktop_notifier

    def _write_log(self, message: str) -> None:
        with self._lock:
            log = self._log
        if log is not None:
            try:
                log(message)
            except Exception:  # noqa: BLE001
                pass

    def _emit(self, event: str, notification: Dict[str, Any]) -> None:
        with self._lock:
            publish = self._publish
        if publish is None:
            return
        try:
            publish({
                "type": f"notification.{event}",
                "event": event,
                "notification": notification,
                "ts": _now_iso(),
            })
        except Exception as exc:  # noqa: BLE001 - UI must never break a timer
            self._write_log(f"SYS: Notification event failed: {exc}")

    # ── persistence ──────────────────────────────────────────────────────────

    def _connect(self):
        import sqlite3

        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _ensure_schema(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    reminder_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'system',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    status TEXT NOT NULL DEFAULT 'unread',
                    created_at TEXT NOT NULL,
                    read_at TEXT NOT NULL DEFAULT '',
                    dismissed_at TEXT NOT NULL DEFAULT '',
                    delivery TEXT NOT NULL DEFAULT '{}',
                    actions TEXT NOT NULL DEFAULT '[]',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'system',
                    dedupe_key TEXT NOT NULL UNIQUE
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_status "
                "ON notifications(status, created_at)")
            connection.commit()

    @staticmethod
    def _row(row: Any) -> Dict[str, Any]:
        data = dict(row)
        for key in ("delivery", "actions", "metadata"):
            try:
                default = "[]" if key == "actions" else "{}"
                data[key] = json.loads(data.get(key) or default)
            except Exception:  # noqa: BLE001
                data[key] = [] if key == "actions" else {}
        return data

    def get(self, notification_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE id = ?",
                (str(notification_id or ""),)).fetchone()
        return self._row(row) if row is not None else None

    def list(self, *, status: str = "", kind: str = "",
             limit: int = 50, unread_only: bool = False
             ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        values: List[Any] = []
        if unread_only:
            clauses.append("status = 'unread'")
        elif status:
            clauses.append("status = ?")
            values.append(status)
        if kind:
            clauses.append("kind = ?")
            values.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(max(1, min(MAX_NOTIFICATIONS, int(limit or 50))))
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM notifications{where} "
                "ORDER BY created_at DESC LIMIT ?", values).fetchall()
        return [self._row(row) for row in rows]

    def unread_count(self) -> int:
        with self._lock, closing(self._connect()) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE status='unread'"
            ).fetchone()[0])

    # ── creation ─────────────────────────────────────────────────────────────

    def create(self, *, title: str, body: str = "", kind: str = "system",
               priority: str = "normal", reminder_id: str = "",
               source: str = "system", dedupe_key: str = "",
               actions: Optional[List[Dict[str, Any]]] = None,
               metadata: Optional[Dict[str, Any]] = None,
               attempt_desktop: bool = True) -> Dict[str, Any]:
        title = _clean(title, MAX_TITLE)
        if not title:
            raise NotificationError("a notification title is required")
        body = _clean(body, MAX_BODY)
        kind = str(kind or "system").strip().casefold()
        if kind not in KINDS:
            kind = "system"
        priority = str(priority or "normal").strip().casefold()
        if priority not in PRIORITIES:
            priority = "normal"
        dedupe_key = str(dedupe_key or uuid.uuid4().hex)[:300]
        actions = [dict(item) for item in (actions or [])
                   if isinstance(item, dict)][:4]
        metadata = dict(metadata or {})
        now = _now_iso()
        notification_id = "ntf_" + uuid.uuid4().hex[:16]
        delivery = {"dashboard": "emitted", "desktop": "pending"}
        with self._lock, closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM notifications WHERE dedupe_key = ?",
                (dedupe_key,)).fetchone()
            if existing is not None:
                return self._row(existing)
            connection.execute("""
                INSERT INTO notifications
                (id, reminder_id, title, body, kind, priority, status,
                 created_at, read_at, dismissed_at, delivery, actions,
                 metadata, source, dedupe_key)
                VALUES (?, ?, ?, ?, ?, ?, 'unread', ?, '', '', ?, ?, ?, ?, ?)
            """, (notification_id, str(reminder_id or ""), title, body, kind,
                  priority, now, _json(delivery), _json(actions),
                  _json(metadata), str(source or "system")[:40], dedupe_key))
            connection.commit()
            row = connection.execute(
                "SELECT * FROM notifications WHERE id = ?",
                (notification_id,)).fetchone()
            notification = self._row(row)
        self._emit("created", notification)
        if attempt_desktop:
            if self._desktop is None:
                # The native notifier can block for up to five seconds. It must
                # never stall the audio loop (AgentRun events are emitted from
                # it) or the scheduler thread, so deliver in the background and
                # publish the real delivery state when it finishes.
                threading.Thread(
                    target=self._deliver_desktop_async,
                    args=(notification_id, title, body),
                    name="jarvis-desktop-notification", daemon=True).start()
            else:
                self._deliver_desktop(notification_id, title, body)
                current = self.get(notification_id)
                if current is not None:
                    notification = current
                    self._emit("updated", notification)
        self._write_log(f"SYS: Notification — {title}")
        return notification

    def _deliver_desktop_async(self, notification_id: str, title: str,
                               body: str) -> None:
        self._deliver_desktop(notification_id, title, body)
        current = self.get(notification_id)
        if current is not None:
            self._emit("updated", current)

    def _deliver_desktop(self, notification_id: str, title: str,
                         body: str) -> None:
        with self._lock:
            notifier = self._desktop
        try:
            result = (notifier(title, body) if notifier is not None
                      else _attempt_desktop(title, body))
        except Exception as exc:  # noqa: BLE001
            result = {"desktop": "failed",
                      "error": f"{type(exc).__name__}: {exc}"[:300]}
        if not isinstance(result, dict):
            result = {"desktop": "failed", "error": "invalid notifier result"}
        status = str(result.get("desktop") or "failed")
        if status not in ("sent", "unsupported", "failed"):
            status = "failed"
        delivery = {"dashboard": "emitted", "desktop": status,
                    "error": str(result.get("error") or "")[:300]}
        with self._lock:
            self._last_delivery = dict(delivery)
        try:
            with self._lock, closing(self._connect()) as connection:
                connection.execute(
                    "UPDATE notifications SET delivery = ? WHERE id = ?",
                    (_json(delivery), notification_id))
                connection.commit()
        except Exception as exc:  # noqa: BLE001
            self._write_log(f"SYS: Notification delivery record failed: {exc}")

    # ── user actions ─────────────────────────────────────────────────────────

    def mark_read(self, notification_id: str) -> Dict[str, Any]:
        return self._set_status(notification_id, "read")

    def dismiss(self, notification_id: str, reason: str = "") -> Dict[str, Any]:
        return self._set_status(notification_id, "dismissed", reason=reason)

    def _set_status(self, notification_id: str, status: str,
                    *, reason: str = "") -> Dict[str, Any]:
        current = self.get(notification_id)
        if current is None:
            raise NotificationError("no notification has that id")
        if status not in STATUSES:
            raise NotificationError("invalid notification status")
        now = _now_iso()
        patch = {"status": status}
        if status == "read":
            patch["read_at"] = current.get("read_at") or now
        if status == "dismissed":
            patch["dismissed_at"] = now
            if reason:
                metadata = dict(current.get("metadata") or {})
                metadata["dismiss_reason"] = str(reason)[:200]
                patch["metadata"] = _json(metadata)
        sets = ", ".join(f"{key} = ?" for key in patch)
        values = []
        for key, value in patch.items():
            values.append(value)
        values.append(notification_id)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                f"UPDATE notifications SET {sets} WHERE id = ?", values)
            connection.commit()
        updated = self.get(notification_id) or current
        self._emit("updated", updated)
        return updated

    def snooze(self, notification_id: str, minutes: int = 10) -> Dict[str, Any]:
        current = self.get(notification_id)
        if current is None:
            raise NotificationError("no notification has that id")
        reminder_id = str(current.get("reminder_id") or "")
        if not reminder_id:
            raise NotificationError("this notification is not snoozable")
        from core.scheduler import get_scheduler

        scheduler = get_scheduler()
        scheduler.snooze(reminder_id, minutes)
        dismissed = self.dismiss(notification_id, reason="snoozed")
        metadata = dict(dismissed.get("metadata") or {})
        metadata["snoozed_minutes"] = max(1, min(1440, int(minutes or 10)))
        return dismissed


# ── process-wide instance ────────────────────────────────────────────────────
_SERVICE: Optional[NotificationService] = None
_SERVICE_LOCK = threading.Lock()


def get_service() -> NotificationService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = NotificationService()
        return _SERVICE


def reset_service(
        service: Optional[NotificationService] = None) -> NotificationService:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service or NotificationService()
        return _SERVICE

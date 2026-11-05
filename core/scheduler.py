"""Persistent reminder and timer scheduling for Jarvis.

One scheduler thread owns every pending reminder.  Reminders are stored in a
small SQLite database next to the existing memory database, so a restart can
restore them without a background service or a sleeping coroutine per item.

Timestamps are stored in UTC.  Recurrence is interpreted in the reminder's
timezone (the system zone by default), which keeps daily/weekly/monthly times
correct across daylight-saving changes.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python 3.9+ always has zoneinfo
    ZoneInfo = None  # type: ignore[assignment]

UTC = timezone.utc
MAX_TITLE = 160
MAX_DESCRIPTION = 2000
MAX_REMINDERS = 1000
MISSED_GRACE_SECONDS = 90
WAIT_CAP_SECONDS = 30.0
SCHEMA_VERSION = 1

PRIORITIES = ("low", "normal", "high", "urgent")
RECURRENCES = ("none", "daily", "weekly", "monthly", "custom")
STATUSES = ("pending", "fired", "missed", "cancelled")
_UNITS = {
    "second": 1, "seconds": 1, "s": 1,
    "minute": 60, "minutes": 60, "m": 60,
    "hour": 3600, "hours": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
    "week": 604800, "weeks": 604800, "w": 604800,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "mo": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "tu": 1,
    "wednesday": 2, "wed": 2, "we": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "th": 3,
    "friday": 4, "fri": 4, "fr": 4,
    "saturday": 5, "sat": 5, "sa": 5,
    "sunday": 6, "sun": 6, "su": 6,
}


class ScheduleError(ValueError):
    """An invalid schedule supplied by a tool caller."""


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ScheduleError("a timestamp is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ScheduleError(f"'{value}' is not a valid ISO timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone())
    return parsed.astimezone(UTC)


def local_zone() -> Any:
    """The system zone, preferring an IANA zone for DST correctness."""
    try:
        from tzlocal import get_localzone

        zone = get_localzone()
        if zone is not None:
            return zone
    except Exception:  # noqa: BLE001 - optional dependency
        pass
    return datetime.now().astimezone().tzinfo or UTC


def zone_for(name: Any) -> Any:
    text = str(name or "").strip()
    if text and ZoneInfo is not None:
        try:
            return ZoneInfo(text)
        except Exception:  # noqa: BLE001 - invalid names fall back to local
            pass
    return local_zone()


def zone_name(zone: Any = None) -> str:
    value = zone or local_zone()
    return str(getattr(value, "key", "") or getattr(value, "name", "")
               or value)


def default_db_path() -> Path:
    override = os.environ.get("JARVIS_REMINDERS_DB", "").strip()
    if override:
        return Path(override).expanduser()
    try:
        from config import CONFIG_DIR

        return Path(CONFIG_DIR) / "reminders.db"
    except Exception:  # noqa: BLE001
        return Path.home() / ".jarvis-reminders.db"


def _int(value: Any, default: int, low: int, high: int) -> int:
    text = "" if value is None else str(value).strip()
    if text in ("", "0", "none", "null"):
        return default
    try:
        number = int(float(text))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _weekday(value: Any) -> int:
    text = str(value or "").strip().casefold()
    if text.isdigit():
        number = int(text)
        # Accept both Python Monday=0 and a user-facing Sunday=0 naming where
        # the number alone is unambiguous only as 1..7.  0 is Monday here.
        return max(0, min(6, number)) if number <= 6 else 6
    if text not in _WEEKDAYS:
        raise ScheduleError(f"'{value}' is not a weekday")
    return _WEEKDAYS[text]


def _normalize_weekdays(value: Any) -> List[int]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [item for item in re.split(r"[,;\s]+", value) if item]
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]
    result = sorted({_weekday(item) for item in parts})
    return result


def normalize_recurrence(recurrence: Any, config: Any,
                         due: datetime, zone: Any) -> Tuple[str, Dict[str, Any]]:
    name = str(recurrence or "none").strip().casefold()
    aliases = {"once": "none", "one-time": "none", "one_time": "none",
               "single": "none", "weekly_on": "weekly"}
    name = aliases.get(name, name)
    if name not in RECURRENCES:
        raise ScheduleError(
            "recurrence must be one of none, daily, weekly, monthly or custom")
    raw = config if isinstance(config, dict) else {}
    local = due.astimezone(zone)
    if name == "none":
        return "none", {}
    if name == "daily":
        return "daily", {
            "interval": _int(raw.get("interval"), 1, 1, 365),
            "time": str(raw.get("time") or local.strftime("%H:%M")),
        }
    if name == "weekly":
        weekdays = _normalize_weekdays(raw.get("weekdays"))
        if not weekdays:
            weekdays = [local.weekday()]
        return "weekly", {
            "interval": _int(raw.get("interval"), 1, 1, 52),
            "weekdays": weekdays,
            "time": str(raw.get("time") or local.strftime("%H:%M")),
        }
    if name == "monthly":
        day = _int(raw.get("day_of_month"), local.day, 1, 31)
        return "monthly", {
            "interval": _int(raw.get("interval"), 1, 1, 24),
            "day_of_month": day,
            "time": str(raw.get("time") or local.strftime("%H:%M")),
        }
    interval = _int(raw.get("interval"), 1, 1, 100000)
    unit = str(raw.get("unit") or "minutes").strip().casefold()
    if unit not in _UNITS:
        raise ScheduleError(
            "custom interval unit must be seconds, minutes, hours, days or weeks")
    return "custom", {"interval": interval, "unit": unit}


def _time_parts(config: Dict[str, Any], anchor: datetime,
                zone: Any) -> Tuple[int, int]:
    text = str(config.get("time") or "").strip()
    if text:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
        if not match:
            raise ScheduleError("time must use HH:MM (24-hour)")
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ScheduleError("time is outside the 24-hour clock")
        return hour, minute
    local = anchor.astimezone(zone)
    return local.hour, local.minute


def next_due(after: datetime, recurrence: str, config: Dict[str, Any],
             zone: Any, anchor: datetime) -> Optional[datetime]:
    """Next occurrence strictly after ``after`` in the reminder's zone."""
    if recurrence == "none":
        return None
    after = after.astimezone(UTC)
    if recurrence == "custom":
        unit = str(config.get("unit") or "minutes")
        interval = max(1, int(config.get("interval") or 1))
        if unit in ("day", "days", "d", "week", "weeks", "w"):
            days = interval * (7 if unit.startswith("w") else 1)
            local_anchor = anchor.astimezone(zone)
            local_after = after.astimezone(zone)
            elapsed_days = (local_after.date() - local_anchor.date()).days
            steps = max(0, elapsed_days // days) + 1
            candidate = local_anchor + timedelta(days=steps * days)
            while candidate.astimezone(UTC) <= after:
                steps += 1
                candidate = local_anchor + timedelta(days=steps * days)
            return candidate.astimezone(UTC)
        seconds = interval * _UNITS[unit]
        step = timedelta(seconds=seconds)
        delta = after - anchor
        steps = max(0, int(delta.total_seconds() // seconds)) + 1
        candidate = anchor + step * steps
        while candidate <= after:
            candidate += step
        return candidate

    hour, minute = _time_parts(config, anchor, zone)
    interval = max(1, int(config.get("interval") or 1))
    local_after = after.astimezone(zone)
    if recurrence == "daily":
        anchor_local = anchor.astimezone(zone)
        candidate = local_after.replace(hour=hour, minute=minute,
                                        second=0, microsecond=0)
        if candidate <= local_after:
            candidate += timedelta(days=1)
        if interval > 1:
            elapsed = (candidate.date() - anchor_local.date()).days
            remainder = elapsed % interval
            if remainder:
                candidate += timedelta(days=interval - remainder)
        while candidate.astimezone(UTC) <= after:
            candidate += timedelta(days=interval)
        return candidate.astimezone(UTC)

    if recurrence == "weekly":
        weekdays = sorted({int(item) for item in config.get("weekdays") or []})
        if not weekdays:
            weekdays = [anchor.astimezone(zone).weekday()]
        anchor_local = anchor.astimezone(zone)
        anchor_monday = anchor_local.date() - timedelta(
            days=anchor_local.weekday())
        local_after = after.astimezone(zone)
        current_monday = local_after.date() - timedelta(
            days=local_after.weekday())
        weeks = max(0, (current_monday - anchor_monday).days // 7)
        if weeks % interval:
            weeks += interval - (weeks % interval)
        for _ in range(0, 5200):
            monday = anchor_monday + timedelta(weeks=weeks)
            for weekday in weekdays:
                date = monday + timedelta(days=weekday)
                candidate = datetime(date.year, date.month, date.day,
                                     hour, minute, tzinfo=zone)
                if candidate.astimezone(UTC) > after:
                    return candidate.astimezone(UTC)
            weeks += interval
        raise ScheduleError("weekly recurrence could not be resolved")

    if recurrence == "monthly":
        day = max(1, min(31, int(config.get("day_of_month") or
                                 anchor.astimezone(zone).day)))
        anchor_local = anchor.astimezone(zone)
        local_after = after.astimezone(zone)
        month_index = 0
        for _ in range(0, 6000):
            total = (anchor_local.year * 12 + anchor_local.month - 1
                     + month_index * interval)
            year, month_zero = divmod(total, 12)
            month = month_zero + 1
            last = calendar.monthrange(year, month)[1]
            date = datetime(year, month, min(day, last), hour, minute,
                            tzinfo=zone)
            if date.astimezone(UTC) > after:
                return date.astimezone(UTC)
            month_index += 1
        raise ScheduleError("monthly recurrence could not be resolved")

    return None


class ReminderStore:
    """Small SQLite repository for reminder rows."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _ensure_schema(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reminder_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM reminder_migrations"
            ).fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"reminder database schema {version} is newer than this "
                    f"build ({SCHEMA_VERSION}); it was not modified")
            if version < 1:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS reminders (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        priority TEXT NOT NULL DEFAULT 'normal',
                        status TEXT NOT NULL DEFAULT 'pending',
                        due_at TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        recurrence TEXT NOT NULL DEFAULT 'none',
                        recurrence_config TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_fired_at TEXT NOT NULL DEFAULT '',
                        cancelled_at TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT 'user',
                        metadata TEXT NOT NULL DEFAULT '{}'
                    )
                """)
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_reminders_due "
                    "ON reminders(status, due_at)")
                connection.execute(
                    "INSERT INTO reminder_migrations(version, applied_at) "
                    "VALUES (1, ?)", (_iso(_now_utc()),))
            connection.commit()

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        try:
            data["recurrence_config"] = json.loads(
                data.get("recurrence_config") or "{}")
        except Exception:  # noqa: BLE001 - malformed rows are isolated
            data["recurrence_config"] = {}
        try:
            data["metadata"] = json.loads(data.get("metadata") or "{}")
        except Exception:  # noqa: BLE001
            data["metadata"] = {}
        return data

    def add(self, record: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("""
                INSERT INTO reminders
                (id, kind, title, description, priority, status, due_at,
                 timezone, recurrence, recurrence_config, created_at,
                 updated_at, last_fired_at, cancelled_at, source, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["id"], record["kind"], record["title"],
                record.get("description", ""), record["priority"],
                record["status"], record["due_at"], record["timezone"],
                record["recurrence"],
                json.dumps(record.get("recurrence_config") or {},
                           sort_keys=True),
                record["created_at"], record["updated_at"],
                record.get("last_fired_at", ""),
                record.get("cancelled_at", ""),
                record.get("source", "user"),
                json.dumps(record.get("metadata") or {}, sort_keys=True),
            ))
            connection.commit()
        return self.get(record["id"]) or record

    def get(self, reminder_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM reminders WHERE id = ?",
                (str(reminder_id or ""),)).fetchone()
        return self._row(row) if row is not None else None

    def update(self, reminder_id: str, patch: Dict[str, Any],
               *, expected_status: Optional[str] = None
               ) -> Optional[Dict[str, Any]]:
        allowed = {
            "title", "description", "priority", "status", "due_at",
            "timezone", "recurrence", "recurrence_config", "updated_at",
            "last_fired_at", "cancelled_at", "metadata",
        }
        fields = []
        values: List[Any] = []
        for key, value in patch.items():
            if key not in allowed:
                continue
            if key in ("recurrence_config", "metadata"):
                value = json.dumps(value or {}, sort_keys=True)
            fields.append(f"{key} = ?")
            values.append(value)
        if not fields:
            return self.get(reminder_id)
        values.append(str(reminder_id))
        where = "id = ?"
        if expected_status is not None:
            where += " AND status = ?"
            values.append(str(expected_status))
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                f"UPDATE reminders SET {', '.join(fields)} WHERE {where}",
                values)
            connection.commit()
            changed = cursor.rowcount
        if changed == 0:
            # A concurrent cancel/snooze/update won the race. Never resurrect
            # the old row from a stale fire snapshot.
            return None
        return self.get(reminder_id)

    def due(self, now: datetime) -> List[Dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM reminders WHERE status = 'pending' "
                "AND due_at <= ? ORDER BY due_at ASC",
                (_iso(now),)).fetchall()
        return [self._row(row) for row in rows]

    def next_pending(self) -> Optional[Dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM reminders WHERE status = 'pending' "
                "ORDER BY due_at ASC LIMIT 1").fetchone()
        return self._row(row) if row is not None else None

    def list(self, *, status: str = "", kind: str = "",
             from_iso: str = "", to_iso: str = "",
             limit: int = 50) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        values: List[Any] = []
        if status:
            clauses.append("status = ?")
            values.append(status)
        if kind:
            clauses.append("kind = ?")
            values.append(kind)
        if from_iso:
            clauses.append("due_at >= ?")
            values.append(from_iso)
        if to_iso:
            clauses.append("due_at <= ?")
            values.append(to_iso)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(max(1, min(MAX_REMINDERS, int(limit or 50))))
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM reminders{where} "
                "ORDER BY due_at ASC LIMIT ?", values).fetchall()
        return [self._row(row) for row in rows]

    def count(self, status: str = "") -> int:
        with self._lock, closing(self._connect()) as connection:
            if status:
                return int(connection.execute(
                    "SELECT COUNT(*) FROM reminders WHERE status = ?",
                    (status,)).fetchone()[0])
            return int(connection.execute(
                "SELECT COUNT(*) FROM reminders").fetchone()[0])


class ReminderScheduler:
    """One centralized scheduler thread over the persistent reminder store."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        self.store = ReminderStore(db_path)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._notifier: Optional[Callable[..., Any]] = None
        self._listener: Optional[Callable[[Dict[str, Any]], None]] = None
        self._log: Optional[Callable[[str], None]] = None
        self._last_error = ""

    # ── wiring ───────────────────────────────────────────────────────────────

    def configure(self, *, notifier: Optional[Callable[..., Any]] = None,
                  listener: Optional[Callable[[Dict[str, Any]], None]] = None,
                  log: Optional[Callable[[str], None]] = None) -> None:
        with self._lock:
            if notifier is not None:
                self._notifier = notifier
            if listener is not None:
                self._listener = listener
            if log is not None:
                self._log = log

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def _write_log(self, message: str) -> None:
        with self._lock:
            log = self._log
        if log is not None:
            try:
                log(message)
            except Exception:  # noqa: BLE001
                pass

    def _emit(self, event: str, record: Dict[str, Any]) -> None:
        with self._lock:
            listener = self._listener
        if listener is not None:
            try:
                listener({"event": event, "reminder": record})
            except Exception:  # noqa: BLE001
                pass

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="jarvis-reminder-scheduler",
                daemon=True)
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        with self._lock:
            # Never forget a thread that is still alive: a later start() would
            # otherwise run a second scheduler over the same rows.
            if thread is not None and thread.is_alive():
                self._thread = thread
            else:
                self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        # Recover missed work before entering the normal wait loop.
        try:
            self.tick(startup=True)
        except Exception as exc:  # noqa: BLE001 - a scheduler must keep running
            self._set_error(f"startup tick failed: {type(exc).__name__}: {exc}")
        while not self._stop.is_set():
            try:
                next_record = self.store.next_pending()
                delay = WAIT_CAP_SECONDS
                if next_record is not None:
                    due = _parse_iso(next_record["due_at"])
                    delay = max(0.05, min(
                        WAIT_CAP_SECONDS,
                        (due - _now_utc()).total_seconds()))
                if self._wake.wait(delay):
                    self._wake.clear()
                if self._stop.is_set():
                    return
                self.tick()
                # A persistently failing due row must not spin at 20 Hz and
                # flood the log. Back off once when nothing advanced.
                remaining = self.store.next_pending()
                if (remaining is not None
                        and _parse_iso(remaining["due_at"]) <= _now_utc()):
                    self._wake.wait(1.0)
            except Exception as exc:  # noqa: BLE001
                self._set_error(
                    f"scheduler tick failed: {type(exc).__name__}: {exc}")
                self._wake.wait(1.0)

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = str(message)[:500]
        self._write_log(f"SYS: {message}")

    def tick(self, now: Optional[datetime] = None, *,
             startup: bool = False) -> int:
        current = (now or _now_utc()).astimezone(UTC)
        fired = 0
        for record in self.store.due(current):
            if self._stop.is_set() and not startup:
                break
            try:
                self._fire(record, current, startup=startup)
                fired += 1
            except Exception as exc:  # noqa: BLE001 - isolate one bad row
                self._set_error(
                    f"reminder {record.get('id')} failed: "
                    f"{type(exc).__name__}: {exc}")
        return fired

    def _fire(self, record: Dict[str, Any], now: datetime, *,
              startup: bool) -> None:
        # The due list was read before any notification work. Re-read the row
        # so a cancel/snooze committed in between is never overwritten by this
        # stale snapshot.
        fresh = self.store.get(record["id"])
        if (fresh is None or fresh.get("status") != "pending"
                or fresh.get("due_at") != record.get("due_at")):
            return
        due = _parse_iso(record["due_at"])
        late = (now - due).total_seconds() > MISSED_GRACE_SECONDS
        kind = record.get("kind", "reminder")
        title = record.get("title") or "Reminder"
        description = record.get("description") or ""
        timezone_name = record.get("timezone") or zone_name()
        local_due = due.astimezone(zone_for(timezone_name))
        if kind == "timer":
            notification_title = (
                "Timer finished" if not late
                else "Timer finished while Jarvis was closed")
            notification_body = description or f"{title} reached zero."
            notification_kind = "timer"
        else:
            notification_title = (
                f"Reminder: {title}" if not late
                else f"Missed reminder: {title}")
            notification_body = description or f"Scheduled for {local_due:%Y-%m-%d %H:%M}."
            notification_kind = "reminder"
        notifier = self._notifier
        if notifier is None:
            # Lazy import keeps scheduler free of a module-level dependency on
            # the web/event layer and avoids an import cycle.
            from core import notification_service

            notifier = notification_service.get_service().create
        notification = notifier(
            title=notification_title,
            body=notification_body,
            kind=notification_kind,
            priority=record.get("priority", "normal"),
            reminder_id=record["id"],
            source="scheduler",
            dedupe_key=f"reminder:{record['id']}:{record['due_at']}",
            actions=[{"action": "snooze", "minutes": 10,
                      "label": "SNOOZE 10M"}],
            metadata={"due_at": record["due_at"],
                      "timezone": timezone_name,
                      "timer": kind == "timer"},
        )
        patch: Dict[str, Any] = {
            "updated_at": _iso(now),
            "last_fired_at": _iso(now),
            "metadata": {
                **dict(record.get("metadata") or {}),
                "last_notification_id": (notification or {}).get("id", ""),
            },
        }
        recurrence = record.get("recurrence") or "none"
        if recurrence != "none":
            zone = zone_for(timezone_name)
            anchor = due
            following = next_due(now, recurrence,
                                 dict(record.get("recurrence_config") or {}),
                                 zone, anchor)
            if following is not None:
                patch["due_at"] = _iso(following)
                patch["status"] = "pending"
            else:
                patch["status"] = "missed"
        else:
            patch["status"] = "missed" if (late and kind != "timer") else "fired"
            if kind == "timer" and late:
                patch["status"] = "fired"
        updated = self.store.update(record["id"], patch,
                                    expected_status="pending")
        if updated is None:
            # A concurrent cancel/snooze/edit won. Remove the notification
            # this stale fire already created instead of showing a reminder
            # the user just cancelled.
            notification_id = (notification or {}).get("id", "")
            if notification_id:
                try:
                    from core import notification_service
                    owner = getattr(notifier, "__self__", None)
                    service = (owner if hasattr(owner, "dismiss")
                               else notification_service.get_service())
                    service.dismiss(notification_id,
                                    reason="reminder changed")
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass
            return
        if updated is not None:
            self._emit("fired", updated)
            self._write_log(
                f"SYS: {notification_title}"
                + (f" — {notification_body}" if notification_body else ""))

    # ── tool-facing operations ───────────────────────────────────────────────

    def create(self, *, kind: str = "reminder", title: str,
               description: str = "", priority: str = "normal",
               due: datetime, timezone_name: str = "",
               recurrence: str = "none",
               recurrence_config: Optional[Dict[str, Any]] = None,
               source: str = "user") -> Dict[str, Any]:
        title = _clean(title, MAX_TITLE)
        if not title:
            raise ScheduleError("a title is required")
        priority = str(priority or "normal").strip().casefold()
        if priority not in PRIORITIES:
            raise ScheduleError(
                "priority must be low, normal, high or urgent")
        zone = zone_for(timezone_name)
        due = due.astimezone(UTC)
        recurrence, config = normalize_recurrence(
            recurrence, recurrence_config or {}, due, zone)
        now = _now_utc()
        if recurrence == "none" and due <= now:
            raise ScheduleError(
                "that time is in the past; choose a future time")
        if recurrence != "none" and due <= now:
            due = next_due(now, recurrence, config, zone, due) or due
        now_text = _iso(now)
        record = {
            "id": "rem_" + uuid.uuid4().hex[:16],
            "kind": "timer" if kind == "timer" else "reminder",
            "title": title,
            "description": _clean(description, MAX_DESCRIPTION),
            "priority": priority,
            "status": "pending",
            "due_at": _iso(due),
            "timezone": zone_name(zone),
            "recurrence": recurrence,
            "recurrence_config": config,
            "created_at": now_text,
            "updated_at": now_text,
            "last_fired_at": "",
            "cancelled_at": "",
            "source": str(source or "user")[:40],
            "metadata": {},
        }
        if self.store.count(status="pending") >= MAX_REMINDERS:
            raise ScheduleError(
                f"the pending reminder limit ({MAX_REMINDERS}) has been reached")
        created = self.store.add(record)
        self._emit("created", created)
        self.wake()
        return created

    def get(self, reminder_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get(reminder_id)

    def find(self, query: str = "", reminder_id: str = "") -> List[Dict[str, Any]]:
        if reminder_id:
            record = self.store.get(reminder_id)
            return [record] if record is not None else []
        needle = str(query or "").strip().casefold()
        if not needle:
            return []
        return [record for record in self.store.list(status="", limit=MAX_REMINDERS)
                if needle in str(record.get("title", "")).casefold()
                or needle in str(record.get("description", "")).casefold()]

    def update(self, reminder_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        record = self.store.get(reminder_id)
        if record is None:
            raise ScheduleError("no reminder has that id")
        if record["status"] == "cancelled":
            raise ScheduleError("that reminder was cancelled")
        if (record["status"] in ("fired", "missed")
                and patch.get("due") is None
                and patch.get("recurrence") is None):
            raise ScheduleError(
                "that reminder already fired; set a new time to reschedule it")
        now = _now_utc()
        changes: Dict[str, Any] = {}
        zone = zone_for(patch.get("timezone_name") or record["timezone"])
        due = _parse_iso(patch.get("due_at") or record["due_at"])
        if patch.get("due") is not None:
            due = patch["due"].astimezone(UTC)
        recurrence = patch.get("recurrence", record["recurrence"])
        recurrence_config = patch.get(
            "recurrence_config", record.get("recurrence_config") or {})
        recurrence, config = normalize_recurrence(
            recurrence, recurrence_config, due, zone)
        if recurrence == "none" and due <= now and patch.get("due") is not None:
            raise ScheduleError("the new time is in the past")
        if (recurrence != "none" and due <= now
                and (patch.get("due") is not None
                     or patch.get("recurrence") is not None)):
            due = next_due(now, recurrence, config, zone, due) or due
        if patch.get("title") is not None:
            title = _clean(patch["title"], MAX_TITLE)
            if not title:
                raise ScheduleError("a title cannot be empty")
            changes["title"] = title
        if patch.get("description") is not None:
            changes["description"] = _clean(
                patch["description"], MAX_DESCRIPTION)
        if patch.get("priority") is not None:
            priority = str(patch["priority"]).strip().casefold()
            if priority not in PRIORITIES:
                raise ScheduleError(
                    "priority must be low, normal, high or urgent")
            changes["priority"] = priority
        changes.update({
            "due_at": _iso(due),
            "timezone": zone_name(zone),
            "recurrence": recurrence,
            "recurrence_config": config,
            "status": "pending",
            "updated_at": _iso(now),
        })
        updated = self.store.update(reminder_id, changes)
        if updated is not None:
            self._emit("updated", updated)
            self.wake()
        return updated or record

    def cancel(self, reminder_id: str) -> Dict[str, Any]:
        record = self.store.get(reminder_id)
        if record is None:
            raise ScheduleError("no reminder has that id")
        if record["status"] == "cancelled":
            return record
        now = _iso(_now_utc())
        updated = self.store.update(reminder_id, {
            "status": "cancelled", "cancelled_at": now,
            "updated_at": now})
        if updated is not None:
            self._emit("cancelled", updated)
            self.wake()
        return updated or record

    def snooze(self, reminder_id: str, minutes: int = 10) -> Dict[str, Any]:
        record = self.store.get(reminder_id)
        if record is None:
            raise ScheduleError("no reminder has that id")
        if record["status"] == "cancelled":
            raise ScheduleError("that reminder was cancelled")
        minutes = max(1, min(1440, int(minutes or 10)))
        due = _now_utc() + timedelta(minutes=minutes)
        updated = self.store.update(reminder_id, {
            "due_at": _iso(due),
            "status": "pending",
            "updated_at": _iso(_now_utc()),
            "metadata": {
                **dict(record.get("metadata") or {}),
                "snoozed_at": _iso(_now_utc()),
                "snoozed_minutes": minutes,
            },
        })
        if updated is not None:
            self._emit("snoozed", updated)
            self.wake()
        return updated or record

    def list(self, *, status: str = "", kind: str = "",
             from_iso: str = "", to_iso: str = "",
             limit: int = 50) -> List[Dict[str, Any]]:
        return self.store.list(status=status, kind=kind,
                               from_iso=from_iso, to_iso=to_iso,
                               limit=limit)


# ── process-wide instance ────────────────────────────────────────────────────
_SERVICE: Optional[ReminderScheduler] = None
_SERVICE_LOCK = threading.Lock()


def get_scheduler() -> ReminderScheduler:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = ReminderScheduler()
        return _SERVICE


def reset_scheduler(
        scheduler: Optional[ReminderScheduler] = None) -> ReminderScheduler:
    global _SERVICE
    with _SERVICE_LOCK:
        old = _SERVICE
        _SERVICE = scheduler or ReminderScheduler()
        if old is not None and old is not _SERVICE:
            old.stop()
        return _SERVICE

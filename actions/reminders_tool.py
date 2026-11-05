"""Reminders, timers and built-in notifications for Jarvis.

This module is the model-facing surface; the persistent scheduler and the
notification service own the real work.  The model supplies interpreted dates
and recurrence rules, never timezone arithmetic or next-occurrence logic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core import notification_service as _notifications
from core import scheduler as _scheduler
from core.scheduler import ScheduleError

UTC = timezone.utc
_ACTIONS = (
    "create_reminder", "create_timer", "create_notification",
    "list_reminders", "get_reminder", "update_reminder",
    "cancel_reminder", "snooze_reminder", "dismiss_notification",
    "list_notifications", "mark_notification_read",
)


def _service() -> _scheduler.ReminderScheduler:
    return _scheduler.get_scheduler()


def _fail(code: str, message: str, **fields: Any) -> Dict[str, Any]:
    print(f"[Reminders] {code}: {message}")
    result = {"ok": False, "success": False, "operation": "reminders",
              "error": message, "error_code": code}
    result.update(fields)
    return result


def _int(value: Any, default: int = 0, low: int = 0, high: int = 10**9) -> int:
    text = "" if value is None else str(value).strip()
    if text in ("", "0", "none", "null"):
        return default
    try:
        number = int(float(text))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _remaining_text(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" + (f" {seconds}s" if seconds else "")
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" + (f" {minutes}m" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days}d" + (f" {hours}h" if hours else "")


def _recurrence_text(record: Dict[str, Any], local: datetime) -> str:
    recurrence = str(record.get("recurrence") or "none")
    config = record.get("recurrence_config") or {}
    interval = max(1, int(config.get("interval") or 1))
    time_text = str(config.get("time") or local.strftime("%H:%M"))
    if recurrence == "none":
        return "one-time"
    if recurrence == "daily":
        return ("Every day" if interval == 1 else f"Every {interval} days")
    if recurrence == "weekly":
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]
        weekdays = config.get("weekdays") or [local.weekday()]
        days = ", ".join(names[int(day) % 7] for day in weekdays)
        prefix = "Every week" if interval == 1 else f"Every {interval} weeks"
        return f"{prefix} on {days} at {time_text}"
    if recurrence == "monthly":
        day = int(config.get("day_of_month") or local.day)
        prefix = "Every month" if interval == 1 else f"Every {interval} months"
        return f"{prefix} on day {day} at {time_text}"
    return f"Every {interval} {config.get('unit', 'minutes')}"


def _view(record: Dict[str, Any]) -> Dict[str, Any]:
    due = _scheduler._parse_iso(record.get("due_at"))
    zone = _scheduler.zone_for(record.get("timezone"))
    local = due.astimezone(zone)
    now = datetime.now(UTC)
    remaining = max(0.0, (due - now).total_seconds())
    return {
        "id": record.get("id", ""),
        "kind": record.get("kind", "reminder"),
        "title": record.get("title", ""),
        "description": record.get("description", ""),
        "priority": record.get("priority", "normal"),
        "status": record.get("status", ""),
        "due_at": record.get("due_at", ""),
        "due_at_local": local.isoformat(timespec="seconds"),
        "timezone": record.get("timezone", ""),
        "recurrence": record.get("recurrence", "none"),
        "recurrence_text": _recurrence_text(record, local),
        "remaining_seconds": int(remaining) if record.get("status") == "pending" else 0,
        "remaining_text": (_remaining_text(remaining)
                           if record.get("status") == "pending" else "—"),
        "created_at": record.get("created_at", ""),
        "last_fired_at": record.get("last_fired_at", ""),
        "snoozed_minutes": (record.get("metadata") or {}).get(
            "snoozed_minutes", 0),
    }


def _find_one(query: str = "", reminder_id: str = "") -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    service = _service()
    if reminder_id:
        record = service.get(reminder_id)
        if record is None:
            return None, _fail("not_found",
                               "No reminder has that id.")
        return record, None
    matches = service.find(query=query)
    if not matches:
        return None, _fail("not_found",
                           "No reminder matched that description.")
    if len(matches) > 1:
        return None, _fail(
            "ambiguous",
            "Several reminders match. Use the exact reminder_id.",
            candidates=[{"id": item["id"], "title": item["title"],
                         "due_at": item["due_at"]} for item in matches[:8]])
    return matches[0], None


def _parse_due(args: Dict[str, Any], zone_name_value: str) -> Tuple[datetime,
                                                                   List[str]]:
    zone = _scheduler.zone_for(zone_name_value)
    notes: List[str] = []
    raw_due = str(args.get("due_at") or "").strip()
    if raw_due:
        text = raw_due
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ScheduleError("due_at must be an ISO 8601 timestamp") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
            notes.append("The supplied date/time had no timezone; it was "
                         f"interpreted as {_scheduler.zone_name(zone)}.")
        return parsed.astimezone(UTC), notes
    seconds = (_int(args.get("in_seconds"), 0, 0, 10**8)
               + _int(args.get("in_minutes"), 0, 0, 10**6) * 60
               + _int(args.get("in_hours"), 0, 0, 10**5) * 3600)
    if seconds > 0:
        return datetime.now(UTC) + timedelta(seconds=seconds), notes
    date_text = str(args.get("date") or "").strip()
    time_text = str(args.get("time") or "").strip()
    if date_text:
        if not time_text:
            raise ScheduleError("include a time with the date, for example 09:30")
        try:
            date_value = datetime.strptime(date_text, "%Y-%m-%d").date()
            time_value = datetime.strptime(time_text, "%H:%M").time()
        except ValueError:
            raise ScheduleError("date must be YYYY-MM-DD and time must be HH:MM") from None
        return datetime.combine(date_value, time_value, tzinfo=zone).astimezone(UTC), notes
    if time_text:
        try:
            time_value = datetime.strptime(time_text, "%H:%M").time()
        except ValueError:
            raise ScheduleError("time must be HH:MM") from None
        local_now = datetime.now(zone)
        candidate = datetime.combine(local_now.date(), time_value, tzinfo=zone)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        notes.append("No date was supplied, so the next occurrence at that "
                     "local time was used.")
        return candidate.astimezone(UTC), notes
    raise ScheduleError(
        "a time is required: pass due_at, date + time, or a duration such as "
        "in_minutes")


def _recurrence_args(args: Dict[str, Any], due: datetime,
                     zone_name_value: str) -> Tuple[str, Dict[str, Any]]:
    recurrence = str(args.get("recurrence") or "none").strip().casefold()
    zone = _scheduler.zone_for(zone_name_value)
    local = due.astimezone(zone)
    config: Dict[str, Any] = {
        "time": str(args.get("time") or local.strftime("%H:%M")),
    }
    interval = _int(args.get("interval"), 1, 1, 100000)
    if recurrence == "daily":
        config["interval"] = interval
    elif recurrence == "weekly":
        weekdays = args.get("weekdays")
        if weekdays:
            config["weekdays"] = _scheduler._normalize_weekdays(weekdays)
        else:
            config["weekdays"] = [local.weekday()]
        config["interval"] = interval
    elif recurrence == "monthly":
        config["day_of_month"] = _int(
            args.get("day_of_month"), local.day, 1, 31)
        config["interval"] = interval
    elif recurrence == "custom":
        config["interval"] = interval
        config["unit"] = str(args.get("interval_unit") or "minutes").strip().casefold()
    return recurrence, config


def _create(args: Dict[str, Any], *, timer: bool = False) -> Dict[str, Any]:
    title = str(args.get("title") or "").strip()
    if not title:
        title = "Timer" if timer else ""
    if not title:
        return _fail("invalid_argument", "A reminder title is required.")
    zone_name_value = str(args.get("timezone") or "").strip()
    try:
        due, notes = _parse_due(args, zone_name_value)
        recurrence, config = _recurrence_args(
            args, due, zone_name_value or _scheduler.zone_name())
        record = _service().create(
            kind="timer" if timer else "reminder",
            title=title,
            description=str(args.get("description") or ""),
            priority=args.get("priority") or "normal",
            due=due,
            timezone_name=zone_name_value,
            recurrence="none" if timer else recurrence,
            recurrence_config={} if timer else config,
        )
    except ScheduleError as exc:
        return _fail("invalid_schedule", str(exc))
    view = _view(record)
    return {
        "ok": True,
        "success": True,
        "operation": "create_timer" if timer else "create_reminder",
        "message": (
            f"Timer set for {view['remaining_text']}."
            if timer else f"Reminder set for {view['due_at_local']}."),
        "reminder": view,
        "interpretation": notes,
    }


def _create_notification(args: Dict[str, Any]) -> Dict[str, Any]:
    title = str(args.get("title") or "").strip()
    if not title:
        return _fail("invalid_argument", "A notification title is required.")
    try:
        notification = _notifications.get_service().create(
            title=title,
            body=str(args.get("description") or args.get("body") or ""),
            kind=str(args.get("kind") or "system"),
            priority=args.get("priority") or "normal",
            source="tool",
        )
    except _notifications.NotificationError as exc:
        return _fail("invalid_argument", str(exc))
    return {
        "ok": True,
        "success": True,
        "operation": "create_notification",
        "message": "Notification created.",
        "notification": notification,
        "delivery": notification.get("delivery", {}),
    }


def _list(args: Dict[str, Any]) -> Dict[str, Any]:
    status = str(args.get("status") or "pending").strip().casefold()
    if status == "all":
        status = ""
    kind = str(args.get("kind") or "").strip().casefold()
    if kind not in ("", "reminder", "timer"):
        return _fail("invalid_argument", "kind must be reminder or timer.")
    limit = _int(args.get("limit"), 50, 1, 200)
    from_at = str(args.get("from_at") or "").strip()
    to_at = str(args.get("to_at") or "").strip()
    records = _service().list(status=status, kind=kind, from_iso=from_at,
                              to_iso=to_at, limit=limit)
    views = [_view(record) for record in records]
    return {
        "ok": True,
        "success": True,
        "operation": "list_reminders",
        "count": len(views),
        "reminders": views,
        "message": f"{len(views)} reminder(s).",
    }


def _get(args: Dict[str, Any]) -> Dict[str, Any]:
    record, error = _find_one(
        str(args.get("query") or ""), str(args.get("reminder_id") or ""))
    if error:
        return error
    return {"ok": True, "success": True, "operation": "get_reminder",
            "reminder": _view(record)}


def _update(args: Dict[str, Any]) -> Dict[str, Any]:
    record, error = _find_one(
        str(args.get("query") or ""), str(args.get("reminder_id") or ""))
    if error:
        return error
    patch: Dict[str, Any] = {}
    for key in ("title", "description", "priority"):
        value = args.get(key)
        if value is not None and str(value).strip():
            patch[key] = value
    zone_name_value = str(args.get("timezone") or record.get("timezone") or "")
    has_time_change = bool(
        args.get("due_at") or args.get("date") or args.get("time")
        or _int(args.get("in_seconds"), 0)
        or _int(args.get("in_minutes"), 0)
        or _int(args.get("in_hours"), 0))
    recurrence_value = str(args.get("recurrence") or "").strip().casefold()
    has_recurrence_change = bool(
        recurrence_value
        or args.get("weekdays")
        or _int(args.get("interval"), 0)
        or args.get("interval_unit")
        or _int(args.get("day_of_month"), 0))
    if has_time_change or has_recurrence_change:
        try:
            if has_time_change:
                due, _notes = _parse_due(args, zone_name_value)
                patch["due"] = due
            else:
                due = _scheduler._parse_iso(record["due_at"])
            if has_recurrence_change:
                recurrence, config = _recurrence_args(
                    args, due, zone_name_value)
                patch["recurrence"] = recurrence
                patch["recurrence_config"] = config
            if args.get("timezone"):
                patch["timezone_name"] = zone_name_value
        except ScheduleError as exc:
            return _fail("invalid_schedule", str(exc))
    if not patch:
        return _fail("invalid_argument", "Nothing was supplied to update.")
    try:
        updated = _service().update(record["id"], patch)
    except ScheduleError as exc:
        return _fail("invalid_schedule", str(exc))
    return {"ok": True, "success": True, "operation": "update_reminder",
            "reminder": _view(updated), "message": "Reminder updated."}


def _cancel(args: Dict[str, Any]) -> Dict[str, Any]:
    record, error = _find_one(
        str(args.get("query") or ""), str(args.get("reminder_id") or ""))
    if error:
        return error
    cancelled = _service().cancel(record["id"])
    return {"ok": True, "success": True, "operation": "cancel_reminder",
            "reminder": _view(cancelled), "message": "Reminder cancelled."}


def _snooze(args: Dict[str, Any]) -> Dict[str, Any]:
    record, error = _find_one(
        str(args.get("query") or ""), str(args.get("reminder_id") or ""))
    if error:
        return error
    minutes = _int(args.get("snooze_minutes"), 10, 1, 1440)
    if _int(args.get("snooze_seconds"), 0, 0, 86400):
        minutes = max(1, _int(args.get("snooze_seconds"), 0, 0, 86400) // 60 or 1)
    try:
        updated = _service().snooze(record["id"], minutes)
    except ScheduleError as exc:
        return _fail("invalid_argument", str(exc))
    return {"ok": True, "success": True, "operation": "snooze_reminder",
            "reminder": _view(updated),
            "message": f"Reminder snoozed for {minutes} minute(s)."}


def _dismiss_notification(args: Dict[str, Any]) -> Dict[str, Any]:
    notification_id = str(args.get("notification_id") or "").strip()
    if not notification_id:
        return _fail("invalid_argument", "notification_id is required.")
    try:
        notification = _notifications.get_service().dismiss(notification_id)
    except _notifications.NotificationError as exc:
        return _fail("not_found", str(exc))
    return {"ok": True, "success": True,
            "operation": "dismiss_notification",
            "notification": notification, "message": "Notification dismissed."}


def _list_notifications(args: Dict[str, Any]) -> Dict[str, Any]:
    service = _notifications.get_service()
    status = str(args.get("status") or "").strip().casefold()
    if status not in ("", "unread", "read", "dismissed"):
        return _fail("invalid_argument", "status must be unread, read or dismissed.")
    limit = _int(args.get("limit"), 20, 1, 100)
    rows = service.list(status=status, limit=limit,
                        unread_only=bool(args.get("unread_only")))
    return {"ok": True, "success": True,
            "operation": "list_notifications", "count": len(rows),
            "unread_count": service.unread_count(),
            "notifications": rows,
            "message": f"{len(rows)} notification(s)."}


def _mark_read(args: Dict[str, Any]) -> Dict[str, Any]:
    notification_id = str(args.get("notification_id") or "").strip()
    if not notification_id:
        return _fail("invalid_argument", "notification_id is required.")
    try:
        notification = _notifications.get_service().mark_read(notification_id)
    except _notifications.NotificationError as exc:
        return _fail("not_found", str(exc))
    return {"ok": True, "success": True,
            "operation": "mark_notification_read",
            "notification": notification, "message": "Notification marked read."}


def reminders(
    action: str,
    title: str = "",
    description: str = "",
    priority: str = "",
    reminder_id: str = "",
    notification_id: str = "",
    query: str = "",
    due_at: str = "",
    date: str = "",
    time: str = "",
    in_seconds: Any = 0,
    in_minutes: Any = 0,
    in_hours: Any = 0,
    recurrence: str = "",
    weekdays: Optional[List[str]] = None,
    interval: Any = 0,
    interval_unit: str = "",
    day_of_month: Any = 0,
    timezone: str = "",
    snooze_minutes: Any = 0,
    snooze_seconds: Any = 0,
    status: str = "",
    kind: str = "",
    from_at: str = "",
    to_at: str = "",
    limit: Any = 0,
    unread_only: Any = False,
) -> Dict[str, Any]:
    """Create and manage reminders, timers and built-in notifications.

    The scheduler computes recurrence and timezone transitions from the
    interpreted fields; callers never supply precomputed next occurrences.
    """
    operation = str(action or "").strip().casefold()
    if operation not in _ACTIONS:
        return _fail(
            "unsupported_action",
            "action must be one of: " + ", ".join(_ACTIONS) + ".")
    args = {
        "title": title, "description": description, "priority": priority,
        "reminder_id": reminder_id, "notification_id": notification_id,
        "query": query, "due_at": due_at, "date": date, "time": time,
        "in_seconds": in_seconds, "in_minutes": in_minutes,
        "in_hours": in_hours, "recurrence": recurrence,
        "weekdays": weekdays, "interval": interval,
        "interval_unit": interval_unit, "day_of_month": day_of_month,
        "timezone": timezone, "snooze_minutes": snooze_minutes,
        "snooze_seconds": snooze_seconds, "status": status, "kind": kind,
        "from_at": from_at, "to_at": to_at, "limit": limit,
        "unread_only": bool(unread_only),
    }
    try:
        if operation == "create_reminder":
            return _create(args)
        if operation == "create_timer":
            return _create(args, timer=True)
        if operation == "create_notification":
            return _create_notification(args)
        if operation == "list_reminders":
            return _list(args)
        if operation == "get_reminder":
            return _get(args)
        if operation == "update_reminder":
            return _update(args)
        if operation == "cancel_reminder":
            return _cancel(args)
        if operation == "snooze_reminder":
            return _snooze(args)
        if operation == "dismiss_notification":
            return _dismiss_notification(args)
        if operation == "list_notifications":
            return _list_notifications(args)
        if operation == "mark_notification_read":
            return _mark_read(args)
    except ScheduleError as exc:
        return _fail("invalid_schedule", str(exc))
    except Exception as exc:  # noqa: BLE001 - a tool must answer, not crash
        print(f"[Reminders] {operation} crashed: {type(exc).__name__}: {exc}")
        return _fail("failed", f"The reminder operation failed: "
                               f"{type(exc).__name__}: {exc}")
    return _fail("unsupported_action", "That reminder action is not supported.")

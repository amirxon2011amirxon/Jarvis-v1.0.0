"""Persistent reminders, timers, notifications and their integrations."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone

from actions import reminders_tool, tool_calling
from core import notification_service, scheduler
from core.scheduler import (
    ReminderScheduler,
    ScheduleError,
    _now_utc,
    next_due,
    zone_for,
)

UTC = timezone.utc


class TempServices(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="jarvis-reminders-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db = os.path.join(self.root, "reminders.db")
        self.events = []
        self.notifications = notification_service.NotificationService(self.db)
        self.notifications.configure(
            publish=self.events.append,
            desktop_notifier=lambda _title, _body: {
                "desktop": "unsupported", "error": ""},
        )
        self.scheduler = ReminderScheduler(self.db)
        self.scheduler.configure(
            notifier=self.notifications.create,
            log=lambda _message: None,
        )

    def tearDown(self) -> None:
        self.scheduler.stop()


class CreationTests(TempServices):
    def test_reminder_is_persistent_and_reloadable(self):
        due = _now_utc() + timedelta(minutes=30)
        created = self.scheduler.create(
            title="Study English", due=due, description="Vocabulary")
        self.assertEqual(created["status"], "pending")
        self.assertEqual(created["title"], "Study English")

        reopened = ReminderScheduler(self.db)
        try:
            stored = reopened.get(created["id"])
            self.assertIsNotNone(stored)
            self.assertEqual(stored["title"], "Study English")
            self.assertEqual(stored["timezone"], scheduler.zone_name())
        finally:
            reopened.stop()

    def test_timer_duration_is_calculated_from_real_elapsed_time(self):
        now = _now_utc()
        timer = self.scheduler.create(
            kind="timer", title="Tea", due=now + timedelta(seconds=30))
        self.assertEqual(timer["kind"], "timer")
        self.assertGreaterEqual(
            (scheduler._parse_iso(timer["due_at"]) - now).total_seconds(), 29)

    def test_fired_reminder_cannot_be_rearmed_by_a_title_edit(self):
        due = _now_utc() + timedelta(seconds=5)
        created = self.scheduler.create(title="Done", due=due)
        self.scheduler.tick(due + timedelta(seconds=10))
        self.assertEqual(self.scheduler.get(created["id"])["status"], "fired")
        with self.assertRaises(ScheduleError):
            self.scheduler.update(created["id"], {"title": "Renamed"})

    def test_cancel_during_a_fire_does_not_resurrect_the_reminder(self):
        notification_service.reset_service(self.notifications)
        self.addCleanup(notification_service.reset_service)
        due = _now_utc() + timedelta(seconds=5)
        created = self.scheduler.create(title="Race", due=due)
        original = self.scheduler._notifier

        def cancel_while_notifying(**kwargs):
            note = original(**kwargs)
            self.scheduler.cancel(created["id"])
            return note

        self.scheduler.configure(notifier=cancel_while_notifying)
        self.scheduler.tick(due + timedelta(seconds=10))
        self.assertEqual(self.scheduler.get(created["id"])["status"],
                         "cancelled")
        notes = [item for item in self.notifications.list()
                 if item["title"] == "Reminder: Race"]
        self.assertTrue(notes)
        self.assertEqual(notes[0]["status"], "dismissed")

    def test_apple_script_escaping_prevents_string_breakout(self):
        payload = '\\" & (do shell script "touch /tmp/injected") & "'
        escaped = notification_service._apple_escape(payload)
        # Every quote must be preceded by an odd number of backslashes, so it
        # stays inside the AppleScript string literal.
        for index, character in enumerate(escaped):
            if character != '"':
                continue
            count = 0
            cursor = index - 1
            while cursor >= 0 and escaped[cursor] == "\\":
                count += 1
                cursor -= 1
            self.assertEqual(count % 2, 1, escaped)

    def test_invalid_and_past_schedules_are_rejected(self):
        with self.assertRaises(ScheduleError):
            self.scheduler.create(title="Past", due=_now_utc() - timedelta(minutes=1))
        with self.assertRaises(ScheduleError):
            self.scheduler.create(title="",
                                  due=_now_utc() + timedelta(minutes=1))
        with self.assertRaises(ScheduleError):
            self.scheduler.create(
                title="Bad recurring", due=_now_utc() + timedelta(minutes=1),
                recurrence="sometimes")
    def test_cancel_update_and_snooze_remove_stale_work(self):
        due = _now_utc() + timedelta(minutes=30)
        reminder = self.scheduler.create(title="Old", due=due)
        updated = self.scheduler.update(
            reminder["id"], {"title": "New", "priority": "high"})
        self.assertEqual(updated["title"], "New")
        self.assertEqual(updated["priority"], "high")

        snoozed = self.scheduler.snooze(reminder["id"], minutes=15)
        self.assertEqual(snoozed["status"], "pending")
        remaining = (scheduler._parse_iso(snoozed["due_at"]) - _now_utc())
        self.assertGreater(remaining.total_seconds(), 13 * 60)

        cancelled = self.scheduler.cancel(reminder["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.scheduler.list(status="pending"), [])


class RecurrenceTests(TempServices):
    def test_daily_and_weekly_next_occurrence(self):
        zone = zone_for("UTC")
        anchor = datetime(2026, 1, 5, 19, 0, tzinfo=UTC)  # Monday
        daily = next_due(anchor + timedelta(minutes=1), "daily",
                         {"interval": 1, "time": "19:00"}, zone, anchor)
        self.assertEqual(daily, datetime(2026, 1, 6, 19, 0, tzinfo=UTC))

        weekly = next_due(
            anchor + timedelta(minutes=1), "weekly",
            {"interval": 1, "weekdays": [0, 3], "time": "19:00"}, zone, anchor)
        self.assertEqual(weekly, datetime(2026, 1, 8, 19, 0, tzinfo=UTC))

    def test_monthly_clamps_to_the_last_valid_day(self):
        zone = zone_for("UTC")
        anchor = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
        following = next_due(
            anchor + timedelta(minutes=1), "monthly",
            {"interval": 1, "day_of_month": 31, "time": "09:00"}, zone, anchor)
        self.assertEqual(following, datetime(2026, 2, 28, 9, 0, tzinfo=UTC))

    def test_timezone_conversion_and_dst(self):
        tokyo = zone_for("Asia/Tokyo")
        utc = timezone.utc
        anchor = datetime(2026, 3, 1, 0, 0, tzinfo=utc)
        # 09:00 in Tokyo is 00:00 UTC.
        following = next_due(
            datetime(2026, 3, 1, 1, 0, tzinfo=utc), "daily",
            {"interval": 1, "time": "09:00"}, tokyo, anchor)
        self.assertEqual(following, datetime(2026, 3, 2, 0, 0, tzinfo=utc))

        new_york = zone_for("America/New_York")
        anchor = datetime(2026, 3, 7, 14, 0, tzinfo=utc)  # 09:00 EST
        after_dst = next_due(
            datetime(2026, 3, 8, 14, 0, tzinfo=utc), "daily",
            {"interval": 1, "time": "09:00"}, new_york, anchor)
        # On 9 March New York is EDT, so 09:00 local is 13:00 UTC.
        self.assertEqual(after_dst, datetime(2026, 3, 9, 13, 0, tzinfo=utc))

    def test_custom_interval(self):
        zone = zone_for("UTC")
        anchor = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        following = next_due(
            anchor + timedelta(minutes=1), "custom",
            {"interval": 2, "unit": "hours"}, zone, anchor)
        self.assertEqual(following, datetime(2026, 1, 1, 14, 0, tzinfo=UTC))


class FiringTests(TempServices):
    def test_due_reminder_creates_one_notification(self):
        now = _now_utc()
        reminder = self.scheduler.create(
            title="Ping", due=now + timedelta(seconds=1))
        self.assertEqual(self.scheduler.tick(now + timedelta(seconds=2)), 1)
        self.assertEqual(self.scheduler.get(reminder["id"])["status"], "fired")
        rows = self.notifications.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Reminder: Ping")
        self.assertEqual(rows[0]["status"], "unread")
        self.assertEqual(rows[0]["delivery"]["dashboard"], "emitted")
        self.assertEqual(rows[0]["delivery"]["desktop"], "unsupported")
        # A second tick cannot duplicate a completed one-time reminder.
        self.assertEqual(self.scheduler.tick(now + timedelta(seconds=5)), 0)
        self.assertEqual(len(self.notifications.list()), 1)

    def test_timer_fires_and_reports_elapsed_downtime(self):
        now = _now_utc()
        timer = self.scheduler.create(
            kind="timer", title="Oven", due=now + timedelta(seconds=1))
        self.scheduler.tick(now + timedelta(minutes=5), startup=True)
        row = self.scheduler.get(timer["id"])
        self.assertEqual(row["status"], "fired")
        note = self.notifications.list()[0]
        self.assertEqual(note["kind"], "timer")
        self.assertIn("closed", note["title"].casefold())

    def test_missed_reminder_is_marked_and_reported(self):
        now = _now_utc()
        reminder = self.scheduler.create(
            title="Late", due=now + timedelta(seconds=1))
        self.scheduler.tick(now + timedelta(minutes=10), startup=True)
        row = self.scheduler.get(reminder["id"])
        self.assertEqual(row["status"], "missed")
        self.assertIn("Missed reminder", self.notifications.list()[0]["title"])

    def test_recurring_advances_instead_of_duplicating(self):
        now = _now_utc()
        reminder = self.scheduler.create(
            title="Daily", due=now + timedelta(seconds=1),
            recurrence="daily",
            recurrence_config={"time": now.strftime("%H:%M")})
        self.scheduler.tick(now + timedelta(seconds=3))
        row = self.scheduler.get(reminder["id"])
        self.assertEqual(row["status"], "pending")
        self.assertGreater(scheduler._parse_iso(row["due_at"]), now)
        self.assertEqual(len(self.notifications.list()), 1)
        self.assertEqual(self.scheduler.tick(now + timedelta(seconds=4)), 0)

    def test_dedupe_key_prevents_duplicate_notifications(self):
        first = self.notifications.create(
            title="One", dedupe_key="same")
        second = self.notifications.create(
            title="Two", dedupe_key="same")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.notifications.list()), 1)

    def test_scheduler_thread_starts_fires_and_stops_cleanly(self):
        now = _now_utc()
        reminder = self.scheduler.create(
            title="Threaded", due=now + timedelta(seconds=0.2))
        self.scheduler.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.scheduler.get(reminder["id"])["status"] != "pending":
                break
            time.sleep(0.02)
        self.scheduler.stop()
        self.assertEqual(self.scheduler.get(reminder["id"])["status"], "fired")
        self.assertEqual(len(self.notifications.list()), 1)

    def test_database_migration_is_idempotent(self):
        reopened = ReminderScheduler(self.db)
        try:
            self.assertEqual(reopened.store.count(), 0)
            with closing(sqlite3.connect(self.db)) as connection:
                version = connection.execute(
                    "SELECT MAX(version) FROM reminder_migrations"
                ).fetchone()[0]
            self.assertEqual(version, 1)
        finally:
            reopened.stop()


class NotificationTests(TempServices):
    def test_history_read_dismiss_and_snooze(self):
        note = self.notifications.create(title="Hello", body="World")
        self.assertEqual(self.notifications.unread_count(), 1)
        read = self.notifications.mark_read(note["id"])
        self.assertEqual(read["status"], "read")
        self.assertEqual(self.notifications.unread_count(), 0)

        second = self.notifications.create(title="Again")
        dismissed = self.notifications.dismiss(second["id"])
        self.assertEqual(dismissed["status"], "dismissed")
        self.assertEqual(len(self.notifications.list()), 2)
        self.assertEqual(len(self.notifications.list(status="dismissed")), 1)

    def test_snooze_notification_moves_the_reminder(self):
        now = _now_utc()
        reminder = self.scheduler.create(
            title="Snooze me", due=now + timedelta(seconds=1))
        self.scheduler.tick(now + timedelta(seconds=2))
        note = self.notifications.list()[0]
        from core import scheduler as scheduler_module
        scheduler_module.reset_scheduler(self.scheduler)
        try:
            self.notifications.snooze(note["id"], 15)
        finally:
            scheduler_module.reset_scheduler()
        row = self.scheduler.get(reminder["id"])
        self.assertEqual(row["status"], "pending")
        self.assertGreater(scheduler._parse_iso(row["due_at"]), now)

    def test_desktop_failure_never_hides_dashboard_notification(self):
        self.notifications.configure(
            desktop_notifier=lambda _t, _b: {
                "desktop": "failed", "error": "no toast service"})
        note = self.notifications.create(title="Desktop fail")
        self.assertEqual(note["status"], "unread")
        self.assertEqual(note["delivery"]["desktop"], "failed")
        self.assertEqual(note["delivery"]["dashboard"], "emitted")
        self.assertEqual(len(self.notifications.list()), 1)

    def test_notification_events_are_published(self):
        self.notifications.create(title="Event me")
        types = [event["type"] for event in self.events]
        self.assertIn("notification.created", types)


class ToolTests(TempServices):
    def setUp(self) -> None:
        super().setUp()
        scheduler.reset_scheduler(self.scheduler)
        notification_service.reset_service(self.notifications)
        self.addCleanup(scheduler.reset_scheduler)
        self.addCleanup(notification_service.reset_service)

    def test_registered_and_declared(self):
        self.assertIn("reminders", tool_calling.get_tool_names())
        declaration = {
            item["name"]: item
            for item in tool_calling.get_tool_declarations(enabled_only=True)
        }["reminders"]
        self.assertEqual(declaration["parameters"]["required"], ["action"])

    def test_create_list_get_snooze_and_cancel(self):
        created = reminders_tool.reminders(
            "create_reminder", title="English", in_minutes=20)
        self.assertTrue(created["ok"])
        reminder_id = created["reminder"]["id"]
        listed = reminders_tool.reminders("list_reminders")
        self.assertEqual(listed["count"], 1)
        got = reminders_tool.reminders(
            "get_reminder", reminder_id=reminder_id)
        self.assertTrue(got["ok"])
        snoozed = reminders_tool.reminders(
            "snooze_reminder", reminder_id=reminder_id, snooze_minutes=5)
        self.assertTrue(snoozed["ok"])
        cancelled = reminders_tool.reminders(
            "cancel_reminder", query="English")
        self.assertTrue(cancelled["ok"])
        self.assertEqual(cancelled["reminder"]["status"], "cancelled")

    def test_update_title_keeps_the_existing_schedule_and_recurrence(self):
        created = reminders_tool.reminders(
            "create_reminder", title="Daily stand", in_minutes=30,
            recurrence="daily")
        self.assertTrue(created["ok"])
        reminder = created["reminder"]
        updated = reminders_tool.reminders(
            "update_reminder", reminder_id=reminder["id"],
            title="Daily stand-up")
        self.assertTrue(updated["ok"], updated)
        self.assertEqual(updated["reminder"]["title"], "Daily stand-up")
        self.assertEqual(updated["reminder"]["recurrence"], "daily")
        self.assertEqual(updated["reminder"]["due_at"], reminder["due_at"])

    def test_ambiguous_title_returns_candidates(self):
        reminders_tool.reminders("create_reminder", title="Study English",
                                 in_minutes=10)
        reminders_tool.reminders("create_reminder", title="Study Maths",
                                 in_minutes=20)
        result = reminders_tool.reminders(
            "cancel_reminder", query="Study")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)

    def test_notifications_tool_history_and_read(self):
        created = reminders_tool.reminders(
            "create_notification", title="Hello", description="Body")
        self.assertTrue(created["ok"])
        note_id = created["notification"]["id"]
        listed = reminders_tool.reminders("list_notifications")
        self.assertEqual(listed["count"], 1)
        read = reminders_tool.reminders(
            "mark_notification_read", notification_id=note_id)
        self.assertTrue(read["ok"])
        dismissed = reminders_tool.reminders(
            "dismiss_notification", notification_id=note_id)
        self.assertTrue(dismissed["ok"])


class AgentNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="jarvis-agent-notes-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.service = notification_service.reset_service(
            notification_service.NotificationService(
                os.path.join(self.root, "notifications.db")))
        self.addCleanup(notification_service.reset_service)

    def _notify(self, event, **run):
        import main

        live = main.JarvisLive.__new__(main.JarvisLive)
        payload = {
            "run_id": "run-1",
            "objective": "Check the system",
            "status": "in_progress",
            "current_step": "Working",
            "result_summary": "",
            "last_error": "",
            "last_tool_summary": "",
        }
        payload.update(run)
        live._notify_agent_run_event({"event": event, "run": payload}, payload)

    def test_completion_failure_and_attention_are_notified_once(self):
        self._notify("completed", status="completed",
                     result_summary="All checked")
        self._notify("completed", status="completed",
                     result_summary="All checked")
        self._notify("failed", status="failed", last_error="Broken")
        self._notify("needs_user", status="needs_user",
                     current_step="Approval required")
        titles = [row["title"] for row in self.service.list()]
        self.assertEqual(titles.count("Task completed"), 1)
        self.assertIn("Task failed", titles)
        self.assertIn("Task needs your attention", titles)

    def test_cancelled_or_ordinary_progress_does_not_notify(self):
        self._notify("tool", status="in_progress")
        self._notify("cancelled", status="cancelled")
        self.assertEqual(self.service.list(), [])


if __name__ == "__main__":
    unittest.main()

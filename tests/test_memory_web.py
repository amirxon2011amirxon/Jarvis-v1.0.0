"""HTTP boundary tests for the memory interface.

The memory database is a temporary file; the real Jarvis database is never
opened or modified.
"""
from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import config
from core import audio_devices
from core import memory as memory_module
from web.events import EventHub
from web.runtime import AzeRuntime
from web.server import create_app

BASE = "http://127.0.0.1:8765"


class FakeMetrics:
    def snapshot(self):
        return {"cpu": 1.0, "mem": 2.0, "net": 0.0, "gpu": -1.0, "tmp": -1.0}

    def stop(self):
        pass


class FakeWeather:
    def get(self, force=False):
        return {"available": False}


class MemoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-memory-api-")
        self.addCleanup(self.temp.cleanup)
        memory_module.reset_default_service()
        self.addCleanup(memory_module.reset_default_service)

        root = Path(self.temp.name)
        self.settings = {
            "enabled": True,
            "auto_extract": False,
            "retrieval_enabled": True,
            "voice_injection": False,
            "max_retrieved": 6,
            "max_context_chars": 1400,
            "search_limit": 40,
            "backup_retention": 2,
            "expiry_days": 0,
            "db_path": root / "memory.db",
            "backup_dir": root / "backups",
        }
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(
            config, "get_memory_settings", return_value=dict(self.settings)))
        self.stack.enter_context(mock.patch.object(
            audio_devices, "list_devices",
            side_effect=lambda kind, refresh=False: []))
        self.stack.enter_context(mock.patch.object(
            audio_devices, "get_output_volume", return_value=None))
        self.stack.enter_context(mock.patch.object(
            audio_devices, "prefetch", return_value=None))
        self.stack.enter_context(mock.patch.object(
            audio_devices, "configure", return_value=None))

        self.runtime = AzeRuntime(
            hub=EventHub(), start_session=False,
            weather_service=FakeWeather(), metrics=FakeMetrics())
        self.client = TestClient(
            create_app(self.runtime,
                       frontend_dir=Path(self.temp.name) / "no-dist"),
            base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def seed(self, content="The user prefers concise replies.",
             memory_type="user", **kwargs):
        return self.runtime._get_memory_service().remember(
            content, memory_type=memory_type, **kwargs)

    def test_empty_state_is_an_honest_empty_result(self):
        stats = self.client.get("/api/memory/stats")
        self.assertEqual(stats.status_code, 200)
        self.assertEqual(stats.json()["data"]["total"], 0)
        listed = self.client.get("/api/memory").json()
        self.assertEqual(listed["data"]["memories"], [])
        self.assertEqual(listed["data"]["total"], 0)
        self.assertEqual(self.client.get("/api/memory/nope").status_code, 404)

    def test_list_get_update_delete(self):
        created = self.seed()
        memory_id = created.memory.id
        listed = self.client.get("/api/memory?type=user&query=concise").json()
        self.assertEqual(listed["data"]["total"], 1)
        detail = self.client.get(f"/api/memory/{memory_id}").json()
        self.assertEqual(detail["memory"]["content"],
                         "The user prefers concise replies.")
        updated = self.client.patch(
            f"/api/memory/{memory_id}",
            json={"content": "The user prefers very concise replies.",
                  "expected_version": detail["memory"]["version"],
                  "importance": 0.9})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["memory"]["importance"], 0.9)
        conflict = self.client.patch(
            f"/api/memory/{memory_id}",
            json={"content": "stale", "expected_version": 1})
        self.assertEqual(conflict.status_code, 409)
        deleted = self.client.delete(
            f"/api/memory/{memory_id}",
            headers={"Content-Type": "application/json"})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/memory/{memory_id}").status_code, 404)

    def test_search_punctuation_is_safe(self):
        self.seed("The C++ API returns beta results.", memory_type="knowledge")
        response = self.client.get(
            "/api/memory", params={"query": 'C++ "beta" NEAR(x)'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 1)

    def test_export_backup_maintenance_and_import(self):
        self.seed("Portable fact.", memory_type="knowledge")
        exported = self.client.get("/api/memory/export")
        self.assertEqual(exported.status_code, 200)
        payload = exported.json()
        self.assertEqual(payload["format"], "jarvis-memory-export")
        self.assertEqual(len(payload["memories"]), 1)

        backup = self.client.post(
            "/api/memory/backup",
            headers={"Content-Type": "application/json"})
        self.assertEqual(backup.status_code, 200)
        self.assertTrue(backup.json()["ok"])
        self.assertTrue(Path(backup.json()["data"]["path"]).is_file())

        maintenance = self.client.post(
            "/api/memory/maintenance",
            headers={"Content-Type": "application/json"})
        self.assertEqual(maintenance.status_code, 200)
        self.assertTrue(maintenance.json()["ok"])

        dry = self.client.post(
            "/api/memory/import", json={"data": payload, "dry_run": True})
        self.assertEqual(dry.status_code, 200)
        self.assertEqual(dry.json()["data"]["would_import"], 1)
        # Nothing was applied by the dry run.
        self.assertEqual(
            self.client.get("/api/memory/stats").json()["data"]["total"], 1)

        applied = self.client.post("/api/memory/import", json={"data": payload})
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.json()["data"]["imported"], 0)
        self.assertEqual(applied.json()["data"]["skipped"], 1)

    def test_invalid_import_is_a_validation_error(self):
        response = self.client.post(
            "/api/memory/import", json={"data": {"format": "wrong"}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            self.client.get("/api/memory/stats").json()["data"]["total"], 0)

    def test_settings_endpoint_reports_real_configuration(self):
        response = self.client.get("/api/memory/settings")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["enabled"])
        self.assertFalse(body["auto_extract"])
        self.assertTrue(body["db_path"].endswith("memory.db"))


if __name__ == "__main__":
    unittest.main()

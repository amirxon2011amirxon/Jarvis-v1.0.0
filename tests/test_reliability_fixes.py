"""Reliability fixes: app catalogue pagination and truthful preflight."""

from __future__ import annotations

import json
import importlib.util
import unittest
from types import SimpleNamespace
from unittest import mock

from actions import open_app_tool, phone_call_tool, tool_calling
from actions import youtube_player_tool
from web import runtime as runtime_module
from web.runtime import AzeRuntime


def entry(name: str, launch: str = "") -> dict:
    return {"name": name, "launch": launch or name, "kind": "shortcut"}


class ListAppsPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.win = mock.patch.object(open_app_tool, "_IS_WIN", True)
        self.win.start()
        self.addCleanup(self.win.stop)

    def _list(self, entries, *args, **kwargs):
        with mock.patch.object(open_app_tool, "_index", return_value=entries):
            return open_app_tool.list_apps(*args, **kwargs)

    def test_declaration_exposes_query_and_pagination(self):
        info = tool_calling.TOOL_REGISTRY["list_apps"]
        properties = info["parameters"]["properties"]
        self.assertIn("query", properties)
        self.assertIn("offset", properties)
        self.assertIn("limit", properties)
        self.assertEqual(info["parameters"]["required"], [])

    def test_full_catalogue_pages_beyond_fifty(self):
        entries = [entry(f"App {index:03d}") for index in range(120)]
        first = self._list(entries, limit=50)
        self.assertEqual(first["total"], 120)
        self.assertEqual(first["count"], 50)
        self.assertEqual(first["offset"], 0)
        self.assertEqual(first["limit"], 50)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 50)
        self.assertEqual(first["apps"][0], "App 000")

        middle = self._list(entries, offset=50, limit=50)
        self.assertEqual(middle["count"], 50)
        self.assertEqual(middle["apps"][0], "App 050")
        self.assertEqual(middle["next_offset"], 100)

        final = self._list(entries, offset=100, limit=50)
        self.assertEqual(final["count"], 20)
        self.assertEqual(final["apps"][0], "App 100")
        self.assertFalse(final["has_more"])
        self.assertIsNone(final["next_offset"])

    def test_empty_and_past_end_pages_are_accurate(self):
        entries = [entry(f"App {index}") for index in range(3)]
        empty = self._list(entries, offset=50, limit=50)
        self.assertEqual(empty["apps"], [])
        self.assertEqual(empty["count"], 0)
        self.assertEqual(empty["total"], 3)
        self.assertFalse(empty["has_more"])
        self.assertIsNone(empty["next_offset"])

    def test_query_pagination_and_duplicate_collapsing(self):
        entries = [entry(f"Tool {index:02d}") for index in range(30)]
        entries.append(entry("Tool 00", launch="other"))
        entries.append(entry("Мой инструмент"))
        first = self._list(entries, "Tool")
        self.assertEqual(first["total"], 30)  # duplicate collapsed
        self.assertEqual(first["count"], 15)
        self.assertEqual(first["next_offset"], 15)
        second = self._list(entries, "Tool", offset=15)
        self.assertEqual(second["count"], 15)
        self.assertFalse(second["has_more"])

        localized = self._list(entries, "Мой")
        self.assertEqual(localized["apps"], ["Мой инструмент"])

    def test_default_catalogue_is_bounded_not_unlimited(self):
        entries = [entry(f"App {index:03d}") for index in range(120)]
        result = self._list(entries)
        self.assertEqual(result["count"], 50)
        self.assertEqual(result["total"], 120)
        self.assertTrue(result["has_more"])


class _FakeTelephony:
    def __init__(self, settings, registration):
        self._settings = settings
        self._registration = registration

    def settings(self):
        return dict(self._settings)

    def registration(self):
        return dict(self._registration)


class GeminiKeyCacheTests(unittest.TestCase):
    def test_reset_key_cache_forces_a_fresh_read(self):
        from core import gemini

        old = gemini._cached_key
        self.addCleanup(setattr, gemini, "_cached_key", old)
        gemini._cached_key = "retired-key"
        gemini.reset_key_cache()
        self.assertIsNone(gemini._cached_key)


class PhoneReadinessTests(unittest.TestCase):
    def _readiness(self, settings, registration=None):
        fake = _FakeTelephony(settings, registration or {})
        with mock.patch.object(phone_call_tool, "_service", return_value=fake):
            return phone_call_tool.readiness()

    def test_missing_password_is_missing_credentials(self):
        result = self._readiness({
            "username": "jarvis", "password": "", "server": "sip.example",
            "identity": "sip:jarvis@sip.example", "transport": "TLS",
            "port": 5061,
        })
        self.assertEqual(result["status"], "missing_credentials")
        self.assertFalse(result["configured"])
        self.assertNotIn("password=", result["detail"].casefold())

    def test_invalid_configuration_is_detected(self):
        result = self._readiness({
            "username": "jarvis", "password": "secret",
            "server": "bad server/url", "identity": "nope",
            "transport": "UDP", "port": 70000,
        })
        self.assertEqual(result["status"], "invalid_configuration")
        self.assertIn("invalid", result["detail"].casefold())

    def test_missing_audio_dependency_is_explicit(self):
        settings = {
            "username": "jarvis", "password": "secret",
            "server": "sip.example", "identity": "sip:jarvis@sip.example",
            "transport": "TLS", "port": 5061,
        }
        real_find = importlib.util.find_spec

        def find(name, *args, **kwargs):
            if name == "pylibsrtp":
                return None
            return real_find(name, *args, **kwargs)

        with mock.patch("importlib.util.find_spec", side_effect=find):
            result = self._readiness(settings)
        self.assertEqual(result["status"], "missing_dependency")
        self.assertIn("pylibsrtp", result["detail"])

    def test_registered_is_ready_and_failure_is_redacted(self):
        settings = {
            "username": "jarvis", "password": "secret",
            "server": "sip.example", "identity": "sip:jarvis@sip.example",
            "transport": "TLS", "port": 5061,
        }
        ready = self._readiness(settings, {"state": "registered"})
        self.assertEqual(ready["status"], "ready")

        failed = self._readiness(
            settings,
            {"state": "failed", "last_error": "password=supersecret denied"})
        self.assertEqual(failed["status"], "service_unavailable")
        self.assertNotIn("supersecret", failed["detail"])
        self.assertIn("[redacted]", failed["detail"])

    def test_configured_without_verified_registration_is_not_called_ready(self):
        settings = {
            "username": "jarvis", "password": "secret",
            "server": "sip.example", "identity": "sip:jarvis@sip.example",
            "transport": "TLS", "port": 5061,
        }
        result = self._readiness(settings, {"state": "idle"})
        self.assertEqual(result["status"], "configured_unverified")
        self.assertNotIn("ready", result["status"])


class YouTubeReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        with youtube_player_tool._READINESS_LOCK:
            youtube_player_tool._READINESS_STATE.update(
                {"status": "", "detail": "", "at": 0.0})

    def test_missing_key(self):
        with mock.patch.object(youtube_player_tool, "_api_key", return_value=""):
            result = youtube_player_tool.readiness()
        self.assertEqual(result["status"], "missing_credentials")
        self.assertFalse(result["configured"])

    def test_malformed_key(self):
        with mock.patch.object(youtube_player_tool, "_api_key",
                               return_value="short key"):
            result = youtube_player_tool.readiness()
        self.assertEqual(result["status"], "invalid_configuration")

    def test_key_alone_is_not_called_valid(self):
        key = "AIza" + "x" * 35
        with mock.patch.object(youtube_player_tool, "_api_key",
                               return_value=key):
            result = youtube_player_tool.readiness()
        self.assertEqual(result["status"], "configured_unverified")
        self.assertNotIn(key, json.dumps(result))

    def test_verified_success_and_failure(self):
        key = "AIza" + "x" * 35
        youtube_player_tool._note_readiness("ready", "API answered", key=key)
        with mock.patch.object(youtube_player_tool, "_api_key",
                               return_value=key):
            result = youtube_player_tool.readiness()
        self.assertEqual(result["status"], "ready")
        self.assertNotIn(key, json.dumps(result))

        youtube_player_tool._note_readiness(
            "service_unavailable", "HTTP 503 from YouTube", key=key)
        with mock.patch.object(youtube_player_tool, "_api_key",
                               return_value=key):
            result = youtube_player_tool.readiness()
        self.assertEqual(result["status"], "service_unavailable")
        self.assertIn("503", result["detail"])

    def test_a_different_key_invalidates_the_verified_state(self):
        first = "AIza" + "a" * 35
        second = "AIza" + "b" * 35
        youtube_player_tool._note_readiness("ready", "API answered", key=first)
        with mock.patch.object(youtube_player_tool, "_api_key",
                               return_value=second):
            result = youtube_player_tool.readiness()
        self.assertEqual(result["status"], "configured_unverified")


class DiagnosticsCapabilityTests(unittest.TestCase):
    def test_capability_readiness_never_raises_and_returns_secret_free_rows(self):
        runtime = AzeRuntime.__new__(AzeRuntime)
        phone_module = SimpleNamespace(
            readiness=lambda: {"capability": "phone", "status": "ready",
                               "detail": "SIP registration is active."})
        youtube_module = SimpleNamespace(
            readiness=lambda: {"capability": "youtube",
                               "status": "missing_credentials",
                               "detail": "No key."})

        def module(name):
            return {"phone_call": phone_module,
                    "youtube_player": youtube_module}.get(name)

        with mock.patch.object(runtime_module, "get_tool_module",
                               side_effect=module):
            result = runtime.capability_readiness()
        self.assertEqual(result["phone"]["status"], "ready")
        self.assertEqual(result["youtube"]["status"], "missing_credentials")
        self.assertNotIn("AIza", json.dumps(result))


if __name__ == "__main__":
    unittest.main()

"""HTTP/WebSocket boundary tests for the local web interface.

These tests never start a Gemini session, never open a device and never touch
the user's real config file.  Settings tests use a temporary api_keys.json.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import config
from core import approvals
from core import audio_devices
from core import notification_service
from web.events import EventHub
from web.runtime import AzeRuntime
from web.server import create_app

BASE = "http://127.0.0.1:8765"


class FakeMetrics:
    def snapshot(self):
        return {"cpu": 12.0, "mem": 34.0, "net": 0.5, "gpu": -1.0, "tmp": -1.0}

    def stop(self):
        pass


class FakeWeather:
    def __init__(self):
        self.calls = 0

    def get(self, force=False):
        self.calls += 1
        return {
            "available": True,
            "location": "Tashkent",
            "temperature_c": 21.4,
            "apparent_c": 21.0,
            "humidity": 40,
            "wind_kmh": 8,
            "weather_code": 0,
            "is_day": True,
            "fetched_at": "2026-10-24T12:00:00+00:00",
        }


class FakeEngine:
    def __init__(self):
        self.session = object()
        self.model = "models/gemini-test-live"
        self.sent = []
        self.interrupted = 0
        self.reconnects = []
        self.stopped = 0

    def send_text(self, text):
        self.sent.append(text)
        return True

    def interrupt(self):
        self.interrupted += 1

    def request_reconnect(self, keep_context=True, reason=""):
        self.reconnects.append((keep_context, reason))

    def set_push_to_talk(self, enabled):
        return "global"

    def stop(self):
        self.stopped += 1


def make_runtime(**kwargs):
    return AzeRuntime(
        hub=EventHub(),
        start_session=False,
        weather_service=FakeWeather(),
        metrics=FakeMetrics(),
        **kwargs,
    )


@contextmanager
def isolated_config(values):
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        path = directory / "api_keys.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        with mock.patch.object(config, "CONFIG_DIR", directory), \
             mock.patch.object(config, "CONFIG_FILE", path):
            yield path


TEST_CONFIG = {
    "gemini_api_key": "test-key-abcdefghijklmnop",
    "os_system": "windows",
    "setup_done": True,
    "assistant_name": "Aze",
    "user_name": "",
    "ui_color": "#ffb02e",
    "voice_name": "Charon",
    "push_to_talk_enabled": False,
    "proactive_audio": True,
    "thinking_enabled": False,
}


class AudioPatchesMixin:
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(
            audio_devices, "list_devices",
            side_effect=lambda kind, refresh=False: (
                ["Mic One", "Mic Two"] if kind == "input" else ["Speaker One"]
            )))
        self.stack.enter_context(mock.patch.object(
            audio_devices, "get_output_volume", return_value=42))
        self.stack.enter_context(mock.patch.object(
            audio_devices, "set_output_volume", return_value=True))
        self.stack.enter_context(mock.patch.object(
            audio_devices, "measure_input",
            return_value={"ok": True, "peak": 2400, "level": 0.2,
                          "verdict": "Working."}))
        self.stack.enter_context(mock.patch.object(
            audio_devices, "play_tone",
            return_value={"ok": True, "verdict": "Tone played."}))


class BoundaryTests(AudioPatchesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.runtime = make_runtime()
        self.app = create_app(self.runtime, frontend_dir=Path(tempfile.gettempdir()) / "aze-no-dist")
        self.client = TestClient(self.app, base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_health_is_loopback_only_and_json(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src", response.headers["content-security-policy"])

    def test_rejects_non_loopback_host(self):
        response = self.client.get("/api/health", headers={"Host": "evil.example"})
        self.assertEqual(response.status_code, 400)

    def test_rejects_cross_origin_reads(self):
        response = self.client.get(
            "/api/health", headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_accepts_same_origin(self):
        response = self.client.get(
            "/api/health", headers={"Origin": BASE})
        self.assertEqual(response.status_code, 200)

    def test_mutations_require_json_content_type(self):
        response = self.client.post("/api/voice/interrupt")
        self.assertEqual(response.status_code, 415)
        response = self.client.post(
            "/api/voice/interrupt",
            data={"x": "1"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status_code, 415)

    def test_malformed_json_is_a_validation_error(self):
        response = self.client.post(
            "/api/voice/mute", content=b"{not json",
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)

    def test_unknown_api_path_is_json_404(self):
        response = self.client.get("/api/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Not found.")

    def test_missing_frontend_has_a_useful_page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 503)
        self.assertIn("FRONTEND ASSETS NOT FOUND", response.text)


class ApprovalApiTests(AudioPatchesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.approvals = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=60, start_sweeper=False))
        self.approvals.set_session("web-api-test")
        self.runtime = make_runtime()
        self.approvals.set_listener(self.runtime.ui.approval_event)
        self.app = create_app(
            self.runtime,
            frontend_dir=Path(tempfile.gettempdir()) / "aze-no-dist")
        self.client = TestClient(self.app, base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(approvals.shutdown_service)

    def pending(self):
        _, request, _ = self.approvals.authorize(
            tool="file_control",
            operation="delete",
            target=r"C:\Temp\report.txt",
            parameters={"paths": [r"c:\temp\report.txt"]},
            summary=r"Delete C:\Temp\report.txt",
        )
        return request

    def test_bootstrap_lists_pending_approvals(self):
        request = self.pending()
        body = self.client.get("/api/bootstrap").json()
        self.assertEqual(len(body["approvals"]), 1)
        self.assertEqual(body["approvals"][0]["approval_id"],
                         request["approval_id"])
        self.assertNotIn("session_id", body["approvals"][0])

    def test_approve_endpoint_grant_exact_request(self):
        request = self.pending()
        response = self.client.post(
            f"/api/approvals/{request['approval_id']}/approve", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["approval"]["status"], "approved")
        self.assertEqual(
            [item["approval_id"] for item in self.approvals.active_requests()],
            [request["approval_id"]])

    def test_reject_endpoint_removes_pending_request(self):
        request = self.pending()
        response = self.client.post(
            f"/api/approvals/{request['approval_id']}/reject", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["approval"]["status"], "rejected")
        self.assertEqual(self.approvals.active_requests(), [])

    def test_unknown_approval_is_404(self):
        response = self.client.post(
            "/api/approvals/does-not-exist/approve", json={})
        self.assertEqual(response.status_code, 404)

    def test_consumed_approval_cannot_be_reapproved(self):
        request = self.pending()
        self.approvals.approve(request["approval_id"])
        self.approvals.authorize(
            tool="file_control",
            operation="delete",
            target=r"C:\Temp\report.txt",
            parameters={"paths": [r"c:\temp\report.txt"]},
            summary=r"Delete C:\Temp\report.txt",
        )
        response = self.client.post(
            f"/api/approvals/{request['approval_id']}/approve", json={})
        self.assertEqual(response.status_code, 409)


class NotificationApiTests(AudioPatchesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.notes = notification_service.reset_service(
            notification_service.NotificationService(
                str(Path(self.temp.name) / "notifications.db")))
        self.notes.configure(
            desktop_notifier=lambda _t, _b: {
                "desktop": "unsupported", "error": ""})
        self.addCleanup(notification_service.reset_service)
        self.runtime = make_runtime()
        self.client = TestClient(
            create_app(self.runtime,
                       frontend_dir=Path(tempfile.gettempdir()) / "aze-no-dist"),
            base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_list_read_and_dismiss_notification(self):
        note = self.notes.create(title="Hello", body="World")
        listed = self.client.get("/api/notifications").json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["notifications"][0]["id"], note["id"])

        read = self.client.post(
            f"/api/notifications/{note['id']}/read", json={}).json()
        self.assertEqual(read["notification"]["status"], "read")

        dismissed = self.client.post(
            f"/api/notifications/{note['id']}/dismiss", json={}).json()
        self.assertEqual(dismissed["notification"]["status"], "dismissed")
        self.assertEqual(len(self.notes.list(status="unread")), 0)

    def test_bootstrap_restores_recent_notifications(self):
        self.notes.create(title="Stored")
        data = self.client.get("/api/bootstrap").json()
        self.assertEqual(data["notifications"][0]["title"], "Stored")


class BootstrapTests(AudioPatchesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = isolated_config(TEST_CONFIG)
        self.config_path = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.runtime = make_runtime()
        self.client = TestClient(
            create_app(self.runtime, frontend_dir=Path(tempfile.gettempdir()) / "aze-no-dist"),
            base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_bootstrap_reflects_the_real_registry_and_modules(self):
        data = self.client.get("/api/bootstrap").json()
        self.assertEqual(data["protocol_version"], 1)
        self.assertEqual(len(data["tools"]), 17)
        self.assertEqual(data["modules"][0]["id"], "actions")
        self.assertEqual(data["modules"][0]["count"], 17)
        self.assertEqual(data["modules"][1]["count"], 0)
        self.assertEqual(data["setup"]["key_masked"][-4:], "mnop")

    def test_bootstrap_never_contains_the_full_api_key(self):
        full_key = TEST_CONFIG["gemini_api_key"]
        response = self.client.get("/api/bootstrap")
        self.assertNotIn(full_key, response.text)
        settings = self.client.get("/api/settings").json()
        self.assertNotIn(full_key, json.dumps(settings))

    def test_tools_endpoint_matches_tool_modules(self):
        tools = self.client.get("/api/tools").json()["tools"]
        names = [tool["name"] for tool in tools]
        self.assertIn("open_web", names)
        self.assertIn("open_file", names)
        self.assertIn("screen_understanding", names)
        self.assertIn("youtube_player", names)
        self.assertEqual(len(names), 17)

    def test_weather_and_metrics_are_real_backend_results(self):
        weather = self.client.get("/api/weather").json()
        self.assertTrue(weather["available"])
        self.assertEqual(weather["location"], "Tashkent")
        metrics = self.client.get("/api/metrics").json()
        self.assertEqual(metrics["cpu"], 12.0)

    def test_metrics_include_runtime_telemetry_not_placeholders(self):
        self.runtime._engine = FakeEngine()
        self.runtime.ui.add_user_message("hello", "telemetry-turn-1")
        self.runtime.ui.stream_assistant_delta("hi")
        metrics = self.client.get("/api/metrics").json()
        self.assertEqual(metrics["cpu"], 12.0)
        self.assertEqual(metrics["model"], "gemini-test-live")
        self.assertEqual(metrics["provider"], "Google Gemini")
        self.assertIsInstance(metrics["response_latency_ms"], int)
        self.assertGreaterEqual(metrics["response_latency_ms"], 0)
        self.assertEqual(metrics["microphone"], "active")
        self.assertEqual(metrics["voice_engine"], "online")
        self.assertEqual(metrics["active_agents"], 0)
        self.assertFalse(metrics["agent_runtime"])

    def test_diagnostics_and_initialize_are_real_checks(self):
        self.runtime._engine = FakeEngine()
        diagnostics = self.client.get("/api/diagnostics").json()
        ids = [check["id"] for check in diagnostics["checks"]]
        self.assertIn("settings", ids)
        self.assertIn("provider", ids)
        self.assertIn("voice", ids)
        self.assertIn("weather", ids)
        self.assertIn("tools", ids)
        self.assertIn("phone", ids)
        self.assertIn("youtube", ids)
        self.assertIn("capabilities", diagnostics)
        self.assertIn(diagnostics["capabilities"]["phone"]["status"],
                      {"ready", "starting", "configured_unverified",
                       "missing_credentials", "invalid_configuration",
                       "missing_dependency", "service_unavailable",
                       "unavailable"})
        self.assertTrue(diagnostics["ok"])
        self.assertTrue(diagnostics["session_connected"])
        self.assertEqual(diagnostics["tools"], 17)
        initialized = self.client.post("/api/initialize", json={}).json()
        self.assertTrue(initialized["ok"])
        self.assertEqual(initialized["model"], "gemini-test-live")

    def test_clear_tools_removes_visible_events(self):
        self.runtime.ui.tool_event({
            "id": "clear-call-1",
            "phase": "start",
            "tool": "open_web",
            "target": "https://example.test",
            "ms": 0,
        })
        self.assertEqual(len(self.runtime.ui.snapshot()["tool_events"]), 1)
        response = self.client.post("/api/tools/clear", json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.runtime.ui.snapshot()["tool_events"], [])


class MessageTests(AudioPatchesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.runtime = make_runtime()
        self.client = TestClient(
            create_app(self.runtime, frontend_dir=Path(tempfile.gettempdir()) / "aze-no-dist"),
            base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_offline_message_is_refused_and_logged(self):
        response = self.client.post(
            "/api/message", json={"text": "hello", "client_message_id": "abc-1"})
        self.assertEqual(response.status_code, 409)
        logs = self.runtime.ui.snapshot()["logs"]
        self.assertEqual(logs[-1]["text"], "SYS: Not connected yet.")
        self.assertEqual(self.runtime.ui.snapshot()["messages"], [])

    def test_connected_message_is_handed_to_the_existing_engine(self):
        engine = FakeEngine()
        self.runtime._engine = engine
        response = self.client.post(
            "/api/message", json={"text": "open Telegram", "client_message_id": "abc-2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(engine.sent, ["open Telegram"])
        messages = self.runtime.ui.snapshot()["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "abc-2")
        self.assertEqual(messages[0]["text"], "open Telegram")

    def test_retried_message_id_does_not_send_a_second_turn(self):
        engine = FakeEngine()
        self.runtime._engine = engine
        payload = {"text": "hello", "client_message_id": "retry-1"}
        first = self.client.post("/api/message", json=payload)
        second = self.client.post("/api/message", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(engine.sent, ["hello"])
        self.assertEqual(len(self.runtime.ui.snapshot()["messages"]), 1)

    def test_blank_and_oversized_messages_are_rejected(self):
        self.assertEqual(
            self.client.post("/api/message", json={"text": "   "}).status_code, 422)
        self.assertEqual(
            self.client.post("/api/message", json={"text": "x" * 8001}).status_code, 422)

    def test_voice_controls_use_the_existing_callbacks(self):
        engine = FakeEngine()
        self.runtime._engine = engine
        self.assertEqual(
            self.client.post(
                "/api/voice/interrupt",
                headers={"Content-Type": "application/json"},
            ).status_code,
            200)
        self.assertEqual(engine.interrupted, 1)
        mute = self.client.post("/api/voice/mute", json={"muted": True}).json()
        self.assertTrue(mute["muted"])
        self.assertEqual(mute["state"], "MUTED")
        ptt = self.client.post("/api/voice/ptt-hold", json={"held": True})
        self.assertEqual(ptt.status_code, 409)  # callback is installed by JarvisLive

    def test_interrupt_is_refused_when_the_session_is_not_connected(self):
        engine = FakeEngine()
        engine.session = None
        self.runtime._engine = engine
        response = self.client.post(
            "/api/voice/interrupt",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(engine.interrupted, 0)


class SettingsTests(AudioPatchesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = isolated_config(TEST_CONFIG)
        self.config_path = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.runtime = make_runtime()
        self.client = TestClient(
            create_app(self.runtime, frontend_dir=Path(tempfile.gettempdir()) / "aze-no-dist"),
            base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_settings_include_audio_and_mask_the_key(self):
        data = self.client.get("/api/settings").json()
        self.assertEqual(data["input_devices"], ["Mic One", "Mic Two"])
        self.assertEqual(data["output_devices"], ["Speaker One"])
        self.assertEqual(data["output_volume"], 42)
        self.assertEqual(data["gemini"]["key_masked"][-4:], "mnop")
        self.assertNotIn(TEST_CONFIG["gemini_api_key"], json.dumps(data))

    def test_valid_patch_writes_through_config(self):
        response = self.client.patch(
            "/api/settings", json={"ui_color": "#ffc400"})
        self.assertEqual(response.status_code, 200)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["ui_color"], "#ffc400")

    def test_invalid_patch_is_rejected_without_writing(self):
        before = self.config_path.read_text(encoding="utf-8")
        self.assertEqual(
            self.client.patch("/api/settings", json={"ui_color": "red"}).status_code,
            422)
        self.assertEqual(
            self.client.patch("/api/settings", json={"unknown": True}).status_code,
            422)
        self.assertEqual(
            self.client.patch("/api/settings", json={"voice_name": "Nobody"}).status_code,
            422)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_audio_patch_saves_both_devices_and_reports_no_session(self):
        response = self.client.patch(
            "/api/settings",
            json={"input_device": "Mic Two", "output_device": "Speaker One"})
        self.assertEqual(response.status_code, 200)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["input_device"], "Mic Two")
        self.assertEqual(saved["output_device"], "Speaker One")
        logs = [entry["text"] for entry in self.runtime.ui.snapshot()["logs"]]
        self.assertIn("SYS: Audio routing updated — reconnecting…", logs)

    def test_push_to_talk_is_not_silently_saved_when_unavailable(self):
        response = self.client.patch(
            "/api/settings", json={"push_to_talk_enabled": True})
        self.assertEqual(response.status_code, 409)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertFalse(saved.get("push_to_talk_enabled", False))

    def test_push_to_talk_can_be_disabled_without_a_live_engine(self):
        config.save_push_to_talk_enabled(True)
        response = self.client.patch(
            "/api/settings", json={"push_to_talk_enabled": False})
        self.assertEqual(response.status_code, 200)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["push_to_talk_enabled"])

    def test_behavior_and_turn_tuning_are_saved(self):
        response = self.client.patch("/api/settings", json={
            "proactive_audio_enabled": False,
            "thinking_enabled": True,
            "turn_tuning": {
                "enabled": True,
                "silence_ms": 700,
                "prefix_ms": 200,
                "end_sensitivity": "low",
                "start_sensitivity": "high",
            },
        })
        self.assertEqual(response.status_code, 200)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["proactive_audio"])
        self.assertTrue(saved["thinking_enabled"])
        self.assertEqual(saved["turn_tuning"]["silence_ms"], 700)

    def test_identity_and_voice_are_saved(self):
        self.runtime.ui.add_user_message("old conversation", "old-1")
        response = self.client.patch("/api/settings", json={
            "assistant_name": "Aze Test",
            "user_name": "Operator",
            "voice_name": "Kore",
        })
        self.assertEqual(response.status_code, 200)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["assistant_name"], "Aze Test")
        self.assertEqual(saved["user_name"], "Operator")
        self.assertEqual(saved["voice_name"], "Kore")
        self.assertEqual(self.runtime.ui.assistant_name, "Aze Test")
        # A voice change cannot resume the old session; the transcript starts
        # fresh so it does not imply context the model no longer has.
        self.assertEqual(self.runtime.ui.snapshot()["messages"], [])

    def test_api_key_update_is_masked_in_the_response(self):
        response = self.client.post(
            "/api/setup/key",
            json={"api_key": "new-secret-key-1234567890", "os_system": "windows"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["setup"]["key_masked"][-4:], "7890")
        self.assertNotIn("new-secret-key-1234567890", response.text)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["gemini_api_key"], "new-secret-key-1234567890")
        self.assertTrue(saved["setup_done"])

    def test_audio_test_and_volume_return_actual_results(self):
        test = self.client.post(
            "/api/audio/test", json={"kind": "input", "device": "Mic One"})
        self.assertEqual(test.status_code, 200)
        self.assertTrue(test.json()["result"]["ok"])
        volume = self.client.post(
            "/api/audio/volume", json={"volume": 55, "device": "Speaker One"})
        self.assertEqual(volume.status_code, 200)
        self.assertTrue(volume.json()["ok"])
        read = self.client.get("/api/audio/volume?device=Speaker%20One")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["volume"], 42)

    def test_module_scan_uses_live_tool_modules(self):
        response = self.client.post(
            "/api/modules/actions/scan",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 17)
        self.assertIn("17 CORE TOOLS LINKED", body["status"])
        self.assertEqual(
            self.client.post(
                "/api/modules/nope/scan",
                headers={"Content-Type": "application/json"},
            ).status_code,
            404)


class ReadinessTests(unittest.TestCase):
    def test_setup_done_is_authoritative(self):
        with isolated_config({"setup_done": True, "gemini_api_key": ""}):
            self.assertTrue(AzeRuntime._config_ready())

    def test_old_shape_with_key_and_os_is_accepted(self):
        with isolated_config({
            "gemini_api_key": "test-key-abcdefghijklmnop",
            "os_system": "windows",
        }):
            self.assertTrue(AzeRuntime._config_ready())

    def test_key_without_os_or_setup_done_requires_setup(self):
        with isolated_config({
            "gemini_api_key": "test-key-abcdefghijklmnop",
        }):
            self.assertFalse(AzeRuntime._config_ready())


class WebSocketTests(AudioPatchesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.runtime = make_runtime()
        self.client = TestClient(
            create_app(self.runtime, frontend_dir=Path(tempfile.gettempdir()) / "aze-no-dist"),
            base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_snapshot_and_ping(self):
        with self.client.websocket_connect(
            "/ws", headers={"origin": BASE, "host": "127.0.0.1:8765"}) as socket:
            snapshot = socket.receive_json()
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertEqual(snapshot["data"]["app"]["name"], "Jarvis")
            self.assertIn("tools", snapshot["data"])
            socket.send_json({"type": "ping"})
            pong = socket.receive_json()
            self.assertEqual(pong["type"], "pong")

    def test_unknown_websocket_message_is_reported(self):
        with self.client.websocket_connect(
            "/ws", headers={"origin": BASE, "host": "127.0.0.1:8765"}) as socket:
            socket.receive_json()
            socket.send_json({"type": "execute_shell", "cmd": "whoami"})
            reply = socket.receive_json()
            self.assertEqual(reply["type"], "error")
            self.assertIn("Unsupported", reply["message"])


class StaticTests(unittest.TestCase):
    def test_built_index_is_served_with_security_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "index.html").write_text(
                "<!doctype html><title>Aze test</title>", encoding="utf-8")
            runtime = make_runtime()
            client = TestClient(
                create_app(runtime, frontend_dir=directory, auto_start=False),
                base_url=BASE)
            with client:
                response = client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIn("Aze test", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertIn("frame-ancestors 'none'",
                              response.headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()


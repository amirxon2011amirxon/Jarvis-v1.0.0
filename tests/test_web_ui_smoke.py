"""End-to-end smoke test of the built React interface against the real server.

Skipped by default because it starts a browser and a local HTTP server.  Run it
with the frontend built and the environment flag set:

    cd frontend; npm run build; cd ..
    $env:AZE_UI_TESTS="1"
    python -m unittest tests.test_web_ui_smoke -v

The fake engine is injected only into this test process.  It never contacts
Gemini, opens a device, or touches the user's configuration.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "frontend" / "dist"


class FakeMetrics:
    def snapshot(self):
        return {"cpu": 12.0, "mem": 34.0, "net": 0.5, "gpu": -1.0, "tmp": -1.0}

    def stop(self):
        pass


class FakeWeather:
    def get(self, force=False):
        return {
            "available": True,
            "location": "Tashkent",
            "temperature_c": 21.4,
            "apparent_c": 21.0,
            "humidity": 40,
            "wind_kmh": 8,
            "weather_code": 0,
            "is_day": True,
        }


class FakeEngine:
    def __init__(self, runtime):
        self.runtime = runtime
        self.session = object()
        self.sent = []
        self.stopped = False

    def send_text(self, text):
        self.sent.append(text)
        threading.Timer(0.15, self._reply).start()
        return True

    def _reply(self):
        ui = self.runtime.ui
        ui.set_state("SPEAKING")
        ui.stream_assistant_delta("Hello ")
        time.sleep(0.06)
        ui.stream_assistant_delta("from the test session.")
        time.sleep(0.06)
        ui.write_log("Aze: Hello from the test session.")
        ui.set_state("LISTENING")

    def interrupt(self):
        self.runtime.ui.cancel_assistant_stream()
        self.runtime.ui.write_log("SYS: Interrupted — listening...")

    def request_reconnect(self, keep_context=True, reason=""):
        pass

    def set_push_to_talk(self, enabled):
        return "global"

    def stop(self):
        self.stopped = True


@unittest.skipUnless(
    os.environ.get("AZE_UI_TESTS") == "1",
    "set AZE_UI_TESTS=1 to run the browser smoke test",
)
class WebUISmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (DIST / "index.html").is_file():
            raise unittest.SkipTest("frontend/dist is not built")
        try:
            import uvicorn
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise unittest.SkipTest(f"browser test dependency unavailable: {exc}")

        from web import launch
        from web.runtime import AzeRuntime
        from web.server import create_app
        from core import audio_devices
        from core import memory as memory_module
        import config as config_module

        cls.config_temp = tempfile.TemporaryDirectory()
        config_dir = Path(cls.config_temp.name)
        cls.config_file = config_dir / "api_keys.json"
        cls.config_file.write_text(json.dumps({
            "gemini_api_key": "smoke-test-key-abcdefghijklmnop",
            "os_system": "windows",
            "setup_done": True,
            "assistant_name": "Aze",
            "user_name": "",
            "ui_color": "#ffb02e",
            "voice_name": "Charon",
            "push_to_talk_enabled": False,
            "proactive_audio": True,
            "thinking_enabled": False,
        }), encoding="utf-8")
        cls.config_patches = [
            mock.patch.object(config_module, "CONFIG_DIR", config_dir),
            mock.patch.object(config_module, "CONFIG_FILE", cls.config_file),
            mock.patch.object(
                config_module, "get_memory_settings",
                return_value={
                    "enabled": True,
                    "auto_extract": False,
                    "retrieval_enabled": True,
                    "voice_injection": False,
                    "max_retrieved": 6,
                    "max_context_chars": 1400,
                    "search_limit": 40,
                    "backup_retention": 2,
                    "expiry_days": 0,
                    "db_path": config_dir / "memory.db",
                    "backup_dir": config_dir / "memory_backups",
                }),
        ]
        for patch in cls.config_patches:
            patch.start()
        cls.addClassCleanup(cls._stop_config_patches)
        cls.addClassCleanup(cls.config_temp.cleanup)
        memory_module.reset_default_service()
        cls.addClassCleanup(memory_module.reset_default_service)

        cls.audio_patches = [
            mock.patch.object(
                audio_devices, "list_devices",
                side_effect=lambda kind, refresh=False: (
                    ["Test Microphone"] if kind == "input" else ["Test Speakers"]
                )),
            mock.patch.object(
                audio_devices, "get_output_volume", return_value=42),
            mock.patch.object(
                audio_devices, "set_output_volume", return_value=True),
            mock.patch.object(
                audio_devices, "measure_input",
                return_value={"ok": True, "peak": 2400, "level": 0.2,
                              "verdict": "Working. Your voice is coming through clearly."}),
            mock.patch.object(
                audio_devices, "play_tone",
                return_value={"ok": True, "verdict": "Tone played. Did you hear it?"}),
        ]
        for patch in cls.audio_patches:
            patch.start()
        cls.addClassCleanup(cls._stop_audio_patches)

        cls.uvicorn = uvicorn
        cls.runtime = AzeRuntime(
            start_session=False,
            metrics=FakeMetrics(),
            weather_service=FakeWeather(),
        )
        cls.engine = FakeEngine(cls.runtime)
        cls.runtime._engine = cls.engine
        app = create_app(cls.runtime, auto_start=False)
        cls.port = launch.find_free_port(8877)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=cls.port,
            log_level="warning", access_log=False)
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 20
        while not cls.server.started:
            if not cls.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.05)

        cls.playwright = sync_playwright().start()
        cls.profile_dir = tempfile.TemporaryDirectory(prefix="aze-ui-chromium-")
        cls.addClassCleanup(cls.profile_dir.cleanup)
        cls.browser = None
        cls.context = cls.playwright.chromium.launch_persistent_context(
            user_data_dir=cls.profile_dir.name,
            headless=True,
            viewport={"width": 1440, "height": 960},
            device_scale_factor=1,
            args=[
                "--disable-gpu",
                "--disable-gpu-shader-disk-cache",
                "--disk-cache-size=1",
                "--media-cache-size=1",
                "--disable-application-cache",
            ],
        )
        cls.page = (
            cls.context.pages[0] if cls.context.pages else cls.context.new_page()
        )
        cls.page_errors: list[str] = []
        cls.page.on("pageerror", lambda error: cls.page_errors.append(str(error)))
        cls.page.goto(
            f"http://127.0.0.1:{cls.port}/",
            wait_until="networkidle",
            timeout=20000,
        )
        try:
            cls.page.wait_for_selector("#aze-command", timeout=2500)
        except Exception:
            cls.page.screenshot(
                path=str(Path(tempfile.gettempdir()) / "aze_ui_gate.png"),
                full_page=True,
            )
            # The gate starts initialising on its own; click only if it is
            # still waiting for a manual start.
            try:
                enter = cls.page.get_by_role(
                    "button", name=re.compile("INITIALISE"))
                if enter.count() and enter.first.is_enabled():
                    enter.first.click()
            except Exception:
                pass
            cls.page.wait_for_selector("#aze-command", timeout=20000)
        cls.page.locator("header").get_by_text("LINKED", exact=True).first.wait_for(
            timeout=10000)
        cls.screenshot = Path(tempfile.gettempdir()) / "aze_ui_smoke.png"
        cls.page.screenshot(path=str(cls.screenshot), full_page=True)

    @classmethod
    def _stop_audio_patches(cls):
        for patch in cls.audio_patches:
            patch.stop()

    @classmethod
    def _stop_config_patches(cls):
        for patch in cls.config_patches:
            patch.stop()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.context.close()
            if cls.browser is not None:
                cls.browser.close()
            cls.playwright.stop()
            time.sleep(0.3)
        finally:
            cls.server.should_exit = True
            cls.thread.join(timeout=10)

    def setUp(self):
        """Start every smoke test from a clean, entered dashboard.

        The suite intentionally shares one browser context and backend. Without
        this reset, a viewport change or an open dialog from one test can make
        the next test wait on an element that is present but not interactive.
        """
        page = self.page
        page.set_viewport_size({"width": 1440, "height": 960})
        page.reload(wait_until="networkidle", timeout=20000)
        try:
            page.wait_for_selector("#aze-command", timeout=8000)
        except Exception:
            enter = page.get_by_role("button", name=re.compile("INITIALISE"))
            if enter.count() and enter.first.is_enabled():
                enter.first.click()
            page.wait_for_selector("#aze-command", timeout=20000)

    def test_01_layout_halo_clock_weather_and_modules(self):
        page = self.page
        self.assertEqual(page.locator("canvas").count(), 1)
        header = page.locator("header.aze-topbar")
        self.assertIn("Aze", header.inner_text())
        self.assertEqual(page.locator(".aze-clock").count(), 1)
        aside = page.locator("aside")
        for label in ("Memory", "Actions", "Agents", "Skills"):
            self.assertEqual(
                aside.get_by_role("button", name=label, exact=True).count(), 1)
        no_overflow = page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        self.assertTrue(no_overflow)
        self.assertEqual(self.page_errors, [])

        for width, height in ((1280, 800),):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(350)
            fits = page.evaluate(
                "() => document.documentElement.scrollWidth <= window.innerWidth + 1"
                " && document.documentElement.scrollHeight <= window.innerHeight + 1"
            )
            self.assertTrue(fits, f"layout overflows at {width}x{height}")
            page.screenshot(
                path=str(Path(tempfile.gettempdir()) / f"aze_ui_{width}.png"),
                full_page=True,
            )
        page.set_viewport_size({"width": 1440, "height": 960})
        # Day mode was removed entirely: the interface is permanently dark,
        # and no stray theme toggle may reappear without a mode behind it.
        self.assertEqual(
            page.evaluate("() => document.documentElement.dataset.theme"),
            "dark",
        )
        self.assertEqual(
            page.get_by_label(re.compile("day mode", re.IGNORECASE)).count(),
            0,
        )

    def test_02_message_round_trip(self):
        page = self.page
        page.fill("#aze-command", "Hello Aze")
        page.keyboard.press("Enter")
        page.locator("article[aria-label='You: Hello Aze']").wait_for(timeout=5000)
        page.locator(
            "article[aria-label='Aze: Hello from the test session.']"
        ).wait_for(timeout=5000)
        self.assertEqual(self.engine.sent, ["Hello Aze"])
        # The typing indicator is driven by the real awaiting-reply state:
        # once the reply has landed it must be gone (never faked).
        page.wait_for_timeout(200)
        self.assertEqual(page.locator(".aze-typing").count(), 0)

    def test_02b_clock_flips_live_with_seconds(self):
        page = self.page
        aria = page.get_attribute(".aze-clock", "aria-label") or ""
        self.assertRegex(aria, r"^Local time \d{2}:\d{2}:\d{2}$")
        first = page.get_attribute(".aze-clock-seconds .aze-clock-flip", "textContent")
        page.wait_for_timeout(2200)
        second = page.get_attribute(".aze-clock-seconds .aze-clock-flip", "textContent")
        self.assertTrue(
            first != second or aria != (
                page.get_attribute(".aze-clock", "aria-label") or ""),
            "clock did not advance over 2.2s",
        )

    def test_03_tool_activity_and_system_log(self):
        page = self.page
        self.runtime.ui.tool_event({
            "id": "smoke-call-1",
            "phase": "start",
            "tool": "open_web",
            "target": "https://example.test",
            "ms": 0,
        })
        self.runtime.ui.write_log("SYS: smoke test event")
        page.get_by_text("EXECUTING 1 ACTION", exact=True).wait_for(timeout=5000)
        page.get_by_role("tab", name="LOG").click()
        page.get_by_text("SYS: smoke test event", exact=True).wait_for(
            timeout=5000)
        self.runtime.ui.tool_event({
            "id": "smoke-call-1",
            "phase": "end",
            "tool": "open_web",
            "target": "https://example.test",
            "ms": 31,
            "ok": True,
            "summary": "opened",
        })
        page.screenshot(
            path=str(Path(tempfile.gettempdir()) / "aze_ui_toolbus.png"),
            full_page=True,
        )
        page.get_by_role("tab", name="CHAT").click()

    def test_04_refresh_restores_conversation_from_backend(self):
        page = self.page
        page.reload(wait_until="networkidle", timeout=20000)
        page.wait_for_selector("#aze-command", timeout=10000)
        page.locator(
            "article[aria-label='Aze: Hello from the test session.']"
        ).wait_for(timeout=5000)

    def test_05_mobile_layout_and_module_drawer(self):
        page = self.page
        page.set_viewport_size({"width": 390, "height": 844})
        page.reload(wait_until="networkidle", timeout=20000)
        page.wait_for_selector("#aze-command", timeout=10000)
        self.assertTrue(page.locator("#aze-command").is_visible())
        no_overflow = page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        self.assertTrue(no_overflow)
        page.screenshot(
            path=str(Path(tempfile.gettempdir()) / "aze_ui_mobile_main.png"),
            full_page=True,
        )
        page.get_by_label("Open modules", exact=True).click()
        page.locator("#module-rail").get_by_text(
            "MODULE ARRAY", exact=True).wait_for(timeout=5000)
        page.screenshot(
            path=str(Path(tempfile.gettempdir()) / "aze_ui_mobile.png"),
            full_page=True,
        )
        page.locator("#module-rail").get_by_label("Close module rail").click()
        page.set_viewport_size({"width": 1440, "height": 960})

    def test_06_settings_dialog_uses_real_sections(self):
        page = self.page
        page.get_by_title("Open settings").click()
        page.get_by_text("System configuration", exact=True).wait_for(timeout=5000)
        page.get_by_text("Identity profile", exact=True).wait_for(timeout=5000)
        page.get_by_role("button", name="APPEARANCE").click()
        page.get_by_text("Appearance matrix", exact=True).wait_for(timeout=5000)
        page.get_by_label("Use accent #d8a63a").click()
        page.get_by_role("button", name="APPLY THEME").click()
        page.get_by_text("Settings saved.", exact=True).wait_for(timeout=5000)
        saved = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["ui_color"], "#d8a63a")
        page.get_by_role("button", name="AUDIO I/O").click()
        page.get_by_text("Microphone", exact=True).first.wait_for(timeout=5000)
        dialog = page.get_by_role("dialog", name="System configuration")
        self.assertIn("Test Speakers", dialog.inner_text())
        page.get_by_role("button", name="BEHAVIOR").click()
        page.get_by_text("Push to talk", exact=True).wait_for(timeout=5000)
        page.get_by_role("button", name="GEMINI LINK").click()
        page.get_by_text("KEY PRESENT // GEMINI LIVE", exact=True).wait_for(timeout=5000)
        page.screenshot(
            path=str(Path(tempfile.gettempdir()) / "aze_ui_settings.png"),
            full_page=True,
        )
        dialog.get_by_label("Close dialog").click()

    def test_07_setup_overlay_appears_when_the_link_requests_a_key(self):
        page = self.page
        self.runtime.ui.prompt_reconfig()
        self.runtime.ui.set_state("OFFLINE")
        page.get_by_text("Initialisation required", exact=True).wait_for(timeout=5000)
        page.get_by_text("FIRST BOOT", exact=True).wait_for(timeout=5000)
        page.reload(wait_until="networkidle", timeout=20000)
        page.wait_for_selector("#aze-command", timeout=10000)

    def test_09_actions_bay_browses_folders_and_selects_tools(self):
        page = self.page
        data = self.runtime._tool_catalog()
        web_tools = [item for item in data["tools"]
                     if item["name"] in ("open_web", "close_web")]
        self.assertEqual(len(web_tools), 2)
        folder_id = web_tools[0]["category"]
        folder = next(item for item in data["categories"]
                      if item["id"] == folder_id)
        folder_label = folder["label"]
        web_ids = sorted(item["id"] for item in web_tools)
        open_app_id = next(item["id"] for item in data["tools"]
                           if item["name"] == "open_app")
        close_web_id = next(item["id"] for item in web_tools
                            if item["name"] == "close_web")
        open_web_id = next(item["id"] for item in web_tools
                           if item["name"] == "open_web")

        page.get_by_role("button", name="Actions", exact=True).click()
        sheet = page.get_by_role("dialog", name="Actions bay control")
        sheet.wait_for(timeout=5000)
        page.wait_for_timeout(500)
        visible_text = sheet.inner_text().lower()
        for item in data["categories"]:
            self.assertIn(item["label"].lower(), visible_text)

        # A category toggle is a folder selection: it must disable every tool
        # the backend reports under that folder.
        web_section = sheet.locator("section.aze-tool-folder").filter(
            has_text=folder_label).first
        web_folder = web_section.get_by_role(
            "button", name=re.compile(r"^" + re.escape(folder_label) + r"\b"))
        folder_count = len(folder["tools"])
        web_folder.click()
        web_section.get_by_text(f"0/{folder_count} ACTIVE", exact=True).wait_for(
            timeout=5000)
        saved = json.loads(self.config_file.read_text(encoding="utf-8"))
        for item_id in sorted(item["id"] for item in folder["tools"]):
            self.assertNotIn(item_id, saved["enabled_tools"])
        self.assertNotIn(open_app_id, saved["enabled_tools"])

        web_folder.click()
        web_section.get_by_text(f"{folder_count}/{folder_count} ACTIVE",
                                exact=True).wait_for(timeout=5000)
        saved = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertIn(open_app_id, saved["enabled_tools"])

        # An individual tool card toggles only that tool.
        close_web = web_section.get_by_role(
            "button", name=re.compile(r"CLOSE WEB"))
        close_web.click()
        web_section.get_by_text(f"{folder_count - 1}/{folder_count} ACTIVE",
                                exact=True).wait_for(timeout=5000)
        saved = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertNotIn(close_web_id, saved["enabled_tools"])
        self.assertIn(open_web_id, saved["enabled_tools"])

        close_web.click()
        web_section.get_by_text(f"{folder_count}/{folder_count} ACTIVE",
                                exact=True).wait_for(timeout=5000)

    def test_08_motion_preferences_drive_ui_and_datasets(self):
        page = self.page
        self.assertEqual(
            page.evaluate("() => document.documentElement.dataset.motion"),
            "balanced",
        )
        page.get_by_title("Open settings").click()
        page.get_by_text("System configuration", exact=True).wait_for(timeout=5000)
        page.get_by_role("button", name="MOTION").click()
        page.get_by_text("Animation quality", exact=True).wait_for(timeout=5000)
        dialog = page.get_by_role("dialog", name="System configuration")
        self.assertIn("SAVED PER DEVICE", dialog.inner_text())

        # Quality tier: switching writes the dataset instantly.
        page.get_by_role("button", name="LOW", exact=True).click()
        self.assertEqual(
            page.evaluate("() => document.documentElement.dataset.motion"),
            "low",
        )
        page.get_by_role("button", name="BALANCED", exact=True).click()
        self.assertEqual(
            page.evaluate("() => document.documentElement.dataset.motion"),
            "balanced",
        )

        # Particle and preference datasets:
        page.get_by_label("Particle effects", exact=True).click()
        self.assertEqual(
            page.evaluate("() => document.documentElement.dataset.particles"),
            "0",
        )
        page.get_by_label("Particle effects", exact=True).click()
        self.assertEqual(
            page.evaluate("() => document.documentElement.dataset.particles"),
            "1",
        )

        # Explicit reduced-motion override:
        page.get_by_label("Reduce motion", exact=True).click()
        self.assertEqual(
            page.evaluate("() => document.documentElement.dataset.motionReduced"),
            "1",
        )
        page.get_by_label("Reduce motion", exact=True).click()
        self.assertIsNone(
            page.evaluate(
                "() => document.documentElement.dataset.motionReduced ?? null")
        )
        stored = json.loads(page.evaluate(
            "() => localStorage.getItem('aze.motion.v1') || '{}'"
        ))
        self.assertEqual(stored.get("quality"), "balanced")
        self.assertEqual(stored.get("reduced"), False)
        dialog.get_by_label("Close dialog").click()

        # OS-level reduced motion is honoured even with the flag off:
        page.emulate_media(reduced_motion="reduce")
        page.reload(wait_until="networkidle", timeout=20000)
        page.wait_for_selector("#aze-command", timeout=10000)
        self.assertEqual(
            page.evaluate("() => document.documentElement.dataset.motionReduced"),
            "1",
        )
        page.emulate_media(reduced_motion="no-preference")
        page.reload(wait_until="networkidle", timeout=20000)
        page.wait_for_selector("#aze-command", timeout=10000)
        self.assertIsNone(
            page.evaluate(
                "() => document.documentElement.dataset.motionReduced ?? null")
        )
        self.assertEqual(
            page.evaluate("() => document.documentElement.dataset.motion"),
            "balanced",
        )
        # Clean the shared test profile back to a pristine default state.
        page.evaluate("() => localStorage.removeItem('aze.motion.v1')")


    def test_10_memory_sidebar_browses_edits_and_deletes(self):
        page = self.page
        service = self.runtime._get_memory_service()
        service.clear_all(confirm=True)
        created = service.remember(
            "The user prefers concise replies.", memory_type="user",
            title="Reply style", tags=["style"])
        self.assertTrue(created.ok)

        page.get_by_role("button", name="Memory", exact=True).click()
        dialog = page.get_by_role("dialog", name="Memory control")
        dialog.wait_for(timeout=5000)
        dialog.get_by_text("Reply style", exact=False).first.wait_for(timeout=5000)
        dialog.get_by_text("Memory", exact=True).first.wait_for(timeout=5000)

        # The list row opens the inspector.
        dialog.get_by_text("Reply style", exact=False).first.click()
        dialog.locator(".aze-memory-content").filter(
            has_text="The user prefers concise replies.").wait_for(timeout=5000)

        # Editing writes through the service and refreshes the row.
        dialog.get_by_role("button", name="EDIT", exact=True).click()
        dialog.locator("textarea").fill("The user prefers very concise replies.")
        dialog.get_by_role("button", name="SAVE", exact=True).click()
        dialog.get_by_text("Memory updated.", exact=True).wait_for(timeout=5000)
        service.search_memories("very concise")
        self.assertEqual(
            service.search_memories("very concise").data["total"], 1)

        # Deleting removes it from the database, not just the view.
        page.on("dialog", lambda dialog_box: dialog_box.accept())
        dialog.get_by_role("button", name="DELETE MEMORY", exact=True).click()
        dialog.get_by_text("Memory deleted.", exact=True).wait_for(timeout=5000)
        self.assertEqual(service.get_memory_stats().data["total"], 0)
        dialog.get_by_label("Close memory control").click()


if __name__ == "__main__":
    unittest.main()

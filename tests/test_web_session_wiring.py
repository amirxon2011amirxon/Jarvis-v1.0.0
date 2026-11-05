"""The existing JarvisLive object must wire cleanly to the web facade."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from main import JarvisLive
from web.runtime import AzeRuntime
from web.ui import WebUI


class FakeHub:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class _Metrics:
    def stop(self):
        pass


class _Weather:
    def get(self, force=False):
        return {"available": False}


class SessionWiringTests(unittest.TestCase):
    def setUp(self):
        self.ui = WebUI(FakeHub())
        with mock.patch("main.get_push_to_talk_enabled", return_value=False):
            self.live = JarvisLive(self.ui)

    def test_callbacks_are_installed_on_the_web_facade(self):
        self.assertEqual(self.ui.on_text_command, self.live._on_text_command)
        self.assertEqual(self.ui.on_interrupt, self.live.interrupt)
        self.assertEqual(self.ui.on_voice_change, self.live._on_voice_change)
        self.assertEqual(self.ui.on_audio_device_change,
                         self.live._on_audio_device_change)
        self.assertEqual(self.ui.on_push_to_talk, self.live.set_push_to_talk)
        self.assertEqual(self.ui.ptt_hold, self.live._on_ptt)

    def test_text_before_a_session_is_refused_not_faked(self):
        self.assertFalse(self.live.send_text("hello"))
        self.assertEqual(self.ui.snapshot()["messages"], [])

    def test_stop_is_safe_before_the_loop_starts(self):
        self.live.stop()
        self.assertTrue(self.live._shutdown)

    def test_cancelled_session_thread_marks_the_link_offline(self):
        runtime = AzeRuntime(
            start_session=False, metrics=_Metrics(), weather_service=_Weather())
        runtime.ui.mark_ready(True)
        runtime.ui.set_muted(True)

        fake_engine = type("FakeEngine", (), {
            "run": lambda self: None,
        })()

        with mock.patch("main.JarvisLive", return_value=fake_engine), \
             mock.patch("web.runtime.asyncio.run",
                        side_effect=asyncio.CancelledError):
            runtime._session_main()

        self.assertEqual(runtime.ui.state, "OFFLINE")
        self.assertTrue(runtime.ui.muted)


if __name__ == "__main__":
    unittest.main()

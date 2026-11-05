"""Regression tests for the Live session's recovery and cleanup paths.

No Gemini connection is made: the session opener is replaced with a context
manager that raises the error under test, and the client is a local fake.
"""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest import mock

import main
from actions import open_app_tool


class FakeUI:
    def __init__(self):
        self._win = self
        self._ready = True
        self.muted = False
        self.states = []
        self.logs = []
        self.prompted = 0

    def set_state(self, state):
        self.states.append(str(state))

    def write_log(self, text):
        self.logs.append(str(text))

    def prompt_reconfig(self):
        self.prompted += 1

    def set_assistant_name(self, _name):
        pass

    def set_audio_level(self, _level):
        pass

    def push_levels(self, _levels, _hop, _at):
        pass


class StopAfterPromptUI(FakeUI):
    def prompt_reconfig(self):
        super().prompt_reconfig()
        raise KeyboardInterrupt


class ApiKeyErrorTests(unittest.TestCase):
    def test_common_key_errors_are_recognised(self):
        for message in (
            "400 INVALID_ARGUMENT: API key not valid. Please pass a valid API key.",
            "API_KEY_INVALID",
            "invalid api key",
            "API key expired",
            "403 PERMISSION_DENIED: API key does not have permission",
            "1007",
        ):
            with self.subTest(message=message):
                self.assertTrue(main._is_api_key_error(message))
        self.assertFalse(main._is_api_key_error("429 RESOURCE_EXHAUSTED"))
        self.assertFalse(main._is_api_key_error("connection reset by peer"))

    def test_network_backoff_has_a_floor_after_a_voluntary_reconnect(self):
        # Short, capped delays: a DNS blip must not leave the assistant
        # "connecting" for a minute once the network is back.
        self.assertEqual(main._network_backoff(0), 1)
        self.assertEqual(main._network_backoff(None), 1)
        self.assertEqual(main._network_backoff(3), 5)
        self.assertEqual(main._network_backoff(40), 5)
        self.assertEqual(main._network_backoff(60), 5)
        self.assertEqual(main._network_backoff("bad"), 1)

    def test_invalid_key_does_not_disable_link_features_first(self):
        ui = StopAfterPromptUI()
        with mock.patch("main.get_push_to_talk_enabled", return_value=False):
            live = main.JarvisLive(ui)

        @asynccontextmanager
        async def failing_session(_model, _config):
            raise RuntimeError(
                "400 INVALID_ARGUMENT: API key not valid. "
                "Please pass a valid API key.")
            yield  # pragma: no cover - makes this a generator

        live._open_session = failing_session

        with mock.patch.object(main, "_get_api_key", return_value="bad-key"), \
             mock.patch.object(main.audio_devices, "configure"), \
             mock.patch.object(main.audio_devices, "prefetch"), \
             mock.patch.object(open_app_tool, "prefetch"), \
             mock.patch.object(main.JarvisLive, "_build_config",
                               return_value=object()):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(live.run())

        self.assertEqual(ui.prompted, 1)
        self.assertTrue(live._tuned_live)
        self.assertTrue(live._enhanced_live)
        self.assertNotIn(
            "SYS: Proactive audio unavailable — reconnecting without it.",
            ui.logs)


class SessionCleanupTests(unittest.TestCase):
    def test_the_per_session_client_is_closed(self):
        class FakeAio:
            def __init__(self):
                self.closed = False
                self.live = self

            @asynccontextmanager
            async def connect(self, **_kwargs):
                yield "session"

            async def aclose(self):
                self.closed = True

        class FakeClient:
            def __init__(self):
                self.aio = FakeAio()

        client = FakeClient()
        live = main.JarvisLive.__new__(main.JarvisLive)
        live._enhanced_live = True

        async def use():
            async with live._open_session("models/x", object()) as session:
                self.assertEqual(session, "session")

        with mock.patch.object(main.genai, "Client", return_value=client), \
             mock.patch.object(main, "_get_api_key", return_value="key"):
            asyncio.run(use())

        self.assertTrue(client.aio.closed)


if __name__ == "__main__":
    unittest.main()

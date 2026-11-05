"""The model ladder must not brick itself when the network drops.

A DNS/connect failure is the link, not the model: no cooldown, no walking the
whole ladder, and a short fail-fast window so the next request does not replay
the same wait. Genuine quota/availability answers still rest only that rung.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from core import gemini


class FakeModels:
    def __init__(self, error_for=None, reply="ok"):
        self.error_for = error_for or {}
        self.reply = reply
        self.calls = []

    def generate_content(self, **kwargs):
        name = kwargs["model"]
        self.calls.append(kwargs)
        error = self.error_for.get(name)
        if error:
            raise error
        return SimpleNamespace(text=self.reply)


class FakeClient:
    def __init__(self, models):
        self.models = models


class LadderTestBase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self._reset)
        self._reset()

    def _reset(self):
        gemini._cooldown.clear()
        gemini._network_down_until = 0.0
        gemini._network_warned = False

    def patch_ladder(self, models):
        patcher = mock.patch.dict(
            gemini._LADDERS, {gemini.FAST: tuple(models)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class ClassificationTests(unittest.TestCase):
    def test_network_failures(self):
        for message in (
            "ConnectError: [Errno 11001] getaddrinfo failed",
            "TimeoutError: timed out during opening handshake",
            "ConnectionRefusedError: [WinError 10061]",
            "ssl: CERTIFICATE_VERIFY_FAILED",
            "Server disconnected without sending a response.",
        ):
            with self.subTest(message=message):
                self.assertTrue(gemini.is_network_error(message))

    def test_model_failures_are_not_network_failures(self):
        for message in (
            "503 UNAVAILABLE",
            "504 DEADLINE_EXCEEDED",
            "429 RESOURCE_EXHAUSTED",
            "404 model not found",
        ):
            with self.subTest(message=message):
                self.assertFalse(gemini.is_network_error(message))

    def test_availability_rest_is_short(self):
        self.assertLessEqual(gemini._UNAVAILABLE_SECONDS, 180)


class NetworkHandlingTests(LadderTestBase):
    def test_network_failure_stops_the_walk_without_cooling(self):
        self.patch_ladder((gemini.LIVE, "m2", "m3"))
        live = mock.Mock(side_effect=ConnectionError(
            "ConnectError: [Errno 11001] getaddrinfo failed"))
        client_patch = mock.patch.object(
            gemini, "client",
            side_effect=AssertionError("REST should not be tried while offline"))
        client_patch.start()
        self.addCleanup(client_patch.stop)
        with mock.patch.object(gemini, "_live_call", live):
            result = gemini.call("hello", key="test-key")
        self.assertIsNone(result)
        self.assertEqual(1, live.call_count)       # the walk stopped there
        self.assertFalse(any(gemini._cooling(m)
                             for m in (gemini.LIVE, "m2", "m3")))
        self.assertTrue(gemini._network_is_down())

    def test_fail_fast_window_skips_the_next_request(self):
        self.patch_ladder(("m1", "m2"))
        models = FakeModels()
        client_patch = mock.patch.object(
            gemini, "client", return_value=FakeClient(models))
        client_patch.start()
        self.addCleanup(client_patch.stop)
        models.error_for = {"m1": ConnectionError("getaddrinfo failed")}
        self.assertIsNone(gemini.call("hello", key="test-key"))
        before = len(models.calls)
        self.assertIsNone(gemini.call("again", key="test-key"))
        self.assertEqual(before, len(models.calls))  # no new network attempt

    def test_success_clears_transient_cooldown_only(self):
        gemini._cool("transient", 600, "unavailable")
        gemini._cool("quota", 600, "quota")
        gemini._note_success()
        self.assertFalse(gemini._cooling("transient"))
        self.assertTrue(gemini._cooling("quota"))

    def test_quota_rests_only_the_answering_out_model(self):
        self.patch_ladder(("m1", "m2"))
        models = FakeModels(error_for={"m1": RuntimeError(
            "429 RESOURCE_EXHAUSTED")})
        with mock.patch.object(gemini, "client",
                               return_value=FakeClient(models)):
            result = gemini.call("hello", key="test-key")
        self.assertIsNotNone(result)
        self.assertTrue(gemini._cooling("m1"))
        self.assertFalse(gemini._cooling("m2"))
        self.assertEqual(["m1", "m2"], [call["model"] for call in models.calls])


class AfcTests(LadderTestBase):
    def test_config_disables_afc_without_mutating_the_caller(self):
        self.patch_ladder(("m1",))
        models = FakeModels()
        original = {"system_instruction": "be terse"}
        with mock.patch.object(gemini, "client",
                               return_value=FakeClient(models)):
            response = gemini.call("hello", key="test-key", config=original)
        self.assertIsNotNone(response)
        sent = models.calls[0]["config"]
        self.assertEqual({"disable": True},
                         sent["automatic_function_calling"])
        self.assertEqual("be terse", sent["system_instruction"])
        self.assertNotIn("automatic_function_calling", original)

    def test_explicit_afc_config_is_respected(self):
        self.patch_ladder(("m1",))
        models = FakeModels()
        config = {"automatic_function_calling": {"maximum_remote_calls": 5}}
        with mock.patch.object(gemini, "client",
                               return_value=FakeClient(models)):
            gemini.call("hello", key="test-key", config=config)
        self.assertEqual({"maximum_remote_calls": 5},
                         models.calls[0]["config"]["automatic_function_calling"])


if __name__ == "__main__":
    unittest.main()

"""Tests for the core Jarvis lifecycle command.

The real backend is never stopped here: a fake handler records the request.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from actions import tool_calling
from core import lifecycle


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        lifecycle._reset_for_tests()
        self.addCleanup(lifecycle._reset_for_tests)

    def test_shutdown_requires_a_registered_process_handler(self):
        result = lifecycle.shutdown_jarvis("user request")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "shutdown_unavailable")

    def test_request_runs_the_handler_with_the_reason(self):
        seen: list[str] = []
        called = threading.Event()

        def handler(reason: str) -> None:
            seen.append(reason)
            called.set()

        unregister = lifecycle.register_shutdown_handler(handler)
        self.assertTrue(lifecycle.has_shutdown_handler())
        self.assertTrue(lifecycle.request_shutdown("test shutdown", delay=0.01))
        self.assertTrue(called.wait(1.0))
        self.assertEqual(seen, ["test shutdown"])
        self.assertTrue(lifecycle.shutdown_requested())

        unregister()
        self.assertFalse(lifecycle.has_shutdown_handler())

    def test_shutdown_jarvis_reports_success_and_is_idempotent(self):
        seen: list[str] = []
        lifecycle.register_shutdown_handler(lambda reason: seen.append(reason))
        first = lifecycle.shutdown_jarvis("voice command")
        self.assertTrue(first["success"])
        self.assertTrue(first.get("shutting_down"))
        second = lifecycle.shutdown_jarvis("again")
        self.assertTrue(second["success"])
        self.assertTrue(second.get("already_shutting_down"))
        deadline = time.monotonic() + 2.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(seen, ["voice command"])

    def test_duplicate_handler_is_registered_once(self):
        calls: list[str] = []

        def handler(reason: str) -> None:
            calls.append(reason)

        lifecycle.register_shutdown_handler(handler)
        lifecycle.register_shutdown_handler(handler)
        self.assertTrue(lifecycle.request_shutdown("once", delay=0.01))
        deadline = time.monotonic() + 1.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(calls, ["once"])

    def test_registered_as_a_core_action_not_an_action_pack_tool(self):
        self.assertIn("shutdown_jarvis", tool_calling.TOOL_REGISTRY)
        self.assertIn("shutdown_jarvis", tool_calling._CORE_ACTION_NAMES)
        self.assertNotIn("shutdown_jarvis", tool_calling._MEMORY_TOOL_NAMES)
        catalog = tool_calling.get_tool_catalog()
        self.assertNotIn("shutdown_jarvis",
                         [tool["name"] for tool in catalog["tools"]])
        self.assertNotIn("shutdown_jarvis",
                         [action for tool in catalog["tools"]
                          for action in tool.get("actions", [])])

        declaration = next(
            item for item in tool_calling.get_tool_declarations()
            if item["name"] == "shutdown_jarvis")
        self.assertEqual(declaration["parameters"]["required"], [])
        self.assertIn("close yourself", declaration["description"])

    def test_shutdown_during_call_ends_call_instead_of_powering_off(self):
        class FakePhone:
            def status(self):
                return {
                    "bridge": {"active": True, "call_id": "c1"},
                    "calls": [{"call_id": "c1", "state": "active"}],
                }

            def hangup(self):
                return {"ok": True, "call": {"call_id": "c1", "state": "ended"}}

        with mock.patch.object(
                tool_calling._telephony, "get_service",
                return_value=FakePhone()), \
                mock.patch.object(lifecycle, "shutdown_jarvis") as shutdown:
            result = tool_calling.execute_tool(
                "shutdown_jarvis", {"reason": "bye Jarvis"})
        self.assertTrue(result.get("success"))
        self.assertTrue(result.get("ended_call"))
        shutdown.assert_not_called()

    def test_registry_dispatch_uses_the_core_function(self):
        seen: list[str] = []
        lifecycle.register_shutdown_handler(lambda reason: seen.append(reason))
        with mock.patch.object(lifecycle, "request_shutdown",
                               return_value=True) as request:
            result = tool_calling.execute_tool(
                "shutdown_jarvis", {"reason": "test"})
        self.assertTrue(result["success"])
        request.assert_called_once()

    def test_process_handler_can_be_unregistered(self):
        unregister = lifecycle.register_shutdown_handler(lambda reason: None)
        unregister()
        self.assertFalse(lifecycle.has_shutdown_handler())

    def test_launcher_handler_notifies_browser_then_stops_server(self):
        from types import SimpleNamespace
        from web.launch import _make_shutdown_handler

        events: list[tuple[str, object]] = []

        class Runtime:
            def prepare_shutdown(self, reason):
                events.append(("prepare", reason))

            def close_interface_tab(self):
                events.append(("close_tab", True))
                return True

        server = SimpleNamespace(should_exit=False)
        handler = _make_shutdown_handler(
            Runtime(), server, delay=0, settle=0)
        with mock.patch("web.launch.time.sleep"):
            handler("user request")
        self.assertEqual(events, [
            ("prepare", "user request"),
            ("close_tab", True),
        ])
        self.assertTrue(server.should_exit)

    def test_launcher_handler_stops_server_even_if_browser_close_fails(self):
        from types import SimpleNamespace
        from web.launch import _make_shutdown_handler

        class Runtime:
            def prepare_shutdown(self, reason):
                raise RuntimeError("ui unavailable")

            def close_interface_tab(self):
                raise RuntimeError("Brave unavailable")

        server = SimpleNamespace(should_exit=False)
        handler = _make_shutdown_handler(
            Runtime(), server, delay=0, settle=0)
        with mock.patch("web.launch.time.sleep"), \
             mock.patch("web.launch.traceback.print_exc"):
            handler("user request")
        self.assertTrue(server.should_exit)

    def test_interface_close_only_targets_the_local_jarvis_tab(self):
        from web import runtime as runtime_module

        closed = []

        class FakeCloseWeb:
            @staticmethod
            def get_open_tabs():
                return [
                    {"tab_id": "jarvis-tab",
                     "title": "J.A.R.V.I.S. // Local Neural Interface",
                     "url": "http://127.0.0.1:8765/", "active": False},
                    {"tab_id": "unrelated-tab",
                     "title": "A YouTube video",
                     "url": "https://www.youtube.com/watch?v=x", "active": True},
                    {"tab_id": "another-local",
                     "title": "Some local dashboard",
                     "url": "http://127.0.0.1:9000/", "active": False},
                ]

            @staticmethod
            def close_tab(tab):
                closed.append(tab["tab_id"])
                return {"success": True}

        runtime = object.__new__(runtime_module.AzeRuntime)
        with mock.patch.object(runtime_module, "get_tool_module",
                               return_value=FakeCloseWeb):
            self.assertTrue(runtime.close_interface_tab())
        self.assertEqual(closed, ["jarvis-tab"])


if __name__ == "__main__":
    unittest.main()

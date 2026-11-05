"""Tests for the shared registry and executor in actions/tool_calling.py.

The tests register short-lived fake tools for the generic executor paths, and
mock the existing app tools when calling them through the real registry. No
application is launched or closed.
Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
import unittest
from types import MappingProxyType, SimpleNamespace
from unittest import mock
from threading import Barrier, BrokenBarrierError

from actions import close_app_tool, open_app_tool, tool_calling


def _echo_tool(value: str = "") -> dict:
    return {"opened": value or "test", "value": value}


def _required_tool(value: str) -> dict:
    return {"result": value}


def _boom_tool() -> dict:
    raise RuntimeError("boom")


def _non_dict_tool():
    return 42


class _Weird:
    def __str__(self):
        return "weird"


def _weird_tool() -> dict:
    return {"result": _Weird()}


def _closing_tool() -> dict:
    return {"closing": "App"}


class RegistryTests(unittest.TestCase):
    EXPECTED = {"list_apps", "open_app", "open_web",
                "list_running_apps", "close_app", "close_web",
                "search_files", "open_file", "open_folder",
                "open_containing_folder", "close_file",
                "screen_understanding", "screenshot", "zip_tool", "file_control", "read_document",
                "web_fetch", "research", "reminders", "system_control",
                "youtube_player",
                "call", "answer_call", "reject_call",
                "hangup", "mute_call", "unmute_call", "get_call_status",
                "get_call_history",
                "memory_remember", "memory_search", "memory_list",
                "memory_update", "memory_forget", "memory_stats",
                "shutdown_jarvis", "task_status", "list_skills",
                "activate_skill", "read_skill_reference"}

    def test_expected_tools_are_registered(self):
        self.assertEqual(set(tool_calling.get_tool_names()), self.EXPECTED)
        for name in self.EXPECTED:
            info = tool_calling.TOOL_REGISTRY[name]
            self.assertTrue(callable(info["func"]))
            self.assertTrue(info["description"])
            self.assertEqual(info["parameters"]["type"], "OBJECT")

    def test_declarations_are_derived_from_the_registry(self):
        declarations = tool_calling.get_tool_declarations()
        self.assertEqual(
            [d["name"] for d in declarations],
            list(tool_calling.TOOL_REGISTRY))
        for declaration in declarations:
            self.assertEqual(
                set(declaration), {"name", "description", "parameters"})
            self.assertNotIn("func", declaration)
            self.assertEqual(declaration["parameters"]["type"], "OBJECT")

    def test_every_declaration_demands_same_turn_execution(self):
        for declaration in tool_calling.get_tool_declarations():
            with self.subTest(tool=declaration["name"]):
                self.assertTrue(
                    declaration["description"].endswith(
                        tool_calling._EXECUTION_MANDATE),
                    declaration["name"])

    def test_required_parameters_match_function_signatures(self):
        for name, info in tool_calling.TOOL_REGISTRY.items():
            signature = inspect.signature(info["func"])
            expected = [
                p.name for p in signature.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD,
                               p.KEYWORD_ONLY)
            ]
            self.assertEqual(info["parameters"]["required"], expected, name)

    def test_has_tool_and_tool_signature(self):
        self.assertTrue(tool_calling.has_tool("open_web"))
        self.assertFalse(tool_calling.has_tool("missing"))
        self.assertIn("url", tool_calling.tool_signature("open_web"))
        self.assertEqual(tool_calling.tool_signature("missing"), "")

    def test_register_tool_rejects_duplicates_and_non_callables(self):
        with self.assertRaises(ValueError):
            tool_calling.register_tool(
                "open_web", _echo_tool, "duplicate", {"type": "OBJECT"})
        with self.assertRaises(TypeError):
            tool_calling.register_tool(
                "_test_non_callable", 123, "bad", {"type": "OBJECT"})
        self.assertFalse(tool_calling.has_tool("_test_non_callable"))

    def test_event_target_names_for_existing_tools(self):
        self.assertEqual(
            tool_calling._event_target("open_app", {"app_name": " Telegram "}),
            "Telegram")
        self.assertEqual(
            tool_calling._event_target("close_app", {"app_name": "Paint"}),
            "Paint")
        self.assertEqual(
            tool_calling._event_target("open_web",
                                       {"url": "https://claude.ai"}),
            "https://claude.ai")
        self.assertEqual(
            tool_calling._event_target("close_web", {"target": " Claude "}),
            "Claude")
        self.assertEqual(
            tool_calling._event_target("list_apps", {}), "installed apps")
        self.assertEqual(
            tool_calling._event_target("list_running_apps", {}), "running apps")
        self.assertEqual(tool_calling._event_target("unknown", {}), "")

    def test_event_summary_shapes(self):
        self.assertEqual(
            tool_calling._event_summary({"error": "bad"}), (False, "bad"))
        self.assertEqual(
            tool_calling._event_summary({"apps": ["A"], "count": 1}),
            (True, "1 app"))
        self.assertEqual(
            tool_calling._event_summary({"apps": ["A", "B"], "count": 2}),
            (True, "2 apps"))
        self.assertEqual(
            tool_calling._event_summary({"opened": "Telegram"}),
            (True, "opened Telegram"))
        self.assertEqual(
            tool_calling._event_summary({"closed": "Telegram"}),
            (True, "closed Telegram"))
        self.assertEqual(
            tool_calling._event_summary({"closing": "Telegram"}),
            (True, "closing Telegram"))
        self.assertEqual(
            tool_calling._event_summary({
                "clarification_required": True,
                "message": "Which one?",
            }),
            (False, "Which one?"))
        self.assertEqual(
            tool_calling._event_summary({"result": "ok"}), (True, "ok"))


class ExecutionTests(unittest.TestCase):
    def register(self, name, func, required=None):
        tool_calling.register_tool(
            name, func, "temporary test tool",
            {"type": "OBJECT", "properties": {}, "required": required or []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, name, None)

    def test_success_result_logs_and_events(self):
        self.register("_test_echo", _echo_tool)
        logs, events = [], []
        result = tool_calling.execute_tool(
            "_test_echo", {"value": "x"}, logs.append, events.append)

        self.assertEqual(result, {"opened": "x", "value": "x"})
        self.assertTrue(any("TOOL: _test_echo" in line for line in logs))
        self.assertEqual(events[0]["phase"], "start")
        self.assertEqual(events[-1]["phase"], "end")
        self.assertTrue(events[-1]["ok"])
        self.assertEqual(events[-1]["summary"], "opened x")
        self.assertFalse(events[-1]["pending"])

    def test_string_arguments_are_not_serialised_without_a_log_callback(self):
        self.register("_test_echo", _echo_tool)
        with mock.patch.object(tool_calling.json, "dumps",
                               wraps=tool_calling.json.dumps) as dumps:
            tool_calling.execute_tool("_test_echo", {"value": "x"})
        # The result-safety round-trip is required; the argument log line is
        # not. The only remaining dumps call must be the result normaliser.
        self.assertEqual(dumps.call_count, 1)

    def test_non_dict_result_is_wrapped(self):
        self.register("_test_non_dict", _non_dict_tool)
        result = tool_calling.execute_tool("_test_non_dict", {})
        self.assertEqual(result, {"result": 42})

    def test_closing_result_is_marked_pending(self):
        self.register("_test_closing", _closing_tool)
        events = []
        result = tool_calling.execute_tool(
            "_test_closing", {}, event_callback=events.append)
        self.assertEqual(result, {"closing": "App"})
        self.assertTrue(events[-1]["ok"])
        self.assertTrue(events[-1]["pending"])
        self.assertEqual(events[-1]["summary"], "closing App")

    def test_unknown_tool_returns_error(self):
        events = []
        result = tool_calling.execute_tool(
            "does_not_exist", {}, event_callback=events.append)
        self.assertIn("Unknown tool", result["error"])
        self.assertIn("open_web", result["error"])
        self.assertFalse(events[-1]["ok"])

    def test_bad_arguments_return_error(self):
        self.register("_test_required", _required_tool)
        events = []
        result = tool_calling.execute_tool(
            "_test_required", {"other": "x"}, event_callback=events.append)
        self.assertIn("Bad arguments for _test_required", result["error"])
        self.assertFalse(events[-1]["ok"])

    def test_tool_exception_returns_error(self):
        self.register("_test_boom", _boom_tool)
        result = tool_calling.execute_tool("_test_boom", {})
        self.assertIn("RuntimeError: boom", result["error"])

    def test_broken_callbacks_do_not_break_tool(self):
        self.register("_test_echo", _echo_tool)

        def broken(_):
            raise RuntimeError("callback failed")

        result = tool_calling.execute_tool(
            "_test_echo", {"value": "x"}, broken, broken)
        self.assertEqual(result, {"opened": "x", "value": "x"})

    def test_non_serialisable_result_is_made_json_safe(self):
        self.register("_test_weird", _weird_tool)
        result = tool_calling.execute_tool("_test_weird", {})
        self.assertEqual(result, {"result": "weird"})

    def test_normalize_args(self):
        self.assertEqual(tool_calling.normalize_args("x", None), {})
        original = {"url": "https://example.com"}
        copied = tool_calling.normalize_args("x", original)
        self.assertEqual(copied, original)
        self.assertIsNot(copied, original)
        mapped = tool_calling.normalize_args(
            "x", MappingProxyType({"url": "https://example.com"}))
        self.assertEqual(mapped, {"url": "https://example.com"})

    def test_real_open_app_tool_through_registry(self):
        entry = {"name": "Telegram", "kind": "shortcut",
                 "launch": r"C:\Telegram.lnk"}
        events = []
        with mock.patch.object(open_app_tool, "_IS_WIN", True), \
             mock.patch.object(open_app_tool, "_index", return_value=[entry]), \
             mock.patch.object(open_app_tool, "_launch") as launch:
            result = tool_calling.execute_tool(
                "open_app", {"app_name": "Telegram"},
                event_callback=events.append)

        self.assertEqual(result, {"opened": "Telegram", "via": "shortcut"})
        launch.assert_called_once_with(entry)
        self.assertEqual(events[-1]["target"], "Telegram")
        self.assertEqual(events[-1]["summary"], "opened Telegram")

    def test_real_close_app_tool_through_registry(self):
        app = {"name": "Telegram", "processes": ["telegram"],
               "titles": ["Telegram"],
               "items": [{"pid": 1, "hwnd": 10, "process": "telegram"}]}
        events = []
        with mock.patch.object(close_app_tool, "_IS_WIN", True), \
             mock.patch.object(
                close_app_tool, "_find_running", return_value=app), \
             mock.patch.object(close_app_tool, "_request_close"), \
             mock.patch.object(close_app_tool, "_still_open", return_value=False):
            result = tool_calling.execute_tool(
                "close_app", {"app_name": "Telegram"},
                event_callback=events.append)

        self.assertEqual(result, {"closed": "Telegram"})
        self.assertEqual(events[-1]["target"], "Telegram")
        self.assertEqual(events[-1]["summary"], "closed Telegram")


class FunctionResponseTests(unittest.TestCase):
    def test_make_function_response_uses_id_name_and_response(self):
        try:
            from google.genai import types  # noqa: F401
        except ImportError:
            self.skipTest("google-genai is not installed")
        call = SimpleNamespace(
            id="call-1", name="open_web", args={"url": "https://example.com"})
        result = {"success": True, "opened": "https://example.com"}
        response = tool_calling.make_function_response(call, result)
        self.assertEqual(response.id, "call-1")
        self.assertEqual(response.name, "open_web")
        self.assertEqual(dict(response.response), {"result": result})

    def test_make_function_response_accepts_dict_call(self):
        try:
            from google.genai import types  # noqa: F401
        except ImportError:
            self.skipTest("google-genai is not installed")
        call = {"id": "call-2", "name": "open_web", "args": {}}
        response = tool_calling.make_function_response(call, {"error": "bad"})
        self.assertEqual(response.id, "call-2")
        self.assertEqual(response.name, "open_web")
        self.assertEqual(dict(response.response), {"result": {"error": "bad"}})

    def test_invoke_returns_a_function_response(self):
        try:
            from google.genai import types  # noqa: F401
        except ImportError:
            self.skipTest("google-genai is not installed")
        name = "_test_invoke"
        tool_calling.register_tool(
            name, _echo_tool, "temporary test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, name, None)
        call = SimpleNamespace(id="call-3", name=name, args={"value": "x"})
        response = tool_calling.invoke(call)
        self.assertEqual(response.id, "call-3")
        self.assertEqual(
            dict(response.response),
            {"result": {"opened": "x", "value": "x"}})


class ParallelDispatchTests(unittest.TestCase):
    def setUp(self):
        tool_calling.refresh_call_cache()
        self.addCleanup(tool_calling.refresh_call_cache)

    def test_independent_calls_actually_overlap(self):
        barrier = Barrier(2, timeout=2.0)

        def meet(value=""):
            try:
                barrier.wait()
            except BrokenBarrierError:
                return {"error": "not concurrent"}
            return {"ok": value}

        tool_calling.register_tool(
            "_test_meet", meet, "temporary test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_meet", None)

        calls = [
            SimpleNamespace(id="meet-a", name="_test_meet", args={"value": "a"}),
            SimpleNamespace(id="meet-b", name="_test_meet", args={"value": "b"}),
        ]
        with mock.patch.object(
                tool_calling, "_conflict_key",
                side_effect=lambda _name, args: f"key:{args.get('value')}"):
            responses = asyncio.run(tool_calling.dispatch_tool_calls(calls))

        self.assertEqual([r.id for r in responses], ["meet-a", "meet-b"])
        self.assertEqual(
            [dict(r.response)["result"] for r in responses],
            [{"ok": "a"}, {"ok": "b"}],
        )

    def test_duplicate_calls_in_one_message_execute_once(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"opened": value}

        tool_calling.register_tool(
            "_test_counted", counted, "temporary test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_counted", None)

        call = SimpleNamespace(id="same-call", name="_test_counted",
                               args={"value": "x"})
        responses = asyncio.run(
            tool_calling.dispatch_tool_calls([call, call]))
        self.assertEqual(executions, ["x"])
        self.assertEqual(len(responses), 2)
        self.assertEqual([dict(r.response) for r in responses],
                         [dict(responses[0].response)] * 2)

    def test_batching_preserves_conflicts_and_discovery_order(self):
        def call(name, args):
            return SimpleNamespace(id=f"{name}-id", name=name, args=args)

        calls = [
            call("list_apps", {}),
            call("open_app", {"app_name": "Telegram"}),
            call("open_app", {"app_name": "Discord"}),
            call("open_app", {"app_name": "Telegram"}),
            call("open_web", {"url": "https://a.test"}),
            call("open_web", {"url": "https://b.test"}),
            call("open_file", {"path": r"C:\docs\a.txt"}),
        ]
        batches = tool_calling._plan_parallel_batches(
            list(enumerate(calls)))
        flattened = [[c.name for _i, c in batch] for batch in batches]
        self.assertEqual(flattened, [
            ["list_apps"],
            ["open_app", "open_app"],
            ["open_app", "open_web"],
            ["open_web", "open_file"],  # disjoint resources may share a batch
        ])
        self.assertEqual(
            [tool_calling._conflict_key("open_app", {"app_name": "Telegram"}),
             tool_calling._conflict_key("open_app", {"app_name": "Discord"})],
            ["app:telegram", "app:discord"])

    def test_execution_guard_stops_remaining_calls(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"ok": value}

        tool_calling.register_tool(
            "_test_guarded", counted, "temporary guarded test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_guarded", None)

        calls = [
            SimpleNamespace(id="guard-a", name="_test_guarded",
                            args={"value": "a"}),
            SimpleNamespace(id="guard-b", name="_test_guarded",
                            args={"value": "b"}),
        ]

        def guard():
            return len(executions) == 0

        responses = asyncio.run(
            tool_calling.dispatch_tool_calls(calls, should_continue=guard))

        self.assertEqual(executions, ["a"])
        self.assertEqual(len(responses), 2)
        first = dict(responses[0].response)["result"]
        second = dict(responses[1].response)["result"]
        self.assertNotIn("error", first)
        self.assertIn("no longer active", second["error"])

    def test_execution_guard_can_block_before_any_call(self):
        executions = []

        def never(value=""):
            executions.append(value)
            return {"ok": value}

        tool_calling.register_tool(
            "_test_never", never, "temporary blocked test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_never", None)

        call = SimpleNamespace(id="guard-c", name="_test_never",
                               args={"value": "x"})
        responses = asyncio.run(tool_calling.dispatch_tool_calls(
            [call], should_continue=lambda: False))
        self.assertEqual(executions, [])
        self.assertEqual(len(responses), 1)
        self.assertIn("no longer active",
                      dict(responses[0].response)["result"]["error"])

    def test_cancellation_during_execution_stops_remaining_calls(self):
        started = threading.Event()
        release = threading.Event()
        executions = []

        def blocking(value=""):
            started.set()
            if not release.wait(2.0):
                return {"error": "release timeout"}
            executions.append(value)
            return {"ok": value}

        tool_calling.register_tool(
            "_test_blocking", blocking, "temporary blocking test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_blocking", None)

        cancelled = False
        calls = [
            SimpleNamespace(id="stop-a", name="_test_blocking",
                            args={"value": "a"}),
            SimpleNamespace(id="stop-b", name="_test_blocking",
                            args={"value": "b"}),
        ]
        box = {}

        def guard():
            return not cancelled

        def run_dispatcher():
            box["responses"] = asyncio.run(
                tool_calling.dispatch_tool_calls(
                    calls, should_continue=guard))

        worker = threading.Thread(target=run_dispatcher, daemon=True)
        worker.start()
        self.assertTrue(started.wait(1.0))
        cancelled = True
        release.set()
        worker.join(3.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(executions, ["a"])
        self.assertEqual(len(box["responses"]), 2)
        self.assertNotIn(
            "error", dict(box["responses"][0].response)["result"])
        self.assertIn(
            "no longer active",
            dict(box["responses"][1].response)["result"]["error"])

    def test_call_guard_blocks_one_call_without_losing_the_batch(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"opened": value}

        tool_calling.register_tool(
            "_test_guard", counted, "temporary call guard test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_guard", None)

        calls = [
            SimpleNamespace(id="guard-a", name="_test_guard",
                            args={"value": "a"}),
            SimpleNamespace(id="guard-b", name="_test_guard",
                            args={"value": "b"}),
        ]

        def guard(call):
            if call.args.get("value") == "a":
                return "repeated failed operation"
            return ""

        responses = asyncio.run(tool_calling.dispatch_tool_calls(
            calls, should_continue=lambda: True, call_guard=guard))
        self.assertEqual(executions, ["b"])
        first = dict(responses[0].response)["result"]
        second = dict(responses[1].response)["result"]
        self.assertTrue(first["recovery_blocked"])
        self.assertEqual(second["opened"], "b")


class AsyncAndTimeoutTests(unittest.TestCase):
    def setUp(self):
        tool_calling.refresh_call_cache()

    def test_async_tool_is_awaited(self):
        async def async_echo(value=""):
            await asyncio.sleep(0)
            return {"opened": value}

        tool_calling.register_tool(
            "_test_async", async_echo, "temporary async tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_async", None)

        call = SimpleNamespace(id="async-1", name="_test_async",
                               args={"value": "hello"})
        responses = asyncio.run(tool_calling.dispatch_tool_calls([call]))
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].id, "async-1")
        self.assertEqual(
            dict(responses[0].response)["result"]["opened"], "hello")

    def test_async_responses_preserve_ids_and_order(self):
        async def async_echo(value=""):
            await asyncio.sleep(0)
            return {"opened": value}

        tool_calling.register_tool(
            "_test_async_order", async_echo, "temporary async order tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(
            tool_calling.TOOL_REGISTRY.pop, "_test_async_order", None)

        calls = [
            SimpleNamespace(id="async-a", name="_test_async_order",
                            args={"value": "a"}),
            SimpleNamespace(id="async-b", name="_test_async_order",
                            args={"value": "b"}),
        ]
        responses = asyncio.run(tool_calling.dispatch_tool_calls(calls))
        self.assertEqual([response.id for response in responses],
                         ["async-a", "async-b"])
        self.assertEqual(
            [dict(response.response)["result"]["opened"]
             for response in responses], ["a", "b"])

    def test_async_exception_becomes_a_structured_error(self):
        async def async_boom():
            raise RuntimeError("async boom")

        tool_calling.register_tool(
            "_test_async_boom", async_boom, "temporary async failure",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(
            tool_calling.TOOL_REGISTRY.pop, "_test_async_boom", None)

        call = SimpleNamespace(id="async-2", name="_test_async_boom", args={})
        responses = asyncio.run(tool_calling.dispatch_tool_calls([call]))
        payload = dict(responses[0].response)["result"]
        self.assertIn("error", payload)
        self.assertIn("async boom", payload["error"])

    def test_sync_tool_timeout_is_structured(self):
        def slow():
            time.sleep(0.2)
            return {"ok": True}

        tool_calling.register_tool(
            "_test_slow", slow, "temporary slow tool",
            {"type": "OBJECT", "properties": {}, "required": []},
            timeout_s=0.05)
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_slow", None)

        call = SimpleNamespace(id="slow-1", name="_test_slow", args={})
        responses = asyncio.run(tool_calling.dispatch_tool_calls([call]))
        self.assertEqual(responses[0].id, "slow-1")
        payload = dict(responses[0].response)["result"]
        self.assertTrue(payload["timeout"])
        self.assertEqual(payload["error_code"], "tool_timeout")
        self.assertEqual(payload["timeout_seconds"], 0.05)

    def test_async_tool_timeout_is_structured(self):
        async def slow_async():
            await asyncio.sleep(1.0)
            return {"ok": True}

        tool_calling.register_tool(
            "_test_slow_async", slow_async, "temporary slow async tool",
            {"type": "OBJECT", "properties": {}, "required": []},
            timeout_s=0.05)
        self.addCleanup(
            tool_calling.TOOL_REGISTRY.pop, "_test_slow_async", None)

        call = SimpleNamespace(
            id="slow-2", name="_test_slow_async", args={})
        responses = asyncio.run(tool_calling.dispatch_tool_calls([call]))
        payload = dict(responses[0].response)["result"]
        self.assertTrue(payload["timeout"])
        self.assertEqual(payload["error_code"], "tool_timeout")

    def test_per_tool_timeout_override_and_default(self):
        def fast():
            return {"ok": True}

        tool_calling.register_tool(
            "_test_timed", fast, "temporary timed tool",
            {"type": "OBJECT", "properties": {}, "required": []},
            timeout_s=2.5)
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_timed", None)
        self.assertEqual(tool_calling.get_tool_timeout("_test_timed"), 2.5)
        self.assertEqual(
            tool_calling.get_tool_timeout("not_registered"),
            tool_calling.DEFAULT_TOOL_TIMEOUT)

    def test_large_result_is_bounded(self):
        def huge():
            return {"items": ["x" * 10_000 for _ in range(10)],
                    "error": "kept"}

        tool_calling.register_tool(
            "_test_huge", huge, "temporary huge result",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_huge", None)
        result = tool_calling.execute_tool("_test_huge", {})
        self.assertEqual(result.get("error"), "kept")
        self.assertLessEqual(
            len(json.dumps(result)), tool_calling.MAX_TOOL_RESULT_CHARS)

    def test_async_tool_direct_execute_tool_is_supported(self):
        async def direct(value=""):
            return {"value": value}

        tool_calling.register_tool(
            "_test_direct_async", direct, "temporary direct async tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(
            tool_calling.TOOL_REGISTRY.pop, "_test_direct_async", None)
        result = tool_calling.execute_tool(
            "_test_direct_async", {"value": "ok"})
        self.assertEqual(result["value"], "ok")


if __name__ == "__main__":
    unittest.main()

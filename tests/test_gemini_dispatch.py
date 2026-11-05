"""Tests for the Gemini Live tool-dispatch bridge.

These cover the seams between a Live function-call message and the registry:
extracting and executing calls, answering every call individually even when a
batch member fails, omitting a call id the server never sent, and the guarantee
that a natural-language claim ("I opened it") is never mistaken for a call.
No application, website or file is opened: launches are mocked.
Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from actions import open_file_tool, tool_calling

try:
    from google.genai import types  # noqa: F401
    HAS_GENAI = True
except ImportError:                                   # pragma: no cover
    HAS_GENAI = False


def _echo_tool(value: str = "") -> dict:
    return {"opened": value or "echo", "value": value}


def _required_tool(value: str) -> dict:
    return {"result": value}


@unittest.skipUnless(HAS_GENAI, "google-genai is not installed")
class DispatchToolCallsTests(unittest.TestCase):
    def setUp(self):
        tool_calling.register_tool(
            "_test_echo", _echo_tool, "temporary test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        tool_calling.register_tool(
            "_test_required", _required_tool, "temporary test tool",
            {"type": "OBJECT", "properties": {}, "required": ["value"]})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_echo", None)
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_required", None)

    def call(self, id, name, args=None):
        return SimpleNamespace(id=id, name=name, args=args or {})

    def test_every_call_in_the_batch_is_answered(self):
        calls = [
            self.call("call-1", "_test_echo", {"value": "x"}),
            self.call("call-2", "_test_required", {"value": "y"}),
        ]
        responses = asyncio.run(tool_calling.dispatch_tool_calls(calls))
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0].id, "call-1")
        self.assertEqual(responses[0].name, "_test_echo")
        self.assertEqual(
            dict(responses[0].response)["result"],
            {"opened": "x", "value": "x"})
        self.assertEqual(responses[1].id, "call-2")
        self.assertEqual(
            dict(responses[1].response)["result"], {"result": "y"})

    def test_one_failing_call_becomes_an_error_response_not_a_lost_batch(self):
        real_invoke = tool_calling.invoke

        def flaky(call, *rest):
            if call.id == "call-bad":
                raise RuntimeError("dispatch blew up")
            return real_invoke(call, *rest)

        calls = [
            self.call("call-bad", "_test_echo", {}),
            self.call("call-good", "_test_echo", {"value": "ok"}),
        ]
        with mock.patch.object(tool_calling, "invoke", side_effect=flaky):
            responses = asyncio.run(
                tool_calling.dispatch_tool_calls(calls))

        self.assertEqual(len(responses), 2)
        # The failed call is still answered, with its own id preserved, so the
        # model never waits for a response that never arrives.
        self.assertEqual(responses[0].id, "call-bad")
        self.assertEqual(responses[0].name, "_test_echo")
        payload = dict(responses[0].response)["result"]
        self.assertIn("error", payload)
        self.assertIn("dispatch blew up", payload["error"])
        self.assertTrue(dict(responses[1].response)["result"]["opened"] == "ok")

    def test_empty_call_list_builds_no_responses(self):
        # main.py must not send an empty tool response; this is the shape it
        # guards on.
        self.assertEqual(asyncio.run(tool_calling.dispatch_tool_calls([])), [])
        self.assertEqual(
            asyncio.run(tool_calling.dispatch_tool_calls(None)), [])

    def test_unknown_and_nameless_calls_are_answered_with_errors(self):
        calls = [self.call("call-9", "not_a_tool"), self.call("call-10", "")]
        responses = asyncio.run(tool_calling.dispatch_tool_calls(calls))
        self.assertEqual(len(responses), 2)
        for response in responses:
            self.assertIn("error", dict(response.response)["result"])
        self.assertEqual(responses[0].id, "call-9")

    def test_broken_callbacks_do_not_break_the_batch(self):
        def broken(_):
            raise RuntimeError("callback failed")

        responses = asyncio.run(tool_calling.dispatch_tool_calls(
            [self.call("call-11", "_test_echo", {"value": "x"})],
            broken, broken))
        self.assertEqual(len(responses), 1)
        self.assertTrue(dict(responses[0].response)["result"]["opened"] == "x")


@unittest.skipUnless(HAS_GENAI, "google-genai is not installed")
class FunctionResponseIdTests(unittest.TestCase):
    def test_call_id_is_repeated_when_present(self):
        call = SimpleNamespace(id="call-1", name="open_file", args={})
        response = tool_calling.make_function_response(call, {"success": True})
        self.assertEqual(response.id, "call-1")
        self.assertEqual(response.name, "open_file")

    def test_no_id_on_the_wire_when_the_call_had_none(self):
        # An id-less call answered with an empty-string id serialises a bogus
        # field; the response must simply omit it instead.
        call = SimpleNamespace(id=None, name="open_file", args={})
        response = tool_calling.make_function_response(call, {"success": True})
        self.assertIsNone(response.id)
        self.assertNotIn("id", response.model_dump(exclude_none=True))
        self.assertEqual(
            response.model_dump(exclude_none=True)["name"], "open_file")

    def test_error_response_carries_the_failure(self):
        call = SimpleNamespace(id="call-2", name="open_file", args={})
        response = tool_calling.error_response(call, ValueError("bad path"))
        self.assertEqual(response.id, "call-2")
        self.assertEqual(response.name, "open_file")
        self.assertIn("bad path", dict(response.response)["result"]["error"])


@unittest.skipUnless(HAS_GENAI, "google-genai is not installed")
class CallDeduplicationTests(unittest.TestCase):
    """A redelivered call must be answered, not executed a second time."""

    def setUp(self):
        tool_calling.refresh_call_cache()
        self.addCleanup(tool_calling.refresh_call_cache)
        self.calls = []

        def counting_tool(value=""):
            self.calls.append(value)
            return {"opened": value or "x"}

        tool_calling.register_tool(
            "_test_counting", counting_tool, "temporary test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_counting", None)

    def test_identical_redelivered_call_executes_once(self):
        call = SimpleNamespace(id="dup-1", name="_test_counting",
                               args={"value": "v"})
        first = tool_calling.invoke(call)
        second = tool_calling.invoke(call)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(dict(first.response), dict(second.response))

    def test_a_different_call_with_the_same_id_still_runs(self):
        first = SimpleNamespace(id="dup-2", name="_test_counting",
                                args={"value": "a"})
        second = SimpleNamespace(id="dup-2", name="_test_counting",
                                 args={"value": "b"})
        tool_calling.invoke(first)
        tool_calling.invoke(second)
        self.assertEqual(self.calls, ["a", "b"])

    def test_refresh_forgets_remembered_calls(self):
        call = SimpleNamespace(id="dup-3", name="_test_counting", args={})
        tool_calling.invoke(call)
        tool_calling.refresh_call_cache()
        tool_calling.invoke(call)
        self.assertEqual(len(self.calls), 2)


class NaturalLanguageClaimTests(unittest.TestCase):
    """A spoken claim is text, never a call: the registry only acts on
    structured function calls with a known name and typed arguments."""

    def test_a_claim_passed_as_arguments_opens_nothing(self):
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = tool_calling.execute_tool(
                "open_file", "I have opened the file for you")
        self.assertIn("error", result)
        self.assertIn("Bad arguments", result["error"])
        startfile.assert_not_called()

    def test_a_claim_in_place_of_a_call_opens_nothing(self):
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            responses = asyncio.run(
                tool_calling.dispatch_tool_calls(["I opened it, promise"]))
        self.assertEqual(len(responses), 1)
        self.assertIn("Unknown tool", dict(responses[0].response)["result"]["error"])
        startfile.assert_not_called()

    def test_a_claim_as_the_path_is_validated_not_executed(self):
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = tool_calling.execute_tool(
                "open_file", {"path": "I opened it already"})
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing")
        startfile.assert_not_called()


class DisabledToolGuardTests(unittest.TestCase):
    """A call for a disabled tool is refused honestly, not with a confusing
    argument error: the model never received that tool's declaration, so any
    arguments it invented were guesses."""

    def call(self, name, args=None):
        return SimpleNamespace(id="call-disabled", name=name,
                               args=args or {})

    def setUp(self):
        tool_calling.refresh_call_cache()

    def test_a_disabled_tool_is_refused_with_an_actionable_error(self):
        with mock.patch.object(tool_calling, "_enabled_record_ids",
                               lambda records=None: set()):
            response = tool_calling.invoke(self.call("open_web", {
                "url": "https://example.com", "target": "new tab"}))
        result = dict(response.response)["result"]
        self.assertIn("disabled", result["error"])
        self.assertIn("Actions bay", result["error"])
        self.assertIn("open_web", result["error"])

    def test_a_runtime_registered_tool_is_never_blocked(self):
        # A runtime-registered action has no category-folder record; a
        # selection cannot hide it, so it executes even when every recorded
        # tool is off.
        tool_calling.register_tool(
            "_test_guard_echo", _echo_tool, "temporary test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_test_guard_echo",
                        None)
        with mock.patch.object(tool_calling, "_enabled_record_ids",
                               lambda records=None: set()):
            response = tool_calling.invoke(
                self.call("_test_guard_echo", {"value": "x"}))
        self.assertEqual(dict(response.response)["result"]["value"], "x")

    def test_an_enabled_tool_still_executes(self):
        from actions import open_web_tool

        class _Process:
            returncode = None

            def poll(self):
                return None

        with mock.patch.object(
                tool_calling, "_enabled_record_ids",
                lambda records=None: {
                    record["id"]
                    for record in tool_calling.discover_tool_modules()}), \
             mock.patch.object(open_web_tool, "get_brave_path",
                               return_value="C:/Brave/brave.exe"), \
             mock.patch.object(open_web_tool.subprocess, "Popen",
                               return_value=_Process()) as popen:
            response = tool_calling.invoke(self.call("open_web", {
                "url": "https://example.com"}))
        result = dict(response.response)
        self.assertEqual(result["result"]["success"], True)
        self.assertEqual(popen.call_args.args[0][1], "https://example.com")


if __name__ == "__main__":
    unittest.main()

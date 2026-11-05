"""End-to-end adapter test: JarvisLive receive loop -> WebUI events."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import main
from actions import file_control_tool, phone_call_tool, research_tool, tool_calling
from actions import system_control_tool as sysctl
from core import agent_run, approvals
from web.ui import WebUI


class FakeHub:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class FakeSession:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.contexts = []

    def receive(self):
        async def generator():
            for message in self.messages:
                yield message
            raise asyncio.CancelledError
        return generator()

    async def send_tool_response(self, function_responses):
        self.sent.append(list(function_responses))

    async def send_client_content(self, **kwargs):
        self.contexts.append(kwargs)


def server_message(data=None, tool_call=None, turn_complete=False,
                   out_text="", in_text=None):
    transcription = SimpleNamespace(text=out_text) if out_text else None
    inbound = SimpleNamespace(text=in_text) if in_text else None
    server = SimpleNamespace(
        output_transcription=transcription,
        input_transcription=inbound,
        turn_complete=turn_complete,
    ) if (turn_complete or out_text or in_text) else None
    return SimpleNamespace(data=data, tool_call=tool_call,
                           server_content=server,
                           session_resumption_update=None)


def tool_call_message(*calls, turn_complete=False):
    payload = SimpleNamespace(function_calls=[
        SimpleNamespace(id=call_id, name=name, args=args)
        for call_id, name, args in calls])
    return server_message(tool_call=payload, turn_complete=turn_complete)


class WebSessionIntegrationTests(unittest.TestCase):
    def setUp(self):
        tool_calling.refresh_call_cache()
        self.addCleanup(tool_calling.refresh_call_cache)
        self.hub = FakeHub()
        self.ui = WebUI(self.hub)
        self.ui.set_assistant_name("Aze")
        live = main.JarvisLive.__new__(main.JarvisLive)
        live.ui = self.ui
        live.session = None
        live.audio_in_queue = asyncio.Queue()
        live._turn_done_event = asyncio.Event()
        live._last_audio_at = time.monotonic()
        live._rx_deadline = None
        live._is_speaking = False
        live._speaking_lock = threading.Lock()
        live._out_latency = 0.2
        live._interrupted = False
        live._tool_in_progress = False
        live._resume_handle = None
        live._asst_name = "Aze"
        live._last_out_logged = ""
        live._last_user_speech = time.monotonic()
        live._play_cursor = 0.0
        live._out_level = 0.0
        live._tail_until = 0.0
        live._loop = None
        live.out_queue = asyncio.Queue()
        live._agent_terminal_until_user = False
        agent_run.reset_service(None)
        self.addCleanup(agent_run.reset_service, None)
        live._agent_runs = agent_run.get_service()
        live._agent_runs.set_conversation_id("test-conversation")
        live._agent_runs.set_listener(live._on_agent_run_event)
        self.live = live

    def run_loop(self, messages):
        self.live.session = FakeSession(messages)
        async def go():
            try:
                await self.live._receive_audio()
            finally:
                chain = getattr(self.live, "_tool_chain", None)
                if chain is not None:
                    try:
                        await chain
                    except asyncio.CancelledError:
                        pass
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(go(), timeout=5))

    def test_streaming_transcript_becomes_one_final_chat_message(self):
        self.run_loop([
            server_message(out_text="Hello there."),
            server_message(out_text=" Welcome.", turn_complete=True),
        ])
        snap = self.ui.snapshot()
        self.assertEqual(len(snap["messages"]), 1)
        self.assertEqual(snap["messages"][0]["role"], "assistant")
        self.assertEqual(snap["messages"][0]["text"], "Hello there. Welcome.")
        deltas = [e for e in self.hub.events if e["type"] == "chat.delta"]
        self.assertEqual(len(deltas), 2)
        final = [e for e in self.hub.events if e["type"] == "chat.message"][-1]
        self.assertEqual(final["message"]["id"], deltas[0]["id"])

    def test_renamed_assistant_still_produces_a_chat_message(self):
        self.ui.set_assistant_name("Nova")
        self.live._asst_name = "Aze"
        self.run_loop([
            server_message(out_text="New name.", turn_complete=True),
        ])
        messages = self.ui.snapshot()["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "New name.")
        self.assertTrue(any(log["text"].startswith("Nova: ")
                            for log in self.ui.snapshot()["logs"]))

    def test_repeated_full_reply_is_suppressed_not_duplicated(self):
        self.run_loop([
            server_message(out_text="Hello there.", turn_complete=True),
            server_message(out_text="Hello there.", turn_complete=True),
        ])
        snap = self.ui.snapshot()
        self.assertEqual(len(snap["messages"]), 1)
        self.assertIn("chat.discarded", [e["type"] for e in self.hub.events])

    def test_a_stopped_reply_does_not_keep_streaming_text(self):
        self.live._interrupted = True
        self.live._interrupted_at = time.monotonic()
        self.run_loop([
            server_message(out_text="The rest of the answer.",
                           turn_complete=True),
        ])
        snap = self.ui.snapshot()
        self.assertEqual(snap["messages"], [])
        self.assertNotIn("chat.delta", [e["type"] for e in self.hub.events])

    def test_multiple_tool_calls_are_reported_and_answered(self):
        tool_calling.register_tool(
            "_web_echo",
            lambda value="": {"opened": value or "x"},
            "web integration test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_echo", None)
        self.run_loop([
            tool_call_message(
                ("web-call-a", "_web_echo", {"value": "A"}),
                ("web-call-b", "_web_echo", {"value": "B"}),
                turn_complete=True),
        ])
        rows = self.ui.snapshot()["tool_events"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["phase"] == "end" and row["ok"] for row in rows))
        self.assertEqual([r.id for r in self.live.session.sent[0]],
                         ["web-call-a", "web-call-b"])

    def test_tool_failure_is_reported_not_faked(self):
        def boom():
            raise RuntimeError("boom")

        tool_calling.register_tool(
            "_web_boom", boom, "web failure test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_boom", None)
        self.run_loop([
            tool_call_message(("web-call-bad", "_web_boom", {}),
                              turn_complete=True),
        ])
        rows = self.ui.snapshot()["tool_events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phase"], "end")
        self.assertFalse(rows[0]["ok"])
        self.assertIn("RuntimeError: boom", rows[0]["summary"])
        self.assertTrue(any("RuntimeError: boom" in entry["text"]
                            for entry in self.ui.snapshot()["logs"]))

    def test_tool_events_and_results_come_from_the_existing_registry(self):
        tool_calling.register_tool(
            "_web_echo",
            lambda value="": {"opened": value or "x"},
            "web integration test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_echo", None)
        self.run_loop([
            tool_call_message(
                ("web-call-1", "_web_echo", {"value": "Demo"}),
                turn_complete=True),
        ])
        rows = self.ui.snapshot()["tool_events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "_web_echo")
        self.assertEqual(rows[0]["phase"], "end")
        self.assertEqual(rows[0]["status"], "success")
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["summary"], "opened Demo")
        self.assertEqual(self.live.session.sent[0][0].id, "web-call-1")

    def test_ordinary_tool_call_does_not_create_an_agent_run(self):
        tool_calling.register_tool(
            "_web_echo",
            lambda value="": {"opened": value or "x"},
            "web integration test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_echo", None)
        self.run_loop([
            tool_call_message(("web-call-1", "_web_echo", {"value": "A"}),
                              turn_complete=True),
        ])
        self.assertIsNone(self.live._agent_runs.current())
        self.assertFalse([
            event for event in self.hub.events
            if event["type"] == "agent.updated"])

    def test_tool_progress_is_recorded_on_an_active_run(self):
        tool_calling.register_tool(
            "_web_echo",
            lambda value="": {"opened": value or "x"},
            "web integration test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_echo", None)
        run = self.live._agent_runs.start("Open a demo")
        self.run_loop([
            tool_call_message(("web-call-1", "_web_echo", {"value": "A"}),
                              turn_complete=True),
        ])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.step_count, 1)
        self.assertEqual(current.tool_call_count, 1)
        self.assertIn("_web_echo", current.last_tool_summary)
        self.assertGreaterEqual(
            len([event for event in self.hub.events
                 if event["type"] == "agent.updated"]), 2)

    def test_tool_call_limit_fails_the_run_without_breaking_dispatch(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"opened": value or "x"}

        tool_calling.register_tool(
            "_web_echo", counted, "web integration test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_echo", None)
        run = self.live._agent_runs.start(
            "Two calls",
            limits=agent_run.AgentRunLimits(
                max_steps=10, max_tool_calls=1,
                max_consecutive_failures=3,
                max_duration_seconds=60))
        self.run_loop([
            tool_call_message(
                ("web-call-a", "_web_echo", {"value": "A"}),
                ("web-call-b", "_web_echo", {"value": "B"}),
                turn_complete=True),
        ])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.FAILED)
        # The batch is rejected before execution, but every call still gets an
        # answer so the model is never left waiting.
        self.assertEqual(executions, [])
        self.assertEqual(len(self.live.session.sent[0]), 2)
        self.assertIn("Tool call limit", current.last_error)

    def test_task_status_tool_starts_and_completes_a_run(self):
        self.run_loop([
            tool_call_message(
                ("task-start", "task_status",
                 {"operation": "start", "objective": "Check the system"}),
                turn_complete=True),
        ])
        started = self.live._agent_runs.current()
        self.assertIsNotNone(started)
        self.assertEqual(started.objective, "Check the system")
        self.assertEqual(started.status, agent_run.IN_PROGRESS)

        self.run_loop([
            tool_call_message(
                ("task-done", "task_status",
                 {"operation": "complete", "summary": "All checked",
                  "evidence": ["System status was observed after the check."]}),
                turn_complete=True),
        ])
        completed = self.live._agent_runs.current()
        self.assertEqual(completed.run_id, started.run_id)
        self.assertEqual(completed.status, agent_run.COMPLETED)
        self.assertEqual(completed.result_summary, "All checked")

    def test_task_start_then_tool_then_complete(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"ok": True, "value": value}

        tool_calling.register_tool(
            "_web_multi_step", counted, "multi-step task test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(
            tool_calling.TOOL_REGISTRY.pop, "_web_multi_step", None)

        self.run_loop([
            tool_call_message(
                ("multi-start", "task_status",
                 {"operation": "start", "objective": "Run the multi-step task"}),
                turn_complete=True),
        ])
        run = self.live._agent_runs.current()
        self.assertIsNotNone(run)
        self.assertEqual(run.status, agent_run.IN_PROGRESS)
        self.assertEqual(executions, [])

        self.run_loop([
            tool_call_message(
                ("multi-work", "_web_multi_step", {"value": "one"}),
                turn_complete=True),
        ])
        run = self.live._agent_runs.current()
        self.assertEqual(executions, ["one"])
        self.assertGreaterEqual(run.tool_call_count, 2)
        self.assertTrue(any(item["tool"] == "_web_multi_step"
                            for item in run.tool_outcomes))
        self.assertEqual(run.status, agent_run.IN_PROGRESS)

        self.run_loop([
            tool_call_message(
                ("multi-done", "task_status",
                 {"operation": "complete",
                  "summary": "The multi-step task finished",
                  "evidence": ["_web_multi_step returned ok for the task."]}),
                turn_complete=True),
        ])
        run = self.live._agent_runs.current()
        self.assertEqual(run.status, agent_run.COMPLETED)
        self.assertEqual(run.result_summary, "The multi-step task finished")

    def test_approved_delete_resumes_the_original_call_and_returns_result(self):
        folder = tempfile.mkdtemp(prefix="jarvis-approved-delete-")
        self.addCleanup(shutil.rmtree, folder, True)
        target = os.path.join(folder, "delete-me.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("remove me")

        service = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=60, start_sweeper=False))
        service.set_session("test-conversation")
        self.addCleanup(approvals.shutdown_service)
        self.live._approvals = service
        service.set_listener(self.live._on_approval_event)

        self.run_loop([
            tool_call_message(
                ("delete-call", "file_control",
                 {"action": "delete", "source_path": target,
                  "permanent": True}),
                turn_complete=True),
        ])
        self.assertTrue(os.path.exists(target))
        request = service.active_requests()[0]
        self.assertEqual(request["status"], "pending")
        self.assertTrue(tool_calling.pending_approval(
            request["approval_id"]))

        class Recorder:
            def __init__(self):
                self.contexts = []

            async def send_client_content(self, **kwargs):
                self.contexts.append(kwargs)

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        recorder = Recorder()
        self.live._loop = loop
        self.live.session = recorder
        try:
            service.approve(request["approval_id"])
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and os.path.exists(target):
                time.sleep(0.02)
            self.assertFalse(os.path.exists(target))
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not recorder.contexts:
                time.sleep(0.02)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2.0)
            loop.close()
        text = recorder.contexts[-1]["turns"]["parts"][0]["text"]
        self.assertIn("APPROVED OPERATION RESULT", text)
        self.assertIn("Deleted", text)
        # This arrives after the model's turn completed; it must be a complete
        # user turn so the model actually responds to the real result.
        self.assertTrue(recorder.contexts[-1]["turn_complete"])
        self.assertIsNone(tool_calling.pending_approval(
            request["approval_id"]))
        self.assertEqual(service.active_requests(), [])

    def test_approved_delete_resumes_an_agent_run(self):
        folder = tempfile.mkdtemp(prefix="jarvis-approved-run-")
        self.addCleanup(shutil.rmtree, folder, True)
        target = os.path.join(folder, "run-delete.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("remove me")

        service = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=60, start_sweeper=False))
        service.set_session("test-conversation")
        self.addCleanup(approvals.shutdown_service)
        self.live._approvals = service
        service.set_listener(self.live._on_approval_event)
        run = self.live._agent_runs.start("Delete the approved file")

        self.run_loop([
            tool_call_message(
                ("run-delete", "file_control",
                 {"action": "delete", "source_path": target,
                  "permanent": True}),
                turn_complete=True),
        ])
        self.assertEqual(self.live._agent_runs.current().status,
                         agent_run.NEEDS_USER)
        request = service.active_requests()[0]
        entry = tool_calling.pending_approval(request["approval_id"])
        self.assertEqual(entry["run_id"], run.run_id)

        class Recorder:
            def __init__(self):
                self.contexts = []

            async def send_client_content(self, **kwargs):
                self.contexts.append(kwargs)

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        recorder = Recorder()
        self.live._loop = loop
        self.live.session = recorder
        try:
            service.approve(request["approval_id"])
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and os.path.exists(target):
                time.sleep(0.02)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not recorder.contexts:
                time.sleep(0.02)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2.0)
            loop.close()
        current = self.live._agent_runs.current()
        self.assertFalse(os.path.exists(target))
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.IN_PROGRESS)
        self.assertTrue(any(item["tool"] == "file_control" and item["ok"]
                            for item in current.tool_outcomes))
        self.assertIn("APPROVED OPERATION RESULT",
                      recorder.contexts[-1]["turns"]["parts"][0]["text"])

    def test_sleep_request_blocks_the_run_and_does_not_fail_it(self):
        run = self.live._agent_runs.start("Lock and sleep")
        with mock.patch.object(sysctl, "IS_WINDOWS", True), \
             mock.patch.object(sysctl, "_windows_sleep_workstation",
                               return_value=True):
            self.run_loop([
                tool_call_message(
                    ("sleep-call", "system_control",
                     {"operation": "sleep_workstation"}),
                    turn_complete=True),
            ])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.BLOCKED)
        self.assertEqual(current.failed_tool_calls, 0)
        self.assertTrue(self.live._agent_terminal_until_user)

    def test_lock_request_leaves_the_run_active(self):
        run = self.live._agent_runs.start("Lock only")
        with mock.patch.object(sysctl, "IS_WINDOWS", True), \
             mock.patch.object(sysctl, "_windows_lock_workstation",
                               return_value=True):
            self.run_loop([
                tool_call_message(
                    ("lock-call", "system_control",
                     {"operation": "lock_workstation"}),
                    turn_complete=True),
            ])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.IN_PROGRESS)
        self.assertTrue(any(item["tool"] == "system_control" and item["ok"]
                            for item in current.tool_outcomes))

    def test_pending_phone_call_does_not_complete_the_run(self):
        run = self.live._agent_runs.start("Call Alice")
        fake = SimpleNamespace(
            settings=lambda: {"username": "u", "password": "p"},
            call=lambda target: {
                "ok": True, "success": True, "state": "starting",
                "connected": False, "pending_connection": True,
                "result": "Calling sip:alice@example.test; waiting for answer.",
                "call": {"state": "dialing", "phase": "starting"}})
        with mock.patch.object(
                phone_call_tool, "_service", return_value=fake), \
             mock.patch.object(
                phone_call_tool, "get_call_allowlist",
                return_value=["sip:alice@example.test"]), \
             mock.patch.object(
                phone_call_tool.telephony, "resolve_target",
                side_effect=lambda value, settings=None: value):
            self.run_loop([
                tool_call_message(
                    ("phone-call-1", "call",
                     {"number_or_sip_address": "sip:alice@example.test"}),
                    turn_complete=True),
            ])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.IN_PROGRESS)
        self.assertEqual(current.failed_tool_calls, 0)
        self.assertIn("awaiting SIP answer", current.current_step)
        self.assertFalse(any(
            item["tool"] == "call" and item["ok"]
            for item in current.tool_outcomes))

    def test_manual_stop_cancels_the_active_run(self):
        run = self.live._agent_runs.start("Stop me")
        self.live.interrupt()
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.CANCELLED)
        self.assertTrue(current.cancel_requested)

    def test_manual_stop_cancels_active_research(self):
        service = research_tool.reset_service()
        self.addCleanup(research_tool.reset_service)
        job_id = service.begin("Long research", "deep", 8)
        self.assertTrue(job_id)

        self.live.interrupt()

        state = service.public_state()
        self.assertEqual(state["status"], "cancelled")
        self.assertTrue(state["cancelled"])
        self.assertEqual(state["job_id"], job_id)
        self.assertIsNone(state["report"])
        self.assertTrue(service.cancelled())

    def test_manual_stop_stops_screen_observation(self):
        with mock.patch.object(
                main.screen_understanding_tool,
                "shutdown_observation") as stop_mock:
            self.live.interrupt()
        stop_mock.assert_called_once_with("user interrupted")

    def test_agent_run_cancellation_propagates_to_research(self):
        service = research_tool.reset_service()
        self.addCleanup(research_tool.reset_service)
        service.begin("Run-owned research", "deep", 8)
        self.live._agent_runs.start("Research task")

        self.live._cancel_agent_run("conversation changed")

        state = service.public_state()
        self.assertEqual(state["status"], "cancelled")
        self.assertIsNone(state["report"])

    def test_manual_stop_without_a_run_blocks_tools_until_a_new_turn(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"ok": value}

        tool_calling.register_tool(
            "_web_after_stop", counted, "post-stop dispatch test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(
            tool_calling.TOOL_REGISTRY.pop, "_web_after_stop", None)
        self.assertIsNone(self.live._agent_runs.current())

        self.live.interrupt()
        self.assertTrue(self.live._agent_terminal_until_user)

        # A late batch from the stopped turn must not execute or start a run.
        self.run_loop([
            tool_call_message(("late-call", "_web_after_stop",
                               {"value": "late"}), turn_complete=True),
        ])
        self.assertEqual(executions, [])
        self.assertIsNone(self.live._agent_runs.current())
        payload = dict(self.live.session.sent[-1][0].response)["result"]
        self.assertIn("no longer active", payload["error"])

        # The next real user turn releases the lock.
        self.assertFalse(self.live.send_text("A real new request"))
        self.assertFalse(self.live._agent_terminal_until_user)
        self.run_loop([
            tool_call_message(("fresh-call", "_web_after_stop",
                               {"value": "fresh"}), turn_complete=True),
        ])
        self.assertEqual(executions, ["fresh"])

    def test_barge_in_keeps_the_active_run(self):
        run = self.live._agent_runs.start("Keep me")
        self.live._begin_barge_in(b"\x00\x00")
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.IN_PROGRESS)
        self.assertFalse(current.cancel_requested)

    def test_server_interruption_keeps_the_active_run(self):
        run = self.live._agent_runs.start("Keep me")
        self.live._handle_server_interruption()
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.IN_PROGRESS)

    def test_cancelled_run_blocks_tool_dispatch(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"ok": value}

        tool_calling.register_tool(
            "_web_blocked", counted, "blocked dispatch test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_blocked", None)
        run = self.live._agent_runs.start("Cancelled task")
        self.live._agent_runs.cancel(run_id=run.run_id, reason="test stop")

        self.run_loop([
            tool_call_message(("blocked-call", "_web_blocked",
                               {"value": "x"}), turn_complete=True),
        ])
        self.assertEqual(executions, [])
        self.assertEqual(len(self.live.session.sent[0]), 1)
        payload = dict(self.live.session.sent[0][0].response)["result"]
        self.assertIn("error", payload)
        self.assertIn("test stop", payload["error"])

    def test_completed_run_blocks_same_turn_batches(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"ok": value}

        tool_calling.register_tool(
            "_web_terminal", counted, "terminal dispatch test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_terminal", None)
        run = self.live._agent_runs.start("Completed task")
        self.live._agent_runs.complete(run_id=run.run_id, summary="done")

        self.run_loop([
            tool_call_message(("terminal-call", "_web_terminal",
                               {"value": "x"}), turn_complete=True),
        ])
        self.assertEqual(executions, [])
        payload = dict(self.live.session.sent[0][0].response)["result"]
        self.assertIn("completed", payload["error"])

    def test_blocked_completed_operation_does_not_consume_a_step(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"opened": value}

        tool_calling.register_tool(
            "_web_once", counted, "duplicate operation test tool",
            {"type": "OBJECT", "properties": {}, "required": []},
            )
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_once", None)
        self.live._agent_runs.start("Open the app")

        with mock.patch.object(
                agent_run, "_SIDE_EFFECT_TOOLS", frozenset({"_web_once"})):
            self.run_loop([
                tool_call_message(("once-1", "_web_once", {"value": "a"}),
                                  turn_complete=True),
            ])
            self.assertEqual(executions, ["a"])
            self.assertEqual(self.live._agent_runs.current().step_count, 1)

            self.run_loop([
                tool_call_message(("once-2", "_web_once", {"value": "a"}),
                                  turn_complete=True),
            ])
        # The duplicate is refused before begin_execution, so it neither runs
        # nor consumes another step.
        self.assertEqual(executions, ["a"])
        self.assertEqual(self.live._agent_runs.current().step_count, 1)
        payload = dict(self.live.session.sent[-1][0].response)["result"]
        self.assertIn("already completed", payload["error"])

    def test_mixed_batch_only_counts_dispatchable_calls(self):
        executions = []

        def first(value=""):
            executions.append(("first", value))
            return {"ok": value}

        def second(value=""):
            executions.append(("second", value))
            return {"ok": value}

        for name, fn in (("_web_mix_a", first), ("_web_mix_b", second)):
            tool_calling.register_tool(
                name, fn, "mixed batch test tool",
                {"type": "OBJECT", "properties": {}, "required": []})
            self.addCleanup(tool_calling.TOOL_REGISTRY.pop, name, None)
        run = self.live._agent_runs.start(
            "Mixed batch",
            limits=agent_run.AgentRunLimits(
                max_steps=10, max_tool_calls=2,
                max_consecutive_failures=3, max_duration_seconds=60))

        with mock.patch.object(
                agent_run, "_SIDE_EFFECT_TOOLS",
                frozenset({"_web_mix_a", "_web_mix_b"})):
            self.run_loop([
                tool_call_message(("mix-a", "_web_mix_a", {"value": "one"}),
                                  turn_complete=True),
            ])
            # The duplicate is refused by the guard, so the batch has only one
            # dispatchable call and fits the remaining budget.
            self.run_loop([
                tool_call_message(
                    ("mix-a-dup", "_web_mix_a", {"value": "one"}),
                    ("mix-b", "_web_mix_b", {"value": "two"}),
                    turn_complete=True),
            ])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.IN_PROGRESS)
        self.assertEqual(executions, [("first", "one"), ("second", "two")])
        self.assertEqual(current.tool_call_count, 2)

    def test_task_start_and_work_in_one_message_are_tracked(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"opened": value}

        tool_calling.register_tool(
            "_web_same_turn", counted, "same-turn task test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(
            tool_calling.TOOL_REGISTRY.pop, "_web_same_turn", None)

        self.run_loop([
            tool_call_message(
                ("same-start", "task_status",
                 {"operation": "start", "objective": "Open the app"}),
                ("same-work", "_web_same_turn", {"value": "a"}),
                turn_complete=True),
        ])
        current = self.live._agent_runs.current()
        self.assertIsNotNone(current)
        self.assertEqual(current.objective, "Open the app")
        self.assertEqual(executions, ["a"])
        self.assertEqual(current.step_count, 1)
        self.assertEqual(current.tool_call_count, 2)
        self.assertTrue(any(
            item["tool"] == "_web_same_turn" for item in current.tool_outcomes))

    def test_task_start_after_a_finished_run_is_tracked(self):
        old = self.live._agent_runs.start("Old task")
        self.live._agent_runs.complete(run_id=old.run_id, summary="done")
        # A new user turn releases the post-completion execution lock.
        self.live._agent_terminal_until_user = False
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"ok": True, "value": value}

        tool_calling.register_tool(
            "_web_after_terminal", counted, "post-terminal task test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(
            tool_calling.TOOL_REGISTRY.pop, "_web_after_terminal", None)

        self.run_loop([
            tool_call_message(
                ("after-start", "task_status",
                 {"operation": "start", "objective": "New objective"}),
                ("after-work", "_web_after_terminal", {"value": "b"}),
                turn_complete=True),
        ])
        current = self.live._agent_runs.current()
        self.assertIsNotNone(current)
        self.assertNotEqual(current.run_id, old.run_id)
        self.assertEqual(current.objective, "New objective")
        self.assertEqual(executions, ["b"])
        self.assertEqual(current.tool_call_count, 2)
        self.assertTrue(any(
            item["tool"] == "_web_after_terminal"
            for item in current.tool_outcomes))

    def test_receive_loop_stays_responsive_while_tool_runs(self):
        started = threading.Event()
        release = threading.Event()

        def blocking():
            started.set()
            release.wait(2.0)
            return {"ok": True}

        tool_calling.register_tool(
            "_web_responsive", blocking, "receive loop test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(
            tool_calling.TOOL_REGISTRY.pop, "_web_responsive", None)

        class ResponsiveSession:
            def __init__(self):
                self.sent = []
                self.allow_second = asyncio.Event()

            def receive(self):
                async def generator():
                    yield tool_call_message(
                        ("responsive-call", "_web_responsive", {}),
                        turn_complete=True)
                    await self.allow_second.wait()
                    yield server_message(
                        out_text="Still responsive.", turn_complete=True)
                    raise asyncio.CancelledError
                return generator()

            async def send_tool_response(self, function_responses):
                self.sent.append(list(function_responses))

            async def send_client_content(self, **kwargs):
                pass

        live = self.live
        session = ResponsiveSession()
        live.session = session

        async def scenario():
            task = asyncio.create_task(live._receive_audio())
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            session.allow_second.set()
            await asyncio.sleep(0.05)
            # The assistant transcript arrived while the tool was still blocked.
            self.assertTrue(any(
                event["type"] == "chat.delta"
                and event.get("text") == "Still responsive."
                for event in self.hub.events))
            self.assertFalse(release.is_set())
            release.set()
            chain = getattr(live, "_tool_chain", None)
            if chain is not None:
                await chain
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        self.assertEqual(len(session.sent), 1)

    def test_stale_tool_result_does_not_update_a_newer_run(self):
        first = self.live._agent_runs.start("First task")
        self.live._agent_runs.complete(run_id=first.run_id, summary="done")
        second = self.live._agent_runs.start("Second task")

        call = SimpleNamespace(id="stale-call", name="open_app", args={})
        response = tool_calling.make_function_response(
            call, {"opened": "Calculator"})
        self.live._record_agent_results(
            [call], [response], first.run_id)

        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, second.run_id)
        self.assertEqual(current.tool_call_count, 0)
        self.assertEqual(current.last_tool_summary, "")

    def test_expired_run_is_blocked_before_tool_dispatch(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"ok": value}

        tool_calling.register_tool(
            "_web_expired", counted, "expired run test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_expired", None)
        run = self.live._agent_runs.start(
            "Expired task",
            limits=agent_run.AgentRunLimits(
                max_steps=10, max_tool_calls=10,
                max_consecutive_failures=3,
                max_duration_seconds=0.01))
        time.sleep(0.02)

        self.run_loop([
            tool_call_message(("expired-call", "_web_expired",
                               {"value": "x"}), turn_complete=True),
        ])
        self.assertEqual(executions, [])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.FAILED)
        payload = dict(self.live.session.sent[0][0].response)["result"]
        self.assertIn("error", payload)

    def test_step_limit_blocks_the_next_tool_batch(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"ok": value}

        tool_calling.register_tool(
            "_web_steps", counted, "step limit test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_steps", None)
        run = self.live._agent_runs.start(
            "One step only",
            limits=agent_run.AgentRunLimits(
                max_steps=1, max_tool_calls=10,
                max_consecutive_failures=3,
                max_duration_seconds=60))

        self.run_loop([
            tool_call_message(("step-a", "_web_steps", {"value": "a"}),
                              turn_complete=True),
        ])
        self.assertEqual(executions, ["a"])

        self.run_loop([
            tool_call_message(("step-b", "_web_steps", {"value": "b"}),
                              turn_complete=True),
        ])
        self.assertEqual(executions, ["a"])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.FAILED)
        self.assertIn("Step limit", current.last_error)
        payload = dict(self.live.session.sent[-1][0].response)["result"]
        self.assertIn("error", payload)

    def test_consecutive_failure_limit_stops_the_next_call_in_the_batch(self):
        executions = []

        def failing(value=""):
            executions.append(value)
            raise RuntimeError(f"failed {value}")

        tool_calling.register_tool(
            "_web_fail", failing, "failure limit test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_fail", None)
        run = self.live._agent_runs.start(
            "Fail once",
            limits=agent_run.AgentRunLimits(
                max_steps=10, max_tool_calls=10,
                max_consecutive_failures=1,
                max_duration_seconds=60))

        self.run_loop([
            tool_call_message(
                ("fail-a", "_web_fail", {"value": "a"}),
                ("fail-b", "_web_fail", {"value": "b"}),
                turn_complete=True),
        ])
        self.assertEqual(executions, ["a"])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.FAILED)
        self.assertEqual(current.consecutive_failures, 1)
        self.assertIn("Consecutive failure", current.last_error)
        self.assertEqual(len(self.live.session.sent[0]), 2)
        second = dict(self.live.session.sent[0][1].response)["result"]
        self.assertIn("error", second)

    def test_soft_failure_result_counts_as_a_failed_operation(self):
        def soft_failure():
            return {"ok": False, "message": "service unavailable"}

        tool_calling.register_tool(
            "_web_softfail", soft_failure, "soft failure test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_softfail", None)
        run = self.live._agent_runs.start(
            "Soft fail",
            limits=agent_run.AgentRunLimits(
                max_steps=10, max_tool_calls=10,
                max_consecutive_failures=1,
                max_duration_seconds=60))

        self.run_loop([
            tool_call_message(("soft-fail", "_web_softfail", {}),
                              turn_complete=True),
        ])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.consecutive_failures, 1)
        self.assertEqual(current.status, agent_run.FAILED)
        self.assertIn("service unavailable", current.last_tool_summary)

    def test_failed_tool_result_carries_failure_analysis_to_gemini(self):
        def missing_file():
            return {"error": "not found: report.pdf", "reason": "missing"}

        tool_calling.register_tool(
            "_web_missing", missing_file, "missing resource test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_missing", None)
        run = self.live._agent_runs.start("Find report.pdf")

        self.run_loop([
            tool_call_message(("missing-call", "_web_missing", {}),
                              turn_complete=True),
        ])
        payload = dict(self.live.session.sent[0][0].response)["result"]
        self.assertEqual(payload["error"], "not found: report.pdf")
        analysis = payload["failure_analysis"]
        self.assertEqual(analysis["category"], "missing")
        self.assertTrue(analysis["recoverable"])
        self.assertFalse(analysis["retry_same_call"])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.failures[-1]["category"], "missing")
        self.assertTrue(current.verification_required)
        self.assertTrue(self.live.session.contexts)
        context_text = "\n".join(
            part.get("text", "")
            for call in self.live.session.contexts
            for part in call.get("turns", {}).get("parts", []))
        self.assertIn("ACTIVE TASK CONTEXT", context_text)
        self.assertIn("Find report.pdf", context_text)
        self.assertIn("not found: report.pdf", context_text)

    def test_repeated_failed_operation_is_blocked_before_execution(self):
        executions = []

        def counted(value=""):
            executions.append(value)
            return {"ok": value}

        tool_calling.register_tool(
            "_web_guard", counted, "guard test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_guard", None)
        run = self.live._agent_runs.start("Do not repeat")

        call = SimpleNamespace(id="guard-1", name="_web_guard",
                               args={"value": "x"})
        signature = agent_run.call_signature(call)
        self.live._agent_runs.record_tool_result(
            run_id=run.run_id, tool_name="_web_guard", ok=False,
            summary="not found", failure=agent_run.classify_failure(
                {"error": "not found"}),
            signature=signature)

        self.run_loop([
            tool_call_message(("guard-2", "_web_guard", {"value": "x"}),
                              turn_complete=True),
        ])
        self.assertEqual(executions, [])
        payload = dict(self.live.session.sent[0][0].response)["result"]
        self.assertTrue(payload.get("recovery_blocked"))
        self.assertIn("Repeated failed operation", payload["error"])
        self.assertEqual(
            self.live._agent_runs.current().tool_call_count, 1)

    def test_verification_rejection_does_not_consume_failure_budget(self):
        run = self.live._agent_runs.start(
            "Analyze the report",
            limits=agent_run.AgentRunLimits(
                max_steps=10, max_tool_calls=10,
                max_consecutive_failures=1,
                max_duration_seconds=60))
        self.run_loop([
            tool_call_message(
                ("verify-missing", "task_status",
                 {"operation": "complete", "summary": "Done"}),
                turn_complete=True),
        ])
        payload = dict(self.live.session.sent[0][0].response)["result"]
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["missing_evidence"])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.IN_PROGRESS)
        self.assertEqual(current.consecutive_failures, 0)
        self.assertEqual(current.verification_attempts, 1)

    def test_completion_with_evidence_succeeds_through_the_live_path(self):
        run = self.live._agent_runs.start("Analyze the report")
        self.run_loop([
            tool_call_message(
                ("verify-ok", "task_status",
                 {"operation": "complete", "summary": "Done",
                  "evidence": ["The report states that revenue increased."]}),
                turn_complete=True),
        ])
        payload = dict(self.live.session.sent[0][0].response)["result"]
        self.assertTrue(payload["ok"])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.COMPLETED)

    def test_tool_timeout_is_recorded_in_the_active_run(self):
        def slow():
            time.sleep(0.2)
            return {"ok": True}

        tool_calling.register_tool(
            "_web_slow_timeout", slow, "timeout test tool",
            {"type": "OBJECT", "properties": {}, "required": []},
            timeout_s=0.05)
        self.addCleanup(
            tool_calling.TOOL_REGISTRY.pop, "_web_slow_timeout", None)
        run = self.live._agent_runs.start("Wait for a slow tool")

        self.run_loop([
            tool_call_message(("slow-timeout", "_web_slow_timeout", {}),
                              turn_complete=True),
        ])
        payload = dict(self.live.session.sent[0][0].response)["result"]
        self.assertTrue(payload["timeout"])
        self.assertEqual(payload["error_code"], "tool_timeout")
        self.assertEqual(payload["failure_analysis"]["category"], "timeout")
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.failures[-1]["category"], "timeout")

    def test_duration_watchdog_closes_run_without_model_activity(self):
        run = self.live._agent_runs.start(
            "Quiet task",
            limits=agent_run.AgentRunLimits(
                max_steps=10, max_tool_calls=10,
                max_consecutive_failures=3,
                max_duration_seconds=0.01))
        time.sleep(0.02)
        self.live._check_agent_duration()
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.FAILED)
        self.assertIn("duration", current.last_error.lower())
        events = [event for event in self.hub.events
                  if event["type"] == "agent.updated"]
        self.assertEqual(events[-1]["event"], "limit")
        self.assertEqual(events[-1]["limit"], "max_duration_seconds")

    def test_stop_during_execution_cancels_and_ignores_the_late_result(self):
        started = threading.Event()
        release = threading.Event()
        executions = []

        def blocking(value=""):
            started.set()
            if not release.wait(2.0):
                return {"error": "release timeout"}
            executions.append(value)
            return {"opened": value}

        tool_calling.register_tool(
            "_web_slow", blocking, "slow execution test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop, "_web_slow", None)
        run = self.live._agent_runs.start("Slow task")

        class SlowSession:
            def __init__(self):
                self.sent = []

            def receive(self):
                async def generator():
                    yield tool_call_message(
                        ("slow-call", "_web_slow", {"value": "slow"}),
                        turn_complete=True)
                    await asyncio.to_thread(release.wait, 2.0)
                    raise asyncio.CancelledError
                return generator()

            async def send_tool_response(self, function_responses):
                self.sent.append(list(function_responses))

            async def send_client_content(self, **kwargs):
                pass

        session = SlowSession()
        self.live.session = session

        async def scenario():
            task = asyncio.create_task(self.live._receive_audio())
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            self.live.interrupt()
            release.set()
            chain = getattr(self.live, "_tool_chain", None)
            if chain is not None:
                await chain
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        # The worker thread cannot be killed, but its late result must not be
        # counted against the cancelled run.
        self.assertEqual(executions, ["slow"])
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.tool_call_count, 0)
        self.assertEqual(current.last_tool_summary, "")
        self.assertTrue(current.cancel_requested)

    def test_approval_required_marks_needs_user_without_a_failure(self):
        service = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=60, start_sweeper=False))
        service.set_session("test-conversation")
        self.addCleanup(approvals.shutdown_service)

        handle = tempfile.NamedTemporaryFile(
            prefix="jarvis-approval-", suffix=".txt", delete=False)
        handle.write(b"x")
        handle.close()
        target = handle.name
        self.addCleanup(lambda: os.path.exists(target) and os.remove(target))
        recycle = mock.patch.object(
            file_control_tool, "_send_to_recycle_bin",
            lambda path: (os.remove(path) or True, ""))
        recycle.start()
        self.addCleanup(recycle.stop)

        run = self.live._agent_runs.start("Delete the temporary file")
        calls = [tool_call_message(
            ("approval-delete", "file_control",
             {"action": "delete", "source_path": target}),
            turn_complete=True)]

        self.run_loop(calls)
        current = self.live._agent_runs.current()
        self.assertEqual(current.run_id, run.run_id)
        self.assertEqual(current.status, agent_run.NEEDS_USER)
        self.assertEqual(current.failures, [])
        self.assertEqual(current.tool_call_count, 0)
        self.assertTrue(os.path.exists(target))
        payload = dict(self.live.session.sent[-1][0].response)["result"]
        self.assertTrue(payload["approval_required"])
        self.assertNotIn("confirmation_token", payload)

        request = service.active_requests()[0]
        service.approve(request["approval_id"])
        self.run_loop(calls)
        current = self.live._agent_runs.current()
        self.assertEqual(current.tool_call_count, 1)
        self.assertEqual(current.failures, [])
        self.assertFalse(os.path.exists(target))
        payload = dict(self.live.session.sent[-1][0].response)["result"]
        self.assertTrue(payload["success"])

    def test_a_new_typed_turn_releases_the_terminal_execution_lock(self):
        self.live._agent_terminal_until_user = True
        self.live._loop = None
        self.assertFalse(self.live.send_text("A new request"))
        self.assertFalse(self.live._agent_terminal_until_user)


if __name__ == "__main__":
    unittest.main()

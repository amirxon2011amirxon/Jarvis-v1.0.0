"""Foundational AgentRun state, limits, tool status and event tests.

These tests never touch Gemini, audio devices or the user's real memory.  They
exercise the run component directly and through the existing task_status tool.
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace

from core import agent_run
from core.agent_run import (
    ACTIVE_STATUSES,
    CANCELLED,
    COMPLETED,
    FAILED,
    IN_PROGRESS,
    NEEDS_USER,
    AgentRun,
    AgentRunLimitError,
    AgentRunLimits,
    AgentRunService,
    AgentRunStateError,
    AgentRunVerificationError,
    call_signature,
    classify_failure,
    task_status,
)


class _Hub:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(dict(event))


class AgentRunCreationTests(unittest.TestCase):
    def test_snapshot_contains_the_tracked_fields(self):
        run = AgentRun(objective="  Plan the trip  ", conversation_id="conv-1")
        snapshot = run.to_dict()

        self.assertEqual(snapshot["objective"], "Plan the trip")
        self.assertEqual(snapshot["conversation_id"], "conv-1")
        self.assertEqual(snapshot["status"], IN_PROGRESS)
        self.assertEqual(snapshot["step_count"], 0)
        self.assertEqual(snapshot["tool_call_count"], 0)
        self.assertEqual(snapshot["consecutive_failures"], 0)
        self.assertFalse(snapshot["cancel_requested"])
        self.assertTrue(snapshot["run_id"])
        self.assertTrue(snapshot["started_at"])
        self.assertTrue(snapshot["updated_at"])
        self.assertIn("max_steps", snapshot["limits"])
        self.assertFalse(snapshot["is_terminal"])

    def test_objective_is_required(self):
        with self.assertRaises(ValueError):
            AgentRun(objective="   ")

    def test_status_transitions(self):
        run = AgentRun(objective="Do the work")
        run.set_status(NEEDS_USER, current_step="Waiting for a choice")
        self.assertEqual(run.to_dict()["status"], NEEDS_USER)
        self.assertIn(NEEDS_USER, ACTIVE_STATUSES)

        run.set_status(IN_PROGRESS, current_step="Continuing")
        self.assertEqual(run.to_dict()["status"], IN_PROGRESS)

        run.set_status(agent_run.BLOCKED, current_step="Waiting on access")
        self.assertEqual(run.to_dict()["status"], agent_run.BLOCKED)
        run.set_status(IN_PROGRESS, current_step="Access granted")

        run.complete("Finished")
        self.assertEqual(run.status, COMPLETED)
        self.assertTrue(run.is_terminal)
        self.assertEqual(run.result_summary, "Finished")
        self.assertTrue(run.finished_at)

    def test_terminal_state_cannot_be_revived(self):
        run = AgentRun(objective="Do the work")
        run.complete("done")
        with self.assertRaises(AgentRunStateError):
            run.set_status(IN_PROGRESS)
        with self.assertRaises(AgentRunStateError):
            run.fail("late failure")

    def test_cancel_sets_the_requested_flag(self):
        run = AgentRun(objective="Do the work")
        run.cancel("user stopped")
        self.assertEqual(run.status, CANCELLED)
        self.assertTrue(run.cancel_requested)
        with self.assertRaises(agent_run.AgentRunError):
            run.set_status(IN_PROGRESS)


class ExecutionLimitTests(unittest.TestCase):
    def limits(self, **overrides):
        values = dict(max_steps=10, max_tool_calls=10,
                      max_consecutive_failures=3,
                      max_duration_seconds=60)
        values.update(overrides)
        return AgentRunLimits(**values)

    def test_step_limit_fails_the_run(self):
        run = AgentRun(objective="Steps", limits=self.limits(max_steps=2))
        run.record_step("one")
        run.record_step("two")
        with self.assertRaises(AgentRunLimitError) as raised:
            run.record_step("three")
        self.assertEqual(raised.exception.limit, "max_steps")
        self.assertEqual(run.status, FAILED)
        self.assertIn("Step limit", run.last_error)

    def test_tool_call_limit_fails_the_run(self):
        run = AgentRun(objective="Tools", limits=self.limits(max_tool_calls=2))
        run.record_tool_call(tool_name="a", ok=True, summary="ok")
        run.record_tool_call(tool_name="b", ok=True, summary="ok")
        with self.assertRaises(AgentRunLimitError) as raised:
            run.record_tool_call(tool_name="c", ok=True, summary="ok")
        self.assertEqual(raised.exception.limit, "max_tool_calls")
        self.assertEqual(run.status, FAILED)

    def test_success_and_failure_counters_survive_outcome_trimming(self):
        run = AgentRun(
            objective="Counters", limits=self.limits(max_tool_calls=100))
        for index in range(25):
            run.record_tool_call(
                tool_name="a", ok=True, summary=f"ok {index}")
        run.record_tool_call(tool_name="b", ok=False, summary="bad")
        snapshot = run.to_dict()
        self.assertEqual(snapshot["tool_call_count"], 26)
        self.assertEqual(snapshot["successful_tool_calls"], 25)
        self.assertEqual(snapshot["failed_tool_calls"], 1)
        # The bounded outcome list is intentionally capped; the counters are
        # the authoritative totals the Agent panel reports.
        self.assertEqual(len(snapshot["tool_outcomes"]), 20)

    def test_consecutive_failure_limit_and_recovery(self):
        run = AgentRun(
            objective="Failures",
            limits=self.limits(max_consecutive_failures=2))
        run.record_tool_call(tool_name="a", ok=False, summary="bad")
        run.record_tool_call(tool_name="b", ok=True, summary="good")
        self.assertEqual(run.consecutive_failures, 0)
        run.record_tool_call(tool_name="c", ok=False, summary="bad")
        with self.assertRaises(AgentRunLimitError) as raised:
            run.record_tool_call(tool_name="d", ok=False, summary="bad")
        self.assertEqual(
            raised.exception.limit, "max_consecutive_failures")
        self.assertEqual(run.status, FAILED)

    def test_duration_limit_fails_on_the_next_check(self):
        run = AgentRun(
            objective="Duration",
            limits=self.limits(max_duration_seconds=0.01))
        time.sleep(0.02)
        with self.assertRaises(AgentRunLimitError) as raised:
            run.check_limits()
        self.assertEqual(raised.exception.limit, "max_duration_seconds")
        self.assertEqual(run.status, FAILED)

    def test_tool_capacity_is_checked_before_execution(self):
        run = AgentRun(
            objective="Capacity", limits=self.limits(max_tool_calls=2))
        run.ensure_tool_capacity(2)
        run.record_tool_call(tool_name="a", ok=True, summary="ok")
        run.record_tool_call(tool_name="b", ok=True, summary="ok")
        with self.assertRaises(AgentRunLimitError) as raised:
            run.ensure_tool_capacity(1)
        self.assertEqual(raised.exception.limit, "max_tool_calls")
        self.assertEqual(run.status, FAILED)

    def test_begin_execution_rejects_a_terminal_run(self):
        service = AgentRunService()
        run = service.start("Terminal")
        service.complete(run_id=run.run_id, summary="done")
        self.assertFalse(service.begin_execution(
            run.run_id, step="late", tool_call_count=1))
        self.assertEqual(service.current().status, COMPLETED)

    def test_begin_execution_checks_duration_before_dispatch(self):
        service = AgentRunService()
        run = service.start(
            "Expired",
            limits=self.limits(max_duration_seconds=0.01))
        time.sleep(0.02)
        self.assertFalse(service.begin_execution(
            run.run_id, step="late", tool_call_count=1))
        self.assertEqual(service.current().status, FAILED)
        self.assertIn(
            "duration", service.current().last_error.lower())


class ServiceTests(unittest.TestCase):
    def setUp(self):
        agent_run.reset_service(None)
        self.addCleanup(agent_run.reset_service, None)
        self.service = agent_run.get_service()
        self.service.set_conversation_id("conv-1")

    def test_start_update_and_complete(self):
        run = self.service.start("Plan the trip")
        self.assertEqual(run.status, IN_PROGRESS)
        self.assertEqual(run.conversation_id, "conv-1")

        self.service.record_step(run.run_id, "Searching flights")
        self.service.record_tool_result(
            run_id=run.run_id, tool_name="web_fetch", ok=True,
            summary="found options")
        self.service.update(run_id=run.run_id, summary="Comparing prices")
        completed = self.service.complete(
            run_id=run.run_id, summary="Trip planned")

        self.assertEqual(completed.status, COMPLETED)
        self.assertEqual(completed.step_count, 1)
        self.assertEqual(completed.tool_call_count, 1)
        self.assertEqual(completed.last_tool_summary,
                         "web_fetch: found options")

    def test_only_one_active_run_can_exist(self):
        self.service.start("First task")
        with self.assertRaises(AgentRunStateError):
            self.service.start("Second task")
        self.service.complete(summary="done")
        second = self.service.start("Second task")
        self.assertEqual(second.objective, "Second task")

    def test_update_without_a_run_is_an_error(self):
        with self.assertRaises(AgentRunStateError):
            self.service.update(step="nothing")

    def test_cancel_is_idempotent(self):
        self.service.start("Task")
        cancelled = self.service.cancel(reason="user stopped")
        self.assertEqual(cancelled.status, CANCELLED)
        again = self.service.cancel(reason="again")
        self.assertEqual(again.status, CANCELLED)
        self.assertEqual(again.run_id, cancelled.run_id)
        self.assertFalse(self.service.can_continue(cancelled.run_id))

    def test_begin_execution_checks_limits_before_dispatch(self):
        run = self.service.start(
            "Budget",
            limits=AgentRunLimits(
                max_steps=1, max_tool_calls=2,
                max_consecutive_failures=3,
                max_duration_seconds=60))
        self.assertTrue(self.service.begin_execution(
            run.run_id, step="first", tool_call_count=2))
        self.assertFalse(self.service.begin_execution(
            run.run_id, step="second", tool_call_count=1))
        self.assertEqual(self.service.current().status, FAILED)


class TaskStatusToolTests(unittest.TestCase):
    def setUp(self):
        agent_run.reset_service(None)
        self.addCleanup(agent_run.reset_service, None)
        agent_run.get_service().set_conversation_id("conv-tool")

    def test_start_update_status_and_complete(self):
        started = task_status("start", objective="Research a topic")
        self.assertTrue(started["ok"])
        run_id = started["run"]["run_id"]
        self.assertEqual(started["run"]["status"], IN_PROGRESS)

        updated = task_status(
            "update", status="needs_user", step="Ask which source to use")
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["run"]["run_id"], run_id)
        self.assertEqual(updated["run"]["status"], NEEDS_USER)

        status = task_status("status")
        self.assertTrue(status["ok"])
        self.assertEqual(status["run"]["run_id"], run_id)

        completed = task_status(
            "complete", summary="Research finished",
            evidence=["Research report cites three independent sources."])
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["run"]["status"], COMPLETED)

    def test_start_requires_an_objective(self):
        result = task_status("start")
        self.assertFalse(result["ok"])
        self.assertIn("objective", result["error"])
        self.assertIsNone(result["run"])

    def test_registered_and_executable_through_the_existing_registry(self):
        from actions import tool_calling

        self.assertIn("task_status", tool_calling.TOOL_REGISTRY)
        declared = {
            item["name"]: item
            for item in tool_calling.get_tool_declarations()
        }
        self.assertIn("task_status", declared)
        result = tool_calling.execute_tool(
            "task_status",
            {"operation": "start", "objective": "Use the registry"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["run"]["objective"], "Use the registry")

    def test_update_without_a_run_is_an_error(self):
        result = task_status("update", step="do something")
        self.assertFalse(result["ok"])
        self.assertIn("no active task run", result["error"])

    def test_status_without_a_run_is_not_an_error(self):
        result = task_status("status")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["run"])

    def test_artifacts_are_attached(self):
        result = task_status(
            "start", objective="Find the report",
            artifacts=["C:/reports/latest.pdf"])
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["run"]["artifacts"][0]["name"],
            "C:/reports/latest.pdf")

    def test_fail_and_cancel(self):
        task_status("start", objective="One")
        failed = task_status("fail", reason="No access")
        self.assertTrue(failed["ok"])
        self.assertEqual(failed["run"]["status"], FAILED)
        self.assertEqual(failed["run"]["last_error"], "No access")

        task_status("start", objective="Two")
        cancelled = task_status("cancel", reason="User stopped")
        self.assertTrue(cancelled["ok"])
        self.assertEqual(cancelled["run"]["status"], CANCELLED)
        self.assertTrue(cancelled["run"]["cancel_requested"])


class FailureAnalysisTests(unittest.TestCase):
    def test_failure_categories_are_distinct(self):
        cases = {
            "temporary": {"error": "service unavailable, try again"},
            "invalid": {"error": "Bad arguments for open_app"},
            "missing": {"error": "no installed app is named Foo"},
            "permission": {"error": "permission denied"},
            "unsupported": {"error": "not supported on this platform"},
            "timeout": {"error": "the operation timed out"},
            "blocker": {"error": "the task is blocked; ask the user"},
        }
        for expected, result in cases.items():
            with self.subTest(expected=expected):
                analysis = classify_failure(result)
                self.assertEqual(analysis["category"], expected)
        confirmation = classify_failure({"confirmation_required": True})
        self.assertEqual(
            confirmation["category"], "confirmation_required")
        self.assertTrue(confirmation["requires_user"])
        self.assertFalse(confirmation["retry_same_call"])
        unknown = classify_failure({"error": "something peculiar happened"})
        self.assertEqual(unknown["category"], "unknown")
        self.assertFalse(unknown["recoverable"])

    def test_failure_is_preserved_and_added_to_context(self):
        run = AgentRun(objective="Open the demo")
        failure = classify_failure({"error": "no installed app is named Foo"})
        run.record_tool_call(
            tool_name="open_app", ok=False,
            summary="no installed app is named Foo", failure=failure,
            signature="open_app\x00foo")
        self.assertIn("no installed app", run.last_error)
        self.assertEqual(run.failures[-1]["category"], "missing")
        self.assertEqual(run.tool_outcomes[-1]["ok"], False)
        context = run.context_summary()
        self.assertIn("Objective: Open the demo", context)
        self.assertIn("open_app", context)
        self.assertIn("no installed app", context)

    def test_temporary_failure_allows_one_retry_then_blocks(self):
        service = AgentRunService()
        run = service.start("Retry")
        call = SimpleNamespace(id="retry-1", name="web_fetch",
                               args={"query": "same"})
        signature = call_signature(call)
        analysis = classify_failure({"error": "temporary network issue"})
        service.record_tool_result(
            run_id=run.run_id, tool_name="web_fetch", ok=False,
            summary="temporary network issue", failure=analysis,
            signature=signature)
        self.assertEqual(service.call_guard(run.run_id, call), "")
        service.record_tool_result(
            run_id=run.run_id, tool_name="web_fetch", ok=False,
            summary="temporary network issue", failure=analysis,
            signature=signature)
        self.assertIn("temporary", service.call_guard(run.run_id, call))

    def test_invalid_arguments_are_not_retried_unchanged(self):
        service = AgentRunService()
        run = service.start("Correct arguments")
        call = SimpleNamespace(id="invalid-1", name="open_file",
                               args={"path": "bad"})
        signature = call_signature(call)
        analysis = classify_failure({"error": "Bad arguments for open_file"})
        service.record_tool_result(
            run_id=run.run_id, tool_name="open_file", ok=False,
            summary="Bad arguments for open_file", failure=analysis,
            signature=signature)
        self.assertIn("invalid", service.call_guard(run.run_id, call))
        corrected = SimpleNamespace(id="invalid-2", name="open_file",
                                    args={"path": "C:/real/file.txt"})
        self.assertEqual(service.call_guard(run.run_id, corrected), "")

    def test_alternative_tool_is_not_blocked(self):
        service = AgentRunService()
        run = service.start("Try another tool")
        call = SimpleNamespace(id="alt-1", name="open_app",
                               args={"app_name": "Foo"})
        analysis = classify_failure({"error": "no installed app is named Foo"})
        service.record_tool_result(
            run_id=run.run_id, tool_name="open_app", ok=False,
            summary="no installed app is named Foo", failure=analysis,
            signature=call_signature(call))
        alternative = SimpleNamespace(id="alt-2", name="open_web",
                                      args={"url": "https://example.test"})
        self.assertEqual(
            service.call_guard(run.run_id, alternative), "")

    def test_pending_confirmation_blocks_unchanged_repeat(self):
        service = AgentRunService()
        run = service.start("Needs confirmation")
        call = SimpleNamespace(id="confirm-1", name="file_control",
                               args={"action": "delete", "path": "x"})
        service.record_pending(
            run_id=run.run_id, tool_name="file_control",
            signature=call_signature(call),
            category="confirmation_required",
            summary="confirmation required")
        self.assertIn("confirmation_required",
                      service.call_guard(run.run_id, call))
        confirmed = SimpleNamespace(
            id="confirm-2", name="file_control",
            args={"action": "delete", "path": "x", "confirm_token": "t"})
        self.assertEqual(service.call_guard(run.run_id, confirmed), "")

    def test_recovery_respects_the_tool_call_budget(self):
        service = AgentRunService()
        run = service.start(
            "Budgeted recovery",
            limits=AgentRunLimits(
                max_steps=10, max_tool_calls=1,
                max_consecutive_failures=3,
                max_duration_seconds=60))
        call = SimpleNamespace(id="budget-1", name="web_fetch", args={})
        service.record_tool_result(
            run_id=run.run_id, tool_name="web_fetch", ok=False,
            summary="temporary network issue",
            failure=classify_failure({"error": "temporary network issue"}),
            signature=call_signature(call))
        self.assertFalse(service.begin_execution(
            run.run_id, step="retry", tool_call_count=1))
        self.assertEqual(service.current().status, FAILED)
        self.assertIn("Tool call limit", service.current().last_error)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        agent_run.reset_service(None)
        self.addCleanup(agent_run.reset_service, None)
        self.service = agent_run.get_service()

    def test_trivial_action_needs_no_extra_verification(self):
        result = task_status("start", objective="Open Calculator")
        self.assertTrue(result["ok"])
        completed = task_status("complete", summary="Opened")
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["run"]["status"], COMPLETED)
        self.assertFalse(completed["run"]["verification"]["required"])

    def test_completion_rejected_when_evidence_is_missing(self):
        task_status("start", objective="Analyze the report")
        result = task_status("complete", summary="Finished")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "verification_required")
        self.assertTrue(result["missing_evidence"])
        self.assertEqual(result["run"]["status"], IN_PROGRESS)
        self.assertFalse(result["run"]["is_terminal"])

    def test_completion_succeeds_with_explicit_evidence(self):
        task_status("start", objective="Analyze the report")
        result = task_status(
            "complete", summary="Finished",
            evidence=["The report says revenue grew in every region."])
        self.assertTrue(result["ok"])
        self.assertEqual(result["run"]["status"], COMPLETED)
        self.assertTrue(result["run"]["verification"]["evidence"])

    def test_research_requires_research_evidence_not_a_single_search(self):
        task_status("start", objective="Research the market")
        self.service.add_evidence(
            summary="A search returned five links.", source="observation",
            tool="web_fetch", kind="observation")
        result = task_status("complete", summary="Done")
        self.assertFalse(result["ok"])
        self.assertIn("research", " ".join(result["missing_evidence"]).lower())
        self.service.add_evidence(
            summary="The research report cross-referenced four sources.",
            source="observation", tool="research", kind="observation")
        result = task_status("complete", summary="Done")
        self.assertTrue(result["ok"])
        self.assertEqual(result["run"]["status"], COMPLETED)

    def test_verification_attempts_are_bounded(self):
        task_status("start", objective="Analyze the report")
        first = task_status("complete")
        second = task_status("complete")
        third = task_status("complete")
        self.assertFalse(first["ok"])
        self.assertFalse(second["ok"])
        self.assertFalse(third["ok"])
        self.assertTrue(third["verification"]["exhausted"])
        self.assertEqual(third["run"]["status"], "blocked")
        fourth = task_status("complete")
        self.assertFalse(fourth["ok"])
        self.assertTrue(fourth["verification"]["exhausted"])
        self.assertEqual(
            fourth["run"]["verification"]["attempts"], 3)

    def test_blocked_task_can_report_its_state(self):
        task_status("start", objective="Analyze the report")
        task_status(
            "update", status="blocked",
            summary="Waiting for the source document")
        result = task_status("status")
        self.assertTrue(result["ok"])
        self.assertEqual(result["run"]["status"], "blocked")
        self.assertIn("Objective:", result["context"])


class ContextPreservationTests(unittest.TestCase):
    def test_steps_outcomes_failures_and_evidence_are_compact(self):
        run = AgentRun(objective="Find and verify a file")
        run.record_step("Search Downloads")
        analysis = classify_failure({"error": "not found"})
        run.record_tool_call(
            tool_name="search_files", ok=False, summary="not found",
            failure=analysis, signature="sig")
        run.record_step("Try another location")
        run.add_evidence(
            "Found the file in Documents.", source="observation",
            tool="search_files", kind="observation")
        snapshot = run.to_dict()
        self.assertEqual(len(snapshot["steps"]), 2)
        self.assertEqual(len(snapshot["tool_outcomes"]), 1)
        self.assertEqual(len(snapshot["failures"]), 1)
        self.assertEqual(len(snapshot["verification"]["evidence"]), 1)
        context = run.context_summary(500)
        self.assertIn("Completed steps (recent)", context)
        self.assertIn("Important errors", context)
        self.assertIn("Verification evidence", context)
        self.assertLessEqual(len(context), 500)


class ContextContinuityTests(unittest.TestCase):
    def setUp(self):
        agent_run.reset_service(None)
        self.addCleanup(agent_run.reset_service, None)
        self.service = agent_run.get_service()

    def test_context_contains_objective_progress_failures_and_verification(self):
        run = AgentRun(objective="Analyze the report",
                       remaining_work="Check the conclusion")
        run.record_step("Open the report")
        run.record_tool_call(
            tool_name="open_file", ok=True, summary="opened report",
            signature="open")
        analysis = classify_failure({"error": "not found: appendix"})
        run.record_tool_call(
            tool_name="search_files", ok=False, summary="not found: appendix",
            failure=analysis, signature="search")
        run.add_evidence(
            "The report says revenue grew.", source="observation",
            tool="search_files", kind="observation")
        text = run.context_for_model(700)
        self.assertIn("ACTIVE TASK CONTEXT", text)
        self.assertIn("Analyze the report", text)
        self.assertIn("Completed steps (recent)", text)
        self.assertIn("Open the report", text)
        self.assertIn("Relevant tool outcomes", text)
        self.assertIn("not found: appendix", text)
        self.assertIn("Verification evidence", text)
        self.assertIn("Remaining: Check the conclusion", text)
        self.assertLessEqual(len(text), 700)

    def test_context_is_bounded_even_with_many_entries(self):
        run = AgentRun(
            objective="Long task",
            limits=AgentRunLimits(
                max_steps=50, max_tool_calls=50,
                max_consecutive_failures=3,
                max_duration_seconds=60))
        for index in range(30):
            run.record_step(f"Step {index} " + ("x" * 100))
            run.record_tool_call(
                tool_name="search_files", ok=True,
                summary=f"outcome {index} " + ("y" * 100))
        text = run.context_for_model(420)
        self.assertLessEqual(len(text), 420)
        self.assertNotIn("Step 0", text)
        self.assertIn("Objective: Long task", text)

    def test_remaining_work_and_status_are_updated(self):
        task_status(
            "start", objective="Analyze report",
            remaining="Read the appendix")
        result = task_status("status")
        self.assertIn("Remaining: Read the appendix", result["context"])
        task_status(
            "update", status="blocked", remaining="Waiting for access")
        result = task_status("status")
        self.assertIn("Status: blocked", result["context"])
        self.assertIn("Remaining: Waiting for access", result["context"])

    def test_reconnect_system_prompt_contains_active_task_context(self):
        from main import JarvisLive

        run = self.service.start("Analyze the report")
        run.record_step("Read the report")

        class UI:
            def set_assistant_name(self, _name):
                pass

        fake = SimpleNamespace(
            ui=UI(), _resume_handle=None, _enhanced_live=False,
            _tuned_live=False, _asst_name="Aze",
            _active_task_context=lambda: run.context_for_model(),
            _active_task_run=lambda: run,
        )
        config = JarvisLive._build_config(fake)
        self.assertIn("ACTIVE TASK CONTEXT", config.system_instruction)
        self.assertIn("Analyze the report", config.system_instruction)

    def test_injection_is_deduplicated_and_refreshed_after_progress(self):
        from main import JarvisLive

        run = self.service.start("Analyze the report")

        class Session:
            def __init__(self):
                self.calls = []

            async def send_client_content(self, **kwargs):
                self.calls.append(kwargs)

        live = JarvisLive.__new__(JarvisLive)
        live._agent_runs = self.service
        live.session = Session()
        live._last_agent_context_key = None

        asyncio.run(live._inject_task_context("test", force=True))
        self.assertEqual(len(live.session.calls), 1)
        text = live.session.calls[0]["turns"]["parts"][0]["text"]
        self.assertIn("Analyze the report", text)
        asyncio.run(live._inject_task_context("duplicate"))
        self.assertEqual(len(live.session.calls), 1)

        run.record_step("Read the report")
        asyncio.run(live._inject_task_context("progress"))
        self.assertEqual(len(live.session.calls), 2)

    def test_context_refreshes_after_failure_and_forced_compression(self):
        from main import JarvisLive

        run = self.service.start("Analyze the report")

        class Session:
            def __init__(self):
                self.calls = []

            async def send_client_content(self, **kwargs):
                self.calls.append(kwargs)

        live = JarvisLive.__new__(JarvisLive)
        live._agent_runs = self.service
        live.session = Session()
        live._last_agent_context_key = None

        asyncio.run(live._inject_task_context("start", force=True))
        self.assertEqual(len(live.session.calls), 1)

        self.service.record_tool_result(
            run_id=run.run_id, tool_name="search_files", ok=False,
            summary="not found: appendix",
            failure=classify_failure({"error": "not found: appendix"}),
            signature="search")
        asyncio.run(live._inject_task_context("failure"))
        self.assertEqual(len(live.session.calls), 2)
        self.assertIn(
            "not found: appendix",
            live.session.calls[-1]["turns"]["parts"][0]["text"])

        # An explicit force represents a compression/reconnect refresh even
        # when the run has not changed since the last injection.
        asyncio.run(live._inject_task_context("compression", force=True))
        self.assertEqual(len(live.session.calls), 3)

    def test_typed_turn_carries_task_context_without_cancelling_run(self):
        from main import JarvisLive

        run = self.service.start("Analyze the report")

        class Session:
            def __init__(self):
                self.calls = []
                self.sent = threading.Event()

            async def send_client_content(self, **kwargs):
                self.calls.append(kwargs)
                self.sent.set()

        loop = asyncio.new_event_loop()
        worker = threading.Thread(target=loop.run_forever, daemon=True)
        worker.start()
        live = JarvisLive.__new__(JarvisLive)
        live._agent_runs = self.service
        live._loop = loop
        live.session = Session()
        live._turn_done_event = None
        live._interrupted = False
        live._interrupted_source = ""
        live._last_out_logged = ""
        live._agent_terminal_until_user = False
        live._memory_context_for = lambda _text: ""
        try:
            self.assertTrue(live.send_text("Also check the appendix."))
            self.assertTrue(live.session.sent.wait(2.0))
        finally:
            loop.call_soon_threadsafe(loop.stop)
            worker.join(2.0)
            loop.close()

        parts = live.session.calls[0]["turns"]["parts"]
        text = "\n".join(part.get("text", "") for part in parts)
        self.assertIn("Analyze the report", text)
        self.assertIn("Also check the appendix.", text)
        self.assertEqual(self.service.current().run_id, run.run_id)
        self.assertEqual(self.service.current().status, IN_PROGRESS)

    def test_completed_operation_is_not_repeated(self):
        run = self.service.start("Open the app")
        call = SimpleNamespace(id="done-1", name="open_app",
                               args={"app_name": "Calculator"})
        self.service.record_tool_result(
            run_id=run.run_id, tool_name="open_app", ok=True,
            summary="opened Calculator",
            signature=call_signature(call))
        self.assertIn("already completed",
                      self.service.call_guard(run.run_id, call))
        changed = SimpleNamespace(id="done-2", name="open_app",
                                  args={"app_name": "Notepad"})
        self.assertEqual(self.service.call_guard(run.run_id, changed), "")

    def test_completed_phone_and_scheduled_actions_are_not_repeated(self):
        run = self.service.start("Call and remind")
        cases = [
            ("call", {"number_or_sip_address": "sip:alice@example.test"},
             {"number_or_sip_address": "sip:alice@example.test"}),
            ("reminders", {"action": "create_reminder", "title": "Stand up"},
             {"action": "create_reminder", "title": "Stand up"}),
            ("screen_understanding",
             {"operation": "start_continuous_observation"},
             {"operation": "start_continuous_observation"}),
        ]
        for name, args, repeat in cases:
            with self.subTest(tool=name):
                call = SimpleNamespace(id=f"{name}-1", name=name, args=args)
                self.service.record_tool_result(
                    run_id=run.run_id, tool_name=name, ok=True,
                    summary="done", signature=call_signature(call),
                    side_effect=agent_run.is_side_effect_tool(name, args))
                repeated = SimpleNamespace(
                    id=f"{name}-2", name=name, args=repeat)
                self.assertIn("already completed",
                              self.service.call_guard(run.run_id, repeated))

    def test_read_only_reminder_and_observation_queries_can_repeat(self):
        run = self.service.start("Inspect state")
        self.assertTrue(agent_run.is_observation_tool(
            "file_control", {"action": "find", "query": "report"}))
        self.assertTrue(agent_run.is_observation_tool(
            "reminders", {"action": "list_reminders"}))
        cases = [
            ("reminders", {"action": "list_reminders"}),
            ("reminders", {"action": "get_reminder", "query": "stand"}),
            ("reminders", {"action": "list_notifications"}),
            ("screen_understanding", {"operation": "get_observation_status"}),
            ("youtube_player", {"operation": "get_status"}),
        ]
        for name, args in cases:
            with self.subTest(tool=name, args=args):
                self.assertFalse(
                    agent_run.is_side_effect_tool(name, args))
                call = SimpleNamespace(id=f"{name}-x", name=name, args=args)
                self.service.record_tool_result(
                    run_id=run.run_id, tool_name=name, ok=True,
                    summary="read", signature=call_signature(call),
                    side_effect=False)
                self.assertEqual(
                    self.service.call_guard(run.run_id, call), "")

    def test_long_arguments_do_not_share_a_signature(self):
        prefix = "x" * 1500
        first = SimpleNamespace(
            name="file_control",
            args={"action": "write", "source_path": "a.txt",
                  "content": prefix + "A"})
        second = SimpleNamespace(
            name="file_control",
            args={"action": "write", "source_path": "a.txt",
                  "content": prefix + "B"})
        self.assertNotEqual(call_signature(first), call_signature(second))

    def test_terminal_run_has_no_active_context(self):
        from main import JarvisLive

        run = self.service.start("Analyze the report")
        self.service.complete(run_id=run.run_id, summary="done")
        live = JarvisLive.__new__(JarvisLive)
        live._agent_runs = self.service
        self.assertEqual(live._active_task_context(), "")


class ProgressEventTests(unittest.TestCase):
    def setUp(self):
        agent_run.reset_service(None)
        self.addCleanup(agent_run.reset_service, None)
        self.service = agent_run.get_service()
        self.events = []
        self.service.set_listener(self.events.append)

    def test_lifecycle_events_carry_the_run_id(self):
        run = self.service.start("Plan")
        self.service.record_step(run.run_id, "step one")
        self.service.record_tool_result(
            run_id=run.run_id, tool_name="web_fetch", ok=True,
            summary="ok")
        self.service.complete(run_id=run.run_id, summary="done")

        names = [event["event"] for event in self.events]
        self.assertEqual(
            names, ["started", "step", "tool", "completed"])
        for event in self.events:
            self.assertEqual(event["run"]["run_id"], run.run_id)
            self.assertIn("status", event["run"])
            self.assertTrue(event["ts"])

    def test_successful_retry_resumes_a_run_that_needed_user(self):
        run = self.service.start("Approve then continue")
        self.service.mark_needs_user(
            run_id=run.run_id, step="Waiting for approval")
        self.assertEqual(self.service.current().status, NEEDS_USER)
        self.service.record_tool_result(
            run_id=run.run_id, tool_name="file_control", ok=True,
            summary="approved operation completed")
        self.assertEqual(self.service.current().status, IN_PROGRESS)

    def test_limit_and_cancel_events_are_emitted(self):
        run = self.service.start("Limits")
        self.service.cancel(run_id=run.run_id, reason="stopped")
        self.assertEqual(self.events[-1]["event"], "cancelled")
        self.assertEqual(self.events[-1]["run"]["status"], CANCELLED)

    def test_limit_event_is_emitted(self):
        run = self.service.start(
            "Step limit",
            limits=AgentRunLimits(
                max_steps=1, max_tool_calls=10,
                max_consecutive_failures=3,
                max_duration_seconds=60))
        self.service.record_step(run.run_id, "one")
        with self.assertRaises(AgentRunLimitError):
            self.service.record_step(run.run_id, "two")
        self.assertEqual(self.events[-1]["event"], "limit")
        self.assertEqual(self.events[-1]["run"]["status"], FAILED)
        self.assertEqual(self.events[-1]["limit"], "max_steps")


class WebSocketEventTests(unittest.TestCase):
    def test_web_ui_publishes_agent_updated_with_run_id(self):
        from web.ui import WebUI

        hub = _Hub()
        ui = WebUI(hub)
        run = AgentRun(objective="Plan", conversation_id="conv")
        ui.agent_event({"event": "started", "run": run.to_dict()})

        self.assertEqual(hub.events[-1]["type"], "agent.updated")
        self.assertEqual(hub.events[-1]["run_id"], run.run_id)
        self.assertEqual(hub.events[-1]["event"], "started")
        self.assertEqual(
            ui.snapshot()["agent_run"]["run_id"], run.run_id)


class PromptAndDeclarationIntegrationTests(unittest.TestCase):
    def test_system_prompt_instructs_task_status_use(self):
        from pathlib import Path

        prompt = (Path(__file__).resolve().parents[1] / "core"
                  / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("task_status", prompt)
        self.assertIn("operation: \"start\"", prompt)
        self.assertIn("multi-step", prompt.casefold())
        self.assertIn("complete", prompt.casefold())

    def test_task_status_survives_enabled_only_declaration_filter(self):
        from unittest import mock

        from actions import tool_calling

        with mock.patch.object(tool_calling, "_enabled_record_ids",
                               return_value=set()):
            declarations = {
                item["name"]: item
                for item in tool_calling.get_tool_declarations(
                    enabled_only=True)
            }
        self.assertIn("task_status", declarations)
        properties = declarations["task_status"]["parameters"]["properties"]
        self.assertIn("objective", properties)
        self.assertIn("evidence", properties)


class NormalConversationTests(unittest.TestCase):
    def setUp(self):
        agent_run.reset_service(None)
        self.addCleanup(agent_run.reset_service, None)
        self.service = agent_run.get_service()

    def test_no_run_is_created_by_reading_status(self):
        self.assertIsNone(self.service.current())
        result = task_status("status")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["run"])
        self.assertIsNone(self.service.current())

    def test_no_run_is_created_by_normal_service_access(self):
        self.service.set_conversation_id("conv")
        self.assertFalse(self.service.has_active_run())
        self.assertIsNone(self.service.current_snapshot())


if __name__ == "__main__":
    unittest.main()

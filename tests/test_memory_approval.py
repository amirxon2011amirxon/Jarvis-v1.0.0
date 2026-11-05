"""Broad memory deletion requires a trusted UI approval."""

from __future__ import annotations

import unittest
from unittest import mock

from core import approvals
from core import memory as memory_module


class FakeResult:
    def __init__(self):
        self.calls = []

    def to_dict(self):
        return {"ok": True, "status": "forgotten", "calls": list(self.calls)}


class FakeMemoryService:
    def __init__(self):
        self.calls = []

    def delete_memory(self, memory_id):
        self.calls.append(("id", memory_id))
        return FakeResult()

    def forget_by_query(self, query, *, confirm=False):
        self.calls.append(("query", query, bool(confirm)))
        return FakeResult()

    def forget_project(self, project_id, *, confirm=False):
        self.calls.append(("project", project_id, bool(confirm)))
        return FakeResult()


class MemoryApprovalTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeMemoryService()
        self.patch = mock.patch.object(
            memory_module, "_tool_service", return_value=self.service)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.approvals = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=60, start_sweeper=False))
        self.approvals.set_session("memory-test")
        self.addCleanup(approvals.shutdown_service)

    def test_broad_query_requires_approval_and_ignores_confirm_flag(self):
        blocked = memory_module.tool_forget_memory(query="Rome", confirm=True)
        self.assertTrue(blocked["approval_required"])
        self.assertEqual(self.service.calls, [])

        request = self.approvals.active_requests()[0]
        self.assertIn("Rome", request["target"])
        self.approvals.approve(request["approval_id"])

        done = memory_module.tool_forget_memory(query="Rome", confirm=False)
        self.assertTrue(done["ok"])
        self.assertEqual(self.service.calls, [("query", "Rome", True)])

    def test_modified_query_requires_a_new_approval(self):
        memory_module.tool_forget_memory(query="Rome")
        request = self.approvals.active_requests()[0]
        self.approvals.approve(request["approval_id"])

        other = memory_module.tool_forget_memory(query="Paris")
        self.assertTrue(other["approval_required"])
        self.assertEqual(self.service.calls, [])

        original = memory_module.tool_forget_memory(query="Rome")
        self.assertTrue(original["ok"])
        self.assertEqual(self.service.calls, [("query", "Rome", True)])

    def test_single_exact_memory_id_is_not_broad(self):
        result = memory_module.tool_forget_memory(memory_id="mem-1")
        self.assertTrue(result["ok"])
        self.assertEqual(self.service.calls, [("id", "mem-1")])


if __name__ == "__main__":
    unittest.main()

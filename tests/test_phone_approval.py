"""Phone-call approval, allowlist and emergency-number tests."""

from __future__ import annotations

import unittest
from unittest import mock

from actions import phone_call_tool
from core import approvals


class FakeService:
    def __init__(self):
        self.calls = []

    def settings(self):
        return {"server": "sip.example.test", "contacts": {}}

    def call(self, target):
        self.calls.append(target)
        return {"ok": True, "result": f"Calling {target}", "call": {"id": "c1"}}


class PhoneApprovalTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.patch = mock.patch.object(
            phone_call_tool, "_service", return_value=self.service)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.approvals = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=60, start_sweeper=False))
        self.approvals.set_session("phone-test")
        self.addCleanup(approvals.shutdown_service)

    def test_emergency_numbers_are_blocked_without_calling(self):
        for number in ("911", "112", "999", "000"):
            with self.subTest(number=number):
                result = phone_call_tool.call(number)
                self.assertEqual(result["error_code"],
                                 "emergency_call_blocked")
                self.assertFalse(result["ok"])
        self.assertEqual(self.service.calls, [])

    def test_new_call_requires_ui_approval_then_is_single_use(self):
        with mock.patch.object(
                phone_call_tool.telephony, "resolve_target",
                return_value="sip:friend@example.test"), \
             mock.patch.object(
                phone_call_tool, "get_call_allowlist", return_value=[]):
            blocked = phone_call_tool.call("friend")
            self.assertTrue(blocked["approval_required"])
            self.assertEqual(self.service.calls, [])

            request = self.approvals.active_requests()[0]
            self.approvals.approve(request["approval_id"])

            called = phone_call_tool.call("friend")
            self.assertTrue(called["ok"])
            self.assertEqual(self.service.calls, ["sip:friend@example.test"])

            again = phone_call_tool.call("friend")
            self.assertTrue(again["approval_required"])
            self.assertEqual(len(self.service.calls), 1)

    def test_allowlisted_target_skips_the_ui_gate(self):
        target = "sip:trusted@example.test"
        with mock.patch.object(
                phone_call_tool, "get_call_allowlist",
                return_value=[target]):
            result = phone_call_tool.call(target)
        self.assertTrue(result["ok"])
        self.assertEqual(self.service.calls, [target])
        self.assertEqual(self.approvals.active_requests(), [])

    def test_approval_unavailable_fails_closed(self):
        with mock.patch.object(
                phone_call_tool.telephony, "resolve_target",
                return_value="sip:friend@example.test"), \
             mock.patch.object(
                phone_call_tool._approvals, "get_service",
                side_effect=RuntimeError("approval store unavailable")):
            result = phone_call_tool.call("friend")
        self.assertFalse(result.get("ok", False))
        self.assertTrue(result.get("approval_unavailable"))
        self.assertEqual(self.service.calls, [])


if __name__ == "__main__":
    unittest.main()

"""Server-side approval lifecycle tests."""

from __future__ import annotations

import time
import unittest

from core import approvals


class ApprovalServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = approvals.ApprovalService(
            ttl_seconds=60, start_sweeper=False)
        self.service.set_session("session-one")
        self.events = []
        self.service.set_listener(self.events.append)
        self.addCleanup(self.service.close)

    def request(self, **overrides):
        values = {
            "tool": "file_control",
            "operation": "delete",
            "target": r"C:\Temp\report.txt",
            "parameters": {"paths": [r"c:\temp\report.txt"]},
            "summary": r"Delete C:\Temp\report.txt",
        }
        values.update(overrides)
        return self.service.authorize(**values)

    def test_request_contains_every_required_public_field(self):
        allowed, request, reason = self.request()
        self.assertFalse(allowed)
        self.assertEqual(reason, "created")
        for key in ("approval_id", "tool", "operation", "target", "summary",
                    "parameters", "created_at", "expires_at", "status"):
            self.assertIn(key, request)
        self.assertEqual(request["status"], approvals.PENDING)
        self.assertNotIn("fingerprint", request)
        self.assertNotIn("session_id", request)

    def test_approval_is_single_use(self):
        _, request, _ = self.request()
        approval_id = request["approval_id"]
        self.service.approve(approval_id)

        allowed, consumed, reason = self.request()
        self.assertTrue(allowed)
        self.assertEqual(reason, "consumed")
        self.assertEqual(consumed["status"], approvals.CONSUMED)

        allowed, again, reason = self.request()
        self.assertFalse(allowed)
        self.assertEqual(again["status"], approvals.PENDING)
        self.assertNotEqual(again["approval_id"], approval_id)
        self.assertEqual(reason, "created")

    def test_modified_target_does_not_use_existing_approval(self):
        _, request, _ = self.request()
        self.service.approve(request["approval_id"])

        allowed, other, reason = self.request(
            target=r"C:\Temp\other.txt",
            parameters={"paths": [r"c:\temp\other.txt"]},
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "created")
        self.assertEqual(other["status"], approvals.PENDING)
        self.assertNotEqual(other["approval_id"], request["approval_id"])

        # The original approval is still only usable for the original target.
        allowed, consumed, _ = self.request()
        self.assertTrue(allowed)
        self.assertEqual(consumed["status"], approvals.CONSUMED)

    def test_rejected_approval_never_authorizes(self):
        _, request, _ = self.request()
        self.service.reject(request["approval_id"])
        allowed, replacement, _ = self.request()
        self.assertFalse(allowed)
        self.assertEqual(replacement["status"], approvals.PENDING)

    def test_expired_approval_is_rejected_and_replaced(self):
        short = approvals.ApprovalService(
            ttl_seconds=0.02, start_sweeper=False)
        short.set_session("session-one")
        self.addCleanup(short.close)
        _, request, _ = short.authorize(
            tool="file_control", operation="delete", target="x",
            parameters={"paths": ["x"]}, summary="Delete x")
        time.sleep(0.04)
        allowed, replacement, reason = short.authorize(
            tool="file_control", operation="delete", target="x",
            parameters={"paths": ["x"]}, summary="Delete x")
        self.assertFalse(allowed)
        self.assertEqual(reason, "created")
        self.assertEqual(replacement["status"], approvals.PENDING)
        self.assertNotEqual(replacement["approval_id"], request["approval_id"])
        statuses = [item["status"] for item in short.all_requests()]
        self.assertIn(approvals.EXPIRED, statuses)

    def test_session_change_revokes_active_requests(self):
        _, request, _ = self.request()
        self.service.set_session("session-two")
        active = self.service.active_requests()
        self.assertEqual(active, [])
        row = next(item for item in self.service.all_requests()
                   if item["approval_id"] == request["approval_id"])
        self.assertEqual(row["status"], approvals.REVOKED)
        self.assertTrue(any(
            event["event"] == "revoked" for event in self.events))

    def test_approval_from_another_session_is_refused(self):
        _, request, _ = self.request()
        self.service.set_session("session-two")
        with self.assertRaises(approvals.ApprovalError):
            self.service.approve(request["approval_id"])

    def test_approve_and_reject_are_idempotent_until_consumed(self):
        _, request, _ = self.request()
        first = self.service.approve(request["approval_id"])
        second = self.service.approve(request["approval_id"])
        self.assertEqual(first["status"], approvals.APPROVED)
        self.assertEqual(second["status"], approvals.APPROVED)
        rejected = self.service.reject(request["approval_id"])
        self.assertEqual(rejected["status"], approvals.REJECTED)
        with self.assertRaises(approvals.ApprovalError):
            self.service.approve(request["approval_id"])

    def test_public_request_is_bounded_to_active_decisions(self):
        _, request, _ = self.request()
        self.assertEqual(len(self.service.active_requests()), 1)
        self.service.reject(request["approval_id"])
        self.assertEqual(self.service.active_requests(), [])


if __name__ == "__main__":
    unittest.main()

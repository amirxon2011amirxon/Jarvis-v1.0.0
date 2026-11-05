"""Phone-call lifecycle, duplicate protection and cleanup regressions.

The SIP socket, RTP media and real calls are never used. Fakes model only the
state transitions and resources the tool layer depends on.
"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from actions import phone_call_tool
from core import approvals, telephony
from core.telephony import Call, SipEngine, TelephonyError, TelephonyService


class FakeRtp:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


class FakeEngine:
    def __init__(self, calls=None, *, delay=0.0, failure=None):
        self.calls_list = list(calls or [])
        self.delay = delay
        self.failure = failure
        self.made = []

    def wait_until_registered(self):
        return None

    def registration(self):
        return {"state": "registered", "registered": True}

    def calls(self):
        return [call.snapshot() for call in self.calls_list]

    def active_call(self):
        for call in self.calls_list:
            if call.state == "active":
                return call
        return None

    def make_call(self, target):
        if self.delay:
            time.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        call = Call(
            id=f"call-{len(self.made) + 1}",
            direction="outgoing",
            remote_uri=target,
            remote_display="",
            local_uri="sip:jarvis@example.test",
            call_id=f"sip-{len(self.made) + 1}",
            state="dialing",
        )
        self.made.append(call)
        self.calls_list.append(call)
        return call


def outgoing_call(state="dialing"):
    return Call(
        id=f"call-{state}",
        direction="outgoing",
        remote_uri="sip:alice@example.test",
        remote_display="Alice",
        local_uri="sip:jarvis@example.test",
        call_id=f"sip-{state}",
        state=state,
    )


class ServiceCallTests(unittest.TestCase):
    def make_service(self, engine):
        service = TelephonyService()
        service.settings = lambda: {
            "username": "jarvis", "password": "secret",
            "server": "example.test", "identity": "sip:jarvis@example.test",
            "transport": "TLS", "port": 5061,
        }
        service._ensure_engine = lambda start=True: engine
        return service

    def test_successful_outgoing_call_is_not_reported_active(self):
        engine = FakeEngine()
        service = self.make_service(engine)
        with mock.patch.object(
                telephony, "resolve_target",
                side_effect=lambda value, settings=None: value):
            result = service.call("sip:alice@example.test")
        self.assertTrue(result["ok"])
        self.assertFalse(result["connected"])
        self.assertTrue(result["pending_connection"])
        self.assertEqual(result["state"], "starting")
        self.assertEqual(result["call"]["state"], "dialing")
        self.assertEqual(result["call"]["phase"], "starting")
        self.assertEqual(len(engine.made), 1)

    def test_failed_outgoing_call_is_specific(self):
        engine = FakeEngine(failure=TelephonyError("SIP server rejected call"))
        service = self.make_service(engine)
        with mock.patch.object(
                telephony, "resolve_target",
                side_effect=lambda value, settings=None: value), \
             self.assertRaises(TelephonyError) as raised:
            service.call("sip:alice@example.test")
        self.assertIn("rejected", str(raised.exception))
        self.assertEqual(engine.made, [])

    def test_second_call_is_refused_in_every_active_phase(self):
        for state in ("incoming", "dialing", "ringing", "connecting",
                      "active", "ending"):
            with self.subTest(state=state):
                engine = FakeEngine([outgoing_call(state)])
                service = self.make_service(engine)
                with mock.patch.object(
                        telephony, "resolve_target",
                        side_effect=lambda value, settings=None: value), \
                     self.assertRaises(TelephonyError):
                    service.call("sip:bob@example.test")
                self.assertEqual(engine.made, [])

    def test_answer_reports_active_only_after_sip_confirmation(self):
        call = Call(
            id="call-in", direction="incoming",
            remote_uri="sip:alice@example.test", remote_display="Alice",
            local_uri="sip:jarvis@example.test", call_id="sip-in",
            state="incoming")
        engine = FakeEngine([call])
        engine.get_call = lambda call_id: call if call_id == call.id else None
        engine.answer = lambda call_id: setattr(call, "state", "active")
        service = self.make_service(engine)
        result = service.answer(call.id)
        self.assertTrue(result["ok"])
        self.assertTrue(result["connected"])
        self.assertEqual(result["state"], "active")

    def test_concurrent_requests_start_only_one_call(self):
        engine = FakeEngine(delay=0.15)
        service = self.make_service(engine)
        results = []

        def dial():
            try:
                with mock.patch.object(
                        telephony, "resolve_target",
                        side_effect=lambda value, settings=None: value):
                    results.append(service.call("sip:alice@example.test"))
            except TelephonyError as exc:
                results.append(exc)

        threads = [threading.Thread(target=dial) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3.0)
        self.assertEqual(len(engine.made), 1)
        self.assertEqual(sum(1 for item in results if isinstance(item, dict)), 1)
        self.assertEqual(
            sum(1 for item in results if isinstance(item, TelephonyError)), 1)


class CallStateAndCleanupTests(unittest.TestCase):
    def _engine(self):
        engine = SipEngine.__new__(SipEngine)
        engine._lock = threading.RLock()
        engine._calls = {}
        engine._by_sip_id = {}
        engine._auto_answer_timers = {}
        engine._history_lock = threading.Lock()
        engine.on_call = None
        engine.log = lambda _message: None
        engine._write_history = lambda _call: None
        return engine

    def test_overall_state_is_idle_when_only_terminal_calls_remain(self):
        self.assertEqual(
            telephony._overall_call_state(
                [{"state": "ended"}, {"state": "failed"}]),
            "idle")
        self.assertEqual(
            telephony._overall_call_state(
                [{"state": "ended"}, {"state": "ringing"}]),
            "ringing")

    def test_snapshot_exposes_normalised_phases(self):
        expected = {
            "incoming": "ringing", "dialing": "starting", "ringing": "ringing",
            "connecting": "connecting", "active": "active",
            "ending": "ending", "ended": "ended", "failed": "failed",
        }
        for state, phase in expected.items():
            call = outgoing_call(state)
            snapshot = call.snapshot()
            self.assertEqual(snapshot["phase"], phase)
            self.assertEqual(snapshot["terminal"], state in ("ended", "failed"))

    def test_failure_terminates_and_releases_rtp(self):
        engine = self._engine()
        call = outgoing_call("active")
        call.rtp = FakeRtp()
        engine._calls[call.id] = call
        engine._by_sip_id[call.call_id] = call
        engine._end_call(call, "remote rejected", terminal_state="failed")
        self.assertEqual(call.state, "failed")
        self.assertTrue(call.snapshot()["terminal"])
        self.assertIsNone(call.rtp)
        self.assertIsNone(call.media)
        self.assertIsNone(call.sip)

    def test_cleanup_is_idempotent(self):
        engine = self._engine()
        call = outgoing_call("active")
        rtp = FakeRtp()
        call.rtp = rtp
        engine._calls[call.id] = call
        engine._by_sip_id[call.call_id] = call
        engine._end_call(call, "local hangup")
        engine._end_call(call, "local hangup again")
        self.assertEqual(rtp.stops, 1)
        self.assertEqual(call.state, "ended")

    def test_network_disconnect_marks_failed_and_clears_calls(self):
        engine = self._engine()
        call = outgoing_call("active")
        call.rtp = FakeRtp()
        engine._calls[call.id] = call
        engine._by_sip_id[call.call_id] = call
        engine._end_all_calls("network disconnected", notify=False,
                              terminal_state="failed")
        self.assertEqual(call.state, "failed")
        self.assertEqual(engine._calls, {})
        self.assertEqual(engine._by_sip_id, {})

    def test_hangup_emits_ending_then_ended(self):
        engine = self._engine()
        call = outgoing_call("active")
        engine._calls[call.id] = call
        engine._by_sip_id[call.call_id] = call
        states = []
        engine.on_call = lambda _call_id, item: states.append(item.state)
        engine._bye = lambda _call: None
        engine.hangup(call.id)
        self.assertEqual(states, ["ending", "ended"])
        self.assertEqual(call.state, "ended")

    def test_repeated_call_after_termination_is_allowed(self):
        engine = FakeEngine([outgoing_call("ended")])
        service = ServiceCallTests().make_service(engine)
        with mock.patch.object(
                telephony, "resolve_target",
                side_effect=lambda value, settings=None: value):
            result = service.call("sip:alice@example.test")
        self.assertTrue(result["ok"])
        self.assertEqual(len(engine.made), 1)


class ToolResultTests(unittest.TestCase):
    def test_tool_reports_pending_connection_not_connected(self):
        fake = SimpleNamespace(
            settings=lambda: {"username": "u", "password": "p"},
            call=lambda target: {
                "ok": True, "state": "starting", "connected": False,
                "pending_connection": True, "call": {"state": "dialing"}},
        )
        with mock.patch.object(phone_call_tool, "_service", return_value=fake), \
             mock.patch.object(phone_call_tool, "get_call_allowlist",
                               return_value=["sip:alice@example.test"]), \
             mock.patch.object(
                 phone_call_tool.telephony, "resolve_target",
                 side_effect=lambda value, settings=None: value):
            result = phone_call_tool.call("sip:alice@example.test")
        self.assertTrue(result["ok"])
        self.assertFalse(result["connected"])
        self.assertTrue(result["pending_connection"])

    def test_tool_reports_specific_failure(self):
        fake = SimpleNamespace(
            settings=lambda: {"username": "u", "password": "p"},
            call=lambda target: (_ for _ in ()).throw(
                TelephonyError("registration lost")))
        with mock.patch.object(phone_call_tool, "_service", return_value=fake), \
             mock.patch.object(phone_call_tool, "get_call_allowlist",
                               return_value=["sip:alice@example.test"]), \
             mock.patch.object(
                 phone_call_tool.telephony, "resolve_target",
                 side_effect=lambda value, settings=None: value):
            result = phone_call_tool.call("sip:alice@example.test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("registration lost", result["error"])


class EngineTransitionTests(unittest.TestCase):
    """SIP-driven transitions with a transport-free engine."""

    def _engine(self):
        engine = SipEngine({"username": "jarvis", "password": "secret",
                            "server": "example.test", "srtp": False,
                            "ring_timeout": 60})
        engine.log = lambda _message: None
        engine._write_history = lambda _call: None
        engine._reply = lambda *_args, **_kwargs: None
        return engine

    def _register(self, engine, call):
        engine._calls[call.id] = call
        engine._by_sip_id[call.call_id] = call
        return call

    def test_hangup_while_ringing_cancels_and_ends(self):
        engine = self._engine()
        call = self._register(engine, outgoing_call("ringing"))
        sent = []
        engine._cancel = lambda item: sent.append(item.id)
        engine.hangup(call.id)
        self.assertEqual(sent, [call.id])
        self.assertEqual(call.state, "ended")

    def test_remote_bye_ends_the_call_and_releases_audio(self):
        engine = self._engine()
        call = self._register(engine, outgoing_call("active"))
        call.rtp = FakeRtp()
        message = telephony.SipMessage(
            start="BYE sip:jarvis@example.test SIP/2.0",
            headers=[("Call-ID", call.call_id)])
        engine._dispatch(message)
        self.assertEqual(call.state, "ended")
        self.assertIn("remote hung up", call.reason)
        self.assertIsNone(call.rtp)
        self.assertNotIn(call.call_id, engine._by_sip_id)

    def test_rejected_outgoing_call_is_failed_with_the_sip_reason(self):
        engine = self._engine()
        call = self._register(engine, outgoing_call("ringing"))
        message = telephony.SipMessage(
            start="SIP/2.0 486 Busy Here",
            headers=[("Call-ID", call.call_id), ("CSeq", "1 INVITE")])
        engine._dispatch(message)
        self.assertEqual(call.state, "failed")
        self.assertTrue(call.snapshot()["terminal"])
        self.assertIn("486", call.reason)
        self.assertNotIn(call.call_id, engine._by_sip_id)

    def test_unanswered_call_times_out_and_is_cancelled(self):
        engine = self._engine()
        call = outgoing_call("dialing")
        call.created_at = time.time() - 120.0
        self._register(engine, call)
        cancelled = []
        engine._cancel = lambda item: cancelled.append(item.id)
        engine._tick()
        self.assertEqual(call.state, "failed")
        self.assertIn("no answer", call.reason)
        self.assertEqual(cancelled, [call.id])

    def test_connecting_call_without_ack_times_out(self):
        engine = self._engine()
        call = outgoing_call("connecting")
        call.answered_at = time.time() - 30.0
        self._register(engine, call)
        engine._tick()
        self.assertEqual(call.state, "failed")
        self.assertIn("no ACK", call.reason)

    def test_sip_transport_loss_fails_every_call(self):
        engine = self._engine()
        call = self._register(engine, outgoing_call("active"))
        call.rtp = FakeRtp()

        class OneShotStop:
            def __init__(self):
                self.checks = 0

            def is_set(self):
                self.checks += 1
                return self.checks >= 3

            def wait(self, _delay):
                return False

            def set(self):
                pass

        engine._stop = OneShotStop()
        engine._connect = lambda: (_ for _ in ()).throw(
            TelephonyError("network down"))
        engine._run()
        self.assertEqual(call.state, "failed")
        self.assertIn("network disconnected", call.reason)
        self.assertEqual(engine._calls, {})
        self.assertEqual(engine._registration_state, "reconnecting")

    def test_failed_calls_are_pruned_from_memory(self):
        engine = self._engine()
        for index in range(25):
            call = outgoing_call("failed")
            call.id = f"old-{index}"
            call.call_id = f"sip-old-{index}"
            call.ended_at = time.time() - (1000 - index)
            engine._calls[call.id] = call
        newest = self._register(engine, outgoing_call("active"))
        engine._end_call(newest, "local hangup")
        self.assertLessEqual(len(engine._calls), 21)
        self.assertTrue(all(
            item.state in ("ended", "failed")
            for item in engine._calls.values()))


class ApprovalSafetyTests(unittest.TestCase):
    """Rejected, expired and replayed approvals never dial."""

    def setUp(self):
        self.service = SimpleNamespace(
            settings=lambda: {"server": "sip.example.test", "contacts": {}},
            calls=[])

        def record(target):
            self.service.calls.append(target)
            return {"ok": True, "result": "Calling", "call": {"id": "c1"}}

        self.service.call = record
        patcher = mock.patch.object(
            phone_call_tool, "_service", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.approvals = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=60, start_sweeper=False))
        self.approvals.set_session("phone-approval-test")
        self.addCleanup(approvals.shutdown_service)

    def _request(self, target="friend"):
        with mock.patch.object(
                phone_call_tool.telephony, "resolve_target",
                return_value=f"sip:{target}@example.test"), \
             mock.patch.object(
                phone_call_tool, "get_call_allowlist", return_value=[]):
            return phone_call_tool.call(target)

    def test_rejected_approval_never_dials(self):
        blocked = self._request()
        self.assertTrue(blocked["approval_required"])
        self.approvals.reject(self.approvals.active_requests()[0]["approval_id"])
        retried = self._request()
        self.assertTrue(retried["approval_required"])
        self.assertEqual(self.service.calls, [])

    def test_expired_approval_never_dials(self):
        blocked = self._request()
        self.assertTrue(blocked["approval_required"])
        request = self.approvals.active_requests()[0]
        self.approvals.approve(request["approval_id"])
        # Move the stored deadline into the past and run the sweeper once.
        stored = self.approvals._requests[request["approval_id"]]
        stored.expires_monotonic = 0.0
        self.approvals.expire()
        retried = self._request()
        self.assertTrue(retried.get("approval_required"))
        self.assertEqual(self.service.calls, [])

    def test_replayed_approval_cannot_dial_or_authorize_another_number(self):
        self._request("friend")
        approval_id = self.approvals.active_requests()[0]["approval_id"]
        self.approvals.approve(approval_id)
        with mock.patch.object(
                phone_call_tool.telephony, "resolve_target",
                return_value="sip:friend@example.test"), \
             mock.patch.object(
                phone_call_tool, "get_call_allowlist", return_value=[]):
            first = phone_call_tool.call("friend")
            replay = phone_call_tool.call("friend")
        self.assertTrue(first["ok"])
        self.assertTrue(replay["approval_required"])
        other = self._request("stranger")
        self.assertTrue(other.get("approval_required"))
        self.assertEqual(
            self.service.calls, ["sip:friend@example.test"])


class CallPhraseTests(unittest.TestCase):
    def _live(self):
        from main import JarvisLive

        live = JarvisLive.__new__(JarvisLive)
        self.hangups = []

        def hangup():
            self.hangups.append(True)
            return {"ok": True}

        live._telephony = SimpleNamespace(
            bridge_active=lambda: True, hangup=hangup)
        live.ui = SimpleNamespace(write_log=lambda _message: None)
        live._loop = None
        return live

    def test_only_a_complete_goodbye_ends_the_call(self):
        live = self._live()
        self.assertFalse(live._maybe_end_phone_call(
            "I'm done with the first half, keep going"))
        self.assertFalse(live._maybe_end_phone_call(
            "that's all for the first section but continue"))
        self.assertEqual(self.hangups, [])

    def test_complete_goodbye_phrases_end_the_call(self):
        for phrase in ("bye", "bye Jarvis", "that's all for now",
                       "okay, thank you, goodbye"):
            with self.subTest(phrase=phrase):
                self.hangups = []
                live = self._live()
                self.assertTrue(live._maybe_end_phone_call(phrase))
                self.assertEqual(self.hangups, [True])


if __name__ == "__main__":
    unittest.main()

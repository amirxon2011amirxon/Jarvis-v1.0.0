"""State-machine tests for the Live session loop in ``main.py``.

These import ``main`` directly and drive ``JarvisLive``'s receive/playback
helpers with fake sessions and a fake UI — no microphone, no speaker, no
network. They cover the failure where the HUD stayed SPEAKING after a tool
call, and the bookkeeping that made it possible.

Run with:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import main
from actions import tool_calling


class FakeUI:
    """The slice of the UI adapter the session loop touches, recording state."""

    def __init__(self):
        self.states = []
        self.logs = []
        self.events = []
        self.muted = False

    def set_state(self, state):
        self.states.append(str(state))

    def write_log(self, text):
        self.logs.append(str(text))

    def tool_event(self, event):
        self.events.append(dict(event))

    def set_audio_level(self, level):
        pass

    def push_levels(self, levels, hop, at):
        pass


def make_live(**overrides):
    """A JarvisLive with the fields the loop needs, without __init__."""
    live = main.JarvisLive.__new__(main.JarvisLive)
    live.ui = FakeUI()
    live.session = None
    live.audio_in_queue = asyncio.Queue()
    live._turn_done_event = asyncio.Event()
    live._last_audio_at = time.monotonic()
    live._rx_deadline = None
    live._is_speaking = False
    live._speaking_lock = threading.Lock()
    live._out_latency = 0.2
    live._interrupted = False
    live._interrupted_at = 0.0
    live._tool_in_progress = False
    live._resume_handle = None
    live._asst_name = "Aze"
    live._last_out_logged = ""
    live._last_user_speech = time.monotonic()
    live._play_cursor = 0.0
    live._out_level = 0.0
    live._tail_until = 0.0
    live._dev_buffer = 0.0
    live._dev_last = 0.0
    for key, value in overrides.items():
        setattr(live, key, value)
    return live


class PlaybackStopTests(unittest.TestCase):
    """``_should_stop_speaking`` must never strand the HUD in SPEAKING."""
    def test_audio_still_queued_keeps_speaking(self):
        live = make_live()
        live.audio_in_queue.put_nowait(b"\x00\x00")
        live._turn_done_event.set()
        self.assertFalse(live._should_stop_speaking(
            time.monotonic() + 10.0))

    def test_turn_complete_stops_after_a_short_grace(self):
        live = make_live()
        live._turn_done_event.set()
        live._last_audio_at = 100.0
        self.assertFalse(live._should_stop_speaking(100.1))
        self.assertTrue(
            live._should_stop_speaking(100.0 + main._TURN_DONE_GRACE + 0.01))

    def test_missing_turn_complete_is_stopped_by_the_idle_watchdog(self):
        # The old bug: late audio cleared the completion event, so nothing
        # ever told the player to stop and the HUD stayed SPEAKING forever.
        live = make_live()
        live._last_audio_at = 100.0
        self.assertFalse(live._should_stop_speaking(102.0))
        self.assertTrue(live._should_stop_speaking(100.0 + main._SPEAK_IDLE))

    def test_set_speaking_transitions_the_ui(self):
        live = make_live()
        live.set_speaking(True)
        self.assertEqual(live.ui.states[-1], "SPEAKING")
        live.set_speaking(False)
        self.assertEqual(live.ui.states[-1], "LISTENING")
        self.assertFalse(live._is_speaking)

    def test_stopping_while_muted_still_leaves_speaking(self):
        # The old code skipped the transition when muted, so the halo stayed on
        # SPEAKING for the rest of the session after the reply had ended.
        live = make_live()
        live.ui.muted = True
        live.set_speaking(True)
        live.set_speaking(False)
        self.assertEqual(live.ui.states[-1], "LISTENING")


class FakeLoop:
    def __init__(self):
        self.running = True
        self.callbacks = []

    def is_running(self):
        return self.running

    def call_soon_threadsafe(self, callback, *args):
        self.callbacks.append((callback, args))


class InterruptTests(unittest.TestCase):
    def test_draining_is_marshalled_onto_the_session_loop(self):
        live = make_live()
        live._loop = FakeLoop()
        live.audio_in_queue.put_nowait(b"\x00\x00")
        live.audio_in_queue.put_nowait(b"\x01\x01")
        live._turn_done_event.set()

        live.interrupt()

        # The queue/event are asyncio objects owned by another loop: the caller
        # must schedule the drain, not perform it from this thread.
        self.assertEqual(len(live._loop.callbacks), 1)
        self.assertEqual(live.audio_in_queue.qsize(), 2)
        callback, _args = live._loop.callbacks[0]
        callback()

        self.assertTrue(live.audio_in_queue.empty())
        self.assertFalse(live._turn_done_event.is_set())
        self.assertTrue(live._interrupted)

    def test_a_new_user_turn_clears_a_stale_interrupt(self):
        live = make_live()
        live._interrupted = True
        live._interrupted_at = time.monotonic() - main._INTERRUPT_RESUME_AFTER - 0.1
        live._maybe_end_interruption()
        self.assertFalse(live._interrupted)

    def test_a_late_transcript_from_the_interrupted_turn_does_not_clear_it(self):
        live = make_live()
        live._interrupted = True
        live._interrupted_at = time.monotonic()
        live._turn_done_event.clear()
        live._maybe_end_interruption()
        self.assertTrue(live._interrupted)


class FakeSession:
    """Yields prepared server messages, records tool responses, then stops."""

    def __init__(self, messages, stop_after=True):
        self.messages = list(messages)
        self.sent = []
        self._stop = stop_after

    def receive(self):
        async def generator():
            for message in self.messages:
                yield message
            raise asyncio.CancelledError
        return generator()

    async def send_tool_response(self, function_responses):
        self.sent.append(list(function_responses))


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


class ReceiveLoopTests(unittest.TestCase):
    def setUp(self):
        tool_calling.refresh_call_cache()
        self.addCleanup(tool_calling.refresh_call_cache)
        main.tool_calling.register_tool(
            "_test_state_tool",
            lambda value="": {"opened": value or "state"},
            "temporary test tool",
            {"type": "OBJECT", "properties": {}, "required": []})
        self.addCleanup(tool_calling.TOOL_REGISTRY.pop,
                        "_test_state_tool", None)

    def run_loop(self, live, session):
        live.session = session

        async def go():
            try:
                await live._receive_audio()
            finally:
                chain = getattr(live, "_tool_chain", None)
                if chain is not None:
                    try:
                        await chain
                    except asyncio.CancelledError:
                        pass
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(go(), timeout=5))

    def test_turn_complete_rides_a_tool_call_message_and_is_not_lost(self):
        # A tool-call message can also carry the turn's transcription and
        # `turn_complete`; the old branch skipped that block with `continue`,
        # which is what left playback without a completion signal.
        live = make_live()
        session = FakeSession([
            tool_call_message(("call-tc", "_test_state_tool", {"value": "x"}),
                              turn_complete=True),
        ])
        self.run_loop(live, session)

        self.assertTrue(live._turn_done_event.is_set())
        self.assertEqual(len(session.sent), 1)
        self.assertEqual(session.sent[0][0].id, "call-tc")

    def test_each_call_in_a_batch_is_answered(self):
        live = make_live()
        session = FakeSession([
            tool_call_message(
                ("call-a", "_test_state_tool", {"value": "a"}),
                ("call-b", "_test_state_tool", {"value": "b"}),
                turn_complete=True),
        ])
        self.run_loop(live, session)

        self.assertEqual([r.id for r in session.sent[0]],
                         ["call-a", "call-b"])
        self.assertFalse(live._tool_in_progress)

    def test_an_interrupted_tool_call_is_still_answered(self):
        live = make_live()
        live._interrupted = True
        live._interrupted_at = time.monotonic()
        session = FakeSession([
            tool_call_message(
                ("call-interrupted", "_test_state_tool", {"value": "x"}),
                turn_complete=True),
        ])
        self.run_loop(live, session)

        self.assertEqual(session.sent[0][0].id, "call-interrupted")
        self.assertFalse(live._interrupted)

    def test_input_and_output_transcription_reach_the_log(self):
        live = make_live()
        session = FakeSession([
            server_message(turn_complete=True, out_text="Here you go.",
                           in_text="open it please"),
        ])
        self.run_loop(live, session)

        joined = "\n".join(live.ui.logs)
        self.assertIn("You: open it please", joined)
        self.assertIn("Aze: Here you go.", joined)

    def test_audio_is_queued_and_timestamped(self):
        live = make_live()
        before = live._last_audio_at
        session = FakeSession([server_message(data=b"\x01\x02" * 100)])
        self.run_loop(live, session)

        self.assertFalse(live.audio_in_queue.empty())
        self.assertGreaterEqual(live._last_audio_at, before)


class NoToolCallTurnTests(unittest.TestCase):
    """A conversational turn must not execute anything, and must still end."""

    def test_a_turn_without_calls_executes_nothing_and_completes(self):
        live = make_live()
        session = FakeSession([
            server_message(turn_complete=True, out_text="Just chatting."),
        ])
        live.session = session

        async def go():
            await live._receive_audio()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(go(), timeout=5))

        self.assertEqual(session.sent, [])
        self.assertTrue(live._turn_done_event.is_set())
        self.assertIn("Aze: Just chatting.", "\n".join(live.ui.logs))


class SendTextTests(unittest.TestCase):
    class FakeFuture:
        def __init__(self):
            self.callbacks = []

        def add_done_callback(self, callback):
            self.callbacks.append(callback)

        def result(self):
            raise RuntimeError("socket closed")

    class FakeSession:
        def send_client_content(self, **_kwargs):
            async def _noop():
                return None
            return _noop()

    def test_a_failed_send_is_logged_instead_of_lost(self):
        live = make_live()
        live._loop = object()
        live.session = self.FakeSession()
        live._last_out_logged = "old reply"
        live._interrupted = True
        future = self.FakeFuture()

        def fake_run(coro, _loop):
            coro.close()
            return future

        with mock.patch.object(
                main.asyncio, "run_coroutine_threadsafe", side_effect=fake_run):
            sent = live.send_text("hello")

        self.assertTrue(sent)
        self.assertFalse(live._interrupted)
        self.assertEqual(live._last_out_logged, "")
        self.assertEqual(len(future.callbacks), 1)
        future.callbacks[0](future)
        self.assertIn("could not be sent", "\n".join(live.ui.logs))


class PlaybackJitterBufferTests(unittest.TestCase):
    """Playback holds for a reserve so network holes are not audible.

    The Live stream arrives in bursts with 200-600 ms holes; writing every
    chunk the instant it arrives left the device dry for seconds per reply.
    ``_wait_for_prebuffer`` must fill the reserve, stay bounded, and yield to
    an interruption.
    """

    def test_reserve_already_met_returns_immediately(self):
        live = make_live()
        live.audio_in_queue.put_nowait(b"\x00\x00")
        live._dev_buffer = main._RESERVE_S
        started = time.monotonic()
        asyncio.run(asyncio.wait_for(live._wait_for_prebuffer(), timeout=0.5))
        self.assertLess(time.monotonic() - started, 0.2)

    def test_empty_stream_wait_is_bounded(self):
        live = make_live()
        live._dev_buffer = 0.0
        live._dev_last = time.monotonic()
        with mock.patch.object(main, "_RESERVE_WAIT_S", 0.15):
            started = time.monotonic()
            asyncio.run(
                asyncio.wait_for(live._wait_for_prebuffer(), timeout=2.0))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.12)
        self.assertLess(elapsed, 1.0)

    def test_interrupt_releases_the_wait(self):
        live = make_live()
        live._interrupted = True
        started = time.monotonic()
        asyncio.run(asyncio.wait_for(live._wait_for_prebuffer(), timeout=0.5))
        self.assertLess(time.monotonic() - started, 0.2)


if __name__ == "__main__":
    unittest.main()

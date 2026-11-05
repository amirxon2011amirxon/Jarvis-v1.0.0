"""Tests for the screen-understanding action.

Screen capture is replaced with fake frame sources and the Gemini Live
session with a recording fake: no test takes a real screenshot, connects to
the API, or touches the user's real config. The fake engine's event loop is a
real asyncio loop on a background thread, because the frame pump is marshalled
onto the conversation loop exactly as it is in the app.

Run from the project root with:

    python -m unittest tests.test_screen_understanding_tool -v
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from actions import screen_understanding_tool as sut
from actions import tool_calling
from web.ui import WebUI


def jpeg_bytes(color=(30, 40, 50), size=(64, 40)) -> bytes:
    image = Image.new("RGB", size, color)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def wait_until(condition, timeout=3.0, interval=0.02, what="condition"):
    """Poll a boolean predicate, bounded — no test hangs on a race."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


class LoopThread:
    """A real asyncio loop on a background thread — the engine's loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2.0)
        self.loop.close()


class FakeSession:
    """Records what crosses the "websocket" of the shared Live session."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.video = []
        self.attempts = 0
        self.client_content = 0
        self.fail_from = None       # index at/after which sends raise
        self.fail_error = ConnectionError("connection dropped")
        self.thread_ids = set()

    async def send_realtime_input(self, *, video=None, **kwargs):
        with self.lock:
            self.thread_ids.add(threading.get_ident())
            self.attempts += 1
            if self.fail_from is not None and len(self.video) >= self.fail_from:
                raise self.fail_error
        await asyncio.sleep(0)
        with self.lock:
            self.video.append(video)

    async def send_client_content(self, **kwargs):
        with self.lock:
            self.client_content += 1

    def count(self) -> int:
        with self.lock:
            return len(self.video)


class FakeEngine:
    def __init__(self) -> None:
        self.harness = LoopThread()
        self._loop = self.harness.loop
        self.session = FakeSession()

    def close(self) -> None:
        self.harness.stop()


class ScreenToolTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.obs = sut.ScreenObserver()
        self._saved_observer = sut._OBSERVER
        sut._OBSERVER = self.obs
        sut._ENGINE.set(None)
        self.engines: list[FakeEngine] = []

    def tearDown(self) -> None:
        try:
            self.obs.stop_continuous()
        except Exception:
            pass
        for engine in self.engines:
            # Flush one loop tick so a posted pump-cancel completes BEFORE
            # the fake loop closes — no dangling task warnings.
            try:
                drain = asyncio.run_coroutine_threadsafe(
                    asyncio.sleep(0), engine._loop)
                drain.result(timeout=2.0)
            except Exception:                        # noqa: BLE001
                pass
            engine.close()
        sut._ENGINE.set(None)
        sut._OBSERVER = self._saved_observer

    def make_engine(self, session: FakeSession | None = None) -> FakeEngine:
        engine = FakeEngine()
        if session is not None:
            engine.session = session
        self.engines.append(engine)
        sut.set_engine(engine)
        return engine

    def start(self, **patches) -> dict:
        """Start continuous observation with fast test pacing."""
        with mock.patch.multiple(
                sut, _FRAME_INTERVAL_S=patches.get("interval", 0.05),
                _START_TIMEOUT_S=patches.get("start_timeout", 3.0)):
            return tool_calling.execute_tool(
                "screen_understanding",
                {"operation": "start_continuous_observation"})

    def stop(self) -> dict:
        return tool_calling.execute_tool(
            "screen_understanding",
            {"operation": "stop_continuous_observation"})

    def one_time(self, question="What do you see on my screen?") -> dict:
        return tool_calling.execute_tool(
            "screen_understanding", {"operation": "one_time",
                                     "question": question})

    def status(self) -> dict:
        return tool_calling.execute_tool(
            "screen_understanding", {"operation": "get_observation_status"})

    def no_capture_threads_left(self) -> bool:
        return not any(thread.is_alive()
                       for thread in threading.enumerate()
                       if thread.name.startswith("aze-screen"))


class OneTimeTests(ScreenToolTestBase):
    """A + B: one-time observation through the existing ladder."""

    def _patch_call(self, answer="A green window with an error dialog."):
        return mock.patch.object(
            sut._gemini, "call",
            return_value=SimpleNamespace(text=answer))

    def test_one_time_answers_and_stays_inactive(self):
        self.obs._frame_source = lambda: jpeg_bytes((0, 255, 0))
        with self._patch_call() as call_mock:
            result = self.one_time("What do you see on my screen?")
        self.assertNotIn("error", result)
        self.assertIn("green", result["result"])
        # The question and ONE current frame reached the vision call.
        contents = call_mock.call_args.args[0]
        self.assertEqual(contents[0], "What do you see on my screen?")
        blob = contents[1].inline_data
        self.assertEqual(blob.mime_type, "image/jpeg")
        self.assertEqual(blob.data, jpeg_bytes((0, 255, 0)))
        self.assertEqual(call_mock.call_args.kwargs["tier"], "smart")
        # Continuous observation stayed INACTIVE — nothing started.
        snap = tool_calling.execute_tool(
            "screen_understanding", {"operation": "get_observation_status"})
        self.assertEqual(snap["state"], sut.INACTIVE)
        self.assertFalse(snap["observation_active"])
        self.assertEqual(snap["started_at"], None)

    def test_one_time_passes_the_error_question_through(self):
        self.obs._frame_source = lambda: jpeg_bytes()
        with self._patch_call() as call_mock:
            result = self.one_time("Explain this error.")
        self.assertNotIn("error", result)
        self.assertEqual(call_mock.call_args.args[0][0], "Explain this error.")

    def test_one_time_requires_a_question(self):
        result = tool_calling.execute_tool(
            "screen_understanding", {"operation": "one_time"})
        self.assertIn("error", result)

    def test_one_time_reports_capture_failure(self):
        def broken():
            raise OSError("no display")
        self.obs._frame_source = broken
        result = self.one_time()
        self.assertIn("error", result)
        self.assertIn("no display", result["error"])
        self.assertEqual(self.status()["state"], sut.INACTIVE)

    def test_one_time_reports_a_silent_model(self):
        self.obs._frame_source = lambda: jpeg_bytes()
        with mock.patch.object(sut._gemini, "call", return_value=None):
            result = self.one_time()
        self.assertIn("error", result)
        self.assertIn("nothing", result["error"])


class StartStopTests(ScreenToolTestBase):
    """C, D, E, F, G: the persistent stream lifecycle."""

    def test_start_streams_frames_into_the_engine_session(self):
        engine = self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        result = self.start()
        self.assertNotIn("error", result)
        self.assertEqual(result["state"], sut.OBSERVING)
        self.assertTrue(wait_until(lambda: engine.session.count() >= 2,
                                   what="frames streamed"))
        blob = engine.session.video[0]
        self.assertEqual(blob.mime_type, "image/jpeg")
        self.assertEqual(blob.data, jpeg_bytes())
        # The frames ride the engine's own loop and never open a session.
        self.assertEqual(engine.session.client_content, 0)
        loop_thread = engine.harness.thread.ident
        self.assertIn(loop_thread, engine.session.thread_ids)

    def test_visual_changes_reach_the_same_session(self):
        engine = self.make_engine()
        frames = [jpeg_bytes((255, 0, 0))]
        self.obs._frame_source = frames.pop
        self.start()
        session = engine.session            # the ONE live session obj
        self.assertTrue(wait_until(lambda: session.count() >= 1,
                                   what="first frame before the change"))
        first = session.video[0].data
        # Something appears on the screen afterwards: the newest frame wins.
        frames.append(jpeg_bytes((0, 0, 255)))
        self.assertTrue(wait_until(
            lambda: any(item.data != first for item in session.video),
            what="changed frame streamed"))
        self.assertIs(engine.session, session,
                      "no second session was created for the change")
        self.assertEqual(session.client_content, 0,
                         "frames are realtime input, never client turns")

    def test_after_a_session_rebuild_the_stream_follows(self):
        engine = self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        self.start()
        self.assertTrue(wait_until(lambda: engine.session.count() >= 1,
                                   what="first frames"))
        # The engine reconnects: a NEW live session object appears.
        replacement = FakeSession()
        engine.session = replacement
        # The pump reads the session fresh per frame: frames resume there.
        self.assertTrue(wait_until(lambda: replacement.count() >= 1,
                                   timeout=3.0, what="frames in new session"))
        self.assertEqual(self.status()["state"], sut.OBSERVING)
        self.assertEqual(replacement.client_content, 0)

    def test_questions_while_observing_use_the_active_session(self):
        engine = self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        self.start()
        with mock.patch.object(sut._gemini, "call") as call_mock:
            result = self.one_time("What do you see now?")
        self.assertNotIn("error", result)
        self.assertTrue(result["observation_active"])
        self.assertTrue(result["uses_active_session"])
        call_mock.assert_not_called()
        # Still observing, and the stream kept flowing.
        sent_before = engine.session.count()
        self.assertTrue(wait_until(lambda: engine.session.count() > sent_before,
                                   what="stream continues after a query"))
        self.assertEqual(self.status()["state"], sut.OBSERVING)

    def test_stop_releases_everything(self):
        engine = self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        self.start()
        self.assertTrue(wait_until(lambda: engine.session.count() >= 2,
                                   what="frames before stop"))
        result = self.stop()
        self.assertNotIn("error", result)
        self.assertEqual(result["state"], sut.INACTIVE)
        self.assertEqual(self.status()["observation_active"], False)
        self.assertTrue(self.no_capture_threads_left())
        # The visual stream is over: the send count stops growing.
        sent = engine.session.count()
        time.sleep(0.15)
        self.assertEqual(engine.session.count(), sent)
        # The shared conversation session was never touched.
        self.assertEqual(engine.session.client_content, 0)

    def test_stop_is_idempotent_when_inactive(self):
        result = self.stop()
        self.assertNotIn("error", result)
        self.assertIn("already inactive", result["result"])
        self.assertEqual(result["state"], sut.INACTIVE)

    def test_duplicate_start_keeps_one_observation(self):
        engine = self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        first = self.start()
        self.assertEqual(first["state"], sut.OBSERVING)
        second = self.start()
        self.assertNotIn("error", second)
        self.assertIn("already active", second["result"])
        threads = [thread for thread in threading.enumerate()
                   if thread.name == "aze-screen-capture"]
        self.assertEqual(len(threads), 1)
        # one stop ends it: no second capture loop left behind
        self.assertEqual(self.stop()["state"], sut.INACTIVE)
        self.assertTrue(self.no_capture_threads_left())

    def test_restart_after_stop_works_fresh(self):
        self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        self.start()
        self.stop()
        result = self.start()
        self.assertNotIn("error", result)
        self.assertEqual(result["state"], sut.OBSERVING)
        self.assertTrue(self.no_capture_threads_left() is False)  # a new capture thread lives again


class ObservationStatePublishTests(ScreenToolTestBase):
    """Every lifecycle change reaches the dashboard listener."""

    def setUp(self) -> None:
        super().setUp()
        self.states: list[str] = []
        self.snapshots: list[dict] = []

        def listener(snapshot: dict) -> None:
            self.snapshots.append(snapshot)
            self.states.append(snapshot["state"])

        self.obs.set_listener(listener)

    def test_start_publishes_starting_then_observing(self):
        self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        result = self.start()

        self.assertEqual(result["state"], sut.OBSERVING)
        self.assertIn(sut.STARTING, self.states)
        self.assertIn(sut.OBSERVING, self.states)
        self.assertLess(self.states.index(sut.STARTING),
                        self.states.index(sut.OBSERVING))
        final = self.snapshots[-1]
        self.assertEqual(final["state"], sut.OBSERVING)
        self.assertTrue(final["observation_active"])

    def test_stop_publishes_stopping_then_inactive(self):
        self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        self.start()
        self.states.clear()
        self.snapshots.clear()

        self.stop()

        self.assertEqual(self.states, [sut.STOPPING, sut.INACTIVE])
        self.assertFalse(self.snapshots[-1]["observation_active"])
        self.assertEqual(self.snapshots[-1]["state"], sut.INACTIVE)

    def test_startup_failure_publishes_inactive(self):
        # No engine registered: start cannot stream, and the indicator must
        # settle on the true resting state rather than staying STARTING.
        result = self.start()

        self.assertIn("error", result)
        self.assertIn(sut.STARTING, self.states)
        self.assertEqual(self.states[-1], sut.INACTIVE)

    def test_unexpected_failure_publishes_error_then_inactive(self):
        self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        self.start()
        self.states.clear()
        self.snapshots.clear()

        self.obs._fatal("simulated stream failure")

        self.assertTrue(wait_until(
            lambda: self.states and self.states[-1] == sut.INACTIVE,
            what="failure cleanup settles inactive"))
        self.assertIn(sut.ERROR, self.states)
        final = self.snapshots[-1]
        self.assertEqual(final["state"], sut.INACTIVE)
        self.assertFalse(final["observation_active"])
        self.assertEqual(final["last_error"], "simulated stream failure")

    def test_shutdown_clears_the_published_active_state(self):
        self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        self.start()
        self.states.clear()

        sut.shutdown_observation("app shutdown")

        self.assertEqual(self.states[-1], sut.INACTIVE)

    def test_web_ui_publishes_and_snapshots_observation(self):
        ui = WebUI()
        emitted: list[dict] = []
        ui._emit = emitted.append
        snapshot = {
            "state": sut.OBSERVING,
            "observation_active": True,
            "started_at": "2026-01-01T00:00:00Z",
            "frames_streamed": 12,
            "frames_dropped": 0,
            "frame_interval_seconds": 1.0,
            "last_error": "",
            "voice_session_connected": True,
        }

        ui.observation_event(snapshot)

        self.assertEqual(emitted[-1]["type"], "observation.updated")
        self.assertEqual(emitted[-1]["state"], sut.OBSERVING)
        self.assertTrue(emitted[-1]["active"])
        self.assertEqual(ui.snapshot()["observation"]["frames_streamed"], 12)


class ShutdownAndFailureTests(ScreenToolTestBase):
    """H, I, J, L: shutdown, voice compatibility and bounded failure."""

    def test_shutdown_stops_observation(self):
        engine = self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        self.start()
        result = sut.shutdown_observation("app shutdown")
        self.assertEqual(result["state"], sut.INACTIVE)
        self.assertTrue(self.no_capture_threads_left())
        self.assertEqual(engine.session.client_content, 0)
        status = self.status()
        self.assertEqual(status["state"], sut.INACTIVE)

    def test_voice_session_remembers_its_job(self):
        engine = self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        self.start()
        self.assertTrue(wait_until(lambda: engine.session.count() >= 1,
                                   what="frames flowing"))
        # The voice path sends a typed turn / tool response alongside the
        # frames; the session accepts it and the stream continues.
        future = asyncio.run_coroutine_threadsafe(
            engine.session.send_client_content(turns="hello", turn_complete=True),
            engine._loop)
        future.result(timeout=2.0)
        self.assertEqual(engine.session.client_content, 1)
        sent_before = engine.session.count()
        self.assertTrue(wait_until(lambda: engine.session.count() > sent_before,
                                   what="stream continues"))
        self.stop()
        # After the screen stream ends the session still accepts realtime
        # audio — stopping observation destroyed nothing shared.
        future = asyncio.run_coroutine_threadsafe(
            engine.session.send_realtime_input(audio="audio-bytes"),
            engine._loop)
        future.result(timeout=2.0)
        self.assertEqual(engine.session.client_content, 1)

    def test_send_failures_stop_the_stream_after_a_bounded_budget(self):
        engine = self.make_engine()
        failing = FakeSession()
        failing.fail_from = 1        # first frame succeeds, then the link dies
        engine.session = failing
        self.obs._frame_source = lambda: jpeg_bytes()
        patcher = mock.patch.object(sut, "_SEND_FAILURE_LIMIT", 4)
        patcher.start()
        self.addCleanup(patcher.stop)
        result = self.start()
        self.assertEqual(result["state"], sut.OBSERVING)
        self.assertTrue(wait_until(
            lambda: self.status()["state"] == sut.INACTIVE
            and self.status()["last_error"],
            timeout=5.0, what="bounded failure cleanup"))
        # One success plus exactly the bounded number of failed attempts.
        self.assertEqual(failing.attempts, 1 + 4)
        self.assertEqual(failing.count(), 1)
        self.assertTrue(self.no_capture_threads_left())
        snap = self.status()
        self.assertIn("ConnectionError", snap["last_error"])
        self.assertEqual(snap["observation_active"], False)

    def test_missing_voice_session_waits_bounded_then_stops(self):
        engine = self.make_engine()
        self.obs._frame_source = lambda: jpeg_bytes()
        engine.session = None        # the conversation session is offline
        patcher = mock.patch.object(sut, "_SESSION_WAIT_LIMIT_S", 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)
        result = self.start()
        self.assertEqual(result["state"], sut.OBSERVING)
        self.assertTrue(wait_until(
            lambda: self.status()["state"] == sut.INACTIVE
            and self.status()["last_error"],
            timeout=5.0, what="waited too long for the session"))
        self.assertEqual(self.status()["state"], sut.INACTIVE)
        self.assertTrue(self.no_capture_threads_left())

    def test_stop_while_starting_leaves_no_orphans(self):
        engine = self.make_engine()
        gate = threading.Event()     # the "screen" produces nothing yet
        self.obs._frame_source = lambda: (jpeg_bytes() if gate.wait(0.75) else None)
        started: dict = {}

        def begin():
            started["result"] = self.start(start_timeout=3.0)

        worker = threading.Thread(target=begin)
        worker.start()
        time.sleep(0.3)              # the start is still in STARTING now
        stop_result = self.stop()
        self.assertEqual(stop_result["state"], sut.INACTIVE)
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(started["result"]["state"], sut.INACTIVE)
        self.assertEqual(self.status()["state"], sut.INACTIVE)
        self.assertTrue(self.no_capture_threads_left())
        gate.set()                   # unblock the fake source for teardown


class RegistrationTests(unittest.TestCase):
    """K: the action is registered and callable through the normal flow."""

    def test_registered_in_the_real_registry(self):
        info = tool_calling.TOOL_REGISTRY.get("screen_understanding")
        self.assertIsNotNone(info)
        self.assertTrue(callable(info["func"]))
        self.assertIn("PRIMARY DISPLAY", info["description"])
        self.assertEqual(info["parameters"]["required"], ["operation"])
        self.assertTrue(tool_calling.has_tool("screen_understanding"))

    def test_declaration_reaches_the_live_session_format(self):
        declarations = {d["name"]: d for d in
                        tool_calling.get_tool_declarations()}
        declaration = declarations["screen_understanding"]
        self.assertEqual(declaration["parameters"]["type"], "OBJECT")
        self.assertIn("operation", declaration["parameters"]["properties"])
        self.assertIn("question", declaration["parameters"]["properties"])

    def test_unknown_operation_is_a_structured_error(self):
        result = tool_calling.execute_tool(
            "screen_understanding", {"operation": "record_a_video"})
        self.assertIn("error", result)
        self.assertIn("one of", result["error"])

    def test_event_target_reports_the_operation(self):
        self.assertEqual(
            tool_calling._event_target(
                "screen_understanding", {"operation": "one_time"}),
            "one_time")

    def test_status_reports_state_even_when_never_started(self):
        observer = sut.ScreenObserver()
        saved = sut._OBSERVER
        sut._OBSERVER = observer
        try:
            result = tool_calling.execute_tool(
                "screen_understanding",
                {"operation": "get_observation_status"})
        finally:
            sut._OBSERVER = saved
        self.assertEqual(result["state"], sut.INACTIVE)
        self.assertFalse(result["observation_active"])


if __name__ == "__main__":
    unittest.main()

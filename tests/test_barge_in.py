"""Full-duplex barge-in: detector gating, pre-roll forwarding and output abort."""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest import mock

import numpy as np

import main
from core.echo import EchoGuard

SR_OUT = 24000
SR_IN = 16000


class FakeUI:
    def __init__(self):
        self.states = []
        self.logs = []
        self.levels = []
        self.muted = False

    def set_state(self, state):
        self.states.append(str(state))

    def write_log(self, text):
        self.logs.append(str(text))

    def set_audio_level(self, level):
        self.levels.append(level)


class FakeLoop:
    def __init__(self):
        self.callbacks = []
        self.running = True

    def is_running(self):
        return self.running

    def call_soon_threadsafe(self, callback, *args):
        self.callbacks.append((callback, args))


class FakeEcho:
    def __init__(self, results):
        self.results = list(results)
        self.armed = True
        self._hist = [(time.monotonic(), np.ones(8, dtype=np.float32), 0.5)]
        self.reset_calls = 0

    def is_user_speech(self, pcm, sr, level, when=None):
        if self.results:
            return self.results.pop(0)
        return False

    def reset(self):
        self.reset_calls += 1


class FakeStream:
    def __init__(self):
        self.aborted = 0
        self.started = 0

    def abort(self):
        self.aborted += 1

    def start(self):
        self.started += 1


def make_live(*, echo=None, speaking=False):
    live = main.JarvisLive.__new__(main.JarvisLive)
    live.ui = FakeUI()
    live.session = None
    live.audio_in_queue = asyncio.Queue()
    live.out_queue = asyncio.Queue()
    live._turn_done_event = asyncio.Event()
    live._loop = FakeLoop()
    live._is_speaking = bool(speaking)
    live._speaking_lock = threading.Lock()
    live._interrupted = False
    live._interrupted_at = 0.0
    live._barge_in_active = False
    live._barge_in_event = threading.Event()
    live._barge_in_count = 0
    live._pre_roll = []
    live._pre_roll_lock = threading.Lock()
    live._out_stream = None
    live._output_aborted = False
    live._out_latency = 0.2
    live._tail_until = 0.0
    live._out_level = 0.0
    live._play_cursor = 0.0
    live._memory_turn_injected = False
    live._ptt_enabled = False
    live._ptt_held = False
    live._echo = echo
    return live


def pcm_block(value: int = 1000) -> np.ndarray:
    return np.full(1024, value, dtype=np.int16)


def tone(seconds: float, sr: int, freqs: list[float], amp: float = 0.35):
    t = np.arange(int(seconds * sr)) / sr
    signal = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
    return (signal * amp * 32767).astype(np.int16)


class PhoneBridgeBargeInTests(unittest.TestCase):
    class FakePhone:
        def __init__(self):
            self.flushed = 0

        def bridge_active(self):
            return True

        def bridge_call_id(self):
            return "phone1"

        def buffered_seconds(self):
            return 0.0

        def flush_audio(self):
            self.flushed += 1

    def _phone_live(self):
        live = make_live(speaking=True)
        live._telephony = self.FakePhone()
        live._phone_echo = EchoGuard()
        live._phone_trace = False
        live._phone_pre_roll = []
        live._phone_pre_roll_lock = threading.Lock()
        live._phone_tail_until = 0.0
        live._phone_rx_blocks = 0
        live._phone_rx_forwarded = 0
        live._phone_rx_suppressed = 0
        live._phone_tx_batches = 0
        live._phone_tx_bytes = 0
        live._phone_tx_dropped = 0
        live._phone_barge_ins = 0
        live._phone_ignored_noise = 0
        live._phone_speech_run = 0
        live._phone_barge_level = 0.10
        live._phone_barge_blocks = 8
        live._phone_speech_grace_until = 0.0
        live.forwarded = []
        live.barges = []
        live._enqueue_mic_chunk = lambda pcm: live.forwarded.append(pcm)
        live._begin_barge_in = lambda pre: live.barges.append(pre)
        return live

    def test_phone_echo_does_not_interrupt_long_replies(self):
        live = self._phone_live()
        output = tone(0.05, SR_OUT, [220, 440, 880], 0.4)
        echo = tone(0.064, SR_IN, [220, 440, 880], 0.4)
        for _ in range(30):
            live._phone_echo.note_output(output, SR_OUT, 0.5)
            live._handle_phone_block("phone1", echo.tobytes())
        self.assertEqual(live.forwarded, [])
        self.assertEqual(live.barges, [])
        self.assertEqual(live._phone_rx_suppressed, 30)

    def test_phone_caller_voice_still_barges_in(self):
        live = self._phone_live()
        output = tone(0.05, SR_OUT, [220, 440, 880], 0.4)
        echo = tone(0.064, SR_IN, [220, 440, 880], 0.4)
        for _ in range(24):
            live._phone_echo.note_output(output, SR_OUT, 0.5)
            live._handle_phone_block("phone1", echo.tobytes())
        voice = tone(0.064, SR_IN, [220, 440, 880, 1400, 2200], 0.7)
        for _ in range(live._phone_barge_blocks + live._phone_echo.required_blocks):
            live._phone_echo.note_output(output, SR_OUT, 0.5)
            live._handle_phone_block("phone1", voice.tobytes())
        self.assertTrue(live.barges)
        self.assertGreaterEqual(live._telephony.flushed, 1)


class EchoGuardTests(unittest.TestCase):
    def test_echo_is_not_mistaken_for_a_voice_after_calibration(self):
        guard = EchoGuard()
        output = tone(0.05, SR_OUT, [220, 440, 880], 0.4)
        echo = tone(0.064, SR_IN, [220, 440, 880], 0.4)
        for _ in range(24):
            guard.note_output(output, SR_OUT, 0.5)
            self.assertFalse(guard.is_user_speech(echo, SR_IN, 0.5))
        self.assertFalse(guard.is_user_speech(echo, SR_IN, 0.5))

    def test_a_distinct_voice_interrupts_after_corroboration(self):
        guard = EchoGuard()
        output = tone(0.05, SR_OUT, [220, 440, 880], 0.4)
        echo = tone(0.064, SR_IN, [220, 440, 880], 0.4)
        for _ in range(24):
            guard.note_output(output, SR_OUT, 0.5)
            guard.is_user_speech(echo, SR_IN, 0.5)
        voice = tone(0.064, SR_IN, [220, 440, 880, 1400, 2200], 0.7)
        results = []
        for _ in range(guard.required_blocks):
            guard.note_output(output, SR_OUT, 0.5)
            results.append(guard.is_user_speech(voice, SR_IN, 0.5))
        self.assertFalse(results[0])
        self.assertTrue(results[-1])

    def test_isolated_speech_like_block_does_not_interrupt(self):
        guard = EchoGuard()
        output = tone(0.05, SR_OUT, [220, 440, 880], 0.4)
        echo = tone(0.064, SR_IN, [220, 440, 880], 0.4)
        for _ in range(24):
            guard.note_output(output, SR_OUT, 0.5)
            guard.is_user_speech(echo, SR_IN, 0.5)
        voice = tone(0.064, SR_IN, [1200, 2400], 0.7)
        guard.note_output(output, SR_OUT, 0.5)
        self.assertFalse(guard.is_user_speech(voice, SR_IN, 0.5))
        guard.note_output(output, SR_OUT, 0.5)
        self.assertFalse(guard.is_user_speech(echo, SR_IN, 0.5))

    def test_quiet_room_noise_never_counts_as_speech(self):
        guard = EchoGuard()
        guard.note_output(tone(0.05, SR_OUT, [220, 440], 0.1), SR_OUT, 0.4)
        for _ in range(20):
            self.assertFalse(guard.is_user_speech(pcm_block(10), SR_IN, 0.0))


class BargeInDetectionTests(unittest.TestCase):
    def test_preroll_and_trigger_are_forwarded_once(self):
        echo = FakeEcho([False, False, True])
        live = make_live(echo=echo, speaking=True)
        for _ in range(3):
            live._handle_mic_block(pcm_block())
        self.assertEqual(len(live._loop.callbacks), 1)
        callback, args = live._loop.callbacks[0]
        self.assertEqual(callback, live._begin_barge_in)
        self.assertEqual(len(args[0]), 3 * pcm_block().nbytes)
        self.assertTrue(live._barge_in_event.is_set())

        callback(*args)
        self.assertTrue(live._interrupted)
        self.assertTrue(live._barge_in_active)
        self.assertFalse(live._is_speaking)
        self.assertEqual(live.ui.states[-1], "LISTENING")
        self.assertEqual(live.out_queue.qsize(), 3)
        self.assertIn("Barge-in detected", " ".join(live.ui.logs))

    def test_echo_does_not_open_the_pre_roll_or_interrupt(self):
        live = make_live(echo=FakeEcho([False] * 12), speaking=True)
        for _ in range(12):
            live._handle_mic_block(pcm_block())
        self.assertEqual(live._loop.callbacks, [])
        self.assertFalse(live._barge_in_event.is_set())
        self.assertEqual(live.out_queue.qsize(), 0)

    def test_unarmed_detector_buffers_but_never_interrupts(self):
        echo = FakeEcho([True] * 4)
        echo.armed = False
        live = make_live(echo=echo, speaking=True)
        for _ in range(4):
            live._handle_mic_block(pcm_block())
        self.assertEqual(live._loop.callbacks, [])
        self.assertEqual(len(live._pre_roll), 4)
        self.assertEqual(live.out_queue.qsize(), 0)

    def test_muted_microphone_never_barges_in(self):
        live = make_live(echo=FakeEcho([True]), speaking=True)
        live.ui.muted = True
        live._handle_mic_block(pcm_block())
        self.assertEqual(live._loop.callbacks, [])

    def test_push_to_talk_not_held_never_barges_in(self):
        live = make_live(echo=FakeEcho([True]), speaking=True)
        live._ptt_enabled = True
        live._ptt_held = False
        live._handle_mic_block(pcm_block())
        self.assertEqual(live._loop.callbacks, [])

    def test_barge_in_drains_queued_reply_and_aborts_device_buffer(self):
        live = make_live(speaking=True)
        stream = FakeStream()
        live._out_stream = stream
        live.audio_in_queue.put_nowait(b"old-1")
        live.audio_in_queue.put_nowait(b"old-2")
        live._turn_done_event.set()

        live._begin_barge_in(pcm_block().tobytes() * 2)

        self.assertTrue(live.audio_in_queue.empty())
        self.assertTrue(live._interrupted)
        self.assertTrue(live._barge_in_active)
        self.assertEqual(live.out_queue.qsize(), 2)
        self.assertEqual(live.ui.states[-1], "LISTENING")
        # No running loop in this test, so abort runs synchronously.
        self.assertEqual(stream.aborted, 1)
        self.assertTrue(live._output_aborted)

    def test_new_reply_clears_barge_state_and_preroll(self):
        live = make_live(speaking=False)
        live._barge_in_active = True
        live._barge_in_event.set()
        with live._pre_roll_lock:
            live._pre_roll.append(b"stale")
        live.set_speaking(True)
        self.assertFalse(live._barge_in_active)
        self.assertFalse(live._barge_in_event.is_set())
        self.assertEqual(live._pre_roll, [])
        self.assertEqual(live.ui.states[-1], "SPEAKING")

    def test_server_interruption_stops_the_same_playback(self):
        live = make_live(speaking=True)
        live.audio_in_queue.put_nowait(b"queued")
        live._handle_server_interruption()
        self.assertTrue(live.audio_in_queue.empty())
        self.assertTrue(live._interrupted)
        self.assertTrue(live._barge_in_active)
        self.assertFalse(live._is_speaking)
        self.assertEqual(live.ui.states[-1], "LISTENING")
        self.assertIn("Speech interrupted", " ".join(live.ui.logs))

    def test_output_abort_sets_the_restart_flag(self):
        live = make_live()
        stream = FakeStream()
        live._out_stream = stream
        live._abort_output_stream()
        self.assertEqual(stream.aborted, 1)
        self.assertTrue(live._output_aborted)


class ConfigTests(unittest.TestCase):
    def test_realtime_config_requests_interruption(self):
        live = make_live()
        live._tuned_live = True
        with mock.patch("main.get_turn_tuning", return_value={
            "enabled": False, "silence_ms": 550, "prefix_ms": 150,
            "end_sensitivity": "high", "start_sensitivity": "default",
        }), mock.patch("main.get_thinking_enabled", return_value=False):
            config = live._tuning_config()
        realtime = config["realtime_input_config"]
        self.assertEqual(
            realtime.activity_handling,
            "START_OF_ACTIVITY_INTERRUPTS",
        )
        self.assertIsNone(realtime.automatic_activity_detection)


if __name__ == "__main__":
    unittest.main()

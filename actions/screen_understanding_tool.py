"""
Screen understanding for Jarvis v1.0.0 — one registered action, two ways of looking.

    one_time                      "What do you see on my screen?" — one frame,
                                  one answer, then it is over.
    start_continuous_observation  "Start watching my screen" — the screen
                                  streams into the Live session until the
                                  user explicitly says stop.
    stop_continuous_observation   "Stop watching my screen"
    get_observation_status        "Are you still watching?"

WHY CONTINUOUS OBSERVATION STREAMS INTO THE CONVERSATION'S OWN SESSION
    The conversation in ``main.py`` runs ONE Gemini Live session: microphone
    audio goes in through ``send_realtime_input(audio=...)``, tool responses
    and typed turns are sent from other tasks on the same loop, and one
    ``_receive_audio`` loop consumes everything.  Continuous screen
    observation adds a THIRD sender on exactly those terms: video frames go
    in through ``send_realtime_input(video=...)``, the documented streaming
    path in google-genai ≥ 2.19 (each call awaits one atomic websocket
    message, and the ``websockets`` transport serialises concurrent sends —
    the same guarantee the audio path already relies on).

    Frames generate no response by themselves — models respond to a turn, and
    a picture with no speech never starts one — so the single receive loop,
    its stall watchdog and its tool-call handling are untouched.  What those
    frames buy is that the model the user is TALKING to is the model that can
    now SEE the screen: "What do you see now?" while observation is active is
    answered by the session itself, in the user's own voice channel, with no
    second session, no second response consumer and no question-forwarding
    hop.  Reconnects are the engine's own business: the frame pump reads
    ``engine.session`` freshly per frame, so after a voluntary or involuntary
    rebuild the stream continues into the new session (a brief visual gap is
    expected and recent frames win); the stream itself never ends a turn.

WHY ONE-TIME DOES NOT TOUCH THE CONVERSATION SESSION
    A one-time look must leave no session behind.  It captures one frame and
    answers through the established side-call ladder in ``core.gemini.call``
    (``tier="smart"``), which leads with a throwaway one-shot Live session on
    its own thread — the app's measured image path — and falls back to the
    REST rungs.  Side calls cannot reach the microphone or the speaker, so
    one-time observation can never talk over the conversation.

CAPTURE
    ``mss`` — lightweight, Windows-native, no extra machinery.  One capture
    thread, primary display only, fixed pace of 1 fps (the Live API's own
    video guidance), frames resized to a 1024-px long edge and JPEG-encoded
    at quality 70 so ordinary text stays legible without paying for
    resolution the stream does not carry.  Frames live only in memory, in a
    bounded two-slot queue where the newest frame wins; nothing ever touches
    the disk, and logs carry counts — never screen contents.

STATE
    INACTIVE → STARTING → OBSERVING → STOPPING → INACTIVE, with ERROR marking
    a stop that could not finish or a failure being cleaned up.  The runtime
    state below is the only authority; conversation history is never
    consulted.  Start and stop are idempotent, safe during startup, and every
    failure path cleans up and returns to a safe state — silently pretending
    observation is running is not an option.
"""
from __future__ import annotations

import asyncio
import atexit
import io
import threading
import time
import traceback
from typing import Any, Callable, Dict, Optional

from core import gemini as _gemini

OPERATIONS = (
    "one_time",
    "start_continuous_observation",
    "stop_continuous_observation",
    "get_observation_status",
)

# ─── Lifecycle states ────────────────────────────────────────────────────────
INACTIVE = "INACTIVE"
STARTING = "STARTING"
OBSERVING = "OBSERVING"
STOPPING = "STOPPING"
ERROR = "ERROR"

# ─── Constants. Tests patch these module attributes where a real pace or a
# real failure budget would make a test run for minutes. ─────────────────────
_FRAME_INTERVAL_S = 1.0        # 1 fps — the Live API's guidance for video
_QUEUE_MAXLEN = 2              # bounded; a lagging stream keeps the newest
_MAX_EDGE = 1024               # downscale: legible text, modest payload
_JPEG_QUALITY = 70
_START_TIMEOUT_S = 8.0         # how long start waits for the first frame
_JOIN_TIMEOUT_S = 2.0          # how long a stop waits for the capture thread
_CAPTURE_FAILURE_LIMIT = 10    # consecutive failed grabs before we give up
_SEND_FAILURE_LIMIT = 30       # consecutive failed sends before we give up
                               # (~30 s outage at 1 fps — bounded recovery,
                               # consistent with the engine's own backoff)
_SESSION_WAIT_LIMIT_S = 120.0  # the voice session rebuilds in seconds; if it
                               # stays gone this long the stream stops


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# ─── Engine hook ─────────────────────────────────────────────────────────────
class _EngineHolder:
    """The one running ``JarvisLive`` engine, registered by ``main.py``.

    The tool never opens its own conversation session: it borrows this
    engine's Live session, read freshly per frame, so reconnects follow the
    engine instead of becoming the tool's problem.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engine: Any = None

    def set(self, engine: Any) -> None:
        with self._lock:
            self._engine = engine

    def get(self) -> Any:
        with self._lock:
            return self._engine


_ENGINE = _EngineHolder()


# ─── Capture helpers ─────────────────────────────────────────────────────────
def _prepare_jpeg(raw: bytes, width: int, height: int) -> bytes:
    """BGRA pixels → downscaled JPEG bytes. All in memory, nothing copies
    through the disk."""
    import numpy as np
    from PIL import Image

    px = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 4))
    # mss grabs BGRA; the model wants RGB. One vectorised channel flip.
    image = Image.fromarray(px[:, :, :3][:, :, ::-1])
    if max(width, height) > _MAX_EDGE:
        image.thumbnail((_MAX_EDGE, _MAX_EDGE))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return buf.getvalue()


def _grab_primary_frame(sct: Any) -> bytes:
    """One frame of the PRIMARY display, JPEG-encoded. The monitor geometry
    is re-read on every grab, so a resolution change IS the handler."""
    monitors = sct.monitors
    index = 1 if len(monitors) > 1 else 0   # monitors[1] is the primary display
    shot = sct.grab(monitors[index])
    return _prepare_jpeg(shot.raw, shot.width, shot.height)


def _capture_single_frame() -> bytes:
    """One throwaway capture for a one-time look: its own mss instance,
    released before this returns."""
    import mss

    sct = mss.mss()
    try:
        return _grab_primary_frame(sct)
    finally:
        try:
            sct.close()
        except Exception:                        # noqa: BLE001
            pass


def _frame_part(jpeg: bytes) -> Any:
    """A screen frame as a Gemini Part, accepted by the REST rungs and by
    the ladder's one-shot Live rung alike (_to_live_parts sees inline_data).
    """
    from google.genai import types as gtypes

    return gtypes.Part(inline_data=gtypes.Blob(data=jpeg,
                                               mime_type="image/jpeg"))


# ─── The observer ────────────────────────────────────────────────────────────
class ScreenObserver:
    """At most one continuous observation per app instance, owned here.

    The frame pump runs as a plain task on the conversation loop — outside
    the engine's per-connection TaskGroup, so a session rebuild cannot cancel
    it — and the capture thread lives on this side. Stop either one and the
    other follows.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = INACTIVE
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()

        self._capture_thread: Optional[threading.Thread] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._pump_loop: Optional[asyncio.AbstractEventLoop] = None
        self._frame_queue: Optional[asyncio.Queue] = None
        self._frame_interval = _FRAME_INTERVAL_S

        self._started_at: Optional[float] = None
        self._started_at_wall: Optional[str] = None
        self._frames_sent = 0
        self._frames_dropped = 0
        self._last_frame_at: Optional[float] = None
        self._last_error = ""

        self._session_missing_since: Optional[float] = None
        self._send_failures = 0
        self._capture_failures = 0

        # Test hook: a fake frame source stands in for mss.
        self._frame_source: Optional[Callable[[], Optional[bytes]]] = None

        # The web layer registers one listener so the dashboard indicator
        # always reflects this object's authoritative state.
        self._listener: Optional[Callable[[Dict[str, Any]], None]] = None

    def set_listener(
        self, listener: Optional[Callable[[Dict[str, Any]], None]]
    ) -> None:
        with self._lock:
            self._listener = listener

    def _publish(self) -> None:
        """Notify the listener with a fresh snapshot, outside the lock."""
        with self._lock:
            listener = self._listener
            data = self._snapshot_locked()
        if listener is None:
            return
        try:
            listener(data)
        except Exception:                            # noqa: BLE001
            # A broken browser must never affect the capture path.
            pass

    # ── snapshot ─────────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> Dict[str, Any]:
        state = self._state
        data: Dict[str, Any] = {
            "state": state,
            "observation_active": state == OBSERVING,
            "started_at": self._started_at_wall,
            "frames_streamed": self._frames_sent,
            "frames_dropped": self._frames_dropped,
            "frame_interval_seconds": self._frame_interval,
            "last_error": self._last_error,
        }
        if self._last_frame_at is not None:
            data["last_frame_age_seconds"] = round(
                max(0.0, time.monotonic() - self._last_frame_at), 1)
        engine = _ENGINE.get()
        data["voice_session_connected"] = bool(
            engine is not None and getattr(engine, "session", None))
        return data

    # ── one-time ─────────────────────────────────────────────────────────────

    def one_time(self, question: str) -> Dict[str, Any]:
        """Look once, answer the question, leave nothing running.

        While continuous observation is active the model is ALREADY seeing
        the screen in this session, so this hands the question back with a
        pointer to the live context instead of a second opinion — and never
        opens another session.
        """
        with self._lock:
            state = self._state
        if state == OBSERVING:
            return {
                "result": (
                    "screen observation is active — answer the question from "
                    "the live visual context you are already receiving"),
                "observation_active": True,
                "uses_active_session": True,
                "state": state,
            }
        if state == STARTING:
            return {
                "result": "screen observation is still starting — the live "
                          "stream will carry the screen in a moment",
                "observation_active": False,
                "state": state,
            }
        try:
            if self._frame_source is not None:
                jpeg = self._frame_source()
            else:
                jpeg = _capture_single_frame()
        except Exception as exc:                     # noqa: BLE001
            return {"error": f"screen capture failed: "
                             f"{type(exc).__name__}: {exc}"}
        if not jpeg:
            return {"error": "screen capture produced no image"}

        try:
            reply = _gemini.call([question, _frame_part(jpeg)],
                                 tier=_gemini.SMART)
        except Exception as exc:                     # noqa: BLE001
            return {"error": f"the vision model failed: "
                             f"{type(exc).__name__}: {exc}"}
        text = getattr(reply, "text", None)
        if not text:
            return {
                "error": ("the vision model returned nothing for this screen — "
                          "try again or rephrase the question"),
            }
        return {
            "result": str(text),
            "operation": "one_time",
            "question": question,
        }

    # ── start ────────────────────────────────────────────────────────────────

    def start_continuous(self) -> Dict[str, Any]:
        """Start streaming the screen into the conversation's Live session."""
        with self._lock:
            state = self._state
            if state in (STARTING, OBSERVING):
                return {
                    "result": "observation is already active",
                    "state": state,
                    "started_at": self._started_at_wall,
                    "frames_streamed": self._frames_sent,
                }
            if state == STOPPING:
                return {
                    "error": ("observation is still stopping — try starting "
                              "again in a moment"),
                    "state": state,
                }
            # ERROR residue has already cleaned itself; a fresh start is safe.
            self._state = STARTING
            self._last_error = ""
            self._stop_event = threading.Event()
            self._ready_event.clear()
            self._started_at = None
            self._started_at_wall = None
            self._frames_sent = 0
            self._frames_dropped = 0
            self._session_missing_since = None
            self._send_failures = 0
            self._capture_failures = 0
            self._frame_interval = _FRAME_INTERVAL_S

        self._publish()

        engine = _ENGINE.get()
        loop: Optional[asyncio.AbstractEventLoop] = (
            getattr(engine, "_loop", None) if engine is not None else None)
        if engine is None or loop is None or loop.is_closed():
            with self._lock:
                self._state = INACTIVE
            self._publish()
            return {
                "error": ("the conversation engine is not running, so there "
                          "is no Live session to stream the screen into"),
                "state": INACTIVE,
            }

        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXLEN)
        with self._lock:
            self._frame_queue = queue
            self._pump_loop = loop

        capture = threading.Thread(target=self._capture_loop, daemon=True,
                                   name="aze-screen-capture")
        with self._lock:
            self._capture_thread = capture
        capture.start()

        # The pump is a plain task on the conversation loop, deliberately
        # OUTSIDE the engine's per-connection TaskGroup: it must survive the
        # session rebuilds the engine performs on its own.
        pump_future = asyncio.run_coroutine_threadsafe(self._pump(), loop)

        # Confirm the stream really came up before claiming success.
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._ready_event.is_set():
                break
            with self._lock:
                state = self._state
                err = self._last_error
            if state == ERROR or (state != STARTING and err):
                # A failure cleaned itself up while we were starting.
                return {"error": err or "screen capture failed to start",
                        "state": self._state}
            if self._stop_event.is_set():
                # A stop landed during startup; stop() is finishing its
                # teardown right now — wait briefly for the final resting
                # state so no caller sees a transient one.
                stop_report = {"result": "observation stopped during startup"}
                limit = _JOIN_TIMEOUT_S + 1.0
                deadline = time.monotonic() + limit
                while time.monotonic() < deadline:
                    with self._lock:
                        state = self._state
                    if state != STOPPING:
                        stop_report["state"] = state
                        return stop_report
                    time.sleep(0.02)
                with self._lock:
                    stop_report["state"] = self._state
                return stop_report
            if pump_future.done():
                self._internal_cleanup()
                try:
                    exc: Any = pump_future.exception(timeout=0)
                except Exception as inner:           # noqa: BLE001
                    exc = inner
                return {"error": f"the screen stream task failed to start: "
                                 f"{exc}",
                        "state": self._state}
            time.sleep(0.05)

        if not self._ready_event.is_set():
            self.stop_continuous(reason="startup timeout")
            return {
                "error": (f"screen capture did not produce a ready frame in "
                          f"{_START_TIMEOUT_S:.0f}s — observation was "
                          f"cleaned up"),
                "state": self.snapshot()["state"],
            }
        if pump_future.done():
            # The pump died after the first frame somehow arrived.
            self._internal_cleanup()
            return {"error": "the screen stream task stopped unexpectedly",
                    "state": self._state}

        with self._lock:
            if self._state == STARTING and not self._stop_event.is_set():
                self._state = OBSERVING
                self._started_at = time.monotonic()
                self._started_at_wall = _now_iso()
                final_state = self._state
            else:
                final_state = self._state
        if final_state != OBSERVING:
            self._publish()
            return {"result": "observation stopped during startup",
                    "state": final_state}

        self._publish()
        print(f"[SCREEN] Continuous observation started ({self._frame_interval}"
              f" fps of the primary display, streaming into the live session).")
        return {
            "result": "screen observation started",
            "state": OBSERVING,
            "started_at": self._started_at_wall,
            "frame_interval_seconds": self._frame_interval,
            "streaming_into": "the active conversation session",
            "voice_session_connected": bool(
                getattr(_ENGINE.get(), "session", None)),
        }

    # ── stop ─────────────────────────────────────────────────────────────────

    def stop_continuous(self, reason: str = "") -> Dict[str, Any]:
        """Stop streaming and release everything the observation owns.

        Idempotent, safe while inactive and safe during startup. With the
        shared-session design the conversation session is NOT ours to close:
        only the capture thread and the frame pump end here, and frames
        already queued are dropped, never sent.
        """
        with self._lock:
            state = self._state
            if state == INACTIVE:
                return {"result": "observation was already inactive",
                        "state": INACTIVE}
            frames = self._frames_sent
            thread = self._capture_thread
            task = self._pump_task
            loop = self._pump_loop
            self._state = STOPPING
            self._stop_event.set()

        self._publish()

        # The pump may be parked in await queue.get(); cancel it on its loop.
        if task is not None and loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:                     # noqa: BLE001
                pass

        stopped = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_TIMEOUT_S)
            stopped = not thread.is_alive()

        with self._lock:
            self._capture_thread = None
            self._pump_task = None
            self._pump_loop = None
            self._frame_queue = None
            self._ready_event.clear()
            self._session_missing_since = None
            self._send_failures = 0
            self._capture_failures = 0
            self._state = INACTIVE if stopped else ERROR
            if not stopped:
                self._last_error = "screen capture did not stop cleanly"

        self._publish()

        if stopped:
            print("[SCREEN] Continuous observation stopped"
                  + (f" ({reason})." if reason else "."))
            return {
                "result": "screen observation stopped",
                "state": INACTIVE,
                "reason": reason,
                "frames_streamed": frames,
            }
        return {"error": self._last_error, "state": ERROR}

    # ── status ───────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        snap = self.snapshot()
        state = snap["state"]
        snap["result"] = {
            INACTIVE: "not watching the screen",
            STARTING: "still starting",
            OBSERVING: "watching the screen",
            STOPPING: "stopping",
            ERROR: "an error occurred and has been cleaned up",
        }.get(state, state)
        if state == ERROR and snap["last_error"]:
            snap["result"] = (f"not watching the screen — an earlier "
                              f"observation failed: {snap['last_error']}")
        return snap

    # ── capture thread ───────────────────────────────────────────────────────

    def _capture_loop(self) -> None:
        """The producer: one grab per interval, newest frame wins.

        Runs on a daemon thread; the pump consumes and sends. Stopping sets
        the stop event, which this loop honours within one interval — no
        busy-wait, no unbounded queue.
        """
        interval = self._frame_interval
        source = self._frame_source
        sct: Any = None
        try:
            try:
                if source is None:
                    import mss
                    sct = mss.mss()
            except Exception as exc:                 # noqa: BLE001
                print(f"[SCREEN] capture could not start: {exc}")
                traceback.print_exc()
                self._fatal(f"screen capture could not start: "
                            f"{type(exc).__name__}: {exc}")
                return

            while not self._stop_event.is_set():
                started = time.monotonic()
                frame = None
                try:
                    if source is not None:
                        frame = source()
                    else:
                        frame = _grab_primary_frame(sct)
                    self._capture_failures = 0
                except Exception as exc:             # noqa: BLE001
                    self._capture_failures += 1
                    if self._capture_failures >= _CAPTURE_FAILURE_LIMIT:
                        self._fatal(f"screen capture keeps failing: "
                                    f"{type(exc).__name__}: {exc}")
                        return
                    print(f"[SCREEN] capture failed "
                          f"({self._capture_failures}/{_CAPTURE_FAILURE_LIMIT})"
                          f": {exc}")
                    frame = None
                if frame:
                    self._enqueue_frame(frame)
                # Cancellable pacing: the wait ends early on stop.
                remaining = interval - (time.monotonic() - started)
                if remaining > 0 and self._stop_event.wait(remaining):
                    break
        except Exception as exc:                     # noqa: BLE001
            # Whatever escaped, the state machine must not be left mid-air.
            self._fatal(f"screen capture crashed: "
                        f"{type(exc).__name__}: {exc}")
        finally:
            if sct is not None:
                try:
                    sct.close()
                except Exception:                    # noqa: BLE001
                    pass

    def _enqueue_frame(self, frame: bytes) -> None:
        """Hand one frame to the pump: newest wins, queue stays bounded.

        ``call_soon_threadsafe`` runs the put ON the conversation loop, so
        the queue — like every other asyncio structure here — is touched
        from its own loop only. A full queue drops the oldest frame: recent
        visuals outrank stale ones.
        """
        loop = self._pump_loop
        queue = self._frame_queue
        if loop is None or queue is None or loop.is_closed():
            return
        payload = frame

        def put() -> None:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()               # drop the stale head
                    queue.put_nowait(payload)
                except Exception:                    # noqa: BLE001
                    pass
            else:
                self._ready_event.set()

        try:
            loop.call_soon_threadsafe(put)
        except RuntimeError:                         # noqa: BLE001
            return  # the loop shut down under us; stop will follow

    # ── pump (runs on the conversation loop) ────────────────────────────────

    async def _pump(self) -> None:
        """Send queued frames into the conversation's CURRENT Live session.

        The session is read freshly per frame, so the engine's own reconnect
        cycle simply continues the stream into the next session. Frames go
        through ``send_realtime_input(video=...)`` — input tokens that never
        force a turn, never an independent analysis request. After a stop is
        set, no further frame is ever sent.
        """
        self._pump_task = asyncio.current_task()
        try:
            while not self._stop_event.is_set():
                queue = self._frame_queue
                if queue is None:
                    return
                frame = await queue.get()
                # A stop may have landed while the frame sat queued.
                if self._stop_event.is_set() or self._frame_queue is None:
                    return
                engine = _ENGINE.get()
                session = getattr(engine, "session", None)
                now = time.monotonic()
                if session is None:
                    # The voice session is rebuilding; frames wait for it,
                    # bounded — the engine reconnects within seconds normally.
                    if self._session_missing_since is None:
                        self._session_missing_since = now
                    elif (now - self._session_missing_since
                            >= _SESSION_WAIT_LIMIT_S):
                        self._fatal("the conversation session has been "
                                    "unavailable for too long")
                        return
                    continue
                self._session_missing_since = None
                try:
                    from google.genai import types as gtypes
                    await session.send_realtime_input(
                        video=gtypes.Blob(data=frame, mime_type="image/jpeg"))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:             # noqa: BLE001
                    self._send_failures += 1
                    if self._send_failures >= _SEND_FAILURE_LIMIT:
                        self._fatal(f"frames persistently rejected: "
                                    f"{type(exc).__name__}: {exc}")
                        return
                    print(f"[SCREEN] send failed ({self._send_failures}"
                          f"/{_SEND_FAILURE_LIMIT}): {exc}")
                else:
                    self._send_failures = 0
                    self._frames_sent += 1
                    self._last_frame_at = now
                    if self._started_at is None:
                        self._started_at = now
        except asyncio.CancelledError:
            raise
        finally:
            self._pump_task = None

    # ── failure plumbing ─────────────────────────────────────────────────────

    def _fatal(self, reason: str) -> None:
        """A fatal stream error: mark it, then clean up off the hot path."""
        with self._lock:
            if self._state in (STOPPING, INACTIVE, ERROR):
                return  # a stop or an earlier failure owns the teardown
            self._last_error = reason
            self._state = ERROR
            self._stop_event.set()
        self._publish()
        print(f"[SCREEN] {reason}")
        threading.Thread(target=self._cleanup_after_fatal, daemon=True,
                         name="aze-screen-cleanup").start()

    def _cleanup_after_fatal(self) -> None:
        self._capture_join()
        self._cancel_pump()
        with self._lock:
            self._capture_thread = None
            self._pump_task = None
            self._pump_loop = None
            self._frame_queue = None
            self._ready_event.clear()
            self._session_missing_since = None
            self._send_failures = 0
            self._capture_failures = 0
            # A safe resting state, with the error on record for the user.
            self._state = INACTIVE
        self._publish()

    def _internal_cleanup(self) -> None:
        """Bounded cleanup used when startup itself fails."""
        self._capture_join()
        self._cancel_pump()
        with self._lock:
            self._capture_thread = None
            self._pump_task = None
            self._pump_loop = None
            self._frame_queue = None
            self._ready_event.clear()
            self._state = INACTIVE
        self._publish()

    def _capture_join(self) -> None:
        thread = self._capture_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_TIMEOUT_S)

    def _cancel_pump(self) -> None:
        task, loop = self._pump_task, self._pump_loop
        if task is not None and loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:                     # noqa: BLE001
                pass


# ─── Module singleton ────────────────────────────────────────────────────────
_OBSERVER = ScreenObserver()


# ─── Engine registration (called by main.JarvisLive) ────────────────────────
def set_engine(engine: Any) -> None:
    """Register the running conversation engine. ``main.py`` calls this once
    per engine instance; a replaced engine simply takes over."""
    _ENGINE.set(engine)
    print("[SCREEN] Engine registered — screen frames will stream into the "
          "conversation session.")


def shutdown_observation(reason: str = "") -> Dict[str, Any]:
    """Stop any continuous observation. Called on app shutdown so no capture
    thread, pump task or queued frame outlives the application."""
    return _OBSERVER.stop_continuous(reason or "shutdown")


def observation_snapshot() -> Dict[str, Any]:
    """Authoritative lifecycle snapshot for the dashboard indicator."""
    return _OBSERVER.snapshot()


def set_observation_listener(
    listener: Optional[Callable[[Dict[str, Any]], None]]
) -> None:
    """Register the single UI listener for observation state changes."""
    _OBSERVER.set_listener(listener)


# ─── Registered entry point ──────────────────────────────────────────────────
def screen_understanding(operation: str, question: Optional[str] = None):
    """The one registered action: four operations, one tool.

    Runs in the tool-executor worker thread (tool_calling convention), which
    is why everything here marshals onto the conversation loop explicitly.
    """
    op = str(operation or "").strip()
    if op not in OPERATIONS:
        return {"error": f"unknown operation '{op}' — expected one of: "
                         + ", ".join(OPERATIONS)}
    if op == "one_time":
        if not isinstance(question, str) or not question.strip():
            return {"error": "one_time needs the user's question in `question`"}
        try:
            return _OBSERVER.one_time(question.strip())
        except Exception as exc:                     # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
    if op == "start_continuous_observation":
        try:
            return _OBSERVER.start_continuous()
        except Exception as exc:                     # noqa: BLE001
            return {"error": f"observation failed to start: "
                             f"{type(exc).__name__}: {exc}"}
    if op == "stop_continuous_observation":
        try:
            return _OBSERVER.stop_continuous()
        except Exception as exc:                     # noqa: BLE001
            return {"error": f"observation failed to stop: "
                             f"{type(exc).__name__}: {exc}"}
    try:
        return _OBSERVER.status()
    except Exception as exc:                         # noqa: BLE001
        return {"error": f"status failed: {type(exc).__name__}: {exc}"}


def _atexit_cleanup() -> None:
    try:
        shutdown_observation("process exit")
    except Exception:                                # noqa: BLE001
        pass


atexit.register(_atexit_cleanup)

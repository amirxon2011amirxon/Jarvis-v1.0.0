"""Jarvis v1.0.0 — a voice-and-text assistant.

One conversation, two ways in: speak into the microphone, or type in the box.
The reply is spoken and written into the activity log.

The default user interface is the local React application in ``frontend/``,
served by ``web/server.py`` and launched with ``python main.py`` (or the
``jarvis`` command).  It talks to the same ``JarvisLive`` session below; no
conversation or tool logic is duplicated.

What runs it:
  * Gemini Live — audio in, audio out, transcriptions, one session.

This build exposes tools (opening and closing applications, opening websites,
browser tabs, and finding or opening local files and folders) through
actions/tool_calling.py. It has no memory store, dashboard or plugin system
beyond that registry. It can act on the computer only through those tools.
"""
import platform as _platform
import subprocess as _subprocess

# Force CREATE_NO_WINDOW on every subprocess call on Windows. Tool discovery
# shells out to native Windows APIs, and a console window flashing on top of the
# HUD is not acceptable.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen


# Console must survive non-UTF-8 code pages; status lines carry emoji.
import sys as _sys

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


import asyncio
import hashlib
import os
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import numpy as np
import sounddevice as sd
from google import genai
from google.genai import types

from config import (
    get_assistant_name, get_gemini_key, get_input_device, get_memory_settings,
    get_output_device, get_proactive_audio_enabled, get_push_to_talk_enabled,
    get_thinking_enabled, get_turn_tuning, get_user_name, get_voice,
)
from core import gemini as _gemini
from core import agent_run as _agent_run
from core import approvals as _approvals
from core import notification_service as _notifications
from core import audio_devices
from core import telephony as _telephony
from core.echo import EchoGuard

from actions import (get_tool_module, research_tool,
                     screen_understanding_tool, tool_calling)
from Skills.Engine import get_engine as _get_skills_engine

# Compatibility re-export. Tool code and category folders can be renamed; the
# stable tool name is what survives.
_open_app_tool = get_tool_module("open_app")
if _open_app_tool is None:
    raise ImportError("The open_app tool module was not found in actions/.")
open_app = _open_app_tool.open_app  # noqa: F401  (compatibility)


# Tool declarations are rebuilt for every Live connection in ``_build_config``
# from actions/tool_calling.py. That makes a folder/tool selection made in the
# ACTIONS bay take effect on the reconnection it triggers, without a second
# registry or a stale import-time snapshot.


def get_base_dir():
    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()
PROMPT_PATH = BASE_DIR / "core" / "prompt.txt"

# The conversation's model. A NAME, not a decision: the ladder lives in
# core/gemini.py and this is whichever rung is currently in use.
LIVE_MODEL = "models/gemini-3.1-flash-live-preview"
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 1024

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL = 2600.0


# ── Optional audio-path tracer ──────────────────────────────────────────────
# Set AZE_AUDIO_TRACE=<file> to record receive/playback timing. Off by default
# and zero-cost when off; it is how the jitter-buffer behaviour was measured.
_TRACE_PATH = os.environ.get("AZE_AUDIO_TRACE")
_TRACE_LOCK = threading.Lock()


def _trace(kind: str, **fields) -> None:
    if not _TRACE_PATH:
        return
    line = f"{time.monotonic():.6f} {kind} " + " ".join(
        f"{k}={v}" for k, v in fields.items())
    with _TRACE_LOCK:
        with open(_TRACE_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")



def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


# Extra time beyond the device's reported output latency before the microphone
# is trusted again: covers room decay and the speaker's own settling.
_TAIL_MARGIN = 0.25

# Barge-in keeps this much microphone audio from *before* the detector fires,
# so the user's first syllable survives. 20 blocks at 16 kHz / 1024 frames is
# just under 1.3 seconds, comfortably longer than the echo guard's longest
# corroboration window.
_BARGE_IN_PREROLL_BLOCKS = 20

# A STOP is aimed at the reply that was in flight. If no `turn_complete` ever
# arrives (for example the click landed after the turn had already ended), the
# next user turn must be allowed to speak normally. A second of grace keeps a
# late input-transcription chunk from the interrupted turn from clearing it.
_INTERRUPT_RESUME_AFTER = 1.0

# How long a sent turn may go unanswered before the session is presumed dead.
# This is a safety net, not a delay: opening an app returns in well under a
# second. 45 s is long enough for a slow model turn and short enough that a
# genuinely dead socket is rebuilt promptly.
_STALL_LIMIT = 45.0
# A Live connection that has not opened by now is treated as a failed attempt
# (DNS or TCP stall) so the run loop can retry quickly instead of hanging.
_CONNECT_TIMEOUT = 10.0

# Playback finishing rules. A reply's audio has drained, so playback is over
# after a short grace once the server said the turn was complete; with no
# completion signal at all — late audio after `turn_complete`, a completion
# dropped by a reconnect — the longer idle bound stops it anyway, so the HUD
# can never stay SPEAKING while nothing is playing.
_TURN_DONE_GRACE = 0.25
_SPEAK_IDLE = 2.5

# ── Playback jitter buffer ───────────────────────────────────────────────────
# The Live stream arrives in bursts with real holes between them (measured:
# 200-600 ms, dozens per reply). Writing every chunk the instant it arrives
# fills only one burst's worth of device buffer, so the next hole is audible.
# Hold writes until this much audio is queued/playing ahead of the speaker;
# bursts then accumulate a reserve that holes cannot drain. Bounded, so a
# genuinely slow stream still plays rather than waiting forever.
_RESERVE_S = 0.65              # target backlog: device buffer + queued audio
_RESERVE_WAIT_S = 0.8          # longest a reply may be held to refill

# The queue is a latency budget, not a bucket: with no cap the model's fast
# generation piled up 40+ seconds of speech ahead of playback (measured), so
# the voice lagged the transcript and a STOP threw away half a reply. Four
# seconds keeps speech close to real time and lets the receive loop apply
# backpressure to the server instead.
_AUDIO_QUEUE_SECONDS = 4.0
_AUDIO_QUEUE_MAX = int(_AUDIO_QUEUE_SECONDS / 0.05)

# Ask PortAudio for a deeper device buffer than its default (~213 ms measured
# on MME). Measured delivery holes reach 500-600 ms; a ~0.73 s device buffer
# plus the 400 ms prebuffer absorbs them without audible silence. The cost is
# under a second of added output latency, which is the right trade for stable
# speech.
_OUTPUT_LATENCY_S = 0.8

_LEVEL_HOP = 480       # 20 ms of 24 kHz audio per level frame

# Delay from handing the first bytes of a reply to an already-running output
# stream to hearing them.
_FIRST_SOUND = CHUNK_SIZE / RECEIVE_SAMPLE_RATE
_CURSOR_SLACK = 0.15


def _pcm_levels(samples) -> list[float]:
    """Slice a PCM block into loudness frames, one per 20 ms."""
    try:
        x = np.asarray(samples, dtype=np.float32)
    except Exception:
        return []
    if x.size == 0:
        return []
    return [_pcm_level(x[start:start + _LEVEL_HOP])
            for start in range(0, x.size, _LEVEL_HOP)]


def _render_prompt(template: str, values: dict) -> str:
    """Fill {tokens} in the prompt template with a plain replace."""
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _function_response_result(response) -> dict:
    """Extract the JSON-safe result dict from a FunctionResponse."""
    payload = getattr(response, "response", None)
    if payload is None and isinstance(response, dict):
        payload = response.get("response")
    try:
        data = dict(payload) if payload is not None else {}
    except Exception:
        return {}
    result = data.get("result", data)
    if isinstance(result, dict):
        return result
    try:
        return tool_calling._as_plain_dict(result)
    except Exception:
        return {"result": result}


def _agent_result_summary(result: dict) -> str:
    """A short, display-safe summary for run progress events."""
    if not isinstance(result, dict):
        return ""
    for key in ("summary", "message", "error", "result"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    try:
        _ok, summary = tool_calling._event_summary(result)
        if summary:
            return str(summary)[:500]
    except Exception:
        pass
    return ""


def _get_api_key() -> str:
    # One settings reader (``config``) so a malformed file and a patched test
    # path behave the same here as everywhere else.
    try:
        return get_gemini_key() or ""
    except Exception:
        return ""


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are {assistant_name}, a helpful voice assistant. Be concise, "
            "warm and direct. You can control the computer using the available tools."
        )


_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

# Natural call-ending phrases on the phone. These are handled deterministically
# so "bye Jarvis" never becomes a shutdown request and does not depend on the
# model choosing the hangup tool in time.
_CALL_END_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|alright|all\s+right|thanks|thank\s+you|yes|yeah|"
    r"sure|well|so)[,!.?\s-]*)*"
    r"(?:bye(?:[-\s]?bye)?|good[-\s]?bye|hang\s*up|"
    r"end\s+(?:the\s+)?call|stop\s+(?:the\s+)?call|"
    r"talk\s+to\s+you\s+later|talk\s+later|that'?s\s+all(?:\s+for\s+now)?|"
    r"i'?m\s+done|we'?re\s+done|see\s+you(?:\s+later)?|"
    r"catch\s+you\s+later)"
    r"(?:[,!.?\s-]*(?:for\s+now|then|thanks|thank\s+you|bye|now|"
    r"jarvis|aze))?"
    r"[\s.!?]*$",
    re.IGNORECASE,
)

# Transcript chunks shorter than this may legitimately repeat ("evet, evet"),
# so only longer ones are treated as duplicates.
_REPEAT_MIN = 12


def _is_repeat_chunk(txt: str, buf: list) -> bool:
    if len(txt) < _REPEAT_MIN:
        return bool(buf) and txt == buf[-1]
    joined = " ".join(buf)
    return txt in joined


def _clean_transcript_raw(text: str) -> str:
    """Remove control tokens without destroying word-boundary whitespace."""
    text = _CTRL_RE.sub("", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)


def _clean_transcript(text: str) -> str:
    return _clean_transcript_raw(text).strip()


class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean voluntary rebuild."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


def _is_api_key_error(err: str) -> bool:
    """True for the errors that mean "ask the user for a key", not "degrade".

    A bad key is reported by Google as ``400 INVALID_ARGUMENT: API key not
    valid``.  That used to match the generic feature-degradation branches first,
    so the app disabled proactive audio and the link tuning, reconnected twice,
    and only then showed the setup screen.
    """
    low = str(err or "").lower()
    return ("api key not valid" in low
            or "api_key_invalid" in low
            or "invalid api key" in low
            or "api key expired" in low
            or ("api key" in low
                and ("permission" in low or "not authorized" in low))
            or "1007" in low)


def _network_backoff(current) -> int:
    """Next retry delay after a connection failure, with a real floor.

    A voluntary reconnect sets the stored delay to 0 for an immediate rebuild.
    If that rebuild then fails with a network error, multiplying 0 by 2 used to
    leave it at 0 and retry in a tight loop.  The cap is deliberately short:
    DNS blips clear in seconds, and waiting a minute to notice that is exactly
    the "stuck on connecting" feeling this backoff exists to avoid.
    """
    try:
        value = int(current or 0)
    except (TypeError, ValueError):
        value = 0
    return min(max(1, value * 2), 5)


class JarvisLive:
    def __init__(self, ui):
        self.ui = ui
        self._asst_name = "Jarvis"
        self.model = LIVE_MODEL
        self.provider = "Google Gemini"
        self.session = None
        self.audio_in_queue = None
        self.out_queue = None
        self._loop = None
        self._is_speaking = False
        self._speaking_lock = threading.Lock()
        self._interrupted = False
        self._interrupted_at = 0.0
        self._interrupted_source = ""

        # Deadline for "we sent a turn and the server has said nothing since".
        self._rx_deadline = None

        # Last time reply audio was handed to the player. Playback stops on
        # this clock when a turn-complete signal never arrives.
        self._last_audio_at = time.monotonic()
        # True when no speaker stream could be opened. The receive loop then
        # drops reply audio instead of filling a queue nobody drains, so tool
        # calls, transcripts and turn-complete signals still flow.
        self._output_unavailable = False

        # Track if a tool call is in progress (server waiting for tool result).
        self._tool_in_progress = False
        self._tool_chain = None
        self._tool_tasks: set = set()
        self._tool_pending = 0
        self._session_generation = 0

        self._last_out_logged = ""

        # Last connection failure, shown by the initialisation gate while the
        # run loop retries. Cleared as soon as a session connects.
        self.last_connection_error = ""

        # Push-to-talk
        self._ptt_enabled = False
        self._ptt_held = False
        self._ptt = None
        self._out_level = 0.0
        self._echo = EchoGuard()
        self._out_latency = 0.20
        self._tail_until = 0.0
        self._play_cursor = 0.0

        # Full-duplex barge-in. The callback fills _pre_roll while the reply is
        # playing; when the echo guard recognises the user, the loop drains the
        # queued reply, aborts the device buffer and streams this pre-roll to
        # Gemini so the first syllable is not lost.
        self._pre_roll: list[bytes] = []
        self._pre_roll_lock = threading.Lock()
        self._barge_in_event = threading.Event()
        self._barge_in_active = False
        self._out_stream = None
        self._output_aborted = False
        self._barge_in_count = 0

        # Estimated audio sitting in the device's own buffer (seconds), used
        # by the playback jitter reserve. Maintained only by _play_audio.
        self._dev_buffer = 0.0
        self._dev_last = 0.0

        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True
        self._reconnect_reason = ""
        self._shutdown = False

        # Session resumption handle. RAM only, never written to disk.
        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._last_user_speech = time.monotonic()
        self._enhanced_live = True
        self._tuned_live = True
        self._conn_backoff = 1

        # Long-term memory integration. The conversation id groups extracted
        # turns; the flag stops a voice turn from injecting context twice.
        self._conversation_id = uuid.uuid4().hex
        self._memory_turn_injected = False
        self._memory_error_logged = False
        self._task_context_turn_injected = False

        # Telephone bridge. The SIP engine is independent of the Live session;
        # this side only exchanges PCM with it. Caller audio arrives at 16 kHz
        # (Gemini input rate) and reply audio leaves at 24 kHz (Gemini output
        # rate), so no STT/TTS implementation is duplicated. The pre-roll keeps
        # the syllable that triggered a barge-in.
        self._telephony = _telephony.get_service()
        self._phone_pre_roll: list[bytes] = []
        self._phone_pre_roll_lock = threading.Lock()
        self._phone_trace = (
            os.environ.get("JARVIS_PHONE_TRACE") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        self._phone_rx_blocks = 0
        self._phone_rx_forwarded = 0
        self._phone_rx_suppressed = 0
        self._phone_tx_batches = 0
        self._phone_tx_bytes = 0
        self._phone_tx_dropped = 0
        self._phone_barge_ins = 0
        self._phone_ignored_noise = 0
        self._phone_speech_run = 0
        # A telephone line carries handset noise, breathing and codec artefacts.
        # The phone path therefore needs a little more corroboration than the
        # local microphone before it treats a sound as a real interruption.
        self._phone_barge_level = 0.10
        self._phone_barge_blocks = 8
        self._phone_speech_grace_until = 0.0
        self._phone_tail_until = 0.0
        # Separate echo guard: the phone's earpiece is picked up by the
        # caller's microphone, and feeding that echo back to Gemini made the
        # server interrupt long replies. The phone path never shares state
        # with the local microphone guard.
        self._phone_echo = EchoGuard()
        try:
            self._telephony.configure(audio_bridge=self._on_phone_audio)
        except Exception as exc:  # noqa: BLE001 - phone is optional
            print(f"[JARVIS] Telephony bridge unavailable: {exc}")

        self.ui.on_push_to_talk = self.set_push_to_talk
        self.ui.ptt_hold = self._on_ptt
        self.ui.on_text_command = self._on_text_command
        self.ui.on_interrupt = self.interrupt
        self.ui.on_voice_change = self._on_voice_change
        self.ui.on_audio_device_change = self._on_audio_device_change

        if get_push_to_talk_enabled():
            try:
                self.set_push_to_talk(True)
            except Exception as e:
                print(f"[JARVIS] Push-to-talk unavailable: {e}")

        # Objective-scoped task tracking. The Live session is still the only
        # planner/executor; this service only records a run that Gemini starts
        # explicitly through the task_status tool, and fans its snapshots to the
        # existing WebSocket event path.
        self._agent_runs = _agent_run.get_service()
        self._agent_terminal_until_user = False
        self._last_agent_context_key = None
        try:
            self._agent_runs.set_listener(self._on_agent_run_event)
            self._agent_runs.set_conversation_id(self._conversation_id)
        except Exception as exc:  # noqa: BLE001 - tracking never blocks voice
            print(f"[JARVIS] Agent run tracking unavailable: {exc}")

        # Server-side action approvals. The tool block is the only path that
        # can consume one; this listener only publishes UI state and tells the
        # model about a decision so it can retry the exact call.
        self._approvals = _approvals.get_service()
        try:
            self._approvals.set_listener(self._on_approval_event)
            self._approvals.set_session(self._conversation_id)
        except Exception as exc:  # noqa: BLE001 - approvals must fail closed
            print(f"[JARVIS] Approval tracking unavailable: {exc}")

    # ── Stall watchdog ───────────────────────────────────────────────────────

    def _arm_stall(self) -> None:
        if self._rx_deadline is None:
            self._rx_deadline = time.monotonic() + _STALL_LIMIT

    def _clear_stall(self) -> None:
        self._rx_deadline = None

    async def _watch_stall(self) -> None:
        while True:
            await asyncio.sleep(2)
            self._check_agent_duration()
            deadline = self._rx_deadline
            if deadline is None or time.monotonic() < deadline:
                continue
            # Don't stall if a tool call is in progress — the server is waiting for us.
            if self._tool_in_progress:
                continue
            self._clear_stall()
            print(f"[JARVIS] No response for {_STALL_LIMIT:.0f}s — rebuilding the session.")
            self.ui.write_log("SYS: The model stopped answering — reconnecting.")
            self.request_reconnect(keep_context=True, reason="no response")
            return

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        loop = getattr(self, "_loop", None)
        ev = self._reconnect_event
        self._reconnect_keep = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        # The voice is fixed at connect time; a rebuild is required. The
        # resumption handle is dropped because resuming restores server-side
        # session state, which may include the old voice. Drop it immediately:
        # if the link is currently reconnecting there is no session to request
        # a rebuild from, but the next connection must still start fresh.
        self._resume_handle = None
        previous_conversation = self._conversation_id
        self._conversation_id = uuid.uuid4().hex
        self._cancel_agent_run("conversation changed")
        approvals = getattr(self, "_approvals", None)
        if approvals is not None:
            try:
                approvals.set_session(self._conversation_id)
            except Exception:
                pass
        service = getattr(self, "_agent_runs", None)
        if service is not None:
            try:
                service.set_conversation_id(self._conversation_id)
            except Exception:
                pass
        self._reset_skill_scope(previous_conversation)
        clear = getattr(self.ui, "clear_conversation", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                pass
        self.request_reconnect(keep_context=False, reason="new voice")

    def _reset_skill_scope(self, previous_conversation: str = "") -> None:
        """Clear task-scoped Skill guidance at a conversation boundary."""
        try:
            engine = _get_skills_engine()
            if previous_conversation:
                engine.clear_scope(previous_conversation)
            engine.set_active_scope(self._conversation_id)
        except Exception as exc:  # noqa: BLE001 - Skills never block the chat
            print(f"[JARVIS] Skill context reset failed: {exc}")

    def _on_audio_device_change(self):
        self.request_reconnect(keep_context=True, reason="audio device")

    async def _watch_reconnect(self):
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep = self._reconnect_keep
        reason = self._reconnect_reason or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def _on_text_command(self, text: str):
        self.send_text(text)

    def _notify_assistant_delta(self, text: str) -> None:
        """Feed a transcription chunk to a UI that supports incremental text."""
        callback = getattr(self.ui, "stream_assistant_delta", None)
        if callable(callback):
            try:
                callback(text)
            except Exception:
                pass

    def _notify_turn_complete(self, suppress: bool = False) -> None:
        callback = getattr(self.ui, "turn_complete", None)
        if callable(callback):
            try:
                callback(suppress=suppress)
            except TypeError:
                # Desktop UI does not implement the optional flag.
                try:
                    callback()
                except Exception:
                    pass
            except Exception:
                pass

    # ── Objective-scoped task tracking ───────────────────────────────────────

    def _on_agent_run_event(self, event: dict) -> None:
        """Forward one run snapshot to the existing UI event surface."""
        run = event.get("run") if isinstance(event, dict) else None
        if isinstance(run, dict):
            if event.get("event") == "started":
                self._agent_terminal_until_user = False
            elif run.get("status") in _agent_run.TERMINAL_STATUSES:
                # A finished/cancelled/failed run must not start another tool
                # batch until the user gives a new instruction.
                self._agent_terminal_until_user = True
        callback = getattr(self.ui, "agent_event", None)
        if callable(callback):
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001 - UI must never break a run
                print(f"[JARVIS] Agent run event failed: {exc}")
        if isinstance(run, dict):
            self._notify_agent_run_event(event, run)

    def _notify_agent_run_event(self, event: dict, run: dict) -> None:
        """Create one built-in notification for meaningful run transitions."""
        event_name = str((event or {}).get("event") or "")
        status = str(run.get("status") or "")
        current_step = str(run.get("current_step") or "")
        notify = event_name in (
            "completed", "failed", "needs_user", "verification_exhausted",
            "limit",
        ) or (event_name == "status" and status == _agent_run.BLOCKED)
        if not notify:
            return
        run_id = str(run.get("run_id") or "")
        if not run_id:
            return
        if event_name == "completed" or status == _agent_run.COMPLETED:
            title = "Task completed"
            priority = "normal"
            body = str(run.get("result_summary") or "The objective completed.")
        elif event_name == "failed" or status == _agent_run.FAILED:
            title = "Task failed"
            priority = "high"
            body = str(run.get("last_error") or run.get("result_summary")
                       or "The objective failed.")
        elif status == _agent_run.BLOCKED or event_name in (
                "verification_exhausted", "limit"):
            title = "Task blocked"
            priority = "high"
            body = str(run.get("last_error") or current_step
                       or "The task needs attention.")
        else:
            title = "Task needs your attention"
            priority = "high"
            body = str(run.get("last_tool_summary") or current_step
                       or "A task is waiting for your input.")
        objective = str(run.get("objective") or "").strip()
        if objective and objective not in body:
            body = f"{objective} — {body}"
        dedupe = (f"agent_run:{run_id}:{event_name}:{status}:"
                  f"{current_step[:80]}")
        try:
            _notifications.get_service().create(
                title=title,
                body=body,
                kind="agent",
                priority=priority,
                source="agent_run",
                dedupe_key=dedupe,
                metadata={"run_id": run_id, "status": status,
                          "event": event_name},
            )
        except Exception as exc:  # noqa: BLE001 - notifications are best effort
            print(f"[JARVIS] Agent run notification failed: {exc}")

    def _on_approval_event(self, event: dict) -> None:
        """Publish approval state and notify the model after a UI decision."""
        raw = event if isinstance(event, dict) else {}
        callback = getattr(self.ui, "approval_event", None)
        if callable(callback):
            try:
                callback(raw)
            except Exception as exc:  # noqa: BLE001 - UI must never break a run
                print(f"[JARVIS] Approval event failed: {exc}")
        approval = raw.get("approval")
        status = str(raw.get("event") or "")
        if isinstance(approval, dict) and status in (
                "approved", "rejected", "expired", "revoked"):
            approval_id = str(approval.get("approval_id") or "")
            if status == "approved":
                if self._resume_approved_operation(approval):
                    return
            else:
                try:
                    tool_calling.discard_pending_approval(approval_id)
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass
            self._notify_approval_state(status, approval)

    def _resume_approved_operation(self, approval: dict) -> bool:
        """Execute the exact blocked call once, without asking the model to
        reconstruct it. Returns True when resume was scheduled."""
        approval_id = str((approval or {}).get("approval_id") or "")
        if not approval_id:
            return False
        try:
            entry = tool_calling.pending_approval(approval_id)
        except Exception:  # noqa: BLE001
            entry = None
        if not entry:
            # No remembered call (old request or a process restart). Fall back
            # to the honest model retry notice; do not claim execution.
            return False
        run_id = str(entry.get("run_id") or "")
        if run_id:
            service = getattr(self, "_agent_runs", None)
            run = service.current() if service is not None else None
            if (run is None or run.run_id != run_id
                    or run.status in _agent_run.TERMINAL_STATUSES):
                tool_calling.discard_pending_approval(approval_id)
                return False
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            # The operation must still run if the Live link is down; the
            # result is logged and will be visible on the next turn.
            threading.Thread(
                target=lambda: asyncio.run(
                    self._execute_approved_operation(approval_id, approval)),
                daemon=True, name="jarvis-approved-operation").start()
            return True
        try:
            asyncio.run_coroutine_threadsafe(
                self._execute_approved_operation(approval_id, approval), loop)
        except Exception as exc:  # noqa: BLE001 - leave retry notice available
            print(f"[JARVIS] Approved operation scheduling failed: {exc}")
            return False
        return True

    async def _execute_approved_operation(self, approval_id: str,
                                          approval: dict) -> None:
        """Run one approved call and return its real result to Gemini."""
        try:
            entry = tool_calling.pending_approval(approval_id)
        except Exception:  # noqa: BLE001
            entry = None
        run_id = str((entry or {}).get("run_id") or "")
        call, result = await asyncio.to_thread(
            tool_calling.resume_pending_approval,
            approval_id,
            self.ui.write_log,
            self.ui.tool_event,
        )
        if call is None or result is None:
            self._notify_approval_state("approved", approval)
            return
        try:
            if not (result.get("success", True)
                    and result.get("ok", True)) or result.get("error"):
                self.ui.write_log(
                    "ERR: Approved operation failed — "
                    + str(result.get("error") or result.get("message")
                          or "unknown error")[:500])
        except Exception:  # noqa: BLE001 - logging is best effort
            pass
        service = getattr(self, "_agent_runs", None)
        if run_id and service is not None:
            run = service.current()
            if (run is not None and run.run_id == run_id
                    and run.status not in _agent_run.TERMINAL_STATUSES):
                try:
                    if run.status == _agent_run.NEEDS_USER:
                        service.update(
                            run_id=run_id, status=_agent_run.IN_PROGRESS,
                            step="Approved operation completed",
                            summary=str(result.get("message")
                                        or result.get("error") or "")[:500])
                    response = tool_calling.make_function_response(call, result)
                    self._record_agent_result(run_id, call, response)
                except Exception as exc:  # noqa: BLE001 - report tool state
                    print(f"[JARVIS] Approved run update failed: {exc}")
        session = self.session
        if session is None:
            return
        try:
            import json as _json

            rendered = _json.dumps(result, ensure_ascii=False, default=str,
                                   sort_keys=True)[:6000]
        except Exception:  # noqa: BLE001
            rendered = str(result)[:6000]
        text = (
            "APPROVED OPERATION RESULT (already executed; do not run it "
            f"again): {rendered}")
        try:
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True,
            )
        except Exception as exc:  # noqa: BLE001 - never break the session
            print(f"[JARVIS] Approved operation result delivery failed: {exc}")

    def _notify_approval_state(self, status: str,
                               approval: dict) -> None:
        """Tell Gemini the decision so it can retry or stop.

        No approval id or credential is sent. The exact operation is still
        authorized only by the server-side record.
        """
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running() or self.session is None:
            return
        summary = str(approval.get("summary") or "").strip()
        target = str(approval.get("target") or "").strip()
        label = summary or target or "the requested operation"
        if status == "approved":
            text = (
                "AUTHORIZATION UPDATE: the user approved in the Jarvis "
                f"interface: {label}. Reissue the exact same tool call now "
                "with identical arguments. Do not ask for confirmation again "
                "and do not change the target.")
        elif status == "rejected":
            text = (
                "AUTHORIZATION UPDATE: the user rejected in the Jarvis "
                f"interface: {label}. Do not retry that operation. Acknowledge "
                "the rejection briefly.")
        else:
            text = (
                "AUTHORIZATION UPDATE: the approval expired for: "
                f"{label}. Nothing ran. If the user still wants it, request "
                "approval again.")
        def schedule() -> None:
            async def send() -> None:
                session = self.session
                if session is None:
                    return
                try:
                    await session.send_client_content(
                        turns={"role": "user",
                               "parts": [{"text": text}]},
                        turn_complete=True,
                    )
                except Exception as exc:  # noqa: BLE001 - never break a turn
                    print(f"[JARVIS] Approval notice failed: {exc}")
            try:
                asyncio.create_task(send())
            except Exception as exc:  # noqa: BLE001
                print(f"[JARVIS] Approval notice scheduling failed: {exc}")
        try:
            loop.call_soon_threadsafe(schedule)
        except Exception as exc:  # noqa: BLE001 - loop may be closing
            print(f"[JARVIS] Approval notice skipped: {exc}")

    def _cancel_agent_run(self, reason: str, *, lock: bool = False) -> None:
        """Cancel the active run, optionally locking tool dispatch.

        ``lock`` is used by explicit STOP/shutdown, which must suppress new
        task starts even when no run exists yet: a late tool batch from the
        same model turn must not slip in between the stop and the next user
        instruction.
        """
        service = getattr(self, "_agent_runs", None)
        if service is None:
            if lock:
                self._agent_terminal_until_user = True
            self._cancel_research(reason)
            self._stop_observation(reason)
            return
        try:
            cancelled = service.cancel(reason=reason)
            if lock or cancelled is not None:
                self._agent_terminal_until_user = True
        except Exception as exc:  # noqa: BLE001 - cancellation is best effort
            print(f"[JARVIS] Agent run cancellation failed: {exc}")
        self._cancel_research(reason)
        self._stop_observation(reason)

    def _stop_observation(self, reason: str) -> None:
        """Stop continuous observation and publish its inactive state.

        Observation is session-owned: STOP, a conversation change and app
        shutdown must also drop the capture thread and frame pump, so the
        dashboard can never keep showing a stale active indicator.
        """
        try:
            screen_understanding_tool.shutdown_observation(reason)
        except Exception as exc:  # noqa: BLE001 - cancellation is best effort
            print(f"[JARVIS] Screen observation stop failed: {exc}")

    def _cancel_research(self, reason: str) -> None:
        """Cooperatively cancel the active research job, if any.

        Research runs inside a synchronous worker. The service event stops
        scheduling new searches/fetches between stages; an HTTP request that
        is already in flight still ends on its existing socket timeout.
        """
        try:
            service = research_tool.get_service()
            service.cancel(reason=reason)
        except Exception as exc:  # noqa: BLE001 - cancellation is best effort
            print(f"[JARVIS] Research cancellation failed: {exc}")

    def _prepare_agent_dispatch(self, calls: list):
        """Admit a tool-call message before any tool starts.

        Returns ``(run_id, blocked)``:
        * ``(None, False)`` — ordinary conversation, or a finished run whose
          user has already started a new turn; dispatch normally.
        * ``(run_id, False)`` — an active run owns this dispatch; the run
          already passed its step, tool-call and duration checks.
        * ``(run_id_or_none, True)`` — the run is terminal/cancelled or a
          limit was hit; reply with errors instead of executing.
        """
        service = getattr(self, "_agent_runs", None)
        if service is None:
            return None, False
        run = service.current()
        if run is None:
            # Explicit STOP/shutdown locks dispatch even with no run object,
            # until the next user turn clears the flag.
            return None, self._agent_terminal_until_user
        if run.status in _agent_run.TERMINAL_STATUSES:
            if self._agent_terminal_until_user:
                return run.run_id, True
            # A new user turn has begun; tools now belong to ordinary
            # conversation, not to the finished run.
            return None, False
        blocked_calls = [
            call for call in calls
            if service.call_guard(run.run_id, call)
        ]
        if blocked_calls and len(blocked_calls) == len(calls):
            return run.run_id, True
        # Only calls that will actually execute count against the tool-call
        # budget; a duplicate/failed call refused by the guard must not make a
        # valid mixed batch look oversized.
        dispatchable = [
            call for call in calls
            if not service.call_guard(run.run_id, call)
        ]
        names = ", ".join(
            str(getattr(call, "name", "") or "tool") for call in dispatchable)
        admitted = service.begin_execution(
            run.run_id,
            step=f"Running {names}" if names else "Working",
            tool_call_count=len(dispatchable),
        )
        return run.run_id, not admitted

    def _agent_can_continue(self, run_id: str) -> bool:
        service = getattr(self, "_agent_runs", None)
        if service is None:
            return False
        return service.can_continue(run_id)

    def _agent_call_guard(self, run_id: str, call) -> str:
        service = getattr(self, "_agent_runs", None)
        if service is None:
            return ""
        try:
            return service.call_guard(run_id, call)
        except Exception:
            return ""

    def _blocked_tool_responses(self, calls: list, run_id: str = "") -> list:
        service = getattr(self, "_agent_runs", None)
        run = service.current() if service is not None else None
        status = run.status if run is not None else "unavailable"
        if run_id and run is not None and run.run_id != run_id:
            reason = "the task run was replaced by a newer run"
        elif status == _agent_run.FAILED and run is not None and run.last_error:
            reason = run.last_error
        elif (status == _agent_run.CANCELLED and run is not None
              and run.result_summary):
            reason = run.result_summary
        elif status in _agent_run.TERMINAL_STATUSES:
            reason = f"the task run is already {status}"
        else:
            reason = "the task run is no longer active"
        responses = []
        for call in calls:
            specific = ""
            if (service is not None and run is not None
                    and run.status not in _agent_run.TERMINAL_STATUSES
                    and (not run_id or run.run_id == run_id)):
                try:
                    specific = service.call_guard(run.run_id, call)
                except Exception:
                    specific = ""
            responses.append(tool_calling.blocked_response(
                call, specific or reason))
        return responses

    def _remember_approval_calls(self, calls: list, responses: list,
                                 run_id: str = "") -> None:
        """Keep exact blocked calls so Approve can resume them, not just ask
        the model to reconstruct the request."""
        for call, response in zip(calls, responses):
            result = _function_response_result(response)
            approval_id = str(result.get("_approval_id") or "")
            if not approval_id:
                continue
            try:
                tool_calling.remember_pending_approval(
                    approval_id, call, run_id=run_id,
                    log_callback=self.ui.write_log,
                    event_callback=self.ui.tool_event)
            except Exception as exc:  # noqa: BLE001 - retry notice remains
                print(f"[JARVIS] Pending approval capture failed: {exc}")
            payload = getattr(response, "response", None)
            if isinstance(payload, dict):
                inner = payload.get("result")
                if isinstance(inner, dict):
                    inner.pop("_approval_id", None)

    def _record_agent_result(self, run_id: str, call,
                             response) -> None:
        if not run_id:
            return None
        service = getattr(self, "_agent_runs", None)
        if service is None:
            return None
        run = service.current()
        if run is None or run.run_id != run_id:
            # A stale batch must never update a newer run.
            return None
        if run.status in _agent_run.TERMINAL_STATUSES:
            return None
        result = _function_response_result(response)
        if result.get("recovery_blocked"):
            # A deliberate pre-execution refusal is not a tool failure and must
            # not consume the consecutive-failure budget.
            return None
        summary = _agent_result_summary(result)
        if result.get("approval_required"):
            # The operation did not run. Mark the task as awaiting the user,
            # but do not count a failure or block the exact retry after the
            # UI approves it.
            try:
                service.mark_needs_user(
                    run_id=run.run_id,
                    step=summary or "Waiting for approval",
                    summary=summary)
            except _agent_run.AgentRunError as exc:
                print(f"[JARVIS] Agent run approval state failed: {exc}")
            return None
        name = str(getattr(call, "name", "") or "")
        args = getattr(call, "args", None)
        signature = _agent_run.call_signature(call)
        side_effect = _agent_run.is_side_effect_tool(name, args)
        ok = ("error" not in result
              and bool(result.get("ok", True))
              and bool(result.get("success", True)))
        if result.get("sleep_requested"):
            # Sleep may suspend this process for hours. Avoid counting it as a
            # normal tool result (the duration watchdog would otherwise fail
            # the run on resume). The action is session-ending when accepted.
            try:
                if ok:
                    service.update(
                        run_id=run.run_id, status=_agent_run.BLOCKED,
                        step="Workstation sleep requested",
                        summary="The workstation is going to sleep; the "
                                "session may disconnect.")
                    self._agent_terminal_until_user = True
                else:
                    service.update(
                        run_id=run.run_id, status=_agent_run.IN_PROGRESS,
                        step="Sleep unavailable",
                        summary=summary or "Sleep was not accepted.")
            except _agent_run.AgentRunError as exc:
                print(f"[JARVIS] Agent run sleep state failed: {exc}")
            return None
        if name == "call" and result.get("pending_connection"):
            # A submitted INVITE is not a connected call. Track the state but
            # do not count it as a completed success.
            try:
                service.update(
                    run_id=run.run_id, step="Calling — awaiting SIP answer",
                    summary=summary or str(result.get("result") or "")[:500])
            except _agent_run.AgentRunError as exc:
                print(f"[JARVIS] Agent run call state failed: {exc}")
            return None
        verification_pending = bool(result.get("verification")) and not ok
        if verification_pending:
            try:
                service.record_tool_result(
                    run_id=run.run_id, tool_name=name, ok=True,
                    summary=summary, signature=signature,
                    side_effect=False)
                service.record_pending(
                    run_id=run.run_id, tool_name=name, signature=signature,
                    category="verification_required", summary=summary)
            except _agent_run.AgentRunError as exc:
                print(f"[JARVIS] Agent run verification state failed: {exc}")
            return None
        pending = bool(result.get("confirmation_required")
                       or result.get("clarification_required"))
        failure = None if ok else _agent_run.classify_failure(result, name)
        try:
            if pending:
                service.record_tool_result(
                    run_id=run.run_id, tool_name=name, ok=True,
                    summary=summary, signature=signature,
                    side_effect=False)
                category = (
                    "clarification_required"
                    if result.get("clarification_required")
                    else "confirmation_required")
                failure = _agent_run.classify_failure(result, name)
                service.record_pending(
                    run_id=run.run_id, tool_name=name, signature=signature,
                    category=category, summary=summary)
                service.mark_needs_user(
                    run_id=run.run_id,
                    step=summary or "Waiting for the user",
                    summary=summary)
            else:
                service.record_tool_result(
                    run_id=run.run_id, tool_name=name, ok=ok,
                    summary=summary, failure=failure, signature=signature,
                    side_effect=side_effect)
                if ok and _agent_run.is_observation_tool(name, args, result):
                    service.add_evidence(
                        run_id=run.run_id, summary=summary or f"{name} completed",
                        source="observation", tool=name, kind="observation")
        except _agent_run.AgentRunError as exc:
            print(f"[JARVIS] Agent run tool limit: {exc}")
            return None
        if failure is not None:
            enriched = dict(result)
            enriched["failure_analysis"] = failure
            try:
                return tool_calling.make_function_response(call, enriched)
            except Exception:
                return None
        return None

    def _record_agent_results(self, calls: list, responses: list,
                              run_id: str = "") -> None:
        if not run_id:
            return
        for call, response in zip(calls, responses):
            self._record_agent_result(run_id, call, response)

    def _check_agent_duration(self) -> None:
        service = getattr(self, "_agent_runs", None)
        if service is None:
            return
        try:
            service.check_duration()
        except _agent_run.AgentRunError as exc:
            print(f"[JARVIS] Agent run duration limit: {exc}")

    def _active_task_run(self):
        service = getattr(self, "_agent_runs", None)
        if service is None:
            return None
        run = service.current()
        if run is None or run.status not in _agent_run.ACTIVE_STATUSES:
            return None
        return run

    def _active_task_context(self, limit: int = 1400) -> str:
        run = self._active_task_run()
        if run is None:
            return ""
        try:
            return run.context_for_model(limit)
        except Exception as exc:  # noqa: BLE001 - context is best effort
            print(f"[JARVIS] Agent task context failed: {exc}")
            return ""

    @staticmethod
    def _agent_context_key(run) -> tuple:
        snapshot = run.to_dict()
        verification = snapshot.get("verification") or {}
        return (
            snapshot.get("run_id"),
            snapshot.get("updated_at"),
            snapshot.get("status"),
            snapshot.get("step_count"),
            snapshot.get("tool_call_count"),
            snapshot.get("consecutive_failures"),
            snapshot.get("current_step"),
            snapshot.get("last_tool_summary"),
            verification.get("attempts"),
            verification.get("exhausted"),
            len(verification.get("evidence") or []),
            len(verification.get("missing") or []),
            len(snapshot.get("failures") or []),
            len(snapshot.get("blockers") or []),
            len(snapshot.get("completed_operations") or []),
            snapshot.get("remaining_work"),
        )

    async def _inject_task_context(self, reason: str = "", *,
                                   force: bool = False,
                                   run_id: str = "") -> None:
        """Send a bounded task-context reference part into the Live session."""
        run = self._active_task_run()
        if run is None:
            return
        if run_id and run.run_id != run_id:
            return
        key = self._agent_context_key(run)
        if not force and key == getattr(self, "_last_agent_context_key", None):
            return
        text = self._active_task_context()
        if not text or not self.session:
            return
        try:
            await self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=False,
            )
            self._last_agent_context_key = key
        except Exception as exc:  # noqa: BLE001 - never break the conversation
            print(f"[JARVIS] Agent task context injection failed: {exc}")

    # ── Responsive tool dispatch ─────────────────────────────────────────────

    @staticmethod
    def _batch_starts_task(calls: list) -> bool:
        for call in calls:
            if str(getattr(call, "name", "") or "") != "task_status":
                continue
            try:
                args = dict(getattr(call, "args", None) or {})
            except Exception:
                args = {}
            if str(args.get("operation") or "").strip().lower() == "start":
                return True
        return False

    def _schedule_tool_dispatch(self, calls: list) -> None:
        """Queue one tool-call message without blocking the receive loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        previous = getattr(self, "_tool_chain", None)
        generation = getattr(self, "_session_generation", 0)
        service = getattr(self, "_agent_runs", None)
        active = service.current() if service is not None else None
        source_run_id = (
            active.run_id if active is not None
            and active.status in _agent_run.ACTIVE_STATUSES else "")
        self._tool_pending = int(getattr(self, "_tool_pending", 0)) + 1
        self._tool_in_progress = True
        tasks = getattr(self, "_tool_tasks", None)
        if tasks is None:
            tasks = set()
            self._tool_tasks = tasks

        async def runner():
            try:
                if previous is not None:
                    try:
                        await previous
                    except asyncio.CancelledError:
                        return
                    except Exception:  # noqa: BLE001 - a prior batch must not stop this one
                        pass
                if generation != getattr(self, "_session_generation", 0):
                    return
                await self._run_tool_batch(calls, source_run_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the session
                print(f"[JARVIS] Tool batch failed: {exc}")
                traceback.print_exc()
            finally:
                if generation == getattr(self, "_session_generation", 0):
                    self._tool_pending = max(
                        0, int(getattr(self, "_tool_pending", 1)) - 1)
                    self._tool_in_progress = self._tool_pending > 0
                    if self._tool_pending == 0:
                        self._arm_stall()

        task = loop.create_task(runner())
        self._tool_chain = task
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _run_tool_batch(self, calls: list,
                              source_run_id: str = "") -> None:
        """Execute one tool batch and send its responses on the owning session."""
        session = self.session
        if session is None:
            return
        started = time.monotonic()
        service = getattr(self, "_agent_runs", None)
        current_run = service.current() if service is not None else None
        # A finished run no longer owns future dispatches. Treat it as empty so
        # a batch that starts a new task does not lose its guards/recording.
        active_run = (
            current_run if current_run is not None
            and current_run.status not in _agent_run.TERMINAL_STATUSES else None)
        stale_owner = bool(
            source_run_id
            and (current_run is None
                 or current_run.run_id != source_run_id
                 or current_run.status in _agent_run.TERMINAL_STATUSES))
        dynamic_start = bool(
            service is not None
            and active_run is None
            and not source_run_id
            and not self._agent_terminal_until_user
            and self._batch_starts_task(calls))
        if stale_owner:
            run_id = source_run_id
            fn_resps = self._blocked_tool_responses(calls, source_run_id)
        elif dynamic_start:
            run_id = ""
            state = {"admitted": False, "blocked": False, "run_id": ""}

            def dynamic_guard() -> bool:
                run = service.current()
                if run is None:
                    return True
                # The batch may be about to start a new task after a previous
                # one finished; do not let the terminal run block its own
                # task_status(start) control call.
                if run.status in _agent_run.TERMINAL_STATUSES:
                    return True
                if state["run_id"] and run.run_id != state["run_id"]:
                    return False
                return service.can_continue(run.run_id)

            def dynamic_record(call, response):
                run = service.current()
                if run is None:
                    return None
                if state["run_id"] and run.run_id != state["run_id"]:
                    return None
                state["run_id"] = run.run_id
                return self._record_agent_result(run.run_id, call, response)

            def dynamic_call_guard(call) -> str:
                run = service.current()
                if run is None:
                    return ""
                # A task_status call is the control operation that may start
                # the new run. Do not bind the batch state to the previous
                # (terminal) run before it executes.
                if str(getattr(call, "name", "") or "") == "task_status":
                    return ""
                if state["run_id"] and run.run_id != state["run_id"]:
                    return "the task run was replaced by a newer run"
                state["run_id"] = run.run_id
                reason = service.call_guard(run.run_id, call)
                if reason or state["blocked"]:
                    return reason or "task run limit reached"
                if not state["admitted"]:
                    # Count only calls that will actually execute; duplicates
                    # refused by the guard must not inflate the batch capacity.
                    dispatchable = sum(
                        1 for item in calls
                        if str(getattr(item, "name", "") or "")
                        != "task_status"
                        and not service.call_guard(run.run_id, item))
                    if not service.begin_execution(
                            run.run_id, step="Running tool batch",
                            tool_call_count=max(1, dispatchable)):
                        state["blocked"] = True
                        return "task run limit reached"
                    state["admitted"] = True
                return ""

            fn_resps = await tool_calling.dispatch_tool_calls(
                calls,
                self.ui.write_log,
                self.ui.tool_event,
                should_continue=dynamic_guard,
                on_result=dynamic_record,
                call_guard=dynamic_call_guard,
            )
            if state["run_id"]:
                run_id = state["run_id"]
        else:
            run_id, blocked = self._prepare_agent_dispatch(calls)
            if blocked:
                fn_resps = self._blocked_tool_responses(calls, run_id or "")
            else:
                guard = ((lambda rid=run_id: self._agent_can_continue(rid))
                         if run_id else None)
                recorder = ((lambda call, response, rid=run_id:
                             self._record_agent_result(rid, call, response))
                            if run_id else None)
                call_guard = ((lambda call, rid=run_id:
                               self._agent_call_guard(rid, call))
                              if run_id else None)
                fn_resps = await tool_calling.dispatch_tool_calls(
                    calls,
                    self.ui.write_log,
                    self.ui.tool_event,
                    should_continue=guard,
                    on_result=recorder,
                    call_guard=call_guard,
                )
        _trace("tool", dur=round(time.monotonic() - started, 3),
               names=",".join(str(getattr(c, "name", "?")) for c in calls))
        if session is not self.session:
            return
        self._remember_approval_calls(calls, fn_resps, run_id or "")
        if any(_function_response_result(response).get("sleep_requested")
               for response in fn_resps):
            # Prevent a later model turn in the same user instruction from
            # continuing to dispatch tools after sleep was requested.
            self._agent_terminal_until_user = True
        await self._inject_task_context("tool result", run_id=run_id or "")
        if fn_resps:
            await session.send_tool_response(function_responses=fn_resps)
        else:
            print("[JARVIS] Tool call message produced no function responses.")

    def _flush_user_transcript(self, in_buf: list) -> None:
        """Show the user's spoken turn before the assistant starts answering."""
        full_in = " ".join(in_buf).strip()
        if not full_in:
            return
        self._last_out_logged = ""
        self.ui.write_log(f"You: {full_in}")
        in_buf.clear()

    def _maybe_end_phone_call(self, text: str) -> bool:
        """End an active SIP call for an explicit natural-language goodbye."""
        if not text or not _CALL_END_RE.search(text):
            return False
        telephony = getattr(self, "_telephony", None)
        if telephony is None or not telephony.bridge_active():
            return False

        def _end() -> None:
            try:
                result = telephony.hangup()
                self.ui.write_log(
                    "SYS: Call ended by caller"
                    + ("." if result.get("ok")
                       else f": {result.get('error', '')}"))
            except Exception as exc:  # noqa: BLE001 - goodbye must not crash audio
                self.ui.write_log(f"SYS: Could not end the call: {exc}")

        try:
            asyncio.get_running_loop().create_task(asyncio.to_thread(_end))
        except RuntimeError:
            _end()
        return True

    def send_text(self, text: str) -> bool:
        """Send one typed turn.  Returns whether it was handed to the session.

        Kept as a named method (rather than only a callback) so the local web
        runtime can report an honest "not connected" state instead of claiming
        a message was sent into a session that does not exist.
        """
        if self._maybe_end_phone_call(text):
            return True
        # A new typed turn is a fresh user instruction; a previously finished
        # task run must not block it.
        self._agent_terminal_until_user = False
        self._task_context_turn_injected = False
        if not self._loop or not self.session:
            return False
        # A typed turn is a new user turn: it must not inherit a STOP aimed at
        # the previous reply, and it must not be compared with that reply when
        # deciding whether the transcript is a duplicate.
        self._interrupted = False
        self._interrupted_source = ""
        self._last_out_logged = ""
        turn_done = self._turn_done_event
        if turn_done is not None:
            try:
                self._loop.call_soon_threadsafe(turn_done.clear)
            except Exception:
                pass
        try:
            parts: list[dict] = []
            task_context = self._active_task_context()
            if task_context:
                parts.append({"text": task_context})
            context_getter = getattr(self, "_memory_context_for", None)
            memory_context = context_getter(text) if callable(context_getter) else ""
            if memory_context:
                parts.append({"text": memory_context})
            parts.append({"text": text})
            future = asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={"role": "user", "parts": parts},
                    turn_complete=True,
                ),
                self._loop,
            )
        except Exception as exc:
            print(f"[JARVIS] Text send failed: {exc}")
            traceback.print_exc()
            return False

        def _report_failure(done) -> None:
            """Observe the future so a failed send is not a silent exception."""
            try:
                done.result()
            except (asyncio.CancelledError, FutureCancelledError):
                return
            except Exception as exc:  # noqa: BLE001 - reported to the user
                print(f"[JARVIS] Text send failed: {exc}")
                traceback.print_exc()
                try:
                    self.ui.write_log(
                        "ERR: The message could not be sent to the session.")
                except Exception:
                    pass

        try:
            future.add_done_callback(_report_failure)
        except Exception:
            # A future without add_done_callback is not a reason to reject the
            # turn that was already scheduled.
            pass
        return True

    def stop(self) -> None:
        """Cancel session tasks and let asyncio.close the loop normally.

        Calling loop.stop() directly leaves run_until_complete with an unfinished
        main coroutine, which produces RuntimeError/CancelledError tracebacks on
        Ctrl+C. Cancelling the tasks lets the TaskGroup and session close cleanly.
        """
        self._shutdown = True
        self._cancel_agent_run("app shutdown", lock=True)
        approvals = getattr(self, "_approvals", None)
        if approvals is not None:
            try:
                approvals.revoke_all("app shutdown")
            except Exception:
                pass
        # Active Skills are task-scoped runtime state. A shutdown must not leave
        # a stale active list for the next process or the Skills panel.
        try:
            _get_skills_engine().clear_all_active()
        except Exception:  # noqa: BLE001 - shutdown still proceeds
            pass
        # Any screen observation belongs wholly to this app: stop it with
        # the app so no capture thread or frame pump outlives the shutdown.
        try:
            screen_understanding_tool.shutdown_observation("app shutdown")
        except Exception:                            # noqa: BLE001
            pass
        try:
            self._telephony.set_voice_online(False)
            self._telephony.stop()
        except Exception:                            # noqa: BLE001
            pass
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            return

        def _cancel_tasks() -> None:
            try:
                tasks = list(asyncio.all_tasks(loop=loop))
            except Exception:
                return
            for task in tasks:
                try:
                    if task is not asyncio.current_task(loop=loop):
                        task.cancel()
                except Exception:
                    pass

        try:
            loop.call_soon_threadsafe(_cancel_tasks)
        except Exception:
            pass

    def _tail_active(self) -> bool:
        return time.monotonic() < self._tail_until

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            was_speaking = self._is_speaking
            self._is_speaking = value
        if value:
            self._tail_until = 0.0
            if not was_speaking:
                # A new reply starts a new barge-in window. Never reuse the
                # previous reply's microphone pre-roll or detector state.
                with getattr(self, "_pre_roll_lock", threading.Lock()):
                    if hasattr(self, "_pre_roll"):
                        self._pre_roll.clear()
                phone_lock = getattr(self, "_phone_pre_roll_lock", None)
                if phone_lock is not None:
                    with phone_lock:
                        self._phone_pre_roll.clear()
                event = getattr(self, "_barge_in_event", None)
                if event is not None:
                    event.clear()
                self._barge_in_active = False
                echo = getattr(self, "_echo", None)
                if echo is not None:
                    echo.reset()
                phone_echo = getattr(self, "_phone_echo", None)
                if phone_echo is not None:
                    phone_echo.reset()
                self._phone_tail_until = 0.0
                self._phone_speech_run = 0
                self._phone_speech_grace_until = time.monotonic() + 0.25
        else:
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
            # The phone path has network + handset latency on top of the
            # speaker's own tail. Keep judging through a short phone tail so
            # the caller's earpiece echo is not mistaken for a new turn.
            self._phone_tail_until = time.monotonic() + 1.2
            self._phone_speech_run = 0
            self._out_level = 0.0
        if value:
            self.ui.set_state("SPEAKING")
        else:
            # Let the UI apply its own muted mapping.  Skipping the call while
            # muted (the old behaviour) left the halo stuck on SPEAKING after
            # the reply had finished.
            self.ui.set_state("LISTENING")

    def _should_stop_speaking(self, now: float) -> bool:
        """Whether playback has finished and the HUD may leave SPEAKING.

        Two independent signals, so one missing message cannot strand the UI:
        the turn-complete event ends the reply after a short grace, and the
        audio clock ends it after silence even when no event ever arrived.
        """
        if not self.audio_in_queue or not self.audio_in_queue.empty():
            return False
        # On a phone call the RTP sender has its own paced queue. Do not declare
        # the reply finished (and open the echo tail) while the caller is still
        # hearing the last few hundred milliseconds.
        telephony = getattr(self, "_telephony", None)
        if (telephony is not None and telephony.bridge_active()
                and telephony.buffered_seconds() > 0.15):
            return False
        idle = now - self._last_audio_at
        if self._turn_done_event is not None and self._turn_done_event.is_set():
            return idle >= _TURN_DONE_GRACE
        return idle >= _SPEAK_IDLE

    async def _wait_for_prebuffer(self) -> None:
        """Hold playback until the audio backlog reaches the jitter reserve.

        Backlog = estimated device buffer + queued audio. Waiting only while
        the backlog is short means a burst builds a reserve instead of being
        played out immediately and leaving the next network hole audible.
        Bounded on both sides: it returns as soon as the reserve is reached,
        and a trickling stream still starts after ``_RESERVE_WAIT_S``.
        """
        queue = self.audio_in_queue
        if queue is None:
            return
        deadline = time.monotonic() + _RESERVE_WAIT_S
        while not self._interrupted and time.monotonic() < deadline:
            now = time.monotonic()
            if self._dev_last:
                self._dev_buffer = max(
                    0.0, self._dev_buffer - (now - self._dev_last))
            self._dev_last = now
            backlog = self._dev_buffer + queue.qsize() * 0.05
            if backlog >= _RESERVE_S:
                return
            await asyncio.sleep(0.01)

    def set_push_to_talk(self, enabled: bool) -> str:
        from core.hotkey import PushToTalk

        self._ptt_enabled = bool(enabled)
        self._ptt_held = False
        if not enabled:
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            return "off"

        if self._ptt is None:
            self._ptt = PushToTalk(self._on_ptt)
        scope = self._ptt.start()
        try:
            self.ui.write_log(
                f"SYS: Push-to-talk on — hold {self._ptt.label}"
                + ("." if scope == "global"
                   else " (works while this window is focused)."))
        except Exception:
            pass
        return scope

    def _on_ptt(self, held: bool) -> None:
        self._ptt_held = held
        try:
            self.ui.set_state("LISTENING")
        except Exception:
            pass

    def _discard_queued_audio(self) -> int:
        """Empty the reply queue on the session loop. Returns chunks dropped."""
        queue = self.audio_in_queue
        drained = 0
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
                except Exception:
                    break
        if self._turn_done_event is not None:
            self._turn_done_event.clear()
        # The telephone sender has its own paced queue. An interruption must
        # silence it immediately; otherwise the caller keeps hearing words that
        # were already generated before the barge-in.
        telephony = getattr(self, "_telephony", None)
        if telephony is not None:
            try:
                telephony.flush_audio()
            except Exception:
                pass
        return drained

    def _schedule_output_abort(self) -> None:
        """Flush the device buffer without blocking the event loop/callback."""
        stream = getattr(self, "_out_stream", None)
        if stream is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._abort_output_stream()
            return
        loop.create_task(asyncio.to_thread(self._abort_output_stream))

    def _abort_output_stream(self) -> None:
        """Abort the PortAudio stream; this also discards its device buffer.

        PortAudio's ``abort`` is explicitly safe from another thread. The
        playback loop notices ``_output_aborted`` and restarts the stream
        before accepting the next reply, so no stale audio resumes.
        """
        stream = getattr(self, "_out_stream", None)
        if stream is None:
            return
        self._output_aborted = True
        try:
            stream.abort()
        except Exception:
            # A stream that is already stopped/closed needs no further work.
            pass

    def interrupt(self) -> None:
        """Stop speaking mid-reply: drain queued audio and open the mic.

        Called from the HTTP worker thread (web) and from the Qt thread
        (desktop), never from the session loop.  Draining an ``asyncio.Queue``
        and clearing an ``asyncio.Event`` from another thread is not safe, so
        those two operations are marshalled onto the session loop.  The flags
        the audio threads read and the UI calls are already thread-safe.
        """
        self._interrupted = True
        self._interrupted_at = time.monotonic()
        self._interrupted_source = "manual"
        self._barge_in_active = False
        self._cancel_agent_run("user interrupted", lock=True)
        approvals = getattr(self, "_approvals", None)
        if approvals is not None:
            try:
                approvals.revoke_all("user interrupted")
            except Exception:
                pass
        loop = self._loop

        def _drain_on_loop() -> None:
            drained = self._discard_queued_audio()
            if drained:
                print(f"[JARVIS] Interrupted — {drained} audio chunks discarded")
            self._schedule_output_abort()

        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(_drain_on_loop)
            except Exception:
                # The loop closed between the check and the call; draining the
                # captured queue here cannot race a session that no longer runs.
                _drain_on_loop()
        else:
            _drain_on_loop()

        self.set_speaking(False)
        self._play_cursor = 0.0
        self._memory_turn_injected = False
        self._task_context_turn_injected = False
        self.ui.write_log("SYS: Interrupted — listening...")

    def _begin_barge_in(self, pre_roll: bytes) -> None:
        """Run on the session loop when local speech interrupts playback.

        The device buffer is aborted, queued reply audio is discarded, and the
        microphone pre-roll (including the syllable that triggered the
        detector) is handed to Gemini. Its automatic VAD then sees the user's
        real utterance and interrupts the server-side generation.
        """
        event = getattr(self, "_barge_in_event", None)
        if event is not None:
            event.clear()
        if not pre_roll:
            return
        self._barge_in_count = getattr(self, "_barge_in_count", 0) + 1
        already_interrupted = self._interrupted
        self._interrupted = True
        self._interrupted_at = time.monotonic()
        self._interrupted_source = "barge"
        self._discard_queued_audio()
        self._schedule_output_abort()
        self.set_speaking(False)
        self._barge_in_active = True
        self._play_cursor = 0.0
        self._memory_turn_injected = False
        self._task_context_turn_injected = False
        echo = getattr(self, "_echo", None)
        if echo is not None:
            echo.reset()
        if not already_interrupted:
            self.ui.write_log("SYS: Barge-in detected — listening...")
        queue = self.out_queue
        if queue is None:
            return
        block_bytes = max(2, CHUNK_SIZE * 2)
        try:
            for start in range(0, len(pre_roll), block_bytes):
                self._enqueue_mic_chunk(pre_roll[start:start + block_bytes])
        except Exception as exc:  # noqa: BLE001 - never kill the mic callback
            print(f"[JARVIS] Could not forward barge-in audio: {exc}")

    def _handle_server_interruption(self) -> None:
        """The server reports that our reply was interrupted by the user.

        Gemini can also detect the start of speech from the forwarded barge-in
        audio. Stop immediately even if the local guard already did, and keep
        the microphone open for the rest of the utterance.
        """
        already = self._interrupted
        self._interrupted = True
        self._interrupted_at = time.monotonic()
        self._interrupted_source = "server" if not already else self._interrupted_source
        if already:
            return
        self._barge_in_active = True
        self._discard_queued_audio()
        self._schedule_output_abort()
        self.set_speaking(False)
        self._play_cursor = 0.0
        self._memory_turn_injected = False
        self._task_context_turn_injected = False
        try:
            self.ui.write_log("SYS: Speech interrupted — listening...")
        except Exception:
            pass

    def _maybe_end_interruption(self) -> None:
        """A new user turn ends the previous STOP's suppression window.

        Normally ``turn_complete`` clears ``_interrupted`` in the receive loop.
        If the click landed after the turn had already completed, no such
        message is coming. Requiring a short delay keeps a late transcript from
        the interrupted utterance from clearing the suppression itself.
        """
        if not self._interrupted:
            return
        source = getattr(self, "_interrupted_source", "")
        elapsed = time.monotonic() - getattr(self, "_interrupted_at", 0.0)
        # A local barge-in waits for the server's interrupted turn to complete
        # before accepting new audio, so stale reply chunks cannot resume. A
        # short safety window covers a server that already completed the turn
        # and therefore will not send another turn_complete.
        if source in ("barge", "server") and elapsed < 2.0:
            return
        if elapsed >= _INTERRUPT_RESUME_AFTER or source in ("barge", "server"):
            self._interrupted = False
            self._interrupted_source = ""

    # ── Session configuration ────────────────────────────────────────────────

    def _memory_service(self):
        """The shared memory service, or None when memory is disabled/unavailable."""
        try:
            if not get_memory_settings().get("enabled", True):
                return None
            from core import memory as _memory

            service = _memory.get_default_service()
            if service is None and not self._memory_error_logged:
                self._memory_error_logged = True
                print("[JARVIS] Memory unavailable: "
                      + (_memory.get_default_error() or "unknown error"))
            return service
        except Exception as exc:  # noqa: BLE001 - chat must still work
            if not self._memory_error_logged:
                self._memory_error_logged = True
                print(f"[JARVIS] Memory integration unavailable: {exc}")
            return None

    def _memory_context_for(self, text: str) -> str:
        """Bounded, ranked context for one turn; empty when nothing is relevant.

        USER preferences are already in the session's system instruction, so
        per-turn retrieval covers the other three categories. That keeps one
        memory from appearing in two prompt layers.
        """
        service = self._memory_service()
        if service is None or not text:
            return ""
        try:
            return service.retrieve_relevant_memories(
                text,
                memory_types=["episodic", "knowledge", "project"],
            ).context
        except Exception as exc:  # noqa: BLE001 - retrieval is enrichment
            print(f"[JARVIS] Memory retrieval failed: {exc}")
            return ""

    def _standing_memory_context(self) -> str:
        service = self._memory_service()
        if service is None:
            return ""
        try:
            return service.session_context()
        except Exception as exc:  # noqa: BLE001
            print(f"[JARVIS] Memory session context failed: {exc}")
            return ""

    async def _inject_voice_memory(self, text: str) -> None:
        """Best-effort query-specific retrieval while a voice turn is open.

        The Live session is a duplex stream, so there is no prompt-build hook.
        A text part with ``turn_complete=False`` adds reference data to the
        model's current context without requesting a second reply. Failures
        are logged and never interrupt the conversation.
        """
        try:
            settings = get_memory_settings()
            if not settings.get("voice_injection", True):
                return
            context = self._memory_context_for(text)
            if not context or self.session is None:
                return
            await self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": context}]},
                turn_complete=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[JARVIS] Voice memory injection failed: {exc}")

    def _queue_memory_extraction(self, user_text: str, assistant_text: str) -> None:
        """Queue one completed turn; never block or fail the chat path."""
        user_text = str(user_text or "").strip()
        assistant_text = str(assistant_text or "").strip()
        if len(user_text) < 8 or len(assistant_text) < 4:
            return
        try:
            settings = get_memory_settings()
            if not settings.get("auto_extract", True):
                return
            service = self._memory_service()
            if service is None:
                return
            turn_id = hashlib.sha256(
                f"{self._conversation_id}\x1f{user_text}\x1f{assistant_text}"
                .encode("utf-8", "replace")).hexdigest()[:40]
            worker = service.start_worker()
            if not worker.submit(turn_id, self._conversation_id,
                                 user_text, assistant_text):
                print("[JARVIS] Memory extraction queue full — turn skipped.")
        except Exception as exc:  # noqa: BLE001
            print(f"[JARVIS] Memory extraction could not be queued: {exc}")

    def _build_config(self) -> types.LiveConnectConfig:
        # Read through ``config`` so a malformed settings file degrades to the
        # same defaults every other consumer uses, instead of raising on `.get`.
        self._asst_name = get_assistant_name()
        user_name = get_user_name()
        set_name = getattr(self.ui, "set_assistant_name", None)
        if callable(set_name):
            try:
                set_name(self._asst_name)
            except Exception:
                pass

        sys_prompt = _load_system_prompt()
        now = datetime.now()
        sys_prompt = _render_prompt(sys_prompt, {
            "assistant_name": self._asst_name,
            "user_name": user_name or "the user",
            "platform": f"{_platform.system()} {_platform.release()}".strip(),
            "date_time": now.strftime("%A, %B %d, %Y — %I:%M %p"),
        })
        # Add a bounded memory block *after* the existing prompt; the prompt is
        # never replaced. Memory is data, explicitly below system instructions.
        memory_getter = getattr(self, "_standing_memory_context", None)
        memory_context = memory_getter() if callable(memory_getter) else ""
        if memory_context:
            sys_prompt = f"{sys_prompt}\n\n{memory_context}"

        # Skills are metadata at connection time. Full SKILL.md instructions are
        # only present when a Skill was activated; on a reconnect without a
        # resumed session they are restored here because the old tool results
        # are gone. A resumed connection already has them in its transcript, so
        # only a compact active index is added to avoid duplicate injection.
        try:
            skills_engine = _get_skills_engine()
            skills_scope = str(
                getattr(self, "_conversation_id", "")
                or f"session-{id(self)}")
            skills_engine.set_active_scope(skills_scope)
            catalog = skills_engine.prompt_catalog()
            if catalog:
                sys_prompt = f"{sys_prompt}\n\n{catalog}"
            skills_context = skills_engine.session_context(
                skills_scope,
                include_instructions=not bool(
                    getattr(self, "_resume_handle", None)),
            )
            if skills_context:
                sys_prompt = f"{sys_prompt}\n\n{skills_context}"
        except Exception as exc:  # noqa: BLE001 - chat must still connect
            print(f"[JARVIS] Skills context unavailable: {exc}")

        # Preserve an active objective across a Live reconnect.  The context is
        # bounded and is also refreshed into the session after tool turns.
        context_getter = getattr(self, "_active_task_context", None)
        task_context = context_getter() if callable(context_getter) else ""
        if task_context:
            sys_prompt = f"{sys_prompt}\n\n{task_context}"
        run_getter = getattr(self, "_active_task_run", None)
        active_run = run_getter() if callable(run_getter) else None
        key_getter = getattr(self, "_agent_context_key", None)
        if active_run is not None and callable(key_getter):
            self._last_agent_context_key = key_getter(active_run)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=sys_prompt,
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        # Read the selection per connection rather than at module import: the
        # ACTIONS bay can enable/disable whole folders and individual tools,
        # and its change requests a reconnect so the model sees the new set.
        declarations = tool_calling.get_tool_declarations(enabled_only=True)
        if declarations:
            cfg["tools"] = [{"function_declarations": declarations}]
        if self._enhanced_live and get_proactive_audio_enabled():
            cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)
        if self._tuned_live:
            cfg.update(self._tuning_config())
        # Barge-in depends on the server treating the first detected activity
        # as an interruption, not waiting for the old reply to finish.
        if "realtime_input_config" not in cfg:
            cfg["realtime_input_config"] = types.RealtimeInputConfig(
                activity_handling=(
                    types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
                ),
            )
        return types.LiveConnectConfig(**cfg)

    def _tuning_config(self) -> dict:
        out: dict = {}
        turn = get_turn_tuning()
        detect = None
        if turn.get("enabled", True):
            detect = types.AutomaticActivityDetection(
                silence_duration_ms=turn["silence_ms"],
                prefix_padding_ms=turn["prefix_ms"],
            )
            if turn["end_sensitivity"] == "high":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_HIGH
            elif turn["end_sensitivity"] == "low":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW
            if turn["start_sensitivity"] == "high":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH
            elif turn["start_sensitivity"] == "low":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_LOW
        out["realtime_input_config"] = types.RealtimeInputConfig(
            automatic_activity_detection=detect,
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        )
        if get_thinking_enabled():
            out["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)
        return out

    # ── Audio transport ──────────────────────────────────────────────────────

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            _t0 = time.monotonic()
            try:
                await self.session.send_realtime_input(
                    audio=types.Blob(
                        data=msg["data"],
                        mime_type=msg.get("mime_type", "audio/pcm"),
                    )
                )
            finally:
                _trace("tx", dur=round(time.monotonic() - _t0, 6))

    def _enqueue_mic_chunk(self, data: bytes) -> None:
        """Queue one microphone block from the loop, dropping under pressure.

        The callback used to call ``put_nowait`` directly through
        ``call_soon_threadsafe``; a full queue raised inside an asyncio callback
        and produced an unhandled-exception log instead of simply dropping a
        block the network could not accept in time.
        """
        queue = self.out_queue
        if queue is None:
            return
        try:
            queue.put_nowait({
                "data": data,
                "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
            })
        except asyncio.QueueFull:
            pass
        except RuntimeError:
            pass

    # ── Telephone audio bridge ───────────────────────────────────────────────

    def _on_phone_audio(self, call_id: str, pcm16k: bytes) -> None:
        """RTP thread -> session loop. Must never block the RTP thread."""
        if not pcm16k or not self._telephony.bridge_active():
            return
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._handle_phone_block, call_id, pcm16k)
        except Exception:
            pass

    def _handle_phone_block(self, call_id: str, pcm16k: bytes) -> None:
        """Feed caller audio into the existing Gemini turn pipeline.

        The phone's earpiece is picked up by the caller's microphone. Feeding
        that echo straight to Gemini made the server hear its own voice,
        classify it as a user interruption, and stop long replies halfway
        through. This path therefore uses its own EchoGuard, just like the local
        microphone path: while Jarvis is speaking (and through the short phone
        echo tail), only audio that is demonstrably a different voice is sent
        on. A genuine barge-in still flushes the RTP sender immediately.
        """
        if not self._telephony.bridge_active():
            return
        if self._telephony.bridge_call_id() not in ("", call_id):
            return
        try:
            samples = np.frombuffer(pcm16k, dtype=np.int16)
            level = _pcm_level(samples)
        except Exception:
            samples = np.empty(0, dtype=np.int16)
            level = 0.0
        self._phone_rx_blocks += 1
        with self._phone_pre_roll_lock:
            self._phone_pre_roll.append(pcm16k)
            if len(self._phone_pre_roll) > 30:  # ~600 ms at 20 ms blocks
                del self._phone_pre_roll[:-30]
        with self._speaking_lock:
            speaking = self._is_speaking
        now = time.monotonic()
        guarded = speaking or now < self._phone_tail_until

        if guarded:
            echo = self._phone_echo
            user_speech = False
            if echo.armed:
                try:
                    user_speech = echo.is_user_speech(
                        samples, SEND_SAMPLE_RATE, level)
                except Exception:
                    user_speech = False
            if not user_speech or level < self._phone_barge_level:
                self._phone_speech_run = 0
                self._phone_rx_suppressed += 1
                if self._phone_trace and self._phone_rx_suppressed % 50 == 1:
                    print(
                        "[PHONE] caller echo/noise suppressed "
                        f"blocks={self._phone_rx_suppressed} level={level:.2f} "
                        f"speaking={speaking}", flush=True)
                return
            self._phone_speech_run += 1
            if speaking and not self._interrupted:
                required = self._phone_barge_blocks
                if (self._phone_speech_run < required
                        or now < self._phone_speech_grace_until):
                    if self._phone_trace and self._phone_speech_run == 1:
                        self._phone_ignored_noise += 1
                        print(
                            "[PHONE] short noise held, waiting for sustained "
                            f"speech (level={level:.2f})", flush=True)
                    return
                with self._phone_pre_roll_lock:
                    pre_roll = b"".join(self._phone_pre_roll)
                    self._phone_pre_roll.clear()
                self._phone_barge_ins += 1
                self._phone_speech_run = 0
                # The existing barge-in path discards queued Gemini audio,
                # aborts local playback and forwards the pre-roll to Gemini.
                # Flushing the RTP sender is the telephone equivalent: the
                # caller hears silence immediately, not stale words.
                self._begin_barge_in(pre_roll)
                self._telephony.flush_audio()
                return
            # A caller who replies immediately after a reply should not wait
            # for the whole echo tail. Require a short, level-qualified run so
            # line noise cannot start a new turn.
            if self._phone_speech_run < 3:
                self._phone_rx_suppressed += 1
                return
            self._phone_speech_run = 0
            self._phone_tail_until = 0.0

        self._phone_rx_forwarded += 1
        self._enqueue_mic_chunk(pcm16k)
        try:
            self.ui.set_audio_level(level)
        except Exception:
            pass
        if self._phone_trace and (
                self._phone_rx_blocks == 1
                or self._phone_rx_blocks % 50 == 0):
            print(
                f"[PHONE] caller rx blocks={self._phone_rx_blocks} "
                f"forwarded={self._phone_rx_forwarded} "
                f"suppressed={self._phone_rx_suppressed} "
                f"barge={self._phone_barge_ins} level={level:.2f}",
                flush=True)

    def _handle_mic_block(self, indata) -> None:
        """Audio-callback body, separated so barge-in can be tested without PortAudio."""
        telephony = getattr(self, "_telephony", None)
        if telephony is not None and telephony.bridge_active():
            # During a phone call the caller is the microphone. Keeping the
            # room microphone open would make Jarvis answer the local room and
            # would echo the room into the call.
            return
        if getattr(self.ui, "muted", False):
            return
        if getattr(self, "_ptt_enabled", False) and not getattr(self, "_ptt_held", False):
            return
        level = _pcm_level(indata)
        with self._speaking_lock:
            jarvis_speaking = self._is_speaking

        echo = getattr(self, "_echo", None)
        if jarvis_speaking:
            # Keep the audio that led up to detection so the first syllable
            # is not lost when the echo guard finally recognises a voice.
            pre_roll_lock = getattr(self, "_pre_roll_lock", None)
            if pre_roll_lock is not None:
                with pre_roll_lock:
                    self._pre_roll.append(indata.tobytes())
                    if len(self._pre_roll) > _BARGE_IN_PREROLL_BLOCKS:
                        del self._pre_roll[:-_BARGE_IN_PREROLL_BLOCKS]
            if echo is None or not getattr(echo, "armed", False):
                # No output reference yet: never guess from room noise.
                return
            try:
                user_speech = echo.is_user_speech(
                    indata, SEND_SAMPLE_RATE, level)
            except Exception:
                return
            if not user_speech:
                return

            event = getattr(self, "_barge_in_event", None)
            if event is not None:
                if event.is_set():
                    return
                event.set()
            pre_roll = b""
            if pre_roll_lock is not None:
                with pre_roll_lock:
                    pre_roll = b"".join(self._pre_roll)
                    self._pre_roll.clear()
            try:
                self.ui.set_audio_level(level)
            except Exception:
                pass
            loop = getattr(self, "_loop", None)
            if loop is None:
                if event is not None:
                    event.clear()
                return
            try:
                loop.call_soon_threadsafe(self._begin_barge_in, pre_roll)
            except Exception:
                if event is not None:
                    event.clear()
            return

        if getattr(self, "_barge_in_active", False):
            # The reply was cancelled locally. Deliver the user's full
            # utterance immediately; the server VAD closes the turn.
            pass
        elif self._tail_active():
            try:
                if echo is None or not echo.is_user_speech(
                        indata, SEND_SAMPLE_RATE, level):
                    return
                self._tail_until = 0.0
            except Exception:
                return
        elif echo is not None and echo._hist:
            echo.reset()
        if not getattr(self.ui, "muted", False):
            loop = getattr(self, "_loop", None)
            if loop is None:
                return
            loop.call_soon_threadsafe(
                self._enqueue_mic_chunk,
                indata.tobytes(),
            )
            try:
                self.ui.set_audio_level(level)
            except Exception:
                pass

    async def _listen_audio(self):
        print("[JARVIS] Mic started")

        def callback(indata, frames, time_info, status):
            self._handle_mic_block(indata)

        try:
            def _open_mic(dev):
                stream = sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )
                # Start here, inside the caller's fallback try. Constructing the
                # stream can succeed while starting it fails on a busy or
                # unsupported endpoint, and that must fall back to the default
                # microphone too.
                stream.start()
                return stream

            mic_name = get_input_device()
            # Device enumeration and capability probes open real PortAudio
            # streams and can take seconds. Never run that on the session loop:
            # the receive/send tasks must stay responsive while it happens.
            mic_dev = await asyncio.to_thread(
                audio_devices.resolve, mic_name, "input")
            if mic_dev is not None:
                print(f"[JARVIS] Input device: {mic_name}")
            elif mic_name:
                self.ui.write_log(
                    f"SYS: Microphone '{mic_name}' is not connected — using "
                    "the system default.")
                print(f"[JARVIS] Input device '{mic_name}' not found")
            try:
                mic_stream = await asyncio.to_thread(_open_mic, mic_dev)
            except Exception as e:
                if mic_dev is None:
                    raise
                print(f"[JARVIS] Mic '{mic_name}' failed: {e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{mic_name}' unavailable — using system default.")
                mic_stream = await asyncio.to_thread(_open_mic, None)

            with mic_stream:
                print("[JARVIS] Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] Mic error: {e}")
            raise

    async def _receive_audio(self):
        print("[JARVIS] Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():
                    self._clear_stall()

                    sru = getattr(response, "session_resumption_update", None)
                    server_content = getattr(response, "server_content", None)
                    if server_content is not None and getattr(
                            server_content, "interrupted", False):
                        self._handle_server_interruption()

                    # Audio first: a single server message can carry both PCM
                    # and a function call.
                    if response.data:
                        if not self._interrupted and not getattr(
                                self, "_output_unavailable", False):
                            self._last_audio_at = time.monotonic()
                            audio_data = response.data
                            _trace("rx", bytes=len(audio_data))
                            slice_bytes = 2400       # ~50 ms at 24 kHz / 16-bit mono
                            for i in range(0, len(audio_data), slice_bytes):
                                # Bounded queue: when the server generates far
                                # ahead of real time this awaits instead of
                                # piling minutes of speech in front of playback.
                                await self.audio_in_queue.put(
                                    audio_data[i:i + slice_bytes])

                    if sru is not None:
                        if getattr(sru, "resumable", False) and getattr(sru, "new_handle", None):
                            self._resume_handle = sru.new_handle

                    # Server content is handled on EVERY message, before the
                    # tool branch. A tool-call message can also carry the turn's
                    # transcription and its `turn_complete`; skipping that block
                    # for tool messages is what left the HUD speaking forever.
                    if server_content:
                        sc = server_content

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                # A new utterance after a STOP is a new turn.
                                self._maybe_end_interruption()
                                # A new spoken instruction releases the
                                # "task finished" execution lock.
                                self._agent_terminal_until_user = False
                                in_buf.append(txt)
                                # A recognised voice turn deserves the same
                                # no-response watchdog as a typed turn.
                                self._arm_stall()
                                self._last_user_speech = time.monotonic()
                                if not getattr(self, "_memory_turn_injected", False):
                                    spoken = " ".join(in_buf).strip()
                                    if len(spoken.split()) >= 2:
                                        self._memory_turn_injected = True
                                        injector = getattr(
                                            self, "_inject_voice_memory", None)
                                        if callable(injector):
                                            await injector(spoken)
                                if not getattr(
                                        self, "_task_context_turn_injected", False):
                                    task_injector = getattr(
                                        self, "_inject_task_context", None)
                                    if (callable(task_injector)
                                            and self._active_task_run() is not None):
                                        self._task_context_turn_injected = True
                                        await task_injector(
                                            "voice turn", force=True)

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            raw_txt = _clean_transcript_raw(
                                sc.output_transcription.text)
                            # Text from a reply the user stopped must not keep
                            # arriving in the chat after the interruption.
                            if (txt and not self._interrupted
                                    and not _is_repeat_chunk(txt, out_buf)):
                                self._flush_user_transcript(in_buf)
                                out_buf.append(txt)
                                self._notify_assistant_delta(raw_txt)

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()
                            # Turn is complete, clear the stall watchdog
                            self._clear_stall()
                            self._check_agent_duration()

                            if self._interrupted:
                                self._interrupted = False
                                self._interrupted_source = ""
                                in_buf = []
                                out_buf = []
                                self._memory_turn_injected = False
                                self._task_context_turn_injected = False
                                # A tool call can ride the same message as the
                                # turn-complete signal. It still has to be
                                # answered, or the model is left waiting.
                                if not response.tool_call:
                                    continue

                            user_text = " ".join(in_buf).strip()
                            if user_text:
                                self._maybe_end_phone_call(user_text)
                            self._flush_user_transcript(in_buf)

                            full_out = " ".join(out_buf).strip()
                            if full_out and len(full_out) >= _REPEAT_MIN and self._last_out_logged:
                                if full_out in self._last_out_logged:
                                    full_out = ""
                            if full_out:
                                self._last_out_logged = full_out
                                log_name = getattr(self.ui, "assistant_name",
                                                   self._asst_name)
                                self.ui.write_log(f"{log_name}: {full_out}")
                            # One extraction task per completed exchange. The
                            # deterministic turn id makes a replay a duplicate.
                            queue_extraction = getattr(
                                self, "_queue_memory_extraction", None)
                            if full_out and user_text and callable(queue_extraction):
                                queue_extraction(user_text, full_out)
                            out_buf = []
                            self._memory_turn_injected = False
                            self._task_context_turn_injected = False
                            self._notify_turn_complete(suppress=not full_out)

                    # Tool call handling. Gemini speaks the acknowledgement
                    # itself (the prompt asks for one before every tool call);
                    # the app never substitutes a local voice.
                    if response.tool_call:
                        calls = list(response.tool_call.function_calls or [])
                        if calls:
                            # Dispatch off the receive loop so interruption,
                            # transcription and resumption updates keep flowing.
                            # The chain preserves model call order and sends each
                            # FunctionResponse on the session that owned it.
                            self._schedule_tool_dispatch(calls)
                        else:
                            # A tool_call message with no calls in it has
                            # nothing to answer; say so instead of sending the
                            # empty response the SDK rejects.
                            print("[JARVIS] Tool call message carried no "
                                  "function calls.")
        except Exception as e:
            print(f"[JARVIS] Recv error: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] Play started")

        spk_name = get_output_device()
        spk_dev = await asyncio.to_thread(
            audio_devices.resolve, spk_name, "output")
        if spk_dev is not None:
            print(f"[JARVIS] Output device: {spk_name}")
        elif spk_name:
            self.ui.write_log(
                f"SYS: Speaker '{spk_name}' is not connected — using the "
                "system default.")
            print(f"[JARVIS] Output device '{spk_name}' not found")

        def _open_spk(dev):
            kwargs = dict(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            try:
                st = sd.RawOutputStream(latency=_OUTPUT_LATENCY_S, **kwargs)
            except Exception:
                # Not every host API accepts a latency hint; the default is
                # still workable, just with less headroom.
                st = sd.RawOutputStream(**kwargs)
            st.start()
            return st

        try:
            stream = await asyncio.to_thread(_open_spk, spk_dev)
        except Exception as e:
            try:
                if spk_dev is not None:
                    print(f"[JARVIS] Output device '{spk_name}' failed: {e} — using default")
                    self.ui.write_log(f"SYS: Speaker '{spk_name}' unavailable — using system default.")
                stream = await asyncio.to_thread(_open_spk, None)
            except Exception as fallback_error:
                print(f"[JARVIS] Speaker could not be opened: {fallback_error}")
                self._output_unavailable = True
                self.ui.write_log(
                    "SYS: Audio output is unavailable — check the selected speaker in Settings.")
                return

        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            self._dev_last = time.monotonic()
            self._out_stream = stream
            self._output_aborted = False
            self._output_unavailable = False
            print(f"[JARVIS] Output latency {self._out_latency*1000:.0f} ms "
                  f"-> echo tail {(self._out_latency + _TAIL_MARGIN)*1000:.0f} ms")
        except Exception:
            pass

        async def restart_output() -> bool:
            """Reopen after an aborted or stopped device stream."""
            nonlocal stream
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            try:
                stream = await asyncio.to_thread(_open_spk, spk_dev)
                self._out_stream = stream
                self._output_aborted = False
                return True
            except Exception as reopen_error:
                print(f"[JARVIS] Speaker reopen failed: {reopen_error}")
                return False

        reopened = False
        try:
            while True:
                if getattr(self, "_output_aborted", False):
                    # Barge-in aborted the stream. Wait until here (after any
                    # in-flight write has unwound) before starting it again.
                    self._output_aborted = False
                    try:
                        stream.start()
                    except Exception:
                        if not await restart_output():
                            self.ui.write_log(
                                "SYS: Speaker could not be reopened — audio output is unavailable.")
                            return
                _t_get = time.monotonic()
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    _trace("get_timeout",
                           depth=self.audio_in_queue.qsize())
                    # One check, two independent signals: the turn-complete
                    # event (after a short grace) or silence on the audio clock
                    # (after the longer bound). Guarded by `_is_speaking` so a
                    # repeated call cannot keep pushing the microphone's echo
                    # tail forward.
                    if self._is_speaking and self._should_stop_speaking(
                            time.monotonic()):
                        if self._turn_done_event is not None:
                            self._turn_done_event.clear()
                        self.set_speaking(False)
                    continue
                _trace("get", wait=round(time.monotonic() - _t_get, 6),
                       depth=self.audio_in_queue.qsize())

                # A chunk may already have been taken from the queue when the
                # user pressed STOP. Do not play it after draining the rest.
                if self._interrupted:
                    continue

                # Jitter buffer: hold until a reserve is playing/queued ahead
                # of the speaker. This is what turns bursty delivery into
                # steady speech instead of "stops, then continues". A phone
                # call skips the device reserve: the RTP sender is the clock and
                # has its own bounded queue, so adding the speaker's buffer
                # would only add latency to the caller.
                phone_active = self._telephony.bridge_active()
                if not phone_active:
                    await self._wait_for_prebuffer()
                if self._interrupted:
                    continue

                self.set_speaking(True)

                batch = bytearray(chunk)
                while len(batch) < 9600:      # ~200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    hop = _LEVEL_HOP / RECEIVE_SAMPLE_RATE
                    levels = _pcm_levels(pcm)
                    now = time.time()
                    horizon = self._out_latency + _CURSOR_SLACK
                    if not (now <= self._play_cursor <= now + horizon):
                        self._play_cursor = now + _FIRST_SOUND
                    at = self._play_cursor
                    self._play_cursor += pcm.size / RECEIVE_SAMPLE_RATE
                    if levels:
                        self.ui.push_levels(levels, hop, at)
                        self._out_level = max(levels)
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE,
                                               self._out_level)
                    else:
                        lvl = _pcm_level(pcm)
                        self.ui.set_audio_level(lvl)
                        self._out_level = lvl
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    pass

                if self._telephony.bridge_active():
                    # 24 kHz Gemini PCM -> the SIP engine resamples to G.711
                    # and sends one RTP packet every 20 ms. Pace this loop to
                    # the sender's queue so a fast model cannot build a
                    # multi-second telephone delay.
                    while (self._telephony.bridge_active()
                           and not self._interrupted
                           and self._telephony.buffered_seconds() > 0.55):
                        await asyncio.sleep(0.02)
                    if not self._telephony.bridge_active():
                        continue
                    self._phone_tx_batches += 1
                    sent = self._telephony.send_audio_24k(bytes(batch))
                    if sent:
                        self._phone_tx_bytes += len(batch)
                        try:
                            self._phone_echo.note_output(
                                pcm, RECEIVE_SAMPLE_RATE, self._out_level)
                        except Exception:
                            pass
                    else:
                        self._phone_tx_dropped += len(batch)
                    if self._phone_trace and (
                            self._phone_tx_batches == 1
                            or self._phone_tx_batches % 5 == 0):
                        print(
                            f"[PHONE] Jarvis tx batches={self._phone_tx_batches} "
                            f"generated={self._phone_tx_bytes + self._phone_tx_dropped}B "
                            f"sent={self._phone_tx_bytes}B "
                            f"dropped={self._phone_tx_dropped}B "
                            f"buffered={self._telephony.buffered_seconds():.2f}s",
                            flush=True)
                    self._dev_buffer = 0.0
                    self._dev_last = time.monotonic()
                    _trace("phone", bytes=len(batch),
                           buffered=round(self._telephony.buffered_seconds(), 3))
                    while (self._telephony.bridge_active()
                           and not self._interrupted
                           and self._telephony.buffered_seconds() > 0.55):
                        await asyncio.sleep(0.02)
                    continue

                _trace("batch", bytes=len(batch), depth=self.audio_in_queue.qsize())
                _t_wr = time.monotonic()
                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                except asyncio.CancelledError:
                    break
                except RuntimeError as exc:
                    if getattr(self, "_output_aborted", False):
                        self._output_aborted = False
                        try:
                            stream.start()
                        except Exception:
                            if not await restart_output():
                                return
                        continue
                    print(f"[JARVIS] Playback stream error: {exc}")
                    break
                except sd.PortAudioError as exc:
                    if getattr(self, "_output_aborted", False):
                        # abort() from the barge-in path unblocks write with a
                        # PortAudio error. Start the same stream again; this is
                        # not a device failure.
                        self._output_aborted = False
                        try:
                            stream.start()
                        except Exception:
                            if not await restart_output():
                                return
                        continue
                    # MME can be interrupted by device enumeration, USB audio
                    # changes or another host opening the endpoint. Recover the
                    # stream instead of tearing down the whole Gemini session.
                    if reopened:
                        print(f"[JARVIS] Speaker output failed again: {exc}")
                        self._output_unavailable = True
                        self.ui.write_log(
                            "SYS: Speaker output failed — check the audio device in Settings.")
                        return
                    reopened = True
                    print(f"[JARVIS] Speaker interrupted: {exc} — reopening")
                    self.ui.write_log("SYS: Speaker interrupted — reopening audio output.")
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass
                    try:
                        await asyncio.to_thread(audio_devices.wait_prefetch)
                        stream = await asyncio.to_thread(_open_spk, spk_dev)
                        self._out_stream = stream
                        self._output_aborted = False
                    except Exception as reopen_error:
                        print(f"[JARVIS] Speaker reopen failed: {reopen_error}")
                        self._output_unavailable = True
                        self.ui.write_log(
                            "SYS: Speaker could not be reopened — audio output is unavailable.")
                        return
                _trace("wr", dur=round(time.monotonic() - _t_wr, 6),
                       bytes=len(batch))
                self._dev_buffer = min(
                    _OUTPUT_LATENCY_S,
                    self._dev_buffer + len(batch) / (RECEIVE_SAMPLE_RATE * 2))
        except Exception as e:
            print(f"[JARVIS] Play error: {e}")
            raise
        finally:
            self.set_speaking(False)
            if getattr(self, "_out_stream", None) is stream:
                self._out_stream = None
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    # ── Session transport ────────────────────────────────────────────────────

    @asynccontextmanager
    async def _open_session(self, live_model: str, config):
        """Open the one conversation this app supports: Gemini Live."""
        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version":
                          "v1alpha" if self._enhanced_live else "v1beta"},
        )
        # DNS and TCP setup can hang far longer than a voice turn should wait;
        # a bounded connect turns that into a fast, retryable failure.
        connection = client.aio.live.connect(model=live_model, config=config)
        try:
            session = await asyncio.wait_for(connection.__aenter__(),
                                             timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                await connection.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                await client.aio.aclose()
            except Exception:
                pass
            raise TimeoutError(
                f"Live connection did not open within "
                f"{_CONNECT_TIMEOUT:.0f}s.") from None
        try:
            yield session
        finally:
            try:
                await connection.__aexit__(None, None, None)
            except Exception:
                pass
            # A reconnect builds a fresh client each time. Release its HTTP
            # transport instead of waiting for garbage collection, or a long
            # session that reconnects repeatedly accumulates sockets.
            try:
                await client.aio.aclose()
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask
                print(f"[JARVIS] Client cleanup failed: {exc}")

    async def _wait_for_key_ready(self) -> None:
        """Wait for the setup UI to accept a key, but never past shutdown."""
        while not self.ui._win._ready:
            if self._shutdown:
                return
            await asyncio.sleep(1)

    async def run(self):
        global LIVE_MODEL
        self._loop = asyncio.get_event_loop()
        # The screen-understanding action streams frames into THIS loop's
        # session; registering the engine lets it follow every rebuild of
        # that session without owning it.
        try:
            screen_understanding_tool.set_engine(self)
        except Exception as exc:                     # noqa: BLE001
            print(f"[JARVIS] Screen tool registration failed: {exc}")

        self._reconnect_event = asyncio.Event()

        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)
        audio_devices.prefetch()
        # Catalogue discovery is a Windows PowerShell call (~1.2 s cold).
        # Warm it in the background so it does not delay the first connect.
        def _warm_catalog() -> None:
            try:
                _open_app_tool.prefetch()
            except Exception:
                pass

        threading.Thread(target=_warm_catalog, daemon=True,
                         name="app-catalog-prefetch").start()

        while True:
            if self._shutdown:
                break
            # Defined before the try: if UI initialisation or config building
            # raises, the recovery block must still see a usable model/handle
            # state instead of raising UnboundLocalError and killing the loop.
            live_model = LIVE_MODEL
            resumed_with = False
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")

                LIVE_MODEL = _gemini.live_model()
                live_model = LIVE_MODEL
                self.model = live_model
                print(f"[JARVIS] Model: {live_model}")

                resumed_with = self._resume_handle is not None
                config = self._build_config()

                async with (
                    self._open_session(live_model, config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    # Every user turn arms the stall watchdog, including typed
                    # turns and every future call site, by wrapping the session
                    # once instead of instrumenting each send.
                    orig_send = session.send_client_content

                    async def _send_and_arm(*a, _orig_send=orig_send, **kw):
                        result = await _orig_send(*a, **kw)
                        self._arm_stall()
                        return result

                    session.send_client_content = _send_and_arm

                    self.session = session
                    self._session_generation = int(
                        getattr(self, "_session_generation", 0)) + 1
                    self._tool_chain = None
                    self._tool_pending = 0
                    self._tool_in_progress = False
                    try:
                        self._telephony.set_voice_online(True)
                    except Exception:
                        pass
                    self.audio_in_queue = asyncio.Queue(
                        maxsize=_AUDIO_QUEUE_MAX)
                    self.out_queue = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()
                    self._rx_deadline = None
                    self._interrupted = False
                    self._interrupted_source = ""
                    # A connected session starts a fresh backoff sequence; a
                    # voluntary reconnect sets this to 0 so the rebuild is
                    # immediate.
                    self._conn_backoff = 1

                    print("[JARVIS] Connected.")
                    self.last_connection_error = ""
                    if resumed_with:
                        self.ui.write_log("SYS: Reconnected — conversation restored.")

                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: Online. Speak or type.")

                    self._reconnect_event.clear()
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._watch_stall())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                if self._shutdown:
                    break
                try:
                    self._telephony.set_voice_online(False)
                except Exception:
                    pass
                if _is_reconnect_signal(e):
                    print("[JARVIS] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        self._resume_handle = None
                        self._last_out_logged = ""
                        previous_conversation = self._conversation_id
                        self._conversation_id = uuid.uuid4().hex
                        self._cancel_agent_run("conversation restarted")
                        approvals = getattr(self, "_approvals", None)
                        if approvals is not None:
                            try:
                                approvals.set_session(self._conversation_id)
                            except Exception:
                                pass
                        service = getattr(self, "_agent_runs", None)
                        if service is not None:
                            try:
                                service.set_conversation_id(
                                    self._conversation_id)
                            except Exception:
                                pass
                        self._reset_skill_scope(previous_conversation)
                    self._conn_backoff = 0
                    continue

                if resumed_with and not _is_api_key_error(str(e)) and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[JARVIS] Resumption handle rejected — starting fresh")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._last_out_logged = ""
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                if is_net_err:
                    self.last_connection_error = (
                        "Network/DNS unavailable — check the connection "
                        "(a VPN may be required).")
                else:
                    self.last_connection_error = (
                        f"{type(e).__name__}: {err_str[:160]}")
                    traceback.print_exc()

                # Key problems are configuration, not model capability. Handle
                # them before the feature-degradation branches: an invalid key
                # arrives as INVALID_ARGUMENT and used to disable proactive
                # audio and the link tuning before the setup screen appeared.
                if not _get_api_key():
                    self.ui.write_log("SYS: No Gemini key — enter one to connect.")
                    self.ui.set_state("OFFLINE")
                    self.ui.prompt_reconfig()
                    await self._wait_for_key_ready()
                    self._conn_backoff = 1
                    continue

                if _is_api_key_error(err_str):
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("OFFLINE")
                    self.ui.prompt_reconfig()
                    await self._wait_for_key_ready()
                    print("[JARVIS] New API key saved — reconnecting...")
                    self._conn_backoff = 1
                    continue

                if _gemini.note_live_failure(live_model, err_str):
                    nxt = _gemini.live_model()
                    self.ui.write_log(
                        f"SYS: Switching to {nxt.split('/')[-1]} — the previous "
                        f"model is out of quota."
                        if nxt != live_model else
                        "SYS: Every live model is rate-limited — retrying.")
                    self._conn_backoff = 0 if nxt != live_model else 2
                    if nxt == live_model:
                        await asyncio.sleep(self._conn_backoff)
                    continue

                if self._tuned_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                    or "realtime_input" in err_str.lower()
                    or "thinking" in err_str.lower()
                ):
                    self._tuned_live = False
                    print("[JARVIS] Live tuning rejected — reconnecting without it.")
                    continue

                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it.")
                    continue

                if is_net_err:
                    # Never turn a deliberate reconnect (backoff 0) into a
                    # tight retry loop when the network itself is down.
                    self._conn_backoff = _network_backoff(
                        getattr(self, "_conn_backoff", 1))
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {self._conn_backoff}s. "
                        "(a VPN may be required)")
                else:
                    self._conn_backoff = 1
            finally:
                for task in list(getattr(self, "_tool_tasks", ())):
                    try:
                        task.cancel()
                    except Exception:
                        pass
                self._tool_chain = None
                self._tool_pending = 0
                self._tool_in_progress = False
                self.session = None

            self.set_speaking(False)
            self.ui.set_state("OFFLINE")

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

        # A shutdown break skips the per-iteration cleanup below.
        self.set_speaking(False)
        try:
            self.ui.set_state("OFFLINE")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    """Launch the local web interface."""
    args = list(_sys.argv[1:] if argv is None else argv)

    from web.launch import main as launch_web

    return launch_web(args)


if __name__ == "__main__":
    # The web server imports `main` for JarvisLive.  When this file is executed
    # as a script, alias the __main__ module so Python does not load a second
    # copy of the module (which would create a second tool registry view).
    _sys.modules.setdefault("main", _sys.modules[__name__])
    raise SystemExit(main())

"""The web counterpart of the PyQt facade in ``ui.py``.

``main.JarvisLive`` was deliberately written against a small duck-typed UI
surface: ``write_log``, ``set_state``, ``tool_event``, ``muted``, the callback
attributes, and ``_win._ready``.  This class implements exactly that surface and
turns each call into a JSON event for the browser.  No conversation, provider or
tool logic is duplicated here.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Optional

from config import get_assistant_name
from web.events import EventHub


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _level_of_log(text: str) -> str:
    if text.startswith(("ERR:", "NET:")):
        return "error"
    if text.startswith("TOOL:"):
        return "tool"
    if text.startswith("SYS:"):
        return "system"
    if text.startswith("You:") or ": " in text[:40]:
        return "chat"
    return "info"


class WebUI:
    """UI adapter used by ``JarvisLive`` in web mode."""

    # Audio levels are the fastest stream in the app.  At 20 Hz the halo is
    # perfectly smooth and the WebSocket stays cheap even with several tabs.
    AUDIO_INTERVAL = 0.05

    def __init__(self, hub: EventHub | None = None) -> None:
        self.hub = hub or EventHub()
        self._lock = threading.RLock()
        self._audio_lock = threading.Lock()

        self._assistant_name = get_assistant_name()
        self._muted = False
        self._state = "INITIALISING"
        self._conversation_id = uuid.uuid4().hex
        self._conversation_title = ""

        # JarvisLive checks `ui._win._ready` before waiting for a new key.
        # Pointing _win at this object keeps that existing contract intact.
        self._win = self
        self._ready = False
        self._ready_event = threading.Event()
        self._shutdown_event = threading.Event()

        self._messages: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._logs: Deque[Dict[str, Any]] = deque(maxlen=600)
        self._tool_rows: Dict[str, Dict[str, Any]] = {}
        self._agent_run: Dict[str, Any] | None = None
        self._observation: Dict[str, Any] | None = None

        self._assistant_stream: Dict[str, Any] | None = None
        self._assistant_started_at = 0.0
        self._turn_started_at = 0.0
        self._last_response_latency_ms: int | None = None
        self._next_seq = 0
        self._recent_typed: Dict[str, float] = {}
        self._last_audio_emit = 0.0
        self._last_test_emit = 0.0

        self.on_text_command: Optional[Callable[[str], Any]] = None
        self.on_interrupt: Optional[Callable[[], Any]] = None
        self.on_voice_change: Optional[Callable[[], Any]] = None
        self.on_audio_device_change: Optional[Callable[[], Any]] = None
        self.on_push_to_talk: Optional[Callable[[bool], Any]] = None
        self.ptt_hold: Optional[Callable[[bool], Any]] = None

    # ── readiness / setup ────────────────────────────────────────────────────

    def mark_ready(self, ready: bool = True) -> None:
        with self._lock:
            self._ready = bool(ready)
            if ready:
                self._ready_event.set()
            else:
                self._ready_event.clear()

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    def wait_for_api_key(self) -> bool:
        """Block until a key is configured.  False means shutting down."""
        while not self._shutdown_event.is_set():
            if self._ready_event.wait(0.2):
                return not self._shutdown_event.is_set()
        return False

    def prompt_reconfig(self) -> None:
        self.mark_ready(False)
        self._emit({"type": "setup.required", "ts": _now_iso()})

    def shutdown(self) -> None:
        self._shutdown_event.set()
        self._ready_event.set()

    @property
    def stopping(self) -> bool:
        return self._shutdown_event.is_set()

    # ── properties JarvisLive reads or assigns ───────────────────────────────

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        with self._lock:
            self._muted = bool(value)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def assistant_name(self) -> str:
        with self._lock:
            return self._assistant_name

    @property
    def last_response_latency_ms(self) -> int | None:
        with self._lock:
            return self._last_response_latency_ms

    @property
    def conversation_id(self) -> str:
        with self._lock:
            return self._conversation_id

    @property
    def conversation_title(self) -> str:
        with self._lock:
            return self._conversation_title

    def set_assistant_name(self, name: str) -> None:
        cleaned = (name or "").strip() or "Jarvis"
        with self._lock:
            self._assistant_name = cleaned

    # ── audio / state events ─────────────────────────────────────────────────

    def set_audio_level(self, level: float) -> None:
        self._emit_audio(level, "input")

    def push_levels(self, levels, hop: float, at: float) -> None:
        try:
            level = max(float(v) for v in levels) if levels else 0.0
        except Exception:
            level = 0.0
        self._emit_audio(level, "output")

    def _emit_audio(self, level: float, source: str) -> None:
        now = time.monotonic()
        with self._audio_lock:
            if now - self._last_audio_emit < self.AUDIO_INTERVAL:
                return
            self._last_audio_emit = now
        try:
            value = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            value = 0.0
        self._emit({"type": "audio", "source": source,
                    "level": round(value, 3), "ts": _now_iso()})

    def emit_audio_test_level(self, kind: str, level: float) -> None:
        now = time.monotonic()
        with self._audio_lock:
            if now - self._last_test_emit < self.AUDIO_INTERVAL:
                return
            self._last_test_emit = now
        try:
            value = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            value = 0.0
        self._emit({"type": "audio.test", "kind": kind,
                    "level": round(value, 3), "ts": _now_iso()})

    def set_state(self, state: str) -> None:
        text = str(state or "LISTENING").upper()
        with self._lock:
            if self._muted and text == "LISTENING":
                text = "MUTED"
            changed = text != self._state
            self._state = text
            muted = self._muted
        if text == "OFFLINE":
            self.cancel_assistant_stream()
        if changed or text == "OFFLINE":
            self._emit({"type": "state", "state": text, "muted": muted,
                        "ts": _now_iso()})

    def start_speaking(self) -> None:
        self.set_state("SPEAKING")

    def stop_speaking(self) -> None:
        if not self.muted:
            self.set_state("LISTENING")

    def set_muted(self, muted: bool) -> None:
        value = bool(muted)
        with self._lock:
            if value == self._muted:
                return
            self._muted = value
            offline = self._state == "OFFLINE"
        # Mute is an input state, not a link state.  Turning the microphone on
        # or off while the session is offline must not make the interface claim
        # the Gemini link is listening.
        if not offline:
            if value:
                self.set_state("MUTED")
            else:
                self.set_state("LISTENING")
        if value:
            self.write_log("SYS: Microphone muted.")
        else:
            self.write_log("SYS: Microphone active.")

    # ── log / chat events ────────────────────────────────────────────────────

    def write_log(self, text: str) -> None:
        message = str(text)
        entry = {"id": uuid.uuid4().hex, "ts": _now_iso(),
                 "text": message, "level": _level_of_log(message)}
        with self._lock:
            self._logs.append(entry)
        self._emit({"type": "log", "entry": entry})

        role, body = self._role_from_log(message)
        if role == "assistant":
            self._finalize_assistant(body)
        elif role == "user":
            if not self._consume_typed(message_text=body):
                self._append_message({
                    "id": uuid.uuid4().hex,
                    "role": "user",
                    "text": body,
                    "ts": _now_iso(),
                    "source": "voice",
                })

    def add_user_message(self, text: str, message_id: str | None = None,
                         source: str = "text") -> Dict[str, Any]:
        body = str(text).strip()
        msg = {
            "id": message_id or uuid.uuid4().hex,
            "role": "user",
            "text": body,
            "ts": _now_iso(),
            "source": source,
        }
        with self._lock:
            # A retried HTTP request with the same client id is the same turn,
            # not a second one.
            for existing in self._messages:
                if existing.get("id") == msg["id"]:
                    return dict(existing)
            now = time.monotonic()
            # Entries are normally pruned while consuming a transcription, but
            # a long typed-only session never reaches that path. Prune here too
            # so the map cannot grow for the lifetime of the process.
            for key, stamp in list(self._recent_typed.items()):
                if now - stamp > 20:
                    self._recent_typed.pop(key, None)
            self._recent_typed[self._norm(body)] = now
            entry = {
                "id": uuid.uuid4().hex,
                "ts": _now_iso(),
                "text": f"You: {body}",
                "level": "chat",
            }
            self._logs.append(entry)
        self._emit({"type": "log", "entry": entry})
        return self._append_message(msg)

    def stream_assistant_delta(self, text: str) -> None:
        chunk = str(text or "")
        if not chunk:
            return
        with self._lock:
            if self._assistant_stream is None:
                self._assistant_stream = {
                    "id": uuid.uuid4().hex,
                    "text": "",
                    "started_at": _now_iso(),
                    "seq": self._next_seq,
                }
                self._next_seq += 1
                self._assistant_started_at = time.monotonic()
                if self._turn_started_at:
                    self._last_response_latency_ms = max(
                        0, round((self._assistant_started_at
                                  - self._turn_started_at) * 1000))
                    self._turn_started_at = 0.0
            self._assistant_stream["text"] += chunk
            message_id = self._assistant_stream["id"]
        self._emit({"type": "chat.delta", "id": message_id,
                    "role": "assistant", "text": chunk, "ts": _now_iso()})

    def turn_complete(self, suppress: bool = False) -> None:
        """Close a partial assistant stream if the final log never arrived."""
        if suppress:
            # A suppressed turn carries no assistant text.
            self.discard_assistant_stream()
            return
        with self._lock:
            stream = self._assistant_stream
        if stream and str(stream.get("text") or "").strip():
            self._finalize_assistant(str(stream["text"]).strip())
        else:
            self.cancel_assistant_stream()

    def _prune_typed_after_reply_locked(self) -> None:
        """Forget typed-turn markers the completed reply has now passed.

        The marker exists to suppress the transcription echo of a typed turn.
        Once a reply that started after that turn has completed, a later
        ``You:`` with the same text is a new utterance, not an echo. Leaving
        the 20-second marker in place made a voice turn disappear.
        """
        cutoff = self._assistant_started_at
        if not cutoff:
            return
        for key, stamp in list(self._recent_typed.items()):
            if stamp <= cutoff:
                self._recent_typed.pop(key, None)

    def discard_assistant_stream(self) -> None:
        """Drop a streamed reply the session classified as a duplicate."""
        with self._lock:
            self._prune_typed_after_reply_locked()
            stream = self._assistant_stream
            self._assistant_stream = None
        if stream is not None:
            self._emit({"type": "chat.discarded", "id": stream["id"],
                        "ts": _now_iso()})

    def cancel_assistant_stream(self) -> None:
        with self._lock:
            self._prune_typed_after_reply_locked()
            stream = self._assistant_stream
            self._assistant_stream = None
        if stream is not None:
            self._emit({"type": "chat.cancelled", "id": stream["id"],
                        "ts": _now_iso()})

    def _finalize_assistant(self, text: str) -> None:
        body = str(text or "").strip()
        if not body:
            self.cancel_assistant_stream()
            return
        with self._lock:
            if self._assistant_stream is None:
                # No incremental transcript was exposed for this reply. Treat
                # the final log as its completion point for typed-turn markers.
                self._assistant_started_at = time.monotonic()
                if self._turn_started_at:
                    self._last_response_latency_ms = max(
                        0, round((self._assistant_started_at
                                  - self._turn_started_at) * 1000))
                    self._turn_started_at = 0.0
            self._prune_typed_after_reply_locked()
            stream = self._assistant_stream
            self._assistant_stream = None
            message_id = stream["id"] if stream else uuid.uuid4().hex
            sequence = stream.get("seq") if stream else None
        self._append_message({
            "id": message_id,
            "role": "assistant",
            "text": body,
            "ts": _now_iso(),
            "source": "voice",
            "streaming": False,
        }, sequence)

    def message_by_id(self, message_id: str | None) -> Dict[str, Any] | None:
        """Return one known row, for idempotent retries."""
        wanted = str(message_id or "")
        if not wanted:
            return None
        with self._lock:
            for message in self._messages:
                if message.get("id") == wanted:
                    return dict(message)
        return None

    def clear_conversation(self) -> None:
        """Start a new visible conversation.

        Used when the voice changes and the Live session is rebuilt without
        resumption: the server has no context for the old messages, so keeping
        them in the transcript would misrepresent what Jarvis can remember.
        """
        with self._lock:
            self._messages.clear()
            self._assistant_stream = None
            self._assistant_started_at = 0.0
            self._turn_started_at = 0.0
            self._last_response_latency_ms = None
            self._next_seq = 0
            self._recent_typed.clear()
            self._agent_run = None
            self._conversation_id = uuid.uuid4().hex
            self._conversation_title = ""
        self._emit({"type": "chat.cleared",
                    "conversation_id": self._conversation_id,
                    "ts": _now_iso()})

    def _append_message(self, message: Dict[str, Any],
                        sequence: int | None = None) -> Dict[str, Any]:
        with self._lock:
            if sequence is None:
                sequence = self._next_seq
                self._next_seq += 1
            else:
                self._next_seq = max(self._next_seq, sequence + 1)
            row = dict(message)
            row["seq"] = sequence
            if row.get("role") == "user" and not self._turn_started_at:
                self._turn_started_at = time.monotonic()
            # A reply can still be streaming when the user types the next turn.
            # Its final row belongs where the reply started, not after the
            # user's newer message, so a snapshot preserves the live order.
            if not self._messages or sequence >= self._messages[-1].get("seq", -1):
                self._messages.append(row)
            else:
                rows = list(self._messages)
                index = len(rows)
                for position, existing in enumerate(rows):
                    if existing.get("seq", -1) > sequence:
                        index = position
                        break
                rows.insert(index, row)
                self._messages = deque(rows, maxlen=self._messages.maxlen)
        self._emit({"type": "chat.message", "message": row})
        if row.get("role") == "user":
            text = str(row.get("text") or "")
            if text:
                with self._lock:
                    if not self._conversation_title:
                        self._conversation_title = " ".join(text.split())[:72]
        return row

    def _role_from_log(self, text: str) -> tuple[str, str]:
        if text.startswith("You: "):
            return "user", text[5:].strip()
        with self._lock:
            name = self._assistant_name
        prefix = f"{name}: "
        if text.startswith(prefix):
            return "assistant", text[len(prefix):].strip()
        return "", ""

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip().casefold()

    def _consume_typed(self, message_text: str) -> bool:
        """A typed turn is also transcribed back; do not show it twice."""
        now = time.monotonic()
        wanted = self._norm(message_text)
        with self._lock:
            for key, stamp in list(self._recent_typed.items()):
                if now - stamp > 20:
                    self._recent_typed.pop(key, None)
                    continue
                if wanted and wanted == key:
                    self._recent_typed.pop(key, None)
                    return True
        return False

    # ── tool events ──────────────────────────────────────────────────────────

    def tool_event(self, event: Dict[str, Any]) -> None:
        raw = dict(event or {})
        call_id = str(raw.get("id") or uuid.uuid4().hex)
        phase = str(raw.get("phase") or "")
        now = _now_iso()
        with self._lock:
            row = dict(self._tool_rows.get(call_id) or {
                "id": call_id,
                "tool": str(raw.get("tool") or ""),
                "target": str(raw.get("target") or ""),
                "started_at": now,
                "phase": phase,
            })
            row.update({
                "phase": phase,
                "tool": str(raw.get("tool") or row.get("tool") or ""),
                "target": str(raw.get("target") or row.get("target") or ""),
                "ms": int(raw.get("ms") or 0),
                "status": str(raw.get("status") or
                              ("running" if phase == "start" else "unknown")),
            })
            if phase == "end":
                row["ok"] = bool(raw.get("ok", False))
                row["summary"] = str(raw.get("summary") or "")
                row["pending"] = bool(raw.get("pending", False))
                row["ended_at"] = now
                if not raw.get("status"):
                    row["status"] = ("pending" if row["pending"]
                                     else "success" if row["ok"] else "failed")
            else:
                row.pop("ok", None)
                row.pop("summary", None)
                row.pop("pending", None)
            self._tool_rows[call_id] = row
            self._trim_tools_locked()
        self._emit({"type": "tool.updated", "event": row})

    def _trim_tools_locked(self) -> None:
        while len(self._tool_rows) > 40:
            self._tool_rows.pop(next(iter(self._tool_rows)), None)

    def clear_tool_events(self) -> None:
        with self._lock:
            self._tool_rows.clear()
        self._emit({"type": "tool.cleared", "ts": _now_iso()})

    # ── agent run events ─────────────────────────────────────────────────────

    def agent_event(self, event: Dict[str, Any]) -> None:
        """Publish one AgentRun snapshot on the existing WebSocket stream."""
        raw = event if isinstance(event, dict) else {}
        run = raw.get("run")
        if isinstance(run, dict):
            with self._lock:
                self._agent_run = dict(run)
        run_id = str((run or {}).get("run_id") or "") if isinstance(
            run, dict) else ""
        payload: Dict[str, Any] = {
            "type": "agent.updated",
            "event": str(raw.get("event") or "updated"),
            "run_id": run_id,
            "run": dict(run) if isinstance(run, dict) else None,
            "ts": _now_iso(),
        }
        if raw.get("error"):
            payload["error"] = str(raw["error"])
        if raw.get("limit"):
            payload["limit"] = str(raw["limit"])
        self._emit(payload)

    # ── screen observation ───────────────────────────────────────────────────

    def observation_event(self, snapshot: Dict[str, Any]) -> None:
        """Publish the authoritative continuous-observation state."""
        data = dict(snapshot) if isinstance(snapshot, dict) else {}
        with self._lock:
            self._observation = data
        self._emit({
            "type": "observation.updated",
            "observation": data,
            "state": str(data.get("state") or ""),
            "active": bool(data.get("observation_active")),
            "ts": _now_iso(),
        })

    # ── action approval events ───────────────────────────────────────────────

    def approval_event(self, event: Dict[str, Any]) -> None:
        """Publish one approval state change on the existing WebSocket."""
        raw = event if isinstance(event, dict) else {}
        approval = raw.get("approval")
        public = dict(approval) if isinstance(approval, dict) else None
        payload: Dict[str, Any] = {
            "type": "approval.updated",
            "event": str(raw.get("event") or "updated"),
            "approval": public,
            "ts": _now_iso(),
        }
        status = str(payload["event"] or "")
        if public and status in (
                "approved", "rejected", "expired", "revoked", "consumed"):
            summary = str(public.get("summary") or public.get("target") or "")
            self.write_log(
                f"SYS: Approval {status}"
                + (f" - {summary}" if summary else ""))
        self._emit(payload)

    # ── snapshots ────────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            messages = [dict(item) for item in self._messages]
            logs = [dict(item) for item in self._logs]
            tools = [dict(item) for item in self._tool_rows.values()]
            return {
                "state": self._state,
                "muted": self._muted,
                "assistant_name": self._assistant_name,
                "ready": self._ready,
                "conversation_id": self._conversation_id,
                "conversation_title": self._conversation_title,
                "messages": messages,
                "logs": logs,
                "tool_events": tools,
                "agent_run": dict(self._agent_run) if self._agent_run else None,
                "observation": dict(self._observation)
                if self._observation else None,
            }

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _emit(self, event: Dict[str, Any]) -> None:
        try:
            self.hub.publish(event)
        except Exception:
            # A broken browser connection must never affect the session.
            pass

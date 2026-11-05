"""Application runtime for the local web UI.

This is the boundary between the HTTP/WebSocket layer and Jarvis's existing
services.  It never re-implements conversation logic: it creates the same
``main.JarvisLive`` object the desktop app used, with ``WebUI`` in place of the
Qt window, and forwards settings to the existing ``config`` module.
"""
from __future__ import annotations

import asyncio
import platform
import threading
import traceback
from typing import Any, Dict, List

import config
from actions import get_tool_module, tool_calling
from core import audio_devices
from core import approvals as _approvals
from core import notification_service as _notifications
from core import scheduler as _scheduler
from core import gemini as _gemini
from core import telephony as _telephony
from Skills.Engine import SkillsEngine
from Skills.Engine import get_engine as get_skills_engine
from web import __version__
from web.events import EventHub
from web.metrics import SysMetrics
from web.ui import WebUI, _now_iso
from web.weather import WeatherService


class RuntimeCommandError(Exception):
    """A command the backend refused, with the HTTP status to report."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _detected_os() -> str:
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux")


def _mask_key(key: str) -> str:
    value = str(key or "").strip()
    return ("•••• " + value[-4:]) if value else ""


_MODULE_DETAILS = {
    "actions": {
        "index": "01",
        "name": "ACTIONS",
        "description": ("Application, file and web tools are live in category "
                        "folders. External action packs dock here."),
        "glyph": "◈",
        "available": True,
    },
    "agents": {
        "index": "02",
        "name": "AGENTS",
        "description": ("Placeholder bay for installed agent packs. The live "
                        "objective run is in Agent Run."),
        "glyph": "◎",
        "available": False,
    },
    "skills": {
        "index": "03",
        "name": "SKILLS",
        "description": ("Installed instruction packages are discovered from the "
                        "top-level Skills directory and activated by the model "
                        "when a task benefits from them."),
        "glyph": "△",
        "available": True,
    },
    "plugins": {
        "index": "04",
        "name": "PLUGINS",
        "description": "Runtime extensions and device bridges dock in this bay.",
        "glyph": "▣",
        "available": False,
    },
    "connectors": {
        "index": "05",
        "name": "CONNECTORS",
        "description": "External service links will be provisioned from this bay.",
        "glyph": "⇄",
        "available": False,
    },
    "mcps": {
        "index": "06",
        "name": "MCPS",
        "description": "Model Context Protocol servers will appear in this bay.",
        "glyph": "⊞",
        "available": False,
    },
    "github": {
        "index": "07",
        "name": "GITHUB REPOS",
        "description": ("No GitHub integration is installed in this build. "
                        "Repository bays will appear here when provisioned."),
        "glyph": "⑂",
        "available": False,
    },
}
_MODULE_ORDER = ("actions", "agents", "skills", "plugins", "connectors",
                 "mcps", "github")


class AzeRuntime:
    def __init__(
        self,
        *,
        hub: EventHub | None = None,
        start_session: bool = True,
        weather_service: WeatherService | None = None,
        metrics: SysMetrics | None = None,
        skills_engine: SkillsEngine | None = None,
    ) -> None:
        self.hub = hub or EventHub()
        self.ui = WebUI(self.hub)
        self._approvals = _approvals.get_service()
        try:
            self._approvals.set_listener(self.ui.approval_event)
        except Exception:  # noqa: BLE001 - approvals must never stop startup
            traceback.print_exc()

        # Persistent reminders/timers and the one built-in notification path.
        self._notifications = _notifications.get_service()
        try:
            self._notifications.configure(
                publish=self.hub.publish, log=self.ui.write_log)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        self._scheduler = _scheduler.get_scheduler()
        try:
            self._scheduler.configure(
                notifier=self._notifications.create,
                log=self.ui.write_log)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        self.weather_service = weather_service or WeatherService()
        self.metrics = metrics or SysMetrics()
        self._start_session = bool(start_session)
        self._settings_lock = threading.RLock()
        self._engine_lock = threading.RLock()
        self._thread_lock = threading.Lock()
        self._audio_test_lock = threading.Lock()
        self._memory_lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._memory_service = None
        self._memory_error = ""
        self._engine = None
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._shutdown_started = False

        # The Skills Engine is filesystem-backed and independent of the Gemini
        # session. It is discovered before the first browser connects so the
        # bootstrap snapshot and the Skills panel always show real packages.
        self.skills_engine = skills_engine or get_skills_engine()
        try:
            self.skills_engine.set_event_sink(self._publish_skills)
            self.skills_engine.initialize()
        except Exception:  # noqa: BLE001 - a Skills fault never stops Jarvis
            traceback.print_exc()

        # The YouTube tool owns the player state; this is the one bridge
        # between it and the existing event hub. A tool call runs in a worker
        # thread and waits for the browser's correlated reply.
        self._youtube_tool = get_tool_module("youtube_player")
        if self._youtube_tool is not None:
            try:
                self._youtube_tool.configure(
                    publish=self.hub.publish,
                    has_clients=lambda: self.hub.client_count() > 0,
                )
            except Exception:  # noqa: BLE001 - the rest of Jarvis still starts
                traceback.print_exc()
                self._youtube_tool = None

        # Deep research publishes progressive report state over the same hub.
        self._research_tool = get_tool_module("research")
        if self._research_tool is not None:
            try:
                self._research_tool.get_service().configure(
                    publish=self.hub.publish,
                    log=self.ui.write_log,
                )
            except Exception:  # noqa: BLE001 - research is optional to startup
                traceback.print_exc()
                self._research_tool = None

        # Continuous screen observation owns the authoritative lifecycle
        # state; forward every transition to the existing dashboard stream.
        self._screen_tool = get_tool_module("screen_understanding")
        if self._screen_tool is not None:
            try:
                self._screen_tool.set_observation_listener(
                    self.ui.observation_event)
            except Exception:  # noqa: BLE001 - indicator is optional to startup
                traceback.print_exc()
                self._screen_tool = None

        # The telephony engine owns SIP/RTP and runs independently of the
        # Gemini session.  This wiring only forwards its events to the existing
        # UI hub and its human-readable notices to the activity log; the voice
        # bridge itself is configured by JarvisLive when the session starts.
        try:
            telephony = _telephony.get_service()
            telephony.configure(
                publish=self.hub.publish,
                log=self.ui.write_log,
            )
            # Register at startup so incoming calls can ring even when Jarvis
            # has not placed an outgoing call yet. Registration is a background
            # thread and a missing password simply leaves the service
            # unconfigured; it never blocks or breaks startup.
            sip_settings = telephony.settings()
            if not config.validate_telephony_settings(
                    sip_settings, require_password=True):
                threading.Thread(
                    target=telephony.start,
                    name="jarvis-sip-autostart",
                    daemon=True,
                ).start()
        except Exception:  # noqa: BLE001 - telephony never blocks startup
            traceback.print_exc()

        # Warm audio enumeration immediately: the readiness gate and the first
        # microphone open read this cache, and both should find it populated.
        try:
            audio_devices.prefetch()
        except Exception:
            pass

        if self._config_ready():
            self.ui.mark_ready(True)

    @staticmethod
    def _config_ready() -> bool:
        """The same first-run rule the desktop shell used.

        ``setup_done`` is authoritative; otherwise a key plus a recorded OS is
        enough (that was the older config shape).
        """
        data = config.load_api_keys()
        if data.get("setup_done"):
            return True
        key = str(data.get("gemini_api_key") or "").strip()
        return bool(key and data.get("os_system"))

    # ── skills events ────────────────────────────────────────────────────────

    def _publish_skills(self, snapshot: Dict[str, Any]) -> None:
        """Fan a real registry snapshot out to every open Skills panel."""
        self.hub.publish({
            "type": "skills.updated",
            "data": snapshot,
            "ts": _now_iso(),
        })

    def skills_snapshot(self) -> Dict[str, Any]:
        try:
            return self.skills_engine.snapshot()
        except Exception:  # noqa: BLE001 - the bootstrap must still load
            traceback.print_exc()
            return {
                "engine_version": "1.0.0",
                "initialized": False,
                "initialized_at": "",
                "active_scope": "",
                "skills": [],
                "active": [],
                "counts": {"total": 0, "enabled": 0, "active": 0,
                           "builtin": 0, "installed": 0, "disabled": 0,
                           "issues": 0},
                "errors": [],
                "state_file": "",
            }

    def skills_detail(self, skill_id: str) -> Dict[str, Any]:
        try:
            return self.skills_engine.detail(skill_id)
        except KeyError as exc:
            raise RuntimeCommandError(
                404, f"No Skill named '{skill_id}' is installed.") from exc
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            raise RuntimeCommandError(
                500, "The Skill package could not be inspected.") from exc

    def read_skill_asset(self, skill_id: str, path: str) -> Dict[str, Any]:
        result = self.skills_engine.read_asset(skill_id, path)
        if "error" in result:
            raise RuntimeCommandError(404, str(result["error"]))
        return result

    def refresh_skills(self) -> Dict[str, Any]:
        snapshot = self.skills_engine.refresh()
        counts = snapshot.get("counts", {})
        self.ui.write_log(
            f"SYS: Skills refreshed — {counts.get('enabled', 0)} enabled, "
            f"{counts.get('installed', 0)} installed, "
            f"{len(snapshot.get('errors', []))} issue(s).")
        return snapshot

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> Dict[str, Any]:
        try:
            snapshot = self.skills_engine.set_enabled(skill_id, enabled)
        except KeyError as exc:
            raise RuntimeCommandError(
                404, f"No Skill named '{skill_id}' is installed.") from exc
        self.ui.write_log(
            f"SYS: Skill {skill_id} {'enabled' if enabled else 'disabled'}.")
        return snapshot

    def uninstall_skill(self, skill_id: str, confirm: bool) -> Dict[str, Any]:
        result = self.skills_engine.uninstall(skill_id, confirm=confirm)
        if not result.get("ok"):
            raise RuntimeCommandError(
                409, str(result.get("error") or "The Skill was not removed."))
        self.ui.write_log(
            f"SYS: {result.get('message') or 'Installed Skill removed.'}")
        return {"ok": True, "message": result.get("message", ""),
                "snapshot": self.skills_engine.snapshot()}

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the existing Gemini Live loop in a daemon thread."""
        try:
            self._scheduler.start()
        except Exception:  # noqa: BLE001 - reminders never block the session
            traceback.print_exc()
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self._start_session or self._stopped:
                return
            self._thread = threading.Thread(
                target=self._session_main, name="aze-gemini-session",
                daemon=True)
            self._thread.start()

    def _session_main(self) -> None:
        try:
            # Existing behaviour: do not construct the live session until a
            # key exists.  The web server stays up and shows first-run setup.
            if not self.ui.wait_for_api_key():
                return
            if self._stopped:
                return

            from main import JarvisLive

            engine = JarvisLive(self.ui)
            with self._engine_lock:
                self._engine = engine
            print("[WEB] Conversation session thread started.")
            asyncio.run(engine.run())
        except asyncio.CancelledError:
            # ``stop()`` cancels the session task. This is an expected shutdown
            # path, not an error worth printing or reporting as a crash.
            pass
        except Exception as exc:  # noqa: BLE001 - report, never kill the server
            if not self._stopped:
                print(f"[WEB] Session thread ended: {type(exc).__name__}: {exc}")
                traceback.print_exc()
                try:
                    self.ui.write_log(
                        f"ERR: Session stopped: {type(exc).__name__}: {exc}")
                except Exception:
                    pass
        finally:
            with self._engine_lock:
                self._engine = None
            if not self._stopped:
                # Link state outranks microphone state: a muted session that
                # died must not keep showing MUTED as if it were healthy.
                try:
                    self.ui.set_state("OFFLINE")
                except Exception:
                    pass

    def stop(self) -> None:
        self._stopped = True
        try:
            self._scheduler.stop()
        except Exception:  # noqa: BLE001 - shutdown proceeds regardless
            traceback.print_exc()
        self.ui.shutdown()
        try:
            self._approvals.revoke_all("app shutdown")
        except Exception:  # noqa: BLE001 - shutdown proceeds regardless
            pass
        try:
            # Active Skills are runtime state, not installed state. Clear them
            # so a stopped process never leaves a stale active list in a panel.
            self.skills_engine.clear_all_active()
        except Exception:  # noqa: BLE001 - shutdown proceeds regardless
            pass
        with self._engine_lock:
            engine = self._engine
        if engine is not None:
            try:
                stop = getattr(engine, "stop", None)
                if callable(stop):
                    stop()
                else:
                    engine.request_reconnect(keep_context=True,
                                             reason="shutdown")
            except Exception:
                pass
        try:
            _telephony.shutdown()
        except Exception:
            pass
        try:
            from core import memory as _memory

            _memory.shutdown_default_service()
        except Exception:
            pass
        try:
            self.metrics.stop()
        except Exception:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=4.0)

    @property
    def engine(self):
        with self._engine_lock:
            return self._engine

    @property
    def session_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def session_connected(self) -> bool:
        engine = self.engine
        return bool(engine is not None and getattr(engine, "session", None))

    # ── snapshots ────────────────────────────────────────────────────────────

    @staticmethod
    def _setup_status() -> Dict[str, Any]:
        data = config.load_api_keys()
        key = str(data.get("gemini_api_key") or "").strip()
        return {
            "configured": bool(AzeRuntime._config_ready()),
            "setup_done": bool(data.get("setup_done")),
            "key_masked": _mask_key(key),
        }

    @staticmethod
    def _tool_catalog() -> Dict[str, Any]:
        """The one live view of category folders, tools and their actions."""
        return tool_calling.get_tool_catalog()

    @staticmethod
    def _tools() -> list[Dict[str, Any]]:
        return AzeRuntime._tool_catalog()["tools"]

    def select_tools(self, selectors: list[str]) -> Dict[str, Any]:
        """Enable exactly the selected tool modules (folders expand to tools).

        The Live API fixes its tool declarations at connection time, so a real
        change requests the same voluntary reconnect used for voice/audio
        settings. The selection itself is persisted by ``tool_calling`` through
        the single settings store.
        """
        try:
            catalog = tool_calling.set_tool_selection(selectors)
        except ValueError as exc:
            raise RuntimeCommandError(422, str(exc)) from exc
        self.ui.write_log(
            f"SYS: Tool selection updated — {catalog['enabled_count']} of "
            f"{catalog['count']} tool modules active.")
        engine = self.engine
        if engine is not None:
            try:
                engine.request_reconnect(
                    keep_context=True, reason="tool selection")
            except Exception:  # noqa: BLE001 - selection is already saved
                pass
        return catalog

    def _modules(self) -> list[Dict[str, Any]]:
        count = tool_calling.get_tool_count()
        rows = []
        for key in _MODULE_ORDER:
            item = dict(_MODULE_DETAILS[key])
            item["id"] = key
            if key == "actions":
                item["count"] = count
                noun = "TOOL" if count == 1 else "TOOLS"
                item["status"] = f"{count} CORE {noun} LINKED"
            elif key == "skills":
                snapshot = self.skills_snapshot()
                skill_count = int(snapshot.get("counts", {}).get("total", 0))
                active_count = int(snapshot.get("counts", {}).get("active", 0))
                noun = "SKILL" if skill_count == 1 else "SKILLS"
                item["count"] = skill_count
                item["status"] = (
                    f"{skill_count} {noun} LINKED · {active_count} ACTIVE")
                item["available"] = bool(
                    snapshot.get("initialized")) or skill_count > 0
            else:
                item["count"] = 0
                item["status"] = f"0 {item['name']} LINKED"
            rows.append(item)
        return rows

    def basic_settings(self) -> Dict[str, Any]:
        return {
            "assistant_name": config.get_assistant_name(),
            "user_name": config.get_user_name(),
            "voice_name": config.get_voice(),
            "voices": list(config.AVAILABLE_VOICES),
            "ui_color": config.get_ui_color(),
            "push_to_talk_enabled": config.get_push_to_talk_enabled(),
            "proactive_audio_enabled": config.get_proactive_audio_enabled(),
            "thinking_enabled": config.get_thinking_enabled(),
            "turn_tuning": config.get_turn_tuning(),
            "weather_location": config.get_weather_location(),
        }

    def settings(self, refresh_audio: bool = False) -> Dict[str, Any]:
        data = self.basic_settings()
        input_devices = audio_devices.list_devices("input", refresh=refresh_audio)
        output_devices = audio_devices.list_devices("output", refresh=refresh_audio)
        data.update({
            "input_device": config.get_input_device(),
            "output_device": config.get_output_device(),
            "input_devices": input_devices,
            "output_devices": output_devices,
            "output_volume": audio_devices.get_output_volume(
                config.get_output_device()),
            "gemini": self._setup_status(),
            "platform": platform.system(),
        })
        return data

    def bootstrap(self) -> Dict[str, Any]:
        snapshot = self.ui.snapshot()
        catalog = self._tool_catalog()
        return {
            "protocol_version": 1,
            "app": {
                "name": "Jarvis",
                "version": __version__,
                "platform": platform.system(),
            },
            "state": snapshot["state"],
            "muted": snapshot["muted"],
            "assistant_name": snapshot["assistant_name"],
            "ready": snapshot["ready"],
            "conversation_id": snapshot.get("conversation_id", ""),
            "conversation_title": snapshot.get("conversation_title", ""),
            "messages": snapshot["messages"],
            "logs": snapshot["logs"],
            "tool_events": snapshot["tool_events"],
            "agent_run": snapshot.get("agent_run"),
            "observation": self.observation_state(),
            "approvals": self._approvals.public_requests(),
            "notifications": self.notification_state(limit=50),
            "capabilities": self.capability_readiness(),
            "tools": catalog["tools"],
            "tool_categories": catalog["categories"],
            "modules": self._modules(),
            "skills": self.skills_snapshot(),
            "research": self.research_state(),
            "setup": self._setup_status(),
            "settings": self.basic_settings(),
            "youtube": self.youtube_state(),
            "server_time": _now_iso(),
        }

    # ── action approvals ─────────────────────────────────────────────────────

    def approve_action(self, approval_id: str) -> Dict[str, Any]:
        try:
            return self._approvals.approve(approval_id)
        except _approvals.ApprovalError as exc:
            status = 404 if exc.status == "not_found" else 409
            raise RuntimeCommandError(status, str(exc)) from exc

    def reject_action(self, approval_id: str) -> Dict[str, Any]:
        try:
            return self._approvals.reject(approval_id)
        except _approvals.ApprovalError as exc:
            status = 404 if exc.status == "not_found" else 409
            raise RuntimeCommandError(status, str(exc)) from exc

    # ── Research report bridge ───────────────────────────────────────────────

    def observation_state(self) -> Dict[str, Any]:
        """Authoritative continuous screen-observation snapshot."""
        module = getattr(self, "_screen_tool", None)
        if module is not None:
            try:
                return module.observation_snapshot()
            except Exception:  # noqa: BLE001 - a snapshot must never fail
                traceback.print_exc()
        return {
            "state": "INACTIVE",
            "observation_active": False,
            "started_at": None,
            "frames_streamed": 0,
            "frames_dropped": 0,
            "frame_interval_seconds": 0,
            "last_error": "",
            "voice_session_connected": False,
        }

    def research_state(self) -> Dict[str, Any]:
        module = self._research_tool
        if module is None:
            return {
                "open": False, "job_id": "", "status": "idle", "topic": "",
                "depth": "deep", "phase": "", "message": "", "progress": 0,
                "sources_found": 0, "sources_read": 0, "started_at": "",
                "updated_at": "", "report": None, "events": [], "error": "",
                "warnings": [], "cancelled": False,
            }
        try:
            return module.get_service().public_state()
        except Exception:  # noqa: BLE001 - a snapshot must never fail
            traceback.print_exc()
            return {
                "open": False, "job_id": "", "status": "idle", "topic": "",
                "depth": "deep", "phase": "", "message": "", "progress": 0,
                "sources_found": 0, "sources_read": 0, "started_at": "",
                "updated_at": "", "report": None, "events": [], "error": "",
                "warnings": [], "cancelled": False,
            }

    # ── YouTube player bridge ────────────────────────────────────────────────

    def youtube_state(self) -> Dict[str, Any]:
        """Current player state for a page load or a reconnect snapshot."""
        module = self._youtube_tool
        if module is None:
            return {"panel_open": False, "player_ready": False,
                    "playback_state": "closed"}
        try:
            return module.get_service().public_state()
        except Exception:  # noqa: BLE001 - a snapshot must never fail
            traceback.print_exc()
            return {"panel_open": False, "player_ready": False,
                    "playback_state": "closed"}

    def handle_youtube_event(self, payload: Dict[str, Any]) -> None:
        """Merge one browser message from the existing WebSocket.

        The handler is deliberately tiny: validation, state merging and
        request correlation live inside the tool so the HTTP/WebSocket layer
        keeps its existing single responsibility.
        """
        module = self._youtube_tool
        if module is None or not isinstance(payload, dict):
            return
        try:
            module.get_service().handle_client_event(payload)
        except Exception:  # noqa: BLE001 - a bad frame must not kill the socket
            traceback.print_exc()

    def handle_youtube_disconnect(self) -> None:
        """Fail in-flight player commands once the last client has gone."""
        module = self._youtube_tool
        if module is None or self.hub.client_count() > 0:
            return
        try:
            module.get_service().browser_disconnected()
        except Exception:  # noqa: BLE001 - disconnect cleanup is best effort
            traceback.print_exc()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def prepare_shutdown(self, reason: str = "") -> None:
        """Notify the browser and stop new work before the server exits.

        The launcher owns the actual uvicorn stop so this layer never has to
        import the server. The event is published first; the browser therefore
        has time to show the power-down screen before the tab is closed.
        """
        with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
        try:
            self.ui.write_log("SYS: Shutdown requested — powering down.")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        self._publish({
            "type": "shutdown",
            "reason": str(reason or "explicit user request")[:200],
            "ts": _now_iso(),
        })

    def close_interface_tab(self) -> bool:
        """Close the Jarvis tab in Brave, if it can be identified safely.

        Only a local tab whose title identifies Jarvis is eligible. If Brave
        is unavailable or no exact match exists, the browser overlay remains
        as the fallback; unrelated tabs are never closed.
        """
        module = get_tool_module("close_web")
        if module is None:
            return False
        try:
            tabs = module.get_open_tabs()
        except Exception as exc:  # noqa: BLE001 - Brave may not be running
            print(f"[WEB] Could not inspect Brave tabs for shutdown: {exc}")
            return False
        matches = []
        for tab in tabs or []:
            title = str(tab.get("title") or "").casefold()
            url = str(tab.get("url") or "").casefold()
            local = ("127.0.0.1" in url or "localhost" in url
                     or "::1" in url)
            if local and (
                "local neural interface" in title
                or "j.a.r.v.i.s" in title
                or "jarvis" in title
            ):
                matches.append(tab)
        if not matches:
            return False
        # Prefer the active Jarvis tab when several windows are open.
        matches.sort(key=lambda item: not bool(item.get("active")))
        try:
            result = module.close_tab(matches[0])
        except Exception:  # noqa: BLE001 - the overlay remains the fallback
            traceback.print_exc()
            return False
        return bool(result.get("success"))

    # ── text / voice controls ────────────────────────────────────────────────

    def submit_text(self, text: str, client_message_id: str | None = None) -> dict:
        body = str(text or "").strip()
        if not body:
            raise RuntimeCommandError(422, "The message is empty.")
        # A retried request with the same id must not become a second turn in
        # the conversation, even if the link dropped between the two attempts.
        existing = self.ui.message_by_id(client_message_id)
        if existing is not None:
            return existing
        engine = self.engine
        if engine is None:
            self.ui.write_log("SYS: Not connected yet.")
            raise RuntimeCommandError(409, "Jarvis is not connected yet.")
        try:
            accepted = bool(engine.send_text(body))
        except Exception:
            traceback.print_exc()
            accepted = False
        if not accepted:
            self.ui.write_log("SYS: Not connected yet.")
            raise RuntimeCommandError(409, "Jarvis is not connected yet.")
        return self.ui.add_user_message(body, client_message_id, source="text")

    def interrupt(self) -> None:
        engine = self.engine
        if engine is None or getattr(engine, "session", None) is None:
            raise RuntimeCommandError(409, "Jarvis is not connected yet.")
        try:
            engine.interrupt()
            self.ui.cancel_assistant_stream()
        except Exception:
            traceback.print_exc()
            raise RuntimeCommandError(500, "Could not stop the reply.")

    def set_muted(self, muted: bool) -> Dict[str, Any]:
        self.ui.set_muted(bool(muted))
        return {"muted": self.ui.muted, "state": self.ui.state}

    def set_ptt_hold(self, held: bool) -> None:
        callback = self.ui.ptt_hold
        if not callable(callback):
            raise RuntimeCommandError(409, "Push-to-talk is not available yet.")
        try:
            callback(bool(held))
        except Exception:
            traceback.print_exc()
            raise RuntimeCommandError(500, "Push-to-talk failed.")

    def request_reconnect(self) -> None:
        engine = self.engine
        if engine is None or getattr(engine, "session", None) is None:
            raise RuntimeCommandError(409, "Jarvis is not connected yet.")
        self.ui.write_log("SYS: Reconnect requested.")
        engine.request_reconnect(keep_context=True, reason="manual")

    # ── settings ─────────────────────────────────────────────────────────────

    def _request_reconnect(self, keep_context: bool, reason: str,
                           log_fallback: bool = True) -> bool:
        engine = self.engine
        if engine is not None and getattr(engine, "session", None) is not None:
            engine.request_reconnect(keep_context=keep_context, reason=reason)
            return True
        if log_fallback:
            self.ui.write_log(
                "SYS: Link profile saved — applies on the next connection.")
        return False

    def apply_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Apply one validated settings patch through the existing config module."""
        with self._settings_lock:
            try:
                self._apply_settings_locked(dict(patch or {}))
            except RuntimeCommandError:
                raise
            except Exception as exc:
                traceback.print_exc()
                raise RuntimeCommandError(
                    500, "Settings could not be saved.") from exc
            settings = self.settings()
        self._publish({"type": "settings.updated",
                       "settings": self.basic_settings(),
                       "setup": self._setup_status(),
                       "ts": _now_iso()})
        return settings

    def _apply_settings_locked(self, patch: Dict[str, Any]) -> None:
        if not patch:
            return

        # ── push-to-talk is applied before it is persisted, like the old UI ──
        if "push_to_talk_enabled" in patch:
            new_ptt = bool(patch["push_to_talk_enabled"])
            if new_ptt != config.get_push_to_talk_enabled():
                engine = self.engine
                if engine is None:
                    if new_ptt:
                        raise RuntimeCommandError(
                            409, "Push-to-talk is not available yet.")
                    # Disabling needs no live engine: persist it so a later
                    # connection does not start the global hook again.
                    config.save_push_to_talk_enabled(False)
                    self.ui.write_log("SYS: Push-to-talk off.")
                else:
                    try:
                        engine.set_push_to_talk(new_ptt)
                    except Exception as exc:
                        traceback.print_exc()
                        raise RuntimeCommandError(
                            500, "Push-to-talk could not be changed.") from exc
                    config.save_push_to_talk_enabled(new_ptt)
                    if not new_ptt:
                        self.ui.write_log("SYS: Push-to-talk off.")

        # ── identity / voice ────────────────────────────────────────────────
        identity_keys = {"assistant_name", "user_name", "voice_name"} & set(patch)
        if identity_keys:
            old_name = config.get_assistant_name()
            old_voice = config.get_voice()
            name = str(patch.get("assistant_name", old_name)).strip() or old_name
            user = str(patch.get("user_name", config.get_user_name())).strip()
            voice = str(patch.get("voice_name", old_voice))
            config.save_assistant_config(name, user)
            if voice != old_voice:
                config.save_voice(voice)
            self.ui.set_assistant_name(name)
            self.ui.write_log("SYS: Identity profile committed.")
            if voice != old_voice:
                # No resumption: the model must not be shown a transcript it
                # cannot remember, and the new voice starts a new conversation.
                try:
                    self._approvals.revoke_all("conversation changed")
                except Exception:  # noqa: BLE001 - reconnect still proceeds
                    pass
                self.ui.clear_conversation()
                engine = self.engine
                if engine is not None:
                    # Voice is fixed at connect time. Clear the handle even
                    # when there is no live session, so a reconnect that is
                    # already in progress cannot restore the old voice.
                    try:
                        engine._resume_handle = None
                    except Exception:
                        pass
                self._request_reconnect(False, "new voice", log_fallback=False)

        # ── appearance ──────────────────────────────────────────────────────
        if "ui_color" in patch:
            colour = str(patch["ui_color"])
            config.save_ui_color(colour)
            self.ui.write_log("SYS: Theme matrix updated.")

        # ── weather location ────────────────────────────────────────────────
        if "weather_location" in patch:
            new_location = str(patch["weather_location"]).strip()
            if new_location != config.get_weather_location():
                config.save_weather_location(new_location)
                if new_location:
                    self.ui.write_log(
                        f"SYS: Weather location set to {new_location}.")
                else:
                    self.ui.write_log("SYS: Weather location reset — "
                                      "using the Tashkent feed.")
                try:
                    self.weather_service.get(force=True)
                except Exception:
                    pass

        # ── audio routing ───────────────────────────────────────────────────
        if "input_device" in patch or "output_device" in patch:
            old_in = config.get_input_device()
            old_out = config.get_output_device()
            new_in = str(patch.get("input_device", old_in)).strip()
            new_out = str(patch.get("output_device", old_out)).strip()
            config.save_input_device(new_in)
            config.save_output_device(new_out)
            if new_in != old_in or new_out != old_out:
                self.ui.write_log("SYS: Audio routing updated — reconnecting…")
                self._request_reconnect(True, "audio device")
            else:
                self.ui.write_log("SYS: Audio routing unchanged.")

        # ── behavior ────────────────────────────────────────────────────────
        if ("proactive_audio_enabled" in patch
                or "thinking_enabled" in patch):
            proactive = bool(patch.get(
                "proactive_audio_enabled",
                config.get_proactive_audio_enabled()))
            thinking = bool(patch.get(
                "thinking_enabled", config.get_thinking_enabled()))
            config.save_proactive_audio_enabled(proactive)
            config.save_thinking_enabled(thinking)
            self.ui.write_log(
                "SYS: Behavior matrix — proactive "
                f"{'ON' if proactive else 'OFF'} / thinking "
                f"{'ON' if thinking else 'OFF'}.")
            self._request_reconnect(True, "settings")

        # ── turn detection ──────────────────────────────────────────────────
        if "turn_tuning" in patch and isinstance(patch["turn_tuning"], dict):
            current = config.get_turn_tuning()
            merged = dict(current)
            merged.update({k: v for k, v in patch["turn_tuning"].items()
                           if v is not None})
            config.save_turn_tuning(merged)
            self.ui.write_log(
                "SYS: Advanced link profile committed — reconnecting…")
            self._request_reconnect(True, "settings")

    def set_api_key(self, api_key: str, os_system: str | None = None) -> Dict[str, Any]:
        """Persist a key and wake/reconnect the existing session thread."""
        key = str(api_key or "").strip()
        if not key:
            raise RuntimeCommandError(422, "An API key is required.")
        try:
            with self._settings_lock:
                config.save_api_keys(key)
                config._patch_config(  # same function the desktop setup screen used
                    os_system=(os_system or _detected_os()),
                    setup_done=True,
                )
        except Exception as exc:
            traceback.print_exc()
            raise RuntimeCommandError(
                500, "The API key could not be saved.") from exc

        # Side calls (memory extraction, screen understanding, research) read
        # this process-wide cache. Without invalidation they keep sending the
        # retired key until the process restarts.
        _gemini.reset_key_cache()

        self.ui.mark_ready(True)
        self.ui.write_log("SYS: Key saved. Connecting…")
        engine = self.engine
        if engine is not None and getattr(engine, "session", None) is not None:
            engine.request_reconnect(keep_context=True, reason="new API key")
        else:
            self.start()
        setup = self._setup_status()
        self._publish({"type": "setup.updated", "setup": setup,
                       "ts": _now_iso()})
        self._publish({"type": "settings.updated",
                       "settings": self.basic_settings(),
                       "setup": setup, "ts": _now_iso()})
        return setup

    # ── audio diagnostics ────────────────────────────────────────────────────

    def audio_devices_data(self, refresh: bool = False) -> Dict[str, Any]:
        return {
            "input": audio_devices.list_devices("input", refresh=refresh),
            "output": audio_devices.list_devices("output", refresh=refresh),
            "input_device": config.get_input_device(),
            "output_device": config.get_output_device(),
            "output_volume": audio_devices.get_output_volume(
                config.get_output_device()),
        }

    def test_audio(self, kind: str, device: str) -> Dict[str, Any]:
        if not self._audio_test_lock.acquire(blocking=False):
            raise RuntimeCommandError(409, "An audio test is already running.")
        try:
            if kind == "input":
                result = audio_devices.measure_input(
                    device, seconds=3.0,
                    on_level=lambda level: self.ui.emit_audio_test_level(
                        "input", level))
            else:
                result = audio_devices.play_tone(device)
            return dict(result)
        finally:
            self._audio_test_lock.release()

    def set_volume(self, volume: int, device: str) -> Dict[str, Any]:
        try:
            ok = bool(audio_devices.set_output_volume(int(volume), device))
        except Exception as exc:
            ok = False
            print(f"[Audio] volume change failed: {exc}")
        self.ui.write_log(
            f"SYS: Output level set to {int(volume)}%." if ok
            else "SYS: Output level could not be set.")
        return {"ok": ok, "volume": int(volume)}

    def get_volume(self, device: str) -> int | None:
        try:
            return audio_devices.get_output_volume(device)
        except Exception as exc:
            print(f"[Audio] volume read failed: {exc}")
            return None

    # ── long-term memory ─────────────────────────────────────────────────────

    def _get_memory_service(self):
        """Lazily open the one memory service shared by chat, tools and UI."""
        with self._memory_lock:
            if self._memory_service is not None:
                return self._memory_service
            settings = config.get_memory_settings()
            if not settings.get("enabled", True):
                raise RuntimeCommandError(
                    503, "Long-term memory is disabled in settings.")
            try:
                from core import memory as _memory

                service = _memory.get_default_service()
                if service is None:
                    detail = _memory.get_default_error() or "unknown error"
                    self._memory_error = detail
                    raise RuntimeCommandError(
                        503, f"Memory storage is unavailable: {detail}")
                self._memory_service = service
                self._memory_error = ""
                return service
            except RuntimeCommandError:
                raise
            except Exception as exc:  # noqa: BLE001 - reported to the UI
                traceback.print_exc()
                self._memory_error = f"{type(exc).__name__}: {exc}"
                raise RuntimeCommandError(
                    503, f"Memory storage is unavailable: {self._memory_error}"
                ) from exc

    @staticmethod
    def _memory_result(result) -> Dict[str, Any]:
        payload = result.to_dict()
        payload["ok"] = bool(result.ok)
        return payload

    @staticmethod
    def _raise_memory_status(result) -> None:
        status = getattr(result, "status", "")
        if status == "not_found":
            raise RuntimeCommandError(404, result.message or "Memory not found.")
        if status == "invalid":
            raise RuntimeCommandError(422, result.message or "Invalid memory.")
        if status in ("conflict", "confirmation_required"):
            raise RuntimeCommandError(409, result.message or "Memory conflict.")
        if status == "storage_error":
            raise RuntimeCommandError(503, result.message or "Memory storage failed.")
        if status in ("secret_rejected",):
            raise RuntimeCommandError(422, result.message)
        if status == "unavailable":
            raise RuntimeCommandError(503, result.message)

    def memory_stats(self) -> Dict[str, Any]:
        result = self._get_memory_service().get_memory_stats()
        self._raise_memory_status(result)
        return self._memory_result(result)

    def memory_list(self, *, type: str | None = None,
                    project_id: str | None = None, query: str = "",
                    status: str = "active", limit: int = 50,
                    offset: int = 0) -> Dict[str, Any]:
        service = self._get_memory_service()
        result = service.search_memories(
            query, memory_type=type, project_id=project_id,
            limit=limit, offset=offset, status=status,
            include_superseded=(status == "all"))
        self._raise_memory_status(result)
        return self._memory_result(result)

    def memory_get(self, memory_id: str) -> Dict[str, Any]:
        result = self._get_memory_service().get_memory(memory_id)
        self._raise_memory_status(result)
        return self._memory_result(result)

    def memory_update(self, memory_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"content", "type", "title", "tags", "project_id",
                   "confidence", "importance", "expected_version",
                   "supersede"}
        clean = {key: value for key, value in (patch or {}).items()
                 if key in allowed}
        if "type" in clean:
            clean["memory_type"] = clean.pop("type")
        result = self._get_memory_service().update_memory(memory_id, **clean)
        self._raise_memory_status(result)
        return self._memory_result(result)

    def memory_delete(self, memory_id: str) -> Dict[str, Any]:
        result = self._get_memory_service().delete_memory(memory_id)
        self._raise_memory_status(result)
        return self._memory_result(result)

    def memory_forget(self, *, memory_id: str | None = None,
                      query: str | None = None,
                      project_id: str | None = None,
                      type: str | None = None,
                      confirm: bool = False) -> Dict[str, Any]:
        service = self._get_memory_service()
        if memory_id:
            result = service.delete_memory(memory_id)
        elif project_id:
            result = service.forget_project(project_id, confirm=confirm)
        elif type:
            result = service.forget_type(type, confirm=confirm)
        elif query:
            result = service.forget_by_query(query, confirm=confirm)
        else:
            result = service.clear_all(confirm=confirm)
        self._raise_memory_status(result)
        return self._memory_result(result)

    def memory_maintenance(self, *, integrity: bool = False) -> Dict[str, Any]:
        result = self._get_memory_service().run_maintenance(integrity=integrity)
        self._raise_memory_status(result)
        return self._memory_result(result)

    def memory_backup(self) -> Dict[str, Any]:
        result = self._get_memory_service().backup()
        self._raise_memory_status(result)
        return self._memory_result(result)

    def memory_export(self) -> Dict[str, Any]:
        payload = self._get_memory_service().export_memories()
        return payload

    def memory_import(self, payload: Any, *, dry_run: bool = False) -> Dict[str, Any]:
        result = self._get_memory_service().import_memories(payload, dry_run=dry_run)
        self._raise_memory_status(result)
        return self._memory_result(result)

    def memory_settings(self) -> Dict[str, Any]:
        settings = config.get_memory_settings()
        return {
            "enabled": settings["enabled"],
            "auto_extract": settings["auto_extract"],
            "retrieval_enabled": settings["retrieval_enabled"],
            "voice_injection": settings["voice_injection"],
            "max_retrieved": settings["max_retrieved"],
            "max_context_chars": settings["max_context_chars"],
            "search_limit": settings["search_limit"],
            "backup_retention": settings["backup_retention"],
            "expiry_days": settings["expiry_days"],
            "db_path": str(settings["db_path"]),
            "backup_dir": str(settings["backup_dir"]),
            "error": self._memory_error,
        }

    def save_memory_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._memory_lock:
            try:
                config.save_memory_settings(dict(patch or {}))
            except ValueError as exc:
                raise RuntimeCommandError(422, str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                raise RuntimeCommandError(
                    500, "Memory settings could not be saved.") from exc
            # Limits are read at service construction; apply them live.
            if self._memory_service is not None:
                settings = config.get_memory_settings()
                self._memory_service.retrieval_enabled = settings["retrieval_enabled"]
                self._memory_service.auto_extract = settings["auto_extract"]
                self._memory_service.max_retrieved = settings["max_retrieved"]
                self._memory_service.max_context_chars = settings["max_context_chars"]
                self._memory_service.search_limit = settings["search_limit"]
                self._memory_service.backup_retention = settings["backup_retention"]
                self._memory_service.expiry_days = settings["expiry_days"]
        self.ui.write_log("SYS: Memory settings updated.")
        return self.memory_settings()

    # ── modules ──────────────────────────────────────────────────────────────

    def scan_module(self, module_id: str) -> Dict[str, Any]:
        if module_id not in _MODULE_DETAILS:
            raise RuntimeCommandError(404, "Unknown module bay.")
        item = _MODULE_DETAILS[module_id]
        count = 0
        if module_id == "actions":
            count = tool_calling.get_tool_count()
            noun = "TOOL" if count == 1 else "TOOLS"
            status = f"{count} CORE {noun} LINKED"
            message = f"SYS: ACTIONS bay scanned — {count} core tools linked."
        elif module_id == "skills":
            snapshot = self.refresh_skills()
            counts = snapshot.get("counts", {})
            count = int(counts.get("total", 0))
            noun = "SKILL" if count == 1 else "SKILLS"
            status = f"{count} {noun} LINKED"
            message = (
                f"SYS: SKILLS bay rescanned — {count} package(s), "
                f"{counts.get('enabled', 0)} enabled, "
                f"{len(snapshot.get('errors', []))} issue(s).")
        else:
            status = "SCAN COMPLETE"
            message = (f"SYS: {item['name']} bay scanned — no external "
                       "packages linked.")
        self.ui.write_log(message)
        return {"module_id": module_id, "status": status,
                "count": count, "message": message}

    # ── weather / metrics ────────────────────────────────────────────────────

    def get_weather(self, force: bool = False) -> Dict[str, Any]:
        return self.weather_service.get(force=force)

    def telemetry(self) -> Dict[str, Any]:
        """Real runtime information for the sidebar, never fabricated.

        The model name is the one the Live session is using; when the engine is
        not up yet it is the model the ladder would choose. Latency is the
        measured time from a user turn to the first assistant transcript, not a
        made-up provider number.
        """
        engine = self.engine
        model = (getattr(engine, "model", None) if engine is not None
                 else None) or _gemini.live_model()
        connected = self.session_connected
        running = self.session_running
        state = self.ui.state
        run = self.ui.snapshot().get("agent_run")
        active_run = bool(
            isinstance(run, dict)
            and run.get("status") in ("in_progress", "needs_user", "blocked"))
        return {
            "model": str(model or "").rsplit("/", 1)[-1] or None,
            "provider": "Google Gemini" if model else None,
            "response_latency_ms": self.ui.last_response_latency_ms,
            "microphone": ("muted" if self.ui.muted
                           else "active" if connected else "offline"),
            "voice_engine": ("online" if connected
                             else "connecting" if running else "offline"),
            "active_agents": 1 if active_run else 0,
            "agent_runtime": False,
            "link_state": state,
        }

    def get_metrics(self) -> Dict[str, Any]:
        data = dict(self.metrics.snapshot())
        data.update(self.telemetry())
        return data

    # ── reminders / notifications ────────────────────────────────────────────

    def notification_state(self, *, limit: int = 50,
                           status: str = "") -> List[Dict[str, Any]]:
        try:
            return self._notifications.list(limit=limit, status=status)
        except Exception:  # noqa: BLE001 - a snapshot must never fail
            traceback.print_exc()
            return []

    def dismiss_notification(self, notification_id: str) -> Dict[str, Any]:
        try:
            return self._notifications.dismiss(notification_id)
        except _notifications.NotificationError as exc:
            raise RuntimeCommandError(404, str(exc)) from exc

    def snooze_notification(self, notification_id: str,
                            minutes: int = 10) -> Dict[str, Any]:
        try:
            return self._notifications.snooze(notification_id, minutes)
        except _notifications.NotificationError as exc:
            raise RuntimeCommandError(409, str(exc)) from exc

    def mark_notification_read(self, notification_id: str) -> Dict[str, Any]:
        try:
            return self._notifications.mark_read(notification_id)
        except _notifications.NotificationError as exc:
            raise RuntimeCommandError(404, str(exc)) from exc

    # ── capability readiness ─────────────────────────────────────────────────

    def capability_readiness(self) -> Dict[str, Any]:
        """Phone and YouTube status from authoritative checks, never secrets."""
        result: Dict[str, Any] = {}
        for module_name, key in (("phone_call", "phone"),
                                 ("youtube_player", "youtube")):
            module = get_tool_module(module_name)
            check = getattr(module, "readiness", None) if module else None
            if callable(check):
                try:
                    data = check()
                    result[key] = dict(data) if isinstance(data, dict) else {
                        "capability": key, "status": "unavailable",
                        "detail": "Readiness returned an unexpected value.",
                    }
                except Exception as exc:  # noqa: BLE001
                    result[key] = {
                        "capability": key,
                        "status": "unavailable",
                        "detail": f"Readiness check failed ({type(exc).__name__}).",
                    }
            else:
                result[key] = {
                    "capability": key,
                    "status": "unavailable",
                    "detail": "The capability module is not installed.",
                }
        return result

    def diagnostics(self) -> Dict[str, Any]:
        """Real readiness checks for the initialise gate.

        Nothing here is inferred from a tool having been requested: provider
        readiness means the Live session is actually connected, and weather is
        the cached result of the existing service.
        """
        setup = self._setup_status()
        telemetry = self.telemetry()
        model = telemetry.get("model")
        voice = str(telemetry.get("voice_engine") or "offline")
        microphone = str(telemetry.get("microphone") or "offline")
        connected = self.session_connected
        running = self.session_running
        try:
            tools = len(self._tools())
        except Exception:
            tools = 0
        try:
            snapshot = getattr(self.weather_service, "cached", None)
            weather = snapshot() if callable(snapshot) \
                else self.weather_service.get(force=False)
        except Exception:
            weather = {"available": False}
        weather_ok = bool(weather.get("available"))
        try:
            # Cached-only: a readiness poll must never wait on device probing.
            inputs = audio_devices.list_devices("input", wait=False)
            outputs = audio_devices.list_devices("output", wait=False)
        except Exception:
            inputs, outputs = [], []

        checks: list[Dict[str, str]] = []
        checks.append({
            "id": "settings",
            "label": "SETTINGS",
            "status": "ok" if setup.get("configured") else "error",
            "detail": ("Configuration loaded" if setup.get("configured")
                       else "Gemini API key is missing"),
        })
        checks.append({
            "id": "provider",
            "label": "PROVIDER",
            "status": "ok" if model else "error",
            "detail": (f"{model} · Google Gemini" if model
                       else "No Live model available"),
        })
        checks.append({
            "id": "session",
            "label": "LIVE SESSION",
            "status": "ok" if connected else ("warn" if running else "error"),
            "detail": ("Connected" if connected else
                       "Connecting" if running else "Not running"),
        })
        checks.append({
            "id": "voice",
            "label": "VOICE ENGINE",
            "status": "ok" if voice == "online" else ("warn" if voice == "connecting" else "error"),
            "detail": voice.capitalize(),
        })
        # While the first connection is opening, the microphone has not been
        # opened yet — that is the normal sequence, not a failure. Report it as
        # a warning so the gate does not show a red error during every launch.
        mic_status = ("ok" if microphone == "active"
                      else ("warn" if microphone == "muted" else "error"))
        mic_detail = microphone.capitalize()
        if microphone != "active" and running and not connected:
            mic_status = "warn"
            mic_detail = "Waiting for the Live session"
        checks.append({
            "id": "microphone",
            "label": "MICROPHONE",
            "status": mic_status,
            "detail": mic_detail,
        })
        checks.append({
            "id": "audio",
            "label": "AUDIO DEVICES",
            "status": "ok" if inputs and outputs else "warn",
            "detail": f"{len(inputs)} in · {len(outputs)} out",
        })
        checks.append({
            "id": "weather",
            "label": "WEATHER",
            "status": "ok" if weather_ok else "warn",
            "detail": (f"Live {weather.get('location') or 'Tashkent'} feed"
                       if weather_ok else "Feed unavailable"),
        })
        checks.append({
            "id": "tools",
            "label": "TOOL REGISTRY",
            "status": "ok" if tools else "error",
            "detail": f"{tools} tools registered",
        })
        capabilities = self.capability_readiness()
        tone = {
            "ready": "ok",
            "starting": "warn",
            "configured_unverified": "warn",
            "missing_credentials": "warn",
            "invalid_configuration": "error",
            "missing_dependency": "error",
            "service_unavailable": "error",
            "unavailable": "error",
        }
        for key, label in (("phone", "PHONE CALL"),
                           ("youtube", "YOUTUBE")):
            item = capabilities.get(key) or {}
            status = str(item.get("status") or "unavailable")
            checks.append({
                "id": key,
                "label": label,
                "status": tone.get(status, "warn"),
                "detail": str(item.get("detail") or status.replace("_", " ")),
            })
        ok = bool(setup.get("configured") and model and connected and tools)
        engine = self.engine
        connection_error = str(
            getattr(engine, "last_connection_error", "") or "")
        return {
            "ok": ok,
            "checks": checks,
            "model": model,
            "provider": telemetry.get("provider"),
            "session_running": running,
            "session_connected": connected,
            "connection_error": connection_error,
            "weather_available": weather_ok,
            "tools": tools,
            "capabilities": capabilities,
            "input_devices": len(inputs),
            "output_devices": len(outputs),
            "state": self.ui.state,
        }

    def initialize(self) -> Dict[str, Any]:
        """Bring the Live session up and return immediately with status.

        The expensive refreshes (weather over the network, audio enumeration)
        are warmed in the background: the gate polls real readiness, so
        blocking this HTTP call on them only made initialisation feel stuck.
        """
        if not self._config_ready():
            return self.diagnostics()
        self.start()

        def _warm() -> None:
            try:
                self.weather_service.get(force=True)
            except Exception:
                pass
            try:
                audio_devices.list_devices("input", refresh=True)
                audio_devices.list_devices("output", refresh=True)
            except Exception:
                pass

        threading.Thread(target=_warm, daemon=True,
                         name="init-refresh").start()
        return self.diagnostics()

    def clear_tools(self) -> None:
        self.ui.clear_tool_events()

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _publish(self, event: Dict[str, Any]) -> None:
        try:
            self.hub.publish(event)
        except Exception:
            pass

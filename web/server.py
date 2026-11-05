"""FastAPI server for Jarvis's local web interface.

The server is loopback-only, same-origin-only, JSON-only for mutations, and
serves the built Vite assets in production.  It does not execute arbitrary
commands or file operations for the browser: the only action path is still the
existing Gemini tool loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import traceback
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from web import __version__, frontend_dist
from web.models import (
    ApiKeyIn,
    AudioTestIn,
    MemoryForgetIn,
    MemoryImportIn,
    MemorySettingsIn,
    MemoryUpdateIn,
    MessageIn,
    MuteIn,
    PttHoldIn,
    SettingsPatch,
    SkillEnabledIn,
    SkillUninstallIn,
    SnoozeNotificationIn,
    ToolSelectionIn,
    VolumeIn,
)
from web.runtime import AzeRuntime, RuntimeCommandError
from web.ui import _now_iso

MAX_BODY = 1_048_576


class _State:
    def __init__(self, runtime: AzeRuntime, frontend_dir: Path) -> None:
        self.runtime = runtime
        self.frontend_dir = frontend_dir


def _dev_origins() -> set[str]:
    raw = os.environ.get("AZE_DEV_ORIGINS", "")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def _host_name(host: str) -> str:
    try:
        parsed = urllib.parse.urlsplit("//" + (host or ""))
        return (parsed.hostname or "").lower()
    except Exception:
        return ""


def _host_is_loopback(host: str) -> bool:
    return _host_name(host) in {"127.0.0.1", "localhost", "::1"}


def _origin_allowed(origin: str | None, host: str) -> bool:
    """Same-origin, or an explicitly configured local Vite dev origin."""
    if not origin:
        return True
    normal = origin.rstrip("/")
    if normal in _dev_origins():
        return True
    try:
        parsed = urllib.parse.urlsplit(normal)
        if parsed.scheme not in ("http", "https"):
            return False
        return parsed.netloc.lower() == (host or "").lower()
    except Exception:
        return False


def _security_headers(response, path: str) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "microphone=(), camera=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault("Vary", "Origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' https://www.youtube.com "
        "https://s.ytimg.com; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://i.ytimg.com https://img.youtube.com; "
        "font-src 'self' data:; "
        "connect-src 'self' ws://127.0.0.1:* ws://localhost:* "
        "https://www.youtube.com https://www.youtube-nocookie.com; "
        "frame-src https://www.youtube.com https://www.youtube-nocookie.com; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'")
    if (path.startswith("/api/")
            or response.headers.get("content-type", "").startswith("text/html")):
        response.headers.setdefault("Cache-Control", "no-store")

def _missing_frontend_html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jarvis — frontend build missing</title>
<style>
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:#060606;
 color:#f4f0e6;font:15px/1.6 ui-monospace,Consolas,monospace}
 main{max-width:680px;padding:40px;border:1px solid #3b3524;background:#0d0d0a}
 h1{color:#ffc400;font-size:18px;letter-spacing:.18em;margin:0 0 16px}
 code{display:block;padding:14px 16px;background:#050505;border:1px solid #2b271c;
 color:#ffdf7a;margin:16px 0;white-space:pre}
 p{color:#a39d8b} a{color:#ffc400}
</style></head><body><main>
<h1>FRONTEND ASSETS NOT FOUND</h1>
<p>The Python backend is running, but its production frontend assets are missing.</p>
<code>If installed with pip:
  pip install --force-reinstall .

If running from source:
  cd frontend
  npm install
  npm run build
  cd ..
  python main.py</code>
<p>For source development, run <code style="display:inline;padding:2px 4px">python main.py --dev</code>
after installing the frontend dependencies.</p>
</main></body></html>"""


def create_app(
    runtime: AzeRuntime | None = None,
    *,
    frontend_dir: Path | str | None = None,
    auto_start: bool = True,
) -> FastAPI:
    runtime = runtime or AzeRuntime()
    frontend_path = Path(frontend_dir) if frontend_dir else frontend_dist()
    state = _State(runtime, frontend_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime.hub.attach_loop(asyncio.get_running_loop())
        if auto_start:
            runtime.start()
        try:
            yield
        finally:
            runtime.stop()
            runtime.hub.detach_loop()

    app = FastAPI(
        title="Jarvis local interface",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.aze = state

    # ── boundary checks ─────────────────────────────────────────────────────

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if not _host_is_loopback(host):
            return JSONResponse({"detail": "Invalid host."}, status_code=400)
        origin = request.headers.get("origin")
        if origin and not _origin_allowed(origin, host):
            return JSONResponse({"detail": "Origin not allowed."}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            try:
                length = int(request.headers.get("content-length") or "0")
            except ValueError:
                length = 0
            if length > MAX_BODY:
                return JSONResponse({"detail": "Request body too large."},
                                    status_code=413)
            if request.url.path.startswith("/api/"):
                content_type = request.headers.get("content-type", "")
                if not content_type.lower().startswith("application/json"):
                    return JSONResponse(
                        {"detail": "Content-Type must be application/json."},
                        status_code=415)
        try:
            response = await call_next(request)
        except Exception:
            traceback.print_exc()
            response = JSONResponse(
                {"detail": "Internal server error."}, status_code=500)
        _security_headers(response, request.url.path)
        return response

    @app.exception_handler(RuntimeCommandError)
    async def command_error(_request: Request, exc: RuntimeCommandError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception):
        traceback.print_exc()
        return JSONResponse({"detail": "Internal server error."}, status_code=500)

    # ── read endpoints ──────────────────────────────────────────────────────

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "app": "Jarvis",
            "version": __version__,
            "configured": runtime._setup_status()["configured"],
            "state": runtime.ui.state,
            "session_running": runtime.session_running,
            "session_connected": runtime.session_connected,
            "ws_clients": runtime.hub.client_count(),
            "time": _now_iso(),
        }

    @app.get("/api/bootstrap")
    def bootstrap() -> dict:
        return runtime.bootstrap()

    @app.get("/api/settings")
    async def get_settings(refresh_audio: bool = Query(default=False)) -> dict:
        return await asyncio.to_thread(runtime.settings, refresh_audio)

    @app.get("/api/tools")
    def tools() -> dict:
        return runtime._tool_catalog()

    @app.post("/api/tools/selection")
    async def select_tools(body: ToolSelectionIn) -> dict:
        catalog = await asyncio.to_thread(runtime.select_tools, body.enabled)
        return {"ok": True, **catalog}

    @app.get("/api/diagnostics")
    async def diagnostics() -> dict:
        return await asyncio.to_thread(runtime.diagnostics)

    @app.post("/api/initialize")
    async def initialize() -> dict:
        return await asyncio.to_thread(runtime.initialize)

    @app.post("/api/tools/clear")
    async def clear_tools() -> dict:
        await asyncio.to_thread(runtime.clear_tools)
        return {"ok": True}

    # ── skills ───────────────────────────────────────────────────────────────

    @app.get("/api/skills")
    async def skills() -> dict:
        return await asyncio.to_thread(runtime.skills_snapshot)

    @app.post("/api/skills/refresh")
    async def refresh_skills() -> dict:
        snapshot = await asyncio.to_thread(runtime.refresh_skills)
        return {"ok": True, "snapshot": snapshot}

    @app.get("/api/skills/{skill_id}")
    async def skill_detail(skill_id: str) -> dict:
        return await asyncio.to_thread(runtime.skills_detail, skill_id)

    @app.get("/api/skills/{skill_id}/asset")
    async def skill_asset(
        skill_id: str,
        path: str = Query(..., max_length=500),
    ) -> dict:
        return await asyncio.to_thread(
            runtime.read_skill_asset, skill_id, path)

    @app.patch("/api/skills/{skill_id}")
    async def enable_skill(skill_id: str, body: SkillEnabledIn) -> dict:
        snapshot = await asyncio.to_thread(
            runtime.set_skill_enabled, skill_id, body.enabled)
        return {"ok": True, "snapshot": snapshot}

    @app.post("/api/skills/{skill_id}/uninstall")
    async def uninstall_skill(skill_id: str, body: SkillUninstallIn) -> dict:
        result = await asyncio.to_thread(
            runtime.uninstall_skill, skill_id, body.confirm)
        return {"ok": True, **result}

    @app.get("/api/research")
    async def research_state() -> dict:
        return await asyncio.to_thread(runtime.research_state)

    @app.get("/api/audio/devices")
    async def audio_devices(refresh: bool = Query(default=False)) -> dict:
        return await asyncio.to_thread(runtime.audio_devices_data, refresh)

    @app.get("/api/weather")
    async def weather(force: bool = Query(default=False)) -> dict:
        return await asyncio.to_thread(runtime.get_weather, force)

    @app.get("/api/metrics")
    def metrics() -> dict:
        return runtime.get_metrics()

    # ── commands ────────────────────────────────────────────────────────────

    @app.post("/api/message")
    async def message(body: MessageIn) -> dict:
        message_row = await asyncio.to_thread(
            runtime.submit_text, body.text, body.client_message_id)
        return {"accepted": True, "message": message_row}

    @app.post("/api/voice/interrupt")
    async def interrupt() -> dict:
        await asyncio.to_thread(runtime.interrupt)
        return {"ok": True}

    # ── action approvals ────────────────────────────────────────────────────

    @app.get("/api/approvals")
    def approvals() -> dict:
        return {"approvals": runtime._approvals.public_requests()}

    @app.post("/api/approvals/{approval_id}/approve")
    async def approve_action(approval_id: str) -> dict:
        approval = await asyncio.to_thread(
            runtime.approve_action, approval_id)
        return {"ok": True, "approval": approval}

    @app.post("/api/approvals/{approval_id}/reject")
    async def reject_action(approval_id: str) -> dict:
        approval = await asyncio.to_thread(
            runtime.reject_action, approval_id)
        return {"ok": True, "approval": approval}

    # ── reminders / notifications ───────────────────────────────────────────

    @app.get("/api/notifications")
    async def notifications(status: str = Query(default=""),
                            limit: int = Query(default=50, ge=1, le=200)):
        rows = await asyncio.to_thread(
            runtime.notification_state, limit=limit, status=status)
        return {"notifications": rows, "count": len(rows)}

    @app.post("/api/notifications/{notification_id}/dismiss")
    async def dismiss_notification(notification_id: str) -> dict:
        notification = await asyncio.to_thread(
            runtime.dismiss_notification, notification_id)
        return {"ok": True, "notification": notification}

    @app.post("/api/notifications/{notification_id}/snooze")
    async def snooze_notification(notification_id: str,
                                  body: SnoozeNotificationIn) -> dict:
        notification = await asyncio.to_thread(
            runtime.snooze_notification, notification_id, body.minutes)
        return {"ok": True, "notification": notification}

    @app.post("/api/notifications/{notification_id}/read")
    async def read_notification(notification_id: str) -> dict:
        notification = await asyncio.to_thread(
            runtime.mark_notification_read, notification_id)
        return {"ok": True, "notification": notification}

    @app.post("/api/voice/mute")
    async def mute(body: MuteIn) -> dict:
        result = await asyncio.to_thread(runtime.set_muted, body.muted)
        return {"ok": True, **result}

    @app.post("/api/voice/ptt-hold")
    async def ptt_hold(body: PttHoldIn) -> dict:
        await asyncio.to_thread(runtime.set_ptt_hold, body.held)
        return {"ok": True, "held": body.held}

    @app.post("/api/session/reconnect")
    async def reconnect() -> dict:
        await asyncio.to_thread(runtime.request_reconnect)
        return {"requested": True}

    @app.patch("/api/settings")
    async def update_settings(body: SettingsPatch) -> dict:
        patch = body.model_dump(exclude_unset=True, exclude_none=True)
        settings = await asyncio.to_thread(runtime.apply_settings, patch)
        return {"ok": True, "settings": settings,
                "setup": runtime._setup_status()}

    @app.post("/api/setup/key")
    async def setup_key(body: ApiKeyIn) -> dict:
        setup = await asyncio.to_thread(
            runtime.set_api_key, body.api_key, body.os_system)
        return {"ok": True, "setup": setup}

    @app.post("/api/audio/test")
    async def audio_test(body: AudioTestIn) -> dict:
        result = await asyncio.to_thread(runtime.test_audio, body.kind, body.device)
        return {"ok": bool(result.get("ok")), "result": result}

    @app.post("/api/audio/volume")
    async def audio_volume(body: VolumeIn) -> dict:
        result = await asyncio.to_thread(
            runtime.set_volume, body.volume, body.device)
        return result

    # ── long-term memory ────────────────────────────────────────────────────

    @app.get("/api/memory/stats")
    async def memory_stats() -> dict:
        return await asyncio.to_thread(runtime.memory_stats)

    @app.get("/api/memory/settings")
    async def memory_settings() -> dict:
        return await asyncio.to_thread(runtime.memory_settings)

    @app.patch("/api/memory/settings")
    async def update_memory_settings(body: MemorySettingsIn) -> dict:
        patch = body.model_dump(exclude_unset=True, exclude_none=True)
        settings = await asyncio.to_thread(runtime.save_memory_settings, patch)
        return {"ok": True, "settings": settings}

    @app.get("/api/memory/export")
    async def memory_export() -> JSONResponse:
        payload = await asyncio.to_thread(runtime.memory_export)
        stamp = _now_iso().replace(":", "-").replace(".", "-")
        return JSONResponse(
            payload,
            headers={"Content-Disposition":
                     f'attachment; filename="jarvis-memory-{stamp}.json"'})

    @app.get("/api/memory")
    async def memory_list(
        memory_type: str | None = Query(default=None, alias="type",
                                        max_length=20),
        project_id: str | None = Query(default=None, max_length=120),
        query: str = Query(default="", max_length=500),
        status: str = Query(default="active", max_length=20),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        return await asyncio.to_thread(
            runtime.memory_list, type=memory_type, project_id=project_id,
            query=query, status=status, limit=limit, offset=offset)

    @app.post("/api/memory/forget")
    async def memory_forget(body: MemoryForgetIn) -> dict:
        return await asyncio.to_thread(
            runtime.memory_forget, memory_id=body.memory_id,
            query=body.query, project_id=body.project_id, type=body.type,
            confirm=body.confirm)

    @app.post("/api/memory/maintenance")
    async def memory_maintenance(integrity: bool = Query(default=False)) -> dict:
        return await asyncio.to_thread(runtime.memory_maintenance,
                                       integrity=integrity)

    @app.post("/api/memory/backup")
    async def memory_backup() -> dict:
        return await asyncio.to_thread(runtime.memory_backup)

    @app.post("/api/memory/import")
    async def memory_import(body: MemoryImportIn) -> dict:
        payload = body.data if body.data is not None else body.content
        if payload is None:
            return JSONResponse({"detail": "No import data supplied."},
                                status_code=422)
        return await asyncio.to_thread(runtime.memory_import, payload,
                                       dry_run=body.dry_run)

    @app.get("/api/memory/{memory_id}")
    async def memory_get(memory_id: str) -> dict:
        return await asyncio.to_thread(runtime.memory_get, memory_id)

    @app.patch("/api/memory/{memory_id}")
    async def memory_update(memory_id: str, body: MemoryUpdateIn) -> dict:
        patch = body.model_dump(exclude_unset=True, exclude_none=True)
        return await asyncio.to_thread(runtime.memory_update, memory_id, patch)

    @app.delete("/api/memory/{memory_id}")
    async def memory_delete(memory_id: str) -> dict:
        return await asyncio.to_thread(runtime.memory_delete, memory_id)

    @app.get("/api/audio/volume")
    async def read_audio_volume(device: str = Query(default="", max_length=200)) -> dict:
        volume = await asyncio.to_thread(runtime.get_volume, device)
        return {"volume": volume}

    @app.post("/api/modules/{module_id}/scan")
    async def scan_module(module_id: str) -> dict:
        result = await asyncio.to_thread(runtime.scan_module, module_id)
        return {"ok": True, **result}

    @app.get("/api/{path:path}", include_in_schema=False)
    def api_not_found(path: str):
        return JSONResponse({"detail": "Not found."}, status_code=404)

    # ── event stream ────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        host = websocket.headers.get("host", "")
        origin = websocket.headers.get("origin")
        if not _host_is_loopback(host) or not _origin_allowed(origin, host):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = runtime.hub.subscribe()
        send_lock = asyncio.Lock()

        async def send(payload: dict) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        try:
            # Subscribe before taking the snapshot: anything that happens while
            # the snapshot is being built is queued and replayed afterwards.
            snapshot = await asyncio.to_thread(runtime.bootstrap)
            await send({"type": "snapshot", "data": snapshot,
                        "ts": _now_iso()})

            async def sender() -> None:
                while True:
                    # Replay anything captured during snapshot construction.
                    while True:
                        try:
                            event = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        await send(event)
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        await send({"type": "ping", "ts": _now_iso()})
                        continue
                    await send(event)

            async def receiver() -> None:
                while True:
                    raw = await websocket.receive_text()
                    # Incoming client frames are bounded. This is a
                    # loopback-only socket.
                    if len(raw) > 65536:
                        await websocket.close(code=1009)
                        return
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        await send({"type": "error",
                                    "message": "Malformed JSON."})
                        continue
                    if not isinstance(payload, dict):
                        await send({"type": "error",
                                    "message": "Expected an object."})
                        continue
                    kind = payload.get("type")
                    if kind == "ping":
                        await send({"type": "pong", "ts": _now_iso()})
                    elif kind in ("pong", "sync"):
                        if kind == "sync":
                            fresh = await asyncio.to_thread(runtime.bootstrap)
                            await send({"type": "snapshot", "data": fresh,
                                        "ts": _now_iso()})
                    elif isinstance(kind, str) and kind.startswith("youtube_"):
                        # Player telemetry and correlated command results.
                        # The service only merges state / wakes the waiting
                        # worker thread, so this never blocks the server loop.
                        await asyncio.to_thread(
                            runtime.handle_youtube_event, payload)
                    else:
                        await send({"type": "error",
                                    "message": "Unsupported message type."})

            sender_task = asyncio.create_task(sender())
            receive_task = asyncio.create_task(receiver())
            done, pending = await asyncio.wait(
                {sender_task, receive_task},
                return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                try:
                    task.result()
                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass
        except WebSocketDisconnect:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            runtime.hub.unsubscribe(queue)
            # If that was the last open tab, any worker thread waiting for a
            # browser result must fail now instead of sitting out its timeout.
            runtime.handle_youtube_disconnect()

    # ── static frontend ─────────────────────────────────────────────────────

    index = frontend_path / "index.html"
    if index.is_file():
        app.mount("/", StaticFiles(directory=str(frontend_path), html=True),
                  name="frontend")
    else:
        @app.get("/", include_in_schema=False)
        def missing_root():
            return HTMLResponse(_missing_frontend_html(), status_code=503)

        @app.get("/{path:path}", include_in_schema=False)
        def missing_asset(path: str):
            return HTMLResponse(_missing_frontend_html(), status_code=503)

    return app

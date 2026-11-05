"""End-to-end tests for the YouTube player over the existing WebSocket.

The browser is the TestClient. It receives exactly the events the React panel
receives and replies with exactly the frames the panel sends. No Gemini
session, microphone, YouTube key or real player is involved.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from actions import youtube_player_tool as yt
from web.events import EventHub
from web.runtime import AzeRuntime
from web.server import create_app


BASE = "http://127.0.0.1:8765"


class FakeMetrics:
    def snapshot(self):
        return {"cpu": 1.0, "mem": 2.0, "net": 0.0, "gpu": -1.0, "tmp": -1.0}

    def stop(self):
        pass


class FakeWeather:
    def get(self, force=False):
        return {"available": True, "location": "Test", "temperature_c": 20}


def make_runtime() -> AzeRuntime:
    return AzeRuntime(hub=EventHub(), start_session=False,
                      weather_service=FakeWeather(), metrics=FakeMetrics())


def wait_for(predicate, timeout: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def player_state(*, playback: str = "playing", video_id: str = "aaaaaaaaaaa"):
    return {
        "panel_open": True,
        "player_ready": True,
        "video_id": video_id,
        "title": "Test video",
        "channel": "Test channel",
        "playback_state": playback,
        "current_time": 20.0,
        "duration": 600.0,
        "volume": 70,
        "muted": False,
        "fullscreen": False,
        "autoplay_blocked": False,
        "error": None,
    }


class YouTubeWebTests(unittest.TestCase):
    def setUp(self) -> None:
        yt.reset_service()
        self.runtime = make_runtime()
        self.client = TestClient(
            create_app(self.runtime,
                       frontend_dir=Path(tempfile.gettempdir()) / "aze-no-dist"),
            base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(yt.reset_service)

    def _socket(self):
        return self.client.websocket_connect(
            "/ws", headers={"origin": BASE, "host": "127.0.0.1:8765"})

    def test_bootstrap_exposes_youtube_state(self):
        data = self.client.get("/api/bootstrap").json()
        self.assertIn("youtube", data)
        self.assertFalse(data["youtube"]["panel_open"])
        self.assertIn("youtube_player",
                      [tool["name"] for tool in data["tools"]])
        core = next(category for category in data["tool_categories"]
                    if category["id"] == "")
        self.assertEqual(core["label"], "Core Tools")
        self.assertIn("youtube_player",
                      [tool["name"] for tool in core["tools"]])

    def test_csp_allows_the_official_youtube_frame_and_script(self):
        response = self.client.get("/api/health")
        policy = response.headers["content-security-policy"]
        self.assertIn("frame-src https://www.youtube.com", policy)
        self.assertIn("script-src 'self' https://www.youtube.com", policy)

    def test_player_state_frame_updates_the_backend_snapshot(self):
        with self._socket() as socket:
            socket.receive_json()
            socket.send_json({
                "type": "youtube_player_state",
                "state": player_state(playback="paused"),
            })
            self.assertTrue(wait_for(
                lambda: self.runtime.youtube_state()["playback_state"] == "paused"))
        data = self.client.get("/api/bootstrap").json()["youtube"]
        self.assertTrue(data["panel_open"])
        self.assertEqual(data["video_id"], "aaaaaaaaaaa")
        self.assertEqual(data["playback_state"], "paused")

    def test_command_round_trip_reaches_the_browser_and_back(self):
        results = []
        with self._socket() as socket:
            socket.receive_json()
            self.runtime.handle_youtube_event({
                "type": "youtube_player_state",
                "state": player_state(),
            })
            thread = threading.Thread(
                target=lambda: results.append(yt.youtube_player("pause")))
            thread.start()
            command = socket.receive_json()
            self.assertEqual(command["type"], "youtube_player_command")
            self.assertEqual(command["command"], "pause")
            self.assertTrue(command["request_id"])
            socket.send_json({
                "type": "youtube_player_result",
                "request_id": command["request_id"],
                "ok": True,
                "state": player_state(playback="paused"),
            })
            thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["state"]["playback_state"], "paused")

    def test_closing_the_socket_fails_a_waiting_command(self):
        results = []
        thread = threading.Thread(
            target=lambda: results.append(yt.youtube_player("pause")))
        with self._socket() as socket:
            socket.receive_json()
            self.runtime.handle_youtube_event({
                "type": "youtube_player_state",
                "state": player_state(),
            })
            thread.start()
            command = socket.receive_json()
            self.assertEqual(command["type"], "youtube_player_command")
        # The server's finally block calls handle_youtube_disconnect once the
        # last queue has left.
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results)
        self.assertFalse(results[0]["success"])
        self.assertEqual(results[0]["error_code"], "interface_disconnected")

    def test_invalid_state_frames_are_ignored_not_fatal(self):
        with self._socket() as socket:
            socket.receive_json()
            socket.send_json({
                "type": "youtube_player_state",
                "state": {
                    "panel_open": True,
                    "video_id": "not-an-id",
                    "volume": "loud",
                    "playback_state": "diagonal",
                },
            })
            socket.send_json({"type": "ping"})
            pong = socket.receive_json()
        self.assertEqual(pong["type"], "pong")


if __name__ == "__main__":
    unittest.main()

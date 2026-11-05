"""Tests for the YouTube discovery, ranking and player bridge.

No test needs a real YouTube key or network access: the API and the browser
are both replaced by local fakes. The bridge fake publishes exactly the same
events the WebSocket layer would, and replies with the same correlated
``youtube_player_result`` shape the frontend sends.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from actions import tool_calling
from actions import youtube_player_tool as yt


def candidate(
    video_id: str,
    title: str,
    *,
    channel: str = "Channel",
    description: str = "",
    duration: int = 600,
    views: int = 10_000,
    likes: int = 200,
    search_rank: int = 0,
    embeddable: bool = True,
    live: bool = False,
    published_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "channel_id": "channel-" + video_id,
        "description": description,
        "tags": [],
        "duration_seconds": duration,
        "view_count": views,
        "like_count": likes,
        "published_at": published_at,
        "live": live,
        "embeddable": embeddable,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "search_rank": search_rank,
        "_score": 0.0,
    }


def player_state(video: dict | None = None, *,
                 playback: str = "playing", **overrides) -> dict:
    video = video or {"video_id": "aaaaaaaaaaa", "title": "Test video",
                      "channel": "Test channel", "duration_seconds": 600}
    state = {
        "panel_open": True,
        "player_ready": True,
        "video_id": video.get("video_id", ""),
        "title": video.get("title", ""),
        "channel": video.get("channel", ""),
        "playback_state": playback,
        "current_time": 30.0,
        "duration": float(video.get("duration_seconds") or 600),
        "volume": 75,
        "muted": False,
        "fullscreen": False,
        "autoplay_blocked": False,
        "error": None,
    }
    state.update(overrides)
    return state


class Bridge:
    """A fake browser connected to the existing event hub."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.connected = True
        self.handler = None
        self.publish_event = threading.Event()

    def publish(self, event: dict) -> None:
        self.events.append(event)
        self.publish_event.set()
        if self.handler is not None:
            self.handler(event)

    def has_clients(self) -> bool:
        return self.connected

    def types(self) -> list[str]:
        return [event.get("type", "") for event in self.events]


class YouTubeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        yt.reset_service()
        self.bridge = Bridge()
        self.service = yt.get_service()
        self.service.configure(publish=self.bridge.publish,
                               has_clients=self.bridge.has_clients)
        self.service.command_timeout = 0.6
        self.service.load_timeout = 0.6
        self.service.status_timeout = 0.3
        self.addCleanup(yt.reset_service)
        self.bridge.handler = self._default_handler

    def _respond(self, request_id: str, state: dict, *, ok: bool = True,
                 error: dict | None = None) -> None:
        payload = {
            "type": "youtube_player_result",
            "request_id": request_id,
            "ok": ok,
            "state": state,
        }
        if error:
            payload["error"] = error
        self.service.handle_client_event(payload)

    def _default_handler(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "youtube_load_video":
            video = event["video"]
            self._respond(event["request_id"], player_state(video))
            return
        if kind == "youtube_close_panel":
            state = player_state(playback="closed")
            state["panel_open"] = False
            state["player_ready"] = False
            state["video_id"] = ""
            state["title"] = ""
            state["channel"] = ""
            self._respond(event["request_id"], state)
            return
        if kind != "youtube_player_command":
            return
        command = event.get("command")
        args = event.get("args") or {}
        video = {
            "video_id": "aaaaaaaaaaa",
            "title": "Test video",
            "channel": "Test channel",
            "duration_seconds": 600,
        }
        state = player_state(video, playback="playing", current_time=30.0)
        if command == "pause":
            state["playback_state"] = "paused"
        elif command == "stop":
            state["playback_state"] = "stopped"
        elif command in ("seek_forward", "seek_backward", "seek_to"):
            seconds = float(args.get("seconds") or 0)
            if command == "seek_forward":
                state["current_time"] = 30.0 + seconds
            elif command == "seek_backward":
                state["current_time"] = max(0.0, 30.0 - seconds)
            else:
                state["current_time"] = seconds
        elif command in ("volume_up", "volume_down", "set_volume"):
            step = int(args.get("volume") or args.get("step") or 10)
            if command == "volume_up":
                state["volume"] = min(100, 75 + step)
            elif command == "volume_down":
                state["volume"] = max(0, 75 - step)
            else:
                state["volume"] = step
        elif command == "mute":
            state["muted"] = True
        elif command == "unmute":
            state["muted"] = False
        elif command == "fullscreen":
            state["fullscreen"] = True
        elif command == "exit_fullscreen":
            state["fullscreen"] = False
        self._respond(event["request_id"], state)


class RegistrationTests(YouTubeTestBase):
    def test_tool_is_discovered_and_registered_once(self):
        records = tool_calling.discover_tool_modules()
        matches = [record for record in records
                   if record["name"] == "youtube_player"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["category"], "")
        self.assertIn("youtube_player", tool_calling.TOOL_REGISTRY)
        self.assertTrue(callable(tool_calling.TOOL_REGISTRY["youtube_player"]["func"]))

    def test_declaration_has_operation_schema_and_llm_guidance(self):
        declaration = next(
            item for item in tool_calling.get_tool_declarations()
            if item["name"] == "youtube_player")
        self.assertEqual(declaration["parameters"]["required"], ["operation"])
        self.assertEqual(declaration["parameters"]["type"], "OBJECT")
        self.assertIn("search_and_play", declaration["description"])
        self.assertIn("never", declaration["description"].lower())
        for operation in yt._OPERATIONS:
            self.assertIn(operation, declaration["description"])

    def test_root_tool_appears_in_the_actions_catalog(self):
        catalog = tool_calling.get_tool_catalog()
        core = next(item for item in catalog["categories"]
                    if item["id"] == "")
        self.assertEqual(core["label"], "Core Tools")
        youtube = next(tool for tool in core["tools"]
                       if tool["name"] == "youtube_player")
        self.assertEqual(youtube["actions"], ["youtube_player"])
        self.assertEqual(core["count"], len(core["tools"]))

    def test_tool_definition_is_dispatchable_through_the_registry(self):
        with mock.patch.object(yt, "_search_candidates",
                               return_value=([candidate(
                                   "bbbbbbbbbbb",
                                   "Transformer architecture explained",
                                   channel="AI Explained")], [])):
            result = tool_calling.execute_tool(
                "youtube_player",
                {"operation": "search_and_play",
                 "query": "transformer architecture"})
        self.assertTrue(result["success"], result)
        self.assertEqual(result["video"]["video_id"], "bbbbbbbbbbb")


class ValidationTests(YouTubeTestBase):
    def test_unknown_operation_is_a_structured_error(self):
        result = yt.youtube_player("dance")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "invalid_operation")
        self.assertIn("search_and_play", result["error"])

    def test_search_requires_a_query_or_video(self):
        result = yt.youtube_player("search_and_play")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "missing_query")

    def test_video_ids_and_youtube_urls_are_validated(self):
        for value in ("abc", "not a url", "https://vimeo.com/123456",
                      "https://www.youtube.com/watch?v=short"):
            with self.subTest(value=value):
                with self.assertRaises(yt.YouTubeError) as context:
                    yt._extract_video_id(value)
                self.assertEqual(context.exception.code, "invalid_video_id")

    def test_bare_ids_watch_links_short_links_and_embeds_are_accepted(self):
        self.assertEqual(yt._extract_video_id("dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(
            yt._extract_video_id(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42"),
            "dQw4w9WgXcQ")
        self.assertEqual(
            yt._extract_video_id("https://youtu.be/dQw4w9WgXcQ?si=x"),
            "dQw4w9WgXcQ")
        self.assertEqual(
            yt._extract_video_id(
                "https://www.youtube.com/shorts/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ")

    def test_parameter_validation(self):
        self.assertEqual(
            yt.youtube_player("set_volume", volume=101)["error_code"],
            "invalid_parameter")
        self.assertEqual(
            yt.youtube_player("seek_to")["error_code"], "invalid_parameter")
        self.assertEqual(
            yt.youtube_player("seek_forward", seconds=-3)["error_code"],
            "invalid_parameter")
        self.assertEqual(
            yt.youtube_player("search_and_play", query="x",
                              autoplay="maybe")["error_code"],
            "invalid_parameter")
        self.assertEqual(
            yt.youtube_player("search_and_play", query="x" * 300)["error_code"],
            "invalid_query")

    def test_invalid_video_is_rejected_before_the_player_is_asked(self):
        result = yt.youtube_player("play", video="https://vimeo.com/1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "invalid_video_id")
        self.assertEqual(self.bridge.events, [])

    def test_missing_api_key_is_an_actionable_configuration_error(self):
        with mock.patch.object(yt, "_api_key", return_value=""):
            with self.assertRaises(yt.YouTubeError) as context:
                yt._request_json("search", {"part": "snippet"})
        self.assertEqual(context.exception.code, "youtube_not_configured")
        self.assertIn("youtube_api_key", context.exception.message)

    def test_quota_and_auth_errors_map_to_meaningful_codes(self):
        class Response:
            def __init__(self, status, reason):
                self.status_code = status
                self._reason = reason

            def json(self):
                return {"error": {"errors": [{"reason": self._reason}]}}

        class Client:
            def __init__(self, response):
                self.response = response

            def get(self, *args, **kwargs):
                return self.response

        with mock.patch.object(yt, "_api_key", return_value="fake-key"), \
             mock.patch.object(yt, "_http_client",
                               return_value=Client(
                                   Response(403, "quotaExceeded"))):
            with self.assertRaises(yt.YouTubeError) as quota:
                yt._request_json("search", {})
        self.assertEqual(quota.exception.code, "youtube_quota_exceeded")

        with mock.patch.object(yt, "_api_key", return_value="fake-key"), \
             mock.patch.object(yt, "_http_client",
                               return_value=Client(Response(403, "keyInvalid"))):
            with self.assertRaises(yt.YouTubeError) as auth:
                yt._request_json("search", {})
        self.assertEqual(auth.exception.code, "youtube_auth_error")

    def test_search_without_results_is_reported_honestly(self):
        with mock.patch.object(yt, "_request_json",
                               return_value={"items": []}):
            result = yt.youtube_player("search_and_play", query="nothing")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "no_results")
        self.assertEqual(self.bridge.events, [])


class SearchTests(YouTubeTestBase):
    def test_search_requests_video_metadata_and_embeddability(self):
        calls = []

        def fake_request(endpoint, params):
            calls.append((endpoint, dict(params)))
            if endpoint == "search":
                return {"items": [
                    {"id": {"videoId": "bbbbbbbbbbb"},
                     "snippet": {
                         "title": "Transformers explained",
                         "channelTitle": "AI Explained",
                         "channelId": "c1",
                         "description": "A clear explanation",
                         "publishedAt": "2026-01-01T00:00:00Z"}},
                    {"id": {"videoId": "ccccccccccc"},
                     "snippet": {
                         "title": "Cooking pasta",
                         "channelTitle": "Food",
                         "channelId": "c2",
                         "description": "Dinner",
                         "publishedAt": "2026-01-01T00:00:00Z"}},
                ]}
            return {"items": [
                {"id": "bbbbbbbbbbb",
                 "snippet": {"title": "Transformers explained",
                             "channelTitle": "AI Explained",
                             "channelId": "c1",
                             "description": "A clear explanation",
                             "publishedAt": "2026-01-01T00:00:00Z"},
                 "contentDetails": {"duration": "PT12M30S"},
                 "statistics": {"viewCount": "100000", "likeCount": "4000"},
                 "status": {"embeddable": True, "privacyStatus": "public",
                            "uploadStatus": "processed"}},
                {"id": "ccccccccccc",
                 "snippet": {"title": "Cooking pasta",
                             "channelTitle": "Food",
                             "channelId": "c2",
                             "description": "Dinner",
                             "publishedAt": "2026-01-01T00:00:00Z"},
                 "contentDetails": {"duration": "PT10M"},
                 "statistics": {"viewCount": "500000"},
                 "status": {"embeddable": False, "privacyStatus": "public",
                            "uploadStatus": "processed"}},
            ]}

        with mock.patch.object(yt, "_request_json", side_effect=fake_request):
            ranked, warnings = yt._search_candidates(
                "transformer architecture", "AI Explained")
        self.assertEqual([item["video_id"] for item in ranked],
                         ["bbbbbbbbbbb"])
        self.assertTrue(any("non-embeddable" in warning for warning in warnings))
        search_params = calls[0][1]
        self.assertEqual(search_params["type"], "video")
        self.assertEqual(search_params["videoEmbeddable"], "true")
        self.assertEqual(search_params["relevanceLanguage"], "en")
        self.assertEqual(search_params["hl"], "en")
        self.assertIn("AI Explained", search_params["q"])
        self.assertEqual(calls[1][0], "videos")
        self.assertIn("status", calls[1][1]["part"])
        self.assertEqual(ranked[0]["duration_seconds"], 750)

    def test_query_language_follows_the_request_script(self):
        self.assertEqual(yt._query_language("transformer architecture"), "en")
        self.assertEqual(yt._query_language("архитектура трансформеров"), "ru")
        self.assertEqual(yt._query_language("هندسة المحولات"), "ar")
        self.assertEqual(yt._query_language(""), "")

    def test_ranking_prefers_relevance_over_first_result(self):
        candidates = [
            candidate("aaaaaaaaaaa", "Random cooking recipe", channel="Food",
                      description="pasta", duration=600, views=2_000_000,
                      search_rank=0),
            candidate("bbbbbbbbbbb",
                      "Transformer architecture explained clearly",
                      channel="AI Explained",
                      description="How transformer architecture works",
                      duration=900, views=50_000, likes=5_000, search_rank=4),
        ]
        ranked = yt._rank_candidates(
            candidates, "how transformer architecture works")
        self.assertEqual(ranked[0]["video_id"], "bbbbbbbbbbb")
        self.assertGreater(ranked[0]["_score"], ranked[1]["_score"])

    def test_preferred_channel_is_honoured_and_duration_matters(self):
        candidates = [
            candidate("aaaaaaaaaaa", "AI agents with Python", channel="Other",
                      duration=600, views=900_000, search_rank=0),
            candidate("bbbbbbbbbbb", "AI agents with Python", channel="Fireship",
                      duration=600, views=100_000, search_rank=2),
        ]
        ranked = yt._rank_candidates(candidates, "AI agents", "Fireship")
        self.assertEqual(ranked[0]["channel"], "Fireship")

        shorts = candidate("ccccccccccc", "AI agents in 30 seconds",
                           channel="Fireship", duration=30, views=10_000_000,
                           search_rank=0)
        explainer = candidate("ddddddddddd", "AI agents with Python",
                              channel="Fireship", duration=720, views=80_000,
                              search_rank=3)
        ranked = yt._rank_candidates([shorts, explainer], "AI agents",
                                     "Fireship")
        self.assertGreater(ranked[0]["_score"], ranked[1]["_score"])

    def test_non_embeddable_and_private_videos_are_excluded(self):
        items = [
            {"id": "bbbbbbbbbbb",
             "snippet": {"title": "Good", "channelTitle": "C"},
             "contentDetails": {"duration": "PT5M"},
             "statistics": {},
             "status": {"embeddable": False, "privacyStatus": "public",
                        "uploadStatus": "processed"}},
            {"id": "ccccccccccc",
             "snippet": {"title": "Private", "channelTitle": "C"},
             "contentDetails": {"duration": "PT5M"},
             "statistics": {},
             "status": {"embeddable": True, "privacyStatus": "private",
                        "uploadStatus": "processed"}},
            {"id": "ddddddddddd",
             "snippet": {"title": "Usable", "channelTitle": "C"},
             "contentDetails": {"duration": "PT5M"},
             "statistics": {},
             "status": {"embeddable": True, "privacyStatus": "public",
                        "uploadStatus": "processed"}},
        ]
        parsed = [yt._parse_video_item(item) for item in items]
        self.assertEqual([item["video_id"] for item in parsed if item],
                         ["ddddddddddd"])

    def test_repeated_search_uses_the_cache(self):
        with mock.patch.object(yt, "_search_candidates",
                               return_value=([candidate("aaaaaaaaaaa",
                                                        "Topic")], [])) as search:
            first = self.service.search("Topic")
            second = self.service.search("Topic")
        self.assertEqual(first, second)
        self.assertEqual(search.call_count, 1)


class PlaybackTests(YouTubeTestBase):
    def test_search_and_play_opens_panel_loads_and_plays(self):
        videos = [candidate("bbbbbbbbbbb",
                            "Transformer architecture explained",
                            channel="AI Explained")]
        with mock.patch.object(yt, "_search_candidates",
                               return_value=(videos, [])):
            result = yt.youtube_player(
                "search_and_play", query="transformer architecture")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["video"]["video_id"], "bbbbbbbbbbb")
        self.assertEqual(result["state"]["playback_state"], "playing")
        self.assertIn("youtube_open_panel", self.bridge.types())
        load = next(event for event in self.bridge.events
                    if event["type"] == "youtube_load_video")
        self.assertTrue(load["autoplay"])
        self.assertEqual(load["video"]["video_id"], "bbbbbbbbbbb")
        self.assertTrue(load["request_id"].startswith("yt-"))

    def test_same_request_reuses_the_selected_video_without_searching(self):
        videos = [candidate("bbbbbbbbbbb", "Local LLMs explained")]
        with mock.patch.object(yt, "_search_candidates",
                               return_value=(videos, [])) as search:
            first = yt.youtube_player("search_and_play", query="local LLMs")
            second = yt.youtube_player("search_and_play", query="local LLMs")
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertTrue(second["reused"])
        self.assertEqual(search.call_count, 1)
        loads = [event for event in self.bridge.events
                 if event["type"] == "youtube_load_video"]
        self.assertEqual(len(loads), 1)

    def test_switching_video_keeps_one_panel_and_loads_the_new_video(self):
        first = [candidate("aaaaaaaaaaa", "Local LLMs explained")]
        second = [candidate("bbbbbbbbbbb", "Neural networks explained")]
        with mock.patch.object(yt, "_search_candidates",
                               return_value=(first, [])):
            yt.youtube_player("search_and_play", query="local LLMs")
        with mock.patch.object(yt, "_search_candidates",
                               return_value=(second, [])):
            result = yt.youtube_player(
                "search_and_play", query="neural networks")
        self.assertTrue(result["success"])
        opens = [event for event in self.bridge.events
                 if event["type"] == "youtube_open_panel"]
        loads = [event for event in self.bridge.events
                 if event["type"] == "youtube_load_video"]
        self.assertEqual(len(opens), 1)
        self.assertEqual([event["video"]["video_id"] for event in loads],
                         ["aaaaaaaaaaa", "bbbbbbbbbbb"])

    def test_another_video_on_the_same_topic_excludes_the_current_one(self):
        videos = [
            candidate("aaaaaaaaaaa", "Local LLMs explained"),
            candidate("bbbbbbbbbbb", "Local LLMs in practice"),
        ]
        with mock.patch.object(yt, "_search_candidates",
                               return_value=(videos, [])):
            first = yt.youtube_player("search_and_play", query="local LLMs")
            second = yt.youtube_player(
                "search_and_play", query="local LLMs",
                exclude_current=True)
        self.assertTrue(first["success"])
        self.assertTrue(second["success"], second)
        self.assertEqual(first["video"]["video_id"], "aaaaaaaaaaa")
        self.assertEqual(second["video"]["video_id"], "bbbbbbbbbbb")
        loads = [event for event in self.bridge.events
                 if event["type"] == "youtube_load_video"]
        self.assertEqual([event["video"]["video_id"] for event in loads],
                         ["aaaaaaaaaaa", "bbbbbbbbbbb"])

    def test_player_command_uses_a_request_id_and_real_result(self):
        self.service.handle_client_event({
            "type": "youtube_player_state",
            "state": player_state(candidate("aaaaaaaaaaa", "Test video")),
        })
        result = yt.youtube_player("pause")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["state"]["playback_state"], "paused")
        command = next(event for event in self.bridge.events
                       if event["type"] == "youtube_player_command")
        self.assertEqual(command["command"], "pause")
        self.assertTrue(command["request_id"])

    def test_command_failure_is_not_reported_as_success(self):
        self.service.handle_client_event({
            "type": "youtube_player_state",
            "state": player_state(candidate("aaaaaaaaaaa", "Test video")),
        })

        def fail(event):
            if event.get("type") == "youtube_player_command":
                self._respond(event["request_id"], player_state(
                    candidate("aaaaaaaaaaa", "Test video")),
                    ok=False,
                    error={"code": "command_failed",
                           "message": "The player refused."})

        self.bridge.handler = fail
        result = yt.youtube_player("pause")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "command_failed")
        self.assertIn("refused", result["error"])

    def test_command_timeout_is_bounded_and_reported(self):
        self.service.handle_client_event({
            "type": "youtube_player_state",
            "state": player_state(candidate("aaaaaaaaaaa", "Test video")),
        })
        self.bridge.handler = lambda event: None
        started = time.monotonic()
        result = self.service.player_command("pause", timeout=0.08)
        elapsed = time.monotonic() - started
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "command_timeout")
        self.assertLess(elapsed, 1.0)

    def test_disconnect_fails_a_waiting_command(self):
        self.service.handle_client_event({
            "type": "youtube_player_state",
            "state": player_state(candidate("aaaaaaaaaaa", "Test video")),
        })
        self.bridge.handler = lambda event: None
        results = []
        thread = threading.Thread(
            target=lambda: results.append(
                self.service.player_command("pause", timeout=3.0)))
        thread.start()
        deadline = time.monotonic() + 1.0
        while not self.bridge.events and time.monotonic() < deadline:
            time.sleep(0.01)
        self.service.browser_disconnected()
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["error_code"], "interface_disconnected")
        self.assertFalse(self.service.public_state()["player_ready"])

    def test_no_interface_means_no_search_or_fake_success(self):
        self.bridge.connected = False
        with mock.patch.object(yt, "_search_candidates") as search:
            result = yt.youtube_player("search_and_play", query="topic")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "interface_unavailable")
        search.assert_not_called()
        self.assertEqual(self.bridge.events, [])

    def test_embedded_failure_retries_a_later_candidate(self):
        videos = [
            candidate("aaaaaaaaaaa", "First candidate"),
            candidate("bbbbbbbbbbb", "Second candidate"),
        ]
        load_events = []

        def handler(event):
            if event.get("type") == "youtube_load_video":
                load_events.append(event)
                if len(load_events) == 1:
                    self._respond(event["request_id"], player_state(
                        event["video"]), ok=False,
                        error={"code": "embedding_disabled",
                               "message": "Owner disabled embedding."})
                else:
                    self._respond(event["request_id"],
                                  player_state(event["video"]))
            elif event.get("type") == "youtube_player_command":
                self._default_handler(event)

        self.bridge.handler = handler
        with mock.patch.object(yt, "_search_candidates",
                               return_value=(videos, [])):
            result = yt.youtube_player("search_and_play", query="topic")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["video"]["video_id"], "bbbbbbbbbbb")
        self.assertEqual(len(load_events), 2)

    def test_autoplay_blocked_is_reported_as_a_real_state(self):
        def handler(event):
            if event.get("type") == "youtube_load_video":
                self._respond(
                    event["request_id"],
                    player_state(event["video"], playback="paused",
                                 autoplay_blocked=True))
            elif event.get("type") == "youtube_player_command":
                self._default_handler(event)

        self.bridge.handler = handler
        with mock.patch.object(yt, "_search_candidates",
                               return_value=([candidate("aaaaaaaaaaa",
                                                        "Topic")], [])):
            result = yt.youtube_player("search_and_play", query="topic")
        self.assertTrue(result["success"])
        self.assertTrue(result["state"]["autoplay_blocked"])
        self.assertIn("blocked autoplay", result["result"])

    def test_direct_video_url_loads_without_search(self):
        video = candidate("dQw4w9WgXcQ", "Direct video", channel="Direct")
        with mock.patch.object(self.service, "_video_metadata",
                               return_value=(video, [])) as metadata, \
             mock.patch.object(yt, "_search_candidates") as search:
            result = yt.youtube_player(
                "play", video="https://youtu.be/dQw4w9WgXcQ")
        self.assertTrue(result["success"], result)
        metadata.assert_called_once_with("dQw4w9WgXcQ")
        search.assert_not_called()
        load = next(event for event in self.bridge.events
                    if event["type"] == "youtube_load_video")
        self.assertEqual(load["video"]["video_id"], "dQw4w9WgXcQ")

    def test_close_stops_and_removes_the_panel(self):
        self.service.handle_client_event({
            "type": "youtube_player_state",
            "state": player_state(candidate("aaaaaaaaaaa", "Test video")),
        })
        result = yt.youtube_player("close")
        self.assertTrue(result["success"])
        self.assertFalse(result["state"]["panel_open"])
        self.assertIn("youtube_close_panel", self.bridge.types())

    def test_get_status_reports_remaining_time_and_paused_state(self):
        video = candidate("aaaaaaaaaaa", "Test video", duration=600)
        self.service.handle_client_event({
            "type": "youtube_player_state",
            "state": player_state(video, playback="paused", current_time=150),
        })
        result = yt.youtube_player("get_status")
        self.assertTrue(result["success"])
        self.assertFalse(result["playing"])
        self.assertTrue(result["paused"])
        self.assertEqual(result["state"]["remaining_seconds"], 450.0)
        self.assertIn("7:30 left", result["result"])

    def test_get_status_when_closed_is_honest(self):
        result = yt.youtube_player("get_status")
        self.assertTrue(result["success"])
        self.assertFalse(result["playing"])
        self.assertIn("closed", result["result"].lower())

    def test_fullscreen_blocked_suggests_the_clickable_fallback(self):
        self.service.handle_client_event({
            "type": "youtube_player_state",
            "state": player_state(candidate("aaaaaaaaaaa", "Test video")),
        })

        def block(event):
            if event.get("type") == "youtube_player_command":
                self._respond(
                    event["request_id"],
                    player_state(candidate("aaaaaaaaaaa", "Test video")),
                    ok=False,
                    error={"code": "fullscreen_blocked",
                           "message": "Browser blocked it."})

        self.bridge.handler = block
        result = yt.youtube_player("fullscreen")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "fullscreen_blocked")
        self.assertIn("FULLSCREEN button", result["result"])


if __name__ == "__main__":
    unittest.main()

"""YouTube discovery and in-app playback for Jarvis.

The assistant never sends the user to a YouTube search page.  Given a natural
language request it asks the YouTube Data API for candidates, ranks them on
query relevance, channel preference, embedding availability, duration and
quality signals, then asks the already-open Jarvis frontend to load the chosen
video inside its own player panel.

One tool (``youtube_player``) exposes every operation.  The tool body runs in a
worker thread, so it blocks only that tool call while a browser request is in
flight; the Live conversation loop and the FastAPI server stay responsive.

Transport
---------
The browser is reached through the application's existing WebSocket event hub.
Commands carry a unique ``request_id`` and wait for the matching
``youtube_player_result``; a bounded timeout turns a silent browser into an
honest error.  This module never assumes a command succeeded because it was
published.

Configuration
-------------
Set ``youtube_api_key`` in ``config/api_keys.json`` (or the
``YOUTUBE_API_KEY`` environment variable).  The key never leaves the backend.
``youtube_region_code`` / ``YOUTUBE_REGION_CODE`` are optional and only used
for YouTube's region-restriction metadata.
"""
from __future__ import annotations

import copy
import hashlib
import html
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import httpx

import config as _config

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
SEARCH_MAX_RESULTS = 10
MAX_PLAY_ATTEMPTS = 3
MAX_QUERY_CHARS = 200
COMMAND_TIMEOUT = 6.0
LOAD_TIMEOUT = 12.0
STATUS_TIMEOUT = 2.5
SEARCH_CACHE_TTL = 600.0          # 10 minutes: avoids repeat quota spend
SEARCH_CACHE_ENTRIES = 8
VIDEO_CACHE_TTL = 900.0
VIDEO_CACHE_ENTRIES = 32
MAX_DESCRIPTION_CHARS = 2000

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?)?$"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_OPERATIONS = (
    "search_and_play", "play", "get_status",
    "pause", "resume", "toggle_playback", "stop",
    "seek_forward", "seek_backward", "seek_to",
    "volume_up", "volume_down", "set_volume", "mute", "unmute",
    "fullscreen", "exit_fullscreen", "close",
)
_PLAYBACK_STATES = frozenset({
    "unstarted", "cued", "playing", "paused", "buffering", "ended",
    "stopped", "loading", "error", "closed",
})
_RETRYABLE_LOAD_ERRORS = frozenset({
    "video_not_found", "video_unavailable", "embedding_disabled",
    "player_error", "invalid_video_id", "load_failed",
})
_YOUTUBE_HOSTS = frozenset({
    "youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "youtu.be",
})

_SEARCH_STOPWORDS = frozenset({
    "a", "an", "and", "are", "about", "as", "at", "be", "by", "can", "could",
    "do", "does", "explain", "explaining", "explanation", "find", "for",
    "from", "give", "good", "how", "i", "in", "into", "is", "it", "me",
    "my", "of", "on", "or", "please", "show", "some", "that", "the", "their",
    "this", "to", "us", "video", "videos", "want", "watch", "what", "which",
    "who", "why", "with", "work", "works", "would", "you", "your",
})


# ─────────────────────────────────────────────────────────────────────────────
# Errors and small value helpers
# ─────────────────────────────────────────────────────────────────────────────
class YouTubeError(Exception):
    """A user-facing failure with a stable machine code."""

    def __init__(self, code: str, message: str, *,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.code = str(code or "youtube_error")
        self.message = str(message or "YouTube operation failed.")
        self.retryable = bool(retryable)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _clean_text(value: Any, limit: int = 500) -> str:
    """Collapse whitespace and strip control characters from external text."""
    text = html.unescape(str(value or ""))
    text = _CONTROL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _norm(value: Any) -> str:
    return _clean_text(value, 1000).casefold()


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _parse_duration(value: Any) -> int:
    """ISO-8601 YouTube duration (``PT1H2M3S``) to whole seconds."""
    text = str(value or "").strip().upper()
    if not text:
        return 0
    match = _ISO_DURATION_RE.match(text)
    if not match:
        return 0
    parts = {key: _safe_int(raw) for key, raw in match.groupdict().items()}
    return max(0, parts["days"] * 86400 + parts["hours"] * 3600
               + parts["minutes"] * 60 + parts["seconds"])


def _format_time(seconds: Any) -> str:
    total = max(0, int(round(_safe_float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


def _thumbnail(video_id: str) -> str:
    return (f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            if video_id else "")


def _error(code: str, message: str, operation: str = "",
           **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "success": False,
        "operation": operation,
        "error": str(message),
        "error_code": str(code),
    }
    payload.update(extra)
    return payload


def _ok(operation: str, summary: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "success": True,
        "operation": operation,
        "result": summary,
    }
    payload.update(extra)
    return payload


def _extract_video_id(value: Any) -> str:
    """Accept a bare 11-character ID or any normal YouTube watch/share URL."""
    raw = _clean_text(value, 2000)
    if not raw:
        raise YouTubeError("invalid_video_id",
                           "A YouTube video ID or URL is required.")
    if _VIDEO_ID_RE.fullmatch(raw):
        return raw

    candidate = raw
    if "://" not in candidate:
        lowered = candidate.casefold().lstrip("/")
        if lowered.startswith(("www.", "youtube.com", "m.youtube.com",
                               "music.youtube.com", "youtu.be",
                               "youtube-nocookie.com")):
            candidate = "https://" + candidate
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise YouTubeError(
            "invalid_video_id",
            "That does not look like a YouTube video ID or YouTube link.")
    host = (parts.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    if host not in _YOUTUBE_HOSTS:
        raise YouTubeError(
            "invalid_video_id",
            "Only YouTube links can be played in the Jarvis player.")

    video_id = ""
    path = parts.path or "/"
    if host == "youtu.be":
        video_id = path.strip("/").split("/", 1)[0]
    elif path.startswith(("/shorts/", "/embed/", "/live/", "/v/")):
        video_id = path.strip("/").split("/", 2)[1]
    else:
        video_id = (parse_qs(parts.query).get("v") or [""])[0]

    video_id = video_id.strip()
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise YouTubeError(
            "invalid_video_id",
            "That YouTube link does not contain a valid 11-character video ID.")
    return video_id


def _validate_query(value: Any) -> str:
    query = _clean_text(value, MAX_QUERY_CHARS + 1)
    if not query:
        raise YouTubeError(
            "missing_query",
            "A search query is required, for example "
            "'transformer architecture explained'.")
    if len(query) > MAX_QUERY_CHARS:
        raise YouTubeError(
            "invalid_query",
            f"The search query is too long (maximum {MAX_QUERY_CHARS} characters).")
    return query


def _coerce_bool(value: Any, field_name: str, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in ("true", "yes", "1", "on"):
            return True
        if text in ("false", "no", "0", "off"):
            return False
    raise YouTubeError("invalid_parameter",
                       f"{field_name} must be true or false.")


def _coerce_number(value: Any, field_name: str,
                   default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise YouTubeError("invalid_parameter", f"{field_name} must be a number.")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise YouTubeError("invalid_parameter",
                           f"{field_name} must be a number.") from None
    if not math.isfinite(result):
        raise YouTubeError("invalid_parameter",
                           f"{field_name} must be a finite number.")
    return result


def _coerce_volume(value: Any, field_name: str = "volume",
                   required: bool = False) -> Optional[int]:
    if value is None or value == "":
        if required:
            raise YouTubeError("invalid_parameter",
                               f"{field_name} must be a number from 0 to 100.")
        return None
    if isinstance(value, bool):
        raise YouTubeError("invalid_parameter",
                           f"{field_name} must be a number from 0 to 100.")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise YouTubeError("invalid_parameter",
                           f"{field_name} must be a number from 0 to 100.") from None
    if result < 0 or result > 100:
        raise YouTubeError("invalid_parameter",
                           f"{field_name} must be between 0 and 100.")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# YouTube Data API client
# ─────────────────────────────────────────────────────────────────────────────
_client_lock = threading.Lock()
_client: Optional[httpx.Client] = None


def _api_key() -> str:
    """Read through config; the environment override is handled there too."""
    try:
        return str(_config.get_youtube_api_key() or "").strip()
    except Exception:
        return ""


_READINESS_LOCK = threading.Lock()
_READINESS_STATE: Dict[str, Any] = {
    "status": "",
    "detail": "",
    "at": 0.0,
    "key_digest": "",
}


def _key_digest(key: str) -> str:
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()[:16]


def _note_readiness(status: str, detail: str = "", key: str = "") -> None:
    """Remember the last verified Data API outcome (never the key)."""
    with _READINESS_LOCK:
        _READINESS_STATE["status"] = str(status or "")
        _READINESS_STATE["detail"] = str(detail or "")[:300]
        _READINESS_STATE["at"] = time.monotonic()
        _READINESS_STATE["key_digest"] = _key_digest(key) if key else ""


def readiness() -> Dict[str, Any]:
    """Truthful YouTube capability status without exposing the API key.

    A key that merely exists is ``configured_unverified``. ``ready`` is only
    reported after a real Data API request succeeded recently; a verified
    failure is reported as ``service_unavailable`` or
    ``invalid_configuration``.
    """
    import importlib.util

    result: Dict[str, Any] = {
        "capability": "youtube",
        "label": "YOUTUBE",
        "status": "unavailable",
        "detail": "",
        "configured": False,
    }
    key = _api_key()
    result["configured"] = bool(key)
    if not key:
        result["status"] = "missing_credentials"
        result["detail"] = ("A YouTube Data API v3 key is required for search. "
                            "Set youtube_api_key or YOUTUBE_API_KEY.")
        return result
    if len(key) < 20 or any(char.isspace() or ord(char) < 32 for char in key):
        result["status"] = "invalid_configuration"
        result["detail"] = ("The YouTube API key is malformed (too short or "
                            "contains whitespace/control characters).")
        return result
    if importlib.util.find_spec("httpx") is None:
        result["status"] = "missing_dependency"
        result["detail"] = "The httpx package needed for YouTube is missing."
        return result
    with _READINESS_LOCK:
        status = str(_READINESS_STATE.get("status") or "")
        detail = str(_READINESS_STATE.get("detail") or "")
        at = float(_READINESS_STATE.get("at") or 0.0)
        key_digest = str(_READINESS_STATE.get("key_digest") or "")
    if (status and time.monotonic() - at <= 900
            and key_digest == _key_digest(key)):
        result["status"] = status
        result["detail"] = detail or (
            "The YouTube Data API answered a recent request."
            if status == "ready" else "The last YouTube request failed.")
        return result
    result["status"] = "configured_unverified"
    result["detail"] = ("A YouTube API key is configured; no live request has "
                        "verified it yet. Direct video playback still works "
                        "without verification.")
    return result


def _http_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=httpx.Timeout(8.0, connect=4.0),
                follow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Jarvis/1.0 (local assistant)",
                },
            )
        return _client


def _reset_http_client() -> None:
    """Tests replace the transport; a closed client must never be reused."""
    global _client
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = None


def _api_error_reason(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict) and item.get("reason"):
                return str(item["reason"])
    return str(error.get("status") or "")


def _request_json(endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """One validated YouTube Data API request.

    Errors are translated into stable codes so the model can explain exactly
    what happened without the browser ever seeing the API key.
    """
    key = _api_key()
    if not key:
        _note_readiness(
            "missing_credentials",
            "A YouTube Data API v3 key is required for search.")
        raise YouTubeError(
            "youtube_not_configured",
            "YouTube search needs a YouTube Data API v3 key. Set "
            "'youtube_api_key' in config/api_keys.json (or YOUTUBE_API_KEY) "
            "and try again.")
    request_params = dict(params or {})
    request_params["key"] = key
    url = f"{YOUTUBE_API_BASE}/{endpoint.lstrip('/')}"
    try:
        response = _http_client().get(url, params=request_params)
    except httpx.TimeoutException:
        _note_readiness("service_unavailable",
                        "YouTube did not answer in time.", key=key)
        raise YouTubeError(
            "youtube_unavailable",
            "YouTube did not answer in time. Check the internet connection "
            "and try again.") from None
    except httpx.HTTPError as exc:
        _note_readiness(
            "service_unavailable",
            f"YouTube could not be reached ({type(exc).__name__}).", key=key)
        raise YouTubeError(
            "youtube_unavailable",
            f"YouTube could not be reached ({type(exc).__name__}).") from None

    status = int(response.status_code)
    if status >= 400:
        try:
            data = response.json()
        except Exception:
            data = None
        reason = _api_error_reason(data)
        lowered = reason.casefold()
        if lowered in ("quotaexceeded", "dailylimitexceeded",
                       "ratelimitexceeded") or status == 429:
            _note_readiness("service_unavailable",
                            "The YouTube Data API quota is exhausted.", key=key)
            raise YouTubeError(
                "youtube_quota_exceeded",
                "The YouTube Data API quota is exhausted. Try again after the "
                "daily quota resets or use another API key.")
        if lowered in ("keyinvalid", "accessnotconfigured", "iprefererblocked",
                       "forbidden", "servicedisabled"):
            _note_readiness(
                "invalid_configuration",
                "YouTube rejected the API credentials or the API is disabled "
                "for the project.", key=key)
            raise YouTubeError(
                "youtube_auth_error",
                "YouTube rejected the API credentials. Check that the YouTube "
                "Data API v3 is enabled for the project and the key is valid.")
        if status == 400:
            raise YouTubeError(
                "invalid_query",
                "YouTube rejected that search request. Try a more specific "
                "topic.")
        if status == 404:
            raise YouTubeError(
                "video_not_found", "YouTube did not find that video.")
        _note_readiness("service_unavailable",
                        f"YouTube returned HTTP {status}.", key=key)
        raise YouTubeError(
            "youtube_unavailable",
            f"YouTube returned HTTP {status}. Try again in a moment.")
    try:
        data = response.json()
    except Exception:
        _note_readiness("service_unavailable",
                        "YouTube returned unreadable JSON.", key=key)
        raise YouTubeError(
            "youtube_bad_response",
            "YouTube returned a response that was not readable JSON.") from None
    if not isinstance(data, dict):
        _note_readiness("service_unavailable",
                        "YouTube returned an unexpected response.", key=key)
        raise YouTubeError(
            "youtube_bad_response",
            "YouTube returned an unexpected response shape.")
    _note_readiness("ready", "The YouTube Data API answered a recent request.",
                    key=key)
    return data


def _parse_video_item(item: Dict[str, Any], *,
                      base: Optional[Dict[str, Any]] = None,
                      search_rank: int = 99) -> Optional[Dict[str, Any]]:
    """Turn one videos.list item into a validated internal candidate."""
    if not isinstance(item, dict):
        return None
    video_id = _clean_text(item.get("id"), 20)
    if not _VIDEO_ID_RE.fullmatch(video_id):
        return None
    snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    details = (item.get("contentDetails")
               if isinstance(item.get("contentDetails"), dict) else {})
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    stats = (item.get("statistics")
             if isinstance(item.get("statistics"), dict) else {})
    base = base or {}

    if status.get("privacyStatus") not in (None, "public"):
        return None
    if status.get("uploadStatus") not in (None, "processed"):
        return None
    if status.get("embeddable") is not True:
        return None
    if snippet.get("liveBroadcastContent") == "upcoming":
        return None

    region = ""
    try:
        region = _config.get_youtube_region_code()
    except Exception:
        region = ""
    restriction = details.get("regionRestriction")
    if region and isinstance(restriction, dict):
        blocked = restriction.get("blocked")
        allowed = restriction.get("allowed")
        if isinstance(blocked, list) and region in [str(v) for v in blocked]:
            return None
        if (isinstance(allowed, list) and allowed
                and region not in [str(v) for v in allowed]):
            return None

    title = _clean_text(snippet.get("title") or base.get("title"), 300)
    if not title:
        return None
    channel = _clean_text(snippet.get("channelTitle")
                          or base.get("channel"), 200)
    description = _clean_text(
        snippet.get("description") or base.get("description"),
        MAX_DESCRIPTION_CHARS)
    tags_value = snippet.get("tags")
    tags = ([_clean_text(tag, 80) for tag in tags_value
             if isinstance(tag, str)]
            if isinstance(tags_value, list) else [])
    duration = _parse_duration(details.get("duration"))
    live = _clean_text(snippet.get("liveBroadcastContent"), 20)

    return {
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "channel_id": _clean_text(snippet.get("channelId")
                                  or base.get("channel_id"), 80),
        "description": description,
        "tags": tags,
        "duration_seconds": duration,
        "view_count": max(0, _safe_int(stats.get("viewCount"))),
        "like_count": max(0, _safe_int(stats.get("likeCount"))),
        "published_at": _clean_text(snippet.get("publishedAt")
                                    or base.get("published_at"), 40),
        "live": live == "live",
        "embeddable": True,
        "url": _video_url(video_id),
        "thumbnail": _thumbnail(video_id),
        "search_rank": max(0, int(search_rank)),
        "_score": 0.0,
    }


def _query_language(query: str) -> str:
    """Best-effort script detection so titles arrive in the request language.

    YouTube localizes snippet titles to the region's interface language when
    no ``hl``/``relevanceLanguage`` is supplied.  On a UZ install that turned
    English searches into Russian titles, which then ranked poorly against the
    English query.  The query itself is the only reliable signal here.
    """
    text = str(query or "")
    if re.search(r"[\u0400-\u04FF]", text):
        return "ru"
    if re.search(r"[\u0600-\u06FF]", text):
        return "ar"
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    if re.search(r"[\u3040-\u30FF]", text):
        return "ja"
    if re.search(r"[\uAC00-\uD7AF]", text):
        return "ko"
    if re.search(r"[\u4E00-\u9FFF]", text):
        return "zh-Hans"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return ""


def _search_candidates(query: str,
                       preferred_channel: str = "") -> Tuple[
                           List[Dict[str, Any]], List[str]]:
    """Search and validate candidates.  Raises on a genuine API failure."""
    search_query = query
    if preferred_channel:
        search_query = f"{query} {preferred_channel}".strip()
    language = _query_language(query)
    params: Dict[str, Any] = {
        "part": "snippet",
        "q": search_query,
        "type": "video",
        "maxResults": SEARCH_MAX_RESULTS,
        "order": "relevance",
        "videoEmbeddable": "true",
        "safeSearch": "moderate",
    }
    if language:
        # Without these YouTube localizes titles into the viewer's region
        # language, which breaks title/query ranking for English requests.
        params["relevanceLanguage"] = language
        params["hl"] = language
    try:
        region = _config.get_youtube_region_code()
        if region:
            params["regionCode"] = region
    except Exception:
        pass

    search_data = _request_json("search", params)
    items = search_data.get("items")
    if not isinstance(items, list):
        raise YouTubeError(
            "youtube_bad_response",
            "YouTube returned search results in an unexpected shape.")
    found: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for rank, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        identity = item.get("id")
        video_id = (_clean_text(identity.get("videoId"), 20)
                    if isinstance(identity, dict) else "")
        if not _VIDEO_ID_RE.fullmatch(video_id) or video_id in seen:
            continue
        seen.add(video_id)
        snippet = item.get("snippet")
        snippet = snippet if isinstance(snippet, dict) else {}
        found.append({
            "video_id": video_id,
            "title": _clean_text(snippet.get("title"), 300),
            "channel": _clean_text(snippet.get("channelTitle"), 200),
            "channel_id": _clean_text(snippet.get("channelId"), 80),
            "description": _clean_text(snippet.get("description"),
                                       MAX_DESCRIPTION_CHARS),
            "published_at": _clean_text(snippet.get("publishedAt"), 40),
            "search_rank": rank,
        })
    if not found:
        return [], []

    videos_data = _request_json("videos", {
        "part": "snippet,contentDetails,statistics,status",
        "id": ",".join(item["video_id"] for item in found),
        "maxResults": len(found),
        **({"hl": language} if language else {}),
    })
    video_items = videos_data.get("items")
    if not isinstance(video_items, list):
        raise YouTubeError(
            "youtube_bad_response",
            "YouTube returned video details in an unexpected shape.")
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in video_items:
        if isinstance(item, dict):
            identity = _clean_text(item.get("id"), 20)
            if identity:
                by_id[identity] = item

    candidates: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for base in found:
        item = by_id.get(base["video_id"])
        if item is None:
            continue
        candidate = _parse_video_item(
            item, base=base, search_rank=base["search_rank"])
        if candidate is not None:
            candidates.append(candidate)
        else:
            warnings.append(
                f"Skipped an unavailable or non-embeddable result: "
                f"{base.get('title') or base['video_id']}")
    if not candidates:
        return [], warnings
    return _rank_candidates(candidates, query, preferred_channel), warnings


# ─────────────────────────────────────────────────────────────────────────────
# Ranking
# ─────────────────────────────────────────────────────────────────────────────
def _stem(token: str) -> str:
    token = token.casefold()
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > 5 and token.endswith(suffix):
            return token[:-len(suffix)]
    return token


def _tokens(value: Any) -> List[str]:
    words = re.findall(r"[^\W_]+", _clean_text(value, 2000).casefold(),
                       flags=re.UNICODE)
    result: List[str] = []
    for word in words:
        if len(word) < 2 or word in _SEARCH_STOPWORDS:
            continue
        stem = _stem(word)
        if stem and stem not in result:
            result.append(stem)
    return result


def _token_hit(query_token: str, title_token: str) -> bool:
    if query_token == title_token:
        return True
    if len(query_token) >= 4 and len(title_token) >= 4:
        return (query_token.startswith(title_token)
                or title_token.startswith(query_token))
    return False


def _coverage(query_tokens: List[str], text_tokens: List[str]) -> float:
    if not query_tokens:
        return 0.0
    hits = sum(1 for token in query_tokens
               if any(_token_hit(token, other) for other in text_tokens))
    return hits / len(query_tokens)


def _channel_strength(preferred: str, channel: str) -> float:
    if not preferred:
        return 0.0
    if _norm(preferred) == _norm(channel):
        return 9.0
    pref_tokens = _tokens(preferred)
    channel_tokens = _tokens(channel)
    if not pref_tokens:
        return 0.0
    coverage = _coverage(pref_tokens, channel_tokens)
    if coverage >= 1.0:
        return 7.5
    return coverage * 4.0


def _duration_bonus(seconds: int) -> float:
    if seconds <= 0:
        return -0.6                  # live/unknown length: harder to commit to
    if seconds < 45:
        return -2.6                  # a Short is rarely what "explain X" means
    if seconds < 90:
        return -1.4
    if seconds < 300:
        return 0.4
    if seconds <= 1200:
        return 1.6                   # 5-20 minutes: the explainer sweet spot
    if seconds <= 2700:
        return 1.0
    if seconds <= 5400:
        return 0.2
    if seconds <= 7200:
        return -0.7
    return -1.6


def _recency_bonus(published_at: str) -> float:
    text = str(published_at or "").strip()
    if not text:
        return 0.0
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds()
                       / 86400)
    except (TypeError, ValueError):
        return 0.0
    if age_days <= 30:
        return 0.5
    if age_days <= 365:
        return 0.3
    return 0.0


def _rank_candidates(candidates: List[Dict[str, Any]], query: str,
                     preferred_channel: str = "") -> List[Dict[str, Any]]:
    """Rank validated candidates instead of trusting the first search hit.

    The score deliberately combines several independent signals: how much of
    the request appears in the title (strongest), the description, the preferred
    channel, embedding availability/status, a sensible explainer duration,
    popularity and a little recency.  Unembeddable or non-public videos never
    reach this function.
    """
    usable = [candidate for candidate in candidates
              if isinstance(candidate, dict)
              and _VIDEO_ID_RE.fullmatch(str(candidate.get("video_id") or ""))
              and candidate.get("embeddable") is not False]
    if not usable:
        return []

    if preferred_channel:
        matches = [candidate for candidate in usable
                   if _channel_strength(preferred_channel,
                                        str(candidate.get("channel") or "")) >= 7.0]
        if matches:
            usable = matches

    query_tokens = _tokens(query)
    ranked: List[Tuple[float, int, Dict[str, Any]]] = []
    for position, candidate in enumerate(usable):
        title_tokens = _tokens(candidate.get("title"))
        description_tokens = _tokens(
            f"{candidate.get('description') or ''} "
            f"{' '.join(candidate.get('tags') or [])}")
        title_coverage = _coverage(query_tokens, title_tokens)
        description_coverage = _coverage(query_tokens, description_tokens)

        score = title_coverage * 9.0 + description_coverage * 1.2
        if title_coverage >= 0.999:
            score += 1.0
        score += _channel_strength(preferred_channel,
                                   str(candidate.get("channel") or ""))

        views = max(0, _safe_int(candidate.get("view_count")))
        likes = max(0, _safe_int(candidate.get("like_count")))
        score += min(7.0, math.log10(views + 1) * 0.8)
        if views > 0 and likes > 0:
            score += min(2.0, (likes / views) * 100.0 * 0.18)

        score += _duration_bonus(_safe_int(candidate.get("duration_seconds")))
        score += _recency_bonus(str(candidate.get("published_at") or ""))
        rank = max(0, _safe_int(candidate.get("search_rank"), 99))
        score += max(0.0, 10.0 - rank) * 0.12
        if candidate.get("live"):
            score -= 2.2
        title = str(candidate.get("title") or "")
        if title and title == title.upper() and any(ch.isalpha() for ch in title):
            score -= 0.6
        if re.search(r"[!?]{3,}", title):
            score -= 0.4

        candidate = dict(candidate)
        candidate["_score"] = round(score, 3)
        ranked.append((score, position, candidate))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [candidate for _score, _position, candidate in ranked]


def _video_payload(candidate: Dict[str, Any]) -> Dict[str, Any]:
    video_id = str(candidate.get("video_id") or "")
    return {
        "video_id": video_id,
        "title": _clean_text(candidate.get("title"), 300) or "YouTube video",
        "channel": _clean_text(candidate.get("channel"), 200),
        "duration_seconds": max(0, _safe_int(
            candidate.get("duration_seconds"))),
        "url": _video_url(video_id),
        "thumbnail": _thumbnail(video_id),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Player state / transport
# ─────────────────────────────────────────────────────────────────────────────
def _empty_state() -> Dict[str, Any]:
    return {
        "panel_open": False,
        "player_ready": False,
        "video_id": "",
        "title": "",
        "channel": "",
        "playback_state": "closed",
        "current_time": 0.0,
        "duration": 0.0,
        "volume": 100,
        "muted": False,
        "fullscreen": False,
        "autoplay_blocked": False,
        "error": None,
        "updated_at": _now_iso(),
        "_updated_monotonic": 0.0,
    }


def _normalize_error(value: Any) -> Optional[Dict[str, str]]:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        code = _clean_text(value.get("code"), 80) or "player_error"
        message = _clean_text(value.get("message"), 500) \
            or "The YouTube player reported a failure."
        return {"code": code, "message": message}
    text = _clean_text(value, 500)
    return {"code": "player_error", "message": text} if text else None


@dataclass
class _PendingRequest:
    request_id: str
    created_at: float = field(default_factory=time.monotonic)
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[Dict[str, Any]] = None


class YouTubePlayerService:
    """One process-wide player state + request/response bridge.

    ``publish`` sends a JSON event through the existing event hub and
    ``has_clients`` reports whether any local interface is connected.  Tests
    can pass either directly; the runtime configures them from ``EventHub``.
    """

    def __init__(
        self,
        *,
        publish: Optional[Callable[[Dict[str, Any]], None]] = None,
        has_clients: Optional[Callable[[], bool]] = None,
        command_timeout: float = COMMAND_TIMEOUT,
        load_timeout: float = LOAD_TIMEOUT,
        status_timeout: float = STATUS_TIMEOUT,
    ) -> None:
        self._publish = publish or (lambda _event: None)
        self._has_clients = has_clients or (lambda: False)
        self.command_timeout = max(0.05, float(command_timeout))
        self.load_timeout = max(0.05, float(load_timeout))
        self.status_timeout = max(0.05, float(status_timeout))

        self._state_lock = threading.RLock()
        self._state = _empty_state()
        self._pending_lock = threading.Lock()
        self._pending: Dict[str, _PendingRequest] = {}

        self._search_cache: "OrderedDict[str, Tuple[float, List[Dict[str, Any]]]]" = \
            OrderedDict()
        self._video_cache: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = \
            OrderedDict()
        self._cache_lock = threading.Lock()
        self._source_query = ""
        self._source_key = ""
        self._selected_at = 0.0

    # ── runtime wiring ───────────────────────────────────────────────────────

    def configure(
        self,
        *,
        publish: Optional[Callable[[Dict[str, Any]], None]] = None,
        has_clients: Optional[Callable[[], bool]] = None,
    ) -> None:
        if publish is not None:
            self._publish = publish
        if has_clients is not None:
            self._has_clients = has_clients

    def reset(self) -> None:
        """Forget state, pending requests and caches (tests / hard reset)."""
        with self._state_lock:
            self._state = _empty_state()
            self._source_query = ""
            self._source_key = ""
            self._selected_at = 0.0
        self._fail_pending(
            "The YouTube player session was reset.",
            code="interface_disconnected")
        with self._cache_lock:
            self._search_cache.clear()
            self._video_cache.clear()

    def _clients_available(self) -> bool:
        try:
            return bool(self._has_clients())
        except Exception:
            return False

    # ── state ────────────────────────────────────────────────────────────────

    def _snapshot_locked(self) -> Dict[str, Any]:
        state = dict(self._state)
        duration = max(0.0, _safe_float(state.get("duration")))
        current = max(0.0, _safe_float(state.get("current_time")))
        if duration:
            current = min(current, duration)
        remaining = max(0.0, duration - current) if duration else 0.0
        video_id = str(state.get("video_id") or "")
        public = {
            key: value for key, value in state.items()
            if not key.startswith("_")
        }
        public.update({
            "current_time": round(current, 2),
            "duration": round(duration, 2),
            "remaining_seconds": round(remaining, 2),
            "video_url": _video_url(video_id),
            "thumbnail": _thumbnail(video_id) if video_id else "",
        })
        public["status_text"] = self._status_text(public)
        return public

    def public_state(self) -> Dict[str, Any]:
        with self._state_lock:
            return copy.deepcopy(self._snapshot_locked())

    @staticmethod
    def _status_text(state: Dict[str, Any]) -> str:
        if not state.get("panel_open"):
            return "The YouTube panel is closed. Nothing is playing."
        error = state.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"The YouTube player reported an error: {error['message']}"
        if not state.get("video_id"):
            return "The YouTube panel is open and waiting for a video."
        title = state.get("title") or "the selected video"
        channel = state.get("channel") or ""
        author = f" by {channel}" if channel else ""
        playback = str(state.get("playback_state") or "loading")
        duration = _safe_float(state.get("duration"))
        current = _safe_float(state.get("current_time"))
        if playback == "loading" or not state.get("player_ready"):
            return f"Loading {title}{author}."
        position = ""
        if duration:
            position = (f" at {_format_time(current)} of "
                        f"{_format_time(duration)}")
        if playback == "playing":
            suffix = ""
            if duration:
                suffix = f" ({_format_time(max(0.0, duration - current))} left)"
            return f"Playing {title}{author}{position}{suffix}."
        if playback == "paused":
            if duration:
                return (f"Paused {title}{author}{position} "
                        f"({_format_time(max(0.0, duration - current))} left).")
            return f"Paused {title}{author}{position}."
        if playback == "buffering":
            return f"Buffering {title}{author}."
        if playback == "ended":
            return f"{title}{author} has ended."
        if playback == "stopped":
            return f"Stopped {title}{author}."
        if playback == "error":
            return f"{title}{author} could not be played."
        return f"{title}{author} is {playback}{position}."

    def _merge_state(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        with self._state_lock:
            state = self._state
            video_changed = False
            if "video_id" in raw:
                video_id = _clean_text(raw.get("video_id"), 20)
                if video_id and not _VIDEO_ID_RE.fullmatch(video_id):
                    video_id = ""
                if video_id != state.get("video_id"):
                    video_changed = True
                    state["video_id"] = video_id
                    if not video_id:
                        state["title"] = ""
                        state["channel"] = ""
                        state["duration"] = 0.0
                        state["current_time"] = 0.0
            if "title" in raw:
                title = _clean_text(raw.get("title"), 300)
                if title or video_changed or not state.get("video_id"):
                    state["title"] = title
            if "channel" in raw:
                channel = _clean_text(raw.get("channel"), 200)
                if channel or video_changed:
                    state["channel"] = channel
            if "playback_state" in raw:
                playback = _clean_text(raw.get("playback_state"), 30).casefold()
                if playback in _PLAYBACK_STATES:
                    state["playback_state"] = playback
            if "current_time" in raw:
                state["current_time"] = max(
                    0.0, _safe_float(raw.get("current_time"),
                                     _safe_float(state.get("current_time"))))
            if "duration" in raw:
                state["duration"] = max(
                    0.0, _safe_float(raw.get("duration"),
                                     _safe_float(state.get("duration"))))
            if "volume" in raw:
                volume = _safe_int(raw.get("volume"),
                                   _safe_int(state.get("volume"), 100))
                state["volume"] = max(0, min(100, volume))
            if "muted" in raw and isinstance(raw.get("muted"), bool):
                state["muted"] = raw["muted"]
            if "fullscreen" in raw and isinstance(raw.get("fullscreen"), bool):
                state["fullscreen"] = raw["fullscreen"]
            if ("autoplay_blocked" in raw
                    and isinstance(raw.get("autoplay_blocked"), bool)):
                state["autoplay_blocked"] = raw["autoplay_blocked"]
            if "player_ready" in raw and isinstance(raw.get("player_ready"), bool):
                state["player_ready"] = raw["player_ready"]
            if "panel_open" in raw and isinstance(raw.get("panel_open"), bool):
                state["panel_open"] = raw["panel_open"]
            if "error" in raw:
                state["error"] = _normalize_error(raw.get("error"))
            if state["panel_open"] is False:
                state["playback_state"] = "closed"
                state["player_ready"] = False
                state["video_id"] = ""
                state["title"] = ""
                state["channel"] = ""
                state["current_time"] = 0.0
                state["duration"] = 0.0
                state["fullscreen"] = False
                state["autoplay_blocked"] = False
            state["updated_at"] = _now_iso()
            state["_updated_monotonic"] = time.monotonic()

    def _set_video_state(self, video: Dict[str, Any], *,
                         panel_open: bool = True) -> None:
        payload = _video_payload(video)
        with self._state_lock:
            state = self._state
            state["panel_open"] = bool(panel_open)
            state["video_id"] = payload["video_id"]
            state["title"] = payload["title"]
            state["channel"] = payload["channel"]
            state["duration"] = float(payload["duration_seconds"])
            state["current_time"] = 0.0
            state["playback_state"] = "loading"
            state["autoplay_blocked"] = False
            state["error"] = None
            state["updated_at"] = _now_iso()
            state["_updated_monotonic"] = time.monotonic()

    # ── event transport ──────────────────────────────────────────────────────

    def _publish_event(self, event: Dict[str, Any]) -> None:
        try:
            self._publish(event)
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
            raise YouTubeError(
                "interface_unavailable",
                f"Could not reach the Jarvis player interface: {exc}") from exc

    def _register_pending(self, request_id: str) -> _PendingRequest:
        pending = _PendingRequest(request_id=request_id)
        with self._pending_lock:
            self._pending[request_id] = pending
        return pending

    def _discard_pending(self, request_id: str) -> None:
        with self._pending_lock:
            self._pending.pop(request_id, None)

    def _resolve_pending(self, request_id: str,
                         response: Dict[str, Any]) -> None:
        with self._pending_lock:
            pending = self._pending.pop(request_id, None)
        if pending is not None:
            pending.response = response
            pending.event.set()

    def _fail_pending(self, message: str, *, code: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.response = {
                "ok": False,
                "error": {"code": code, "message": message},
            }
            item.event.set()

    def browser_disconnected(self) -> None:
        """Fail in-flight work when the last local interface goes away.

        The panel state is kept so a reconnecting browser can rebuild the same
        video from the bootstrap snapshot; only the live player readiness is
        cleared, because the player instance died with the socket.
        """
        with self._state_lock:
            if self._state.get("panel_open"):
                self._state["player_ready"] = False
                self._state["_updated_monotonic"] = time.monotonic()
                self._state["updated_at"] = _now_iso()
        self._fail_pending(
            "The Jarvis interface disconnected before the player answered.",
            code="interface_disconnected")

    def _request(self, event_type: str, payload: Dict[str, Any], *,
                 timeout: float) -> Dict[str, Any]:
        if not self._clients_available():
            return {
                "ok": False,
                "error": {
                    "code": "interface_unavailable",
                    "message": ("No Jarvis interface is connected, so the "
                                "YouTube player cannot be controlled."),
                },
            }
        request_id = f"yt-{uuid.uuid4().hex}"
        pending = self._register_pending(request_id)
        event = {
            "type": event_type,
            "request_id": request_id,
            "ts": _now_iso(),
        }
        event.update(payload or {})
        try:
            self._publish_event(event)
        except YouTubeError as exc:
            self._discard_pending(request_id)
            return {"ok": False, "error": {"code": exc.code,
                                           "message": exc.message}}
        if not pending.event.wait(max(0.05, float(timeout))):
            self._discard_pending(request_id)
            return {
                "ok": False,
                "error": {
                    "code": "command_timeout",
                    "message": ("The YouTube player did not answer within "
                                f"{float(timeout):.1f} seconds. It may still "
                                "be loading; try again in a moment."),
                },
            }
        response = pending.response or {}
        state = response.get("state")
        if isinstance(state, dict):
            self._merge_state(state)
        error = _normalize_error(response.get("error"))
        ok = bool(response.get("ok", error is None))
        if error is not None:
            ok = False
        if not ok and error is None:
            error = {
                "code": "player_error",
                "message": "The YouTube player reported a failure.",
            }
        return {
            "ok": ok,
            "state": self.public_state(),
            "error": error,
            "request_id": request_id,
        }

    def handle_client_event(self, event: Dict[str, Any]) -> None:
        """Merge one browser message.  Safe to call from any thread."""
        if not isinstance(event, dict):
            return
        kind = str(event.get("type") or "")
        request_id = _clean_text(event.get("request_id"), 120)
        state = event.get("state")
        if kind in ("youtube_player_ready", "youtube_player_state"):
            self._merge_state(state)
            if kind == "youtube_player_ready" and isinstance(state, dict):
                self._merge_state({"player_ready": True})
            if request_id:
                self._resolve_pending(request_id, {
                    "ok": True, "state": self.public_state()})
            return
        if kind == "youtube_player_result":
            if isinstance(state, dict):
                self._merge_state(state)
            error = _normalize_error(event.get("error"))
            ok = bool(event.get("ok", error is None))
            if error is not None:
                ok = False
            if not ok and error is None:
                error = {
                    "code": "player_error",
                    "message": "The YouTube player reported a failure.",
                }
            if request_id:
                self._resolve_pending(request_id, {
                    "ok": ok,
                    "state": self.public_state(),
                    "error": error,
                })
            return
        if kind == "youtube_player_error":
            error = _normalize_error(event.get("error")) or {
                "code": "player_error",
                "message": "The YouTube player reported a failure.",
            }
            with self._state_lock:
                self._state["error"] = error
                self._state["updated_at"] = _now_iso()
                self._state["_updated_monotonic"] = time.monotonic()
            if isinstance(state, dict):
                self._merge_state(state)
            if request_id:
                self._resolve_pending(request_id, {
                    "ok": False,
                    "state": self.public_state(),
                    "error": error,
                })

    # ── YouTube metadata / search ────────────────────────────────────────────

    def _cache_get_search(self, key: str) -> Optional[List[Dict[str, Any]]]:
        now = time.monotonic()
        with self._cache_lock:
            entry = self._search_cache.get(key)
            if entry is None:
                return None
            expiry, candidates = entry
            if expiry < now:
                self._search_cache.pop(key, None)
                return None
            self._search_cache.move_to_end(key)
            return copy.deepcopy(candidates)

    def _cache_put_search(self, key: str,
                          candidates: List[Dict[str, Any]]) -> None:
        with self._cache_lock:
            self._search_cache[key] = (
                time.monotonic() + SEARCH_CACHE_TTL,
                copy.deepcopy(candidates))
            self._search_cache.move_to_end(key)
            while len(self._search_cache) > SEARCH_CACHE_ENTRIES:
                self._search_cache.popitem(last=False)

    def _cache_get_video(self, video_id: str) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        with self._cache_lock:
            entry = self._video_cache.get(video_id)
            if entry is None:
                return None
            expiry, candidate = entry
            if expiry < now:
                self._video_cache.pop(video_id, None)
                return None
            self._video_cache.move_to_end(video_id)
            return copy.deepcopy(candidate)

    def _cache_put_video(self, video_id: str,
                         candidate: Dict[str, Any]) -> None:
        with self._cache_lock:
            self._video_cache[video_id] = (
                time.monotonic() + VIDEO_CACHE_TTL,
                copy.deepcopy(candidate))
            self._video_cache.move_to_end(video_id)
            while len(self._video_cache) > VIDEO_CACHE_ENTRIES:
                self._video_cache.popitem(last=False)

    def search(self, query: str, channel: str = "") -> List[Dict[str, Any]]:
        key = f"{_norm(query)}\x00{_norm(channel)}"
        cached = self._cache_get_search(key)
        if cached is not None:
            return cached
        candidates, _warnings = _search_candidates(query, channel)
        if candidates:
            self._cache_put_search(key, candidates)
        return candidates

    def _video_metadata(self, video_id: str) -> Tuple[
            Dict[str, Any], List[str]]:
        """Metadata for a user-supplied ID; direct playback works without a key."""
        cached = self._cache_get_video(video_id)
        if cached is not None:
            return cached, []
        minimal = {
            "video_id": video_id,
            "title": "YouTube video",
            "channel": "",
            "duration_seconds": 0,
            "view_count": 0,
            "like_count": 0,
            "published_at": "",
            "embeddable": True,
            "live": False,
            "url": _video_url(video_id),
            "thumbnail": _thumbnail(video_id),
        }
        if not _api_key():
            return minimal, [
                "The YouTube API key is not configured, so the title and "
                "channel could not be looked up."]
        try:
            data = _request_json("videos", {
                "part": "snippet,contentDetails,statistics,status",
                "id": video_id,
                "maxResults": 1,
                "hl": "en",
            })
        except YouTubeError as exc:
            if exc.code == "video_not_found":
                raise
            # A quota/auth/network failure must not prevent a user-supplied
            # video from loading; the player itself is the final authority.
            return minimal, [exc.message]
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise YouTubeError(
                "video_not_found",
                "YouTube has no playable video with that ID.")
        candidate = _parse_video_item(items[0])
        if candidate is None:
            raise YouTubeError(
                "video_unavailable",
                "That video is private, unavailable in this region, or does "
                "not allow embedded playback.")
        self._cache_put_video(video_id, candidate)
        return candidate, []

    # ── playback flows ───────────────────────────────────────────────────────

    def _open_panel(self, video: Optional[Dict[str, Any]],
                    autoplay: bool) -> None:
        payload = {
            "type": "youtube_open_panel",
            "video": _video_payload(video) if video else None,
            "autoplay": bool(autoplay),
            "ts": _now_iso(),
        }
        self._publish_event(payload)

    def load_video(self, video: Dict[str, Any], *, autoplay: bool,
                   start_seconds: float = 0.0) -> Dict[str, Any]:
        payload = _video_payload(video)
        if not _VIDEO_ID_RE.fullmatch(payload["video_id"]):
            raise YouTubeError("invalid_video_id",
                               "The selected video ID is not valid.")
        with self._state_lock:
            was_open = bool(self._state.get("panel_open"))
        self._set_video_state(payload, panel_open=True)
        if not was_open:
            self._open_panel(payload, autoplay)
        return self._request("youtube_load_video", {
            "video": payload,
            "autoplay": bool(autoplay),
            "start_seconds": max(0.0, _safe_float(start_seconds)),
        }, timeout=self.load_timeout)

    def _autoplay_summary(self, video: Dict[str, Any],
                          result: Dict[str, Any]) -> str:
        state = result.get("state") or {}
        playback = str(state.get("playback_state") or "")
        title = _clean_text(video.get("title"), 300) or "the video"
        channel = _clean_text(video.get("channel"), 200)
        author = f" by {channel}" if channel else ""
        if state.get("autoplay_blocked"):
            return (f"Loaded {title}{author}, but the browser blocked "
                    "autoplay. Ask the user to press play in the YouTube "
                    "panel.")
        if playback in ("playing", "buffering"):
            return f"Playing {title}{author}."
        if playback in ("paused", "cued", "unstarted", "stopped"):
            return f"Loaded {title}{author}, paused and ready."
        if playback == "ended":
            return f"Loaded {title}{author}; it has already ended."
        return f"Loaded {title}{author}."

    def _play_candidates(self, candidates: List[Dict[str, Any]], *,
                         query: str, channel: str = "",
                         autoplay: bool) -> Dict[str, Any]:
        if not candidates:
            raise YouTubeError(
                "no_results",
                "YouTube returned no usable public videos for that request.")
        attempts: List[Dict[str, str]] = []
        result: Dict[str, Any] = {}
        video: Dict[str, Any] = {}
        limit = min(MAX_PLAY_ATTEMPTS, len(candidates))
        for index in range(limit):
            video = candidates[index]
            result = self.load_video(video, autoplay=autoplay)
            if result.get("ok"):
                with self._state_lock:
                    self._source_query = _clean_text(query, MAX_QUERY_CHARS)
                    self._source_key = f"{_norm(query)}\x00{_norm(channel)}"
                    self._selected_at = time.monotonic()
                    self._state["error"] = None
                    self._state["updated_at"] = _now_iso()
                    self._state["_updated_monotonic"] = time.monotonic()
                return _ok(
                    "search_and_play",
                    self._autoplay_summary(video, result),
                    video=_video_payload(video),
                    state=result.get("state") or self.public_state(),
                    reused=False,
                )
            error = result.get("error") or {
                "code": "load_failed",
                "message": "The selected video could not be loaded.",
            }
            attempts.append({
                "video_id": str(video.get("video_id") or ""),
                "title": _clean_text(video.get("title"), 300),
                "code": str(error.get("code") or "load_failed"),
                "message": str(error.get("message") or ""),
            })
            if error.get("code") not in _RETRYABLE_LOAD_ERRORS:
                break

        error = result.get("error") or {}
        message = str(error.get("message") or
                      "No candidate video could be embedded.")
        if len(attempts) > 1:
            message = (f"The first {len(attempts)} usable results could not "
                       f"be embedded. {message}")
        with self._state_lock:
            self._state["error"] = {
                "code": str(error.get("code") or "playback_failed"),
                "message": message,
            }
            self._state["playback_state"] = "error"
            self._state["updated_at"] = _now_iso()
            self._state["_updated_monotonic"] = time.monotonic()
        return _error(
            "playback_failed", message, "search_and_play",
            attempts=attempts, state=self.public_state())

    def search_and_play(self, query: str, *, channel: str = "",
                        autoplay: bool = True,
                        exclude_current: bool = False) -> Dict[str, Any]:
        query = _validate_query(query)
        if not self._clients_available():
            raise YouTubeError(
                "interface_unavailable",
                "The Jarvis interface is not connected, so the YouTube panel "
                "cannot be opened. Start or refresh Jarvis and try again.")
        source_key = f"{_norm(query)}\x00{_norm(channel)}"
        with self._state_lock:
            state = dict(self._state)
            selected_at = self._selected_at
            current_key = self._source_key
        if (state.get("panel_open") and state.get("video_id")
                and current_key == source_key
                and not state.get("error")
                and not exclude_current
                and (time.monotonic() - selected_at) < 1800.0):
            # An appropriate video is already selected for this exact request.
            # Do not spend another 100 quota units; resume it if it was paused.
            if autoplay and state.get("player_ready") and \
                    state.get("playback_state") in (
                        "paused", "cued", "stopped", "ended", "unstarted"):
                resumed = self.player_command("play")
                if resumed.get("success"):
                    return _ok(
                        "search_and_play",
                        f"Resumed {state.get('title') or 'the current video'}.",
                        video=_video_payload({
                            "video_id": state.get("video_id"),
                            "title": state.get("title"),
                            "channel": state.get("channel"),
                            "duration_seconds": state.get("duration"),
                        }),
                        state=resumed.get("state") or self.public_state(),
                        reused=True,
                    )
            return _ok(
                "search_and_play",
                self._status_text(state),
                video=_video_payload({
                    "video_id": state.get("video_id"),
                    "title": state.get("title"),
                    "channel": state.get("channel"),
                    "duration_seconds": state.get("duration"),
                }),
                state=self.public_state(),
                reused=True,
            )
        candidates = self.search(query, channel)
        skip_id = ""
        if state.get("video_id") and (exclude_current or state.get("error")):
            skip_id = str(state.get("video_id"))
        if skip_id:
            candidates = [item for item in candidates
                          if str(item.get("video_id") or "") != skip_id]
            if not candidates:
                raise YouTubeError(
                    "no_results",
                    "I could not find another usable video for that request. "
                    "Try a more specific topic.")
        return self._play_candidates(candidates, query=query, channel=channel,
                                     autoplay=autoplay)

    def play_specific(self, value: str, *, autoplay: bool = True) -> Dict[str, Any]:
        video_id = _extract_video_id(value)
        if not self._clients_available():
            raise YouTubeError(
                "interface_unavailable",
                "The Jarvis interface is not connected, so the YouTube panel "
                "cannot be opened. Start or refresh Jarvis and try again.")
        candidate, warnings = self._video_metadata(video_id)
        result = self.load_video(candidate, autoplay=autoplay)
        if not result.get("ok"):
            error = result.get("error") or {
                "code": "load_failed",
                "message": "The requested video could not be loaded.",
            }
            with self._state_lock:
                self._state["error"] = error
                self._state["playback_state"] = "error"
                self._state["updated_at"] = _now_iso()
                self._state["_updated_monotonic"] = time.monotonic()
            return _error(
                str(error.get("code") or "playback_failed"),
                str(error.get("message") or "The requested video could not be loaded."),
                "play",
                video=_video_payload(candidate),
                state=self.public_state())
        with self._state_lock:
            self._source_query = ""
            self._source_key = ""
            self._selected_at = time.monotonic()
        response = _ok(
            "play",
            self._autoplay_summary(candidate, result),
            video=_video_payload(candidate),
            state=result.get("state") or self.public_state(),
            reused=False,
        )
        if warnings:
            response["warnings"] = warnings
        return response

    # ── commands ─────────────────────────────────────────────────────────────

    def _require_player(self) -> None:
        with self._state_lock:
            state = dict(self._state)
        if not state.get("panel_open"):
            raise YouTubeError(
                "nothing_playing",
                "The YouTube panel is closed, so there is no video to control.")
        if not state.get("player_ready"):
            raise YouTubeError(
                "player_not_ready",
                "The YouTube player is still starting. Try again in a moment.")

    def player_command(self, command: str, args: Optional[Dict[str, Any]] = None,
                       *, timeout: Optional[float] = None) -> Dict[str, Any]:
        self._require_player()
        result = self._request("youtube_player_command", {
            "command": str(command or ""),
            "args": dict(args or {}),
        }, timeout=self.command_timeout if timeout is None else timeout)
        state = result.get("state") or self.public_state()
        if not result.get("ok"):
            error = result.get("error") or {
                "code": "player_error",
                "message": "The YouTube player did not complete that command.",
            }
            return _error(
                str(error.get("code") or "player_error"),
                str(error.get("message") or "The YouTube player failed."),
                "player_command",
                state=state)
        return _ok(command, _command_summary(command, state), state=state)

    # ── public operations ────────────────────────────────────────────────────

    def get_status(self, *, refresh: bool = True) -> Dict[str, Any]:
        warnings: List[str] = []
        public = self.public_state()
        with self._state_lock:
            updated_at = _safe_float(self._state.get("_updated_monotonic"))
        fresh_enough = time.monotonic() - updated_at < 2.5
        if (refresh and public.get("panel_open")
                and public.get("player_ready")
                and not fresh_enough and self._clients_available()):
            result = self._request(
                "youtube_player_command",
                {"command": "get_status", "args": {}},
                timeout=self.status_timeout)
            if result.get("ok"):
                public = dict(result.get("state") or self.public_state())
            else:
                error = result.get("error") or {}
                warnings.append(str(error.get("message")
                                     or "The player did not refresh its state."))
        if not public.get("panel_open"):
            return _ok(
                "get_status",
                "The YouTube panel is closed. Nothing is playing.",
                playing=False,
                state=public,
                warnings=warnings,
            )
        playback = str(public.get("playback_state") or "")
        return _ok(
            "get_status",
            self._status_text(public),
            playing=playback in ("playing", "buffering"),
            paused=playback == "paused",
            state=public,
            warnings=warnings,
        )

    def pause(self) -> Dict[str, Any]:
        return self.player_command("pause")

    def resume(self) -> Dict[str, Any]:
        return self.player_command("play")

    def toggle(self) -> Dict[str, Any]:
        return self.player_command("toggle_playback")

    def stop(self) -> Dict[str, Any]:
        return self.player_command("stop")

    def seek_forward(self, seconds: float) -> Dict[str, Any]:
        return self.player_command("seek_forward", {"seconds": seconds})

    def seek_backward(self, seconds: float) -> Dict[str, Any]:
        return self.player_command("seek_backward", {"seconds": seconds})

    def seek_to(self, seconds: float) -> Dict[str, Any]:
        return self.player_command("seek_to", {"seconds": seconds})

    def volume_up(self, step: int = 10) -> Dict[str, Any]:
        return self.player_command("volume_up", {"step": max(1, int(step))})

    def volume_down(self, step: int = 10) -> Dict[str, Any]:
        return self.player_command("volume_down", {"step": max(1, int(step))})

    def set_volume(self, volume: int) -> Dict[str, Any]:
        return self.player_command("set_volume", {"volume": int(volume)})

    def mute(self) -> Dict[str, Any]:
        return self.player_command("mute")

    def unmute(self) -> Dict[str, Any]:
        return self.player_command("unmute")

    def fullscreen(self) -> Dict[str, Any]:
        result = self.player_command("fullscreen")
        if not result.get("success") and result.get("error_code") == "fullscreen_blocked":
            result["result"] = (
                "The browser blocked programmatic fullscreen. The YouTube "
                "panel shows an accessible FULLSCREEN button for the user.")
        return result

    def exit_fullscreen(self) -> Dict[str, Any]:
        return self.player_command("exit_fullscreen")

    def close(self) -> Dict[str, Any]:
        with self._state_lock:
            state = dict(self._state)
        if not state.get("panel_open"):
            return _ok("close", "The YouTube panel is already closed.",
                       state=self.public_state())
        if not self._clients_available():
            with self._state_lock:
                self._state = _empty_state()
                self._source_query = ""
                self._source_key = ""
                self._selected_at = 0.0
            return _ok(
                "close",
                "The YouTube panel was not connected and has been closed.",
                state=self.public_state())
        result = self._request("youtube_close_panel", {}, timeout=self.command_timeout)
        if result.get("ok"):
            with self._state_lock:
                self._state = _empty_state()
                self._source_query = ""
                self._source_key = ""
                self._selected_at = 0.0
            return _ok("close", "Closed the YouTube panel and stopped playback.",
                       state=self.public_state())
        error = result.get("error") or {
            "code": "close_failed",
            "message": "The YouTube panel did not close.",
        }
        return _error(
            str(error.get("code") or "close_failed"),
            str(error.get("message") or "The YouTube panel did not close."),
            "close",
            state=self.public_state())


def _command_summary(command: str, state: Dict[str, Any]) -> str:
    playback = str(state.get("playback_state") or "")
    title = state.get("title") or "the video"
    at = _format_time(state.get("current_time"))
    volume = _safe_int(state.get("volume"), 0)
    muted = bool(state.get("muted"))
    if command in ("pause",):
        return f"Paused {title} at {at}."
    if command in ("play", "resume"):
        if state.get("autoplay_blocked"):
            return ("The browser blocked playback. Press play in the YouTube "
                    "panel to start it.")
        return f"Playing {title}."
    if command == "toggle_playback":
        return (f"Paused {title} at {at}." if playback == "paused"
                else f"Playing {title}.")
    if command == "stop":
        return f"Stopped {title}."
    if command in ("seek_forward", "seek_backward", "seek_to"):
        return f"{title} is now at {at}."
    if command in ("volume_up", "volume_down", "set_volume"):
        return f"Volume is {volume}%{' (muted)' if muted else ''}."
    if command == "mute":
        return "Muted the YouTube player."
    if command == "unmute":
        return "Unmuted the YouTube player."
    if command == "fullscreen":
        return "The YouTube player is fullscreen."
    if command == "exit_fullscreen":
        return "Exited YouTube fullscreen."
    return "The YouTube player completed the command."


# ─────────────────────────────────────────────────────────────────────────────
# Process-wide service
# ─────────────────────────────────────────────────────────────────────────────
_service_lock = threading.Lock()
_service: Optional[YouTubePlayerService] = None


def get_service() -> YouTubePlayerService:
    global _service
    with _service_lock:
        if _service is None:
            _service = YouTubePlayerService()
        return _service


def configure(*, publish: Optional[Callable[[Dict[str, Any]], None]] = None,
              has_clients: Optional[Callable[[], bool]] = None) -> None:
    """Connect the tool to the application's existing event hub."""
    get_service().configure(publish=publish, has_clients=has_clients)


def reset_service() -> None:
    """Replace the singleton (primarily for isolated automated tests)."""
    global _service
    with _service_lock:
        old = _service
        _service = None
    if old is not None:
        old.reset()


# ─────────────────────────────────────────────────────────────────────────────
# Tool entry point
# ─────────────────────────────────────────────────────────────────────────────
def youtube_player(
    operation: str,
    query: Optional[str] = None,
    channel: Optional[str] = None,
    video: Optional[str] = None,
    autoplay: bool = True,
    seconds: Optional[float] = None,
    volume: Optional[int] = None,
    exclude_current: bool = False,
) -> Dict[str, Any]:
    """Discover, open and control YouTube videos inside the Jarvis panel.

    ONE tool for the whole workflow. Use ``search_and_play`` for any request
    that describes what to watch: Jarvis searches YouTube, ranks the results,
    picks the best public, embeddable match, opens its own player panel and
    starts playback. Pass ``video`` only when the user supplied an exact video
    ID or YouTube URL. Set ``exclude_current`` when the user asks for another
    or a different video on the same topic. Use the playback, seeking, volume,
    fullscreen and close operations for follow-up commands; leave their
    parameters empty when the user did not specify a value.

    ``get_status`` answers questions about the current video, position,
    remaining time, volume, mute and fullscreen state. Never claim a video is
    playing, paused or closed unless this tool returns ``success: true``.
    """
    op = _clean_text(operation, 40).casefold()
    try:
        if not op:
            raise YouTubeError("missing_operation",
                               "A YouTube operation is required.")
        if op not in _OPERATIONS:
            raise YouTubeError(
                "invalid_operation",
                f"Unknown YouTube operation '{op}'. Valid operations: "
                + ", ".join(_OPERATIONS) + ".")
        service = get_service()
        autoplay_value = _coerce_bool(autoplay, "autoplay")
        exclude_value = (False if exclude_current is None
                         else _coerce_bool(exclude_current,
                                           "exclude_current", default=False))
        query_text = _clean_text(query, MAX_QUERY_CHARS + 1) if query else ""
        channel_text = _clean_text(channel, 120) if channel else ""
        video_text = _clean_text(video, 2000) if video else ""

        if op == "search_and_play":
            if video_text:
                return service.play_specific(video_text,
                                             autoplay=autoplay_value)
            if not query_text:
                raise YouTubeError(
                    "missing_query",
                    "Tell me what to search for, or provide a YouTube video "
                    "ID or URL.")
            return service.search_and_play(
                query_text, channel=channel_text, autoplay=autoplay_value,
                exclude_current=exclude_value)

        if op == "play":
            if video_text:
                return service.play_specific(video_text,
                                             autoplay=autoplay_value)
            if query_text:
                return service.search_and_play(
                    query_text, channel=channel_text, autoplay=autoplay_value,
                    exclude_current=exclude_value)
            return service.resume()

        if op == "get_status":
            return service.get_status()

        if op == "pause":
            return service.pause()
        if op == "resume":
            return service.resume()
        if op == "toggle_playback":
            return service.toggle()
        if op == "stop":
            return service.stop()

        if op in ("seek_forward", "seek_backward"):
            amount = _coerce_number(seconds, "seconds", 10.0)
            assert amount is not None
            if amount <= 0:
                raise YouTubeError("invalid_parameter",
                                   "seconds must be greater than zero.")
            if amount > 86400:
                raise YouTubeError("invalid_parameter",
                                   "seconds is too large.")
            return (service.seek_forward(amount) if op == "seek_forward"
                    else service.seek_backward(amount))
        if op == "seek_to":
            target = _coerce_number(seconds, "seconds")
            if target is None:
                raise YouTubeError(
                    "invalid_parameter",
                    "seek_to needs the target time in seconds.")
            if target < 0:
                raise YouTubeError("invalid_parameter",
                                   "The target time cannot be negative.")
            return service.seek_to(target)

        step = _coerce_volume(volume, "volume") if volume is not None else 10
        if op == "volume_up":
            return service.volume_up(step or 10)
        if op == "volume_down":
            return service.volume_down(step or 10)
        if op == "set_volume":
            target_volume = _coerce_volume(volume, "volume", required=True)
            assert target_volume is not None
            return service.set_volume(target_volume)

        if op == "mute":
            return service.mute()
        if op == "unmute":
            return service.unmute()
        if op == "fullscreen":
            return service.fullscreen()
        if op == "exit_fullscreen":
            return service.exit_fullscreen()
        if op == "close":
            return service.close()
        raise YouTubeError("invalid_operation",
                           f"Unknown YouTube operation '{op}'.")
    except YouTubeError as exc:
        return _error(exc.code, exc.message, op)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return _error(
            "youtube_internal_error",
            f"{type(exc).__name__}: {exc}",
            op)

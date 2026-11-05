"""Top-bar weather.

No API key is needed: Open-Meteo's public current-weather endpoint is used. The
call is made by Python, not the browser, so the page keeps a strict CSP and no
third-party origin is exposed.  Failures are reported as data, never as an
exception.

The location is the one the user configured (``config.get_weather_location``).
Empty, the default, keeps the fixed Tashkent feed.  A custom name is geocoded
once through Open-Meteo's geocoding endpoint and cached; an unknown name is
reported as unavailable data — never a crash.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import config

DEFAULT_LOCATION = "Tashkent"
LATITUDE = 41.2995
LONGITUDE = 69.2401
TIMEZONE = "Asia/Tashkent"
CACHE_SECONDS = 600
FAILURE_CACHE_SECONDS = 60
GEOCODE_SECONDS = 600


def _forecast_url(latitude: float, longitude: float, timezone_name: str) -> str:
    return (
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode({
            "latitude": round(latitude, 4),
            "longitude": round(longitude, 4),
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                       "weather_code,wind_speed_10m,is_day",
            "timezone": timezone_name,
            "wind_speed_unit": "kmh",
        })
    )


_URL = _forecast_url(LATITUDE, LONGITUDE, TIMEZONE)


def _unavailable(location: str, reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "location": location,
        "error": reason,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


class WeatherService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._at = 0.0
        self._label = ""
        self._latitude = LATITUDE
        self._longitude = LONGITUDE
        self._timezone = TIMEZONE
        self._geocode_at = 0.0

    def cached(self) -> dict[str, Any]:
        """The last result without ever fetching — for fast readiness checks."""
        with self._lock:
            if self._cache is not None:
                return dict(self._cache)
        return {"available": False,
                "location": self._label or self._configured_location()
                or "Tashkent"}

    # ── location resolution ────────────────────────────────────────────────

    def _configured_location(self) -> str:
        try:
            return config.get_weather_location()
        except Exception:
            return ""

    def _geocode(self, name: str) -> bool:
        """Resolve a free-text city into coordinates; True on success."""
        if time.monotonic() - self._geocode_at < GEOCODE_SECONDS and name == self._label:
            return True
        try:
            request = urllib.request.Request(
                "https://geocoding-api.open-meteo.com/v1/search?"
                + urllib.parse.urlencode({"name": name, "count": 1,
                                          "language": "en"}),
                headers={"User-Agent": "Jarvis-local-web/1.0"})
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            match = (payload.get("results") or [None])[0]
            if not isinstance(match, dict):
                return False
            self._latitude = float(match["latitude"])
            self._longitude = float(match["longitude"])
            self._timezone = str(match.get("timezone") or "auto")
            self._geocode_at = time.monotonic()
            return True
        except Exception:
            return False

    # ── current conditions ─────────────────────────────────────────────────

    def get(self, force: bool = False) -> dict[str, Any]:
        configured = self._configured_location()
        with self._lock:
            if configured != self._label:
                location_changed = True
            else:
                location_changed = False
            label = self._label or configured or DEFAULT_LOCATION
            if location_changed:
                self._cache = None
                if configured:
                    if self._geocode(configured):
                        self._label = configured
                        label = configured
                    else:
                        self._label = "unresolved"
                        result = _unavailable(
                            configured, "Location not found")
                        self._at = time.monotonic()
                        return dict(result)
                else:
                    self._label = ""
                    label = DEFAULT_LOCATION
                    self._latitude, self._longitude, self._timezone = (
                        LATITUDE, LONGITUDE, TIMEZONE)

            now = time.monotonic()
            if not force and self._cache is not None:
                ttl = CACHE_SECONDS if self._cache.get("available") else FAILURE_CACHE_SECONDS
                if now - self._at < ttl:
                    return dict(self._cache)

        url = _forecast_url(self._latitude, self._longitude, self._timezone)
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Jarvis-local-web/1.0"})
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            current = payload.get("current") or {}
            result = {
                "available": True,
                "location": self._label or DEFAULT_LOCATION,
                "timezone": self._timezone,
                "temperature_c": current.get("temperature_2m"),
                "apparent_c": current.get("apparent_temperature"),
                "humidity": current.get("relative_humidity_2m"),
                "wind_kmh": current.get("wind_speed_10m"),
                "weather_code": current.get("weather_code"),
                "is_day": bool(current.get("is_day", 1)),
                "observed_at": current.get("time"),
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        except Exception as exc:  # network, JSON or platform failure
            print(f"[Weather] unavailable: {type(exc).__name__}: {exc}")
            result = _unavailable(self._label or DEFAULT_LOCATION,
                                  "Weather service unavailable")

        with self._lock:
            self._cache = result
            self._at = time.monotonic()
        return dict(result)

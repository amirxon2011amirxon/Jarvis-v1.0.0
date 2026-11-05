"""Validated request structures for the local HTTP API.

Pydantic rejects unexpected fields, so the browser cannot smuggle new settings
or tool arguments through this boundary.  Only the settings that already exist
in ``config`` are accepted.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import AVAILABLE_VOICES

OSName = Literal["windows", "mac", "linux"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageIn(StrictModel):
    text: str = Field(min_length=1, max_length=8000)
    client_message_id: str | None = Field(default=None, max_length=64)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message is empty")
        return text

    @field_validator("client_message_id")
    @classmethod
    def _clean_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not all(ch.isalnum() or ch in "-_" for ch in cleaned):
            raise ValueError("invalid client_message_id")
        return cleaned


class MuteIn(StrictModel):
    muted: bool


class PttHoldIn(StrictModel):
    held: bool


class TurnTuningPatch(StrictModel):
    enabled: bool | None = None
    silence_ms: int | None = Field(default=None, ge=200, le=3000)
    prefix_ms: int | None = Field(default=None, ge=0, le=1000)
    end_sensitivity: Literal["low", "default", "high"] | None = None
    start_sensitivity: Literal["low", "default", "high"] | None = None


class SettingsPatch(StrictModel):
    assistant_name: str | None = Field(default=None, min_length=1, max_length=24)
    user_name: str | None = Field(default=None, max_length=32)
    voice_name: str | None = None
    ui_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    push_to_talk_enabled: bool | None = None
    proactive_audio_enabled: bool | None = None
    thinking_enabled: bool | None = None
    turn_tuning: TurnTuningPatch | None = None
    input_device: str | None = Field(default=None, max_length=200)
    output_device: str | None = Field(default=None, max_length=200)
    weather_location: str | None = Field(default=None, max_length=80)

    @field_validator("assistant_name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("assistant_name cannot be blank")
        return cleaned

    @field_validator("user_name", "input_device", "output_device")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("voice_name")
    @classmethod
    def _known_voice(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in AVAILABLE_VOICES:
            raise ValueError("unknown voice")
        return value


class ApiKeyIn(StrictModel):
    api_key: str = Field(min_length=1, max_length=4096)
    os_system: OSName | None = None

    @field_validator("api_key")
    @classmethod
    def _strip_key(cls, value: str) -> str:
        key = value.strip()
        if not key:
            raise ValueError("api key is empty")
        return key


class AudioTestIn(StrictModel):
    kind: Literal["input", "output"]
    device: str = Field(default="", max_length=200)


class VolumeIn(StrictModel):
    volume: int = Field(ge=0, le=100)
    device: str = Field(default="", max_length=200)


class SnoozeNotificationIn(StrictModel):
    minutes: int = Field(default=10, ge=1, le=1440)


class ScanResult(StrictModel):
    module_id: str
    status: str
    count: int
    message: str


class ToolSelectionIn(StrictModel):
    """The complete set of tool modules that should remain enabled.

    Entries are canonical ids (``web/open_web``), category folder names
    (``web``) or unique bare tool names (``open_web``). The backend resolves a
    category to its tools, so the browser never maintains a second mapping.
    """
    enabled: list[str] = Field(max_length=256)

    @field_validator("enabled")
    @classmethod
    def _clean_selectors(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            selector = item.strip()
            if not selector:
                continue
            if len(selector) > 120:
                raise ValueError("tool selector is too long")
            cleaned.append(selector)
        return cleaned


class SkillEnabledIn(StrictModel):
    enabled: bool


class SkillUninstallIn(StrictModel):
    confirm: bool = False


# ── Long-term memory ─────────────────────────────────────────────────────────
MemoryType = Literal["user", "episodic", "knowledge", "project"]
MemoryStatus = Literal["active", "superseded", "all"]


class MemoryUpdateIn(StrictModel):
    content: str | None = Field(default=None, max_length=8000)
    type: MemoryType | None = None
    title: str | None = Field(default=None, max_length=200)
    tags: list[str] | None = Field(default=None, max_length=16)
    project_id: str | None = Field(default=None, max_length=120)
    confidence: float | None = Field(default=None, ge=0, le=1)
    importance: float | None = Field(default=None, ge=0, le=1)
    expected_version: int | None = Field(default=None, ge=1)
    supersede: bool | None = None

    @field_validator("content", "title", "project_id")
    @classmethod
    def _strip_memory_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("tags")
    @classmethod
    def _clean_memory_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [tag.strip()[:40] for tag in value if tag.strip()]
        return cleaned


class MemoryForgetIn(StrictModel):
    memory_id: str | None = Field(default=None, max_length=64,
                                  pattern=r"^[A-Za-z0-9_-]+$")
    query: str | None = Field(default=None, max_length=500)
    project_id: str | None = Field(default=None, max_length=120)
    type: MemoryType | None = None
    confirm: bool = False


class MemoryImportIn(StrictModel):
    data: dict | None = None
    content: str | None = Field(default=None, max_length=900_000)
    dry_run: bool = False


class MemorySettingsIn(StrictModel):
    enabled: bool | None = None
    auto_extract: bool | None = None
    retrieval_enabled: bool | None = None
    voice_injection: bool | None = None
    max_retrieved: int | None = Field(default=None, ge=1, le=20)
    max_context_chars: int | None = Field(default=None, ge=200, le=8000)
    search_limit: int | None = Field(default=None, ge=5, le=200)
    backup_retention: int | None = Field(default=None, ge=1, le=50)
    expiry_days: int | None = Field(default=None, ge=0, le=3650)
    db_path: str | None = Field(default=None, max_length=500)
    backup_dir: str | None = Field(default=None, max_length=500)

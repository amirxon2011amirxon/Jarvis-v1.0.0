"""Settings for Jarvis v1.0.0.

Everything the app remembers between launches lives in one file —
``config/api_keys.json`` — and this module is the only reader and writer of it.
The API key, identity, voice, audio devices and conversation tuning all go
through here, so there is exactly one place to back up, one place already
git-ignored, and one place a user already knows to look.

Secrets that must never enter that file — currently the SIP password — are
read from the project-root ``.env`` (also git-ignored) or the process
environment. Existing environment variables always win over ``.env``.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
# A source checkout keeps its existing config/ layout. A pip-installed copy is
# identified by the absence of the frontend sources and stores mutable data in
# the user's data directory instead of site-packages.
_IS_SOURCE_TREE = (BASE_DIR / "frontend" / "src").is_dir()
IS_SOURCE_TREE = _IS_SOURCE_TREE


def _default_data_dir() -> Path:
    override = (os.environ.get("JARVIS_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    if _IS_SOURCE_TREE:
        return BASE_DIR
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "Jarvis"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Jarvis"
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "jarvis"
    return Path.home() / ".jarvis"


DATA_DIR = _default_data_dir()
IS_INSTALLED = not _IS_SOURCE_TREE


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Interpret JSON booleans and hand-edited string booleans safely."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in ("1", "true", "yes", "on", "enabled"):
            return True
        if text in ("0", "false", "no", "off", "disabled", ""):
            return False
    return default


def _load_env_file(path: Path) -> int:
    """Load a simple ``.env`` file without a third-party dependency.

    Existing process environment variables always win, so a shell export or
    deployment secret overrides the file.  Values are never printed.  The
    parser supports blank lines, ``export``, ``#`` comments, quoted values and
    inline comments after whitespace; it deliberately does not interpolate
    other variables, because a SIP password may contain ``$``.
    """
    try:
        if not path.is_file():
            return 0
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return 0
    loaded = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not (key[0].isalpha() or key[0] == "_") or not all(
                char.isalnum() or char in "_-" for char in key):
            continue
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in ("'", '"') and len(value) >= 2:
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = (value.replace("\\n", "\n").replace("\\r", "\r")
                         .replace("\\t", "\t").replace('\\"', '"')
                         .replace("\\\\", "\\"))
        else:
            # An unquoted value ends at whitespace followed by #.
            comment = value.find(" #")
            if comment >= 0:
                value = value[:comment].rstrip()
        os.environ.setdefault(key, value)
        loaded += 1
    return loaded


def _load_project_env() -> int:
    """Load the project-root ``.env`` once, at import.

    ``JARVIS_ENV_FILE`` can point at an isolated file for deployments/tests.
    A missing file is normal and silent; a missing SIP password is reported by
    the telephony layer with an actionable message, never by dumping the file.
    """
    override = (os.environ.get("JARVIS_ENV_FILE") or "").strip()
    path = Path(override) if override else (DATA_DIR / ".env")
    return _load_env_file(path)


_load_project_env()

CONFIG_DIR = DATA_DIR / "config"
# An install can move the whole settings file (tests use this isolation, the
# convention ``JARVIS_MEMORY_DB`` already set for the memory store).
CONFIG_FILE = Path(os.getenv("JARVIS_CONFIG_FILE") or
                   (CONFIG_DIR / "api_keys.json"))
_SHAPE_WARNED = False


def _config_path() -> Path:
    """The settings file — resolved per call so ``JARVIS_CONFIG_FILE`` can
    isolate tests even after this module is imported (the memory store
    follows the same late-binding convention)."""
    override = (os.getenv("JARVIS_CONFIG_FILE") or "").strip()
    if override:
        return Path(override)
    return CONFIG_FILE


def ensure_config_dir() -> None:
    _config_path().parent.mkdir(parents=True, exist_ok=True)


def config_exists() -> bool:
    return _config_path().exists()


def load_api_keys() -> dict:
    """Read the settings object, or an empty mapping when it is unreadable.

    The file is user-editable, so valid JSON of the wrong shape (a list, a
    string, ``null``) must degrade exactly like invalid JSON.  Returning those
    values would make every caller fail on ``.get`` — including startup.
    """
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[Config] Failed to load api_keys.json: {e}")
        return {}
    if not isinstance(data, dict):
        global _SHAPE_WARNED
        if not _SHAPE_WARNED:
            print("[Config] api_keys.json does not contain a JSON object; "
                  "using defaults.")
            _SHAPE_WARNED = True
        return {}
    return data


# Every read-modify-write of the settings file takes this lock: without it,
# two subsystems patching the file at the same time lose one of the writes
# (the exact bug that once made MCP server records vanish between saves).
_CONFIG_LOCK = threading.RLock()


def _replace_with_retry(temp_name: str, path: Path) -> None:
    """os.replace with a short Windows sharing-violation retry.

    Antivirus/indexers and a concurrent reader can hold the destination open
    for a few milliseconds; the old code surfaced that as a failed settings
    save even though the temp file was valid.
    """
    for attempt in range(10):
        try:
            os.replace(temp_name, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05)


def _patch_config(**fields) -> None:
    """Read-modify-write one or more keys without disturbing the rest.

    The new object is written to a sibling temp file and moved into place, so
    a crash or a concurrent reader never sees a half-written settings file
    (which would load as ``{}`` and look like a lost key). The module-level
    lock serializes concurrent writers across subsystems.
    """
    with _CONFIG_LOCK:
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    data = existing
            except Exception:
                data = {}
        data.update(fields)
        # The temp sibling must live on the target's own drive (os.replace does
        # not cross drives); isolation via JARVIS_CONFIG_FILE is remote-resilient.
        fd, temp_name = tempfile.mkstemp(
            prefix=".api_keys-", suffix=".tmp",
            dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=4)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


def _remove_config_keys(*keys: str) -> None:
    """Drop whole keys from the settings file (the migration cleanup path).

    Deliberately explicit: settings are only ever removed key-by-key, never
    by rewriting the document from scratch.
    """
    with _CONFIG_LOCK:
        path = _config_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        changed = False
        for key in keys:
            if key in data:
                data.pop(key, None)
                changed = True
        if not changed:
            return
        fd, temp_name = tempfile.mkstemp(
            prefix=".api_keys-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=4)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


def _text_setting(key: str, default: str = "") -> str:
    """A textual setting, or ``default`` for a missing/wrong-typed value.

    Settings are hand-edited often enough that a number where a string is
    expected must not turn into an ``AttributeError`` deep inside a caller.
    """
    value = load_api_keys().get(key)
    if isinstance(value, str):
        return value.strip()
    return default


# ── API key ──────────────────────────────────────────────────────────────────
def save_api_keys(gemini_api_key: str) -> None:
    _patch_config(gemini_api_key=gemini_api_key.strip())


def get_gemini_key() -> str | None:
    """The stored key, or None when it is absent or not a string."""
    return _text_setting("gemini_api_key") or None


def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


# ── Identity ─────────────────────────────────────────────────────────────────
def get_assistant_name() -> str:
    return _text_setting("assistant_name") or "Jarvis"


def get_user_name() -> str:
    return _text_setting("user_name")


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    _patch_config(
        assistant_name=assistant_name.strip() or "Jarvis",
        user_name=user_name.strip(),
    )


# ── Assistant voice (Gemini Live prebuilt voices) ────────────────────────────
AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE = "Charon"


def get_voice() -> str:
    v = _text_setting("voice_name") or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def save_voice(voice_name: str) -> None:
    v = (voice_name or "").strip()
    _patch_config(voice_name=v if v in AVAILABLE_VOICES else DEFAULT_VOICE)


# ── Voice input switches ─────────────────────────────────────────────────────
def get_push_to_talk_enabled() -> bool:
    """Hold-a-key-to-speak. When on, the mic is closed unless the chord is held."""
    return _coerce_bool(load_api_keys().get("push_to_talk_enabled", False))


def save_push_to_talk_enabled(enabled: bool) -> None:
    _patch_config(push_to_talk_enabled=bool(enabled))


# ── HUD appearance ───────────────────────────────────────────────────────────
DEFAULT_UI_COLOR = "#d8a63a"


def get_ui_color() -> str:
    value = _text_setting("ui_color").lower()
    if value.startswith("#") and len(value) == 7:
        try:
            int(value[1:], 16)
            return value
        except ValueError:
            pass
    return DEFAULT_UI_COLOR


def save_ui_color(color: str) -> None:
    value = (color or "").strip().lower()
    _patch_config(ui_color=value if value.startswith("#") and len(value) == 7
                  else DEFAULT_UI_COLOR)


# ── Weather location ─────────────────────────────────────────────────────────
def get_weather_location() -> str:
    """Optional free-text city for the top-bar weather card.

    Empty (the default) keeps the fixed Tashkent feed the web HUD shipped
    with; a non-empty name is geocoded once and cached by ``web.weather``.
    """
    value = _text_setting("weather_location")[:80]
    return value


def save_weather_location(name: str) -> None:
    _patch_config(weather_location=(name or "").strip()[:80])


# ── Tool selection ───────────────────────────────────────────────────────────
# Existing settings files store the exact enabled tool-module ids. When a new
# built-in module ships, a selection written before that release cannot contain
# it. This version marker runs a one-time migration so a capability the user has
# never had a chance to disable is not silently absent from the model's tools.
TOOL_SELECTION_VERSION = 5
# Tools introduced in each version. Only tools newer than the saved selection
# are added, so a later migration cannot quietly re-enable a capability the
# user deliberately disabled in an earlier build.
_SELECTION_INTRODUCED = {
    3: ("research", "phone_call"),
    4: ("screenshot",),
    5: ("zip",),
}


def get_enabled_tool_ids() -> list[str] | None:
    """The tool modules the model may call, or ``None`` for "all of them".

    ``None`` is the default and also the fallback for a malformed value: a
    hand-edited settings file must never silently disable every tool. An empty
    list is meaningful and is preserved — the user can switch all tools off.
    Entries may be canonical ids (``web/open_web``) or historical bare names
    (``open_web``); ``actions.tool_calling`` resolves both.
    """
    data = load_api_keys()
    if "enabled_tools" not in data:
        return None
    raw = data.get("enabled_tools")
    if not isinstance(raw, list):
        return None
    cleaned = [item.strip() for item in raw
               if isinstance(item, str) and item.strip()]

    # Historical selection files briefly used the action name for the ZIP
    # module. Normalise it to the module id so the tool is not silently
    # disabled after upgrading.
    alias_changed = False
    if "zip_tool" in cleaned:
        cleaned = ["zip" if item == "zip_tool" else item for item in cleaned]
        cleaned = list(dict.fromkeys(cleaned))
        alias_changed = True

    try:
        version = int(data.get("enabled_tools_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if alias_changed:
        try:
            _patch_config(
                enabled_tools=sorted(set(cleaned)),
                enabled_tools_version=max(version, TOOL_SELECTION_VERSION),
            )
        except Exception:
            pass
    if version < TOOL_SELECTION_VERSION and cleaned:
        changed = False
        for introduced, tool_ids in _SELECTION_INTRODUCED.items():
            if version >= introduced:
                continue
            for tool_id in tool_ids:
                if tool_id not in cleaned:
                    cleaned.append(tool_id)
                    changed = True
        if changed or "enabled_tools_version" not in data:
            try:
                _patch_config(
                    enabled_tools=sorted(set(cleaned)),
                    enabled_tools_version=TOOL_SELECTION_VERSION,
                )
            except Exception:
                # A read must still return a usable selection even if the
                # settings file is read-only.
                pass
    return cleaned


def save_enabled_tool_ids(tool_ids: list[str]) -> None:
    """Persist the enabled tool module ids (an empty list disables all)."""
    cleaned = sorted({str(item).strip() for item in (tool_ids or [])
                      if str(item).strip()})
    _patch_config(
        enabled_tools=cleaned,
        enabled_tools_version=TOOL_SELECTION_VERSION,
    )


# ── Live-session tuning ──────────────────────────────────────────────────────
def get_thinking_enabled() -> bool:
    return _coerce_bool(load_api_keys().get("thinking_enabled", False))


def save_thinking_enabled(enabled: bool) -> None:
    _patch_config(thinking_enabled=bool(enabled))


def get_turn_tuning() -> dict:
    """How eagerly the server decides you have stopped speaking."""
    cfg = load_api_keys().get("turn_tuning")
    cfg = cfg if isinstance(cfg, dict) else {}

    def _int(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(cfg.get("enabled", False)),
        "silence_ms": _int("silence_ms", 550, 200, 3000),
        "prefix_ms": _int("prefix_ms", 150, 0, 1000),
        "end_sensitivity": str(cfg.get("end_sensitivity", "high")).lower(),
        "start_sensitivity": str(cfg.get("start_sensitivity", "default")).lower(),
    }


def save_turn_tuning(values: dict) -> None:
    cur = load_api_keys().get("turn_tuning")
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(values or {})
    _patch_config(turn_tuning=cur)


def get_proactive_audio_enabled() -> bool:
    """Whether the model may decide an utterance was not aimed at it."""
    return _coerce_bool(load_api_keys().get("proactive_audio", True), True)


def save_proactive_audio_enabled(enabled: bool) -> None:
    _patch_config(proactive_audio=bool(enabled))


# ── Audio devices ────────────────────────────────────────────────────────────
# Stored as device NAMES, not sounddevice indices: indices shift whenever a USB
# device is plugged in, so a saved index silently points at different hardware.
def get_input_device() -> str:
    return _text_setting("input_device")


def save_input_device(name: str) -> None:
    _patch_config(input_device=(name or "").strip())


def get_output_device() -> str:
    return _text_setting("output_device")


def save_output_device(name: str) -> None:
    _patch_config(output_device=(name or "").strip())


# ── YouTube ──────────────────────────────────────────────────────────────────
# The Data API key is optional for direct-ID playback but required for search.
# The environment override wins so a portable install or a test run can supply
# one without editing the shared settings file.
def get_youtube_api_key() -> str:
    env = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
    return env or _text_setting("youtube_api_key")


def save_youtube_api_key(api_key: str) -> None:
    _patch_config(youtube_api_key=(api_key or "").strip())


def get_youtube_region_code() -> str:
    """Optional ISO 3166-1 alpha-2 code used for region availability checks."""
    env = (os.environ.get("YOUTUBE_REGION_CODE") or "").strip().upper()
    value = env or _text_setting("youtube_region_code").upper()
    return value if len(value) == 2 and value.isalpha() else ""


# ── Browser ──────────────────────────────────────────────────────────────────
def get_brave_path() -> str:
    """Optional explicit Brave executable; empty means auto-detect."""
    return _text_setting("brave_path")


# ── Telephony (SIP/VoIP) ─────────────────────────────────────────────────────
# Credentials live in the environment, never in api_keys.json.  The username
# and server have practical defaults; the password is required to register.
def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def get_telephony_settings() -> dict:
    """Validated SIP/RTP configuration.

    ``JARVIS_SIP_PASSWORD`` is read here but is never written to disk, never
    printed and never returned by the status action.  The settings file may
    hold non-secret options under ``telephony``; an environment variable wins
    for deployment overrides.
    """
    data = load_api_keys()
    raw = data.get("telephony")
    raw = raw if isinstance(raw, dict) else {}

    username = (
        os.environ.get("JARVIS_SIP_USERNAME")
        or os.environ.get("SIP_USERNAME")
        or raw.get("username")
        or "jarvis2011"
    ).strip()
    password = (
        os.environ.get("JARVIS_SIP_PASSWORD")
        or os.environ.get("SIP_PASSWORD")
        or ""
    )
    server = (
        os.environ.get("JARVIS_SIP_SERVER")
        or os.environ.get("SIP_DOMAIN")
        or raw.get("server")
        or "sip.linphone.org"
    ).strip()
    try:
        port = int(os.environ.get("JARVIS_SIP_PORT")
                   or os.environ.get("SIP_PORT")
                   or raw.get("port") or 5061)
    except (TypeError, ValueError):
        port = 5061
    port = max(1, min(65535, port))
    identity = (
        os.environ.get("JARVIS_SIP_IDENTITY")
        or os.environ.get("SIP_IDENTITY")
        or raw.get("identity")
        or f"sip:{username}@{server}"
    ).strip()
    transport = (
        os.environ.get("JARVIS_SIP_TRANSPORT")
        or os.environ.get("SIP_TRANSPORT")
        or raw.get("transport")
        or "TLS"
    ).strip().upper()
    display_name = (
        os.environ.get("JARVIS_SIP_DISPLAY_NAME")
        or os.environ.get("SIP_DISPLAY_NAME")
        or raw.get("display_name")
        or get_assistant_name()
    ).strip()

    try:
        ring_timeout = int(os.environ.get("JARVIS_SIP_RING_TIMEOUT")
                           or raw.get("ring_timeout") or 60)
    except (TypeError, ValueError):
        ring_timeout = 60
    ring_timeout = max(10, min(300, ring_timeout))
    try:
        expires = int(os.environ.get("JARVIS_SIP_REGISTER_EXPIRES")
                      or raw.get("register_expires") or 300)
    except (TypeError, ValueError):
        expires = 300
    expires = max(60, min(3600, expires))

    auto_answer = _env_bool(
        "JARVIS_SIP_AUTO_ANSWER",
        _env_bool(
            "SIP_AUTO_ANSWER", bool(raw.get("auto_answer", False))))
    # Linphone's service answers with RTP/SAVPF and SDES crypto. SRTP is on by
    # default; disabling it makes real Linphone clients reject the offer 488.
    srtp = _env_bool("JARVIS_SIP_SRTP", bool(raw.get("srtp", True)))
    tls_verify = not _env_bool(
        "JARVIS_SIP_TLS_INSECURE", not bool(raw.get("tls_verify", True)))
    stun_server = (
        os.environ.get("JARVIS_SIP_STUN")
        or raw.get("stun_server")
        or "stun.l.google.com:19302"
    ).strip()
    if stun_server.lower() in ("", "0", "off", "false", "none"):
        stun_server = ""

    history_path = Path(
        os.environ.get("JARVIS_TELEPHONY_HISTORY")
        or raw.get("history_path")
        or (CONFIG_DIR / "telephony_history.json"))
    contacts_path = Path(
        os.environ.get("JARVIS_CONTACTS_FILE")
        or raw.get("contacts_path")
        or (CONFIG_DIR / "contacts.json"))

    return {
        "username": username,
        "password": password,
        "server": server,
        "port": port,
        "identity": identity,
        "transport": transport,
        "display_name": display_name,
        "auto_answer": auto_answer,
        "srtp": srtp,
        "ring_timeout": ring_timeout,
        "register_expires": expires,
        "stun_server": stun_server,
        "tls_verify": tls_verify,
        "history_path": history_path,
        "contacts_path": contacts_path,
        "contacts": {},
    }


def validate_telephony_settings(
    settings: dict, *, require_password: bool = True,
) -> list[str]:
    """Return human-readable configuration problems, never secret values."""
    problems: list[str] = []
    username = str(settings.get("username") or "").strip()
    password = str(settings.get("password") or "")
    server = str(settings.get("server") or "").strip()
    identity = str(settings.get("identity") or "").strip()
    transport = str(settings.get("transport") or "").strip().upper()
    try:
        port = int(settings.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not username:
        problems.append("SIP username is empty")
    if require_password and not password:
        problems.append(
            "SIP password is missing - set SIP_PASSWORD in .env "
            "(or JARVIS_SIP_PASSWORD in the environment)")
    if not server or any(char.isspace() for char in server) or "/" in server:
        problems.append("SIP domain/server is invalid")
    if not (1 <= port <= 65535):
        problems.append("SIP port is invalid")
    if transport not in ("TLS", "SIPS"):
        problems.append("SIP transport must be TLS")
    if not identity.lower().startswith(("sip:", "sips:")) or "@" not in identity:
        problems.append("SIP identity must be a sip:user@domain address")
    return problems


def get_call_allowlist() -> list[str]:
    """Trusted targets that may be dialled without a UI approval.

    Configure with ``JARVIS_CALL_ALLOWLIST`` (comma/semicolon separated) or a
    ``telephony.allowlist`` list/string in ``config/api_keys.json``.  Entries
    may be contact names, SIP addresses or phone numbers.  This list is read
    by the backend only; the model cannot add to it.
    """
    raw = os.environ.get("JARVIS_CALL_ALLOWLIST", "")
    items: list[str] = []
    if raw.strip():
        items = [part.strip() for part in raw.replace(";", ",").split(",")]
    else:
        data = load_api_keys().get("telephony")
        if isinstance(data, dict):
            value = data.get("allowlist")
            if isinstance(value, list):
                items = [str(item).strip() for item in value]
            elif isinstance(value, str):
                items = [part.strip()
                         for part in value.replace(";", ",").split(",")]
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items:
        value = str(item or "").strip()[:200]
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            cleaned.append(value)
    return cleaned[:100]


def save_telephony_settings(patch: dict) -> dict:
    """Persist non-secret telephony options in the existing settings file.

    Passwords are deliberately rejected: use JARVIS_SIP_PASSWORD instead.
    """
    current = load_api_keys().get("telephony")
    merged = dict(current) if isinstance(current, dict) else {}
    # An older build may have written a plaintext password here. It is no
    # longer a supported setting, and saving must remove it rather than
    # round-tripping the secret forever.
    for legacy_key in [key for key in merged
                       if "password" in str(key).lower()]:
        merged.pop(legacy_key, None)
    allowed = {
        "username", "server", "port", "identity", "transport",
        "display_name", "auto_answer", "srtp", "ring_timeout",
        "register_expires", "stun_server", "tls_verify", "history_path",
        "contacts_path", "allowlist",
    }
    for key, value in (patch or {}).items():
        if key not in allowed:
            if "password" in str(key).lower():
                raise ValueError("SIP passwords belong in JARVIS_SIP_PASSWORD")
            raise ValueError(f"unknown telephony setting {key!r}")
        if key in ("auto_answer", "srtp", "tls_verify"):
            merged[key] = _coerce_bool(value, False)
        elif key in ("port", "ring_timeout", "register_expires"):
            merged[key] = int(value)
        elif key == "allowlist":
            if isinstance(value, str):
                merged[key] = [
                    part.strip()
                    for part in value.replace(";", ",").split(",")
                    if part.strip()
                ]
            elif isinstance(value, list):
                merged[key] = [str(item).strip() for item in value
                               if str(item).strip()]
            elif value is None:
                merged.pop(key, None)
            else:
                raise ValueError("allowlist must be a list or comma-separated string")
        elif value is None:
            merged.pop(key, None)
        else:
            merged[key] = str(value).strip()
    _patch_config(telephony=merged)
    return get_telephony_settings()


# ── Long-term memory ─────────────────────────────────────────────────────────
# The database lives in the existing application-data directory (``config/``),
# never in the Python installation, the package directory or a temp folder.
MEMORY_DEFAULT_DB = CONFIG_DIR / "memory.db"
MEMORY_DEFAULT_BACKUPS = CONFIG_DIR / "memory_backups"

# Environment overrides keep automated tests (and portable installs) away from
# the real database without a second configuration mechanism.
_ENV_MEMORY_DB = os.environ.get("JARVIS_MEMORY_DB", "").strip()
_ENV_MEMORY_BACKUPS = os.environ.get("JARVIS_MEMORY_BACKUPS", "").strip()
if _ENV_MEMORY_DB:
    MEMORY_DEFAULT_DB = Path(_ENV_MEMORY_DB).expanduser()
if _ENV_MEMORY_BACKUPS:
    MEMORY_DEFAULT_BACKUPS = Path(_ENV_MEMORY_BACKUPS).expanduser()


def _memory_number(raw: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw.get(key, default))))
    except (TypeError, ValueError):
        return default


def _memory_path(raw: dict, key: str, default: Path) -> Path:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        return default
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def get_memory_settings() -> dict:
    """Validated memory configuration with practical defaults.

    A hand-edited or malformed nested object degrades to defaults rather than
    disabling memory silently.
    """
    data = load_api_keys()
    raw = data.get("memory")
    raw = raw if isinstance(raw, dict) else {}
    return {
        "enabled": _coerce_bool(raw.get("enabled", True), True),
        "auto_extract": _coerce_bool(raw.get("auto_extract", True), True),
        "retrieval_enabled": _coerce_bool(raw.get("retrieval_enabled", True), True),
        "voice_injection": _coerce_bool(raw.get("voice_injection", True), True),
        "max_retrieved": _memory_number(raw, "max_retrieved", 6, 1, 20),
        "max_context_chars": _memory_number(raw, "max_context_chars", 1400, 200, 8000),
        "search_limit": _memory_number(raw, "search_limit", 40, 5, 200),
        "backup_retention": _memory_number(raw, "backup_retention", 5, 1, 50),
        "expiry_days": _memory_number(raw, "expiry_days", 0, 0, 3650),
        "db_path": _memory_path(raw, "db_path", MEMORY_DEFAULT_DB),
        "backup_dir": _memory_path(raw, "backup_dir", MEMORY_DEFAULT_BACKUPS),
    }


def save_memory_settings(patch: dict) -> dict:
    """Merge one validated memory-settings patch into the single settings file."""
    current = load_api_keys().get("memory")
    merged = dict(current) if isinstance(current, dict) else {}
    for key, value in (patch or {}).items():
        if value is None:
            continue
        if key in ("enabled", "auto_extract", "retrieval_enabled",
                   "voice_injection"):
            merged[key] = bool(value)
        elif key in ("max_retrieved", "max_context_chars", "search_limit",
                     "backup_retention", "expiry_days"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a number")
            merged[key] = int(value)
        elif key in ("db_path", "backup_dir"):
            text = str(value).strip()
            if text:
                merged[key] = text
            else:
                merged.pop(key, None)
        else:
            raise ValueError(f"unknown memory setting {key!r}")
    _patch_config(memory=merged)
    return get_memory_settings()


# ── OS (kept for compatibility with the first-run screen) ────────────────────
def get_config() -> dict:
    return load_api_keys()


def _platform_os() -> str:
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )


def get_os() -> str:
    return (_text_setting("os_system") or _platform_os()).lower()


def is_windows() -> bool:
    return get_os() == "windows"


def is_mac() -> bool:
    return get_os() == "mac"


def is_linux() -> bool:
    return get_os() == "linux"

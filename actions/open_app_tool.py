# actions/computer_control/open_app_tool.py
"""Discover installed applications and open one by its exact name.

Design
------
There are two tools, not one clever matcher:

``list_apps(query="")``
    Return the names of applications the user can actually open. Discovery
    covers Desktop and Start-Menu shortcuts (including nested program folders)
    plus installed Microsoft Store / UWP apps. System binaries, PATH helpers
    and command-line utilities are deliberately *not* included.

``open_app(app_name)``
    Open a name returned by ``list_apps``. Matching is exact (case, spacing and
    punctuation insensitive). If the name is not an installed app, nothing is
    opened and the closest installed names are returned instead.

Why it works this way
---------------------
The assistant — not this module — chooses which app the user meant. Previous
versions guessed with aliases and fuzzy matching, which opened the wrong thing
("paint" -> ``print.exe``, "edge" -> ``edge-playback.exe``) and hard-coded
applications the user did not have. Discovery is now limited to normal
openable apps, and only an exact choice is launched.

Public API
----------
``list_apps(query="", offset=0, limit=0)``
    -> {"query", "count", "apps", "total", "offset", "limit",
        "has_more", "next_offset"}
``open_app(app_name)``        -> {"opened", "via"} | {"error"}
``get_installed_programs()``  -> detailed catalogue entries
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

_IS_WIN = sys.platform.startswith("win")

# Seconds a cached catalogue stays valid. Discovery runs at most once every few
# minutes; looking up a name afterwards is a dict lookup.
_INDEX_TTL = 300.0
_FAILURE_TTL = 30.0

# Folders that contain maintenance shortcuts, not applications.
_EXCLUDE_PATH_KEYWORDS = (
    "administrative tools",
    "system tools",
    "windows powershell",
    "windows powershell ise",
    "microsoft office tools",
    "\\startup",
)

_EXCLUDE_NAME_KEYWORDS = (
    "uninstall",
    "uninst",
    "деинсталл",
    "удалить",
    "remove",
    "delete",
    "readme",
    "setup",
    "release notes",
    "frequently asked",
    "faq",
    "documentation",
    "manual",
    "check for updates",
    "website",
    "homepage",
    "license",
)

# Words users put around an app name that are not part of the name.
_FILLER_WORDS = frozenset({
    "open", "launch", "start", "run", "please", "the", "app", "application",
    "program", "for", "me", "can", "could", "you", "a", "an", "my", "up",
})


# ─────────────────────────────────────────────────────────────────────────────
#  Catalogue
# ─────────────────────────────────────────────────────────────────────────────
_INDEX: List[Dict[str, str]] = []
_INDEX_AT = 0.0
_INDEX_LOCK = threading.Lock()
_PREFETCH_THREAD: threading.Thread | None = None


def _norm(text: str) -> str:
    """Case/punctuation-insensitive form used for exact matching."""
    s = (text or "").lower().strip()
    s = re.sub(r"\.(exe|lnk|msc|bat|cmd)$", "", s)
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(text: str) -> List[str]:
    return [t for t in _norm(text).split() if t]


def _entry(name: str, kind: str, launch: str) -> Dict[str, str]:
    return {"name": name.strip(), "kind": kind, "launch": launch}


def _shortcut_dirs() -> List[Path]:
    """Desktop and Start-Menu folders (All-Users and per-user)."""
    dirs: List[Path] = []
    program_data = os.getenv("PROGRAMDATA", r"C:\ProgramData")
    dirs.append(Path(program_data) / r"Microsoft\Windows\Start Menu\Programs")
    appdata = os.getenv("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / r"Microsoft\Windows\Start Menu\Programs")
    dirs.append(Path.home() / "Desktop")
    public = os.getenv("PUBLIC", r"C:\Users\Public")
    dirs.append(Path(public) / "Desktop")
    return [d for d in dirs if d.is_dir()]


def _collect_shortcuts(base_dirs: List[Path]) -> List[Dict[str, str]]:
    """Every user-visible .lnk, including those in nested program folders."""
    found: List[Dict[str, str]] = []
    for base in base_dirs:
        try:
            paths = base.rglob("*.lnk")
        except Exception:
            continue
        for path in paths:
            lowered = str(path).lower()
            if any(kw in lowered for kw in _EXCLUDE_PATH_KEYWORDS):
                continue
            name = path.stem
            if any(kw in name.lower() for kw in _EXCLUDE_NAME_KEYWORDS):
                continue
            found.append(_entry(name, "shortcut", str(path)))
    return found


def _run_powershell_json(command: str, timeout: int = 25) -> Any:
    """Run a PowerShell snippet that emits JSON; return parsed data or None."""
    if not _IS_WIN:
        return None
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, encoding="utf-8-sig",
            errors="replace", timeout=timeout,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return None
        data = json.loads(raw)
        if isinstance(data, dict):
            return [data]
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _collect_appsfolder() -> List[Dict[str, str]]:
    """Every application registered in the Windows shell's Apps folder.

    This is the same list the Start menu shows: Start-Menu shortcuts, Store
    apps, Settings, File Explorer, and so on. It contains no System32 binaries,
    which is exactly the set a user means by "openable apps". Dynamic, so no
    hard-coded application names or aliases are needed.
    """
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$shell = New-Object -ComObject Shell.Application; "
        "$folder = $shell.Namespace('shell:AppsFolder'); "
        "if ($folder) { $folder.Items() | ForEach-Object { "
        "[PSCustomObject]@{ Name = $_.Name; AppID = $_.Path } } | "
        "ConvertTo-Json -Compress }"
    )
    data = _run_powershell_json(command)
    if not data:
        return _collect_start_apps()          # older/restricted systems

    out: List[Dict[str, str]] = []
    for item in data:
        name = str(item.get("Name") or "").strip()
        app_id = str(item.get("AppID") or "").strip()
        if not name or not app_id:
            continue
        if any(kw in name.lower() for kw in _EXCLUDE_NAME_KEYWORDS):
            continue
        lowered_id = app_id.lower()
        if lowered_id.startswith(("http://", "https://", "mailto:")):
            continue
        if lowered_id.endswith((".html", ".htm", ".hta", ".pdf", ".txt", ".url")):
            continue
        out.append(_entry(name, "uwp", app_id))
    return out


def _collect_start_apps() -> List[Dict[str, str]]:
    """Fallback discovery via Get-StartApps (also covers Store apps)."""
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    )
    data = _run_powershell_json(command)
    if not data:
        return []

    out: List[Dict[str, str]] = []
    for item in data:
        name = str(item.get("Name") or "").strip()
        app_id = str(item.get("AppID") or "").strip()
        if not name or not app_id:
            continue
        if any(kw in name.lower() for kw in _EXCLUDE_NAME_KEYWORDS):
            continue
        out.append(_entry(name, "uwp", app_id))
    return out


def _build_index() -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    entries.extend(_collect_shortcuts(_shortcut_dirs()))
    entries.extend(_collect_appsfolder())

    # One entry per visible name. A Start-Menu shortcut is preferred over a
    # duplicate Store entry because launching a .lnk is the most reliable path.
    preference = {"shortcut": 2, "uwp": 1}
    best: Dict[str, Dict[str, str]] = {}
    for entry in entries:
        key = entry["name"].strip().lower()
        if not key:
            continue
        current = best.get(key)
        if current is None or preference.get(entry["kind"], 0) > preference.get(
                current["kind"], 0):
            best[key] = entry
    return list(best.values())


def _index(refresh: bool = False) -> List[Dict[str, str]]:
    global _INDEX, _INDEX_AT
    with _INDEX_LOCK:
        stale = (time.time() - _INDEX_AT) > _INDEX_TTL
        if refresh or not _INDEX or stale:
            built = _build_index()
            if built:
                _INDEX = built
                _INDEX_AT = time.time()
            elif not _INDEX:
                # A transient discovery failure must not be cached as "no apps
                # installed" for the full five minutes.
                _INDEX_AT = time.time() - _INDEX_TTL + _FAILURE_TTL
            # If an old catalogue exists, keep it rather than replacing a real
            # answer with a transient empty one; it will be retried after TTL.
        return list(_INDEX)


def prefetch() -> None:
    """Warm the installed-app catalogue on a background thread.

    PowerShell/AppsFolder discovery costs ~1.2 s on the reference machine.
    Doing it during session startup keeps that cost off the first tool call
    without blocking the UI or the voice loop. Failure simply leaves the normal
    lazy path in place.
    """
    global _PREFETCH_THREAD
    if not _IS_WIN:
        return
    with _INDEX_LOCK:
        if _INDEX and (time.time() - _INDEX_AT) <= _INDEX_TTL:
            return
        if _PREFETCH_THREAD is not None and _PREFETCH_THREAD.is_alive():
            return
        thread = threading.Thread(
            target=_index, name="aze-app-catalogue", daemon=True)
        _PREFETCH_THREAD = thread
        thread.start()


def get_installed_programs(resolve: bool = False) -> List[Dict[str, Any]]:
    """Detailed catalogue (name + source + launch target).

    ``resolve`` is kept for backwards compatibility; targets are already
    concrete paths or Store AppIDs.
    """
    if not _IS_WIN:
        return []
    out: List[Dict[str, Any]] = []
    for entry in _index():
        item: Dict[str, Any] = {"name": entry["name"], "source": entry["kind"]}
        if entry["kind"] == "shortcut":
            item["shortcut_path"] = entry["launch"]
        else:
            item["launch"] = entry["launch"]
        out.append(item)
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Discovery
# ─────────────────────────────────────────────────────────────────────────────
def _search_score(query: str, entry: Dict[str, str]) -> float:
    """How relevant an installed app is to a discovery query.

    The display name is what matters. The launch target (shortcut path or
    Store AppID) is also searched because Store names are localised: on a
    Russian Windows, "terminal" should still surface the app whose AUMID
    contains "WindowsTerminal".
    """
    name = _norm(entry["name"])
    q = _norm(query)
    if not q:
        return 0.0

    if q == name:
        return 100.0
    if name.startswith(q):
        return 80.0
    if q in name:
        return 70.0

    q_tokens = [t for t in _tokens(q) if t not in _FILLER_WORDS] or _tokens(q)
    n_tokens = _tokens(name)
    if q_tokens and n_tokens and all(t in n_tokens for t in q_tokens):
        return 65.0
    if q_tokens and n_tokens and all(t in q_tokens for t in n_tokens):
        return 60.0

    metadata = _norm(entry.get("launch", ""))
    if q in metadata:
        return 55.0
    if q_tokens and all(t in metadata for t in q_tokens):
        return 50.0

    ratio = difflib.SequenceMatcher(None, q, name).ratio()
    return 45.0 * ratio


def _page_number(value: Any, default: int, low: int, high: int,
                 *, zero_is_valid: bool = False) -> int:
    text = "" if value is None else str(value).strip()
    if text in ("", "none", "null"):
        return default
    try:
        number = int(float(text))
    except (TypeError, ValueError):
        return default
    if number == 0 and not zero_is_valid:
        return default
    return max(low, min(high, number))


def list_apps(query: str = "", offset: Any = 0,
              limit: Any = 0) -> Dict[str, Any]:
    """Return installed applications, optionally filtered by ``query``.

    Results are paginated so the complete catalogue is reachable even when it
    is larger than one tool response: pass ``offset`` and ``limit`` and follow
    ``next_offset`` until ``has_more`` is false. With no query this is the full
    catalogue in stable, case-insensitive order. With a query it returns the
    closest names (exact, prefix, substring, then fuzzy) so the assistant can
    choose one itself. Nothing is opened here.
    """
    if not _IS_WIN:
        return {"query": query, "count": 0, "apps": [], "total": 0,
                "offset": 0, "limit": 0, "has_more": False,
                "next_offset": None,
                "note": "Opening applications is only implemented on Windows."}

    entries = _index()
    q = (query or "").strip()
    start = _page_number(offset, 0, 0, 1_000_000, zero_is_valid=True)
    if q:
        page_size = _page_number(limit, 15, 1, 200)
        scored = []
        for entry in entries:
            score = _search_score(q, entry)
            # 40 keeps genuine typos ("telegrm" -> Telegram) while dropping
            # chance letter overlaps ("chrome" -> Hermes), which is just noise.
            if score >= 40.0:
                scored.append((score, entry["name"]))
        scored.sort(key=lambda pair: (-pair[0], pair[1].lower()))
        unique: List[str] = []
        seen = set()
        for _, name in scored:
            key = name.lower()
            if key not in seen:
                seen.add(key)
                unique.append(name)
        names = unique
    else:
        page_size = _page_number(limit, 50, 1, 200)
        names = sorted({e["name"] for e in entries}, key=str.lower)

    total = len(names)
    page = names[start:start + page_size]
    consumed = start + len(page)
    has_more = consumed < total
    return {
        "query": q,
        "count": len(page),
        "apps": page,
        "total": total,
        "offset": start,
        "limit": page_size,
        "has_more": has_more,
        "next_offset": consumed if has_more else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Launching
# ─────────────────────────────────────────────────────────────────────────────
def _launch(entry: Dict[str, str]) -> None:
    kind = entry["kind"]
    target = entry["launch"]

    if kind == "uwp":
        if os.path.exists(target):
            os.startfile(target)                  # Apps folder exposed a real path
        else:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"])
        return

    if kind == "shortcut":
        os.startfile(target)                      # resolves the .lnk itself
        return

    if os.path.exists(target):
        os.startfile(target)
        return

    raise FileNotFoundError(f"'{target}' could not be found")


def open_app(app_name: str) -> Dict[str, Any]:
    """Open an installed application by exact name.

    Returns ``{"opened": name, "via": kind}`` on success. If the name does not
    exactly match an installed app, nothing is opened; the error tells the
    assistant to call ``list_apps`` and choose an exact name. Never raises.
    """
    try:
        if not _IS_WIN:
            return {"error": "Opening applications is only implemented on Windows."}

        wanted = str(app_name or "").strip()
        if not wanted:
            return {"error": "No application name was provided."}

        target_key = _norm(wanted)
        matches = [e for e in _index() if _norm(e["name"]) == target_key]

        if not matches:
            return {
                "error": (
                    f"No installed app is named '{wanted}'. Call list_apps to "
                    "see every installed app, choose the best match yourself, "
                    "then call open_app with that exact name."
                )
            }

        # Duplicate display names are collapsed by _build_index, so this is one
        # entry in practice; if not, a stable choice is better than random.
        entry = matches[0]
        try:
            _launch(entry)
        except Exception as exc:
            return {"error": f"Could not open '{entry['name']}': {exc}"}

        return {
            "opened": entry["name"],
            "via": entry["kind"],
        }
    except Exception as exc:  # pragma: no cover - defensive, never crash a call
        return {"error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        command = args[0].lower()
        if command in ("list", "list_apps", "discover"):
            query = " ".join(args[1:])
            print(json.dumps(list_apps(query), indent=2, ensure_ascii=False))
        else:
            print(json.dumps(open_app(" ".join(args)), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(get_installed_programs(), indent=2, ensure_ascii=False))

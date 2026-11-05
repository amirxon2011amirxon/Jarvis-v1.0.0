# actions/web/close_web_tool.py
"""Close one website tab in the user's real Brave session.

The assistant resolves the user's words to a website name (or asks for the
current tab) and passes that target here. This module does **not** keep a site
directory, call another model, launch a separate browser or terminate Brave.
It reads Brave's own tab strip and closes exactly one tab.

How the real session is reached
-------------------------------
Brave is Chromium, and Chromium exposes its tab strip through Windows UI
Automation (UIA). That is the same accessibility tree a screen reader uses:
each tab is a ``TabItem`` inside a ``BraveTabContainer``, with its title, its
selected state, a stable runtime id, and a ``TabCloseButton``. Reading and
invoking those elements acts on the tabs the user already has open, including
tabs they opened by hand. No debugging port, extension, second profile or
separate browser instance is involved.

Windows UI Automation is used through ``comtypes`` (already a dependency of the
optional ``pycaw`` package on Windows; it is now declared explicitly). The
import is deferred until a tab operation is requested, so normal Aze startup is
unaffected.

What each public function does
------------------------------
``get_open_tabs()``
    Return every tab in every open Brave window. Each entry carries ``tab_id``,
    ``title``, ``url`` (the active tab's address; Chromium only exposes the
    active document's URL), ``window_id``, ``window_index``, ``position`` and
    ``active``.

``match_target_tab(tabs, target)``
    Decide which tab the user meant. Matching uses the tab's real hostname when
    the browser exposes it, and its title otherwise. Site identity is the
    registrable-domain label (``docs.github.com`` -> ``github``), never a
    substring: ``github.evil.com`` is not GitHub. If more than one tab matches,
    the result asks for a choice instead of picking one.

``close_tab(tab)``
    Re-find the tab by its actual UIA identifier, select it only long enough for
    Brave to expose its close button, invoke that button, and verify the tab is
    gone. It never sends a window close, never runs ``taskkill`` and never
    navigates the tab somewhere else.

``close_web(target, tab_id="")``
    The tool entry point. ``target`` is a website/service name or
    ``"current_tab"``. ``tab_id`` is only used after the user has chosen one of
    the tabs named in a previous clarification result.

Limitations
-----------
Windows only. Chromium exposes the URL of the active tab only, so an inactive
tab is matched by its title; the tab title usually contains the site identity.
A tab with a title that never mentions the site cannot be found by name, and
the tool says so rather than guessing. UIA must be available; if Brave is
running without an open window, or Windows accessibility is unavailable, the
result explains that instead of falling back to a different browser session.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

_BROWSER = "Brave"
_WINDOW_CLASS = "Chrome_WidgetWin_1"
_TAB_CONTAINER_CLASS = "bravetabcontainer"
_CLOSE_BUTTON_CLASS = "tabclosebutton"
_BRAVE_PROCESS_NAMES = frozenset({"brave.exe", "brave", "brave-browser"})

# Chromium's accessibility tree only exists for the active document. The URL of
# every inactive tab is therefore unavailable without switching tabs, which
# would be a side effect of its own. Titles are used for those tabs instead.
_CLOSE_BUTTON_TIMEOUT = 1.0
_VERIFY_TIMEOUT = 2.0
_POLL_INTERVAL = 0.1

# How a target may be phrased. The assistant normally passes a bare site name or
# "current_tab"; these make the tool tolerant of a verbatim phrase.
_ARTICLE_RE = re.compile(r"^(?:the|my|a|an)\s+", re.IGNORECASE)
_VERB_RE = re.compile(r"^(?:close|quit|shut|stop|kill)\s+", re.IGNORECASE)
_TRAILING_NOUN_RE = re.compile(
    r"\s+(?:tab|page|website|site|window|web\s+page)$", re.IGNORECASE)
_POSSESSIVE_RE = re.compile(r"(?:'s|’s)$", re.IGNORECASE)
_CURRENT_RE = re.compile(
    r"^(?:(?:the|my)\s+)?(?:current|this|active)(?:\s+(?:tab|page|website|site|window))?$"
    r"|^(?:(?:the|my)\s+)?tab$",
    re.IGNORECASE,
)
_BROWSER_TARGETS = frozenset({
    "brave", "brave browser", "brave app", "brave window",
    "browser", "web browser",
})

# A quoted span inside an accessible name. Chromium wraps a tab's real title in
# quotes when it exposes the tab's hover card instead of the plain name.
_QUOTED_TITLE_RE = re.compile(r'["“”«»„]([^"“”«»„]{1,300})["“”«»„]')
_QUOTE_RE = re.compile(r'["“”«»„]')

# The hover card reports the tab's memory use next to the quoted title. That
# marker is language-independent (``117 МБ памяти``, ``117 MB of memory``) and a
# real page title does not end in a byte count, so it identifies the hover card
# without relying on any UI wording.
_MEMORY_SIZE_RE = re.compile(
    r"\d[\d.,]*\s*(?:bytes?|[KMGTP]i?[Bb]|[КкГгМмТтПп]?[Бб])",
    re.IGNORECASE,
)
_MULTI_PART_SUFFIX_LABELS = frozenset({
    "co", "com", "org", "net", "ac", "gov", "ne", "or", "go", "edu",
})


class BrowserError(Exception):
    """Base class for a tab operation that could not be carried out."""


class BraveNotRunning(BrowserError):
    """No Brave process exists."""


class BrowserControlError(BrowserError):
    """Brave exists but its tab strip could not be read or controlled."""


@dataclass
class _Tab:
    """Internal tab record; ``element`` is a live UIA element, never exposed."""

    tab_id: str
    title: str
    url: str
    window_id: str
    window_index: int
    position: int
    active: bool
    element: Any = field(default=None, repr=False)
    window_element: Any = field(default=None, repr=False)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tab_id": self.tab_id,
            "title": self.title,
            "url": self.url,
            "window_id": self.window_id,
            "window_index": self.window_index,
            "position": self.position,
            "active": self.active,
        }


@dataclass(frozen=True)
class _Target:
    kind: str                       # "current", "host" or "site"
    raw: str
    host: str = ""
    tokens: Tuple[str, ...] = ()
    compact: str = ""


# ─────────────────────────────────────────────────────────────────────────────
#  Windows UI Automation plumbing
# ─────────────────────────────────────────────────────────────────────────────
_UIA_LOCK = threading.Lock()
_UIA_TLS = threading.local()


def _uia_context() -> Tuple[Any, Any]:
    """Return ``(UIAutomationClient module, IUIAutomation)`` for this thread.

    COM is apartment-scoped, and tool calls run on worker threads, so the
    automation object is cached per thread. The first use initialises COM and
    generates comtypes' UIAutomation typelib wrapper.
    """
    if not sys.platform.startswith("win"):
        raise BrowserControlError(
            "Closing browser tabs is only implemented on Windows.")

    cached = getattr(_UIA_TLS, "context", None)
    if cached is not None:
        return cached

    with _UIA_LOCK:
        cached = getattr(_UIA_TLS, "context", None)
        if cached is not None:
            return cached
        try:
            import comtypes
            import comtypes.client

            comtypes.CoInitializeEx()
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA

            automation = comtypes.client.CreateObject(
                UIA.CUIAutomation, interface=UIA.IUIAutomation)
        except Exception as exc:
            raise BrowserControlError(
                "Windows UI Automation is unavailable: "
                f"{type(exc).__name__}: {exc}") from exc

        _UIA_TLS.context = (UIA, automation)
        return _UIA_TLS.context


def _brave_process_names() -> set[str]:
    names: set[str] = set()
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            try:
                name = str((proc.info or {}).get("name") or "").strip().lower()
            except Exception:
                continue
            if name in _BRAVE_PROCESS_NAMES:
                names.add(name)
    except Exception:
        return set()
    return names


def _is_brave_process(pid: int, cache: Dict[int, bool]) -> bool:
    if pid in cache:
        return cache[pid]
    try:
        import psutil
        name = str(psutil.Process(pid).name() or "").strip().lower()
        result = name in _BRAVE_PROCESS_NAMES
    except Exception:
        result = False
    cache[pid] = result
    return result


def _display_title(text: Any) -> str:
    """Clean a UIA tab name without inventing anything.

    Chromium sometimes exposes a tab's hover card as its accessible name
    instead of the plain title::

        Вкладка "Новая сессия - Claude" использует 117 МБ памяти
        Tab "Claude" uses 117 MB of memory

    The quoted part is the real page title. The byte count around it is what
    identifies a hover card: no genuine page title ends in a memory size. That
    check is language-independent and does not depend on the title's length,
    which matters because the old length comparison only rescued long titles
    and turned every ordinary one into an unmatchable string.

    A title that merely contains quotes but no such marker is handled by the
    original length comparison, so ``He said "hi" - Claude`` is left intact.
    """
    value = " ".join(str(text or "").split())
    if not value:
        return ""

    quotes = [match.start() for match in _QUOTE_RE.finditer(value)]
    if len(quotes) >= 2 and _MEMORY_SIZE_RE.search(value):
        # Outermost quotes: a hovered tab whose own title contains quotes keeps
        # them, because the card wraps the whole title rather than a fragment.
        inside = value[quotes[0] + 1:quotes[-1]].strip()
        if inside:
            return inside

    match = _QUOTED_TITLE_RE.search(value)
    if match:
        inside = match.group(1).strip()
        outside = (value[:match.start()] + value[match.end():]).strip()
        if inside and len(inside) >= len(outside):
            return inside
    return value


def _clean_window_title(text: Any) -> str:
    """Strip Brave's trailing product name from a window title."""
    value = " ".join(str(text or "").split())
    value = re.sub(
        r"\s+[—–-]\s+Brave(?:\s+\([^)]*\))?\s*$", "", value,
        flags=re.IGNORECASE)
    return value.strip()


def _tab_container(UIA: Any, uia: Any, window: Any) -> Optional[Any]:
    """The tab strip inside one Brave window, not page-level tab controls."""
    try:
        condition = uia.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId, UIA.UIA_TabControlTypeId)
        controls = window.FindAll(UIA.TreeScope_Descendants, condition)
    except Exception:
        return None
    for index in range(controls.Length):
        try:
            control = controls.GetElement(index)
            if _TAB_CONTAINER_CLASS in str(
                    control.CurrentClassName or "").lower():
                return control
        except Exception:
            continue
    return None


def _tab_elements(UIA: Any, uia: Any, window: Any) -> List[Any]:
    """The browser's own tab items, in left-to-right order."""
    container = _tab_container(UIA, uia, window)
    condition = uia.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_TabItemControlTypeId)

    if container is not None:
        found = container.FindAll(UIA.TreeScope_Descendants, condition)
        return [found.GetElement(i) for i in range(found.Length)]

    # Fallback for a Brave build that renames the container: accept only tab
    # items whose Chromium class is a browser tab and whose rectangle sits in
    # the window's top strip. This excludes page-level tab controls.
    found = window.FindAll(UIA.TreeScope_Descendants, condition)
    tabs: List[Any] = []
    try:
        rect = window.CurrentBoundingRectangle
        top_limit = rect.top + 140
    except Exception:
        top_limit = None
    for index in range(found.Length):
        try:
            item = found.GetElement(index)
            class_name = str(item.CurrentClassName or "").lower()
            if class_name not in ("bravetab", "chrometab"):
                continue
            if top_limit is not None:
                item_rect = item.CurrentBoundingRectangle
                if item_rect.top > top_limit:
                    continue
            tabs.append(item)
        except Exception:
            continue
    return tabs


def _selected(tab: Any, UIA: Any) -> bool:
    try:
        pattern = tab.GetCurrentPattern(
            UIA.UIA_SelectionItemPatternId).QueryInterface(
                UIA.IUIAutomationSelectionItemPattern)
        return bool(pattern.CurrentIsSelected)
    except Exception:
        return False


def _runtime_id(tab: Any) -> Tuple[int, ...]:
    try:
        return tuple(int(part) for part in tab.GetRuntimeId())
    except Exception:
        return ()


def _active_url(UIA: Any, uia: Any, window: Any) -> str:
    """The address of the active document, if Chromium exposes it."""
    try:
        condition = uia.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId, UIA.UIA_DocumentControlTypeId)
        documents = window.FindAll(UIA.TreeScope_Descendants, condition)
        for index in range(documents.Length):
            document = documents.GetElement(index)
            try:
                pattern = document.GetCurrentPattern(
                    UIA.UIA_ValuePatternId).QueryInterface(
                        UIA.IUIAutomationValuePattern)
                value = str(pattern.CurrentValue or "").strip()
                if value:
                    return value
            except Exception:
                continue
    except Exception:
        pass

    # Some Brave builds expose only the omnibox. It is the same address, just
    # without a scheme, and only for the active tab.
    try:
        condition = uia.CreatePropertyCondition(
            UIA.UIA_ClassNamePropertyId, "BraveOmniboxViewViews")
        edits = window.FindAll(UIA.TreeScope_Descendants, condition)
        for index in range(edits.Length):
            edit = edits.GetElement(index)
            try:
                pattern = edit.GetCurrentPattern(
                    UIA.UIA_ValuePatternId).QueryInterface(
                        UIA.IUIAutomationValuePattern)
                value = str(pattern.CurrentValue or "").strip()
                if value and " " not in value and "." in value:
                    return value if "://" in value else "https://" + value
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _read_tabs(require_windows: bool = True) -> List[_Tab]:
    """Read every tab from every open Brave window.

    Raises ``BraveNotRunning`` when there is no Brave process, and
    ``BrowserControlError`` when Brave exists but its tab strip cannot be read.
    ``require_windows=False`` returns an empty list instead when Brave has no
    open window; verification uses that because the last tab closing normally
    also closes its window.
    """
    # Do this before paying for the COM/typelib setup. It also gives the
    # clearest answer when Brave simply is not running.
    brave_running = bool(_brave_process_names())
    if not brave_running:
        if not require_windows:
            return []
        raise BraveNotRunning("Brave is not running.")

    UIA, uia = _uia_context()

    try:
        root = uia.GetRootElement()
        condition = uia.CreatePropertyCondition(
            UIA.UIA_ClassNamePropertyId, _WINDOW_CLASS)
        windows = root.FindAll(UIA.TreeScope_Children, condition)
    except Exception as exc:
        raise BrowserControlError(
            f"Brave's window list could not be read: {type(exc).__name__}: {exc}"
        ) from exc

    process_cache: Dict[int, bool] = {}
    windows_with_tabs: List[Tuple[Any, int, int, str, List[Any]]] = []
    for index in range(windows.Length):
        try:
            window = windows.GetElement(index)
            pid = int(window.CurrentProcessId)
            if not _is_brave_process(pid, process_cache):
                continue
            tabs = _tab_elements(UIA, uia, window)
            if not tabs:
                continue
            try:
                hwnd = int(window.CurrentNativeWindowHandle)
            except Exception:
                hwnd = 0
            title = _clean_window_title(window.CurrentName)
            windows_with_tabs.append((window, pid, hwnd, title, tabs))
        except Exception:
            continue

    if not windows_with_tabs:
        if not require_windows:
            return []
        raise BrowserControlError(
            "Brave is running, but no open Brave window with tabs was found.")

    records: List[_Tab] = []
    for window_index, (window, pid, hwnd, window_title, elements) in enumerate(
            windows_with_tabs, start=1):
        window_id = f"{pid}:{hwnd or window_index}"
        active_url = _active_url(UIA, uia, window)
        for position, element in enumerate(elements, start=1):
            try:
                active = _selected(element, UIA)
                title = _display_title(element.CurrentName)
                if active and window_title and window_title.casefold() != "brave":
                    title = window_title
                runtime = _runtime_id(element)
                if runtime:
                    tab_id = "uia:{pid}:{hwnd}:{runtime}".format(
                        pid=pid, hwnd=hwnd or window_index,
                        runtime=".".join(str(part) for part in runtime))
                else:
                    # A runtime id is what makes a later close safe. Without
                    # one, tie the identifier to the position and title so a
                    # changed tab strip cannot silently close a different tab.
                    tab_id = "uia:{pid}:{hwnd}:{pos}:{title}".format(
                        pid=pid, hwnd=hwnd or window_index,
                        pos=position, title=title)
                records.append(_Tab(
                    tab_id=tab_id,
                    title=title,
                    url=active_url if active else "",
                    window_id=window_id,
                    window_index=window_index,
                    position=position,
                    active=active,
                    element=element,
                    window_element=window,
                ))
            except Exception:
                continue
    return records


# ─────────────────────────────────────────────────────────────────────────────
#  Matching
# ─────────────────────────────────────────────────────────────────────────────
def _tokens(text: str) -> Tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", str(text or "").casefold(), re.UNICODE))


def _normalise_host(host: str) -> str:
    value = str(host or "").strip().strip(".").lower()
    if value.startswith("www."):
        value = value[4:]
    return value


def _registrable_label(host: str) -> str:
    """Approximate the site identity label of a hostname.

    ``docs.github.com`` -> ``github``; ``www.notion.so`` -> ``notion``;
    ``example.co.uk`` -> ``example``. This is registrable-domain reasoning, not
    a substring test, so ``github.evil.com`` -> ``evil``.
    """
    value = _normalise_host(host)
    labels = [label for label in value.split(".") if label]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if (len(labels) >= 3 and labels[-2] in _MULTI_PART_SUFFIX_LABELS
            and len(labels[-1]) == 2):
        return labels[-3]
    return labels[-2]


def _host_from_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else "https://" + raw
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    try:
        host = parts.hostname or ""
    except ValueError:
        return ""
    return _normalise_host(host)


def _host_from_target(text: str) -> str:
    """Return a hostname when the target is an address, otherwise ``""``."""
    raw = str(text or "").strip()
    if not raw or " " in raw:
        return ""
    if "://" in raw:
        return _host_from_url(raw)
    candidate = raw
    if "/" in candidate:
        candidate = candidate.split("/", 1)[0]
    if ":" in candidate:
        candidate = candidate.split(":", 1)[0]
    if "." not in candidate:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9.-]+", candidate):
        return ""
    host = _normalise_host(candidate)
    return host if "." in host else ""


def _normalise_target_text(text: str) -> str:
    value = " ".join(str(text or "").split()).strip()
    value = value.strip("\"'“”‘’")
    value = _POSSESSIVE_RE.sub("", value)
    value = value.rstrip(" ?!.,")
    # A verbatim "close Claude" is tolerated, but a one-word site named
    # "Close" is not stripped away.
    if " " in value:
        value = _VERB_RE.sub("", value, count=1).strip()
    while True:
        stripped = _ARTICLE_RE.sub("", value, count=1)
        if stripped == value:
            break
        value = stripped
    return value.strip()


def _is_current_target(text: str) -> bool:
    value = " ".join(str(text or "").split())
    value = re.sub(r"[_-]+", " ", value)
    return bool(_CURRENT_RE.match(value))


def _is_browser_target(text: str) -> bool:
    value = _normalise_target_text(text).casefold()
    return value in _BROWSER_TARGETS


def _target_from_site(text: str) -> _Target:
    tokens = _tokens(text)
    return _Target(
        kind="site",
        raw=str(text or "").strip(),
        tokens=tokens,
        compact="".join(tokens),
    )


def _parse_target(text: str) -> _Target:
    value = _normalise_target_text(text)
    if not value:
        return _Target(kind="invalid", raw="")

    if _is_current_target(value):
        return _Target(kind="current", raw=value)

    host = _host_from_target(value)
    if host:
        return _Target(kind="host", raw=value, host=host)

    # "Hugging Face tab" / "the Notion page" -> the site words.
    while True:
        stripped = _TRAILING_NOUN_RE.sub("", value, count=1).strip()
        if stripped == value:
            break
        value = stripped
    if not value:
        return _Target(kind="invalid", raw="")
    if _is_current_target(value):
        return _Target(kind="current", raw=value)
    return _target_from_site(value)


def _title_score(title: str, target: _Target) -> int:
    """Score a title as a site-name match, using whole words only."""
    title_tokens = _tokens(title)
    if not title_tokens or not target.tokens:
        return 0
    if "".join(title_tokens) == target.compact:
        return 95

    size = len(target.tokens)
    if size:
        for start in range(0, len(title_tokens) - size + 1):
            if title_tokens[start:start + size] == target.tokens:
                if start == 0 or start + size == len(title_tokens):
                    return 90
                # A site name in the middle is usually a page *about* that
                # site ("How to use Claude - YouTube"), not the site's tab.
                # Without a URL there is no safe way to tell, so do not match.
                return 0

    # "Hugging Face" in the title vs "huggingface" as one target token.
    for width in range(1, min(len(title_tokens), max(2, size + 2)) + 1):
        for start in range(0, len(title_tokens) - width + 1):
            if "".join(title_tokens[start:start + width]) == target.compact:
                if start == 0 or start + width == len(title_tokens):
                    return 78
                return 0
    return 0


def _score_tab(tab: Dict[str, Any], target: _Target) -> int:
    title = str(tab.get("title") or "")
    url = str(tab.get("url") or "")
    host = _host_from_url(url)

    if target.kind == "host":
        if host:
            if host == target.host or host.endswith("." + target.host):
                return 100
            if _registrable_label(host) == _registrable_label(target.host):
                return 95
            return 0
        # No URL is exposed for an inactive tab. The title is still evidence,
        # but only the site label from the address is used, not the whole path.
        label = _registrable_label(target.host)
        if not label:
            return 0
        return min(88, _title_score(title, _target_from_site(label)))

    if target.kind == "site":
        if host:
            if _registrable_label(host) == target.compact:
                return 100
            if host == target.compact or host.endswith("." + target.compact):
                return 95
            # The URL says this is a different site. A title that merely
            # mentions the requested word (a YouTube video about Claude, for
            # example) is not the Claude tab.
            return 0
        return _title_score(title, target)

    return 0


def _public_tab(tab: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tab_id": str(tab.get("tab_id") or ""),
        "title": str(tab.get("title") or ""),
        "url": str(tab.get("url") or ""),
        "window_id": str(tab.get("window_id") or ""),
        "window_index": int(tab.get("window_index") or 0),
        "position": int(tab.get("position") or 0),
        "active": bool(tab.get("active")),
    }


def _foreground_hwnd() -> int:
    if not sys.platform.startswith("win"):
        return 0
    try:
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _window_hwnd(window_id: str) -> int:
    try:
        return int(str(window_id).rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return 0


def _ambiguous(target: str, matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "status": "ambiguous",
        "tab": None,
        "matches": matches,
        "reason": "multiple",
        "message": (
            f'{len(matches)} open tabs match "{target}". '
            "Which one should I close?"
        ),
    }


def match_target_tab(tabs: List[Dict[str, Any]],
                     target: str) -> Dict[str, Any]:
    """Pick the one tab the target names, or ask instead of guessing.

    Returns a dictionary with ``status`` (``matched``, ``ambiguous``,
    ``not_found`` or ``invalid``), the chosen ``tab`` when unique, every
    matching tab in ``matches``, and a human-readable ``message``.
    """
    raw = " ".join(str(target or "").split()).strip()
    if not raw:
        return {
            "status": "invalid", "tab": None, "matches": [],
            "reason": "empty",
            "message": "I need to know which website to close.",
        }
    if _is_browser_target(raw):
        return {
            "status": "invalid", "tab": None, "matches": [],
            "reason": "application",
            "message": (
                "Closing Brave itself is an application action, not a "
                "website tab."
            ),
        }

    parsed = _parse_target(raw)
    if parsed.kind == "invalid":
        return {
            "status": "invalid", "tab": None, "matches": [],
            "reason": "empty",
            "message": "I need to know which website to close.",
        }

    if parsed.kind == "current":
        active = [_public_tab(tab) for tab in tabs if tab.get("active")]
        if not active:
            return {
                "status": "not_found", "tab": None, "matches": [],
                "reason": "active",
                "message": "No Brave tab is currently active.",
            }
        if len(active) == 1:
            return {
                "status": "matched", "tab": active[0], "matches": active,
                "reason": "active", "message": "",
            }
        # Several Brave windows each have an active tab. The focused Brave
        # window is the relevant one; if Brave is not focused, ask.
        foreground = _foreground_hwnd()
        preferred = [
            tab for tab in active
            if foreground and _window_hwnd(tab["window_id"]) == foreground
        ]
        if len(preferred) == 1:
            return {
                "status": "matched", "tab": preferred[0],
                "matches": preferred, "reason": "active", "message": "",
            }
        return _ambiguous(raw, active)

    scored: List[Tuple[int, Dict[str, Any]]] = []
    for tab in tabs:
        public = _public_tab(tab)
        score = _score_tab(public, parsed)
        if score > 0:
            scored.append((score, public))

    if not scored:
        return {
            "status": "not_found", "tab": None, "matches": [],
            "reason": "no-match",
            "message": f'No open Brave tab matches "{raw}".',
        }

    scored.sort(key=lambda item: (
        item[1]["window_index"], item[1]["position"]))
    matches = [tab for _, tab in scored]
    if len(matches) > 1:
        return _ambiguous(raw, matches)

    return {
        "status": "matched",
        "tab": matches[0],
        "matches": matches,
        "reason": "host" if _host_from_url(matches[0]["url"]) else "title",
        "message": "",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Discovery and closing
# ─────────────────────────────────────────────────────────────────────────────
def get_open_tabs() -> List[Dict[str, Any]]:
    """Every tab in every open Brave window, for matching and clarification.

    Raises ``BraveNotRunning`` or ``BrowserControlError`` when Brave's real
    session cannot be read. ``close_web`` turns those into structured results.
    """
    return [tab.as_dict() for tab in _read_tabs()]


def _select_tab(tab: _Tab) -> None:
    UIA, _ = _uia_context()
    pattern = tab.element.GetCurrentPattern(
        UIA.UIA_SelectionItemPatternId).QueryInterface(
            UIA.IUIAutomationSelectionItemPattern)
    if not pattern.CurrentIsSelected:
        pattern.Select()


def _find_close_button(tab: _Tab) -> Optional[Any]:
    UIA, uia = _uia_context()
    condition = uia.CreatePropertyCondition(
        UIA.UIA_ClassNamePropertyId, "TabCloseButton")
    try:
        found = tab.element.FindAll(UIA.TreeScope_Descendants, condition)
        if found.Length:
            return found.GetElement(0)
    except Exception:
        return None

    # Some Chromium builds expose the button under a slightly different class.
    # Its accessibility role is still a Button, and the class contains "close".
    try:
        button_condition = uia.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId, UIA.UIA_ButtonControlTypeId)
        found = tab.element.FindAll(
            UIA.TreeScope_Descendants, button_condition)
        for index in range(found.Length):
            button = found.GetElement(index)
            if _CLOSE_BUTTON_CLASS in str(
                    button.CurrentClassName or "").lower():
                return button
    except Exception:
        pass
    return None


def _wait_for_close_button(tab: _Tab, timeout: float) -> Optional[Any]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        button = _find_close_button(tab)
        if button is not None:
            return button
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_INTERVAL)


def _invoke_close(button: Any) -> None:
    UIA, _ = _uia_context()
    pattern = button.GetCurrentPattern(
        UIA.UIA_InvokePatternId).QueryInterface(
            UIA.IUIAutomationInvokePattern)
    pattern.Invoke()


def _wait_until_gone(tab_id: str, timeout: float) -> Optional[bool]:
    """``True`` gone, ``False`` still present, ``None`` cannot verify."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if not any(tab.tab_id == tab_id
                       for tab in _read_tabs(require_windows=False)):
                return True
        except BraveNotRunning:
            # The last tab was in the last window and Brave handled the
            # resulting window state itself. Nothing was terminated by us.
            return True
        except BrowserControlError:
            return None
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL)


def _restore_selection(previous: Optional[_Tab]) -> None:
    if previous is None:
        return
    try:
        _select_tab(previous)
    except Exception:
        pass


def _failure(message: str, error: str,
             tab: Optional[_Tab] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "message": message,
        "error": error,
    }
    if tab is not None:
        result["tab"] = tab.as_dict()
    return result


def _close_one(tab: _Tab, previous: Optional[_Tab]) -> Dict[str, Any]:
    try:
        _select_tab(tab)
    except Exception as exc:
        return _failure(
            "That tab could not be selected for closing.",
            f"The tab could not be selected: {type(exc).__name__}: {exc}",
            tab)

    button = _wait_for_close_button(tab, _CLOSE_BUTTON_TIMEOUT)
    if button is None:
        _restore_selection(previous)
        return _failure(
            "Brave did not expose a close control for that tab.",
            "The tab's close button was not available in Brave's "
            "accessibility tree.",
            tab)

    try:
        _invoke_close(button)
    except Exception as exc:
        _restore_selection(previous)
        return _failure(
            "Brave did not close that tab.",
            f"The close command failed: {type(exc).__name__}: {exc}",
            tab)

    gone = _wait_until_gone(tab.tab_id, _VERIFY_TIMEOUT)
    if gone is True:
        # Selecting a background tab to expose its close button changes the
        # active tab. Put the user's previous tab back, so closing a background
        # tab leaves the foreground exactly as it was.
        if not tab.active:
            _restore_selection(previous)
        title = tab.title or "tab"
        return {
            "success": True,
            "closed": title,
            "tab": tab.as_dict(),
            "verified": True,
            "message": f'Closed the "{title}" tab.',
        }
    if gone is False:
        _restore_selection(previous)
        return _failure(
            "Brave did not close that tab. It may be waiting for confirmation.",
            "The tab was still present after the close request.",
            tab)

    if not tab.active:
        _restore_selection(previous)
    title = tab.title or "tab"
    return {
        "success": False,
        "ok": False,
        "closed": "",
        "close_requested": True,
        "tab": tab.as_dict(),
        "verified": False,
        "reason": "unverified",
        "message": (
            f'Brave was asked to close the "{title}" tab, but I could not '
            "verify that it closed. Do not claim it is closed."
        ),
    }


def close_tab(tab: Dict[str, Any]) -> Dict[str, Any]:
    """Close exactly one tab identified by its real UIA ``tab_id``.

    The tab is looked up again immediately before closing, so a tab that
    disappeared in the meantime is reported as gone and nothing else is
    touched. Never raises.
    """
    try:
        if not isinstance(tab, dict):
            return _failure(
                "No tab was identified.",
                "close_tab needs a tab record with a tab_id.")
        tab_id = str(tab.get("tab_id") or "").strip()
        if not tab_id:
            return _failure(
                "No tab was identified.",
                "The tab record has no tab_id.")

        try:
            current = _read_tabs()
        except BraveNotRunning as exc:
            return _failure(
                "Brave isn't running, so there is no tab to close.",
                str(exc))
        except BrowserControlError as exc:
            return _failure(
                "I couldn't read Brave's tabs.", str(exc))

        target = next((item for item in current
                       if item.tab_id == tab_id), None)
        if target is None:
            return _failure(
                "That tab is no longer open.",
                "The tab disappeared before it could be closed.")

        previous = next(
            (item for item in current
             if item.window_id == target.window_id
             and item.active and item.tab_id != target.tab_id),
            None,
        )
        return _close_one(target, previous)
    except Exception as exc:  # pragma: no cover - defensive
        return _failure(
            "The tab could not be closed.",
            f"{type(exc).__name__}: {exc}")


def close_web(target: str, tab_id: str = "") -> Dict[str, Any]:
    """Close one website tab in the user's running Brave session.

    ``target`` is a website/service name (``"Claude"``, ``"Hugging Face"``) or
    ``"current_tab"``. ``tab_id`` is optional and is used only after the user
    has chosen one of the tabs named in a clarification result. Never raises;
    every failure is a structured result.
    """
    raw = " ".join(str(target or "").split()).strip()
    if not raw:
        return {
            "success": False,
            "message": "I need to know which website to close.",
            "error": "No target was provided.",
        }
    if len(raw) > 200 or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return {
            "success": False,
            "target": raw,
            "message": "That tab target is not valid.",
            "error": "The target contains control characters or is too long.",
        }
    if _is_browser_target(raw):
        return {
            "success": False,
            "target": raw,
            "message": (
                "Closing Brave itself is an application action, not a "
                "website tab."
            ),
            "error": (
                "close_web only closes one website tab. Use close_app to "
                "close the Brave application."
            ),
        }

    chosen = str(tab_id or "").strip()
    if chosen:
        result = close_tab({"tab_id": chosen})
        result.setdefault("target", raw)
        return result

    try:
        tabs = get_open_tabs()
    except BraveNotRunning as exc:
        return {
            "success": False,
            "target": raw,
            "message": "Brave isn't running, so there is no tab to close.",
            "error": str(exc),
        }
    except BrowserControlError as exc:
        return {
            "success": False,
            "target": raw,
            "message": "I couldn't read Brave's tabs.",
            "error": str(exc),
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "success": False,
            "target": raw,
            "message": "I couldn't read Brave's tabs.",
            "error": f"{type(exc).__name__}: {exc}",
        }

    match = match_target_tab(tabs, raw)
    status = match.get("status")

    if status == "matched":
        result = close_tab(match["tab"])
        result["target"] = raw
        return result

    if status == "ambiguous":
        return {
            "success": False,
            "clarification_required": True,
            "target": raw,
            "message": match["message"],
            "matches": match["matches"],
        }

    return {
        "success": False,
        "target": raw,
        "message": match.get("message") or "No matching tab was found.",
        "error": match.get("message") or "No matching tab was found.",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args and args[0].lower() in ("list", "tabs", "discover"):
        try:
            print(json.dumps(get_open_tabs(), indent=2, ensure_ascii=False))
        except BrowserError as exc:
            print(json.dumps(
                {"error": str(exc)}, indent=2, ensure_ascii=False))
    elif args:
        print(json.dumps(
            close_web(" ".join(args)), indent=2, ensure_ascii=False))
    else:
        _usage = f"python -m {__spec__.name}" if __spec__ else "python <tool file>"
        print(f"Usage: {_usage} <site|current_tab>")
        print(f"   or: {_usage} list")
        raise SystemExit(1)

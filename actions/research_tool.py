"""Deep, autonomous web research for Jarvis.

This module is orchestration, not another web client. It reuses the hardened
search and fetch paths in ``web_fetch_tool`` (validated URLs, pinned DNS,
bounded redirects, streaming byte caps, readable-text extraction and caching)
and adds:

* multi-angle query planning;
* candidate ranking, canonical-URL and near-duplicate page detection;
* breadth-first reading of multiple independent sources;
* heuristic source-credibility scoring;
* extractive claim selection with cross-source corroboration;
* contradiction and uncertainty detection;
* a structured report for the Research panel plus a Markdown version
  for the model and for copying;
* progressive ``research_state`` events while work is in progress.

Nothing in the report is invented. Every finding, statistic and quoted sentence
comes from a successfully fetched page, and every claim carries the source ids
it came from. The report explicitly separates cross-referenced findings from
single-source and unverified material.
"""
from __future__ import annotations

import copy
import os
import re
import threading
import time
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from actions import get_tool_module


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_web = get_tool_module("web_fetch")
if _web is None:  # pragma: no cover - packaging error, not a runtime path
    raise ImportError("research_tool requires actions/web_fetch_tool.py")

search_web = _web.search_web
fetch_pages = _web.fetch_pages


# ── limits ───────────────────────────────────────────────────────────────────
MAX_TOPIC_CHARS = 600
MAX_FOCUS_CHARS = 200
MAX_SOURCES_CAP = 20
DEFAULT_SOURCES = {"standard": 8, "deep": 12, "exhaustive": 16}
DEPTH_QUERIES = {"standard": 3, "deep": 5, "exhaustive": 7}
RESULTS_PER_QUERY = {"standard": 5, "deep": 6, "exhaustive": 8}
FETCH_BATCH = 6
MAX_CANDIDATES = 28
MAX_CLAIM_CANDIDATES = 90
MAX_SENTENCES_PER_SOURCE = 140
RESEARCH_DEADLINE_SECONDS = 100.0

# Generic language only. No topics or source names are hardcoded here.
_STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because
been before being below between both but by can could did do does doing down
during each few for from further had has have having he her here hers herself
him himself his how i if in into is it its itself just me more most my myself
no nor not now of off on once only or other our ours ourselves out over own
same she should so some such than that the their theirs them themselves then
there these they this those through to too under until up very was we were what
when where which while who whom why will with would you your yours yourself
yourselves
""".split())

_GENERIC_CLAIM_WORDS = frozenset("""
according also analysis based because been being between could data different
during each example first found from further has have having however including
into its large likely made many more most much other overall people percent
reported reports said says second several show showed shows significant similar
since some study such than that their them there these they this those three
through total two under used uses using very was were what when where which
while with within without would
""".split())

_BOILERPLATE = (
    "accept all cookies", "all rights reserved", "cookie policy",
    "cookies to improve", "enable javascript", "javascript is disabled",
    "manage cookies", "newsletter", "privacy policy", "sign in",
    "skip to main content", "subscribe to", "terms of service",
)
_UNCERTAINTY_WORDS = (
    "appears", "approximately", "could", "estimate", "estimated", "likely",
    "may", "might", "not clear", "possibly", "preliminary", "reportedly",
    "suggest", "suggests", "uncertain", "unclear", "unconfirmed", "unlikely",
)
_EVIDENCE_WORDS = (
    "according to", "analysis", "data", "found", "measured", "report",
    "research", "sample", "shows", "study", "survey", "test", "tested",
)
_CONFLICT_WORDS = (
    "although", "but", "contrary", "conversely", "dispute", "disputed",
    "however", "inconsistent", "nevertheless", "unlike", "whereas", "yet",
)
_POSITIVE_DIRECTION = frozenset({
    "above", "gain", "grew", "growth", "higher", "increase", "increased",
    "more", "positive", "rise", "rose", "up",
})
_NEGATIVE_DIRECTION = frozenset({
    "below", "decline", "decreased", "down", "fall", "fell", "fewer", "lower",
    "less", "negative", "shrink", "shrinking",
})
_NEGATION_WORDS = frozenset({
    "false", "never", "no", "not", "unlikely", "without",
})

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#.'-]*", re.I)
_NUMBER_RE = re.compile(
    r"(?<![\w])(?:[$€£]\s?)?\d[\d,]*(?:\.\d+)?\s?"
    r"(?:%|percent|million|billion|trillion|thousand|bn|k|km|kg|gb|mb|ms|"
    r"seconds?|minutes?|hours?|days?|weeks?|months?|years?|people|users|"
    r"requests|tokens|usd|eur|gbp)?",
    re.I,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(\[])")


# ── text helpers ─────────────────────────────────────────────────────────────
def _clean(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _tokens(text: object) -> List[str]:
    """Normalised content tokens used for relevance and similarity."""
    result: List[str] = []
    for raw in _WORD_RE.findall(str(text or "").casefold()):
        token = raw.strip(".'-")
        if len(token) < 2 or token in _STOPWORDS:
            continue
        if len(token) > 4:
            if token.endswith("ies"):
                token = token[:-3] + "y"
            elif token.endswith("sses"):
                token = token[:-2]
            elif token.endswith("s") and not token.endswith("ss"):
                token = token[:-1]
        if token not in result:
            result.append(token)
    return result


def _numbers(text: object) -> Tuple[str, ...]:
    values: List[str] = []
    for raw in _NUMBER_RE.findall(str(text or "")):
        value = raw.casefold().replace(",", "").replace(" ", "")
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _domain_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _canonical_url(url: str) -> str:
    """Canonical identity for deduplication; fragment and trackers removed."""
    try:
        parts = urlsplit(str(url or "").strip())
        port_number = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname.casefold().removeprefix("www.")
    port = f":{port_number}" if port_number and port_number not in (80, 443) else ""
    path = re.sub(r"/+$", "", parts.path) or "/"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in {
            "fbclid", "gclid", "msclkid", "yclid", "mc_cid", "mc_eid",
        }:
            continue
        query.append((key, value))
    return urlunsplit((
        parts.scheme.casefold(), host + port, path,
        urlencode(query), "",
    ))


def _sentences(content: str, topic_terms: set[str]) -> List[str]:
    """Extract usable sentences, rejecting navigation/boilerplate fragments."""
    result: List[str] = []
    for raw_line in str(content or "").splitlines():
        line = _clean(raw_line)
        if len(line) < 45:
            continue
        pieces = _SENTENCE_SPLIT.split(line)
        for raw_sentence in pieces:
            sentence = _clean(raw_sentence)
            if not (45 <= len(sentence) <= 650):
                continue
            words = sentence.split()
            if len(words) < 6:
                continue
            lowered = sentence.casefold()
            if any(phrase in lowered for phrase in _BOILERPLATE) and not (
                    set(_tokens(sentence)) & topic_terms):
                continue
            if sum(ch.isalpha() for ch in sentence) < 30:
                continue
            if sentence not in result:
                result.append(sentence)
            if len(result) >= MAX_SENTENCES_PER_SOURCE:
                return result
    return result


def _query_variants(topic: str, focus: str, depth: str) -> List[str]:
    year = datetime.now().year
    queries = [
        topic,
        f"{topic} official documentation",
        f"{topic} research study data",
        f"{topic} limitations criticism",
        f"{topic} latest {year}",
        f"{topic} statistics report",
        f"{topic} independent analysis comparison",
    ]
    if focus:
        queries.insert(1, f"{topic} {focus}")
    desired = DEPTH_QUERIES.get(depth, DEPTH_QUERIES["deep"])
    unique: List[str] = []
    seen: set[str] = set()
    for query in queries:
        query = query.strip()
        if len(query) > 480:
            query = query[:477].rstrip() + "..."
        key = query.casefold().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(query)
        if len(unique) >= desired:
            break
    return unique


# ── research service / UI bridge ─────────────────────────────────────────────
def _empty_state() -> Dict[str, Any]:
    return {
        "open": False,
        "job_id": "",
        "status": "idle",
        "topic": "",
        "depth": "deep",
        "phase": "",
        "message": "",
        "progress": 0,
        "sources_found": 0,
        "sources_read": 0,
        "started_at": "",
        "updated_at": "",
        "report": None,
        "events": [],
        "error": "",
        "warnings": [],
        "cancelled": False,
    }


class ResearchService:
    """Thread-safe report/state shared by the tool and the local web UI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._publish: Optional[Callable[[Dict[str, Any]], None]] = None
        self._log: Optional[Callable[[str], None]] = None
        self._state: Dict[str, Any] = _empty_state()
        self._cancel = threading.Event()

    def configure(
        self,
        publish: Callable[[Dict[str, Any]], None],
        *,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        with self._lock:
            self._publish = publish
            self._log = log

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _job_matches_locked(self, job_id: Optional[str]) -> bool:
        return not job_id or str(job_id) == str(self._state.get("job_id") or "")

    def _emit_log(self, message: str) -> None:
        sink = self._log
        if sink is None:
            return
        try:
            sink(message)
        except Exception:
            pass

    def begin(self, topic: str, depth: str, max_sources: int) -> Optional[str]:
        """Start a job. None means another job is already running."""
        with self._lock:
            if self._state.get("status") == "running":
                return None
            job_id = "research-" + uuid.uuid4().hex[:12]
            state = _empty_state()
            state.update({
                "open": True,
                "job_id": job_id,
                "status": "running",
                "topic": topic,
                "depth": depth,
                "phase": "Planning",
                "message": (
                    f"Planning research queries (target up to {max_sources} "
                    "independent sources)…"),
                "progress": 1,
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
                "events": [{
                    "ts": _now_iso(),
                    "phase": "Planning",
                    "message": f"Starting deep research on: {topic}",
                }],
                "warnings": [],
            })
            self._state = state
            self._cancel.clear()
            self._emit_locked()
            self._emit_log(f"SYS: Deep research started — {topic}")
            return job_id

    def update(
        self,
        *,
        phase: str = "",
        message: str = "",
        progress: Optional[int] = None,
        sources_found: Optional[int] = None,
        sources_read: Optional[int] = None,
        warning: str = "",
        job_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            if not self._job_matches_locked(job_id):
                return
            if self._state.get("status") != "running" or self._cancel.is_set():
                return
            if phase:
                self._state["phase"] = phase
            if message:
                self._state["message"] = message
                self._append_event_locked(phase or self._state["phase"], message)
            if progress is not None:
                self._state["progress"] = max(0, min(99, int(progress)))
            if sources_found is not None:
                self._state["sources_found"] = max(0, int(sources_found))
            if sources_read is not None:
                self._state["sources_read"] = max(0, int(sources_read))
            if warning:
                warnings = self._state.setdefault("warnings", [])
                if warning not in warnings:
                    warnings.append(warning)
            self._state["updated_at"] = _now_iso()
            self._emit_locked()

    def complete(self, report: Dict[str, Any],
                 *, job_id: Optional[str] = None) -> bool:
        with self._lock:
            if not self._job_matches_locked(job_id):
                return False
            if self._cancel.is_set() or self._state.get("status") != "running":
                return False
            self._state.update({
                "status": "complete",
                "phase": "Complete",
                "message": (
                    f"Research complete — {len(report.get('sources', []))} "
                    "sources read."),
                "progress": 100,
                "report": copy.deepcopy(report),
                "updated_at": _now_iso(),
                "cancelled": False,
            })
            self._append_event_locked("Complete", self._state["message"])
            self._emit_locked()
            self._emit_log(
                f"SYS: Research complete — {len(report.get('sources', []))} "
                "sources read; report ready.")
            return True

    def fail(
        self,
        message: str,
        *,
        report: Optional[Dict[str, Any]] = None,
        job_id: Optional[str] = None,
    ) -> bool:
        with self._lock:
            if not self._job_matches_locked(job_id):
                return False
            if self._cancel.is_set() or self._state.get(
                    "status") == "cancelled":
                return False
            self._state.update({
                "status": "error",
                "phase": "Failed",
                "message": message,
                "progress": self._state.get("progress", 0),
                "error": message,
                "report": copy.deepcopy(report) if report else None,
                "updated_at": _now_iso(),
                "cancelled": False,
            })
            self._append_event_locked("Failed", message)
            self._emit_locked()
            self._emit_log(f"ERR: Deep research failed — {message}")
            return True

    def cancel(self, reason: str = "",
               *, job_id: Optional[str] = None) -> bool:
        """Cooperatively cancel the active research job."""
        message = _clean(reason) or "Research cancelled by the user."
        with self._lock:
            if not self._job_matches_locked(job_id):
                return False
            if self._state.get("status") != "running":
                return False
            self._cancel.set()
            self._state.update({
                "status": "cancelled",
                "phase": "Cancelled",
                "message": message,
                "progress": self._state.get("progress", 0),
                "report": None,
                "error": "",
                "cancelled": True,
                "updated_at": _now_iso(),
            })
            self._append_event_locked("Cancelled", message)
            self._emit_locked()
            self._emit_log(f"SYS: Deep research cancelled — {message}")
            return True

    def public_state(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def reset(self) -> None:
        with self._lock:
            self._cancel.clear()
            self._state = _empty_state()
            self._emit_locked()

    def _append_event_locked(self, phase: str, message: str) -> None:
        events = self._state.setdefault("events", [])
        events.append({"ts": _now_iso(), "phase": phase,
                       "message": _clean(message)})
        while len(events) > 30:
            events.pop(0)

    def _emit_locked(self) -> None:
        sink = self._publish
        if sink is None:
            return
        try:
            sink({
                "type": "research_state",
                "state": copy.deepcopy(self._state),
                "ts": _now_iso(),
            })
        except Exception as exc:  # noqa: BLE001 - UI must never stop research
            print(f"[Research] Event publish failed: {exc}")


_SERVICE = ResearchService()
_SERVICE_LOCK = threading.Lock()


def get_service() -> ResearchService:
    return _SERVICE


def reset_service() -> ResearchService:
    """Replace the process service (tests and isolated hosts)."""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = ResearchService()
        return _SERVICE


# ── candidates and sources ───────────────────────────────────────────────────
@dataclass
class _Candidate:
    url: str
    canonical: str
    title: str
    snippet: str
    date: str
    provider: str
    query: str
    score: float = 0.0


@dataclass
class _Source:
    id: int
    url: str
    final_url: str
    title: str
    domain: str
    content: str
    retrieved_at: str
    published: str
    snippet: str
    credibility_score: float
    credibility_label: str
    credibility_reasons: List[str] = field(default_factory=list)


@dataclass
class _Claim:
    id: str
    text: str
    source_id: int
    source_domain: str
    tokens: Tuple[str, ...]
    numbers: Tuple[str, ...]
    score: float
    uncertain: bool
    conflict_signal: bool
    support_source_ids: List[int] = field(default_factory=list)
    support_domains: List[str] = field(default_factory=list)
    theme: str = "General evidence"


def _authority_signal(url: str) -> Tuple[float, List[str]]:
    """Generic authority hints from domain suffix, path and file type."""
    score = 0.0
    reasons: List[str] = []
    try:
        parts = urlsplit(url)
    except ValueError:
        return score, reasons
    host = (parts.hostname or "").casefold()
    path = parts.path.casefold()
    if host.endswith(".gov") or ".gov." in host:
        score += 3.0
        reasons.append("government domain")
    elif (host.endswith(".edu") or ".edu." in host
          or host.endswith(".ac.uk") or ".ac." in host):
        score += 2.5
        reasons.append("academic domain")
    elif host.endswith(".int"):
        score += 2.4
        reasons.append("international organisation domain")
    elif host.endswith(".org"):
        score += 0.6
        reasons.append("organisational domain")

    markers = (
        "/docs", "/documentation", "/reference", "/research", "/paper",
        "/papers", "/publication", "/publications", "/report", "/reports",
        "/data", "/dataset", "/whitepaper", "/methodology", "/about",
    )
    if any(marker in path for marker in markers):
        score += 1.2
        reasons.append("documentation, research or data path")
    if path.endswith(".pdf"):
        score += 0.8
        reasons.append("PDF document")
    if "/tag/" in path or "/category/" in path or path.rstrip("/").endswith(
            "/search"):
        score -= 0.8
        reasons.append("listing or archive path")
    return score, reasons


def _candidate_score(candidate: _Candidate, topic_terms: set[str]) -> float:
    title_tokens = set(_tokens(candidate.title))
    snippet_tokens = set(_tokens(candidate.snippet))
    host_tokens = set(_tokens(_domain_of(candidate.url)))
    score = float(len(topic_terms & title_tokens)) * 1.1
    score += float(len(topic_terms & snippet_tokens)) * 0.55
    score += float(len(topic_terms & host_tokens)) * 1.8
    authority, _ = _authority_signal(candidate.url)
    score += authority
    if candidate.date:
        score += 0.35
    if len(candidate.snippet) >= 90:
        score += 0.25
    return round(score, 4)


def _source_credibility(
    source: _Source, topic_terms: set[str]
) -> Tuple[float, str, List[str]]:
    """Heuristic credibility, explicitly reported as a heuristic."""
    score = 2.0
    reasons: List[str] = []
    authority, authority_reasons = _authority_signal(source.final_url)
    score += authority
    reasons.extend(authority_reasons)

    host = source.domain
    title_tokens = set(_tokens(source.title))
    host_tokens = set(_tokens(host))
    content_tokens = set(_tokens(source.content[:5000]))

    overlap = topic_terms & host_tokens
    if overlap:
        score += 1.5
        reasons.append("domain matches the research subject")
    elif topic_terms & title_tokens:
        score += 0.8
        reasons.append("title matches the research subject")
    if len(content_tokens & topic_terms) >= 4:
        score += 0.5
        reasons.append("page content covers the research subject")

    if len(source.content) >= 5000:
        score += 0.9
        reasons.append("substantial extracted text")
    elif len(source.content) >= 1500:
        score += 0.35
        reasons.append("moderate extracted text")
    elif len(source.content) < 600:
        score -= 1.4
        reasons.append("very little extracted text")
    if source.published:
        score += 0.25
        reasons.append("publication date supplied")

    lowered_title = source.title.casefold()
    if any(term in lowered_title for term in (
            "press release", "sponsored", "advertisement", "advertorial")):
        score -= 1.5
        reasons.append("promotional presentation")

    score = max(0.0, min(10.0, round(score, 2)))
    if score >= 6.5:
        label = "high"
    elif score >= 4.0:
        label = "medium"
    else:
        label = "low"
    if not reasons:
        reasons.append("generic web source")
    reasons.append("heuristic score, not an endorsement")
    return score, label, reasons


def _near_duplicate(a: str, b: str) -> bool:
    """Cheap near-duplicate detection for syndicated articles/press releases."""
    def signature(text: str) -> set[tuple[str, ...]]:
        words = re.findall(r"[a-z0-9]+", text.casefold())[:350]
        if len(words) < 25:
            return set()
        return {tuple(words[index:index + 5])
                for index in range(0, len(words) - 4)}

    left, right = signature(a[:4000]), signature(b[:4000])
    if not left or not right:
        return False
    overlap = len(left & right)
    return overlap / max(1, min(len(left), len(right))) >= 0.82


def _select_candidates(
    candidates: Sequence[_Candidate], limit: int
) -> List[_Candidate]:
    selected: List[_Candidate] = []
    per_domain: Counter[str] = Counter()
    for candidate in sorted(
            candidates, key=lambda item: (-item.score, item.url.casefold())):
        domain = _domain_of(candidate.url)
        if per_domain[domain] >= 2:
            continue
        if any(item.canonical == candidate.canonical for item in selected):
            continue
        selected.append(candidate)
        per_domain[domain] += 1
        if len(selected) >= limit:
            break
    return selected


# ── claim extraction and corroboration ───────────────────────────────────────
def _claim_score(
    sentence: str,
    tokens: Sequence[str],
    topic_terms: set[str],
    numbers: Sequence[str],
    credibility: float,
    position: int,
) -> Tuple[float, bool, bool]:
    lowered = sentence.casefold()
    overlap = topic_terms & set(tokens)
    score = 1.0
    score += min(4, len(overlap)) * 0.85
    score += min(3, len(numbers)) * 0.75
    score += credibility / 5.0
    score += 0.5 if any(word in lowered for word in _EVIDENCE_WORDS) else 0.0
    score += 0.25 if 80 <= len(sentence) <= 340 else 0.0
    score += max(0.0, 0.3 - position * 0.02)
    uncertain = any(word in lowered for word in _UNCERTAINTY_WORDS)
    conflict_signal = any(word in lowered for word in _CONFLICT_WORDS)
    if uncertain:
        score -= 0.15
    if conflict_signal:
        score -= 0.1
    return round(score, 3), uncertain, conflict_signal


def _extract_claims(
    sources: Sequence[_Source],
    topic_terms: set[str],
    *,
    require_topic: bool = True,
) -> List[_Claim]:
    claims: List[_Claim] = []
    for source in sources:
        local: List[_Claim] = []
        for position, sentence in enumerate(
                _sentences(source.content, topic_terms)):
            tokens = tuple(_tokens(sentence))
            if len(tokens) < 5:
                continue
            if require_topic and not (set(tokens) & topic_terms):
                continue
            numbers = _numbers(sentence)
            score, uncertain, conflict_signal = _claim_score(
                sentence, tokens, topic_terms, numbers,
                source.credibility_score, position)
            local.append(_Claim(
                id=f"s{source.id}c{len(local) + 1}",
                text=sentence,
                source_id=source.id,
                source_domain=source.domain,
                tokens=tokens,
                numbers=numbers,
                score=score,
                uncertain=uncertain,
                conflict_signal=conflict_signal,
            ))
        local.sort(key=lambda item: -item.score)
        claims.extend(local[:18])
    claims.sort(key=lambda item: -item.score)
    return claims[:MAX_CLAIM_CANDIDATES]


def _context_similarity(a: _Claim, b: _Claim) -> float:
    if a.source_id == b.source_id or a.source_domain == b.source_domain:
        return 0.0
    left, right = set(a.tokens), set(b.tokens)
    if len(left) < 5 or len(right) < 5:
        return 0.0
    intersection = len(left & right)
    if intersection < 4:
        return 0.0
    jaccard = intersection / len(left | right)
    containment = intersection / min(len(left), len(right))
    return max(jaccard, containment * 0.82)


def _supports(a: _Claim, b: _Claim) -> bool:
    similarity = _context_similarity(a, b)
    if similarity < 0.46:
        return False
    if a.numbers and b.numbers and not (set(a.numbers) & set(b.numbers)):
        # Same subject, incompatible figures: this is a possible conflict, not
        # corroboration.
        return False
    if (a.numbers or b.numbers) and similarity < 0.55:
        return False
    return True


def _apply_support(claims: Sequence[_Claim]) -> None:
    for claim in claims:
        source_ids = {claim.source_id}
        domains = {claim.source_domain}
        for other in claims:
            if other is claim:
                continue
            if _supports(claim, other):
                source_ids.add(other.source_id)
                domains.add(other.source_domain)
        claim.support_source_ids = sorted(source_ids)
        claim.support_domains = sorted(domains)


def _contradiction(a: _Claim, b: _Claim) -> Optional[str]:
    if a.source_id == b.source_id or a.source_domain == b.source_domain:
        return None
    if _context_similarity(a, b) < 0.42:
        return None
    if _supports(a, b):
        return None
    if a.numbers and b.numbers and not (set(a.numbers) & set(b.numbers)):
        return "The sources report different figures for a closely related claim."
    left, right = set(a.tokens), set(b.tokens)
    a_pos = bool(left & _POSITIVE_DIRECTION)
    a_neg = bool(left & _NEGATIVE_DIRECTION)
    b_pos = bool(right & _POSITIVE_DIRECTION)
    b_neg = bool(right & _NEGATIVE_DIRECTION)
    if (a_pos and b_neg) or (a_neg and b_pos):
        return "The sources describe opposite directions or conclusions."
    a_negation = bool(left & _NEGATION_WORDS)
    b_negation = bool(right & _NEGATION_WORDS)
    if a_negation != b_negation and _context_similarity(a, b) >= 0.52:
        return "One source negates a claim that another states."
    return None


def _assign_themes(claims: Sequence[_Claim], topic_terms: set[str]) -> None:
    counter: Counter[str] = Counter()
    for claim in claims:
        for token in set(claim.tokens):
            if token in topic_terms or token in _GENERIC_CLAIM_WORDS:
                continue
            counter[token] += 1
    themes = [word for word, count in counter.most_common(5) if count >= 2]
    for claim in claims:
        theme = next((word for word in themes if word in claim.tokens), "")
        claim.theme = theme.replace("-", " ").title() if theme else "General evidence"


def _item(claim: _Claim, *, kind: str = "evidence",
          note: str = "") -> Dict[str, Any]:
    cross_referenced = len(claim.support_domains) >= 2
    return {
        "id": claim.id,
        "text": claim.text,
        "kind": kind,
        "theme": claim.theme,
        "source_ids": list(claim.support_source_ids or [claim.source_id]),
        "support_count": len(claim.support_domains or [claim.source_domain]),
        "confidence": "cross-referenced" if cross_referenced else "single-source",
        "numbers": list(claim.numbers),
        "note": note,
    }


def _select_diverse(
    claims: Sequence[_Claim],
    limit: int,
    *,
    excluded: set[str] | None = None,
    require_support: bool = False,
    numeric: bool = False,
) -> List[_Claim]:
    excluded = excluded or set()
    selected: List[_Claim] = []
    per_domain: Counter[str] = Counter()
    for claim in claims:
        if claim.id in excluded:
            continue
        if require_support and len(claim.support_domains) < 2:
            continue
        if numeric and not claim.numbers:
            continue
        if any(_context_text_similar(claim.text, other.text) >= 0.8
               for other in selected):
            continue
        if per_domain[claim.source_domain] >= 2:
            continue
        selected.append(claim)
        per_domain[claim.source_domain] += 1
        if len(selected) >= limit:
            break
    return selected


def _context_text_similar(left: str, right: str) -> float:
    a, b = set(_tokens(left)), set(_tokens(right))
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    return max(intersection / len(a | b),
               intersection / min(len(a), len(b)) * 0.82)


def _confidence(
    sources: Sequence[_Source],
    key_findings: Sequence[Dict[str, Any]],
    contradictions: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    domains = {source.domain for source in sources}
    supported = sum(1 for item in key_findings
                    if item["confidence"] == "cross-referenced")
    high_sources = sum(1 for source in sources
                       if source.credibility_label == "high")
    if (len(sources) >= 6 and len(domains) >= 4 and supported >= 3
            and len(contradictions) <= 2 and high_sources >= 2):
        level = "high"
    elif len(sources) >= 3 and len(domains) >= 2 and supported >= 1:
        level = "medium"
    else:
        level = "low"
    reasons = [
        f"{len(sources)} readable source(s) across {len(domains)} independent "
        "domain(s)",
        f"{supported} key finding(s) corroborated by at least two domains",
    ]
    if contradictions:
        reasons.append(
            f"{len(contradictions)} possible contradiction(s) were identified")
    if high_sources:
        reasons.append(f"{high_sources} source(s) scored high on the "
                       "credibility heuristic")
    if level == "low":
        reasons.append(
            "the evidence base is too thin for a confident conclusion")
    return {"level": level, "reason": "; ".join(reasons)}


def _source_dict(source: _Source) -> Dict[str, Any]:
    return {
        "id": source.id,
        "title": source.title or source.domain or source.final_url,
        "url": source.final_url,
        "domain": source.domain,
        "published": source.published,
        "retrieved_at": source.retrieved_at,
        "snippet": source.snippet,
        "credibility": {
            "score": source.credibility_score,
            "label": source.credibility_label,
            "reasons": list(source.credibility_reasons),
        },
    }


def _markdown_report(report: Dict[str, Any]) -> str:
    lines = [
        f"# Research report: {report['topic']}",
        "",
        f"Generated: {report['generated_at']}",
        f"Confidence: {report['confidence']['level'].upper()} — "
        f"{report['confidence']['reason']}",
        "",
        "## Summary",
        "",
        report["summary"],
    ]
    for section in report["sections"]:
        items = section.get("items") or []
        if not items:
            continue
        lines.extend(["", f"## {section['heading']}", ""])
        for item in items:
            sources = ", ".join(
                f"[{source_id}]" for source_id in item.get("source_ids", []))
            note = f" — {item['note']}" if item.get("note") else ""
            if item.get("kind") == "contradiction" and item.get(
                    "contrasting_text"):
                lines.append(
                    f"- {item['text']} {sources}\n"
                    f"  Contrasting view: {item['contrasting_text']} "
                    f"{', '.join(f'[{i}]' for i in item.get('contrast_source_ids', []))}"
                    f"{note}")
            else:
                lines.append(f"- {item['text']} {sources}{note}")
    if report["limitations"]:
        lines.extend(["", "## Limitations", ""])
        lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(["", "## Sources", ""])
    for source in report["sources"]:
        lines.append(
            f"{source['id']}. **{source['title']}** — {source['domain']} — "
            f"{source['url']} (credibility: {source['credibility']['label']})")
    return "\n".join(lines).strip()


def _plain_prose(report: Dict[str, Any]) -> str:
    """A paragraph-only version of the report for spoken/chat delivery."""
    sources = {source["id"]: source for source in report.get("sources", [])}

    def names(ids: Sequence[int]) -> str:
        labels = []
        for source_id in ids[:3]:
            source = sources.get(source_id) or {}
            labels.append(str(source.get("domain") or source.get("title") or ""))
        return ", ".join(label for label in labels if label)

    paragraphs = [report["summary"]]
    key = report.get("key_findings") or []
    if key:
        first = key[0]
        sentence = first["text"]
        attribution = names(first.get("source_ids", []))
        if attribution:
            sentence += f" ({attribution})"
        paragraphs.append(sentence)
        if len(key) > 1:
            second = key[1]
            sentence = f"Another finding states that {second['text'].lstrip()}"
            attribution = names(second.get("source_ids", []))
            if attribution:
                sentence += f" ({attribution})"
            paragraphs.append(sentence)
    stats = report.get("statistics") or []
    if stats:
        paragraph = "The numeric evidence includes " + "; ".join(
            item["text"].rstrip(".") for item in stats[:2]) + "."
        paragraphs.append(paragraph)
    contradictions = report.get("contradictions") or []
    if contradictions:
        item = contradictions[0]
        paragraphs.append(
            "The sources disagree in places. "
            + item["text"].rstrip(".")
            + " A contrasting view states that "
            + item.get("contrasting_text", "").rstrip(".")
            + ". " + item.get("note", ""))
    uncertainties = report.get("uncertainties") or []
    if uncertainties:
        item = uncertainties[0]
        paragraphs.append(
            "One claim remains unverified: "
            + item["text"].rstrip(".")
            + " This is based on a single source and should be treated with "
            "caution.")
    conclusions = report.get("conclusions") or []
    if conclusions:
        paragraphs.append(conclusions[-1]["text"])
    return "\n\n".join(
        paragraph.strip() for paragraph in paragraphs if paragraph.strip())


# ── report construction ──────────────────────────────────────────────────────
def _build_report(
    *,
    topic: str,
    focus: str,
    depth: str,
    queries: Sequence[str],
    sources: Sequence[_Source],
    candidates: Sequence[_Candidate],
    search_errors: Sequence[str],
    fetch_failures: Sequence[Dict[str, str]],
    duplicate_pages: Sequence[str],
    elapsed: float,
    max_sources: int,
    pages_attempted: int,
) -> Dict[str, Any]:
    topic_terms = set(_tokens(topic + " " + focus))
    claims = _extract_claims(sources, topic_terms)
    if not claims and topic_terms:
        # A page can use synonyms the query did not contain. Fall back to
        # extractive claims without requiring an exact topic token, but keep
        # the score lower and the uncertainty labelling intact.
        claims = _extract_claims(sources, topic_terms, require_topic=False)
    _apply_support(claims)
    _assign_themes(claims, topic_terms)

    excluded: set[str] = set()
    key_claims = _select_diverse(claims, 5)
    key_findings = [_item(claim, kind="finding") for claim in key_claims]
    excluded.update(claim.id for claim in key_claims)

    evidence_claims = _select_diverse(
        claims, 10, excluded=excluded, require_support=True)
    if not evidence_claims:
        evidence_claims = _select_diverse(
            claims, 6, excluded=excluded)
    evidence = [_item(claim, kind="evidence") for claim in evidence_claims]
    excluded.update(claim.id for claim in evidence_claims)

    statistic_claims = _select_diverse(
        claims, 8, excluded=excluded, numeric=True)
    if not statistic_claims:
        # Key findings often contain the only numeric evidence. Reuse those in
        # the statistics section so the report still surfaces real data points.
        statistic_claims = _select_diverse(claims, 6, numeric=True)
    statistics = [
        _item(claim, kind="statistic",
              note="Numeric claim extracted from the source page.")
        for claim in statistic_claims
    ]
    excluded.update(claim.id for claim in statistic_claims)

    single_source = [
        claim for claim in claims
        if len(claim.support_domains) < 2
        and claim.id not in {item["id"] for item in key_findings}
    ]
    uncertainty_claims = _select_diverse(single_source, 8)
    uncertainties = [
        _item(claim, kind="unverified",
              note="Only one independent source was located for this claim.")
        for claim in uncertainty_claims
    ]

    contradictions: List[Dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for index, left in enumerate(claims):
        for right in claims[index + 1:]:
            pair = tuple(sorted((left.id, right.id)))
            if pair in seen_pairs:
                continue
            explanation = _contradiction(left, right)
            if not explanation:
                continue
            seen_pairs.add(pair)
            contradictions.append({
                "id": f"c{len(contradictions) + 1}",
                "kind": "contradiction",
                "text": left.text,
                "contrasting_text": right.text,
                "source_ids": [left.source_id, right.source_id],
                "contrast_source_ids": [right.source_id],
                "support_count": max(1, len(left.support_domains)),
                "confidence": "conflicting",
                "theme": left.theme,
                "numbers": list(left.numbers) + list(right.numbers),
                "note": explanation,
            })
            if len(contradictions) >= 5:
                break
        if len(contradictions) >= 5:
            break

    confidence = _confidence(sources, key_findings, contradictions)
    domains = sorted({source.domain for source in sources})
    supported_count = sum(
        1 for item in key_findings
        if item["confidence"] == "cross-referenced")

    if key_findings:
        strongest = key_findings[0]
        if supported_count:
            summary = (
                f"Across {len(sources)} readable sources from "
                f"{len(domains)} independent domains, {supported_count} key "
                f"finding(s) were corroborated. The strongest evidence states: "
                f"{strongest['text']}"
            )
        else:
            summary = (
                f"{len(sources)} source(s) from {len(domains)} domain(s) were "
                f"read, but no claim was corroborated by two independent "
                f"domains. The highest-scoring extracted claim is: "
                f"{strongest['text']}"
            )
    else:
        summary = (
            "No usable claims could be extracted from the readable sources. "
            "Treat this research as incomplete.")

    conclusions: List[Dict[str, Any]] = []
    if key_claims:
        strongest = key_claims[0]
        conclusions.append({
            "id": "conclusion-1",
            "kind": "conclusion",
            "text": (
                "The best-supported finding in the collected evidence states: "
                + strongest.text),
            "source_ids": list(strongest.support_source_ids
                               or [strongest.source_id]),
            "support_count": len(strongest.support_domains
                                 or [strongest.source_domain]),
            "confidence": ("cross-referenced"
                           if len(strongest.support_domains) >= 2
                           else "single-source"),
            "theme": strongest.theme,
            "note": "",
        })
    if contradictions:
        conclusions.append({
            "id": "conclusion-2",
            "kind": "conclusion",
            "text": (
                "The sources are not fully consistent; material disagreements "
                "should be treated as unresolved rather than averaged away."),
            "source_ids": sorted({
                source_id for item in contradictions
                for source_id in item.get("source_ids", [])
            }),
            "support_count": len(contradictions),
            "confidence": "conflicting",
            "theme": "Conflicting evidence",
            "note": "",
        })
    conclusions.append({
        "id": f"conclusion-{len(conclusions) + 1}",
        "kind": "conclusion",
        "text": (
            f"Overall confidence is {confidence['level']}. "
            + confidence["reason"].capitalize() + "."),
        "source_ids": [source.id for source in sources],
        "support_count": len(sources),
        "confidence": confidence["level"],
        "theme": "Confidence",
        "note": "Confidence is based on source breadth, corroboration and "
                "heuristic credibility, not on certainty about the topic.",
    })

    limitations: List[str] = []
    if len(sources) < 3:
        limitations.append(
            f"Only {len(sources)} source(s) could be read; the evidence base "
            "is thin.")
    if len(domains) < 2:
        limitations.append(
            "All readable sources share one domain, so cross-domain "
            "independence is limited.")
    if not any(len(claim.support_domains) >= 2 for claim in claims):
        limitations.append(
            "No extracted claim was corroborated by two independent domains.")
    limitations.extend(f"Search issue: {message}" for message in
                       search_errors[:3])
    for failure in fetch_failures[:6]:
        limitations.append(
            f"Could not read {failure.get('url', '')}: "
            f"{failure.get('message', 'fetch failed')}")
    if duplicate_pages:
        limitations.append(
            f"Excluded {len(duplicate_pages)} duplicate or near-duplicate "
            "page(s) from the corroboration set.")
    if len(sources) < max_sources:
        limitations.append(
            f"Requested up to {max_sources} sources; {len(sources)} could be "
            "read.")
    limitations.extend([
        "Credibility scores are heuristic signals based on domain suffix, "
        "path, topic match and extracted text; they are not endorsements.",
        "Page text is extracted automatically and may omit surrounding "
        "context. JavaScript-only or paywalled content was not accessed.",
        "Statements marked single-source or unverified should not be treated "
        "as established facts.",
    ])

    sections = [
        {"id": "key-findings", "heading": "Key findings",
         "items": key_findings},
        {"id": "supporting-evidence", "heading": "Supporting evidence",
         "items": evidence},
        {"id": "statistics", "heading": "Data points and statistics",
         "items": statistics},
        {"id": "contradictions",
         "heading": "Contradictions and conflicting evidence",
         "items": contradictions},
        {"id": "uncertainty",
         "heading": "Uncertainty and unverified information",
         "items": uncertainties},
        {"id": "conclusions", "heading": "Conclusions",
         "items": conclusions},
    ]

    report: Dict[str, Any] = {
        "topic": topic,
        "focus": focus,
        "depth": depth,
        "generated_at": _now_iso(),
        "confidence": confidence,
        "summary": summary,
        "key_findings": key_findings,
        "sections": sections,
        "statistics": statistics,
        "contradictions": contradictions,
        "uncertainties": uncertainties,
        "conclusions": conclusions,
        "limitations": limitations,
        "sources": [_source_dict(source) for source in sources],
        "methodology": {
            "queries": list(queries),
            "depth": depth,
            "requested_sources": max_sources,
            "candidates_discovered": len(candidates),
            "pages_attempted": pages_attempted,
            "pages_read": len(sources),
            "domains_read": domains,
            "duplicate_pages_excluded": len(duplicate_pages),
            "search_issues": list(search_errors),
            "elapsed_seconds": round(elapsed, 2),
        },
    }
    report["report_markdown"] = _markdown_report(report)
    report["prose"] = _plain_prose(report)
    return report


# ── orchestration ────────────────────────────────────────────────────────────
def _merge_candidates(
    existing: List[_Candidate], rows: Iterable[Dict[str, Any]], query: str
) -> int:
    """Merge search rows in place. Returns the number of new candidates."""
    by_canonical = {item.canonical: item for item in existing}
    added = 0
    for row in rows:
        url = str(row.get("url") or "").strip()
        canonical = _canonical_url(url)
        if not canonical:
            continue
        if canonical in by_canonical:
            current = by_canonical[canonical]
            if len(str(row.get("snippet") or "")) > len(current.snippet):
                current.snippet = _clean(row.get("snippet"))
            if row.get("date") and not current.date:
                current.date = _clean(row.get("date"))
            continue
        candidate = _Candidate(
            url=url,
            canonical=canonical,
            title=_clean(row.get("title")) or _domain_of(url) or "Untitled source",
            snippet=_clean(row.get("snippet")),
            date=_clean(row.get("date")),
            provider=_clean(row.get("provider")) or "web",
            query=query,
        )
        existing.append(candidate)
        by_canonical[canonical] = candidate
        added += 1
    return added


def _cancelled(service: ResearchService, job_id: str) -> bool:
    state = service.public_state()
    return (service.cancelled()
            or bool(job_id and state.get("job_id") != job_id)
            or state.get("status") == "cancelled")


def _cancel_outcome(service: ResearchService, job_id: str) -> Dict[str, Any]:
    state = service.public_state()
    message = (state.get("message") if state.get("status") == "cancelled"
               else "") or "Research was cancelled."
    return {
        "ok": False,
        "code": "CANCELLED",
        "message": message,
        "service": service,
    }


def _run_research(
    *,
    topic: str,
    focus: str,
    depth: str,
    max_sources: int,
    service: ResearchService,
    job_id: str = "",
) -> Dict[str, Any]:
    started = time.monotonic()
    deadline = started + RESEARCH_DEADLINE_SECONDS
    queries = _query_variants(topic, focus, depth)
    results_per_query = RESULTS_PER_QUERY.get(
        depth, RESULTS_PER_QUERY["deep"])
    topic_terms = set(_tokens(topic + " " + focus))

    if _cancelled(service, job_id):
        return _cancel_outcome(service, job_id)

    def note(**kwargs: Any) -> None:
        service.update(job_id=job_id, **kwargs)

    candidates: List[_Candidate] = []
    search_errors: List[str] = []

    for index, query in enumerate(queries):
        if _cancelled(service, job_id):
            return _cancel_outcome(service, job_id)
        if time.monotonic() >= deadline:
            search_errors.append(
                "The research time budget expired before every query ran.")
            break
        progress = min(28, 4 + int(24 * (index / max(1, len(queries)))))
        note(
            phase="Searching",
            message=f"Searching {index + 1}/{len(queries)}: {query}",
            progress=progress,
            sources_found=len(candidates),
        )
        try:
            result = search_web(
                query, results_per_query,
                cancel_event=service.cancel_event)
        except Exception as exc:  # noqa: BLE001 - one query cannot stop research
            if _cancelled(service, job_id):
                return _cancel_outcome(service, job_id)
            message = f"Search failed unexpectedly: {type(exc).__name__}: {exc}"
            if message not in search_errors:
                search_errors.append(message)
            note(warning=message)
            continue
        if _cancelled(service, job_id):
            return _cancel_outcome(service, job_id)
        if result.get("error", {}).get("code") == "CANCELLED":
            return _cancel_outcome(service, job_id)
        if result.get("success"):
            _merge_candidates(candidates, result.get("results") or [], query)
            note(
                sources_found=len(candidates),
                progress=min(29, progress + 1),
            )
        else:
            error = result.get("error") or {}
            message = _clean(error.get("message")) or "Search failed."
            if message not in search_errors:
                search_errors.append(message)
            note(warning=f"Search issue: {message}")

    if _cancelled(service, job_id):
        return _cancel_outcome(service, job_id)

    for candidate in candidates:
        candidate.score = _candidate_score(candidate, topic_terms)
    candidates.sort(key=lambda item: (-item.score, item.url.casefold()))

    if not candidates:
        message = (
            "Deep research could not find any usable search results. "
            + (" ".join(search_errors[:2]) if search_errors else
               "The search providers returned no results."))
        service.fail(message, job_id=job_id)
        return {
            "ok": False,
            "code": "NO_SEARCH_RESULTS",
            "message": message,
            "service": service,
        }

    note(
        phase="Ranking sources",
        message=(
            f"Ranking {len(candidates)} candidate pages by relevance, "
            "authority signals and source diversity."),
        progress=32,
        sources_found=len(candidates),
    )

    selected = _select_candidates(
        candidates,
        min(MAX_CANDIDATES, max(max_sources + 7, 10)),
    )
    note(
        phase="Reading sources",
        message=f"Reading up to {len(selected)} candidate pages…",
        progress=36,
        sources_found=len(candidates),
    )

    sources: List[_Source] = []
    fetch_failures: List[Dict[str, str]] = []
    duplicate_pages: List[str] = []
    attempted = 0

    for offset in range(0, len(selected), FETCH_BATCH):
        if _cancelled(service, job_id):
            return _cancel_outcome(service, job_id)
        if time.monotonic() >= deadline:
            fetch_failures.append({
                "url": "",
                "message": "The research time budget expired before every "
                           "candidate page could be read.",
            })
            break
        if len(sources) >= max_sources:
            break
        batch = selected[offset:offset + FETCH_BATCH]
        attempted += len(batch)
        note(
            phase="Reading sources",
            message=(
                f"Fetching pages {offset + 1}–{offset + len(batch)} "
                f"({len(sources)} readable so far)…"),
            progress=min(82, 38 + int(44 * (attempted / max(1, len(selected))))),
            sources_found=len(candidates),
            sources_read=len(sources),
        )
        try:
            payload = fetch_pages(
                [item.url for item in batch],
                cancel_event=service.cancel_event)
        except Exception as exc:  # noqa: BLE001 - partial results still count
            if _cancelled(service, job_id):
                return _cancel_outcome(service, job_id)
            for candidate in batch:
                fetch_failures.append({
                    "url": candidate.url,
                    "message": f"Fetch failed unexpectedly: "
                               f"{type(exc).__name__}: {exc}",
                })
            continue
        rows = payload.get("results") or []
        if _cancelled(service, job_id) or payload.get("cancelled"):
            return _cancel_outcome(service, job_id)
        if len(rows) < len(batch):
            for candidate in batch[len(rows):]:
                fetch_failures.append({
                    "url": candidate.url,
                    "message": "The fetch layer returned no result for this URL.",
                })
        for candidate, row in zip(batch, rows):
            if _cancelled(service, job_id):
                return _cancel_outcome(service, job_id)
            if not row.get("success"):
                error = row.get("error") or {}
                fetch_failures.append({
                    "url": candidate.url,
                    "message": _clean(error.get("message"))
                    or "The page could not be fetched.",
                })
                continue
            content = str(row.get("content") or "")
            if len(content) < 200:
                fetch_failures.append({
                    "url": candidate.url,
                    "message": "The page had too little readable text.",
                })
                continue
            if any(_near_duplicate(content, source.content)
                   for source in sources):
                duplicate_pages.append(candidate.url)
                continue
            final_url = str(row.get("final_url") or candidate.url)
            source = _Source(
                id=len(sources) + 1,
                url=candidate.url,
                final_url=final_url,
                title=_clean(row.get("title")) or candidate.title,
                domain=_domain_of(final_url),
                content=content,
                retrieved_at=_clean(row.get("retrieved_at")) or _now_iso(),
                published=candidate.date,
                snippet=candidate.snippet,
                credibility_score=0.0,
                credibility_label="low",
            )
            score, label, reasons = _source_credibility(source, topic_terms)
            source.credibility_score = score
            source.credibility_label = label
            source.credibility_reasons = reasons
            sources.append(source)
            if len(sources) >= max_sources:
                break
        note(
            phase="Reading sources",
            message=(
                f"{len(sources)} readable source(s); "
                f"{len(fetch_failures)} page(s) unavailable."),
            progress=min(84, 40 + int(44 * (attempted / max(1, len(selected))))),
            sources_found=len(candidates),
            sources_read=len(sources),
        )

    if _cancelled(service, job_id):
        return _cancel_outcome(service, job_id)

    if not sources:
        message = (
            "Search discovered candidate pages, but none could be read. "
            "The research report was not created because there is no "
            "evidence to report.")
        service.fail(message, job_id=job_id)
        return {
            "ok": False,
            "code": "NO_READABLE_SOURCES",
            "message": message,
            "service": service,
        }

    note(
        phase="Analysing evidence",
        message=(
            f"Cross-referencing claims across {len(sources)} sources and "
            f"{len({source.domain for source in sources})} domains…"),
        progress=88,
        sources_found=len(candidates),
        sources_read=len(sources),
    )
    elapsed = time.monotonic() - started
    report = _build_report(
        topic=topic,
        focus=focus,
        depth=depth,
        queries=queries,
        sources=sources,
        candidates=candidates,
        search_errors=search_errors,
        fetch_failures=fetch_failures,
        duplicate_pages=duplicate_pages,
        elapsed=elapsed,
        max_sources=max_sources,
        pages_attempted=attempted,
    )
    if _cancelled(service, job_id):
        return _cancel_outcome(service, job_id)
    note(
        phase="Writing report",
        message="Organising findings, statistics, conflicts and sources…",
        progress=96,
        sources_found=len(candidates),
        sources_read=len(sources),
    )
    if not service.complete(report, job_id=job_id):
        return _cancel_outcome(service, job_id)
    return {
        "ok": True,
        "report": report,
        "search_errors": search_errors,
        "fetch_failures": fetch_failures,
        "duplicate_pages": duplicate_pages,
        "elapsed": elapsed,
        "service": service,
    }


def _failure_result(
    code: str,
    message: str,
    *,
    service: ResearchService,
    job_id: Optional[str] = None,
    details: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    if service.public_state().get("status") != "error":
        service.fail(message, job_id=job_id)
    return {
        "success": False,
        "operation": "research",
        "job_id": service.public_state().get("job_id", ""),
        "error": {"code": code, "message": message},
        "details": list(details or []),
        "warnings": [],
        "untrusted_content": True,
    }


def _cancelled_result(
    service: ResearchService,
    job_id: str,
    message: str,
) -> Dict[str, Any]:
    return {
        "success": False,
        "operation": "research",
        "job_id": job_id,
        "cancelled": True,
        "status": "cancelled",
        "error": {"code": "CANCELLED", "message": message},
        "details": [],
        "warnings": [],
        "untrusted_content": True,
    }


# ── tool entry point ─────────────────────────────────────────────────────────
def _save_report(path_text: str, markdown: str) -> Dict[str, Any]:
    """Write a completed report from memory, then verify the exact bytes."""
    requested = str(path_text or "").strip()
    if not requested:
        return {"success": False, "requested": False,
                "error": "No save path was supplied."}
    if not markdown:
        return {"success": False, "requested": True,
                "error": "The report has no markdown content to save."}
    module = get_tool_module("file_control")
    if module is None:
        return {"success": False, "requested": True,
                "error": "The file-control tool is unavailable."}
    path, error = module._resolve_path(requested)  # noqa: SLF001
    if error:
        return {"success": False, "requested": True,
                "error": str(error.get("error") or "Invalid save path.")}
    if os.path.lexists(path):
        return {"success": False, "requested": True, "path": path,
                "error": ("That file already exists. Choose a new file name "
                          "so the research report is not overwritten.")}
    print(f"[Research] save path={path} content_length={len(markdown)}")
    result = module.file_control(
        "write", source_path=requested, content=markdown)
    if result.get("approval_required"):
        return {"success": False, "requested": True, "path": path,
                "error": "The save path requires approval; choose a new file."}
    if not result.get("success"):
        return {"success": False, "requested": True, "path": path,
                "error": str(result.get("error") or "The report was not saved.")}
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            actual = handle.read()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "requested": True, "path": path,
                "error": f"Saved file could not be verified: {exc}"}
    if actual != markdown:
        print(f"[Research] save verify FAILED path={path} "
              f"expected={len(markdown)} actual={len(actual)}")
        return {"success": False, "requested": True, "path": path,
                "error": "Saved file did not match the report content."}
    print(f"[Research] save verified path={path} chars={len(actual)}")
    return {"success": True, "requested": True, "path": path,
            "characters": len(markdown), "verified": True}


def research(
    topic: str,
    max_sources: Any = 0,
    depth: str = "deep",
    focus: str = "",
    save_path: str = "",
) -> Dict[str, Any]:
    """Run deep, multi-source web research and open the report panel.

    ``topic`` is the research question. ``max_sources`` bounds readable pages
    (default depends on depth; maximum 20). ``depth`` is ``standard``, ``deep``
    or ``exhaustive``. ``focus`` is an optional extra angle to search for.
    ``save_path`` optionally writes the completed markdown report directly from
    memory to a new file (the model never has to copy report text into another
    tool call).
    """
    text = _clean(topic)
    if not text:
        return {
            "success": False,
            "operation": "research",
            "error": {"code": "INVALID_ARGUMENT",
                      "message": "A research topic or question is required."},
            "warnings": [],
        }
    if len(text) > MAX_TOPIC_CHARS:
        return {
            "success": False,
            "operation": "research",
            "error": {
                "code": "INVALID_ARGUMENT",
                "message": f"The research topic is longer than "
                           f"{MAX_TOPIC_CHARS} characters.",
            },
            "warnings": [],
        }
    angle = _clean(focus)[:MAX_FOCUS_CHARS]
    mode = _clean(depth).casefold() or "deep"
    if mode not in DEFAULT_SOURCES:
        mode = "deep"
    try:
        limit = int(float(max_sources or DEFAULT_SOURCES[mode]))
    except (TypeError, ValueError):
        limit = DEFAULT_SOURCES[mode]
    limit = max(3, min(MAX_SOURCES_CAP, limit))

    service = get_service()
    job_id = service.begin(text, mode, limit)
    if job_id is None:
        state = service.public_state()
        return {
            "success": False,
            "operation": "research",
            "job_id": state.get("job_id", ""),
            "error": {
                "code": "RESEARCH_ALREADY_RUNNING",
                "message": (
                    "A deep research job is already running. Wait for its "
                    "report or let it finish before starting another."),
            },
            "warnings": [],
        }

    try:
        outcome = _run_research(
            topic=text,
            focus=angle,
            depth=mode,
            max_sources=limit,
            service=service,
            job_id=job_id,
        )
    except Exception as exc:  # noqa: BLE001 - a tool must answer, not crash
        if _cancelled(service, job_id):
            return _cancelled_result(
                service, job_id, "The research was cancelled.")
        traceback.print_exc()
        message = (
            "Deep research failed unexpectedly. No report or sources were "
            f"fabricated. ({type(exc).__name__}: {exc})")
        return _failure_result(
            "RESEARCH_FAILED", message, service=service, job_id=job_id)

    if outcome.get("ok") is False and (
            outcome.get("code") == "CANCELLED"
            or service.public_state().get("status") == "cancelled"):
        return _cancelled_result(
            service, job_id,
            str(outcome.get("message") or "The research was cancelled."))

    if not outcome.get("ok"):
        return _failure_result(
            str(outcome.get("code") or "RESEARCH_FAILED"),
            str(outcome.get("message") or "Deep research failed."),
            service=service,
            job_id=job_id,
        )

    report = outcome["report"]
    state = service.public_state()
    if state.get("status") == "cancelled" or state.get("job_id") != job_id:
        return _cancelled_result(
            service, job_id, "The research was cancelled.")
    result = {
        "success": True,
        "operation": "research",
        "job_id": job_id,
        "topic": report["topic"],
        "focus": report.get("focus", ""),
        "status": "complete",
        "confidence": report["confidence"]["level"],
        "confidence_reason": report["confidence"]["reason"],
        "summary": report["summary"],
        "key_findings": report["key_findings"],
        "statistics": report["statistics"][:5],
        "contradictions": report["contradictions"][:3],
        "uncertainties": report["uncertainties"][:5],
        "source_count": len(report["sources"]),
        "domain_count": len({
            source["domain"] for source in report["sources"]}),
        "sources": report["sources"],
        "limitations": report["limitations"],
        "report_prose": report["prose"],
        "warnings": list(report["limitations"]),
        "untrusted_content": True,
        "elapsed_seconds": report["methodology"]["elapsed_seconds"],
    }
    if outcome.get("search_errors"):
        result["search_errors"] = list(outcome["search_errors"])
    if outcome.get("fetch_failures"):
        result["fetch_failures"] = list(outcome["fetch_failures"])
    if outcome.get("duplicate_pages"):
        result["duplicate_pages"] = list(outcome["duplicate_pages"])
    if str(save_path or "").strip():
        save = _save_report(save_path, report.get("report_markdown", ""))
        result["save"] = save
        result["saved"] = bool(save.get("success"))
        if save.get("success"):
            result["saved_path"] = save.get("path", "")
            result["message"] = (
                f"Research complete; report saved to {save.get('path')}.")
        else:
            result["save_error"] = str(save.get("error") or "")
            result["message"] = (
                "Research complete, but the report could not be saved: "
                + result["save_error"])
            result["warnings"].append(result["message"])
    return result

"""Deep research tool, synthesis pipeline and report-panel integration tests.

All network access is mocked at the research tool boundary. The tests verify
that real fetched text is used, sources are deduplicated, claims are
cross-referenced, conflicts and uncertainty are surfaced, and partial failures
produce an honest report rather than fabricated evidence.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from actions import research_tool, tool_calling
from web.events import EventHub
from web.runtime import AzeRuntime
from web.server import create_app

BASE = "http://127.0.0.1:8765"


PAGES = {
    "https://alpha.org/report": {
        "title": "Alpha benchmark report",
        "content": (
            "A benchmark study found that latency improved by 30 percent "
            "across 42 percent of tested voice systems. The data shows "
            "accuracy increased by 12 percent in noisy environments. "
            "Independent researchers reported broadly similar gains for "
            "accessibility use cases. The full dataset and methodology are "
            "published alongside the report, including sample sizes and test "
            "conditions for every measured system."
        ),
    },
    "https://beta.edu/paper": {
        "title": "Beta independent paper",
        "content": (
            "An independent benchmark study found latency improved by 30 "
            "percent in 42 percent of tested voice systems. The authors report "
            "accuracy gains of 12 percent in noisy environments. The evidence "
            "supports accessibility improvements and the paper describes its "
            "sampling method, limitations and replication materials in detail."
        ),
    },
    "https://gamma.gov/data": {
        "title": "Gamma public dataset",
        "content": (
            "A separate benchmark study found latency improved by 55 percent "
            "for a different vendor sample. The dataset reports that accuracy "
            "fell by 8 percent in high-noise conditions. Not all deployments "
            "replicate the improvement, and the methodology section explains "
            "that the sample covered a different hardware profile."
        ),
    },
    "https://delta.org/security": {
        "title": "Delta security analysis",
        "content": (
            "Security researchers suggest that voice authentication may be "
            "vulnerable to replay attacks. This finding is preliminary and only "
            "one laboratory has reported it. No independent replication was "
            "located. The authors describe their test setup and explicitly call "
            "for further work before drawing deployment conclusions."
        ),
    },
}


def _page_content(url: str) -> str:
    return str(PAGES[url]["content"])


def _search_result(query: str, limit: int, **kwargs) -> dict:
    return {
        "success": True,
        "operation": "search",
        "results": [
            {
                "title": page["title"],
                "url": url,
                "snippet": "voice model benchmark study data and analysis",
                "source": url.split("/")[2],
                "date": "2026-01-01",
                "provider": "web",
            }
            for url, page in PAGES.items()
        ],
    }


def _fetch_result(urls: list[str], **kwargs) -> dict:
    rows = []
    for url in urls:
        if url not in PAGES:
            rows.append({
                "success": False,
                "url": url,
                "error": {"code": "FETCH_FAILED",
                          "message": "The page could not be fetched."},
            })
            continue
        rows.append({
            "success": True,
            "operation": "fetch",
            "url": url,
            "final_url": url,
            "title": PAGES[url]["title"],
            "content": _page_content(url),
            "source": url.split("/")[2],
            "retrieved_at": "2026-01-01T00:00:00+00:00",
            "untrusted_content": True,
            "warnings": [],
        })
    return {"success": True, "operation": "fetch_multiple", "results": rows}


class _Weather:
    def get(self, force: bool = False) -> dict:
        return {"available": False}


class _Metrics:
    def snapshot(self) -> dict:
        return {"cpu": 0.0, "mem": 0.0, "net": 0.0, "gpu": 0.0, "tmp": 0.0}

    def stop(self) -> None:
        pass


class ResearchSynthesisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = research_tool.reset_service()
        self.events = []
        self.service.configure(self.events.append)
        self.search_patch = mock.patch.object(
            research_tool, "search_web", side_effect=_search_result)
        self.fetch_patch = mock.patch.object(
            research_tool, "fetch_pages", side_effect=_fetch_result)
        self.search_patch.start()
        self.fetch_patch.start()
        self.addCleanup(self.search_patch.stop)
        self.addCleanup(self.fetch_patch.stop)
        self.addCleanup(research_tool.reset_service)

    def test_full_report_uses_real_cross_referenced_evidence(self):
        result = research_tool.research(
            "voice model benchmark accuracy", max_sources=8, depth="standard")

        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "research")
        self.assertEqual(result["source_count"], 4)
        self.assertEqual(result["status"], "complete")
        self.assertIn(result["confidence"], ("medium", "high"))
        self.assertGreaterEqual(len(result["key_findings"]), 1)
        self.assertTrue(any(
            item["confidence"] == "cross-referenced"
            for item in result["key_findings"]))
        self.assertTrue(result["statistics"])
        self.assertTrue(result["contradictions"])
        self.assertTrue(result["uncertainties"])
        self.assertTrue(result["report_prose"])
        self.assertNotIn("\n- ", result["report_prose"])
        stored_report = self.service.public_state()["report"]
        self.assertTrue(stored_report["report_markdown"].startswith(
            "# Research report:"))
        self.assertIn("## Sources", stored_report["report_markdown"])
        self.assertNotIn("report_markdown", result)

        source_urls = {source["url"] for source in result["sources"]}
        self.assertEqual(source_urls, set(PAGES))
        for source in result["sources"]:
            self.assertIn(source["credibility"]["label"],
                          ("high", "medium", "low"))
            self.assertTrue(source["credibility"]["reasons"])

        report = self.service.public_state()["report"]
        self.assertIsNotNone(report)
        headings = {section["heading"] for section in report["sections"]}
        self.assertIn("Key findings", headings)
        self.assertIn("Supporting evidence", headings)
        self.assertIn("Data points and statistics", headings)
        self.assertIn("Contradictions and conflicting evidence", headings)
        self.assertIn("Uncertainty and unverified information", headings)
        self.assertIn("Conclusions", headings)

        state = self.service.public_state()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["progress"], 100)
        self.assertTrue(state["open"])
        self.assertEqual(state["sources_read"], 4)
        self.assertTrue(state["events"])
        self.assertTrue(any(event["type"] == "research_state"
                            for event in self.events))
        self.assertEqual(state["topic"], "voice model benchmark accuracy")

    def test_research_report_saves_intact_to_a_new_file(self):
        target = Path(tempfile.mkdtemp(prefix="jarvis-report-")) / "report.md"
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        result = research_tool.research(
            "voice model benchmark accuracy", max_sources=8,
            depth="standard", save_path=str(target))
        self.assertTrue(result["success"])
        self.assertTrue(result["saved"])
        self.assertTrue(result["save"]["verified"])
        expected = self.service.public_state()["report"]["report_markdown"]
        self.assertTrue(expected)
        self.assertEqual(target.read_text(encoding="utf-8"), expected)
        self.assertIn("## Sources", target.read_text(encoding="utf-8"))

    def test_research_save_never_overwrites_an_existing_file(self):
        target = Path(tempfile.mkdtemp(prefix="jarvis-report-")) / "report.md"
        target.write_text("keep this", encoding="utf-8")
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        result = research_tool.research(
            "voice model benchmark accuracy", max_sources=8,
            depth="standard", save_path=str(target))
        self.assertTrue(result["success"])
        self.assertFalse(result["saved"])
        self.assertIn("already exists", result["save_error"].casefold())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep this")

    def test_search_failure_returns_error_and_no_report(self):
        with mock.patch.object(
                research_tool, "search_web",
                return_value={"success": False, "error": {
                    "code": "SEARCH_FAILED",
                    "message": "The search provider did not answer."}}):
            result = research_tool.research("unfindable topic", depth="standard")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "NO_SEARCH_RESULTS")
        state = self.service.public_state()
        self.assertEqual(state["status"], "error")
        self.assertIsNone(state["report"])
        self.assertIn("search provider", state["error"].casefold())

    def test_partial_fetch_failure_still_produces_report(self):
        def partial_fetch(urls, **kwargs):
            rows = _fetch_result(urls)["results"]
            for row in rows:
                if "gamma" in row["url"]:
                    row["success"] = False
                    row["error"] = {
                        "code": "HTTP_403",
                        "message": "The page refused automated access.",
                    }
            return {"success": True, "results": rows}

        with mock.patch.object(
                research_tool, "fetch_pages", side_effect=partial_fetch):
            result = research_tool.research(
                "voice model benchmark accuracy", max_sources=8,
                depth="standard")

        self.assertTrue(result["success"])
        self.assertEqual(result["source_count"], 3)
        self.assertTrue(any("refused automated access" in item
                            for item in result["limitations"]))
        self.assertTrue(result["report_prose"])

    def test_duplicate_urls_and_near_duplicate_pages_are_excluded(self):
        duplicate_pages = {
            "https://alpha.org/report": {
                "title": "Alpha report",
                "content": _page_content("https://alpha.org/report"),
            },
            "https://copy.example/report": {
                "title": "Alpha report copy",
                "content": _page_content("https://alpha.org/report"),
            },
            "https://beta.edu/paper": {
                "title": "Beta paper",
                "content": _page_content("https://beta.edu/paper"),
            },
        }

        def duplicate_search(query, limit, **kwargs):
            return {
                "success": True,
                "results": [
                    {"title": page["title"], "url": url,
                     "snippet": "voice model benchmark study",
                     "date": "2026-01-01", "provider": "web"}
                    for url, page in duplicate_pages.items()
                ] + [
                    {"title": page["title"], "url": url,
                     "snippet": "voice model benchmark study",
                     "date": "2026-01-01", "provider": "web"}
                    for url, page in duplicate_pages.items()
                    if "alpha" in url
                ],
            }

        def duplicate_fetch(urls, **kwargs):
            rows = []
            for url in urls:
                page = duplicate_pages[url]
                rows.append({
                    "success": True, "url": url, "final_url": url,
                    "title": page["title"], "content": page["content"],
                    "retrieved_at": "2026-01-01T00:00:00+00:00",
                })
            return {"success": True, "results": rows}

        with mock.patch.object(
                research_tool, "search_web", side_effect=duplicate_search), \
             mock.patch.object(
                research_tool, "fetch_pages", side_effect=duplicate_fetch):
            result = research_tool.research(
                "voice model benchmark accuracy", max_sources=8,
                depth="standard")

        self.assertTrue(result["success"])
        self.assertEqual(result["source_count"], 2)
        self.assertTrue(any("duplicate" in item.casefold()
                            for item in result["limitations"]))

    def test_tool_is_registered_with_the_expected_schema(self):
        info = tool_calling.TOOL_REGISTRY["research"]
        self.assertTrue(callable(info["func"]))
        self.assertEqual(info["parameters"]["required"], ["topic"])
        self.assertEqual(info["parameters"]["type"], "OBJECT")
        self.assertIn("Research report", info["description"])
        declarations = {
            item["name"]: item
            for item in tool_calling.get_tool_declarations()
        }
        self.assertIn("research", declarations)

    def test_research_blocks_a_second_concurrent_job(self):
        self.service.begin("already running", "deep", 12)
        result = research_tool.research("another topic")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"],
                         "RESEARCH_ALREADY_RUNNING")

    def test_begin_logs_and_publishes_the_report_panel_state(self):
        logs = []
        self.service.configure(
            self.events.append,
            log=logs.append,
        )
        job_id = self.service.begin("Panel test", "deep", 8)
        self.assertTrue(job_id)
        self.assertTrue(any("Deep research started" in line for line in logs))
        state = self.service.public_state()
        self.assertTrue(state["open"])
        self.assertEqual(state["status"], "running")


class ResearchCancellationTests(unittest.TestCase):
    """Cooperative cancellation must stop work without looking like failure."""

    def setUp(self) -> None:
        self.service = research_tool.reset_service()
        self.events: list[dict] = []
        self.service.configure(self.events.append)
        self.addCleanup(research_tool.reset_service)

    def test_cancellation_before_execution_prevents_any_work(self):
        job_id = self.service.begin("cancelled topic", "standard", 5)
        self.assertTrue(self.service.cancel("stop before start"))

        searches: list[tuple] = []
        with mock.patch.object(
                research_tool, "search_web",
                side_effect=lambda *args, **kwargs:
                    searches.append(args) or {"success": True,
                                              "results": []}), \
             mock.patch.object(
                research_tool, "fetch_pages",
                side_effect=AssertionError("fetch must not run")):
            outcome = research_tool._run_research(
                topic="cancelled topic", focus="", depth="standard",
                max_sources=5, service=self.service, job_id=job_id)

        self.assertEqual(outcome["code"], "CANCELLED")
        self.assertEqual(searches, [])
        state = self.service.public_state()
        self.assertEqual(state["status"], "cancelled")
        self.assertIsNone(state["report"])

    def test_cancellation_between_searches_stops_further_queries(self):
        searches: list[str] = []

        def search(query, limit, **kwargs):
            searches.append(query)
            self.service.cancel("stop requested")
            return _search_result(query, limit)

        with mock.patch.object(
                research_tool, "search_web", side_effect=search), \
             mock.patch.object(
                research_tool, "fetch_pages",
                side_effect=AssertionError("fetch must not run")):
            result = research_tool.research(
                "voice model benchmark accuracy", depth="standard")

        self.assertTrue(result["cancelled"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error"]["code"], "CANCELLED")
        self.assertEqual(len(searches), 1)
        state = self.service.public_state()
        self.assertEqual(state["status"], "cancelled")
        self.assertNotEqual(state["status"], "error")
        self.assertIsNone(state["report"])
        self.assertTrue(state["cancelled"])
        self.assertFalse(any(
            event.get("state", {}).get("status") == "complete"
            for event in self.events))

    def test_cancellation_prevents_additional_fetch_batches(self):
        fetches: list[list[str]] = []

        def fetch(urls, **kwargs):
            fetches.append(list(urls))
            self.service.cancel("stop during fetch")
            return _fetch_result(urls)

        with mock.patch.object(
                research_tool, "search_web", side_effect=_search_result), \
             mock.patch.object(
                research_tool, "fetch_pages", side_effect=fetch), \
             mock.patch.object(research_tool, "FETCH_BATCH", 2):
            result = research_tool.research(
                "voice model benchmark accuracy", max_sources=8,
                depth="standard")

        self.assertTrue(result["cancelled"])
        self.assertEqual(len(fetches), 1)
        self.assertIsNone(self.service.public_state()["report"])

    def test_late_results_from_a_cancelled_job_are_ignored(self):
        job_id = self.service.begin("late results", "standard", 5)
        self.assertTrue(self.service.cancel("stopped"))
        late_report = {"sources": [], "topic": "late results"}

        self.assertFalse(self.service.complete(late_report, job_id=job_id))
        self.assertFalse(self.service.fail("late failure", job_id=job_id))
        self.service.update(message="late progress", job_id=job_id)

        state = self.service.public_state()
        self.assertEqual(state["status"], "cancelled")
        self.assertIsNone(state["report"])
        self.assertEqual(state["message"], "stopped")

    def test_late_results_from_a_superseded_job_are_ignored(self):
        first = self.service.begin("first", "standard", 5)
        self.assertTrue(self.service.cancel("stopped"))
        second = self.service.begin("second", "standard", 5)
        self.assertNotEqual(first, second)

        self.assertFalse(self.service.complete(
            {"sources": [], "topic": "first"}, job_id=first))
        state = self.service.public_state()
        self.assertEqual(state["job_id"], second)
        self.assertEqual(state["status"], "running")
        self.assertIsNone(state["report"])


class ResearchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = research_tool.reset_service()
        self.addCleanup(research_tool.reset_service)
        self.runtime = AzeRuntime(
            hub=EventHub(), start_session=False,
            weather_service=_Weather(), metrics=_Metrics())
        self.client = TestClient(
            create_app(self.runtime, frontend_dir=Path(tempfile.gettempdir())
                       / "jarvis-no-dist"),
            base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_bootstrap_and_api_share_live_research_state(self):
        self.assertEqual(self.service.public_state()["status"], "idle")
        bootstrap = self.client.get("/api/bootstrap").json()
        self.assertEqual(bootstrap["research"]["status"], "idle")

        self.service.begin("A live research topic", "deep", 12)
        runtime_state = self.client.get("/api/research").json()
        self.assertEqual(runtime_state["status"], "running")
        self.assertEqual(runtime_state["topic"], "A live research topic")

        self.service.complete({
            "topic": "A live research topic",
            "sources": [],
            "confidence": {"level": "low", "reason": "test"},
        })
        final = self.client.get("/api/research").json()
        self.assertEqual(final["status"], "complete")
        self.assertEqual(final["progress"], 100)
        self.assertIsNotNone(final["report"])

    def test_websocket_receives_progressive_research_state(self):
        with self.client.websocket_connect(
            "/ws", headers={"origin": BASE, "host": "127.0.0.1:8765"}
        ) as socket:
            snapshot = socket.receive_json()
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertIn("research", snapshot["data"])

            self.service.begin("Streaming topic", "deep", 8)
            event = None
            for _ in range(4):
                candidate = socket.receive_json()
                if candidate.get("type") == "research_state":
                    event = candidate
                    break
            self.assertIsNotNone(event)
            self.assertEqual(event["state"]["status"], "running")
            self.assertTrue(event["state"]["open"])


if __name__ == "__main__":
    unittest.main()

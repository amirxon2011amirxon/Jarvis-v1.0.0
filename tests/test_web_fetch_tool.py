"""web_fetch tool: fetching, extraction, search plumbing and SSRF guards.

All network I/O is mocked with ``httpx.MockTransport`` and the search provider
is patched, so the suite never depends on DNS or a live website.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import sys
import unittest
from unittest import mock

import httpx

import actions
from actions import tool_calling
from actions import web_fetch_tool as w

PUBLIC_IP = "93.184.216.34"


def fake_resolve(host: str, port: int):
    """Real literal/local checks, public-IP stand-in for public names."""
    name = host.strip().rstrip(".").lower()
    if (name in w._BLOCKED_HOSTS
            or any(name.endswith(s) for s in w._BLOCKED_HOST_SUFFIXES)
            or ("." not in name and ":" not in name)):
        return _REAL_RESOLVE(host, port)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return [PUBLIC_IP], None, None
    reason = w._blocked_ip(host)
    if reason:
        return [], "BLOCKED_DESTINATION", f"'{host}' {reason}"
    return [host], None, None


_REAL_RESOLVE = w._resolve_host


class Base(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.routes = {}
        self.default = None

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            for prefix, response in self.routes.items():
                if request.url.path.startswith(prefix):
                    return response(request) if callable(response) else response
            if self.default is not None:
                return self.default(request)
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  content=b"<html><body>ok</body></html>")

        self.client = httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False)
        self.client_patch = mock.patch.object(w, "_client", self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.resolve_patch = mock.patch.object(
            w, "_resolve_host", side_effect=fake_resolve)
        self.resolve_patch.start()
        self.addCleanup(self.resolve_patch.stop)
        self.addCleanup(w._reset_client)
        w._reset_caches()

    def html(self, body: str, *, title: str = "Test Page") -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=(f"<html><head><title>{title}</title></head>"
                     f"<body><article><h1>{title}</h1><p>{body}</p>"
                     f"</article></body></html>").encode())


class RegistrationTests(unittest.TestCase):
    def test_module_and_registry(self):
        module = actions.get_tool_module("web_fetch")
        self.assertIsNotNone(module)
        info = tool_calling.TOOL_REGISTRY["web_fetch"]
        self.assertTrue(callable(info["func"]))
        self.assertEqual(info["parameters"]["type"], "OBJECT")
        self.assertEqual(info["parameters"]["required"], ["operation"])
        for name in ("operation", "query", "url", "urls", "max_results",
                     "max_pages"):
            self.assertIn(name, info["parameters"]["properties"])

    def test_declaration_is_valid_for_the_sdk(self):
        from google.genai import types

        declaration = next(
            item for item in tool_calling.get_tool_declarations()
            if item["name"] == "web_fetch")
        types.FunctionDeclaration(**declaration)


class FetchTests(Base):
    def test_repeated_fetch_is_served_from_cache(self):
        calls = {"n": 0}

        def counting(request):
            calls["n"] += 1
            return self.html("cached body")

        self.default = counting
        first = w.web_fetch("fetch", url="https://example.com/cached")
        second = w.web_fetch("fetch", url="https://example.com/cached")
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(1, calls["n"])

    def test_html_fetch_extracts_title_and_content(self):
        self.routes["/article"] = self.html("Readable body text.")
        result = w.web_fetch("fetch", url=f"https://example.com/article")
        self.assertTrue(result["success"])
        self.assertEqual("Test Page", result["title"])
        self.assertIn("Readable body text.", result["content"])
        self.assertEqual("example.com", result["source"])
        self.assertTrue(result["untrusted_content"])
        self.assertEqual(200, result["status_code"])
        self.assertTrue(result["retrieved_at"])

    def test_plain_text(self):
        self.routes["/notes.txt"] = httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"line one\nline two")
        result = w.web_fetch("fetch", url="https://example.com/notes.txt")
        self.assertTrue(result["success"])
        self.assertIn("line one", result["content"])

    def test_json_response_is_pretty_printed(self):
        self.routes["/data"] = httpx.Response(
            200, headers={"content-type": "application/json"},
            content=json.dumps({"a": 1}).encode())
        result = w.web_fetch("fetch", url="https://example.com/data")
        self.assertIn('"a": 1', result["content"])

    def test_http_404_is_a_clear_error(self):
        self.routes["/missing"] = httpx.Response(404)
        result = w.web_fetch("fetch", url="https://example.com/missing")
        self.assertFalse(result["success"])
        self.assertEqual("HTTP_ERROR", result["error"]["code"])

    def test_unsupported_content_type(self):
        self.routes["/file.pdf"] = httpx.Response(
            200, headers={"content-type": "application/pdf"},
            content=b"%PDF-1.7")
        result = w.web_fetch("fetch", url="https://example.com/file.pdf")
        self.assertFalse(result["success"])
        self.assertEqual("UNSUPPORTED_CONTENT_TYPE", result["error"]["code"])

    def test_timeout_after_one_retry(self):
        calls = {"n": 0}

        def boom(request):
            calls["n"] += 1
            raise httpx.ReadTimeout("timed out", request=request)

        self.default = boom
        result = w.web_fetch("fetch", url="https://example.com/slow")
        self.assertFalse(result["success"])
        self.assertEqual("TIMEOUT", result["error"]["code"])
        self.assertEqual(2, calls["n"])  # one retry, never a loop

    def test_oversized_body_is_capped_and_marked(self):
        self.routes["/big"] = httpx.Response(
            200, headers={"content-type": "text/plain"},
            content=b"x" * 5000)
        with mock.patch.object(w, "MAX_BYTES", 500):
            result = w.web_fetch("fetch", url="https://example.com/big")
        self.assertTrue(result["success"])
        self.assertTrue(result["truncated"])
        self.assertTrue(any("exceeded" in warning
                            for warning in result["warnings"]))

    def test_content_truncation_preserves_attribution(self):
        self.routes["/long"] = self.html("word " * 400)
        with mock.patch.object(w, "MAX_CHARS", 200):
            result = w.web_fetch("fetch", url="https://example.com/long")
        self.assertTrue(result["truncated"])
        self.assertEqual("https://example.com/long", result["url"])
        self.assertEqual("https://example.com/long", result["final_url"])
        self.assertTrue(any("truncated" in warning.lower()
                            for warning in result["warnings"]))

    def test_redirect_is_followed_and_final_url_kept(self):
        def start(request):
            return httpx.Response(302, headers={
                "location": "https://example.com/next"})

        self.routes["/start"] = start
        self.routes["/next"] = self.html("after redirect")
        result = w.web_fetch("fetch", url="https://example.com/start")
        self.assertTrue(result["success"])
        self.assertEqual("https://example.com/next", result["final_url"])
        # The second request went to the same validated host (pinned address).
        self.assertTrue(any(url.endswith("/next") for url in self.requests))

    def test_redirect_to_loopback_is_blocked(self):
        self.routes["/evil"] = lambda request: httpx.Response(
            302, headers={"location": "http://127.0.0.1:9000/admin"})
        result = w.web_fetch("fetch", url=f"https://example.com/evil")
        self.assertFalse(result["success"])
        self.assertEqual("BLOCKED_REDIRECT", result["error"]["code"])
        self.assertEqual(1, len(self.requests))  # the local URL was never hit

    def test_redirect_loop_detected(self):
        self.routes["/loop"] = lambda request: httpx.Response(
            302, headers={"location": "https://example.com/loop"})
        result = w.web_fetch("fetch", url="https://example.com/loop")
        self.assertFalse(result["success"])
        self.assertEqual("REDIRECT_LOOP", result["error"]["code"])

    def test_too_many_redirects(self):
        for index in range(1, 8):
            self.routes[f"/r{index}"] = (lambda target: lambda request:
                                         httpx.Response(
                                             302,
                                             headers={"location":
                                                      f"https://example.com/r{target}"})
                                         )(index + 1)
        result = w.web_fetch("fetch", url="https://example.com/r1")
        self.assertFalse(result["success"])
        self.assertEqual("TOO_MANY_REDIRECTS", result["error"]["code"])

    def test_https_to_http_downgrade_blocked(self):
        self.routes["/down"] = lambda request: httpx.Response(
            302, headers={"location": "http://example.com/plain"})
        result = w.web_fetch("fetch", url="https://example.com/down")
        self.assertEqual("BLOCKED_REDIRECT", result["error"]["code"])

    def test_dns_failure(self):
        with mock.patch.object(w, "_resolve_host",
                               return_value=([], "DNS_ERROR", "no such host")):
            result = w.web_fetch("fetch", url="https://nope.example.com/x")
        self.assertEqual("DNS_ERROR", result["error"]["code"])

    def test_empty_page_is_an_error_not_fake_content(self):
        self.routes["/empty"] = httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html></html>")
        result = w.web_fetch("fetch", url="https://example.com/empty")
        self.assertFalse(result["success"])
        self.assertEqual("EMPTY_CONTENT", result["error"]["code"])


class SecurityTests(Base):
    def test_loopback_is_rejected_before_any_request(self):
        result = w.web_fetch("fetch", url="http://127.0.0.1:8000/admin")
        self.assertFalse(result["success"])
        self.assertEqual("BLOCKED_DESTINATION", result["error"]["code"])
        self.assertEqual([], self.requests)

    def test_private_ipv4_is_rejected(self):
        result = w.web_fetch("fetch", url="http://192.168.1.10/router")
        self.assertEqual("BLOCKED_DESTINATION", result["error"]["code"])
        self.assertEqual([], self.requests)

    def test_ipv6_loopback_and_private(self):
        for url in ("http://[::1]/", "http://[fd00::1]/", "http://[fe80::1]/"):
            with self.subTest(url=url):
                result = w.web_fetch("fetch", url=url)
                self.assertEqual("BLOCKED_DESTINATION", result["error"]["code"])
        self.assertEqual([], self.requests)

    def test_credentials_in_url_are_rejected(self):
        result = w.web_fetch("fetch", url="https://user:pass@example.com/")
        self.assertEqual("INVALID_URL", result["error"]["code"])

    def test_non_http_schemes_are_rejected(self):
        for url in ("file:///C:/Windows/win.ini", "ftp://example.com/x",
                    "javascript:alert(1)"):
            with self.subTest(url=url):
                result = w.web_fetch("fetch", url=url)
                self.assertEqual("INVALID_URL", result["error"]["code"])

    def test_local_hostnames_are_rejected(self):
        for host in ("localhost", "printer.local", "intranet"):
            with self.subTest(host=host):
                result = w.web_fetch("fetch", url=f"http://{host}/")
                self.assertEqual("BLOCKED_DESTINATION",
                                 result["error"]["code"])

    def test_metadata_never_leaks_headers(self):
        self.routes["/a"] = self.html("body")
        result = w.web_fetch("fetch", url="https://example.com/a")
        text = json.dumps(result)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("Host:", text)


class MultiFetchTests(Base):
    def test_partial_success_keeps_good_results(self):
        self.routes["/good"] = self.html("good page")
        self.routes["/gone"] = httpx.Response(404)
        result = w.web_fetch("fetch_multiple", urls=[
            "https://example.com/good", "https://example.com/gone"])
        self.assertEqual(1, result["succeeded"])
        self.assertEqual(1, result["failed"])
        self.assertTrue(any(row.get("success") for row in result["results"]))
        failed = [row for row in result["results"] if not row.get("success")]
        self.assertEqual("HTTP_ERROR", failed[0]["error"]["code"])
        self.assertEqual("https://example.com/gone", failed[0]["url"])

    def test_all_failures_report_failure(self):
        self.routes["/gone"] = httpx.Response(404)
        result = w.web_fetch("fetch_multiple", urls=[
            "https://example.com/gone"])
        self.assertFalse(result["success"])
        self.assertEqual("ALL_FETCHES_FAILED", result["error"]["code"])

    def test_url_cap(self):
        urls = [f"https://example.com/{index}" for index in range(w.MAX_URLS + 1)]
        result = w.web_fetch("fetch_multiple", urls=urls)
        self.assertEqual("TOO_MANY_URLS", result["error"]["code"])


class SearchTests(Base):
    def setUp(self):
        super().setUp()
        self.results = [
            {"title": "Docs", "url": "https://docs.python.org/3/x.html",
             "snippet": "official", "source": "docs.python.org", "date": ""},
            {"title": "Blog", "url": "https://example.org/post",
             "snippet": "notes", "source": "example.org", "date": "2026-01-01"},
        ]
        # The ddgs route is optional and would make real network calls in the
        # threaded provider; tests opt into it explicitly.
        self.ddgs_off = mock.patch.object(
            w, "_ddgs_search",
            return_value=([], {"code": "SEARCH_FAILED",
                               "message": "disabled in tests"}))
        self.ddgs_off.start()
        self.addCleanup(self.ddgs_off.stop)

    def test_search_success_shape(self):
        with mock.patch.object(w, "_search_provider",
                               return_value=(self.results, None)):
            result = w.web_fetch("search", query="python docs", max_results=2)
        self.assertTrue(result["success"])
        self.assertEqual(2, result["count"])
        self.assertEqual("duckduckgo", result["provider"]["name"])
        self.assertEqual("docs.python.org", result["results"][0]["source"])

    def test_search_failure_is_reported(self):
        with mock.patch.object(w, "_search_provider",
                               return_value=([], {"code": "SEARCH_FAILED",
                                                  "message": "blocked"})):
            result = w.web_fetch("search", query="anything")
        self.assertFalse(result["success"])
        self.assertEqual("SEARCH_FAILED", result["error"]["code"])

    def test_repeated_search_is_cached(self):
        calls = {"n": 0}

        def provider(query, limit):
            calls["n"] += 1
            return self.results, None

        with mock.patch.object(w, "_search_provider", side_effect=provider):
            w.web_fetch("search", query="cached query")
            w.web_fetch("search", query="cached query")
        self.assertEqual(1, calls["n"])

    def test_lite_provider_parses_and_unwraps_links(self):
        self.routes["/lite"] = httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"""
            <table>
              <tr><td>
                <a rel="nofollow" class='result-link'
                   href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F%3Fq%3D1&amp;rut=x">
                  Docs page</a>
              </td></tr>
              <tr><td class='result-snippet'>official docs</td></tr>
            </table>""")
        result = w.web_fetch("search", query="docs", max_results=3)
        self.assertTrue(result["success"])
        self.assertEqual(1, result["count"])
        self.assertEqual("https://docs.python.org/3/?q=1",
                         result["results"][0]["url"])
        self.assertEqual("docs.python.org", result["results"][0]["source"])
        self.assertIn("official docs", result["results"][0]["snippet"])

    def test_bing_links_are_unwrapped(self):
        target = "https://docs.python.org/3/library/sqlite3.html"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        href = f"https://www.bing.com/ck/a?!&&p=x&u=a1{encoded}&ntb=1"
        self.assertEqual(w._unwrap_bing(href), target)

    def test_bing_provider_parses_results(self):
        target = "https://docs.python.org/3/"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        self.routes["/search"] = httpx.Response(
            200, headers={"content-type": "text/html"},
            content=("""<ol><li class="b_algo"><h2>
                <a href="https://www.bing.com/ck/a?u=a1{encoded}">Python docs</a>
                </h2><div class="b_caption"><p>Official documentation.</p></div>
                </li></ol>""").format(encoded=encoded).encode())
        results, error = w._bing_search("python", 3)
        self.assertIsNone(error)
        self.assertEqual(1, len(results))
        self.assertEqual(target, results[0]["url"])
        self.assertEqual("docs.python.org", results[0]["source"])
        self.assertIn("Official", results[0]["snippet"])

    def test_search_falls_back_to_ddgs(self):
        with mock.patch.object(w, "_ddg_lite_search",
                               return_value=([], {"code": "SEARCH_FAILED",
                                                  "message": "blocked"})), \
             mock.patch.object(w, "_ddg_html_search",
                               return_value=([], {"code": "SEARCH_FAILED",
                                                  "message": "blocked"})), \
             mock.patch.object(w, "_ddgs_search",
                               return_value=(self.results, None)):
            results, error = w._search_provider("q", 2)
        self.assertIsNone(error)
        self.assertEqual(2, len(results))

    def test_research_combines_search_and_pages(self):
        class BaseFetch:
            def _fetch_page(self, url):
                return {"success": True, "operation": "fetch", "url": url,
                        "final_url": url, "title": "T", "content": "body",
                        "source": "example.com", "warnings": [],
                        "untrusted_content": True}

        with mock.patch.object(w, "_search_provider",
                               return_value=(self.results, None)), \
             mock.patch.object(w, "_fetch_page",
                               side_effect=BaseFetch()._fetch_page):
            result = w.web_fetch("research", query="python", max_pages=2)
        self.assertEqual("research", result["operation"])
        self.assertEqual(2, result["succeeded"])
        self.assertEqual("https://docs.python.org/3/x.html",
                         result["results"][0]["url"])
        self.assertEqual(self.results, result["search_results"])

    def test_direct_fetch_does_not_need_search(self):
        # No provider call is involved in a fetch (search credentials/config
        # cannot affect it).
        with mock.patch.object(w, "_search_provider",
                               side_effect=AssertionError("search used")):
            result = w.web_fetch("fetch", url="http://127.0.0.1/")
        self.assertEqual("BLOCKED_DESTINATION", result["error"]["code"])


class ProviderUnitTests(unittest.TestCase):
    def test_missing_search_package_is_actionable(self):
        with mock.patch.dict(sys.modules, {"ddgs": None}):
            results, error = w._ddgs_search("query", 3)
        self.assertEqual([], results)
        self.assertEqual("SEARCH_UNAVAILABLE", error["code"])
        self.assertIn("ddgs", error["message"])


class ArgumentTests(unittest.TestCase):
    def test_unknown_operation(self):
        result = w.web_fetch("crawl")
        self.assertEqual("INVALID_ARGUMENT", result["error"]["code"])

    def test_fetch_requires_url(self):
        result = w.web_fetch("fetch")
        self.assertEqual("INVALID_URL", result["error"]["code"])

    def test_search_requires_query(self):
        result = w.web_fetch("search", query="   ")
        self.assertEqual("INVALID_ARGUMENT", result["error"]["code"])

    def test_fetch_multiple_requires_urls(self):
        result = w.web_fetch("fetch_multiple", urls=[])
        self.assertEqual("INVALID_ARGUMENT", result["error"]["code"])

    def test_execute_tool_dispatch(self):
        with mock.patch.object(w, "_search_provider",
                               return_value=([], {"code": "SEARCH_FAILED",
                                                  "message": "offline"})):
            result = tool_calling.execute_tool(
                "web_fetch", {"operation": "search", "query": "x"})
        self.assertEqual("SEARCH_FAILED", result["error"]["code"])


if __name__ == "__main__":
    unittest.main()

"""Tests for the open_web tool and its registration in Aze's tool registry.

Browser execution is always mocked: no test opens a real website.
Run from the project root with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions import open_web_tool, tool_calling


BRAVE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"


class _FakeProcess:
    """Minimal Popen stand-in; poll() reports the exit code (None = running)."""

    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class _BrowserMockMixin:
    def open_with_mock(self, url):
        with mock.patch.object(
                open_web_tool, "get_brave_path", return_value=BRAVE), \
             mock.patch.object(
                open_web_tool.subprocess, "Popen",
                return_value=_FakeProcess()) as popen:
            result = open_web_tool.open_web(url)
        return result, popen


class UrlValidationTests(unittest.TestCase):
    def assert_valid(self, url, expected=None):
        normalised, error = open_web_tool.validate_url(url)
        self.assertEqual(error, "", f"{url!r} should be valid: {error}")
        self.assertEqual(normalised, expected if expected is not None else url)

    def assert_invalid(self, url):
        normalised, error = open_web_tool.validate_url(url)
        self.assertEqual(normalised, "")
        self.assertTrue(error, f"{url!r} should be rejected")

    def test_known_website_url(self):
        self.assert_valid("https://claude.ai")

    def test_arbitrary_top_level_domains(self):
        for url in (
            "https://example.com",
            "https://example.uz",
            "https://example.dev",
            "https://example.net",
            "https://example.no",
            "https://example.ai",
            "https://example.co",
            "https://example.museum",
            "https://example.technology",
        ):
            with self.subTest(url=url):
                self.assert_valid(url)

    def test_complete_url_is_preserved(self):
        url = "https://example.com/docs/python/?q=asyncio&lang=en#install"
        self.assert_valid(url)

    def test_http_url(self):
        self.assert_valid("http://example.com/page")

    def test_local_dashboard(self):
        self.assert_valid("http://localhost:3080")
        self.assert_valid("http://127.0.0.1:3080/dashboard")

    def test_bare_host_gets_https(self):
        self.assert_valid("example.com/docs?x=1", "https://example.com/docs?x=1")
        self.assert_valid("localhost:3080", "https://localhost:3080")
        self.assert_valid("[::1]:3080", "https://[::1]:3080")

    def test_unsupported_schemes_are_rejected(self):
        for url in (
            "javascript:alert(1)",
            "file:///C:/Windows/System32/calc.exe",
            "data:text/html,<h1>hi</h1>",
            "ftp://example.com",
            "mailto:someone@example.com",
        ):
            with self.subTest(url=url):
                self.assert_invalid(url)

    def test_malformed_urls_are_rejected(self):
        for url in (
            "",
            "   ",
            "not a url",
            "https://",
            "https://exa mple.com",
            "https://example.com:99999",
            "https://example.com:0",
            "https://-bad.example",
            "https://bad-.example",
            "https://example..com",
            "https://example",
            "https://user:pass@example.com",
            "https://example.com\\path",
            "//example.com",
        ):
            with self.subTest(url=url):
                self.assert_invalid(url)

    def test_credentials_are_rejected(self):
        self.assert_invalid("https://user@example.com")


class BraveDiscoveryTests(unittest.TestCase):
    def test_brave_path_is_configurable(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / "brave.exe"
            fake.write_bytes(b"")
            with mock.patch.dict(os.environ, {"BRAVE_PATH": str(fake)}):
                self.assertEqual(
                    Path(open_web_tool.get_brave_path()).resolve(),
                    fake.resolve())

    def test_missing_brave_returns_empty_path(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "no-brave-here.exe"
            with mock.patch.object(
                    open_web_tool, "_candidate_paths",
                    return_value=[str(missing)]):
                self.assertEqual(open_web_tool.get_brave_path(), "")


class OpenWebTests(_BrowserMockMixin, unittest.TestCase):
    def test_known_website_opens(self):
        result, popen = self.open_with_mock("https://claude.ai")
        self.assertTrue(result["success"])
        self.assertEqual(result["url"], "https://claude.ai")
        self.assertEqual(result["opened"], "https://claude.ai")
        self.assertEqual(result["message"], "Website opened successfully.")
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [BRAVE, "https://claude.ai"])
        self.assertFalse(kwargs.get("shell", False))

    def test_ai_uz_dev_domains_launch(self):
        for url in ("https://example.ai", "https://example.uz",
                    "https://example.dev"):
            with self.subTest(url=url):
                result, popen = self.open_with_mock(url)
                self.assertTrue(result["success"])
                self.assertEqual(popen.call_args.args[0], [BRAVE, url])

    def test_complete_url_launches_unchanged(self):
        url = "https://example.com/docs/page?q=python&lang=en#install"
        result, popen = self.open_with_mock(url)
        self.assertTrue(result["success"])
        self.assertEqual(result["opened"], url)
        self.assertEqual(popen.call_args.args[0], [BRAVE, url])

    def test_http_and_local_urls_launch(self):
        for url in ("http://example.com/page", "http://localhost:3080"):
            with self.subTest(url=url):
                result, popen = self.open_with_mock(url)
                self.assertTrue(result["success"])
                self.assertEqual(popen.call_args.args[0], [BRAVE, url])

    def test_invalid_url_never_launches(self):
        with mock.patch.object(open_web_tool.subprocess, "Popen") as popen:
            result = open_web_tool.open_web("javascript:alert(1)")
        self.assertFalse(result["success"])
        self.assertEqual(result["url"], "javascript:alert(1)")
        self.assertEqual(result["message"], "Invalid or unsupported URL.")
        self.assertIn("error", result)
        popen.assert_not_called()

    def test_missing_brave_is_structured_error(self):
        with mock.patch.object(open_web_tool, "get_brave_path", return_value=""), \
             mock.patch.object(open_web_tool.subprocess, "Popen") as popen:
            result = open_web_tool.open_web("https://example.com")
        self.assertFalse(result["success"])
        self.assertIn("Brave executable was not found", result["error"])
        popen.assert_not_called()

    def test_launch_failure_is_structured_error(self):
        with mock.patch.object(
                open_web_tool, "get_brave_path", return_value=BRAVE), \
             mock.patch.object(
                open_web_tool.subprocess, "Popen",
                side_effect=OSError("permission denied")):
            result = open_web_tool.open_web("https://example.com")
        self.assertFalse(result["success"])
        self.assertFalse(result.get("opened"))
        self.assertIn("Could not launch Brave", result["error"])
        self.assertIn("permission denied", result["error"])

    def test_immediate_nonzero_exit_is_structured_error(self):
        with mock.patch.object(
                open_web_tool, "get_brave_path", return_value=BRAVE), \
             mock.patch.object(
                open_web_tool.subprocess, "Popen",
                return_value=_FakeProcess(returncode=1)):
            result = open_web_tool.open_web("https://example.com")
        self.assertFalse(result["success"])
        self.assertIn("exited immediately with code 1", result["error"])


class RegistryIntegrationTests(_BrowserMockMixin, unittest.TestCase):
    def test_tool_is_declared_for_the_llm(self):
        self.assertIn("open_web", tool_calling.get_tool_names())
        declarations = {d["name"]: d for d in tool_calling.get_tool_declarations()}
        self.assertIn("open_web", declarations)
        declaration = declarations["open_web"]
        self.assertTrue(declaration["description"])
        self.assertEqual(declaration["parameters"]["type"], "OBJECT")
        self.assertIn("url", declaration["parameters"]["properties"])
        self.assertEqual(declaration["parameters"]["required"], ["url"])

    def test_tool_is_callable_through_execute_tool(self):
        with mock.patch.object(
                open_web_tool, "get_brave_path", return_value=BRAVE), \
             mock.patch.object(
                open_web_tool.subprocess, "Popen",
                return_value=_FakeProcess()) as popen:
            result = tool_calling.execute_tool(
                "open_web", {"url": "https://huggingface.co"})
        self.assertTrue(result["success"])
        self.assertEqual(result["opened"], "https://huggingface.co")
        self.assertEqual(
            popen.call_args.args[0], [BRAVE, "https://huggingface.co"])

    def test_execution_errors_reach_the_llm(self):
        result = tool_calling.execute_tool(
            "open_web", {"url": "file:///C:/Windows/System32/calc.exe"})
        self.assertIn("error", result)
        self.assertFalse(result["success"])
        self.assertIn("Only http:// and https://", result["error"])

    def test_bad_arguments_are_reported(self):
        result = tool_calling.execute_tool("open_web", {"address": "https://example.com"})
        self.assertIn("error", result)
        self.assertIn("Bad arguments", result["error"])
        # The error names the declared parameters so the model can correct
        # itself instead of guessing another key.
        self.assertIn("expected: url", result["error"])


if __name__ == "__main__":
    unittest.main()

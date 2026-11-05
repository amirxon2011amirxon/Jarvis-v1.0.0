"""Tests for the app-first opening policy and fallback sequence.

The LLM makes the routing decision. These tests drive the same sequence the
prompt asks it to follow through Aze's real ``execute_tool`` pipeline, with all
application and browser launches mocked.
Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from actions import open_app_tool, open_web_tool, tool_calling


BRAVE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "core" / "prompt.txt"


class _FakeProcess:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


def entry(name):
    return {"name": name, "kind": "shortcut",
            "launch": rf"C:\Start Menu\{name}.lnk"}


@contextmanager
def mocked_openers(entries, launch_error=None):
    """Mock both tools' side effects and expose their call trackers."""
    with mock.patch.object(open_app_tool, "_IS_WIN", True), \
         mock.patch.object(
            open_app_tool, "_index", return_value=entries) as index, \
         mock.patch.object(
            open_app_tool, "_launch", side_effect=launch_error) as launch, \
         mock.patch.object(
            open_web_tool, "get_brave_path", return_value=BRAVE), \
         mock.patch.object(
            open_web_tool.subprocess, "Popen",
            return_value=_FakeProcess()) as popen:
        yield index, launch, popen


def simulate_llm_open(target, url, explicit_website=False):
    """The tool sequence the prompt asks the model to perform."""
    if explicit_website:
        return tool_calling.execute_tool("open_web", {"url": url})

    listed = tool_calling.execute_tool("list_apps", {})
    match = next(
        (name for name in listed.get("apps", [])
         if name.casefold() == target.casefold()),
        None,
    )
    if match is not None:
        return tool_calling.execute_tool("open_app", {"app_name": match})
    return tool_calling.execute_tool("open_web", {"url": url})


class AppFirstFlowTests(unittest.TestCase):
    def test_installed_app_opens_without_touching_the_website(self):
        with mocked_openers([entry("Discord")]) as (index, launch, popen):
            result = simulate_llm_open("Discord", "https://discord.com")

        self.assertEqual(result["opened"], "Discord")
        self.assertEqual(result["via"], "shortcut")
        launch.assert_called_once_with(entry("Discord"))
        index.assert_called()
        popen.assert_not_called()

    def test_missing_app_falls_back_to_the_official_website(self):
        with mocked_openers([entry("Telegram")]) as (index, launch, popen):
            result = simulate_llm_open(
                "Hugging Face", "https://huggingface.co")

        self.assertTrue(result["success"])
        self.assertEqual(result["opened"], "https://huggingface.co")
        index.assert_called()
        launch.assert_not_called()
        popen.assert_called_once_with(
            [BRAVE, "https://huggingface.co"], shell=False,
            **self._windows_flags())

    def test_explicit_website_skips_app_discovery(self):
        with mocked_openers([entry("Discord")]) as (index, launch, popen):
            result = simulate_llm_open(
                "Discord", "https://discord.com", explicit_website=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["opened"], "https://discord.com")
        index.assert_not_called()
        launch.assert_not_called()
        popen.assert_called_once()

    def test_launch_failure_is_reported_without_website_fallback(self):
        with mocked_openers(
                [entry("Discord")], launch_error=OSError("blocked")) as (
                index, launch, popen):
            result = simulate_llm_open("Discord", "https://discord.com")

        self.assertIn("Could not open 'Discord'", result["error"])
        self.assertIn("blocked", result["error"])
        index.assert_called()
        launch.assert_called_once()
        popen.assert_not_called()

    def test_fallback_preserves_unusual_tld_path_and_query(self):
        url = "https://example.uz/docs/page?q=python&lang=en#install"
        with mocked_openers([]) as (index, launch, popen):
            result = simulate_llm_open("Example", url)

        self.assertTrue(result["success"])
        self.assertEqual(result["opened"], url)
        launch.assert_not_called()
        popen.assert_called_once_with(
            [BRAVE, url], shell=False, **self._windows_flags())

    def test_unsafe_fallback_url_is_rejected_before_brave(self):
        with mocked_openers([]) as (index, launch, popen):
            result = simulate_llm_open("Unsafe", "javascript:alert(1)")

        self.assertFalse(result["success"])
        self.assertIn("Only http:// and https://", result["error"])
        popen.assert_not_called()

    @staticmethod
    def _windows_flags():
        # open_web_tool adds CREATE_NO_WINDOW on Windows only.
        import os
        import subprocess
        if os.name != "nt":
            return {}
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


class PromptPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = PROMPT_PATH.read_text(encoding="utf-8")
        cls.prompt = " ".join(text.lower().split())

    def test_prompt_identity_is_templated(self):
        # The prompt used to hard-code "Aze", so changing the assistant name in
        # settings changed the UI and the transcript prefix but not the model's
        # own identity.
        from main import _render_prompt

        text = PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn("{assistant_name}", text)
        rendered = _render_prompt(text, {
            "assistant_name": "Nova",
            "user_name": "Sam",
            "platform": "Windows",
            "date_time": "Saturday",
        })
        self.assertIn("You are Nova.", rendered)
        self.assertIn("Treat Sam with respect", rendered)
        self.assertNotIn("Aze", rendered)

    def test_prompt_prefers_installed_apps_by_default(self):
        self.assertIn("prefers an installed desktop application over a website",
                      self.prompt)
        self.assertIn("if a clear match exists, call `open_app`", self.prompt)
        self.assertIn(
            "do not open the website when a matching desktop application is "
            "installed", self.prompt)

    def test_prompt_explicit_website_or_browser_skips_app_discovery(self):
        self.assertIn("explicitly ask for a website", self.prompt)
        self.assertIn("in the browser", self.prompt)
        self.assertIn("with brave", self.prompt)
        self.assertIn("skip app discovery", self.prompt)

    def test_prompt_falls_back_only_when_no_app_matches(self):
        self.assertIn("if no installed application genuinely matches",
                      self.prompt)
        self.assertIn("no installed app is named", self.prompt)
        self.assertIn("website fallback", self.prompt)

    def test_prompt_never_falls_back_after_launch_failure(self):
        self.assertIn("could not open", self.prompt)
        self.assertIn("never silently switch to the website", self.prompt)
        self.assertIn("launch failure", self.prompt)

    def test_prompt_does_not_invent_local_or_unfamiliar_urls(self):
        self.assertIn("local dashboard", self.prompt)
        self.assertIn("never guess `localhost` or a port", self.prompt)
        self.assertIn("never invent a url", self.prompt)
        self.assertIn("ask one short clarifying question", self.prompt)


class ToolDescriptionPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.declarations = {
            item["name"]: " ".join(
                item["description"].lower().split())
            for item in tool_calling.get_tool_declarations()
        }

    def test_list_apps_is_the_default_entry_point_for_apps(self):
        # The description scopes list_apps to APPLICATION requests. The
        # previous wording — "any request to open something by name" with an
        # open_web fallback — hijacked file requests ("open my homework"),
        # which is how Aze ended up opening websites or claiming an open
        # without touching a file.
        description = self.declarations["list_apps"]
        self.assertIn("call this first", description)
        self.assertIn("open an application by name", description)
        self.assertIn("for files", description)
        self.assertIn("call search_files", description)
        self.assertIn("never open a website as a fallback for a file or "
                      "folder", description)
        self.assertIn("if none matches", description)
        self.assertIn("open_web", description)

    def test_open_app_distinguishes_missing_from_launch_failure(self):
        description = self.declarations["open_app"]
        self.assertIn("no installed app has that name", description)
        self.assertIn("fall back to open_web", description)
        self.assertIn("could not be opened", description)
        self.assertIn("do not open the website", description)

    def test_open_web_is_not_a_silent_app_failure_fallback(self):
        description = self.declarations["open_web"]
        self.assertIn("explicitly asks for a website", description)
        self.assertIn("no genuine desktop-app match", description)
        self.assertIn("never use it as a silent fallback", description)
        self.assertIn("ask the user", description)


if __name__ == "__main__":
    unittest.main()

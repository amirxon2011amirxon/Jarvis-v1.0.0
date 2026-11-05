"""Tests for the close_web tool.

Every browser operation is mocked or replaced with a fake backend. No test reads
or closes a real Brave tab, launches a browser or terminates a process.
Run from the project root with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from unittest import mock

from actions import close_web_tool, open_web_tool, tool_calling


def tab(tab_id, title, url="", window_id="1:10", window_index=1,
        position=1, active=False):
    return {
        "tab_id": tab_id,
        "title": title,
        "url": url,
        "window_id": window_id,
        "window_index": window_index,
        "position": position,
        "active": active,
    }


def internal_tab(tab_id="uia:1:10:1", title="Claude", url="",
                 window_id="1:10", window_index=1, position=1,
                 active=True):
    return close_web_tool._Tab(
        tab_id=tab_id,
        title=title,
        url=url,
        window_id=window_id,
        window_index=window_index,
        position=position,
        active=active,
    )


class MatchingTests(unittest.TestCase):
    def test_site_name_matches_hostname_paths_and_subdomains(self):
        tabs = [
            tab("claude", "Claude", "https://claude.ai/new"),
            tab("notion", "Notion", "https://www.notion.so/projects"),
            tab("github", "GitHub", "https://docs.github.com/en"),
            tab("hf", "Hugging Face",
                "https://huggingface.co/models"),
        ]
        cases = {
            "Claude": "claude",
            "claude": "claude",
            "Notion": "notion",
            "GitHub": "github",
            "github": "github",
            "Hugging Face": "hf",
            "HuggingFace": "hf",
        }
        for target, expected in cases.items():
            with self.subTest(target=target):
                result = close_web_tool.match_target_tab(tabs, target)
                self.assertEqual(result["status"], "matched")
                self.assertEqual(result["tab"]["tab_id"], expected)

    def test_domain_that_merely_contains_a_site_word_is_not_that_site(self):
        tabs = [
            tab("evil", "GitHub", "https://github.evil.com/"),
            tab("squat", "GitHub", "https://notgithub.com/"),
            tab("tracker", "GitHub", "https://github-tracker.com/"),
        ]
        result = close_web_tool.match_target_tab(tabs, "GitHub")
        self.assertEqual(result["status"], "not_found")

    def test_title_mention_on_another_site_is_not_the_site(self):
        tabs = [tab("video", "Claude tutorial - YouTube",
                    "https://www.youtube.com/watch?v=1")]
        result = close_web_tool.match_target_tab(tabs, "Claude")
        self.assertEqual(result["status"], "not_found")

    def test_inactive_tab_without_url_can_match_by_title(self):
        tabs = [tab("claude", "Claude", "")]
        result = close_web_tool.match_target_tab(tabs, "Claude")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["reason"], "title")

    def test_multiple_matching_tabs_require_clarification(self):
        tabs = [
            tab("one", "Claude", "https://claude.ai/new", position=1),
            tab("two", "Claude", "", position=2),
        ]
        result = close_web_tool.match_target_tab(tabs, "Claude")
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["tab"])
        self.assertEqual(
            [item["tab_id"] for item in result["matches"]], ["one", "two"])
        self.assertIn("Which one", result["message"])

    def test_no_matching_tab(self):
        tabs = [tab("notion", "Notion", "https://www.notion.so")]
        result = close_web_tool.match_target_tab(tabs, "Claude")
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["matches"], [])

    def test_current_tab_uses_the_active_flag(self):
        tabs = [
            tab("claude", "Claude", "https://claude.ai", active=True),
            tab("notion", "Notion", "https://www.notion.so", position=2),
        ]
        for target in ("current_tab", "this tab", "the current website",
                       "active tab", "tab"):
            with self.subTest(target=target):
                result = close_web_tool.match_target_tab(tabs, target)
                self.assertEqual(result["status"], "matched")
                self.assertEqual(result["tab"]["tab_id"], "claude")

    def test_current_tab_across_windows_uses_the_foreground_window(self):
        tabs = [
            tab("first", "Claude", window_id="1:10", window_index=1,
                active=True),
            tab("second", "Notion", window_id="1:20", window_index=2,
                active=True),
        ]
        with mock.patch.object(
                close_web_tool, "_foreground_hwnd", return_value=20):
            result = close_web_tool.match_target_tab(tabs, "current_tab")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tab"]["tab_id"], "second")

    def test_current_tab_across_windows_without_focus_is_ambiguous(self):
        tabs = [
            tab("first", "Claude", window_id="1:10", window_index=1,
                active=True),
            tab("second", "Notion", window_id="1:20", window_index=2,
                active=True),
        ]
        with mock.patch.object(
                close_web_tool, "_foreground_hwnd", return_value=999):
            result = close_web_tool.match_target_tab(tabs, "current_tab")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["matches"]), 2)

    def test_empty_and_application_targets_are_rejected(self):
        tabs = [tab("brave", "Brave", "https://brave.com")]
        for target in ("", "   ", "Brave", "the browser", "Brave Browser"):
            with self.subTest(target=target):
                result = close_web_tool.match_target_tab(tabs, target)
                self.assertEqual(result["status"], "invalid")

    def test_verbatim_phrase_is_tolerated(self):
        tabs = [tab("notion", "Notion", "https://www.notion.so")]
        for target in ("close Notion", "Close the Notion page",
                       "close the notion tab."):
            with self.subTest(target=target):
                result = close_web_tool.match_target_tab(tabs, target)
                self.assertEqual(result["status"], "matched")

    def test_url_target_matches_the_host(self):
        tabs = [
            tab("claude", "Claude", "https://claude.ai/new"),
            tab("notion", "Notion", "https://www.notion.so", position=2),
        ]
        result = close_web_tool.match_target_tab(
            tabs, "https://claude.ai/new")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tab"]["tab_id"], "claude")


class HoverCardTitleTests(unittest.TestCase):
    """Chromium exposes a hovered tab's accessible name as its hover card.

    The card wraps the real page title in quotes and reports the tab's memory
    use around it. Before this was handled, an ordinary title such as
    "Новая сессия - Claude" became ``Вкладка "Новая сессия - Claude" использует
    117 МБ памяти``, matched nothing, and the caller was told
    ``No open Brave tab matches "Claude"``.
    """

    def test_hover_card_name_becomes_the_page_title(self):
        cases = {
            'Вкладка "Новая сессия - Claude" использует 117 МБ памяти':
                "Новая сессия - Claude",
            'Вкладка "Claude" использует 117 МБ памяти': "Claude",
            'Tab "Claude" uses 117 MB of memory': "Claude",
            'Tab "Notion" uses 1.2 GB of memory': "Notion",
            'Tab "Docs" uses 850 KB of memory': "Docs",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(close_web_tool._display_title(raw), expected)

    def test_a_title_that_merely_contains_quotes_is_left_alone(self):
        for title in ('He said "hi" - Claude',
                      'The AI workspace that works for you. | Notion'):
            with self.subTest(title=title):
                self.assertEqual(close_web_tool._display_title(title), title)

    def test_hovered_tab_is_still_matched_by_name(self):
        tabs = [
            tab("claude",
                close_web_tool._display_title(
                    'Вкладка "Новая сессия - Claude" использует 117 МБ памяти'),
                ""),
            tab("notion", "The AI workspace that works for you. | Notion",
                "https://www.notion.com/", position=2, active=True),
        ]
        result = close_web_tool.match_target_tab(tabs, "Claude")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tab"]["tab_id"], "claude")

    def test_hovered_tab_is_closed_end_to_end(self):
        tabs = [
            tab("claude",
                close_web_tool._display_title(
                    'Вкладка "Новая сессия - Claude" использует 117 МБ памяти'),
                ""),
            tab("notion", "The AI workspace that works for you. | Notion",
                "https://www.notion.com/", position=2, active=True),
        ]
        closed = {"success": True, "closed": "Новая сессия - Claude"}
        with mock.patch.object(
                close_web_tool, "get_open_tabs", return_value=tabs), \
             mock.patch.object(
                close_web_tool, "close_tab", return_value=closed) as close:
            result = close_web_tool.close_web("Claude")

        self.assertTrue(result["success"])
        close.assert_called_once()
        self.assertEqual(close.call_args.args[0]["tab_id"], "claude")


class CloseWebOrchestrationTests(unittest.TestCase):
    def test_close_claude_leaves_notion_and_github_open(self):
        tabs = [
            tab("claude", "Claude", "https://claude.ai", active=True),
            tab("notion", "Notion", "https://www.notion.so", position=2),
            tab("github", "GitHub", "https://github.com", position=3),
        ]
        closed = {"success": True, "closed": "Claude",
                  "message": 'Closed the "Claude" tab.'}
        with mock.patch.object(
                close_web_tool, "get_open_tabs", return_value=tabs), \
             mock.patch.object(
                close_web_tool, "close_tab", return_value=closed) as close:
            result = close_web_tool.close_web("Claude")

        self.assertTrue(result["success"])
        self.assertEqual(result["closed"], "Claude")
        close.assert_called_once()
        self.assertEqual(close.call_args.args[0]["tab_id"], "claude")

    def test_closing_the_only_tab_uses_the_normal_close_path(self):
        tabs = [tab("claude", "Claude", "https://claude.ai", active=True)]
        closed = {"success": True, "closed": "Claude", "verified": True,
                  "message": 'Closed the "Claude" tab.'}
        with mock.patch.object(
                close_web_tool, "get_open_tabs", return_value=tabs), \
             mock.patch.object(
                close_web_tool, "close_tab", return_value=closed) as close:
            result = close_web_tool.close_web("Claude")

        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        close.assert_called_once()
        self.assertEqual(close.call_args.args[0]["tab_id"], "claude")

    def test_close_current_tab(self):
        tabs = [
            tab("claude", "Claude", "https://claude.ai", active=True),
            tab("notion", "Notion", "https://www.notion.so", position=2),
        ]
        closed = {"success": True, "closed": "Claude"}
        with mock.patch.object(
                close_web_tool, "get_open_tabs", return_value=tabs), \
             mock.patch.object(
                close_web_tool, "close_tab", return_value=closed) as close:
            result = close_web_tool.close_web("current_tab")

        self.assertTrue(result["success"])
        self.assertEqual(close.call_args.args[0]["tab_id"], "claude")

    def test_ambiguous_target_does_not_close_anything(self):
        tabs = [
            tab("one", "Claude", "https://claude.ai", position=1),
            tab("two", "Claude", "", position=2),
        ]
        with mock.patch.object(
                close_web_tool, "get_open_tabs", return_value=tabs), \
             mock.patch.object(close_web_tool, "close_tab") as close:
            result = close_web_tool.close_web("Claude")

        self.assertFalse(result["success"])
        self.assertTrue(result["clarification_required"])
        self.assertEqual(len(result["matches"]), 2)
        close.assert_not_called()

    def test_no_match_does_not_close_anything(self):
        tabs = [tab("notion", "Notion", "https://www.notion.so")]
        with mock.patch.object(
                close_web_tool, "get_open_tabs", return_value=tabs), \
             mock.patch.object(close_web_tool, "close_tab") as close:
            result = close_web_tool.close_web("Claude")

        self.assertFalse(result["success"])
        self.assertIn("No open Brave tab", result["error"])
        close.assert_not_called()

    def test_brave_not_running_is_a_structured_error(self):
        with mock.patch.object(
                close_web_tool, "get_open_tabs",
                side_effect=close_web_tool.BraveNotRunning("Brave is not running.")), \
             mock.patch.object(close_web_tool, "close_tab") as close:
            result = close_web_tool.close_web("Claude")

        self.assertFalse(result["success"])
        self.assertIn("Brave isn't running", result["message"])
        self.assertIn("Brave is not running", result["error"])
        close.assert_not_called()

    def test_browser_control_failure_is_a_structured_error(self):
        with mock.patch.object(
                close_web_tool, "get_open_tabs",
                side_effect=close_web_tool.BrowserControlError(
                    "UI Automation is unavailable.")), \
             mock.patch.object(close_web_tool, "close_tab") as close:
            result = close_web_tool.close_web("Claude")

        self.assertFalse(result["success"])
        self.assertIn("couldn't read Brave's tabs", result["message"])
        self.assertIn("UI Automation is unavailable", result["error"])
        close.assert_not_called()

    def test_empty_target_is_rejected_before_browser_access(self):
        with mock.patch.object(close_web_tool, "get_open_tabs") as tabs, \
             mock.patch.object(close_web_tool, "close_tab") as close:
            result = close_web_tool.close_web("   ")

        self.assertFalse(result["success"])
        self.assertIn("which website", result["message"])
        tabs.assert_not_called()
        close.assert_not_called()

    def test_application_target_is_rejected(self):
        with mock.patch.object(close_web_tool, "get_open_tabs") as tabs, \
             mock.patch.object(close_web_tool, "close_tab") as close:
            result = close_web_tool.close_web("Brave")

        self.assertFalse(result["success"])
        self.assertIn("application action", result["message"])
        self.assertIn("close_app", result["error"])
        tabs.assert_not_called()
        close.assert_not_called()

    def test_chosen_tab_id_is_used_directly(self):
        closed = {"success": True, "closed": "Claude"}
        with mock.patch.object(close_web_tool, "get_open_tabs") as tabs, \
             mock.patch.object(
                close_web_tool, "close_tab", return_value=closed) as close:
            result = close_web_tool.close_web(
                "Claude", tab_id="uia:1:10:42.10.4.0.0.1")

        self.assertTrue(result["success"])
        close.assert_called_once_with(
            {"tab_id": "uia:1:10:42.10.4.0.0.1"})
        tabs.assert_not_called()
        self.assertEqual(result["target"], "Claude")

    def test_named_target_across_multiple_windows_closes_only_one(self):
        tabs = [
            tab("notion", "Notion", "https://www.notion.so",
                window_id="1:10", window_index=1),
            tab("github", "GitHub", "https://github.com",
                window_id="1:10", window_index=1, position=2),
            tab("claude", "Claude", "https://claude.ai/new",
                window_id="1:20", window_index=2),
        ]
        with mock.patch.object(
                close_web_tool, "get_open_tabs", return_value=tabs), \
             mock.patch.object(
                close_web_tool, "close_tab",
                return_value={"success": True, "closed": "Claude"}) as close:
            result = close_web_tool.close_web("Claude")

        self.assertTrue(result["success"])
        close.assert_called_once()
        self.assertEqual(close.call_args.args[0]["tab_id"], "claude")

    def test_duplicate_target_across_multiple_windows_is_ambiguous(self):
        tabs = [
            tab("claude-a", "Claude", "https://claude.ai",
                window_id="1:10", window_index=1),
            tab("claude-b", "Claude", "https://claude.ai/new",
                window_id="1:20", window_index=2),
        ]
        with mock.patch.object(
                close_web_tool, "get_open_tabs", return_value=tabs), \
             mock.patch.object(close_web_tool, "close_tab") as close:
            result = close_web_tool.close_web("Claude")

        self.assertTrue(result["clarification_required"])
        close.assert_not_called()


class CloseTabTests(unittest.TestCase):
    def test_success_is_verified(self):
        target = internal_tab()
        with mock.patch.object(
                close_web_tool, "_read_tabs", return_value=[target]), \
             mock.patch.object(close_web_tool, "_select_tab"), \
             mock.patch.object(
                close_web_tool, "_wait_for_close_button",
                return_value=object()), \
             mock.patch.object(close_web_tool, "_invoke_close"), \
             mock.patch.object(
                close_web_tool, "_wait_until_gone", return_value=True):
            result = close_web_tool.close_tab(
                {"tab_id": target.tab_id})

        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["closed"], "Claude")
        self.assertEqual(result["tab"]["tab_id"], target.tab_id)

    def test_closing_a_background_tab_restores_the_previous_active_tab(self):
        target = internal_tab(active=False)
        previous = internal_tab(tab_id="uia:previous", title="Notion",
                                active=True)
        with mock.patch.object(
                close_web_tool, "_read_tabs",
                return_value=[previous, target]), \
             mock.patch.object(close_web_tool, "_select_tab"), \
             mock.patch.object(
                close_web_tool, "_wait_for_close_button",
                return_value=object()), \
             mock.patch.object(close_web_tool, "_invoke_close"), \
             mock.patch.object(
                close_web_tool, "_wait_until_gone", return_value=True), \
             mock.patch.object(close_web_tool, "_restore_selection") as restore:
            result = close_web_tool.close_tab(
                {"tab_id": target.tab_id})

        self.assertTrue(result["success"])
        restore.assert_called_once_with(previous)

    def test_tab_disappearing_before_close_is_reported(self):
        with mock.patch.object(
                close_web_tool, "_read_tabs", return_value=[]), \
             mock.patch.object(close_web_tool, "_close_one") as close:
            result = close_web_tool.close_tab({"tab_id": "uia:gone"})

        self.assertFalse(result["success"])
        self.assertIn("no longer open", result["message"])
        close.assert_not_called()

    def test_failed_close_operation_is_reported(self):
        target = internal_tab()
        with mock.patch.object(
                close_web_tool, "_read_tabs", return_value=[target]), \
             mock.patch.object(close_web_tool, "_select_tab"), \
             mock.patch.object(
                close_web_tool, "_wait_for_close_button",
                return_value=object()), \
             mock.patch.object(
                close_web_tool, "_invoke_close",
                side_effect=RuntimeError("close refused")), \
             mock.patch.object(close_web_tool, "_restore_selection") as restore:
            result = close_web_tool.close_tab(
                {"tab_id": target.tab_id})

        self.assertFalse(result["success"])
        self.assertIn("did not close", result["message"])
        self.assertIn("close refused", result["error"])
        restore.assert_called_once()

    def test_still_present_after_close_is_reported(self):
        target = internal_tab(active=False)
        previous = internal_tab(tab_id="uia:previous", title="Notion",
                                active=True)
        with mock.patch.object(
                close_web_tool, "_read_tabs",
                return_value=[previous, target]), \
             mock.patch.object(close_web_tool, "_select_tab"), \
             mock.patch.object(
                close_web_tool, "_wait_for_close_button",
                return_value=object()), \
             mock.patch.object(close_web_tool, "_invoke_close"), \
             mock.patch.object(
                close_web_tool, "_wait_until_gone", return_value=False), \
             mock.patch.object(close_web_tool, "_restore_selection") as restore:
            result = close_web_tool.close_tab(
                {"tab_id": target.tab_id})

        self.assertFalse(result["success"])
        self.assertIn("may be waiting for confirmation", result["message"])
        restore.assert_called_once_with(previous)

    def test_unverified_close_is_not_reported_as_verified(self):
        target = internal_tab()
        with mock.patch.object(
                close_web_tool, "_read_tabs", return_value=[target]), \
             mock.patch.object(close_web_tool, "_select_tab"), \
             mock.patch.object(
                close_web_tool, "_wait_for_close_button",
                return_value=object()), \
             mock.patch.object(close_web_tool, "_invoke_close"), \
             mock.patch.object(
                close_web_tool, "_wait_until_gone", return_value=None):
            result = close_web_tool.close_tab(
                {"tab_id": target.tab_id})

        self.assertFalse(result["success"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["close_requested"])
        self.assertEqual(result["closed"], "")
        self.assertIn("could not verify", result["message"])

    def test_missing_close_control_is_reported(self):
        target = internal_tab()
        with mock.patch.object(
                close_web_tool, "_read_tabs", return_value=[target]), \
             mock.patch.object(close_web_tool, "_select_tab"), \
             mock.patch.object(
                close_web_tool, "_wait_for_close_button",
                return_value=None), \
             mock.patch.object(close_web_tool, "_restore_selection") as restore:
            result = close_web_tool.close_tab(
                {"tab_id": target.tab_id})

        self.assertFalse(result["success"])
        self.assertIn("close control", result["message"])
        restore.assert_called_once()

    def test_backend_failure_is_a_structured_error(self):
        with mock.patch.object(
                close_web_tool, "_read_tabs",
                side_effect=close_web_tool.BrowserControlError(
                    "connection lost")):
            result = close_web_tool.close_tab({"tab_id": "uia:1"})

        self.assertFalse(result["success"])
        self.assertIn("couldn't read Brave's tabs", result["message"])
        self.assertIn("connection lost", result["error"])

    def test_invalid_tab_record_is_rejected(self):
        for value in ({}, {"tab_id": ""}, None, "not-a-tab"):
            with self.subTest(value=value):
                result = close_web_tool.close_tab(value)
                self.assertFalse(result["success"])
                self.assertIn("No tab was identified", result["message"])


class RegistryIntegrationTests(unittest.TestCase):
    def test_close_web_is_declared_for_the_llm(self):
        self.assertIn("close_web", tool_calling.get_tool_names())
        declaration = {
            item["name"]: item
            for item in tool_calling.get_tool_declarations()
        }["close_web"]
        self.assertEqual(declaration["parameters"]["type"], "OBJECT")
        self.assertIn("target", declaration["parameters"]["properties"])
        self.assertIn("tab_id", declaration["parameters"]["properties"])
        self.assertEqual(declaration["parameters"]["required"], ["target"])
        description = declaration["description"].lower()
        self.assertIn("current_tab", description)
        self.assertIn("clarification_required", description)
        self.assertIn("close brave", description)

    def test_close_web_is_callable_through_execute_tool(self):
        tabs = [tab("claude", "Claude", "https://claude.ai", active=True)]
        closed = {"success": True, "closed": "Claude",
                  "verified": True, "message": 'Closed the "Claude" tab.'}
        events = []
        with mock.patch.object(
                close_web_tool, "get_open_tabs", return_value=tabs), \
             mock.patch.object(
                close_web_tool, "close_tab", return_value=closed):
            result = tool_calling.execute_tool(
                "close_web", {"target": "Claude"},
                event_callback=events.append)

        self.assertTrue(result["success"])
        self.assertEqual(result["closed"], "Claude")
        self.assertEqual(events[0]["target"], "Claude")
        self.assertTrue(events[-1]["ok"])
        self.assertEqual(events[-1]["summary"], "closed Claude")

    def test_ambiguity_reaches_the_llm_without_closing(self):
        tabs = [
            tab("one", "Claude", "https://claude.ai", position=1),
            tab("two", "Claude", "", position=2),
        ]
        events = []
        with mock.patch.object(
                close_web_tool, "get_open_tabs", return_value=tabs), \
             mock.patch.object(close_web_tool, "close_tab") as close:
            result = tool_calling.execute_tool(
                "close_web", {"target": "Claude"},
                event_callback=events.append)

        self.assertTrue(result["clarification_required"])
        self.assertEqual(len(result["matches"]), 2)
        close.assert_not_called()
        self.assertFalse(events[-1]["ok"])
        self.assertIn("Which one", events[-1]["summary"])

    def test_bad_arguments_are_reported(self):
        result = tool_calling.execute_tool(
            "close_web", {"website": "Claude"})
        self.assertIn("error", result)
        self.assertIn("Bad arguments", result["error"])

    def test_open_web_still_works_through_the_same_pipeline(self):
        brave = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

        class FakeProcess:
            def poll(self):
                return None

        with mock.patch.object(
                open_web_tool, "get_brave_path", return_value=brave), \
             mock.patch.object(
                open_web_tool.subprocess, "Popen",
                return_value=FakeProcess()) as popen:
            result = tool_calling.execute_tool(
                "open_web", {"url": "https://claude.ai"})

        self.assertTrue(result["success"])
        self.assertEqual(result["opened"], "https://claude.ai")
        self.assertEqual(popen.call_args.args[0], [brave, "https://claude.ai"])


if __name__ == "__main__":
    unittest.main()

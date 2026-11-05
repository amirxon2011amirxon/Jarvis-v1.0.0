"""Tests for closing one open file or folder window.

Every Windows operation is mocked: no window is really closed, no process is
touched, no application is launched. The tests cover target identification,
the multi-document refusal, honest "initiated but not confirmed" reporting,
and registration through Aze's tool registry.

Run with:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from actions import close_file_tool, tool_calling


def win(hwnd, title, process, pid=None):
    return {"hwnd": hwnd, "title": title, "process": process,
            "pid": pid or hwnd}


class CloseFileTestCase(unittest.TestCase):
    """A fake window list; posting a close only records the handle."""

    def setUp(self):
        self.windows = []
        self.explorer = []
        self.posted = []

        patches = [
            mock.patch.object(
                close_file_tool, "_windows",
                side_effect=lambda: list(self.windows)),
            mock.patch.object(
                close_file_tool, "_explorer_windows",
                side_effect=lambda: list(self.explorer)),
            mock.patch.object(
                close_file_tool, "_post_close", side_effect=self._post),
            # A closed window disappears on the first check.
            mock.patch.object(
                close_file_tool, "_window_alive", return_value=False),
            # Localized display names are a real shell query; tests that care
            # patch this explicitly.
            mock.patch.object(
                close_file_tool, "_localized_name", return_value=""),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _post(self, hwnd):
        self.posted.append(hwnd)
        return True


class SingleFileTests(CloseFileTestCase):
    def test_closes_one_matching_document(self):
        self.windows = [
            win(1, "report.xlsx - Excel", "excel"),
            win(2, "notes.txt - Notepad", "notepad"),
        ]
        result = close_file_tool.close_file(path=r"C:\docs\report.xlsx")

        self.assertTrue(result["success"])
        self.assertEqual(result["closed"], "report.xlsx - Excel")
        self.assertEqual(result["via"], "window close")
        self.assertEqual(self.posted, [1])

    def test_only_the_named_workbook_closes(self):
        self.windows = [
            win(1, "report 2024.xlsx - Excel", "excel"),
            win(2, "report 2025.xlsx - Excel", "excel"),
        ]
        result = close_file_tool.close_file(path=r"C:\docs\report 2024.xlsx")

        self.assertTrue(result["success"])
        self.assertEqual(self.posted, [1])

    def test_a_longer_file_name_is_not_the_requested_file(self):
        self.windows = [win(3, "report.xlsx.bak - Notepad", "notepad")]
        result = close_file_tool.close_file(path=r"C:\docs\report.xlsx")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "not_found")
        self.assertEqual(self.posted, [])

    def test_app_context_narrows_two_windows_with_the_same_name(self):
        self.windows = [
            win(1, "report.xlsx - Excel", "excel"),
            win(2, "report.xlsx - Notepad", "notepad"),
        ]
        result = close_file_tool.close_file(name="report.xlsx", app="Excel")

        self.assertTrue(result["success"])
        self.assertEqual(self.posted, [1])


class FolderTests(CloseFileTestCase):
    def test_folder_window_closes_by_exact_shell_path(self):
        self.windows = [
            win(30, "Projects", "explorer"),
            win(31, "Downloads", "explorer"),
            win(32, "something.docx - Word", "winword"),
        ]
        self.explorer = [{"hwnd": 30, "path": r"D:\Work\Projects"}]

        result = close_file_tool.close_file(path=r"D:\Work\Projects")

        self.assertTrue(result["success"])
        self.assertEqual(result["item_type"], "folder")
        self.assertEqual(self.posted, [30])

    def test_same_folder_name_in_two_places_is_told_apart(self):
        self.windows = [
            win(30, "Projects", "explorer"),
            win(31, "Projects", "explorer"),
        ]
        self.explorer = [
            {"hwnd": 30, "path": r"D:\A\Projects"},
            {"hwnd": 31, "path": r"D:\B\Projects"},
        ]

        result = close_file_tool.close_file(path=r"D:\B\Projects")

        self.assertTrue(result["success"])
        self.assertEqual(self.posted, [31])

    def test_folder_by_name_matches_the_explorer_title(self):
        self.windows = [win(40, "Downloads", "explorer")]
        with mock.patch.object(close_file_tool, "_known_folder_path",
                               return_value=""):
            result = close_file_tool.close_file(name="Downloads")
        self.assertTrue(result["success"])
        self.assertEqual(self.posted, [40])

    def test_a_shell_folder_word_is_matched_by_its_real_path(self):
        # Explorer shows a localized display name for shell folders, so a
        # title-only match for "Music" can never work. The word is resolved to
        # the folder path and matched through the shell's window collection.
        music = r"C:\Users\delux\Music"
        self.windows = [win(30, "Личная папка", "explorer")]
        self.explorer = [{"hwnd": 30, "path": music}]
        with mock.patch.object(close_file_tool, "_known_folder_path",
                               return_value=music):
            result = close_file_tool.close_file(name="Music")

        self.assertTrue(result["success"])
        self.assertEqual(self.posted, [30])

    def test_a_localized_folder_title_matches_by_display_name(self):
        # When the shell's window collection is unavailable, the localized
        # display name is the fallback for a known folder path.
        self.windows = [win(31, "Рабочие файлы", "explorer")]
        self.explorer = []
        with mock.patch.object(close_file_tool, "_localized_name",
                               return_value="Рабочие файлы"):
            result = close_file_tool.close_file(
                path=r"C:\Users\delux\Downloads")

        self.assertTrue(result["success"])
        self.assertEqual(self.posted, [31])

    def test_a_system_language_folder_word_is_matched_by_path(self):
        folder = r"C:\Users\delux\Music"
        self.windows = [win(32, "Личная папка", "explorer")]
        self.explorer = [{"hwnd": 32, "path": folder}]
        with mock.patch.object(
                close_file_tool, "_known_folder_path",
                side_effect=lambda name:
                folder if name.casefold() == "личная папка" else ""):
            result = close_file_tool.close_file(name="Личная папка")

        self.assertTrue(result["success"])
        self.assertEqual(self.posted, [32])


class AmbiguityTests(CloseFileTestCase):
    def test_multiple_matches_close_nothing_without_an_explicit_all(self):
        self.windows = [
            win(1, "report 2024.xlsx - Excel", "excel"),
            win(2, "report 2025.xlsx - Excel", "excel"),
        ]
        result = close_file_tool.close_file(name="report")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(self.posted, [])

    def test_string_false_all_matches_stays_ambiguous(self):
        self.windows = [
            win(1, "report 2024.xlsx - Excel", "excel"),
            win(2, "report 2025.xlsx - Excel", "excel"),
        ]
        result = close_file_tool.close_file(name="report",
                                            all_matches="false")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "ambiguous")
        self.assertEqual(self.posted, [])

    def test_an_unknown_target_closes_nothing(self):
        self.windows = [win(1, "notes.txt - Notepad", "notepad")]
        result = close_file_tool.close_file(name="budget.xlsx")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "not_found")
        self.assertEqual(self.posted, [])


class AppOnlyTests(CloseFileTestCase):
    """"Close File Explorer" means that app's windows, with the same rules."""

    def test_one_window_of_the_named_app_closes(self):
        self.windows = [
            win(30, "Рабочая папка", "explorer"),
            win(1, "report.xlsx - Excel", "excel"),
        ]
        result = close_file_tool.close_file(app="File Explorer")

        self.assertTrue(result["success"])
        self.assertEqual(self.posted, [30])

    def test_several_windows_of_the_named_app_are_ambiguous(self):
        self.windows = [
            win(30, "Projects", "explorer"),
            win(31, "Downloads", "explorer"),
        ]
        result = close_file_tool.close_file(app="File Explorer")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(self.posted, [])

    def test_no_window_of_the_named_app_is_not_found(self):
        self.windows = [win(1, "report.xlsx - Excel", "excel")]
        result = close_file_tool.close_file(app="Notepad")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "not_found")
        self.assertEqual(self.posted, [])


class CloseAllTests(CloseFileTestCase):
    def test_close_all_matching_leaves_unrelated_windows_alone(self):
        self.windows = [
            win(1, "a.pdf - SumatraPDF", "sumatrapdf"),
            win(2, "b.pdf - SumatraPDF", "sumatrapdf"),
            win(3, "notes.txt - Notepad", "notepad"),
        ]
        result = close_file_tool.close_file(name=".pdf", all_matches=True)

        self.assertTrue(result["success"])
        self.assertEqual(self.posted, [1, 2])
        self.assertEqual(len(result["closed"]), 2)
        self.assertNotIn(3, self.posted)


class SafetyTests(CloseFileTestCase):
    def test_an_unconfirmed_close_is_reported_as_initiated(self):
        self.windows = [win(1, "report.xlsx - Excel", "excel")]
        with mock.patch.object(
                close_file_tool, "_await_closed", return_value=False):
            result = close_file_tool.close_file(path=r"C:\docs\report.xlsx")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "close_initiated")
        self.assertEqual(result["closing"], "report.xlsx - Excel")
        self.assertIn("still open", result["message"])

    def test_a_multi_document_window_is_refused(self):
        self.windows = [win(5, "main.py - project - Visual Studio Code", "code")]
        result = close_file_tool.close_file(path=r"D:\project\main.py")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "unsupported")
        self.assertIn("several files", result["message"])
        self.assertEqual(self.posted, [])

    def test_a_refused_window_is_left_open_even_in_an_all_request(self):
        self.windows = [
            win(1, "main.py - project - Visual Studio Code", "code"),
            win(2, "a.pdf - SumatraPDF", "sumatrapdf"),
        ]
        result = close_file_tool.close_file(name="a", all_matches=True)

        self.assertEqual(self.posted, [2])
        self.assertFalse(result["success"])
        self.assertEqual(len(result["refused"]), 1)

    def test_a_refused_post_is_reported_not_claimed(self):
        self.windows = [win(1, "report.xlsx - Excel", "excel")]
        with mock.patch.object(
                close_file_tool, "_post_close", return_value=False):
            result = close_file_tool.close_file(path=r"C:\docs\report.xlsx")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "close_failed")
        self.assertNotIn("closed", result)

    def test_closing_never_terminates_a_process(self):
        self.windows = [win(1, "report.xlsx - Excel", "excel")]
        with mock.patch("subprocess.run") as run, \
             mock.patch("subprocess.Popen") as popen:
            result = close_file_tool.close_file(path=r"C:\docs\report.xlsx")
        self.assertTrue(result["success"])
        run.assert_not_called()
        popen.assert_not_called()


class RecentTargetTests(CloseFileTestCase):
    def test_no_arguments_uses_the_most_recently_opened_item(self):
        self.windows = [win(1, "report.xlsx - Excel", "excel")]
        with mock.patch(
                "actions.open_file_tool.recently_opened",
                return_value=[{"path": r"C:\docs\report.xlsx",
                               "item_type": "file"}]), \
             mock.patch(
                "actions.open_file_tool._remember_opened"):
            result = close_file_tool.close_file()

        self.assertTrue(result["success"])
        self.assertTrue(result["used_recent"])
        self.assertEqual(self.posted, [1])

    def test_no_arguments_and_no_history_asks_for_a_target(self):
        with mock.patch(
                "actions.open_file_tool.recently_opened",
                return_value=[]):
            result = close_file_tool.close_file()

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_target")
        self.assertIn("which file or folder", result["message"])
        self.assertEqual(self.posted, [])

    def test_history_entry_that_is_no_longer_open_is_skipped(self):
        self.windows = [win(2, "budget.xlsx - Excel", "excel")]
        with mock.patch(
                "actions.open_file_tool.recently_opened",
                return_value=[
                    {"path": r"C:\docs\gone.xlsx", "item_type": "file"},
                    {"path": r"C:\docs\budget.xlsx", "item_type": "file"},
                ]):
            result = close_file_tool.close_file()

        self.assertTrue(result["success"])
        self.assertEqual(self.posted, [2])


class RegistrationTests(CloseFileTestCase):
    def test_the_tool_is_registered_and_declared(self):
        self.assertIn("close_file", tool_calling.get_tool_names())
        declarations = {item["name"]: item
                        for item in tool_calling.get_tool_declarations()}
        declaration = declarations["close_file"]

        self.assertEqual(declaration["parameters"]["type"], "OBJECT")
        self.assertEqual(declaration["parameters"]["required"], [])
        for name in ("path", "name", "app", "all_matches"):
            self.assertIn(name, declaration["parameters"]["properties"])
        self.assertIn("close_app", declaration["description"])

    def test_dispatch_reaches_the_handler_and_reports_closed(self):
        self.windows = [win(1, "report.xlsx - Excel", "excel")]
        events = []
        result = tool_calling.execute_tool(
            "close_file", {"path": r"C:\docs\report.xlsx"},
            event_callback=events.append)

        self.assertTrue(result["success"])
        self.assertEqual(events[-1]["tool"], "close_file")
        self.assertEqual(events[-1]["target"], r"C:\docs\report.xlsx")
        self.assertTrue(events[-1]["ok"])
        self.assertEqual(events[-1]["summary"], "closed report.xlsx - Excel")

    def test_event_summary_joins_a_batch(self):
        self.assertEqual(
            tool_calling._event_summary({"closed": ["a", "b"]}),
            (True, "closed a, b"))

    def test_event_target_prefers_the_path(self):
        self.assertEqual(
            tool_calling._event_target("close_file",
                                       {"path": " C:\\x ", "name": "x"}),
            "C:\\x")
        self.assertEqual(
            tool_calling._event_target("close_file", {"name": "report"}),
            "report")


class RealMatchingTests(unittest.TestCase):
    """Pure matching helpers, no Windows calls."""

    def test_whole_name_boundaries(self):
        has = close_file_tool._title_has_name
        self.assertTrue(has("report.xlsx - Excel", "report.xlsx"))
        self.assertTrue(has("report.xlsx* - Excel", "report.xlsx"))
        self.assertTrue(has(r"D:\docs\report.xlsx - Code", "report.xlsx"))
        self.assertFalse(has("report.xlsx.bak - Notepad", "report.xlsx"))
        self.assertFalse(has("my-report.xlsx - Excel", "report.xlsx"))
        # A literal "t" was accidentally part of the boundary set (the source
        # contained a double-escaped `\\t`), so a file name beginning with t
        # after any other character was treated as a separate name.
        self.assertFalse(has("mytestreport.xlsx - Excel", "report.xlsx"))
        self.assertFalse(has("target.xlsx - Excel", "arget.xlsx"))

    def test_app_aliases(self):
        matches = close_file_tool._app_matches
        self.assertTrue(matches({"process": "excel",
                                 "title": "a - Excel"}, "Excel"))
        self.assertTrue(matches({"process": "winword",
                                 "title": "a - Word"}, "Word"))
        self.assertTrue(matches({"process": "code",
                                 "title": "a - Visual Studio Code"},
                                "VS Code"))
        self.assertFalse(matches({"process": "notepad",
                                  "title": "a - Notepad"}, "Excel"))
        # A known application word must not be found inside an unrelated
        # window title: this used to make `app=Word` close a browser window.
        self.assertFalse(matches(
            {"process": "chrome", "title": "Password manager - Chrome"},
            "Word"))
        # UWP apps share ApplicationFrameHost; their title is their identity.
        self.assertTrue(matches(
            {"process": "applicationframehost", "title": "Calculator"},
            "Calculator"))

    def test_cyrillic_window_titles_match(self):
        self.assertTrue(close_file_tool._title_has_name(
            "Отчёт за 2026 год.docx - Word", "Отчёт за 2026 год.docx"))
        self.assertTrue(close_file_tool._title_contains(
            "Рабочая папка", "рабочая папка"))
        self.assertFalse(close_file_tool._title_has_name(
            "Отчёт за 2026 год.docx.bak - Notepad", "Отчёт за 2026 год.docx"))

    @unittest.skipUnless(os.name == "nt", "Windows path conversion")
    def test_file_urls_decode_to_real_paths(self):
        path = close_file_tool._path_from_url(
            "file:///C:/Users/delux/%D0%9F%D0%B0%D0%BF%D0%BA%D0%B0")
        self.assertEqual(os.path.normcase(path),
                         os.path.normcase("C:\\Users\\delux\\Папка"))
        self.assertEqual(close_file_tool._path_from_url("about:blank"), "")


if __name__ == "__main__":
    unittest.main()

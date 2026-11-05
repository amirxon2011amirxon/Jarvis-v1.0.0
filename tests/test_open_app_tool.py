"""Tests for actions/computer_control/open_app_tool.py.

Discovery and launching are mocked or pointed at temporary folders. No test
opens a real application or enumerates the real Start Menu.
Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions import open_app_tool as tool


def entry(name, kind="shortcut", launch=None):
    return {"name": name, "kind": kind,
            "launch": launch or rf"C:\Start Menu\{name}.lnk"}


class NormalisationAndScoringTests(unittest.TestCase):
    def test_norm_is_case_and_punctuation_insensitive(self):
        self.assertEqual(tool._norm("  Telegram.EXE "), "telegram")
        self.assertEqual(tool._norm("Visual Studio Code!"), "visual studio code")
        self.assertEqual(tool._norm("Paint (64-bit)"), "paint 64 bit")

    def test_search_score_ranks_exact_over_prefix_over_substring(self):
        app = entry("Visual Studio Code")
        exact = tool._search_score("Visual Studio Code", app)
        prefix = tool._search_score("Visual", app)
        substring = tool._search_score("Studio", app)
        self.assertEqual(exact, 100.0)
        self.assertGreater(prefix, substring)
        self.assertGreater(substring, 0)

    def test_search_score_uses_launch_metadata(self):
        app = {"name": "Terminal", "kind": "uwp",
               "launch": "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"}
        self.assertEqual(tool._search_score("windowsterminal", app), 55.0)


class DiscoveryTests(unittest.TestCase):
    def test_collect_shortcuts_finds_nested_and_skips_maintenance(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "Programs"
            nested = base / "Nested Folder"
            admin = base / "Administrative Tools"
            nested.mkdir(parents=True)
            admin.mkdir()
            for path in (
                base / "Telegram.lnk",
                nested / "Visual Studio Code.lnk",
                base / "Uninstall Tool.lnk",
                base / "README.lnk",
                admin / "Services.lnk",
            ):
                path.write_text("", encoding="utf-8")

            entries = tool._collect_shortcuts([base])

        self.assertEqual(
            sorted(e["name"] for e in entries),
            ["Telegram", "Visual Studio Code"])
        self.assertTrue(all(e["kind"] == "shortcut" for e in entries))

    def test_collect_appsfolder_filters_non_apps(self):
        data = [
            {"Name": "Calculator",
             "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
            {"Name": "Website", "AppID": "https://example.com"},
            {"Name": "Mail", "AppID": "mailto:someone@example.com"},
            {"Name": "Manual", "AppID": r"C:\Docs\manual.pdf"},
            {"Name": "Uninstall Foo", "AppID": "foo"},
        ]
        with mock.patch.object(tool, "_run_powershell_json", return_value=data):
            entries = tool._collect_appsfolder()

        self.assertEqual([e["name"] for e in entries], ["Calculator"])
        self.assertEqual(entries[0]["kind"], "uwp")

    def test_collect_appsfolder_falls_back_to_start_apps(self):
        fallback = [entry("Calculator", kind="uwp")]
        with mock.patch.object(tool, "_run_powershell_json", return_value=None), \
             mock.patch.object(
                tool, "_collect_start_apps", return_value=fallback) as start:
            entries = tool._collect_appsfolder()

        self.assertEqual(entries, fallback)
        start.assert_called_once_with()

    def test_collect_start_apps_filters_non_apps(self):
        data = [
            {"Name": "Settings", "AppID": "Microsoft.Windows.Settings!App"},
            {"Name": "Remove Me", "AppID": "remove"},
        ]
        with mock.patch.object(tool, "_run_powershell_json", return_value=data):
            entries = tool._collect_start_apps()

        self.assertEqual([e["name"] for e in entries], ["Settings"])

    def test_build_index_prefers_shortcut_over_duplicate_uwp(self):
        shortcut = entry("Calculator", kind="shortcut", launch=r"C:\Calc.lnk")
        uwp = entry("Calculator", kind="uwp", launch="Microsoft.Calculator!App")
        with mock.patch.object(tool, "_shortcut_dirs", return_value=[]), \
             mock.patch.object(
                tool, "_collect_shortcuts", return_value=[shortcut]), \
             mock.patch.object(
                tool, "_collect_appsfolder", return_value=[uwp]):
            entries = tool._build_index()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "shortcut")

    def test_index_caches_until_refresh(self):
        first = [entry("Telegram")]
        second = [entry("Calculator")]
        old_index, old_at = tool._INDEX, tool._INDEX_AT
        try:
            tool._INDEX, tool._INDEX_AT = [], 0.0
            with mock.patch.object(
                    tool, "_build_index", side_effect=[first, second]) as build:
                self.assertEqual(tool._index(), first)
                self.assertEqual(tool._index(), first)
                self.assertEqual(tool._index(refresh=True), second)
            self.assertEqual(build.call_count, 2)
        finally:
            tool._INDEX, tool._INDEX_AT = old_index, old_at

    def test_a_failed_refresh_keeps_the_previous_catalogue(self):
        first = [entry("Telegram")]
        old_index, old_at = tool._INDEX, tool._INDEX_AT
        try:
            tool._INDEX, tool._INDEX_AT = list(first), 0.0
            with mock.patch.object(tool, "_build_index", return_value=[]):
                self.assertEqual(tool._index(refresh=True), first)
            self.assertEqual(tool._INDEX, first)
        finally:
            tool._INDEX, tool._INDEX_AT = old_index, old_at

    def test_prefetch_warms_the_catalogue_off_the_calling_thread(self):
        old_index, old_at = tool._INDEX, tool._INDEX_AT
        old_thread = tool._PREFETCH_THREAD
        try:
            tool._INDEX, tool._INDEX_AT = [], 0.0
            tool._PREFETCH_THREAD = None
            with mock.patch.object(tool, "_IS_WIN", True), \
                 mock.patch.object(
                     tool, "_build_index",
                     return_value=[entry("Telegram")]):
                tool.prefetch()
                thread = tool._PREFETCH_THREAD
                self.assertIsNotNone(thread)
                thread.join(2.0)
            self.assertEqual([e["name"] for e in tool._INDEX], ["Telegram"])
        finally:
            tool._INDEX, tool._INDEX_AT = old_index, old_at
            tool._PREFETCH_THREAD = old_thread

    def test_get_installed_programs_reports_launch_targets(self):
        entries = [
            entry("Telegram", kind="shortcut", launch=r"C:\Telegram.lnk"),
            entry("Calculator", kind="uwp", launch="Microsoft.Calculator!App"),
        ]
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_index", return_value=entries):
            programs = tool.get_installed_programs()

        self.assertEqual(programs[0], {"name": "Telegram", "source": "shortcut",
                                       "shortcut_path": r"C:\Telegram.lnk"})
        self.assertEqual(programs[1], {"name": "Calculator", "source": "uwp",
                                       "launch": "Microsoft.Calculator!App"})

    def test_get_installed_programs_is_empty_off_windows(self):
        with mock.patch.object(tool, "_IS_WIN", False):
            self.assertEqual(tool.get_installed_programs(), [])


class PowerShellTests(unittest.TestCase):
    def test_non_windows_does_not_run_powershell(self):
        with mock.patch.object(tool, "_IS_WIN", False), \
             mock.patch.object(tool.subprocess, "run") as run:
            self.assertIsNone(tool._run_powershell_json("Get-Process"))
        run.assert_not_called()

    def test_single_object_is_wrapped_in_a_list(self):
        proc = mock.Mock(stdout='{"Name": "Calculator"}')
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(
                tool.subprocess, "run", return_value=proc) as run:
            data = tool._run_powershell_json("Get-StartApps")

        self.assertEqual(data, [{"Name": "Calculator"}])
        args = run.call_args.args[0]
        self.assertEqual(args[0], "powershell.exe")
        self.assertIn("-NoProfile", args)

    def test_list_output_is_returned(self):
        proc = mock.Mock(stdout='[{"Name": "A"}, {"Name": "B"}]')
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool.subprocess, "run", return_value=proc):
            data = tool._run_powershell_json("Get-StartApps")
        self.assertEqual([item["Name"] for item in data], ["A", "B"])

    def test_invalid_or_empty_output_returns_none(self):
        for stdout in ("", "not json", "{} trailing"):
            with self.subTest(stdout=stdout):
                proc = mock.Mock(stdout=stdout)
                with mock.patch.object(tool, "_IS_WIN", True), \
                     mock.patch.object(
                        tool.subprocess, "run", return_value=proc):
                    self.assertIsNone(tool._run_powershell_json("cmd"))

    def test_subprocess_failure_returns_none(self):
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(
                tool.subprocess, "run", side_effect=OSError("no powershell")):
            self.assertIsNone(tool._run_powershell_json("cmd"))


class ListAppsTests(unittest.TestCase):
    def setUp(self):
        self.win = mock.patch.object(tool, "_IS_WIN", True)
        self.win.start()
        self.addCleanup(self.win.stop)

    def _list(self, entries, query=""):
        with mock.patch.object(tool, "_index", return_value=entries):
            return tool.list_apps(query)

    def test_no_query_returns_sorted_full_catalogue(self):
        result = self._list([
            entry("Visual Studio Code"),
            entry("calculator"),
            entry("Telegram"),
        ])
        self.assertEqual(result["query"], "")
        self.assertEqual(result["count"], 3)
        self.assertEqual(
            result["apps"], ["calculator", "Telegram", "Visual Studio Code"])

    def test_exact_name_ranks_before_prefix_match(self):
        result = self._list([
            entry("Telegram Desktop"),
            entry("Telegram"),
            entry("Notepad"),
        ], "Telegram")
        self.assertEqual(result["apps"][:2], ["Telegram", "Telegram Desktop"])

    def test_token_query_matches_reordered_words(self):
        result = self._list([entry("Visual Studio Code")], "code studio")
        self.assertEqual(result["apps"], ["Visual Studio Code"])

    def test_small_typo_still_finds_the_app(self):
        result = self._list([entry("Telegram")], "telegrm")
        self.assertEqual(result["apps"], ["Telegram"])

    def test_unrelated_query_returns_nothing(self):
        result = self._list([entry("Telegram")], "zzzz")
        self.assertEqual(result["apps"], [])
        self.assertEqual(result["count"], 0)

    def test_results_are_capped_at_fifteen(self):
        entries = [entry(f"App {i:02d}") for i in range(1, 21)]
        result = self._list(entries, "App")
        self.assertEqual(len(result["apps"]), 15)
        self.assertEqual(result["apps"][0], "App 01")

    def test_non_windows_returns_a_note(self):
        with mock.patch.object(tool, "_IS_WIN", False):
            result = tool.list_apps("Telegram")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["apps"], [])
        self.assertIn("Windows", result["note"])


class OpenAppTests(unittest.TestCase):
    def test_opens_exact_name_case_and_punctuation_insensitively(self):
        entries = [entry("Telegram", launch=r"C:\Telegram.lnk")]
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_index", return_value=entries), \
             mock.patch.object(tool, "_launch") as launch:
            result = tool.open_app("telegram.")

        self.assertEqual(result, {"opened": "Telegram", "via": "shortcut"})
        launch.assert_called_once_with(entries[0])

    def test_opens_an_app_by_a_cyrillic_name(self):
        # Store/system apps are registered under their system-language name;
        # the exact-name rule must work with any script, without a
        # language-specific alias table.
        entries = [entry("Мой инструмент",
                         launch="Vendor.SampleApp_8wekyb3d8bbwe!App")]
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_index", return_value=entries), \
             mock.patch.object(tool, "_launch") as launch:
            result = tool.open_app("Мой инструмент")

        self.assertEqual(result, {"opened": "Мой инструмент",
                                  "via": "shortcut"})
        launch.assert_called_once_with(entries[0])

    def test_unknown_name_returns_error_and_opens_nothing(self):
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_index", return_value=[entry("Telegram")]), \
             mock.patch.object(tool, "_launch") as launch:
            result = tool.open_app("Telegram Desktop")

        self.assertIn("error", result)
        self.assertIn("list_apps", result["error"])
        launch.assert_not_called()

    def test_empty_name_returns_error(self):
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_launch") as launch:
            result = tool.open_app("   ")
        self.assertIn("No application name", result["error"])
        launch.assert_not_called()

    def test_non_windows_returns_error(self):
        with mock.patch.object(tool, "_IS_WIN", False):
            result = tool.open_app("Telegram")
        self.assertIn("Windows", result["error"])

    def test_launch_failure_returns_structured_error(self):
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_index", return_value=[entry("Telegram")]), \
             mock.patch.object(
                tool, "_launch", side_effect=OSError("permission denied")):
            result = tool.open_app("Telegram")

        self.assertIn("Could not open 'Telegram'", result["error"])
        self.assertIn("permission denied", result["error"])

    def test_unexpected_discovery_error_is_returned(self):
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(
                tool, "_index", side_effect=RuntimeError("catalogue failed")):
            result = tool.open_app("Telegram")
        self.assertEqual(result, {"error": "catalogue failed"})


class LaunchTests(unittest.TestCase):
    def test_shortcut_is_opened_with_startfile(self):
        item = entry("Telegram", kind="shortcut", launch=r"C:\Telegram.lnk")
        with mock.patch.object(tool.os, "startfile", create=True) as startfile:
            tool._launch(item)
        startfile.assert_called_once_with(r"C:\Telegram.lnk")

    def test_uwp_real_path_is_opened_with_startfile(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "app.exe"
            target.write_text("", encoding="utf-8")
            item = entry("Calculator", kind="uwp", launch=str(target))
            with mock.patch.object(tool.os, "startfile", create=True) as startfile:
                tool._launch(item)
            startfile.assert_called_once_with(str(target))

    def test_uwp_app_id_is_opened_through_apps_folder(self):
        item = entry("Calculator", kind="uwp",
                     launch="Microsoft.Calculator_8wekyb3d8bbwe!App")
        with mock.patch.object(tool.os.path, "exists", return_value=False), \
             mock.patch.object(tool.subprocess, "Popen") as popen:
            tool._launch(item)
        popen.assert_called_once_with(
            ["explorer.exe",
             "shell:AppsFolder\\Microsoft.Calculator_8wekyb3d8bbwe!App"])

    def test_missing_generic_target_raises(self):
        item = entry("Broken", kind="other", launch=r"C:\missing.exe")
        with mock.patch.object(tool.os.path, "exists", return_value=False):
            with self.assertRaises(FileNotFoundError):
                tool._launch(item)


if __name__ == "__main__":
    unittest.main()

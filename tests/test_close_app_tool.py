"""Tests for actions/computer_control/close_app_tool.py.

Window discovery is mocked and closing is mocked: the tests never talk to real
processes, call taskkill, or close anything on the machine.
Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from unittest import mock

from actions import close_app_tool as tool


def running_app(name, process="telegram", titles=None, items=None):
    return {
        "name": name,
        "processes": [process],
        "titles": titles if titles is not None else [name],
        "items": items if items is not None else [
            {"pid": 1, "hwnd": 10, "process": process}],
    }


class WindowDiscoveryTests(unittest.TestCase):
    def test_collect_windows_filters_shell_and_empty_windows(self):
        data = [
            {"ProcessName": "Telegram", "Id": 101,
             "MainWindowTitle": "Telegram", "MainWindowHandle": 1001},
            {"ProcessName": "ApplicationFrameHost", "Id": 102,
             "MainWindowTitle": "Calculator", "MainWindowHandle": 1002},
            {"ProcessName": "SearchHost", "Id": 103,
             "MainWindowTitle": "Search", "MainWindowHandle": 1003},
            {"ProcessName": "Notepad", "Id": 104,
             "MainWindowTitle": "", "MainWindowHandle": 1004},
            {"ProcessName": "Notepad", "Id": 105,
             "MainWindowTitle": "Untitled", "MainWindowHandle": 0},
            {"ProcessName": "Protected", "Id": 999,
             "MainWindowTitle": "Secret", "MainWindowHandle": 1005},
            {"ProcessName": "Notepad", "Id": 106,
             "MainWindowTitle": "Default IME", "MainWindowHandle": 1006},
        ]
        with mock.patch.object(
                tool, "_run_powershell_json", return_value=data), \
             mock.patch.object(tool, "_protected_pids", return_value={999}):
            windows = tool._collect_windows_powershell()

        self.assertEqual([w["name"] for w in windows],
                         ["Telegram", "Calculator"])
        self.assertEqual(windows[0]["pid"], 101)
        self.assertEqual(windows[0]["hwnd"], 1001)
        self.assertEqual(windows[1]["process"], "ApplicationFrameHost")

    def test_native_window_enumerator_avoids_powershell(self):
        raw = [{"process": "telegram", "title": "Telegram",
                "pid": 101, "hwnd": 1001}]
        from actions import close_file_tool

        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(close_file_tool, "_windows",
                               return_value=raw), \
             mock.patch.object(tool, "_run_powershell_json") as powershell:
            windows = tool._collect_windows()

        self.assertEqual(windows[0]["name"], "telegram")
        self.assertEqual(windows[0]["title"], "Telegram")
        powershell.assert_not_called()

    def test_group_dedupes_titles_processes_and_windows(self):
        windows = [
            {"name": "Telegram", "process": "telegram", "pid": 1,
             "hwnd": 10, "title": "Telegram"},
            {"name": "telegram", "process": "telegram", "pid": 1,
             "hwnd": 10, "title": "Telegram"},
            {"name": "Telegram", "process": "telegram", "pid": 2,
             "hwnd": 11, "title": "Chat"},
        ]
        groups = tool._group(windows)

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["name"], "Telegram")
        self.assertEqual(group["titles"], ["Telegram", "Chat"])
        self.assertEqual(group["processes"], ["telegram"])
        self.assertEqual(len(group["items"]), 2)

    def test_clip_collapses_whitespace_and_truncates(self):
        self.assertEqual(tool._clip("  hello\n world  ", 20), "hello world")
        clipped = tool._clip("x" * 130, 120)
        self.assertEqual(len(clipped), 120)
        self.assertTrue(clipped.endswith("…"))

    def test_index_caches_until_refresh(self):
        window = {"name": "Telegram", "process": "telegram", "pid": 1,
                  "hwnd": 10, "title": "Telegram"}
        old_index, old_at = tool._INDEX, tool._INDEX_AT
        try:
            tool._INDEX, tool._INDEX_AT = [], 0.0
            with mock.patch.object(
                    tool, "_collect_windows", return_value=[window]) as collect:
                self.assertEqual(tool._index(refresh=True)[0]["name"], "Telegram")
                self.assertEqual(tool._index()[0]["name"], "Telegram")
            self.assertEqual(collect.call_count, 1)
        finally:
            tool._INDEX, tool._INDEX_AT = old_index, old_at

    def test_list_running_apps_is_sorted_and_limits_titles(self):
        entries = [
            running_app("Visual Studio Code", process="code",
                        titles=["one", "two", "three", "four"]),
            running_app("Calculator", process="CalculatorApp"),
        ]
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_index", return_value=entries) as index:
            result = tool.list_running_apps()

        self.assertEqual([a["name"] for a in result["apps"]],
                         ["Calculator", "Visual Studio Code"])
        self.assertEqual(result["apps"][1]["titles"], ["one", "two", "three"])
        index.assert_called_once_with(refresh=True)

    def test_list_running_apps_non_windows_returns_note(self):
        with mock.patch.object(tool, "_IS_WIN", False):
            result = tool.list_running_apps()
        self.assertEqual(result["apps"], [])
        self.assertIn("Windows", result["note"])

    def test_get_running_programs_exposes_pids_and_windows(self):
        entries = [running_app(
            "Telegram", items=[{"pid": 7, "hwnd": 70, "process": "telegram"}])]
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_index", return_value=entries):
            programs = tool.get_running_programs()

        self.assertEqual(programs[0]["name"], "Telegram")
        self.assertEqual(programs[0]["pids"], [7])
        self.assertEqual(programs[0]["windows"], [70])

    def test_get_running_programs_is_empty_off_windows(self):
        with mock.patch.object(tool, "_IS_WIN", False):
            self.assertEqual(tool.get_running_programs(), [])


class PowerShellTests(unittest.TestCase):
    def test_non_windows_does_not_run_powershell(self):
        with mock.patch.object(tool, "_IS_WIN", False), \
             mock.patch.object(tool.subprocess, "run") as run:
            self.assertIsNone(tool._run_powershell_json("Get-Process"))
        run.assert_not_called()

    def test_single_object_is_wrapped_in_a_list(self):
        proc = mock.Mock(stdout='{"ProcessName": "Telegram"}')
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(
                tool.subprocess, "run", return_value=proc) as run:
            data = tool._run_powershell_json("Get-Process")

        self.assertEqual(data, [{"ProcessName": "Telegram"}])
        args = run.call_args.args[0]
        self.assertEqual(args[0], "powershell.exe")
        self.assertIn("-NoProfile", args)

    def test_invalid_output_returns_none(self):
        proc = mock.Mock(stdout="not json")
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool.subprocess, "run", return_value=proc):
            self.assertIsNone(tool._run_powershell_json("cmd"))


class CloseHelperTests(unittest.TestCase):
    def test_window_alive_uses_is_window(self):
        fake_windll = mock.Mock()
        fake_windll.user32.IsWindow.return_value = 1
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool.ctypes, "windll", fake_windll, create=True):
            self.assertTrue(tool._window_alive(123))
        fake_windll.user32.IsWindow.assert_called_once_with(123)

        fake_windll.user32.IsWindow.return_value = 0
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool.ctypes, "windll", fake_windll, create=True):
            self.assertFalse(tool._window_alive(123))

    def test_window_alive_rejects_missing_handle_and_other_platforms(self):
        self.assertFalse(tool._window_alive(0))
        with mock.patch.object(tool, "_IS_WIN", False):
            self.assertFalse(tool._window_alive(123))

    def test_process_alive_reads_the_exit_code(self):
        fake_windll = mock.Mock()
        fake_windll.kernel32.OpenProcess.return_value = 123

        def get_exit_code(handle, code_ref):
            code_ref._obj.value = tool._STILL_ACTIVE
            return 1

        fake_windll.kernel32.GetExitCodeProcess.side_effect = get_exit_code
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool.ctypes, "windll", fake_windll, create=True):
            self.assertTrue(tool._process_alive(42))

        fake_windll.kernel32.OpenProcess.assert_called_once_with(
            tool._PROCESS_QUERY_LIMITED_INFORMATION, False, 42)
        fake_windll.kernel32.CloseHandle.assert_called_once_with(123)

    def test_process_alive_handles_dead_or_unopenable_process(self):
        fake_windll = mock.Mock()
        fake_windll.kernel32.OpenProcess.return_value = 0
        fake_windll.kernel32.GetLastError.return_value = 87  # invalid parameter
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool.ctypes, "windll", fake_windll, create=True):
            self.assertFalse(tool._process_alive(42))

        # Access denied does not prove the process exited; report it as alive
        # so close_app says "closing" rather than claiming a false success.
        fake_windll.kernel32.GetLastError.return_value = 5   # access denied
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool.ctypes, "windll", fake_windll, create=True):
            self.assertTrue(tool._process_alive(42))

        fake_windll.kernel32.OpenProcess.return_value = 123

        def exited(handle, code_ref):
            code_ref._obj.value = 0
            return 1

        fake_windll.kernel32.GetExitCodeProcess.side_effect = exited
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool.ctypes, "windll", fake_windll, create=True):
            self.assertFalse(tool._process_alive(42))
    def test_find_running_matches_name_process_and_title(self):
        item = running_app("Visual Studio Code", process="Code",
                           titles=["main.py - Visual Studio Code"])
        with mock.patch.object(tool, "_index", return_value=[item]):
            self.assertIs(tool._find_running("visual studio code"), item)
            self.assertIs(tool._find_running("code"), item)
            self.assertIs(tool._find_running(
                "main.py - Visual Studio Code"), item)
            self.assertIsNone(tool._find_running("Calculator"))

    def test_empty_name_does_not_scan_windows(self):
        with mock.patch.object(tool, "_index") as index:
            self.assertIsNone(tool._find_running("   "))
        index.assert_not_called()

    def test_explorer_close_uses_window_message_not_taskkill(self):
        with mock.patch.object(tool, "_post_close") as post, \
             mock.patch.object(tool.subprocess, "run") as run:
            tool._request_close(
                {"process": "explorer", "pid": 1, "hwnd": 10})
        post.assert_called_once_with(10)
        run.assert_not_called()

    def test_uwp_frame_close_uses_window_message_not_taskkill(self):
        with mock.patch.object(tool, "_post_close") as post, \
             mock.patch.object(tool.subprocess, "run") as run:
            tool._request_close(
                {"process": "applicationframehost", "pid": 1, "hwnd": 10})
        post.assert_called_once_with(10)
        run.assert_not_called()

    def test_normal_app_close_uses_graceful_taskkill(self):
        with mock.patch.object(tool.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run, \
             mock.patch.object(tool, "_post_close") as post:
            tool._request_close(
                {"process": "telegram", "pid": 42, "hwnd": 10})
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["taskkill", "/PID", "42"])
        self.assertEqual(kwargs["timeout"], 10)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        post.assert_not_called()

    def test_taskkill_refusal_falls_back_to_window_message(self):
        # check=False makes a refusal a return code rather than an exception.
        with mock.patch.object(
                tool.subprocess, "run",
                return_value=mock.Mock(returncode=5)), \
             mock.patch.object(tool, "_post_close") as post:
            tool._request_close(
                {"process": "telegram", "pid": 42, "hwnd": 10})
        post.assert_called_once_with(10)

    def test_taskkill_failure_falls_back_to_window_message(self):
        with mock.patch.object(
                tool.subprocess, "run", side_effect=OSError("denied")), \
             mock.patch.object(tool, "_post_close") as post:
            tool._request_close(
                {"process": "telegram", "pid": 42, "hwnd": 10})
        post.assert_called_once_with(10)

    def test_still_open_dispatches_to_window_or_process(self):
        with mock.patch.object(tool, "_window_alive", return_value=True) as window, \
             mock.patch.object(tool, "_process_alive") as process:
            self.assertTrue(tool._still_open(
                {"process": "explorer", "pid": 1, "hwnd": 10}))
            self.assertTrue(tool._still_open(
                {"process": "applicationframehost", "pid": 2, "hwnd": 20}))
        self.assertEqual(window.call_count, 2)
        process.assert_not_called()

        with mock.patch.object(tool, "_window_alive") as window, \
             mock.patch.object(tool, "_process_alive", return_value=True) as process:
            self.assertTrue(tool._still_open(
                {"process": "telegram", "pid": 3, "hwnd": 30}))
        process.assert_called_once_with(3)
        window.assert_not_called()

    def test_post_close_uses_windows_message_api(self):
        fake_windll = mock.Mock()
        fake_windll.user32.PostMessageW.return_value = 1
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool.ctypes, "windll", fake_windll, create=True):
            self.assertTrue(tool._post_close(123))
        fake_windll.user32.PostMessageW.assert_called_once_with(
            123, tool._WM_CLOSE, 0, 0)

        fake_windll.user32.PostMessageW.return_value = 0
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool.ctypes, "windll", fake_windll, create=True):
            self.assertFalse(tool._post_close(123))

    def test_post_close_rejects_missing_handle(self):
        self.assertFalse(tool._post_close(0))

    def test_explorer_is_not_protected_just_for_being_the_launcher(self):
        terminal = mock.Mock(pid=111)
        terminal.name.return_value = "WindowsTerminal.exe"
        explorer = mock.Mock(pid=222)
        explorer.name.return_value = "explorer.exe"
        process = mock.Mock()
        process.parents.return_value = [terminal, explorer]

        old = tool._PROTECTED_PIDS
        tool._PROTECTED_PIDS = None
        try:
            with mock.patch("psutil.Process", return_value=process):
                protected = tool._protected_pids()
        finally:
            tool._PROTECTED_PIDS = old

        self.assertIn(tool.os.getpid(), protected)
        self.assertIn(111, protected)
        self.assertNotIn(222, protected)


class CloseAppTests(unittest.TestCase):
    def test_close_success(self):
        app = running_app("Telegram")
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_find_running", return_value=app), \
             mock.patch.object(tool, "_request_close") as request, \
             mock.patch.object(tool, "_still_open", return_value=False):
            result = tool.close_app("Telegram")

        self.assertEqual(result, {"closed": "Telegram"})
        request.assert_called_once_with(app["items"][0])

    def test_close_dedupes_identical_windows(self):
        item = {"pid": 1, "hwnd": 10, "process": "telegram"}
        app = running_app("Telegram", items=[item, dict(item), {
            "pid": 2, "hwnd": 20, "process": "telegram"}])
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_find_running", return_value=app), \
             mock.patch.object(tool, "_request_close") as request, \
             mock.patch.object(tool, "_still_open", return_value=False):
            result = tool.close_app("Telegram")

        self.assertEqual(result, {"closed": "Telegram"})
        self.assertEqual(request.call_count, 2)

    def test_close_reports_when_app_is_still_running(self):
        app = running_app("Telegram")
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_find_running", return_value=app), \
             mock.patch.object(tool, "_request_close"), \
             mock.patch.object(tool, "_still_open", return_value=True), \
             mock.patch.object(
                tool.time, "time", side_effect=[100.0, 100.0, 103.0]), \
             mock.patch.object(tool.time, "sleep") as sleep:
            result = tool.close_app("Telegram")

        self.assertEqual(result["closing"], "Telegram")
        self.assertEqual(result["still_running"], ["telegram"])
        self.assertIn("still running", result["note"])
        sleep.assert_called_once_with(0.15)

    def test_not_found_refreshes_then_returns_error(self):
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(
                tool, "_find_running", side_effect=[None, None]) as find, \
             mock.patch.object(tool, "_request_close") as request:
            result = tool.close_app("Telegram")

        self.assertIn("No running app is named 'Telegram'", result["error"])
        self.assertEqual(find.call_count, 2)
        self.assertEqual(find.call_args_list[1],
                         mock.call("Telegram", refresh=True))
        request.assert_not_called()

    def test_found_on_refresh_is_closed(self):
        app = running_app("Telegram")
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(
                tool, "_find_running", side_effect=[None, app]) as find, \
             mock.patch.object(tool, "_request_close") as request, \
             mock.patch.object(tool, "_still_open", return_value=False):
            result = tool.close_app("Telegram")

        self.assertEqual(result, {"closed": "Telegram"})
        self.assertEqual(find.call_count, 2)
        request.assert_called_once()

    def test_empty_name_returns_error(self):
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(tool, "_request_close") as request:
            result = tool.close_app("  ")
        self.assertIn("No application name", result["error"])
        request.assert_not_called()

    def test_non_windows_returns_error(self):
        with mock.patch.object(tool, "_IS_WIN", False):
            result = tool.close_app("Telegram")
        self.assertIn("Windows", result["error"])

    def test_unexpected_error_is_returned(self):
        with mock.patch.object(tool, "_IS_WIN", True), \
             mock.patch.object(
                tool, "_find_running", side_effect=RuntimeError("lookup failed")):
            result = tool.close_app("Telegram")
        self.assertEqual(result, {"error": "lookup failed"})


if __name__ == "__main__":
    unittest.main()

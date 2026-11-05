"""system_control: dispatch, validation, and hardware-independent behaviour.

Hardware-touching paths (brightness, volume, mute) are patched so tests never
change the machine.  Read-only psutil calls are exercised for real against the
test process.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

import actions
from actions import tool_calling
from actions import system_control_tool as sysctl


class Endpoint:
    def __init__(self, volume=30, muted=False):
        self.volume = volume
        self.muted = muted

    def GetMasterVolumeLevelScalar(self):
        return self.volume / 100

    def SetMasterVolumeLevelScalar(self, value, _ctx):
        self.volume = round(value * 100)

    def GetMute(self):
        return 1 if self.muted else 0

    def SetMute(self, value, _ctx):
        self.muted = bool(value)


class FakeAudio:
    def __init__(self, volume=30, muted=False, accept=True):
        self.endpoint = Endpoint(volume, muted)
        self.accept = accept

    def get_output_volume(self, name=""):
        return self.endpoint.volume

    def set_output_volume(self, percent, name=""):
        if not self.accept:
            return False
        self.endpoint.volume = max(0, min(100, int(percent)))
        return True

    def _endpoint_for(self, name=""):
        return self.endpoint

    def list_devices(self, kind, refresh=False):
        return ["Speakers", "Headphones"]


class RegistrationTests(unittest.TestCase):
    def test_module_is_discovered(self):
        module = actions.get_tool_module("system_control")
        self.assertIsNotNone(module)
        self.assertIs(module.system_control, sysctl.system_control)

    def test_registry_and_schema(self):
        info = tool_calling.TOOL_REGISTRY["system_control"]
        self.assertEqual(info["parameters"]["type"], "OBJECT")
        self.assertEqual(info["parameters"]["required"], ["operation"])
        for name in ("operation", "value", "delta", "pid", "limit", "sort_by",
                     "device", "path", "page", "utility", "display_index"):
            self.assertIn(name, info["parameters"]["properties"])

    def test_declaration_is_valid_for_the_sdk(self):
        from google.genai import types

        declaration = next(
            item for item in tool_calling.get_tool_declarations()
            if item["name"] == "system_control")
        types.FunctionDeclaration(**declaration)

    def test_execute_tool_dispatch(self):
        result = tool_calling.execute_tool(
            "system_control", {"operation": "get_memory_info"})
        self.assertTrue(result["success"])
        self.assertIn("percent", result)


class ValidationTests(unittest.TestCase):
    def test_unknown_operation(self):
        result = sysctl.system_control("format_drive")
        self.assertEqual("unsupported", result["reason"])

    def test_missing_values(self):
        self.assertEqual("invalid_argument",
                         sysctl.system_control("set_brightness")["reason"])
        self.assertEqual("invalid_argument",
                         sysctl.system_control("set_volume")["reason"])
        self.assertEqual("invalid_argument",
                         sysctl.system_control("set_mute")["reason"])

    def test_non_numeric_values(self):
        self.assertEqual("invalid_argument",
                         sysctl.system_control("set_volume", value="loud")["reason"])

    def test_invalid_page_and_utility(self):
        self.assertEqual("invalid_argument",
                         sysctl.system_control("open_settings", page="rm -rf")["reason"])
        self.assertEqual("invalid_argument",
                         sysctl.system_control("open_system_utility",
                                               utility="cmd.exe /c dir")["reason"])

    def test_invalid_process_arguments(self):
        self.assertEqual("invalid_argument",
                         sysctl.system_control("get_process_details")["reason"])
        self.assertEqual("invalid_argument",
                         sysctl.system_control("get_top_processes",
                                               sort_by="disk")["reason"])

    def test_value_is_clamped(self):
        with mock.patch.object(sysctl, "_wmi_brightness_get",
                               side_effect=[50, 100]), \
             mock.patch.object(sysctl, "_wmi_brightness_set") as setter:
            result = sysctl.system_control("set_brightness", value=999)
        self.assertTrue(result["success"])
        setter.assert_called_once_with(100)


class BrightnessTests(unittest.TestCase):
    def test_get(self):
        with mock.patch.object(sysctl, "_wmi_brightness_get", return_value=40), \
             mock.patch.object(sysctl, "_ddc_monitors", return_value=[]):
            result = sysctl.system_control("get_brightness")
        self.assertTrue(result["success"])
        self.assertEqual(40, result["internal"])

    def test_set_verifies(self):
        with mock.patch.object(sysctl, "_wmi_brightness_get",
                               side_effect=[20, 60]), \
             mock.patch.object(sysctl, "_wmi_brightness_set") as setter:
            result = sysctl.system_control("set_brightness", value=60)
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        setter.assert_called_once_with(60)

    def test_set_reports_unverified_change(self):
        with mock.patch.object(sysctl, "_wmi_brightness_get",
                               side_effect=[20, 21]), \
             mock.patch.object(sysctl, "_wmi_brightness_set"):
            result = sysctl.system_control("set_brightness", value=80)
        self.assertEqual("not_verified", result["reason"])

    def test_unsupported_hardware(self):
        with mock.patch.object(sysctl, "_wmi_brightness_get", return_value=None), \
             mock.patch.object(sysctl, "_ddc_monitors", return_value=[]):
            result = sysctl.system_control("get_brightness")
            setting = sysctl.system_control("set_brightness", value=50)
        self.assertFalse(result["supported"])
        self.assertFalse(setting["supported"])
        self.assertEqual("unsupported", setting["reason"])

    def test_adjust_uses_current_value_and_clamps(self):
        with mock.patch.object(sysctl, "_wmi_brightness_get",
                               side_effect=[95, 95, 100]), \
             mock.patch.object(sysctl, "_ddc_monitors", return_value=[]), \
             mock.patch.object(sysctl, "_wmi_brightness_set") as setter:
            result = sysctl.system_control("adjust_brightness", delta=20)
        self.assertTrue(result["success"])
        self.assertEqual("adjust_brightness", result["operation"])
        setter.assert_called_once_with(100)

    def test_ddc_display_selection(self):
        applied = {"display_index": 0, "ok": True, "verified": True,
                   "brightness": 55, "name": "DELL U2720Q"}
        with mock.patch.object(sysctl, "_ddc_apply",
                               return_value={"monitors": [{"index": 0,
                                                           "name": "DELL U2720Q",
                                                           "brightness": 55}],
                                             "applied": applied,
                                             "error": ""}):
            result = sysctl.system_control("set_brightness", value=55,
                                           display_index=0)
        self.assertTrue(result["success"])
        self.assertEqual("DELL U2720Q", result["target"])

    def test_ddc_invalid_index(self):
        with mock.patch.object(sysctl, "_ddc_apply",
                               return_value={"monitors": [{"index": 0,
                                                           "name": "A",
                                                           "brightness": 50}],
                                             "applied": None, "error": ""}):
            result = sysctl.system_control("set_brightness", value=50,
                                           display_index=3)
        self.assertEqual("invalid_display", result["reason"])


class VolumeTests(unittest.TestCase):
    def setUp(self):
        self.audio = FakeAudio()
        patcher = mock.patch.object(sysctl, "_audio_module",
                                    return_value=self.audio)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_get_volume_and_mute(self):
        volume = sysctl.system_control("get_volume")
        mute = sysctl.system_control("get_mute")
        self.assertEqual(30, volume["volume"])
        self.assertTrue(volume["supported"])
        self.assertFalse(mute["muted"])

    def test_set_volume_verifies(self):
        result = sysctl.system_control("set_volume", value=55)
        self.assertTrue(result["success"])
        self.assertEqual(55, result["volume"])
        self.assertTrue(result["verified"])

    def test_adjust_volume_clamps(self):
        result = sysctl.system_control("adjust_volume", delta=100)
        self.assertTrue(result["success"])
        self.assertEqual(100, result["volume"])

    def test_mute_and_unmute(self):
        muted = sysctl.system_control("set_mute", value=1)
        self.assertTrue(muted["success"])
        self.assertTrue(muted["muted"])
        unmuted = sysctl.system_control("set_mute", value=0)
        self.assertFalse(unmuted["muted"])

    def test_volume_failure_is_reported(self):
        self.audio.accept = False
        result = sysctl.system_control("set_volume", value=40)
        self.assertEqual("failed", result["reason"])

    def test_audio_devices(self):
        result = sysctl.system_control("get_audio_devices")
        self.assertTrue(result["success"])
        self.assertEqual(["Speakers", "Headphones"], result["devices"])

    def test_switching_output_is_unsupported_not_faked(self):
        result = sysctl.system_control("set_audio_output", device="Headphones")
        self.assertFalse(result["success"])
        self.assertEqual("unsupported", result["reason"])
        self.assertIn("Headphones", result["devices"])


class ProcessTests(unittest.TestCase):
    @staticmethod
    def call(function, *args, **kwargs):
        try:
            function(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - mirrors dispatcher mapping
            return exc
        return None

    def test_top_processes_and_self_details(self):
        top = sysctl.system_control("get_top_processes", limit=3,
                                    sort_by="memory")
        self.assertTrue(top["success"])
        self.assertLessEqual(len(top["processes"]), 3)
        self.assertTrue(any(row.get("memory_mb") for row in top["processes"]))

        details = sysctl.system_control("get_process_details", pid=os.getpid())
        self.assertTrue(details["success"])
        self.assertEqual(os.getpid(), details["process"]["pid"])

    def test_missing_process(self):
        result = sysctl.system_control("get_process_details", pid=999999)
        self.assertEqual("not_found", result["reason"])

    def test_permission_denied_is_mapped(self):
        with mock.patch.object(sysctl, "_process_details",
                               side_effect=PermissionError("denied")):
            result = sysctl.system_control("get_process_details", pid=4)
        self.assertEqual("denied", result["reason"])

    def test_diagnose_structure(self):
        data = sysctl.system_control("diagnose_performance")
        self.assertTrue(data["success"])
        for key in ("cpu_percent", "memory_percent", "findings", "top_cpu",
                    "top_memory"):
            self.assertIn(key, data)
        self.assertTrue(data["findings"])

    def test_cmdline_secrets_are_redacted(self):
        text = sysctl._redact_cmdline(
            ["app.exe", "--token=abc123", "--password=hunter2", "normal"])
        self.assertNotIn("abc123", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("--token=***", text)


class InfoAndNavigationTests(unittest.TestCase):
    def test_system_memory_disk_network(self):
        system = sysctl.system_control("get_system_info")
        memory = sysctl.system_control("get_memory_info")
        disk = sysctl.system_control("get_disk_info")
        network = sysctl.system_control("get_network_info")
        self.assertTrue(system["success"])
        self.assertGreater(memory["total_gb"], 0)
        self.assertTrue(disk["success"])
        self.assertIn("drives", disk)
        self.assertTrue(network["success"])
        self.assertIn("interfaces", network)

    def test_settings_allowlist_hit(self):
        with mock.patch.object(sysctl, "_start_target") as starter:
            result = sysctl.system_control("open_settings", page="display")
        self.assertTrue(result["success"])
        starter.assert_called_once_with("ms-settings:display")
        self.assertFalse(result["setting_changed"])

    def test_utility_allowlist_hit(self):
        with mock.patch.object(sysctl, "_start_target") as starter:
            result = sysctl.system_control("open_system_utility",
                                           utility="task_manager")
        self.assertTrue(result["success"])
        starter.assert_called_once_with("taskmgr")


class WorkstationControlTests(unittest.TestCase):
    def test_lock_success_is_reported(self):
        with mock.patch.object(sysctl, "IS_WINDOWS", True), \
             mock.patch.object(sysctl, "_windows_lock_workstation",
                               return_value=True):
            result = sysctl.system_control("lock_workstation")
        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "lock_workstation")
        self.assertTrue(result["locked"])
        self.assertTrue(result["session_locked"])
        self.assertNotIn("logout", result.get("message", "").casefold())

    def test_lock_failure_is_reported(self):
        with mock.patch.object(sysctl, "IS_WINDOWS", True), \
             mock.patch.object(sysctl, "_windows_lock_workstation",
                               return_value=False):
            result = sysctl.system_control("lock_workstation")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "failed")

    def test_workstation_actions_are_windows_only(self):
        with mock.patch.object(sysctl, "IS_WINDOWS", False):
            lock = sysctl.system_control("lock_workstation")
            sleep = sysctl.system_control("sleep_workstation")
        self.assertEqual(lock["reason"], "unsupported")
        self.assertEqual(sleep["reason"], "unsupported")
        self.assertIn("Windows", lock["error"])

    def test_sleep_success_reports_session_ending_action(self):
        with mock.patch.object(sysctl, "IS_WINDOWS", True), \
             mock.patch.object(sysctl, "_windows_sleep_workstation",
                               return_value=True):
            result = sysctl.system_control("sleep_workstation")
        self.assertTrue(result["success"])
        self.assertTrue(result["sleep_requested"])
        self.assertTrue(result["session_may_close"])
        self.assertTrue(result["terminal"])
        self.assertIn("sleep", result["message"].casefold())

    def test_unavailable_sleep_state_is_not_substituted(self):
        with mock.patch.object(sysctl, "IS_WINDOWS", True), \
             mock.patch.object(sysctl, "_windows_sleep_workstation",
                               return_value=False):
            result = sysctl.system_control("sleep_workstation")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "unavailable")
        self.assertIn("nothing was", result["error"].casefold())
        for forbidden in ("shutdown", "restart", "log out", "hibernate"):
            self.assertNotIn(forbidden, result.get("message", "").casefold())

    def test_sleep_api_failure_is_reported(self):
        with mock.patch.object(sysctl, "IS_WINDOWS", True), \
             mock.patch.object(sysctl, "_windows_sleep_workstation",
                               side_effect=OSError("power API unavailable")):
            result = sysctl.system_control("sleep_workstation")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "unavailable")
        self.assertIn("power API unavailable", result["error"])

    def test_sleep_uses_a_session_ending_timeout(self):
        sleep_timeout = tool_calling._call_timeout(
            "system_control", {"operation": "sleep_workstation"})
        lock_timeout = tool_calling._call_timeout(
            "system_control", {"operation": "lock_workstation"})
        self.assertGreater(sleep_timeout, 86_400)
        self.assertLess(lock_timeout, 60)

    def test_declaration_exposes_the_new_operations(self):
        info = tool_calling.TOOL_REGISTRY["system_control"]
        description = info["description"]
        self.assertIn("lock_workstation", description)
        self.assertIn("sleep_workstation", description)
        names = [item["name"] for item in tool_calling.get_tool_declarations()]
        self.assertIn("system_control", names)


class SubprocessTests(unittest.TestCase):
    def test_timeout_is_handled(self):
        with mock.patch.object(sysctl.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("x", 1)):
            code, out, err = sysctl._run(["x"], timeout=1)
        self.assertEqual(-1, code)
        self.assertIn("Timed out", err)

    def test_missing_binary_is_handled(self):
        code, out, err = sysctl._run(["definitely-not-a-real-binary-xyz"])
        self.assertEqual(-1, code)
        self.assertIn("not available", err)


if __name__ == "__main__":
    unittest.main()

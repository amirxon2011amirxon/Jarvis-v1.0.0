"""Settings must survive a hand-edited, valid-JSON-wrong-shape file.

``config/api_keys.json`` is intentionally user-editable.  A list, string or
``null`` is not a settings object; it must behave like a missing file instead of
making every ``.get`` caller raise (which previously broke startup).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config


class IsolatedConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "api_keys.json"
        self.patch = mock.patch.multiple(
            config,
            CONFIG_DIR=self.directory,
            CONFIG_FILE=self.path,
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, value) -> None:
        self.path.write_text(json.dumps(value), encoding="utf-8")


class WrongShapeTests(IsolatedConfig):
    def test_non_object_json_loads_as_empty_settings(self):
        for value in ([], ["a"], "text", 17, None):
            with self.subTest(value=value):
                self.write(value)
                self.assertEqual(config.load_api_keys(), {})
                self.assertIsNone(config.get_gemini_key())
                self.assertEqual(config.get_assistant_name(), "Jarvis")
                self.assertEqual(config.get_input_device(), "")
                self.assertEqual(config.get_brave_path(), "")
                self.assertEqual(config.get_os(), config._platform_os())

    def test_patch_replaces_a_non_object_file_with_a_settings_object(self):
        for value in ([], "text", 17, None):
            with self.subTest(value=value):
                self.write(value)
                config._patch_config(assistant_name="Jarvis")
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.assertEqual(saved, {"assistant_name": "Jarvis"})

    def test_transient_windows_sharing_error_is_retried(self):
        self.write({"assistant_name": "Old"})
        attempts = {"count": 0}
        real_replace = config.os.replace

        def flaky(source, target):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise PermissionError("file is in use")
            return real_replace(source, target)

        with mock.patch.object(config.os, "replace", side_effect=flaky):
            config._patch_config(assistant_name="New")
        self.assertEqual(config.get_assistant_name(), "New")
        self.assertGreaterEqual(attempts["count"], 2)

    def test_a_failed_atomic_replace_keeps_the_previous_file(self):
        self.write({"assistant_name": "Old"})
        with mock.patch.object(config.os, "replace",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config._patch_config(assistant_name="New")
        self.assertEqual(config.get_assistant_name(), "Old")
        self.assertEqual(list(self.directory.glob(".api_keys-*.tmp")), [])


class WrongValueTypeTests(IsolatedConfig):
    def test_text_settings_do_not_raise_on_wrong_types(self):
        self.write({
            "gemini_api_key": 12345,
            "assistant_name": ["Jarvis"],
            "user_name": 99,
            "voice_name": {"name": "Kore"},
            "input_device": 7,
            "output_device": None,
            "brave_path": ["C:/Brave"],
            "os_system": 42,
            "ui_color": 123,
        })

        self.assertIsNone(config.get_gemini_key())
        self.assertEqual(config.get_assistant_name(), "Jarvis")
        self.assertEqual(config.get_user_name(), "")
        self.assertEqual(config.get_voice(), config.DEFAULT_VOICE)
        self.assertEqual(config.get_input_device(), "")
        self.assertEqual(config.get_output_device(), "")
        self.assertEqual(config.get_brave_path(), "")
        self.assertEqual(config.get_os(), config._platform_os())
        self.assertEqual(config.get_ui_color(), config.DEFAULT_UI_COLOR)

    def test_string_booleans_are_not_coerced_to_true(self):
        self.write({
            "push_to_talk_enabled": "false",
            "thinking_enabled": "0",
            "proactive_audio": "no",
            "memory": {"enabled": "false", "auto_extract": "off"},
        })
        self.assertFalse(config.get_push_to_talk_enabled())
        self.assertFalse(config.get_thinking_enabled())
        self.assertFalse(config.get_proactive_audio_enabled())
        self.assertFalse(config.get_memory_settings()["enabled"])
        self.assertFalse(config.get_memory_settings()["auto_extract"])

    def test_legacy_telephony_password_is_removed_on_save(self):
        self.write({"telephony": {"username": "jarvis",
                                  "password": "legacy-secret"}})
        config.save_telephony_settings({"username": "jarvis"})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("password", saved["telephony"])

    def test_string_settings_still_work(self):
        self.write({
            "gemini_api_key": "  test-key-abcdefghijklmnop  ",
            "assistant_name": "  Jarvis  ",
            "user_name": "  Operator ",
            "voice_name": "Kore",
            "input_device": "  Mic One ",
            "output_device": " Speaker ",
            "brave_path": r" C:\Brave\brave.exe ",
            "os_system": " WINDOWS ",
        })

        self.assertEqual(config.get_gemini_key(),
                         "test-key-abcdefghijklmnop")
        self.assertEqual(config.get_assistant_name(), "Jarvis")
        self.assertEqual(config.get_user_name(), "Operator")
        self.assertEqual(config.get_voice(), "Kore")
        self.assertEqual(config.get_input_device(), "Mic One")
        self.assertEqual(config.get_output_device(), "Speaker")
        self.assertEqual(config.get_brave_path(), r"C:\Brave\brave.exe")
        self.assertEqual(config.get_os(), "windows")
        self.assertTrue(config.is_configured())


if __name__ == "__main__":
    unittest.main()

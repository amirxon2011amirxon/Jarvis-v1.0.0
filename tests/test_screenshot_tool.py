"""Regression tests for the explicit one-shot screenshot tool.

The real display is never touched: the capture seam returns bytes.  Tests
exercise path resolution, unique naming, permission/capture failures and the
registry/declaration contract.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions import screenshot_tool as shot
from actions import tool_calling

PNG = shot.PNG_SIGNATURE + b"\x00" * 64


class ScreenshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="jarvis-screenshot-")
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def capture(self, data: bytes = PNG):
        return mock.patch.object(
            shot, "_capture_primary_png", return_value=data)

    def files(self):
        return sorted(Path(self.root).rglob("*.png"))

    def test_successful_capture_saves_a_real_png(self):
        with self.capture() as backend:
            result = shot.screenshot(self.root)
        backend.assert_called_once_with()
        self.assertTrue(result["success"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"],
                         Path(result["saved_path"]).name)
        self.assertTrue(Path(result["saved_path"]).is_absolute())
        self.assertEqual(Path(result["saved_path"]).read_bytes(), PNG)
        self.assertEqual(len(self.files()), 1)

    def test_default_directory_is_pictures_screenshots(self):
        pictures = Path(self.root) / "Pictures"
        pictures.mkdir()
        with mock.patch(
                "actions.open_file_tool._home_folder",
                return_value=str(pictures)), self.capture():
            result = shot.screenshot()
        self.assertTrue(result["success"], result)
        expected = pictures / "Screenshots"
        self.assertEqual(Path(result["saved_path"]).parent, expected)
        self.assertTrue(expected.is_dir())

    def test_custom_save_path_is_created(self):
        destination = Path(self.root) / "Projects" / "Shots"
        with self.capture():
            result = shot.screenshot(str(destination))
        self.assertTrue(result["success"], result)
        self.assertEqual(Path(result["saved_path"]).parent, destination)

    def test_filename_is_timestamped(self):
        with self.capture():
            result = shot.screenshot(self.root)
        self.assertRegex(
            result["filename"],
            r"^Screenshot_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.png$")

    def test_existing_filename_is_not_overwritten(self):
        destination = Path(self.root)
        existing = destination / "Screenshot_2026-11-05_16-30-45.png"
        existing.write_bytes(b"keep me")
        with mock.patch.object(
                shot, "_timestamp_slug", return_value="2026-11-05_16-30-45"), \
                self.capture():
            result = shot.screenshot(self.root)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["filename"],
                         "Screenshot_2026-11-05_16-30-45_2.png")
        self.assertEqual(existing.read_bytes(), b"keep me")

    def test_invalid_destination_file_is_rejected(self):
        target = Path(self.root) / "not-a-folder.txt"
        target.write_text("x", encoding="utf-8")
        with self.capture() as backend:
            result = shot.screenshot(str(target))
        self.assertFalse(result["success"])
        self.assertIn("not a folder", result["error"])
        backend.assert_not_called()

    def test_bare_known_folder_name_is_accepted(self):
        downloads = Path(self.root) / "Downloads"
        downloads.mkdir()
        with mock.patch(
                "actions.open_file_tool._home_folder",
                return_value=str(downloads)), self.capture():
            result = shot.screenshot("Downloads")
        self.assertTrue(result["success"], result)
        self.assertEqual(Path(result["saved_path"]).parent, downloads)

    def test_unknown_relative_destination_is_rejected(self):
        with self.capture() as backend:
            result = shot.screenshot("relative\\shots")
        self.assertFalse(result["success"])
        self.assertIn("absolute folder", result["error"])
        backend.assert_not_called()

    def test_permission_error_is_reported(self):
        with mock.patch.object(
                shot.Path, "mkdir", side_effect=PermissionError("denied")), \
                self.capture() as backend:
            result = shot.screenshot(self.root)
        self.assertFalse(result["success"])
        self.assertIn("permission", result["error"].lower())
        backend.assert_not_called()

    def test_capture_backend_failure_is_truthful(self):
        with mock.patch.object(
                shot, "_capture_primary_png",
                side_effect=RuntimeError("display unavailable")):
            result = shot.screenshot(self.root)
        self.assertFalse(result["success"])
        self.assertIn("display unavailable", result["error"])
        self.assertEqual(self.files(), [])

    def test_invalid_image_from_backend_is_rejected(self):
        with self.capture(b"not a png"):
            result = shot.screenshot(self.root)
        self.assertFalse(result["success"])
        self.assertIn("invalid PNG", result["error"])
        self.assertEqual(self.files(), [])

    def test_write_failure_reports_and_leaves_no_partial_file(self):
        with mock.patch.object(
                shot, "_write_exclusive",
                side_effect=PermissionError("sharing violation")), self.capture():
            result = shot.screenshot(self.root)
        self.assertFalse(result["success"])
        self.assertIn("sharing violation", result["error"])
        self.assertEqual(self.files(), [])


class RegistrationTests(unittest.TestCase):
    def test_tool_is_registered_and_declared(self):
        info = tool_calling.TOOL_REGISTRY["screenshot"]
        self.assertIs(info["func"], shot.screenshot)
        declaration = {
            item["name"]: item
            for item in tool_calling.get_tool_declarations(enabled_only=False)
        }["screenshot"]
        self.assertEqual(declaration["parameters"]["required"], [])
        self.assertIn("save_path", declaration["parameters"]["properties"])
        self.assertIn("explicitly", declaration["description"])

    def test_import_and_declaration_do_not_capture(self):
        with mock.patch.object(
                shot, "_capture_primary_png", return_value=PNG) as backend:
            tool_calling.get_tool_declarations(enabled_only=False)
            tool_calling.get_tool_catalog()
        backend.assert_not_called()

    def test_tool_module_is_discovered(self):
        self.assertIn("screenshot", tool_calling.get_tool_module_names())


if __name__ == "__main__":
    unittest.main()

"""Release consistency checks for the v1.0.0 source tree."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import config
import web


ROOT = Path(__file__).resolve().parent.parent


class ReleaseMetadataTests(unittest.TestCase):
    def test_version_is_consistent(self):
        setup_text = (ROOT / "setup.py").read_text(encoding="utf-8")
        setup_version = re.search(r'version="([^"]+)"', setup_text)
        self.assertIsNotNone(setup_version)
        frontend_version = json.loads(
            (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
        )["version"]
        self.assertEqual(setup_version.group(1), "1.0.0")
        self.assertEqual(web.__version__, "1.0.0")
        self.assertEqual(frontend_version, "1.0.0")

    def test_release_metadata_files_exist(self):
        for name in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md",
                     "LICENSE", "THIRD_PARTY_LICENSES.md",
                     "requirements.txt", "requirements-dev.txt",
                     "pyproject.toml", "MANIFEST.in"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())

    def test_console_entry_point_is_declared(self):
        setup_text = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn("jarvis=web.launch:main", setup_text)

    def test_source_tree_keeps_repository_config_and_assets(self):
        self.assertTrue(config.IS_SOURCE_TREE)
        self.assertEqual(config.DATA_DIR, config.BASE_DIR)
        self.assertTrue((web.frontend_dist() / "index.html").is_file())
        self.assertTrue((ROOT / "Skills" / "Builtin").is_dir())


if __name__ == "__main__":
    unittest.main()

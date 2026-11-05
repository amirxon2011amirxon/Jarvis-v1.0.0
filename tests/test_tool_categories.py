"""Tool category architecture: folder discovery, selection and the API.

Folder names are deliberately not treated as identities in these tests: the
same assertions must hold after a category folder is renamed. These tests never
launch an app or open a browser, and they use an isolated settings file.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

import actions as actions_package
import config
from actions import (
    close_app_tool,
    close_file_tool,
    close_web_tool,
    open_app_tool,
    open_file_tool,
    open_web_tool,
    screen_understanding_tool,
    tool_calling,
)
from web.events import EventHub
from web.runtime import AzeRuntime
from web.server import create_app

BASE = "http://127.0.0.1:8765"
TOOL_ROOT = Path(tool_calling.__file__).resolve().parent


@contextmanager
def isolated_config(values=None):
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        path = directory / "api_keys.json"
        path.write_text(json.dumps(values or {}), encoding="utf-8")
        with mock.patch.object(config, "CONFIG_DIR", directory), \
             mock.patch.object(config, "CONFIG_FILE", path):
            yield path


def catalog() -> dict:
    return tool_calling.get_tool_catalog()


def tools_by_name() -> dict:
    return {item["name"]: item for item in catalog()["tools"]}


def tool_id(name: str) -> str:
    return tools_by_name()[name]["id"]


class _Metrics:
    def snapshot(self):
        return {"cpu": 1.0, "mem": 2.0, "net": 0.0, "gpu": -1.0, "tmp": -1.0}

    def stop(self):
        pass


class _Weather:
    def get(self, force=False):
        return {"available": True, "location": "Test"}


class _Engine:
    session = object()

    def __init__(self):
        self.reconnects = []

    def request_reconnect(self, keep_context=True, reason=""):
        self.reconnects.append((keep_context, reason))


class DiscoveryTests(unittest.TestCase):
    def test_tools_are_flat_in_the_actions_root(self):
        records = tool_calling.discover_tool_modules()
        self.assertEqual(tool_calling.get_tool_count(), 17)
        self.assertEqual(
            {item["name"] for item in records}, {
                "open_app", "close_app", "open_file", "close_file",
                "open_web", "close_web", "research", "screen_understanding",
                "screenshot", "zip", "file_control", "read_document", "web_fetch",
                "reminders", "system_control", "youtube_player", "phone_call",
            })
        for record in records:
            with self.subTest(tool=record["name"]):
                self.assertEqual(record["category"], "")
                self.assertEqual(record["id"], record["name"])
                parent = Path(record["path"]).parent.relative_to(TOOL_ROOT)
                self.assertEqual(parent.as_posix(), ".")

    def test_catalog_exposes_categories_tools_and_registered_actions(self):
        with isolated_config():
            data = catalog()
            tools = tools_by_name()
        self.assertEqual(data["count"], 17)
        self.assertEqual(sum(item["count"] for item in data["categories"]), 17)
        self.assertTrue(data["all_enabled"])

        self.assertEqual(tools["open_app"]["actions"], ["list_apps", "open_app"])
        self.assertEqual(tools["close_app"]["actions"],
                         ["list_running_apps", "close_app"])
        self.assertEqual(
            tools["open_file"]["actions"],
            ["search_files", "open_file", "open_folder",
             "open_containing_folder"])
        self.assertEqual(tools["close_file"]["actions"], ["close_file"])
        self.assertEqual(tools["open_web"]["actions"], ["open_web"])
        self.assertEqual(tools["close_web"]["actions"], ["close_web"])
        self.assertEqual(tools["screen_understanding"]["actions"],
                         ["screen_understanding"])
        # Every registered action belongs to exactly one discovered module,
        # except the core memory actions, which are subsystem actions rather
        # than docked tool modules.
        assigned = sum((item["actions"] for item in data["tools"]), [])
        self.assertEqual(
            sorted(assigned),
            sorted(name for name in tool_calling.get_tool_names()
                   if name not in tool_calling._CORE_ACTION_NAMES))

    def test_no_duplicate_tool_ids_or_module_names(self):
        records = tool_calling.discover_tool_modules()
        ids = [item["id"] for item in records]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(tool_calling.get_tool_names()),
                         len(set(tool_calling.get_tool_names())))

    def test_flat_aliases_and_stable_lookup_point_at_the_same_module(self):
        modules = (
            open_app_tool, close_app_tool, open_file_tool, close_file_tool,
            open_web_tool, close_web_tool, screen_understanding_tool,
        )
        for module in modules:
            file_stem = module.__name__.rsplit(".", 1)[-1]
            tool_name = file_stem[: -len("_tool")]
            with self.subTest(tool=tool_name):
                self.assertIs(getattr(actions_package, file_stem), module)
                self.assertIs(sys.modules[f"actions.{file_stem}"], module)
                self.assertIs(actions_package.get_tool_module(tool_name), module)

    def test_a_new_category_folder_is_discovered_without_editing_a_list(self):
        folder = TOOL_ROOT / "test_pack_folder"
        self.addCleanup(shutil.rmtree, folder, True)
        folder.mkdir(exist_ok=True)
        (folder / "sample_tool.py").write_text(
            '"""A dynamically discovered test tool."""\n', encoding="utf-8")

        records = {item["id"]: item for item in
                   tool_calling.discover_tool_modules()}
        self.assertIn("test_pack_folder/sample", records)
        self.assertEqual(records["test_pack_folder/sample"]["category"],
                         "test_pack_folder")

        with isolated_config():
            data = tool_calling.set_tool_selection(["test_pack_folder"])
            self.assertEqual(
                [item["id"] for item in data["tools"] if item["enabled"]],
                ["test_pack_folder/sample"])
            labels = {item["id"]: item["label"] for item in data["categories"]}
            self.assertEqual(labels["test_pack_folder"], "Test Pack Folder")

    def test_a_root_level_tool_still_appears_after_the_folder_refactor(self):
        path = TOOL_ROOT / "legacy_demo_tool.py"
        self.addCleanup(path.unlink, True)
        path.write_text('"""Legacy flat tool."""\n', encoding="utf-8")

        records = {item["id"]: item for item in
                   tool_calling.discover_tool_modules()}
        self.assertIn("legacy_demo", records)
        self.assertEqual(records["legacy_demo"]["category"], "")

        with isolated_config():
            data = tool_calling.set_tool_selection(["legacy_demo"])
            self.assertIn("legacy_demo",
                          [item["id"] for item in data["tools"]
                           if item["enabled"]])


class LegacyFolderSelectionTests(unittest.TestCase):
    def test_saved_folder_ids_migrate_to_the_flat_tool_name(self):
        with isolated_config({
                "enabled_tools": ["web/open_web"],
                "enabled_tools_version": 2}):
            self.assertEqual(
                tool_calling.get_enabled_tool_module_ids(),
                ["open_web", "phone_call", "research", "screenshot", "zip"])
            names = [item["name"] for item in
                     tool_calling.get_tool_declarations(enabled_only=True)]
            self.assertIn("open_web", names)
            self.assertNotIn("close_web", names)

        data = catalog()
        tools = {item["name"]: item for item in data["tools"]}
        self.assertEqual(tools["open_web"]["id"], "open_web")
        self.assertEqual(tools["open_web"]["actions"], ["open_web"])
        self.assertEqual(tools["close_web"]["actions"], ["close_web"])
        self.assertIs(actions_package.get_tool_module("open_web"), open_web_tool)


class SelectionTests(unittest.TestCase):
    def test_explicit_tool_selection(self):
        selected = ["open_web", "close_web", "web_fetch"]
        with isolated_config():
            data = tool_calling.set_tool_selection(selected)
            active = sorted(item["id"] for item in data["tools"]
                            if item["enabled"])
            self.assertEqual(active, sorted(selected))
            self.assertEqual(config.get_enabled_tool_ids(), sorted(selected))
            names = [item["name"] for item in
                     tool_calling.get_tool_declarations(enabled_only=True)]
            self.assertIn("open_web", names)
            self.assertIn("close_web", names)
            self.assertNotIn("open_file", names)

    def test_individual_tool_selection_and_empty_selection(self):
        with isolated_config():
            tool_calling.set_tool_selection([tool_id("open_file")])
            names = [item["name"] for item in
                     tool_calling.get_tool_declarations(enabled_only=True)]
            self.assertEqual(
                names,
                ["search_files", "open_file", "open_folder",
                 "open_containing_folder", "reminders",
                 "memory_remember", "memory_search", "memory_list",
                 "memory_update", "memory_forget", "memory_stats",
                 "shutdown_jarvis", "task_status", "list_skills",
                 "activate_skill", "read_skill_reference"])

            tool_calling.set_tool_selection([])
            # Core memory, lifecycle and Skills subsystems are not docked tool
            # packs, so they remain declared with every tool switched off.
            self.assertEqual(
                [item["name"] for item in
                 tool_calling.get_tool_declarations(enabled_only=True)],
                ["reminders", "memory_remember", "memory_search",
                 "memory_list",
                 "memory_update", "memory_forget", "memory_stats",
                 "shutdown_jarvis", "task_status", "list_skills",
                 "activate_skill", "read_skill_reference"])
            self.assertEqual(config.get_enabled_tool_ids(), [])

    def test_bare_names_and_mixed_selectors_are_accepted(self):
        screen_id = tool_id("screen_understanding")
        with isolated_config():
            data = tool_calling.set_tool_selection(
                ["open_web", screen_id])
            active = {item["id"] for item in data["tools"] if item["enabled"]}
            self.assertEqual(active, {tool_id("open_web"), screen_id})

    def test_unknown_selectors_are_rejected_without_writing(self):
        with isolated_config({
                "enabled_tools": ["open_web"],
                "enabled_tools_version": 2}):
            with self.assertRaises(ValueError):
                tool_calling.set_tool_selection(
                    ["open_web", "does_not_exist"])
            self.assertEqual(
                config.get_enabled_tool_ids(),
                ["open_web", "research", "phone_call", "screenshot", "zip"])

    def test_malformed_saved_selection_falls_back_to_all(self):
        with isolated_config({"enabled_tools": "not-a-list"}):
            self.assertIsNone(config.get_enabled_tool_ids())
            self.assertTrue(tool_calling.get_tool_catalog()["all_enabled"])

    def test_research_is_migrated_into_a_pre_version_selection_once(self):
        with isolated_config({"enabled_tools": ["open_web"]}) as path:
            migrated = config.get_enabled_tool_ids()
            self.assertIn("open_web", migrated)
            self.assertIn("research", migrated)
            self.assertIn("phone_call", migrated)
            self.assertIn("screenshot", migrated)
            self.assertIn("zip", migrated)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("research", saved["enabled_tools"])
            self.assertIn("phone_call", saved["enabled_tools"])
            self.assertIn("screenshot", saved["enabled_tools"])
            self.assertIn("zip", saved["enabled_tools"])
            self.assertEqual(saved["enabled_tools_version"], 5)

            # A later explicit save that leaves research out is the user's
            # choice and must not be re-migrated.
            config.save_enabled_tool_ids(["open_web"])
            self.assertEqual(config.get_enabled_tool_ids(), ["open_web"])

    def test_selection_is_persisted_and_reloaded(self):
        selected = tool_id("screen_understanding")
        with isolated_config():
            tool_calling.set_tool_selection([selected])
            with open(config.CONFIG_FILE, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["enabled_tools"], [selected])
            self.assertEqual(
                tool_calling.get_enabled_tool_module_ids(), [selected])


class LivePipelineTests(unittest.TestCase):
    def test_live_config_declares_only_the_selected_tools(self):
        from main import JarvisLive

        class UI:
            def set_assistant_name(self, name):
                pass

        with isolated_config({
            "gemini_api_key": "test-key-abcdefghijklmnop",
            "setup_done": True,
        }):
            config.save_enabled_tool_ids(
                ["open_web", "close_web", "web_fetch"])
            fake = SimpleNamespace(
                ui=UI(), _resume_handle=None, _enhanced_live=False,
                _tuned_live=False, _asst_name="Aze")
            live_config = JarvisLive._build_config(fake)
            names = {item.name for item in
                     live_config.tools[0].function_declarations}
            self.assertEqual(names, {
                "open_web", "close_web", "web_fetch", "reminders",
                "memory_remember", "memory_search", "memory_list",
                "memory_update", "memory_forget", "memory_stats",
                "shutdown_jarvis", "task_status",
                "list_skills", "activate_skill", "read_skill_reference",
            })

            config.save_enabled_tool_ids([])
            live_config = JarvisLive._build_config(fake)
            remaining = {item.name for item in
                         live_config.tools[0].function_declarations}
            self.assertEqual(remaining, {
                "reminders",
                "memory_remember", "memory_search", "memory_list",
                "memory_update", "memory_forget", "memory_stats",
                "shutdown_jarvis", "task_status",
                "list_skills", "activate_skill", "read_skill_reference",
            })


class ToolApiTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = isolated_config({"setup_done": True})
        self.config_path = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.runtime = AzeRuntime(
            hub=EventHub(), start_session=False,
            weather_service=_Weather(), metrics=_Metrics())
        self.client = TestClient(
            create_app(self.runtime,
                       frontend_dir=Path(tempfile.gettempdir()) / "aze-no-dist"),
            base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_tools_endpoint_returns_folder_catalog(self):
        data = self.client.get("/api/tools").json()
        self.assertEqual(data["count"], 17)
        self.assertEqual(
            [item["id"] for item in data["categories"]],
            [item["id"] for item in catalog()["categories"]])

    def test_selecting_tools_resolves_ids_and_reconnects(self):
        engine = _Engine()
        self.runtime._engine = engine
        expected = ["close_web", "open_web", "web_fetch"]
        response = self.client.post(
            "/api/tools/selection", json={"enabled": expected})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(
            sorted(item["id"] for item in body["tools"] if item["enabled"]),
            expected)
        self.assertEqual(engine.reconnects[-1][1], "tool selection")
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8"))
            ["enabled_tools"],
            expected)

    def test_an_unknown_selector_is_a_422(self):
        response = self.client.post(
            "/api/tools/selection", json={"enabled": ["not_a_tool"]})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Unknown tool or category", response.json()["detail"])

    def test_selection_requires_a_json_list(self):
        response = self.client.post(
            "/api/tools/selection", json={"enabled": "web"})
        self.assertEqual(response.status_code, 422)
        missing = self.client.post("/api/tools/selection", json={})
        self.assertEqual(missing.status_code, 422)

    def test_bootstrap_includes_tool_categories(self):
        data = self.client.get("/api/bootstrap").json()
        self.assertEqual(len(data["tool_categories"]),
                         len(catalog()["categories"]))
        self.assertEqual(len(data["tools"]), 17)


if __name__ == "__main__":
    unittest.main()


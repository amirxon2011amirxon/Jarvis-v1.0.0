"""Tests for the Jarvis Skills Engine.

Coverage:
* built-in discovery and manifest parsing;
* malformed/duplicate package isolation;
* validation, semantic versioning and path traversal;
* activation, duplicate prevention, multiple/dynamic activation;
* progressive disclosure and reference loading;
* enabled-state persistence across restarts;
* conversation-scoped context persistence and cleanup;
* LLM tool integration through the existing registry;
* built-in uninstall protection and installed-package removal;
* HTTP bootstrap/API and WebSocket panel synchronisation.

Run with:
    python -m unittest tests.test_skills_engine -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import yaml
from fastapi.testclient import TestClient

import config
from actions import tool_calling
from Skills.Engine import SkillsEngine, reset_engine
from web.events import EventHub
from web.runtime import AzeRuntime
from web.server import create_app

REPO = Path(__file__).resolve().parent.parent
REPO_SKILLS = REPO / "Skills"
BASE = "http://127.0.0.1:8765"


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def package_dir(root: Path, source: str, skill_id: str) -> Path:
    folder = "Builtin" if source == "builtin" else "Installed"
    return root / folder / skill_id


def write_skill(
    root: Path,
    skill_id: str,
    *,
    source: str = "builtin",
    name: str | None = None,
    version: str = "1.0.0",
    description: str | None = None,
    instructions: str | None = None,
    extra_manifest: dict | None = None,
    references: list[str] | None = None,
    resources: list[str] | None = None,
) -> Path:
    package = package_dir(root, source, skill_id)
    (package / "references").mkdir(parents=True, exist_ok=True)
    (package / "resources").mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": skill_id,
        "name": name or skill_id.replace("-", " ").title(),
        "version": version,
        "description": description or f"Specialised guidance for {skill_id}.",
        "author": "Tests",
        "license": "MIT",
        "metadata": {
            "category": "testing",
            "tags": ["test", skill_id],
        },
        "requirements": {"tools": ["file_control"]},
        "entrypoint": "SKILL.md",
    }
    if references:
        manifest["references"] = references
    if resources:
        manifest["resources"] = resources
    if extra_manifest:
        manifest.update(extra_manifest)
    (package / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    text = instructions or (
        f"# {manifest['name']}\n\n"
        "## Objective\n\n"
        f"Deliver specialised work for {skill_id} without duplicating general "
        "system instructions.\n\n"
        "## Workflow\n\n"
        "1. Inspect the request and the existing project.\n"
        "2. Apply the specialised checklist.\n"
        "3. Verify the result before reporting it.\n"
    )
    (package / "SKILL.md").write_text(text, encoding="utf-8")
    return package


def make_engine(root: Path) -> SkillsEngine:
    return SkillsEngine(root=root, state_path=root / "state.json")


def skill_ids(snapshot: dict) -> list[str]:
    return [item["id"] for item in snapshot["skills"]]


def error_codes(snapshot: dict) -> list[str]:
    return [item["code"] for item in snapshot["errors"]]


@contextmanager
def isolated_config(values: dict | None = None):
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        path = directory / "api_keys.json"
        path.write_text(json.dumps(values or {"setup_done": True}),
                        encoding="utf-8")
        with mock.patch.object(config, "CONFIG_DIR", directory), \
             mock.patch.object(config, "CONFIG_FILE", path):
            yield path


class _Weather:
    def get(self, force: bool = False) -> dict:
        return {"available": True, "location": "Test", "temperature_c": 20}


class _Metrics:
    def snapshot(self) -> dict:
        return {"cpu": 0.0, "mem": 0.0, "net": 0.0, "gpu": 0.0, "tmp": 0.0}

    def stop(self) -> None:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Discovery / validation
# ─────────────────────────────────────────────────────────────────────────────


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        (self.root / "Builtin").mkdir()
        (self.root / "Installed").mkdir()

    def test_builtin_catalog_contains_the_seven_real_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = SkillsEngine(
                root=REPO_SKILLS, state_path=Path(tmp) / "state.json")
            snapshot = engine.initialize()
            self.assertEqual(set(skill_ids(snapshot)), {
                "frontend-design",
                "web-development",
                "threejs-development",
                "web-research",
                "python-development",
                "code-review",
                "ui-ux-design",
            })
            self.assertEqual(snapshot["errors"], [])
            for row in snapshot["skills"]:
                self.assertEqual(row["version"], "1.0.0")
                self.assertEqual(row["source"], "builtin")
                self.assertTrue(row["enabled"])
                self.assertTrue(row["available"])
                self.assertTrue(row["description"])
                self.assertEqual(row["validation"]["valid"], True)
                skill_md = REPO_SKILLS / "Builtin" / row["id"] / "SKILL.md"
                self.assertGreater(len(skill_md.read_text(encoding="utf-8")),
                                   1000, row["id"])

    def test_manifest_metadata_is_parsed(self):
        package = write_skill(
            self.root, "sample-skill", references=["references/checklist.md"],
            resources=["resources/tokens.css"])
        (package / "references" / "checklist.md").write_text(
            "# Checklist\n", encoding="utf-8")
        (package / "resources" / "tokens.css").write_text(
            ":root { --x: 1; }\n", encoding="utf-8")
        engine = make_engine(self.root)
        snapshot = engine.initialize()
        row = snapshot["skills"][0]
        self.assertEqual(row["id"], "sample-skill")
        self.assertEqual(row["name"], "Sample Skill")
        self.assertEqual(row["category"], "testing")
        self.assertIn("sample-skill", row["tags"])
        self.assertEqual(row["requirements"]["tools"], ["file_control"])
        self.assertEqual(row["references"], ["references/checklist.md"])
        self.assertEqual(row["resources"], ["resources/tokens.css"])

    def test_level_one_discovery_never_reads_skill_md(self):
        write_skill(self.root, "lazy-skill")
        engine = make_engine(self.root)
        with mock.patch(
            "Skills.Engine.loader.SkillLoader.load_entrypoint",
            side_effect=AssertionError("SKILL.md must not be read at discovery"),
        ):
            snapshot = engine.initialize()
        self.assertEqual(skill_ids(snapshot), ["lazy-skill"])
        self.assertNotIn("instructions", snapshot["skills"][0])

    def test_invalid_packages_are_reported_and_valid_ones_still_load(self):
        write_skill(self.root, "good-skill")
        broken = package_dir(self.root, "builtin", "broken-yaml")
        broken.mkdir(parents=True)
        (broken / "manifest.yaml").write_text(
            "id: broken-yaml\nname: [unclosed\n", encoding="utf-8")
        (self.root / "Builtin" / "missing-manifest").mkdir()
        write_skill(self.root, "bad-version", version="one")
        engine = make_engine(self.root)
        snapshot = engine.initialize()
        self.assertIn("good-skill", skill_ids(snapshot))
        self.assertNotIn("bad-version", skill_ids(snapshot))
        codes = error_codes(snapshot)
        self.assertIn("invalid_manifest", codes)
        self.assertIn("missing_manifest", codes)
        self.assertIn("validation_failed", codes)

    def test_duplicate_ids_keep_the_first_package(self):
        write_skill(self.root, "same-id", source="builtin")
        write_skill(
            self.root, "not-the-same-folder", source="installed",
            name="Installed Same Id", extra_manifest={"id": "same-id"})
        engine = make_engine(self.root)
        snapshot = engine.initialize()
        self.assertEqual(skill_ids(snapshot), ["same-id"])
        self.assertEqual(snapshot["skills"][0]["source"], "builtin")
        self.assertIn("duplicate_id", error_codes(snapshot))

    def test_entrypoint_traversal_is_rejected(self):
        outside = self.root / "outside.md"
        outside.write_text("# outside\n", encoding="utf-8")
        write_skill(
            self.root, "escape-skill",
            extra_manifest={"entrypoint": "../../outside.md"})
        engine = make_engine(self.root)
        snapshot = engine.initialize()
        self.assertEqual(snapshot["skills"], [])
        self.assertIn("validation_failed", error_codes(snapshot))
        self.assertIn("entrypoint", snapshot["errors"][0]["message"])

    def test_declared_reference_traversal_is_rejected(self):
        write_skill(
            self.root, "bad-reference",
            extra_manifest={"references": ["../manifest.yaml"]})
        engine = make_engine(self.root)
        snapshot = engine.initialize()
        self.assertEqual(snapshot["skills"], [])
        self.assertIn("validation_failed", error_codes(snapshot))

    def test_missing_skill_md_is_invalid(self):
        package = write_skill(self.root, "no-instructions")
        (package / "SKILL.md").unlink()
        snapshot = make_engine(self.root).initialize()
        self.assertEqual(snapshot["skills"], [])
        self.assertIn("SKILL.md", snapshot["errors"][0]["message"])


# ─────────────────────────────────────────────────────────────────────────────
#  Activation / context / persistence
# ─────────────────────────────────────────────────────────────────────────────


class ActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        (self.root / "Builtin").mkdir()
        (self.root / "Installed").mkdir()
        self.package = write_skill(
            self.root, "alpha-skill",
            instructions="# Alpha\n\nAlways verify the alpha result.\n",
            references=["references/checklist.md"])
        (self.package / "references" / "checklist.md").write_text(
            "# Alpha Checklist\n\n- verify\n", encoding="utf-8")
        self.engine = make_engine(self.root)
        self.engine.initialize()
        self.engine.set_active_scope("conversation-1")

    def test_activation_loads_instructions_and_marks_active(self):
        result = self.engine.activate("alpha-skill")
        self.assertTrue(result.ok)
        self.assertFalse(result.already_active)
        self.assertIn("Always verify the alpha result", result.instructions)
        self.assertEqual(result.references, ("references/checklist.md",))
        self.assertTrue(
            self.engine.context.is_active("conversation-1", "alpha-skill"))
        snapshot = self.engine.snapshot()
        self.assertEqual(snapshot["counts"]["active"], 1)
        self.assertEqual(snapshot["active"][0]["id"], "alpha-skill")
        self.assertTrue(snapshot["skills"][0]["active"])

    def test_duplicate_activation_does_not_inject_twice(self):
        first = self.engine.activate("alpha-skill")
        second = self.engine.activate("alpha-skill")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertTrue(second.already_active)
        self.assertEqual(second.instructions, "")
        self.assertEqual(self.engine.snapshot()["counts"]["active"], 1)
        context = self.engine.session_context("conversation-1")
        self.assertEqual(context.count("Always verify the alpha result"), 1)

    def test_multiple_and_dynamic_activation(self):
        write_skill(
            self.root, "beta-skill", source="installed",
            instructions="# Beta\n\nUse the beta workflow.\n")
        self.engine.refresh()
        first = self.engine.activate("alpha-skill")
        second = self.engine.activate("beta-skill")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        snapshot = self.engine.snapshot()
        self.assertEqual(snapshot["counts"]["active"], 2)
        self.assertEqual(
            {item["id"] for item in snapshot["active"]},
            {"alpha-skill", "beta-skill"})
        context = self.engine.session_context("conversation-1")
        self.assertIn("Always verify the alpha result", context)
        self.assertIn("Use the beta workflow", context)

    def test_disabled_skill_cannot_activate_and_state_persists(self):
        self.engine.set_enabled("alpha-skill", False)
        result = self.engine.activate("alpha-skill")
        self.assertFalse(result.ok)
        self.assertIn("disabled", result.error)
        self.assertTrue((self.package / "SKILL.md").exists())
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["disabled"], ["alpha-skill"])

        restarted = make_engine(self.root)
        snapshot = restarted.initialize()
        self.assertFalse(snapshot["skills"][0]["enabled"])
        self.assertTrue(snapshot["skills"][0]["available"])
        self.assertFalse(restarted.activate("alpha-skill").ok)

        restarted.set_enabled("alpha-skill", True)
        self.assertTrue(restarted.activate("alpha-skill").ok)

    def test_disabling_an_active_skill_removes_it_from_context(self):
        self.engine.activate("alpha-skill")
        self.engine.set_enabled("alpha-skill", False)
        self.assertFalse(
            self.engine.context.is_active("conversation-1", "alpha-skill"))
        self.assertEqual(self.engine.snapshot()["counts"]["active"], 0)

    def test_unknown_skill_returns_actionable_error(self):
        result = self.engine.activate("does-not-exist")
        self.assertFalse(result.ok)
        self.assertIn("list_skills", result.error)

    def test_reference_loading_is_on_demand_and_bounded(self):
        self.engine.activate("alpha-skill")
        result = self.engine.read_reference(
            "alpha-skill", "references/checklist.md")
        self.assertIn("Alpha Checklist", result["content"])
        self.assertEqual(result["kind"], "references")

        for path in (
            "../manifest.yaml",
            "/etc/passwd",
            "C:\\Windows\\win.ini",
            "references/../../manifest.yaml",
        ):
            with self.subTest(path=path):
                blocked = self.engine.read_reference("alpha-skill", path)
                self.assertIn("error", blocked)

        not_active = self.engine.read_reference(
            "alpha-skill", "references/checklist.md",
            scope_id="another-conversation")
        self.assertIn("not active", not_active["error"])

    def test_progressive_disclosure_metadata_has_no_instructions(self):
        listed = self.engine.list_for_llm("conversation-1")
        self.assertEqual(listed["count"], 1)
        row = listed["skills"][0]
        self.assertNotIn("instructions", row)
        self.assertNotIn("Always verify the alpha result", json.dumps(row))
        self.assertNotIn("Always verify the alpha result",
                         self.engine.prompt_catalog())
        detail = self.engine.detail("alpha-skill", "conversation-1")
        self.assertIn("Always verify the alpha result", detail["instructions"])

    def test_context_survives_other_tool_calls(self):
        self.engine.activate("alpha-skill")
        self.engine.list_for_llm("conversation-1")
        self.engine.detail("alpha-skill", "conversation-1")
        self.assertTrue(
            self.engine.context.is_active("conversation-1", "alpha-skill"))
        self.assertIn("Alpha", self.engine.session_context("conversation-1"))

    def test_context_cleanup_at_conversation_boundary(self):
        self.engine.activate("alpha-skill")
        removed = self.engine.clear_scope("conversation-1")
        self.assertEqual(removed, ["alpha-skill"])
        self.assertEqual(self.engine.session_context("conversation-1"), "")
        self.assertEqual(self.engine.snapshot()["counts"]["active"], 0)

    def test_new_active_scope_clears_the_previous_conversation(self):
        self.engine.activate("alpha-skill")
        self.engine.set_active_scope("conversation-2")
        self.assertFalse(
            self.engine.context.is_active("conversation-1", "alpha-skill"))
        self.assertEqual(self.engine.snapshot()["counts"]["active"], 0)

    def test_session_context_full_and_compact_forms(self):
        self.engine.activate("alpha-skill")
        full = self.engine.session_context(
            "conversation-1", include_instructions=True)
        compact = self.engine.session_context(
            "conversation-1", include_instructions=False)
        self.assertIn("Always verify the alpha result", full)
        self.assertNotIn("Always verify the alpha result", compact)
        self.assertIn("alpha-skill", compact)
        self.assertIn("ACTIVE SKILLS", compact)

    def test_requirement_availability_is_advisory(self):
        write_skill(
            self.root, "needs-tool",
            extra_manifest={"requirements": {"tools": ["missing_tool"]}})
        self.engine.configure_tool_availability(
            lambda name: name != "missing_tool")
        self.engine.refresh()
        result = self.engine.activate("needs-tool")
        self.assertTrue(result.ok)
        self.assertTrue(any("missing_tool" in warning
                            for warning in result.warnings))
        row = next(item for item in self.engine.snapshot()["skills"]
                   if item["id"] == "needs-tool")
        self.assertIn("missing_tool", row["missing_tools"])

    def test_installed_skill_survives_a_restart(self):
        write_skill(
            self.root, "persistent-skill", source="installed",
            instructions="# Persistent\n\nSurvive a restart.\n")
        first = make_engine(self.root)
        self.assertIn("persistent-skill", skill_ids(first.initialize()))
        del first
        second = make_engine(self.root)
        snapshot = second.initialize()
        self.assertIn("persistent-skill", skill_ids(snapshot))
        row = next(item for item in snapshot["skills"]
                   if item["id"] == "persistent-skill")
        self.assertEqual(row["source"], "installed")

    def test_builtin_uninstall_is_protected(self):
        result = self.engine.uninstall("alpha-skill", confirm=True)
        self.assertFalse(result["ok"])
        self.assertIn("Built-in", result["error"])
        self.assertTrue(self.package.exists())

    def test_installed_uninstall_removes_only_that_package(self):
        installed = write_skill(
            self.root, "remove-me", source="installed")
        self.engine.refresh()
        result = self.engine.uninstall("remove-me", confirm=True)
        self.assertTrue(result["ok"])
        self.assertFalse(installed.exists())
        self.assertNotIn("remove-me", skill_ids(self.engine.snapshot()))
        self.assertTrue(self.package.exists())

    def test_uninstall_requires_confirmation(self):
        installed = write_skill(
            self.root, "confirm-me", source="installed")
        result = self.engine.uninstall("confirm-me", confirm=False)
        self.assertFalse(result["ok"])
        self.assertIn("confirm", result["error"])
        self.assertTrue(installed.exists())


# ─────────────────────────────────────────────────────────────────────────────
#  Existing tool registry integration
# ─────────────────────────────────────────────────────────────────────────────


class ToolIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        (self.root / "Builtin").mkdir()
        (self.root / "Installed").mkdir()
        package = write_skill(
            self.root, "tool-skill",
            instructions="# Tool Skill\n\nApply the tool workflow.\n",
            references=["references/guide.md"])
        (package / "references" / "guide.md").write_text(
            "# Guide\n\nUse the guide.\n", encoding="utf-8")
        self.engine = make_engine(self.root)
        self.engine.initialize()
        self.engine.set_active_scope("tool-conversation")
        self.previous = reset_engine(self.engine)

    def tearDown(self) -> None:
        try:
            self.engine.clear_all_active()
        finally:
            reset_engine(self.previous)

    def test_registry_contains_the_three_skill_tools(self):
        for name in ("list_skills", "activate_skill",
                     "read_skill_reference"):
            self.assertIn(name, tool_calling.TOOL_REGISTRY)
            declarations = {
                item["name"]: item
                for item in tool_calling.get_tool_declarations()
            }
            self.assertIn(name, declarations)

    def test_list_activate_and_read_through_execute_tool(self):
        listed = tool_calling.execute_tool("list_skills", {})
        self.assertEqual(listed["count"], 1)
        self.assertNotIn("instructions", listed["skills"][0])

        activated = tool_calling.execute_tool(
            "activate_skill", {"skill_id": "tool-skill"})
        self.assertTrue(activated["ok"])
        self.assertIn("Apply the tool workflow", activated["instructions"])

        again = tool_calling.execute_tool(
            "activate_skill", {"skill_id": "tool-skill"})
        self.assertTrue(again["already_active"])
        self.assertEqual(again.get("instructions", ""), "")

        reference = tool_calling.execute_tool(
            "read_skill_reference",
            {"skill_id": "tool-skill", "path": "references/guide.md"})
        self.assertIn("Use the guide", reference["content"])

        blocked = tool_calling.execute_tool(
            "read_skill_reference",
            {"skill_id": "tool-skill", "path": "../manifest.yaml"})
        self.assertIn("error", blocked)

        listed_again = tool_calling.execute_tool("list_skills", {})
        self.assertTrue(listed_again["skills"][0]["active"])
        self.assertEqual(listed_again["active"], ["tool-skill"])
        self.assertTrue(self.engine.context.is_active(
            "tool-conversation", "tool-skill"))

    def test_disabled_skill_is_refused_through_the_tool(self):
        self.engine.set_enabled("tool-skill", False)
        result = tool_calling.execute_tool(
            "activate_skill", {"skill_id": "tool-skill"})
        self.assertIn("error", result)
        self.assertIn("disabled", result["error"])


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP / panel integration
# ─────────────────────────────────────────────────────────────────────────────


class SkillsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.state_path = self.root / "state.json"
        self.engine = SkillsEngine(
            root=REPO_SKILLS, state_path=self.state_path)
        self.config_context = isolated_config(
            {"setup_done": True, "gemini_api_key": "test-abcdefghijklmnop"})
        self.config_context.__enter__()
        self.addCleanup(self.config_context.__exit__, None, None, None)
        self.runtime = AzeRuntime(
            hub=EventHub(), start_session=False,
            weather_service=_Weather(), metrics=_Metrics(),
            skills_engine=self.engine)
        self.client = TestClient(
            create_app(self.runtime, frontend_dir=self.root / "no-dist"),
            base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def tearDown(self) -> None:
        self.engine.clear_all_active()
        super().tearDown()

    def test_bootstrap_and_api_use_the_real_engine_snapshot(self):
        bootstrap = self.client.get("/api/bootstrap").json()
        snapshot = self.client.get("/api/skills").json()
        self.assertEqual(
            [item["id"] for item in bootstrap["skills"]["skills"]],
            [item["id"] for item in snapshot["skills"]])
        self.assertEqual(snapshot["counts"]["total"], 7)
        skills_module = next(
            item for item in bootstrap["modules"] if item["id"] == "skills")
        self.assertTrue(skills_module["available"])
        self.assertEqual(skills_module["count"], 7)
        self.assertIn("7 SKILLS LINKED", skills_module["status"])

    def test_detail_and_asset_endpoints_are_read_only_and_safe(self):
        detail = self.client.get("/api/skills/frontend-design")
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertIn("SKILL.md", body["entrypoint"])
        self.assertIn("# Frontend Design", body["instructions"])
        self.assertTrue(body["references"])

        asset = self.client.get(
            "/api/skills/frontend-design/asset",
            params={"path": "references/visual-quality-checklist.md"})
        self.assertEqual(asset.status_code, 200)
        self.assertIn("Visual Quality Checklist", asset.json()["content"])

        traversal = self.client.get(
            "/api/skills/frontend-design/asset",
            params={"path": "../manifest.yaml"})
        self.assertEqual(traversal.status_code, 404)

        unknown = self.client.get("/api/skills/not-a-skill")
        self.assertEqual(unknown.status_code, 404)

    def test_enable_disable_persists_without_touching_package(self):
        package = REPO_SKILLS / "Builtin" / "web-research"
        response = self.client.patch(
            "/api/skills/web-research", json={"enabled": False})
        self.assertEqual(response.status_code, 200)
        snapshot = response.json()["snapshot"]
        row = next(item for item in snapshot["skills"]
                   if item["id"] == "web-research")
        self.assertFalse(row["enabled"])
        self.assertTrue((package / "SKILL.md").exists())
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8"))["disabled"],
            ["web-research"])

        restarted = SkillsEngine(
            root=REPO_SKILLS, state_path=self.state_path)
        row = next(item for item in restarted.initialize()["skills"]
                   if item["id"] == "web-research")
        self.assertFalse(row["enabled"])

        enabled = self.client.patch(
            "/api/skills/web-research", json={"enabled": True})
        self.assertTrue(enabled.json()["snapshot"]["skills"][0]["enabled"])

    def test_refresh_and_builtin_uninstall_protection(self):
        refreshed = self.client.post("/api/skills/refresh", json={})
        self.assertEqual(refreshed.status_code, 200)
        self.assertTrue(refreshed.json()["snapshot"]["initialized"])

        uninstall = self.client.post(
            "/api/skills/frontend-design/uninstall", json={"confirm": True})
        self.assertEqual(uninstall.status_code, 409)
        self.assertIn("Built-in", uninstall.json()["detail"])
        self.assertTrue(
            (REPO_SKILLS / "Builtin" / "frontend-design" / "SKILL.md").exists())

    def test_websocket_publishes_live_skill_state(self):
        with self.client.websocket_connect(
            "/ws", headers={"origin": BASE, "host": "127.0.0.1:8765"}
        ) as socket:
            snapshot = socket.receive_json()
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertEqual(
                snapshot["data"]["skills"]["counts"]["total"], 7)

            self.runtime.set_skill_enabled("code-review", False)
            event = None
            for _ in range(4):
                candidate = socket.receive_json()
                if candidate.get("type") == "skills.updated":
                    event = candidate
                    break
                if candidate.get("type") == "ping":
                    break
            self.assertIsNotNone(event, "no skills.updated event arrived")
            self.assertFalse(
                next(item for item in event["data"]["skills"]
                     if item["id"] == "code-review")["enabled"])

            self.runtime.set_skill_enabled("code-review", True)

    def test_active_skill_appears_in_snapshot_and_panel_event(self):
        self.engine.set_active_scope("api-conversation")
        result = self.engine.activate("ui-ux-design")
        self.assertTrue(result.ok)
        snapshot = self.client.get("/api/skills").json()
        self.assertEqual(snapshot["counts"]["active"], 1)
        active = snapshot["active"][0]
        self.assertEqual(active["id"], "ui-ux-design")
        self.assertEqual(active["state"], "active")
        self.assertTrue(active["activated_at"])

        events = []
        self.engine.set_event_sink(events.append)
        self.engine.clear_scope("api-conversation")
        self.assertTrue(events)
        self.assertEqual(events[-1]["counts"]["active"], 0)


class MainIntegrationTests(unittest.TestCase):
    """The Live config is the point where Skill guidance enters the model."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        (self.root / "Builtin").mkdir()
        (self.root / "Installed").mkdir()
        write_skill(
            self.root, "live-skill",
            instructions="# Live Skill\n\nApply the live workflow.\n")
        self.engine = make_engine(self.root)
        self.engine.initialize()
        self.engine.set_active_scope("conversation-live")
        self.previous = reset_engine(self.engine)

    def tearDown(self) -> None:
        try:
            self.engine.clear_all_active()
        finally:
            reset_engine(self.previous)

    def test_live_config_contains_catalog_and_active_instructions(self):
        from types import SimpleNamespace
        from main import JarvisLive

        class UI:
            def set_assistant_name(self, name):
                pass

        self.assertTrue(self.engine.activate("live-skill").ok)
        fake = SimpleNamespace(
            ui=UI(),
            _resume_handle=None,
            _enhanced_live=False,
            _tuned_live=False,
            _asst_name="Jarvis",
            _conversation_id="conversation-live",
        )
        with isolated_config({
            "gemini_api_key": "test-key-abcdefghijklmnop",
            "setup_done": True,
        }):
            cfg = JarvisLive._build_config(fake)
            prompt = cfg.system_instruction
            self.assertIn("# AVAILABLE SKILLS", prompt)
            self.assertIn("live-skill", prompt)
            self.assertIn("Apply the live workflow", prompt)

            # A resumed connection already has the activation tool result; the
            # system prompt must not duplicate the full SKILL.md text.
            fake._resume_handle = "resume-handle"
            resumed = JarvisLive._build_config(fake)
            self.assertIn("# ACTIVE SKILLS", resumed.system_instruction)
            self.assertNotIn(
                "Apply the live workflow", resumed.system_instruction)


if __name__ == "__main__":
    unittest.main()

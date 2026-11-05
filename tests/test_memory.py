"""Tests for the persistent memory subsystem.

Every test uses an isolated temporary database. No test contacts a provider,
opens a device, reads the user's real settings, or touches the real database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from core.memory import (
    ACTIVE,
    MEMORY_TYPES,
    SCHEMA_VERSION,
    MemoryConflict,
    MemoryInitializationError,
    MemoryRepository,
    MemoryService,
    MemoryStorageError,
)


class FakeLLM:
    """The only LLM dependency in these tests: a deterministic stub."""

    def __init__(self, extract_payload=None, *, extract_error=False,
                 relation="separate"):
        self.extract_payload = extract_payload
        self.extract_error = extract_error
        self.relation = relation
        self.extract_calls = 0
        self.relate_calls = 0
        self.prompts: list[str] = []

    def extract(self, prompt, **_kwargs):
        self.extract_calls += 1
        self.prompts.append(prompt)
        if self.extract_error:
            raise RuntimeError("provider unavailable")
        return self.extract_payload

    def relate(self, prompt, **_kwargs):
        self.relate_calls += 1
        return {"relation": self.relation}


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis-memory-test-")
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "memory.db"
        self.services: list[MemoryService] = []

    def make_service(self, *, llm=None, auto_extract=True, **kwargs) -> MemoryService:
        service = MemoryService(
            self.db_path,
            auto_extract=auto_extract,
            backup_dir=Path(self.temp.name) / "backups",
            backup_retention=2,
            llm=llm,
            **kwargs,
        )
        self.services.append(service)
        return service

    def tearDown(self):
        for service in self.services:
            try:
                service.shutdown()
            except Exception:
                pass
        super().tearDown()


class StorageTests(MemoryTestCase):
    def test_first_and_repeated_initialization_are_idempotent(self):
        first = self.make_service()
        self.assertTrue(self.db_path.is_file())
        first_id = first.remember("I prefer concise replies.", memory_type="user").memory.id
        second = self.make_service()
        self.assertEqual(second.repository.schema_version, SCHEMA_VERSION)
        self.assertEqual(second.get_memory(first_id).status, "ok")
        self.assertEqual(second.get_memory_stats().data["total"], 1)

    def test_create_retrieve_update_delete(self):
        service = self.make_service()
        created = service.remember(
            "The user writes Python.", memory_type="user",
            title="Language", tags=["code", "python"], importance=0.8)
        self.assertTrue(created.ok)
        memory_id = created.memory.id
        got = service.get_memory(memory_id)
        self.assertEqual(got.memory.content, "The user writes Python.")
        self.assertEqual(got.memory.tags, ["code", "python"])
        updated = service.update_memory(
            memory_id, content="The user writes Python and Rust.",
            tags=["code"], expected_version=got.memory.version)
        self.assertTrue(updated.ok)
        self.assertEqual(updated.memory.version, got.memory.version + 1)
        deleted = service.delete_memory(memory_id)
        self.assertEqual(deleted.status, "deleted")
        self.assertEqual(service.get_memory(memory_id).status, "not_found")
        self.assertEqual(service.search_memories("Python").data["total"], 0)

    def test_transaction_rollback_leaves_no_row(self):
        service = self.make_service()
        with self.assertRaises(RuntimeError):
            with service.repository._write() as con:
                con.execute(
                    "INSERT INTO memories (id, type, content, title, created_at, "
                    "updated_at, source_type, source_ref, confidence, importance, "
                    "metadata, content_hash, version) VALUES "
                    "('rollback-probe','user','x','','now','now','manual','',0.7,"
                    "0.5,'{}','hash',1)")
                raise RuntimeError("abort")
        self.assertIsNone(service.repository.get("rollback-probe"))

    def test_invalid_inputs_are_rejected(self):
        service = self.make_service()
        cases = [
            ("", "user"),
            ("x" * 9000, "user"),
            ("A valid sentence.", "not-a-type"),
        ]
        for content, kind in cases:
            with self.subTest(content=content[:20], kind=kind):
                result = service.remember(content, memory_type=kind)
                self.assertFalse(result.ok)
                self.assertIn(result.status, {"invalid"})
        bad_float = service.remember("A fact.", confidence=4)
        self.assertEqual(bad_float.status, "invalid")
        self.assertEqual(service.get_memory_stats().data["total"], 0)

    def test_secrets_are_refused(self):
        service = self.make_service()
        result = service.remember("My API key is AIza" + "a" * 35,
                                  memory_type="user")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "secret_rejected")
        self.assertEqual(service.get_memory_stats().data["total"], 0)

    def test_secrets_are_refused_in_every_stored_field(self):
        service = self.make_service()
        secret = "AIza" + "a" * 35
        for kwargs in (
            {"title": secret},
            {"tags": [secret]},
            {"metadata": {"note": secret}},
        ):
            with self.subTest(field=next(iter(kwargs))):
                result = service.remember(
                    "An ordinary fact.", memory_type="user", **kwargs)
                self.assertFalse(result.ok)
                self.assertEqual(result.status, "secret_rejected")
        self.assertEqual(service.get_memory_stats().data["total"], 0)

    def test_default_memory_tools_respect_the_disabled_switch(self):
        service = self.make_service()
        with mock.patch("config.get_memory_settings",
                        return_value={"enabled": False,
                                      "db_path": self.db_path}):
            from core import memory as memory_module
            result = memory_module.tool_remember("A private fact.")
        self.assertFalse(result["ok"])
        self.assertEqual(service.get_memory_stats().data["total"], 0)

    def test_duplicate_identifier_import_is_skipped(self):
        service = self.make_service()
        memory_id = service.remember("A durable fact.").memory.id
        payload = service.export_memories()
        second = MemoryService(Path(self.temp.name) / "second.db")
        self.services.append(second)
        first = second.import_memories(payload)
        self.assertEqual(first.data["imported"], 1)
        again = second.import_memories(payload)
        self.assertEqual(again.data["imported"], 0)
        self.assertEqual(again.data["skipped"], 1)
        self.assertEqual(second.get_memory(memory_id).status, "ok")

    def test_unsupported_schema_is_not_reset(self):
        service = self.make_service()
        service.remember("Keep me.")
        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (999, '2026-01-01T00:00:00Z')")
            con.commit()
        before = self.db_path.stat().st_size
        with self.assertRaises(MemoryInitializationError):
            MemoryService(self.db_path)
        self.assertEqual(self.db_path.stat().st_size, before)
        with closing(sqlite3.connect(self.db_path)) as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)

    def test_persists_across_reinitialization(self):
        first = self.make_service()
        first.remember("Survives a restart.", memory_type="knowledge")
        second = self.make_service()
        self.assertEqual(second.search_memories("restart").data["total"], 1)

    def test_concurrent_writes_are_serialized(self):
        service = self.make_service()

        def write(index: int):
            return service.remember(
                f"Concurrent fact number {index}.", memory_type="knowledge")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write, range(32)))
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(service.get_memory_stats().data["total"], 32)


class SearchAndRetrievalTests(MemoryTestCase):
    def setUp(self):
        super().setUp()
        self.service = self.make_service()
        self.user = self.service.remember(
            "I prefer lightweight applications with low RAM use.",
            memory_type="user", importance=0.9, tags=["performance"]).memory
        self.project = self.service.remember(
            "Project Orion uses a SQLite repository.",
            memory_type="project", project_id="Orion", importance=0.8).memory
        self.other = self.service.remember(
            "The user visited a museum in spring.",
            memory_type="episodic", importance=0.3).memory

    def test_search_by_content_and_category(self):
        result = self.service.search_memories("lightweight")
        self.assertEqual(result.data["total"], 1)
        self.assertEqual(result.data["memories"][0]["id"], self.user.id)
        typed = self.service.search_memories("", memory_type="project")
        self.assertEqual([m["id"] for m in typed.data["memories"]], [self.project.id])

    def test_search_by_project(self):
        rows = self.service.search_memories("SQLite", project_id="Orion")
        self.assertEqual(rows.data["total"], 1)
        self.assertEqual(rows.data["memories"][0]["project_id"], "Orion")
        self.assertEqual(
            self.service.search_memories("SQLite", project_id="Other").data["total"], 0)

    def test_search_handles_punctuation_and_empty_input(self):
        self.service.remember(
            'The C++ API returns "beta" [v2] -- see docs.',
            memory_type="knowledge", title="C++ notes")
        for query in ('C++', '"beta"', '[v2]', '--', 'docs!', 'NEAR(foo)'):
            with self.subTest(query=query):
                result = self.service.search_memories(query)
                self.assertTrue(result.ok)
        self.assertEqual(self.service.search_memories("").data["total"], 4)
        self.assertEqual(self.service.search_memories("   ").data["total"], 4)
        self.assertTrue(self.service.search_memories("!!!").ok)

    def test_ranking_prefers_relevant_and_important(self):
        result = self.service.search_memories("lightweight RAM")
        self.assertEqual(result.data["memories"][0]["id"], self.user.id)
        self.assertGreater(result.data["memories"][0]["score"], 0)

    def test_superseded_and_deleted_are_filtered(self):
        replacement = self.service.update_memory(
            self.project.id,
            content="Project Orion now uses PostgreSQL.",
            supersede=True,
        )
        self.assertTrue(replacement.ok)
        active = self.service.search_memories("Orion")
        self.assertNotIn(self.project.id,
                         [row["id"] for row in active.data["memories"]])
        history = self.service.search_memories("Orion", include_superseded=True)
        self.assertIn(self.project.id,
                      [row["id"] for row in history.data["memories"]])
        self.service.delete_memory(self.user.id)
        self.assertEqual(self.service.search_memories("lightweight").data["total"], 0)

    def test_retrieval_is_bounded_and_empty_when_nothing_matches(self):
        result = self.service.retrieve_relevant_memories(
            "lightweight applications", limit=1, max_chars=200)
        self.assertTrue(result.context)
        self.assertLessEqual(len(result.memories), 1)
        self.assertLessEqual(len(result.context), 200)
        none = self.service.retrieve_relevant_memories("quantum banana telescope")
        self.assertEqual(none.memories, [])
        self.assertEqual(none.context, "")
        short = self.service.retrieve_relevant_memories("a")
        self.assertEqual(short.context, "")

    def test_search_index_rebuild_does_not_resurrect_deleted(self):
        self.service.delete_memory(self.other.id)
        self.service.run_maintenance(rebuild_index=True)
        result = self.service.search_memories("museum")
        self.assertEqual(result.data["total"], 0)

    def test_retrieval_filters_low_confidence(self):
        low = self.service.remember(
            "Maybe the user likes jazz?", memory_type="user",
            confidence=0.2, source_type="conversation").memory
        result = self.service.retrieve_relevant_memories("jazz")
        self.assertNotIn(low.id, [memory.id for memory in result.memories])


class ExtractionTests(MemoryTestCase):
    USER_MESSAGE = "I always prefer concise answers. Remember that."
    ASSISTANT_MESSAGE = "Understood. I will keep replies concise."

    def _payload(self):
        return {"candidates": [
            {
                "type": "user",
                "content": "The user prefers concise answers.",
                "tags": ["style"],
                "importance": 0.9,
                "confidence": 0.95,
                "explicit": True,
            },
            {
                "type": "project",
                "content": "Project Orion will use SQLite for persistence.",
                "project_id": "Orion",
                "importance": 0.8,
                "confidence": 0.9,
            },
            {
                "type": "episodic",
                "content": "The user was debugging a temporary test harness.",
                "temporary": True,
                "importance": 0.8,
                "confidence": 0.9,
            },
            {
                "type": "knowledge",
                "content": "The build may work, maybe.",
                "importance": 0.6,
                "confidence": 0.2,
            },
            {
                "type": "user",
                "content": "The user's password is hunter2secret.",
                "importance": 0.9,
                "confidence": 0.9,
            },
        ]}

    def test_valid_extraction_saves_only_durable_candidates(self):
        llm = FakeLLM(self._payload())
        service = self.make_service(llm=llm)
        result = service.process_turn(
            "turn-1", "conv-1", self.USER_MESSAGE, self.ASSISTANT_MESSAGE)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["saved"], 2)
        rows = service.search_memories("").data["memories"]
        contents = {row["content"] for row in rows}
        self.assertIn("The user prefers concise answers.", contents)
        self.assertNotIn("The user was debugging a temporary test harness.", contents)
        self.assertFalse(any("password" in content for content in contents))
        self.assertEqual(llm.extract_calls, 1)

    def test_invalid_structured_output_is_rejected(self):
        for payload in ({"candidates": "nope"}, {"nothing": []}, "plain text"):
            with self.subTest(payload=payload):
                service = self.make_service(llm=FakeLLM(payload))
                self.assertIn(service.extract_candidates(
                    self.USER_MESSAGE, self.ASSISTANT_MESSAGE), (None, []))
                self.assertEqual(service.get_memory_stats().data["total"], 0)

    def test_duplicate_candidates_collapse(self):
        payload = {"candidates": [
            {"type": "user", "content": "The user prefers dark mode.",
             "confidence": 0.9, "importance": 0.7},
            {"type": "user", "content": "User prefers dark mode!",
             "confidence": 0.9, "importance": 0.7},
        ]}
        service = self.make_service(llm=FakeLLM(payload))
        candidates = service.extract_candidates(
            self.USER_MESSAGE, self.ASSISTANT_MESSAGE)
        self.assertEqual(len(candidates), 1)

    def test_repeated_turn_is_not_processed_twice(self):
        llm = FakeLLM(self._payload())
        service = self.make_service(llm=llm)
        first = service.process_turn(
            "same-turn", "conv", self.USER_MESSAGE, self.ASSISTANT_MESSAGE)
        second = service.process_turn(
            "same-turn", "conv", self.USER_MESSAGE, self.ASSISTANT_MESSAGE)
        self.assertTrue(first.ok)
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(llm.extract_calls, 1)
        self.assertEqual(service.get_memory_stats().data["total"], 2)

    def test_failed_llm_does_not_save_and_can_retry(self):
        llm = FakeLLM(extract_error=True)
        service = self.make_service(llm=llm)
        result = service.process_turn(
            "turn-fail", "conv", self.USER_MESSAGE, self.ASSISTANT_MESSAGE)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(service.get_memory_stats().data["total"], 0)
        # A failed turn is retryable rather than reported as completed.
        llm.extract_error = False
        llm.extract_payload = self._payload()
        retry = service.process_turn(
            "turn-fail", "conv", self.USER_MESSAGE, self.ASSISTANT_MESSAGE)
        self.assertTrue(retry.ok)
        self.assertEqual(retry.data["saved"], 2)

    def test_automatic_extraction_can_be_disabled(self):
        service = self.make_service(llm=FakeLLM(self._payload()),
                                    auto_extract=False)
        result = service.process_turn(
            "turn-off", "conv", self.USER_MESSAGE, self.ASSISTANT_MESSAGE)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(service.get_memory_stats().data["total"], 0)

    def test_background_worker_processes_a_turn(self):
        llm = FakeLLM(self._payload())
        service = self.make_service(llm=llm)
        worker = service.start_worker()
        self.assertTrue(worker.submit(
            "worker-turn", "conv", self.USER_MESSAGE, self.ASSISTANT_MESSAGE))
        worker.queue.join()
        self.assertEqual(service.get_memory_stats().data["total"], 2)
        service.shutdown()


class UpdateAndConflictTests(MemoryTestCase):
    def test_update_preserves_identity(self):
        service = self.make_service()
        original = service.remember("Prefers tea.").memory
        updated = service.update_memory(
            original.id, content="Prefers green tea.",
            expected_version=original.version)
        self.assertTrue(updated.ok)
        self.assertEqual(updated.memory.id, original.id)
        self.assertEqual(service.get_memory_stats().data["total"], 1)

    def test_correction_supersedes_and_keeps_history(self):
        service = self.make_service()
        original = service.remember(
            "Project Orion uses provider A.", memory_type="project",
            project_id="Orion").memory
        corrected = service.correct_memory(
            original.id, "Project Orion uses provider B.")
        self.assertTrue(corrected.ok)
        self.assertIsNotNone(corrected.memory)
        self.assertEqual(service.get_memory(original.id).memory.status, "superseded")
        active = service.search_memories("Orion", project_id="Orion")
        self.assertEqual(len(active.data["memories"]), 1)
        self.assertEqual(active.data["memories"][0]["content"],
                         "Project Orion uses provider B.")

    def test_unrelated_similar_words_are_not_merged(self):
        service = self.make_service()
        first = service.remember("The user likes jazz music.", memory_type="knowledge")
        second = service.remember("The user likes jazz apples.", memory_type="knowledge")
        self.assertEqual(first.status, "created")
        self.assertEqual(second.status, "created")
        self.assertEqual(service.get_memory_stats().data["total"], 2)

    def test_exact_duplicate_merges(self):
        service = self.make_service()
        first = service.remember("The user prefers dark mode.", importance=0.6).memory
        second = service.remember("The user prefers dark mode.", importance=0.9,
                                  tags=["ui"])
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(second.memory.id, first.id)
        self.assertAlmostEqual(
            service.get_memory(first.id).memory.importance, 0.9)

    def test_stale_version_is_a_conflict(self):
        service = self.make_service()
        original = service.remember("Versioned fact.").memory
        service.update_memory(original.id, content="Version two.",
                              expected_version=original.version)
        stale = service.update_memory(original.id, content="Version three.",
                                      expected_version=original.version)
        self.assertFalse(stale.ok)
        self.assertEqual(stale.status, "conflict")
        self.assertEqual(service.get_memory(original.id).memory.content,
                         "Version two.")


class ForgettingTests(MemoryTestCase):
    def setUp(self):
        super().setUp()
        self.service = self.make_service()
        self.a = self.service.remember(
            "The user likes lightweight apps.", memory_type="user").memory
        self.b = self.service.remember(
            "Project Orion uses SQLite.", memory_type="project",
            project_id="Orion").memory
        self.c = self.service.remember(
            "The user visited Rome.", memory_type="episodic").memory

    def test_delete_one_and_keep_unrelated(self):
        result = self.service.delete_memory(self.a.id)
        self.assertEqual(result.status, "deleted")
        self.assertEqual(self.service.get_memory_stats().data["total"], 2)
        self.assertEqual(self.service.get_memory(self.c.id).status, "ok")

    def test_forget_topic_is_scoped_and_requires_confirmation(self):
        ambiguous = self.service.forget_by_query("user")
        self.assertEqual(ambiguous.status, "confirmation_required")
        self.assertGreaterEqual(len(ambiguous.data["matches"]), 2)
        self.assertEqual(self.service.get_memory_stats().data["total"], 3)
        deleted = self.service.forget_by_query("Rome", confirm=True)
        self.assertTrue(deleted.ok)
        self.assertEqual(self.service.search_memories("Rome").data["total"], 0)
        self.assertEqual(self.service.get_memory_stats().data["total"], 2)

    def test_forget_project(self):
        preview = self.service.forget_project("Orion")
        self.assertEqual(preview.status, "confirmation_required")
        result = self.service.forget_project("Orion", confirm=True)
        self.assertTrue(result.ok)
        self.assertEqual(self.service.search_memories("Orion").data["total"], 0)
        self.assertEqual(self.service.get_memory_stats().data["total"], 2)

    def test_forget_category_and_clear_all(self):
        preview = self.service.forget_type("episodic")
        self.assertEqual(preview.status, "confirmation_required")
        self.assertTrue(self.service.forget_type("episodic", confirm=True).ok)
        self.assertEqual(self.service.get_memory(self.c.id).status, "not_found")
        self.assertTrue(self.service.clear_all(confirm=True).ok)
        self.assertEqual(self.service.get_memory_stats().data["total"], 0)
        self.assertEqual(self.service.search_memories("lightweight").data["total"], 0)

    def test_clear_all_deletes_every_page_not_only_the_first(self):
        service = self.make_service(auto_extract=False)
        for index in range(505):
            created = service.remember(
                f"Bulk fact number {index}.", memory_type="user",
                dedupe=False)
            self.assertTrue(created.ok, created.message)
        before = service.get_memory_stats().data["total"]
        self.assertGreater(before, 500)
        result = service.clear_all(confirm=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.data["deleted"], before)
        self.assertEqual(service.get_memory_stats().data["total"], 0)

    def test_forget_by_query_deletes_more_than_one_page(self):
        service = self.make_service(auto_extract=False)
        for index in range(65):
            self.assertTrue(service.remember(
                f"Archive note number {index}.", memory_type="episodic",
                dedupe=False).ok)
        result = service.forget_by_query("Archive", confirm=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.data["deleted"], 65)
        self.assertEqual(service.search_memories("Archive").data["total"], 0)

    def test_editing_a_memory_into_forgotten_content_is_rejected(self):
        self.assertTrue(self.service.delete_memory(self.c.id).ok)
        result = self.service.update_memory(
            self.a.id, content=self.c.content, memory_type="episodic")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "forgotten")
        self.assertNotEqual(
            self.service.get_memory(self.a.id).memory.content,
            self.c.content)

    def test_relationships_are_removed_with_the_memory(self):
        self.service.link_memories(self.a.id, self.b.id, "related_to")
        self.assertTrue(self.service.get_memory(self.a.id).memory.relations)
        self.service.delete_memory(self.b.id)
        relations = self.service.get_memory(self.a.id).memory.relations
        self.assertEqual(relations, [])

    def test_extraction_cannot_resurrect_a_deleted_memory(self):
        payload = {"candidates": [{
            "type": "user",
            "content": "The user likes lightweight apps.",
            "confidence": 0.95,
            "importance": 0.9,
        }]}
        service = self.make_service(llm=FakeLLM(payload))
        service.delete_memory(self.a.id)
        result = service.process_turn(
            "resurrection-turn", "conv",
            "I like lightweight apps.", "Noted, lightweight apps.")
        self.assertTrue(result.ok)
        self.assertEqual(service.search_memories("lightweight").data["total"], 0)

    def test_explicit_remember_can_restore_a_forgotten_fact(self):
        self.service.delete_memory(self.a.id)
        restored = self.service.remember("The user likes lightweight apps.")
        self.assertTrue(restored.ok)
        self.assertEqual(self.service.search_memories("lightweight").data["total"], 1)


class BackupAndImportTests(MemoryTestCase):
    def test_backup_is_consistent_and_retention_is_bounded(self):
        service = self.make_service()
        service.remember("Backup me.", memory_type="knowledge")
        for _ in range(4):
            result = service.backup()
            self.assertTrue(result.ok)
        backups = list((Path(self.temp.name) / "backups").glob("memory-backup-*.db"))
        self.assertLessEqual(len(backups), 2)
        with closing(sqlite3.connect(backups[0])) as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)

    def test_export_import_round_trip(self):
        service = self.make_service()
        a = service.remember("Round-trip fact.", tags=["a"], importance=0.9).memory
        b = service.remember("Project fact.", memory_type="project",
                             project_id="Orion").memory
        service.link_memories(a.id, b.id, "related_to")
        payload = service.export_memories()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["memories"]), 2)
        self.assertEqual(len(payload["relations"]), 1)
        target = MemoryService(Path(self.temp.name) / "imported.db")
        self.services.append(target)
        dry = target.import_memories(payload, dry_run=True)
        self.assertEqual(dry.data["would_import"], 2)
        self.assertEqual(target.get_memory_stats().data["total"], 0)
        result = target.import_memories(payload)
        self.assertEqual(result.data["imported"], 2)
        self.assertEqual(target.get_memory_stats().data["total"], 2)

    def test_malformed_export_is_rejected_without_changes(self):
        service = self.make_service()
        service.remember("Existing memory.")
        for payload in ("{not json", {"format": "other"},
                        {"format": "jarvis-memory-export",
                         "schema_version": 99, "memories": []}):
            with self.subTest(payload=payload):
                result = service.import_memories(payload)
                self.assertFalse(result.ok)
                self.assertEqual(result.status, "invalid")
        self.assertEqual(service.get_memory_stats().data["total"], 1)

    def test_bad_record_in_import_does_not_remove_existing(self):
        service = self.make_service()
        service.remember("Existing memory.")
        payload = {
            "format": "jarvis-memory-export",
            "schema_version": 1,
            "memories": [
                {"id": "goodrecord1", "type": "user",
                 "content": "Imported fact.", "confidence": 0.9,
                 "importance": 0.7},
                {"id": "bad!", "type": "nonsense", "content": ""},
            ],
        }
        result = service.import_memories(payload)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["imported"], 1)
        self.assertEqual(len(result.data["invalid"]), 1)
        self.assertEqual(service.get_memory_stats().data["total"], 2)

    def test_export_excludes_deleted_records(self):
        service = self.make_service()
        keep = service.remember("Keep this.").memory
        gone = service.remember("Delete this.").memory
        service.delete_memory(gone.id)
        exported = service.export_memories()
        self.assertEqual([row["id"] for row in exported["memories"]], [keep.id])

    def test_import_does_not_resurrect_forgotten_memory(self):
        source = self.make_service()
        record = source.remember("Forgotten content.").memory
        payload = source.export_memories()
        source.delete_memory(record.id)
        result = source.import_memories(payload)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["imported"], 0)
        self.assertEqual(result.data["skipped"], 1)
        self.assertEqual(source.search_memories("Forgotten").data["total"], 0)


class MaintenanceTests(MemoryTestCase):
    def test_integrity_and_maintenance_are_retryable(self):
        service = self.make_service()
        service.remember("Maintain me.", memory_type="knowledge")
        first = service.run_maintenance(integrity=True)
        second = service.run_maintenance(integrity=True)
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(second.data["integrity"], ["ok"])
        self.assertEqual(service.get_memory_stats().data["total"], 1)

    def test_expired_records_are_not_returned(self):
        service = self.make_service()
        service.remember("Expired fact.", expires_at="2020-01-01T00:00:00Z")
        self.assertEqual(service.search_memories("Expired").data["total"], 0)

    def test_stats_are_aggregated_not_loaded_row_by_row(self):
        service = self.make_service()
        for index in range(5):
            service.remember(f"Fact {index}.", memory_type="knowledge")
        stats = service.get_memory_stats().data
        self.assertEqual(stats["total"], 5)
        self.assertEqual(stats["by_type"]["knowledge"], 5)
        self.assertEqual(stats["by_type"]["user"], 0)
        self.assertIn("recent", stats)


if __name__ == "__main__":
    unittest.main()

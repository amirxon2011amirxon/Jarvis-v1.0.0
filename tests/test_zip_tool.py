"""ZIP archive tool regressions: creation, extraction, safety and updates."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from actions import tool_calling
from actions import zip_tool as zt
from core import approvals


class ZipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="jarvis-zip-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.approvals = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=60, start_sweeper=False))
        self.approvals.set_session("zip-test")
        self.addCleanup(approvals.shutdown_service)

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def make_file(self, *parts: str, content: str = "x") -> str:
        path = self.path(*parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
        return path

    def make_zip(self, *parts: str, entries: dict[str, str]) -> str:
        path = self.path(*parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        return path

    def approve_next(self) -> None:
        rows = self.approvals.active_requests()
        self.assertTrue(rows, "expected an approval request")
        self.approvals.approve(rows[-1]["approval_id"])

    def approved(self, operation, **kwargs):
        blocked = zt.zip_tool(operation, **kwargs)
        self.assertTrue(blocked.get("approval_required"), blocked)
        self.approve_next()
        return zt.zip_tool(operation, **kwargs)

    def names(self, path: str) -> set[str]:
        with zipfile.ZipFile(path) as archive:
            return {info.filename for info in archive.infolist()}

    # ── creation ────────────────────────────────────────────────────────────
    def test_create_single_and_multiple_files(self):
        one = self.make_file("a.txt", content="A")
        two = self.make_file("sub", "b.txt", content="B")
        destination = self.path("out", "bundle.zip")
        result = zt.zip_tool("create", sources=[one, two],
                             destination=destination)
        self.assertTrue(result["success"], result)
        self.assertEqual(self.names(destination), {"a.txt", "b.txt"})
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(archive.read("a.txt"), b"A")
            self.assertEqual(archive.read("b.txt"), b"B")

    def test_create_nested_directories_and_empty_directory(self):
        source = self.path("project")
        os.makedirs(os.path.join(source, "empty"))
        self.make_file("project", "src", "main.py", content="print(1)")
        destination = self.path("project.zip")
        result = zt.zip_tool("create", sources=[source],
                             destination=destination)
        self.assertTrue(result["success"], result)
        names = self.names(destination)
        self.assertIn("project/", names)
        self.assertIn("project/empty/", names)
        self.assertIn("project/src/main.py", names)

    def test_destination_inside_source_is_not_archived(self):
        source = self.path("tree")
        os.makedirs(source)
        self.make_file("tree", "a.txt")
        destination = os.path.join(source, "bundle.zip")
        result = zt.zip_tool("create", sources=[source],
                             destination=destination)
        self.assertTrue(result["success"], result)
        self.assertNotIn("tree/bundle.zip", self.names(destination))

    def test_existing_archive_requires_approval(self):
        source = self.make_file("a.txt")
        destination = self.path("a.zip")
        self.assertTrue(zt.zip_tool(
            "create", sources=[source], destination=destination)["success"])
        blocked = zt.zip_tool("create", sources=[source],
                              destination=destination)
        self.assertTrue(blocked["approval_required"])
        self.approve_next()
        self.assertTrue(zt.zip_tool(
            "create", sources=[source], destination=destination)["success"])

    def test_duplicate_entry_names_are_rejected(self):
        first = self.make_file("one", "same.txt", content="1")
        second = self.make_file("two", "same.txt", content="2")
        destination = self.path("dupe.zip")
        result = zt.zip_tool("create", sources=[first, second],
                             destination=destination)
        self.assertFalse(result["success"])
        self.assertIn("Duplicate", result["error"])
        self.assertFalse(os.path.exists(destination))

    def test_creation_verification_failure_leaves_no_archive(self):
        source = self.make_file("a.txt")
        destination = self.path("bad.zip")
        with mock.patch.object(zt, "_verify_archive",
                               return_value=(False, "forced failure")):
            result = zt.zip_tool("create", sources=[source],
                                 destination=destination)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "verify_failed")
        self.assertFalse(os.path.exists(destination))

    def test_compression_choices(self):
        source = self.make_file("a.txt", content="A" * 500)
        for method in ("store", "deflate", "bzip2", "lzma"):
            with self.subTest(method=method):
                destination = self.path(f"{method}.zip")
                result = zt.zip_tool(
                    "create", sources=[source], destination=destination,
                    compression=method)
                self.assertTrue(result["success"], result)
                with zipfile.ZipFile(destination) as archive:
                    self.assertIsNotNone(archive.read("a.txt"))

    # ── extraction ──────────────────────────────────────────────────────────
    def test_extract_all_and_selected_entries(self):
        archive = self.make_zip("in.zip", entries={
            "a.txt": "A", "docs/b.txt": "B", "other/c.txt": "C"})
        destination = self.path("out")
        result = zt.zip_tool("extract", archive_path=archive,
                             destination=destination)
        self.assertTrue(result["success"], result)
        self.assertEqual(Path(destination, "docs", "b.txt").read_text(), "B")

        selected = self.path("selected")
        result = zt.zip_tool(
            "extract", archive_path=archive, destination=selected,
            selected_entries=["a.txt", "docs/b.txt"])
        self.assertTrue(result["success"], result)
        self.assertTrue(Path(selected, "a.txt").exists())
        self.assertFalse(Path(selected, "other", "c.txt").exists())

    def test_extracting_a_selected_directory_includes_its_children(self):
        archive = self.make_zip("dir.zip", entries={
            "docs/a.txt": "A", "docs/sub/b.txt": "B", "other.txt": "O"})
        destination = self.path("dir-out")
        result = zt.zip_tool(
            "extract", archive_path=archive, destination=destination,
            selected_entries=["docs"])
        self.assertTrue(result["success"], result)
        self.assertTrue(Path(destination, "docs", "a.txt").exists())
        self.assertTrue(Path(destination, "docs", "sub", "b.txt").exists())
        self.assertFalse(Path(destination, "other.txt").exists())

    def test_existing_destination_file_is_refused_and_overwrite_is_approved(self):
        archive = self.make_zip("in.zip", entries={"a.txt": "new"})
        destination = self.path("out")
        os.makedirs(destination)
        Path(destination, "a.txt").write_text("old", encoding="utf-8")
        blocked = zt.zip_tool("extract", archive_path=archive,
                              destination=destination)
        self.assertEqual(blocked["reason"], "conflict")
        self.assertEqual(Path(destination, "a.txt").read_text(), "old")

        result = self.approved("extract", archive_path=archive,
                               destination=destination, overwrite=True)
        self.assertTrue(result["success"], result)
        self.assertEqual(Path(destination, "a.txt").read_text(), "new")

    def test_zip_slip_absolute_drive_and_unc_names_are_refused(self):
        cases = {
            "slip.zip": "../escape.txt",
            "absolute.zip": "/absolute.txt",
            "drive.zip": "C:/Windows/evil.txt",
            "unc.zip": "//server/share/evil.txt",
        }
        for filename, entry in cases.items():
            with self.subTest(entry=entry):
                archive = self.make_zip(filename, entries={entry: "evil"})
                destination = self.path("out-" + filename)
                result = zt.zip_tool("extract", archive_path=archive,
                                     destination=destination)
                self.assertFalse(result["success"])
                self.assertFalse(os.path.exists(self.path("escape.txt")))
                self.assertFalse(os.path.exists(self.path("absolute.txt")))

    def test_symlink_entry_is_refused(self):
        archive = self.path("link.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            info = zipfile.ZipInfo("link")
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
        result = zt.zip_tool("extract", archive_path=archive,
                             destination=self.path("out"))
        self.assertFalse(result["success"])
        self.assertIn("symbolic link", result["error"])

    def test_case_insensitive_duplicate_entries_are_refused(self):
        archive = self.path("case.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("A.txt", "one")
            zf.writestr("a.txt", "two")
        result = zt.zip_tool("extract", archive_path=archive,
                             destination=self.path("out"))
        self.assertFalse(result["success"])
        self.assertIn("duplicate", result["error"].lower())

    def test_archive_bomb_limits_refuse_extraction(self):
        archive = self.make_zip("bomb.zip", entries={"a.txt": "x" * 1000})
        with mock.patch.dict(os.environ,
                             {"JARVIS_ZIP_MAX_TOTAL_BYTES": "100"}):
            result = zt.zip_tool("extract", archive_path=archive,
                                 destination=self.path("out"))
        self.assertFalse(result["success"])
        self.assertIn("safety limit", result["error"])

    def test_corrupt_archive_is_rejected(self):
        archive = self.path("bad.zip")
        Path(archive).write_bytes(b"not a zip")
        for operation in ("inspect", "extract", "test"):
            with self.subTest(operation=operation):
                kwargs = {"operation": operation, "archive_path": archive}
                if operation == "extract":
                    kwargs["destination"] = self.path("out")
                result = zt.zip_tool(**kwargs)
                self.assertFalse(result["success"])

    # ── inspection / list / test ────────────────────────────────────────────
    def test_inspect_and_paginated_list(self):
        archive = self.make_zip("many.zip", entries={
            f"dir/file{i}.txt": str(i) for i in range(12)})
        inspected = zt.zip_tool("inspect", archive_path=archive)
        self.assertTrue(inspected["success"], inspected)
        self.assertEqual(inspected["total_entries"], 12)
        self.assertLessEqual(len(inspected["entries"]), 100)
        page = zt.zip_tool("list", archive_path=archive,
                           path_prefix="dir", page=2, page_size=5)
        self.assertTrue(page["success"], page)
        self.assertEqual(page["page"], 2)
        self.assertEqual(len(page["entries"]), 5)
        self.assertTrue(page["has_more"])

    def test_empty_archive_inspection(self):
        archive = self.path("empty.zip")
        with zipfile.ZipFile(archive, "w"):
            pass
        result = zt.zip_tool("inspect", archive_path=archive)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["total_entries"], 0)
        self.assertEqual(result["compression_ratio"], 0.0)

    def test_test_operation_reports_crc_failure(self):
        archive = self.make_zip("crc.zip", entries={"a.txt": "hello"})
        data = bytearray(Path(archive).read_bytes())
        marker = data.find(b"hello")
        self.assertGreater(marker, 0)
        data[marker] = ord("j")
        Path(archive).write_bytes(data)
        result = zt.zip_tool("test", archive_path=archive)
        self.assertFalse(result["success"])
        self.assertFalse(result["integrity_verified"])
        self.assertTrue(result["corrupt_entries"])

    def test_encrypted_archive_requires_a_password_and_never_returns_it(self):
        archive = self.path("secret.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.txt", "secret")
        # Python's zipfile cannot create encrypted archives; mark the entry so
        # the tool's encrypted-entry detection is exercised.
        data = bytearray(Path(archive).read_bytes())
        central = data.find(b"PK\x01\x02")
        self.assertGreater(central, 0)
        data[central + 8] |= 0x01
        Path(archive).write_bytes(data)
        result = zt.zip_tool("extract", archive_path=archive,
                             destination=self.path("out"))
        self.assertFalse(result["success"])
        self.assertIn("encrypted", result["error"].lower())

    # ── modification ────────────────────────────────────────────────────────
    def test_add_file_requires_approval_and_preserves_existing(self):
        archive = self.make_zip("base.zip", entries={"old.txt": "old"})
        new = self.make_file("new.txt", content="new")
        result = self.approved("add", archive_path=archive, sources=[new])
        self.assertTrue(result["success"], result)
        self.assertEqual(self.names(archive), {"old.txt", "new.txt"})
        with zipfile.ZipFile(archive) as zf:
            self.assertEqual(zf.read("old.txt"), b"old")

    def test_add_duplicate_is_skipped(self):
        archive = self.make_zip("base.zip", entries={"a.txt": "old"})
        source = self.make_file("a.txt", content="new")
        result = zt.zip_tool("add", archive_path=archive, sources=[source])
        self.assertTrue(result["success"], result)
        self.assertEqual(result["files_skipped"], 1)
        self.assertEqual(self.names(archive), {"a.txt"})

    def test_delete_entries_requires_approval_and_preserves_others(self):
        archive = self.make_zip("base.zip", entries={
            "a.txt": "a", "b.txt": "b", "docs/c.txt": "c"})
        result = self.approved("delete_entries", archive_path=archive,
                               entries=["a.txt", "docs/c.txt"])
        self.assertTrue(result["success"], result)
        self.assertEqual(self.names(archive), {"b.txt"})

    def test_failed_modification_keeps_original_archive_intact(self):
        archive = self.make_zip("base.zip", entries={"a.txt": "old"})
        original = Path(archive).read_bytes()
        source = self.make_file("new.txt")
        with mock.patch.object(zt, "_verify_archive",
                               return_value=(False, "forced")):
            result = self.approved("add", archive_path=archive,
                                   sources=[source])
        self.assertFalse(result["success"])
        self.assertEqual(Path(archive).read_bytes(), original)

    def test_rejected_approval_does_not_modify_archive(self):
        archive = self.make_zip("base.zip", entries={"a.txt": "old"})
        source = self.make_file("new.txt")
        blocked = zt.zip_tool("add", archive_path=archive, sources=[source])
        self.assertTrue(blocked["approval_required"])
        self.approvals.reject(
            self.approvals.active_requests()[0]["approval_id"])
        self.assertEqual(self.names(archive), {"a.txt"})


class RegistrationTests(unittest.TestCase):
    def test_registered_declared_and_timed(self):
        info = tool_calling.TOOL_REGISTRY["zip_tool"]
        self.assertIs(info["func"], zt.zip_tool)
        self.assertEqual(info["timeout_s"], 120.0)
        declaration = {
            item["name"]: item
            for item in tool_calling.get_tool_declarations(enabled_only=False)
        }["zip_tool"]
        self.assertEqual(declaration["parameters"]["required"], ["operation"])
        for name in ("sources", "destination", "archive_path",
                     "compression", "overwrite", "selected_entries",
                     "entries", "path_prefix", "page", "page_size"):
            self.assertIn(name, declaration["parameters"]["properties"])

    def test_invalid_operation_and_missing_arguments(self):
        bad = zt.zip_tool("nonsense")
        self.assertFalse(bad["success"])
        missing = zt.zip_tool("create")
        self.assertFalse(missing["success"])


if __name__ == "__main__":
    unittest.main()

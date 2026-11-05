"""filesystem management tool: operations, safety rails and dispatch.

Everything here runs inside temporary directories.  The Recycle Bin call is
mocked so no test touches the real one, and no user folder is ever modified.
"""

from __future__ import annotations

import os
import importlib.util
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import actions
from actions import tool_calling
from actions import file_control_tool as fc
from core import approvals


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def fake_recycle(path: str):
    """Stand-in for the shell call: removes the path instead."""
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    return True, ""


class TempTree(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="jarvis-file-control-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.root)
        self.approvals = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=60, start_sweeper=False))
        self.approvals.set_session("file-control-test")
        self.addCleanup(approvals.shutdown_service)
        self.recycle = mock.patch.object(
            fc, "_send_to_recycle_bin", fake_recycle)
        self.recycle.start()
        self.addCleanup(self.recycle.stop)

    def approve_next(self) -> str:
        rows = self.approvals.active_requests()
        self.assertTrue(rows, "expected an approval request")
        approval_id = rows[-1]["approval_id"]
        self.approvals.approve(approval_id)
        return approval_id

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def make_file(self, *parts: str, content: str = "x") -> str:
        path = self.path(*parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path


class RegistrationTests(unittest.TestCase):
    def test_module_is_discovered_by_the_real_registry(self):
        module = actions.get_tool_module("file_control")
        self.assertIsNotNone(module)
        self.assertIs(module.file_control, fc.file_control)

    def test_registry_entry_and_schema(self):
        info = tool_calling.TOOL_REGISTRY["file_control"]
        self.assertTrue(callable(info["func"]))
        self.assertEqual(info["parameters"]["type"], "OBJECT")
        self.assertEqual(info["parameters"]["required"], ["action"])
        properties = info["parameters"]["properties"]
        for name in ("action", "source_path", "destination_path",
                     "source_paths", "new_name", "item_type", "extension",
                     "query", "recursive", "permanent", "limit",
                     "min_size_mb"):
            self.assertIn(name, properties)
        self.assertNotIn("confirm_token", properties)

    def test_declaration_reaches_the_llm(self):
        declarations = tool_calling.get_tool_declarations()
        names = [item["name"] for item in declarations]
        self.assertIn("file_control", names)

    def test_execute_tool_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = tool_calling.execute_tool(
                "file_control",
                {"action": "create", "source_path": os.path.join(tmp, "d"),
                 "item_type": "folder"})
            self.assertTrue(result["success"])
            self.assertTrue(os.path.isdir(os.path.join(tmp, "d")))
            listed = tool_calling.execute_tool(
                "file_control", {"action": "list", "source_path": tmp})
            self.assertEqual(1, listed["count"])

    def test_unknown_action_is_rejected(self):
        result = fc.file_control("explode", source_path="C:\\x")
        self.assertFalse(result["success"])
        self.assertEqual("unsupported", result["reason"])


class CreateTests(TempTree):
    def test_create_folder_and_file(self):
        folder = self.path("AI Projects")
        result = fc.file_control("create", source_path=folder,
                                 item_type="folder")
        self.assertTrue(result["success"])
        self.assertTrue(os.path.isdir(folder))

        file_path = os.path.join(folder, "notes.txt")
        result = fc.file_control("create", source_path=file_path,
                                 item_type="file")
        self.assertTrue(result["success"])
        self.assertTrue(os.path.isfile(file_path))

    @unittest.skipUnless(has_module("docx"), "python-docx is not installed")
    def test_create_docx_with_content_actually_writes_it(self):
        from docx import Document
        from actions import read_document_tool

        path = self.path("root", "notes.docx")
        result = fc.file_control(
            "create", source_path=path,
            content="Jarvis wrote this document.\nSecond paragraph.")
        self.assertTrue(result["success"], result)
        self.assertEqual(
            [p.text for p in Document(path).paragraphs],
            ["Jarvis wrote this document.", "Second paragraph."])
        read = read_document_tool.read_document(path)
        self.assertIn("Jarvis wrote this document.", read["content"])

    @unittest.skipUnless(has_module("docx"), "python-docx is not installed")
    def test_create_empty_docx_is_a_valid_word_document(self):
        from docx import Document

        path = self.path("root", "blank.docx")
        result = fc.file_control(
            "create", source_path=path, item_type="file")
        self.assertTrue(result["success"], result)
        Document(path)  # must not raise PackageNotFoundError

    def test_create_text_file_with_content_actually_writes_it(self):
        path = self.path("root", "note.txt")
        result = fc.file_control(
            "create", source_path=path, content="Real content.")
        self.assertTrue(result["success"], result)
        self.assertEqual(
            Path(path).read_text(encoding="utf-8"), "Real content.")

    def test_nested_create_requires_recursive(self):
        deep = self.path("a", "b", "c")
        refused = fc.file_control("create", source_path=deep,
                                  item_type="folder")
        self.assertFalse(refused["success"])
        self.assertEqual("not_found", refused["reason"])
        made = fc.file_control("create", source_path=deep,
                               item_type="folder", recursive=True)
        self.assertTrue(made["success"])
        self.assertTrue(os.path.isdir(deep))

    def test_never_overwrites(self):
        target = self.make_file("root", "existing.txt", content="keep")
        result = fc.file_control("create", source_path=target,
                                 item_type="file")
        self.assertFalse(result["success"])
        self.assertEqual("conflict", result["reason"])
        with open(target, encoding="utf-8") as handle:
            self.assertEqual("keep", handle.read())

    def test_reserved_and_invalid_names(self):
        for name in ("CON", "lpt1.txt", "bad:name.txt", "trailing."):
            with self.subTest(name=name):
                result = fc.file_control(
                    "create", source_path=self.path(name), item_type="file")
                self.assertFalse(result["success"])
                self.assertIn(result["reason"], ("invalid", "failed"))

    def test_bare_relative_name_asks_instead_of_guessing(self):
        result = fc.file_control("create", source_path="AI Projects",
                                 item_type="folder")
        self.assertTrue(result.get("clarification_required"))
        self.assertEqual("ambiguous", result["reason"])


class RenameTests(TempTree):
    def test_rename_file_and_folder(self):
        source = self.make_file("root", "report.docx")
        result = fc.file_control("rename", source_path=source,
                                 new_name="final_report.docx")
        self.assertTrue(result["success"])
        self.assertTrue(os.path.isfile(self.path("root", "final_report.docx")))

        folder = self.path("root", "Old Projects")
        os.makedirs(folder)
        result = fc.file_control("rename", source_path=folder,
                                 new_name="New Projects")
        self.assertTrue(result["success"])
        self.assertTrue(os.path.isdir(self.path("root", "New Projects")))

    def test_rename_refuses_overwrite(self):
        source = self.make_file("root", "a.txt")
        self.make_file("root", "b.txt")
        result = fc.file_control("rename", source_path=source, new_name="b.txt")
        self.assertFalse(result["success"])
        self.assertEqual("conflict", result["reason"])

    def test_rename_rejects_paths_and_missing_source(self):
        source = self.make_file("root", "a.txt")
        result = fc.file_control("rename", source_path=source,
                                 new_name="sub\\a.txt")
        self.assertEqual("invalid", result["reason"])
        result = fc.file_control("rename", source_path=self.path("nope.txt"),
                                 new_name="x.txt")
        self.assertEqual("not_found", result["reason"])


class MoveCopyTests(TempTree):
    def test_move_file_into_folder(self):
        source = self.make_file("root", "doc.pdf")
        destination = self.path("out")
        os.makedirs(destination)
        result = fc.file_control("move", source_path=source,
                                 destination_path=destination)
        self.assertTrue(result["success"])
        self.assertFalse(os.path.exists(source))
        self.assertTrue(os.path.isfile(os.path.join(destination, "doc.pdf")))

    def test_move_conflict_leaves_source(self):
        source = self.make_file("root", "doc.pdf")
        destination = self.path("out")
        os.makedirs(destination)
        self.make_file("out", "doc.pdf")
        result = fc.file_control("move", source_path=source,
                                 destination_path=destination)
        self.assertEqual("conflict", result["reason"])
        self.assertTrue(os.path.exists(source))

    def test_move_folder_into_itself_is_refused(self):
        inner = self.path("root", "inner")
        os.makedirs(inner)
        result = fc.file_control("move", source_path=self.root,
                                 destination_path=inner)
        self.assertFalse(result["success"])
        self.assertEqual("invalid_destination", result["reason"])

    def test_moving_a_tree_with_a_junction_is_refused(self):
        source = self.path("root", "linked-tree")
        os.makedirs(source)
        self.make_file("root", "linked-tree", "a.txt")
        destination = self.path("out")
        os.makedirs(destination)
        with mock.patch.object(
                fc, "_first_link",
                return_value=os.path.join(source, "junction")):
            result = fc.file_control(
                "move", source_path=source, destination_path=destination)
        self.assertEqual("unsafe_transfer", result["reason"])
        self.assertTrue(os.path.isdir(source))
        self.assertFalse(os.path.exists(os.path.join(destination,
                                                     "linked-tree")))

    def test_copy_directory_recursively(self):
        self.make_file("root", "a.txt")
        self.make_file("root", "nested", "b.txt")
        target = self.path("copy")
        result = fc.file_control("copy", source_path=self.root,
                                 destination_path=target)
        self.assertTrue(result["success"])
        self.assertTrue(os.path.isfile(os.path.join(target, "a.txt")))
        self.assertTrue(os.path.isfile(os.path.join(target, "nested", "b.txt")))

    def test_copy_refuses_to_overwrite(self):
        source = self.make_file("root", "a.txt")
        # The destination is an existing *file*: copying onto it conflicts.
        target = self.make_file("copy-target")
        result = fc.file_control("copy", source_path=source,
                                 destination_path=target)
        self.assertEqual("conflict", result["reason"])
        self.assertTrue(os.path.isfile(target))

    def test_batch_by_extension(self):
        for name in ("a.pdf", "b.pdf", "c.txt"):
            self.make_file("root", name)
        target = self.path("pdfs")
        os.makedirs(target)
        result = fc.file_control("move", source_path=self.root,
                                 extension="pdf", destination_path=target)
        self.assertEqual(2, result["succeeded"])
        self.assertEqual(0, result["failed"])
        self.assertEqual(["a.pdf", "b.pdf"], sorted(os.listdir(target)))
        self.assertTrue(os.path.isfile(self.path("root", "c.txt")))

    def test_batch_partial_failure_is_per_item(self):
        source = self.make_file("root", "a.txt")
        target = self.path("out")
        os.makedirs(target)
        result = fc.file_control(
            "move", source_paths=[source, self.path("root", "missing.txt")],
            destination_path=target)
        self.assertEqual("partial", result["reason"])
        self.assertEqual(1, result["succeeded"])
        self.assertEqual(1, result["failed"])
        states = [item["success"] for item in result["items"]]
        self.assertEqual([True, False], states)


class DeleteTests(TempTree):
    def test_delete_file_requires_ui_approval_then_recycles(self):
        target = self.make_file("root", "junk.png")
        blocked = fc.file_control("delete", source_path=target)
        self.assertTrue(blocked.get("approval_required"))
        self.assertNotIn("confirmation_token", blocked)
        self.assertTrue(os.path.exists(target))

        self.approve_next()
        done = fc.file_control("delete", source_path=target)
        self.assertTrue(done["success"])
        self.assertEqual("recycle_bin", done["method"])
        self.assertFalse(os.path.exists(target))

    def test_approval_unavailable_fails_closed(self):
        target = self.make_file("root", "held.txt")
        with mock.patch.object(
                fc._approvals, "get_service",
                side_effect=RuntimeError("approval store unavailable")):
            result = fc.file_control("delete", source_path=target)
        self.assertTrue(result.get("approval_unavailable"))
        self.assertFalse(result.get("success"))
        self.assertTrue(os.path.exists(target))

    def test_rejected_approval_never_deletes(self):
        target = self.make_file("root", "keep.txt")
        blocked = fc.file_control("delete", source_path=target)
        self.assertTrue(blocked["approval_required"])
        request = self.approvals.active_requests()[0]
        self.approvals.reject(request["approval_id"])
        again = fc.file_control("delete", source_path=target)
        self.assertTrue(again["approval_required"])
        self.assertTrue(os.path.exists(target))
        self.assertEqual(self.approvals.active_requests()[0]["status"],
                         "pending")

    def test_expired_approval_never_deletes(self):
        service = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=0.02,
                                      start_sweeper=False))
        service.set_session("file-control-test")
        self.addCleanup(approvals.shutdown_service)
        target = self.make_file("root", "expires.txt")
        first = fc.file_control("delete", source_path=target)
        self.assertTrue(first["approval_required"])
        first_request = service.active_requests()[0]
        time.sleep(0.04)
        second = fc.file_control("delete", source_path=target)
        self.assertTrue(second["approval_required"])
        self.assertTrue(os.path.exists(target))
        second_request = service.active_requests()[0]
        self.assertNotEqual(first_request["approval_id"],
                            second_request["approval_id"])

    def test_legacy_confirmation_flag_never_authorizes_delete(self):
        target = self.make_file("root", "held.txt")
        result = fc.file_control(
            "delete", source_path=target, confirm_token="model-made-token")
        self.assertTrue(result["approval_required"])
        self.assertNotIn("approval_id", result)
        self.assertNotIn("confirmation_token", result)
        self.assertFalse(result.get("success"))
        self.assertTrue(os.path.exists(target))

    def test_every_drive_root_is_protected(self):
        for root in ("C:\\", "D:\\", "E:\\"):
            with self.subTest(root=root):
                self.assertIn("root of drive",
                              fc._protected_reason(root))

    def test_recycle_is_refused_on_network_and_removable_volumes(self):
        name = fc._recycle_unavailable_reason
        self.assertIn("Network", name(r"\\server\share\file.txt"))
        with mock.patch.object(
                fc.ctypes.windll.kernel32, "GetDriveTypeW", return_value=2):
            self.assertIn("Removable", name(r"D:\photos\a.jpg"))
        with mock.patch.object(
                fc.ctypes.windll.kernel32, "GetDriveTypeW", return_value=4):
            self.assertIn("Network", name(r"E:\mapped\a.txt"))

    def test_recycle_failure_never_falls_back_to_permanent(self):
        target = self.make_file("root", "held.txt")
        first = fc.file_control("delete", source_path=target)
        self.assertTrue(first["approval_required"])
        self.approve_next()
        with mock.patch.object(fc, "_send_to_recycle_bin",
                               return_value=(False, "denied")):
            result = fc.file_control("delete", source_path=target)
        self.assertEqual("recycle_failed", result["reason"])
        self.assertTrue(os.path.exists(target))

    def test_non_empty_folder_needs_recursive(self):
        target = self.path("root", "full")
        os.makedirs(target)
        self.make_file("root", "full", "file.txt")
        result = fc.file_control("delete", source_path=target)
        self.assertEqual("folder_not_empty", result["reason"])
        self.assertTrue(os.path.isdir(target))

    def test_recursive_delete_is_bound_to_the_exact_target(self):
        one = self.path("root", "one")
        two = self.path("root", "two")
        for folder in (one, two):
            os.makedirs(folder)
            self.make_file(folder, "f.txt")

        first = fc.file_control("delete", source_path=one, recursive=True)
        self.assertTrue(first.get("approval_required"))
        self.approve_next()

        # The approved target is one, not the same-shaped request for two.
        stolen = fc.file_control("delete", source_path=two, recursive=True)
        self.assertTrue(stolen.get("approval_required"))
        self.assertTrue(os.path.isdir(two))

        done = fc.file_control("delete", source_path=one, recursive=True)
        self.assertTrue(done["success"])
        self.assertFalse(os.path.exists(one))

    def test_permanent_delete_requires_its_own_ui_approval(self):
        target = self.make_file("root", "secret.txt")
        first = fc.file_control("delete", source_path=target, permanent=True)
        self.assertTrue(first.get("approval_required"))
        # A token or flag cannot authorize it.
        flagged = fc.file_control(
            "delete", source_path=target, permanent=True,
            confirm_token="bogus")
        self.assertTrue(flagged.get("approval_required"))
        self.assertTrue(os.path.exists(target))

        self.approve_next()
        done = fc.file_control("delete", source_path=target, permanent=True)
        self.assertTrue(done["success"])
        self.assertEqual("permanent", done["method"])
        self.assertFalse(os.path.exists(target))

    def test_permanent_delete_after_ui_approval(self):
        folder = self.path("root", "gone")
        os.makedirs(folder)
        self.make_file(folder, "inside.txt")
        first = fc.file_control("delete", source_path=folder, recursive=True,
                                permanent=True)
        self.assertTrue(first["approval_required"])
        self.approve_next()
        done = fc.file_control("delete", source_path=folder, recursive=True,
                               permanent=True)
        self.assertTrue(done["success"])
        self.assertFalse(os.path.exists(folder))

    def test_protected_application_folder_is_refused(self):
        app_root = str(Path(fc.__file__).resolve().parents[2])
        result = fc.file_control("delete", source_path=app_root, recursive=True)
        self.assertEqual("protected", result["reason"])
        self.assertTrue(os.path.isdir(app_root))


class FindListTests(TempTree):
    def setUp(self):
        super().setUp()
        self.make_file("root", "alpha.txt")
        self.make_file("root", "beta.md")
        self.make_file("root", "sub", "gamma.txt")
        self.make_file("root", "sub", "delta.pdf", content="x" * 2048)

    def test_find_recursive_by_extension(self):
        result = fc.file_control("find", source_path=self.root,
                                 extension="txt")
        names = sorted(item["name"] for item in result["results"])
        self.assertEqual(["alpha.txt", "gamma.txt"], names)

    def test_find_non_recursive(self):
        result = fc.file_control("find", source_path=self.root,
                                 recursive=False)
        # alpha.txt, beta.md and the sub folder itself — never gamma.txt.
        self.assertEqual(3, result["count"])
        names = [item["name"] for item in result["results"]]
        self.assertIn("sub", names)
        self.assertNotIn("gamma.txt", names)

    def test_find_partial_name_and_type(self):
        result = fc.file_control("find", source_path=self.root, query="AMM")
        self.assertEqual(["gamma.txt"], [item["name"]
                                         for item in result["results"]])
        folders = fc.file_control("find", source_path=self.root,
                                  item_type="folder")
        self.assertEqual(["sub"], [item["name"] for item in folders["results"]])

    def test_find_size_filter_and_limit(self):
        big = fc.file_control("find", source_path=self.root,
                              min_size_mb=0.001)
        self.assertEqual(["delta.pdf"], [item["name"] for item in big["results"]])
        limited = fc.file_control("find", source_path=self.root, limit=1)
        self.assertEqual(1, limited["count"])
        self.assertTrue(limited["truncated"])

    def test_list_metadata_and_filter(self):
        result = fc.file_control("list", source_path=self.root)
        self.assertEqual(3, result["count"])
        by_name = {item["name"]: item for item in result["items"]}
        self.assertEqual("folder", by_name["sub"]["type"])
        self.assertIn("size", by_name["alpha.txt"])
        only_txt = fc.file_control("list", source_path=self.root,
                                   extension="txt")
        self.assertEqual(1, only_txt["count"])

    def test_missing_directory(self):
        result = fc.file_control("find", source_path=self.path("nope"))
        self.assertEqual("not_found", result["reason"])


class ContentEditTests(TempTree):
    """Approval-gated write/append/replace/remove/clear, format-aware."""

    def _edit(self, action: str, path: str, **kwargs):
        return fc.file_control(action, source_path=path, **kwargs)

    def _approve_and_edit(self, action: str, path: str, **kwargs):
        blocked = self._edit(action, path, **kwargs)
        self.assertTrue(blocked.get("approval_required"), blocked)
        self.approve_next()
        return self._edit(action, path, **kwargs)

    def test_write_new_file_needs_no_approval(self):
        path = self.path("root", "notes.txt")
        result = self._edit("write", path, content="hello\nworld\n")
        self.assertTrue(result["success"])
        self.assertEqual(result["format"], "text")
        self.assertEqual(open(path, encoding="utf-8").read(),
                         "hello\nworld\n")
        self.assertFalse(self.approvals.active_requests())

    def test_write_existing_requires_approval_and_is_single_use(self):
        path = self.make_file("root", "notes.txt", content="old")
        blocked = self._edit("write", path, content="new")
        self.assertTrue(blocked["approval_required"])
        self.assertEqual(open(path, encoding="utf-8").read(), "old")
        self.approve_next()
        done = self._edit("write", path, content="new")
        self.assertTrue(done["success"])
        self.assertEqual(open(path, encoding="utf-8").read(), "new")
        # The approval was consumed; another write must ask again.
        again = self._edit("write", path, content="newer")
        self.assertTrue(again["approval_required"])
        self.assertEqual(open(path, encoding="utf-8").read(), "new")

    def test_rejected_approval_leaves_content_untouched(self):
        path = self.make_file("root", "notes.txt", content="keep")
        blocked = self._edit("append", path, content=" more")
        self.assertTrue(blocked["approval_required"])
        request = self.approvals.active_requests()[0]
        self.approvals.reject(request["approval_id"])
        again = self._edit("append", path, content=" more")
        self.assertTrue(again["approval_required"])
        self.assertEqual(open(path, encoding="utf-8").read(), "keep")

    def test_append_preserves_existing_content(self):
        path = self.make_file("root", "notes.txt", content="first\n")
        result = self._approve_and_edit("append", path, content="second\n")
        self.assertTrue(result["success"])
        self.assertEqual(open(path, encoding="utf-8").read(),
                         "first\nsecond\n")

    def test_replace_content_reports_count_and_no_match_is_harmless(self):
        path = self.make_file("root", "notes.txt", content="cat cat dog")
        missing = self._edit("replace_content", path,
                             find_text="bird", replacement_text="x")
        self.assertFalse(missing["success"])
        self.assertEqual(missing["reason"], "not_found")
        self.assertFalse(self.approvals.active_requests())
        self.assertEqual(open(path, encoding="utf-8").read(), "cat cat dog")

        result = self._approve_and_edit(
            "replace_content", path, find_text="cat", replacement_text="fox")
        self.assertTrue(result["success"])
        self.assertEqual(result["replacements"], 2)
        self.assertEqual(open(path, encoding="utf-8").read(),
                         "fox fox dog")

    def test_remove_content_and_clear_content(self):
        path = self.make_file("root", "notes.txt", content="alpha beta alpha")
        result = self._approve_and_edit(
            "remove_content", path, find_text="alpha")
        self.assertTrue(result["success"])
        self.assertEqual(result["replacements"], 2)
        self.assertEqual(open(path, encoding="utf-8").read(), " beta ")

        result = self._approve_and_edit("clear_content", path)
        self.assertTrue(result["success"])
        self.assertEqual(open(path, encoding="utf-8").read(), "")

    def test_encoding_and_line_endings_are_preserved(self):
        path = self.path("root", "utf16.txt")
        with open(path, "wb") as handle:
            handle.write("first line\r\n".encode("utf-16"))
        result = self._approve_and_edit("append", path, content="second\n")
        self.assertTrue(result["success"])
        raw = open(path, "rb").read()
        self.assertEqual(raw.decode("utf-16"), "first line\r\nsecond\r\n")

    def test_atomic_write_failure_keeps_original(self):
        path = self.make_file("root", "notes.txt", content="original")
        self.assertTrue(self._edit("append", path,
                                   content="+more")["approval_required"])
        self.approve_next()
        with mock.patch.object(fc, "_commit_temp",
                               side_effect=OSError("simulated rename failure")):
            result = self._edit("append", path, content="+more")
        self.assertFalse(result["success"])
        self.assertEqual(open(path, encoding="utf-8").read(), "original")

    def test_approval_is_bound_to_the_reviewed_bytes(self):
        path = self.make_file("root", "notes.txt", content="original")
        self.assertTrue(self._edit("append", path,
                                   content="+more")["approval_required"])
        self.approve_next()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("changed outside the approval")
        result = self._edit("append", path, content="+more")
        # The reviewed file changed, so the old approval cannot match the new
        # request: a fresh approval is required and nothing is written.
        self.assertTrue(result["approval_required"])
        self.assertEqual(open(path, encoding="utf-8").read(),
                         "changed outside the approval")

    def test_protected_and_link_targets_are_refused(self):
        path = self.make_file("root", "notes.txt", content="old")
        with mock.patch.object(fc, "_write_blocked_reason",
                               return_value="it is protected"):
            result = self._edit("write", path, content="new")
        self.assertEqual(result["reason"], "protected")
        self.assertEqual(open(path, encoding="utf-8").read(), "old")

        with mock.patch.object(fc, "_is_link", return_value=True):
            result = self._edit("write", path, content="new")
        self.assertEqual(result["reason"], "unsupported")

    def test_read_only_formats_return_a_clear_error(self):
        path = self.make_file("root", "a.pdf", content="%PDF-1.4\n")
        result = self._edit("clear_content", path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "unsupported")
        self.assertIn("read-only", result["error"])

    @unittest.skipUnless(has_module("docx"), "python-docx is not installed")
    def test_docx_structured_edit(self):
        from docx import Document
        from actions import read_document_tool

        path = self.path("root", "a.docx")
        document = Document()
        document.add_heading("Heading", level=1)
        document.add_paragraph("old text")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "A"
        table.cell(0, 1).text = "B"
        document.save(path)

        result = self._approve_and_edit(
            "append", path, content="appended paragraph")
        self.assertTrue(result["success"])
        result = self._approve_and_edit(
            "replace_content", path, find_text="old", replacement_text="new")
        self.assertTrue(result["success"])
        self.assertEqual(result["replacements"], 1)
        read = read_document_tool.read_document(path)
        self.assertIn("appended paragraph", read["content"])
        self.assertIn("new text", read["content"])
        self.assertIn("| A | B |", read["content"])

        result = self._approve_and_edit("clear_content", path)
        self.assertTrue(result["success"])
        cleared = read_document_tool.read_document(path)
        self.assertFalse(cleared["success"])

    @unittest.skipUnless(has_module("openpyxl"), "openpyxl is not installed")
    def test_xlsx_cell_edit_preserves_other_sheets(self):
        import openpyxl
        from actions import read_document_tool

        path = self.path("root", "a.xlsx")
        workbook = openpyxl.Workbook()
        data = workbook.active
        data.title = "Data"
        data["A1"] = "old"
        other = workbook.create_sheet("Keep")
        other["A1"] = "untouched"
        workbook.save(path)

        result = self._approve_and_edit(
            "write", path, content="new", sheet="Data", cell="A1")
        self.assertTrue(result["success"])
        check = openpyxl.load_workbook(path)
        self.assertEqual(check["Data"]["A1"].value, "new")
        self.assertEqual(check["Keep"]["A1"].value, "untouched")

        result = self._approve_and_edit(
            "append", path, content="B,2\nC,3", sheet="Data")
        self.assertTrue(result["success"])
        read = read_document_tool.read_document(path, sheet="Data")
        self.assertIn("| B | 2 |", read["content"])

    @unittest.skipUnless(has_module("pptx"), "python-pptx is not installed")
    def test_pptx_slide_append_and_text_replace(self):
        from actions import read_document_tool
        from pptx import Presentation

        path = self.path("root", "a.pptx")
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "old title"
        presentation.save(path)

        result = self._approve_and_edit(
            "append", path, content="second slide text")
        self.assertTrue(result["success"])
        result = self._approve_and_edit(
            "replace_content", path, find_text="old", replacement_text="new")
        self.assertTrue(result["success"])
        read = read_document_tool.read_document(path)
        self.assertIn("new title", read["content"])
        self.assertIn("second slide text", read["content"])

    @unittest.skipUnless(has_module("docx"), "python-docx is not installed")
    def test_write_repairs_an_invalid_empty_docx_placeholder(self):
        from docx import Document

        path = self.make_file("root", "broken.docx", content="")
        result = self._approve_and_edit(
            "write", path, content="Recovered content.")
        self.assertTrue(result["success"], result)
        self.assertEqual(
            [p.text for p in Document(path).paragraphs],
            ["Recovered content."])

    @unittest.skipUnless(has_module("openpyxl"), "openpyxl is not installed")
    def test_write_repairs_an_invalid_empty_xlsx_placeholder(self):
        import openpyxl

        path = self.make_file("root", "broken.xlsx", content="")
        result = self._approve_and_edit(
            "write", path, content="Recovered", cell="A1")
        self.assertTrue(result["success"], result)
        check = openpyxl.load_workbook(path)
        self.assertEqual(check.active["A1"].value, "Recovered")

    @unittest.skipUnless(has_module("pptx"), "python-pptx is not installed")
    def test_write_repairs_an_invalid_empty_pptx_placeholder(self):
        from actions import read_document_tool

        path = self.make_file("root", "broken.pptx", content="")
        result = self._approve_and_edit(
            "write", path, content="Recovered slide.")
        self.assertTrue(result["success"], result)
        read = read_document_tool.read_document(path)
        self.assertIn("Recovered slide.", read["content"])

    def test_csv_structured_edit_and_append(self):
        from actions import read_document_tool

        path = self.make_file("root", "table.csv",
                              content="name,value\nA,1\nB,2\n")
        result = self._approve_and_edit(
            "replace_content", path, find_text="A", replacement_text="Z",
            column="name")
        self.assertTrue(result["success"])
        result = self._approve_and_edit(
            "append", path, content="C,3\n")
        self.assertTrue(result["success"])
        result = self._approve_and_edit(
            "write", path, content="9", row=2, column="value")
        self.assertTrue(result["success"])
        read = read_document_tool.read_document(path)
        self.assertIn("| Z | 9 |", read["content"])
        self.assertIn("| C | 3 |", read["content"])

    @unittest.skipUnless(has_module("openpyxl"), "openpyxl is not installed")
    def test_xlsx_append_to_empty_sheet_starts_at_row_one(self):
        import openpyxl

        path = self.path("root", "empty.xlsx")
        workbook = openpyxl.Workbook()
        workbook.save(path)
        result = self._approve_and_edit(
            "append", path, content="A,1", sheet="Sheet")
        self.assertTrue(result["success"], result)
        check = openpyxl.load_workbook(path)
        self.assertEqual(check["Sheet"]["A1"].value, "A")
        self.assertEqual(check["Sheet"]["B1"].value, 1)

    @unittest.skipUnless(has_module("openpyxl"), "openpyxl is not installed")
    def test_xlsx_sheet_name_matching_is_case_insensitive(self):
        import openpyxl

        path = self.path("root", "case.xlsx")
        workbook = openpyxl.Workbook()
        workbook.active.title = "Data"
        workbook.active["A1"] = "old"
        workbook.save(path)
        result = self._approve_and_edit(
            "write", path, content="new", sheet="data", cell="A1")
        self.assertTrue(result["success"], result)
        check = openpyxl.load_workbook(path)
        self.assertEqual(check["Data"]["A1"].value, "new")

    def test_plain_text_write_verification_failure_removes_new_file(self):
        path = self.path("root", "verify.txt")
        with mock.patch.object(
                fc, "_read_text_for_edit",
                return_value=("different", "utf-8", [])):
            result = fc.file_control(
                "write", source_path=path, content="expected")
        self.assertEqual(result["reason"], "verify_failed")
        self.assertFalse(os.path.exists(path))

    def test_partial_create_is_removed_when_the_write_fails(self):
        path = self.path("root", "partial.txt")
        with mock.patch.object(
                fc.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fc._create_bytes(path, b"partial")
        self.assertFalse(os.path.exists(path))

    def test_filtered_batch_with_item_type_selects_folders(self):
        folder = self.path("root", "selected")
        os.makedirs(folder)
        self.make_file("root", "selected", "inside.txt")
        self.make_file("root", "not-selected.txt")
        destination = self.path("out")
        os.makedirs(destination)
        result = fc.file_control(
            "copy", source_path=self.root, destination_path=destination,
            item_type="folder")
        self.assertTrue(result["success"], result)
        self.assertTrue(os.path.isdir(os.path.join(destination, "selected")))
        self.assertFalse(os.path.exists(os.path.join(destination,
                                                     "not-selected.txt")))

    @unittest.skipUnless(has_module("yaml"), "PyYAML is not installed")
    def test_json_and_yaml_structured_edits(self):
        from actions import read_document_tool

        json_path = self.make_file("root", "a.json",
                                   content='{"a": 1, "b": "old"}')
        result = self._approve_and_edit(
            "replace_content", json_path, find_text="old",
            replacement_text="new")
        self.assertTrue(result["success"])
        self.assertEqual(json.loads(open(json_path, encoding="utf-8").read()),
                         {"a": 1, "b": "new"})
        result = self._approve_and_edit(
            "append", json_path, content='{"c": 2}')
        self.assertTrue(result["success"])
        self.assertEqual(json.loads(open(json_path, encoding="utf-8").read()),
                         {"a": 1, "b": "new", "c": 2})

        yaml_path = self.make_file("root", "a.yaml", content="a: 1\nb: old\n")
        result = self._approve_and_edit(
            "replace_content", yaml_path, find_text="old",
            replacement_text="new")
        self.assertTrue(result["success"])
        read = read_document_tool.read_document(yaml_path)
        self.assertIn("b: new", read["content"])

    def test_json_key_replacement_collision_does_not_drop_data(self):
        path = self.make_file("root", "merge.json",
                              content='{"old": 1, "new": 2}')
        result = self._edit(
            "replace_content", path, find_text="old",
            replacement_text="new")
        self.assertFalse(result.get("success", True))
        self.assertEqual(result.get("reason"), "conflict")
        self.assertFalse(self.approvals.active_requests())
        self.assertEqual(
            json.loads(open(path, encoding="utf-8").read()),
            {"old": 1, "new": 2})

    def test_invalid_json_write_is_rejected_before_approval(self):
        path = self.make_file("root", "a.json", content='{"a": 1}')
        result = self._edit("write", path, content="{broken")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_argument")
        self.assertFalse(self.approvals.active_requests())
        self.assertEqual(open(path, encoding="utf-8").read(), '{"a": 1}')

    def test_helpful_approval_reuse_and_expiry(self):
        path = self.make_file("root", "notes.txt", content="base")
        first = self._edit("append", path, content="+1")
        self.assertTrue(first["approval_required"])
        # A model-supplied token cannot authorize the change.
        with_token = self._edit("append", path, content="+1",
                                confirm_token="model-token")
        self.assertTrue(with_token["approval_required"])
        self.assertNotIn("approval_id", with_token)
        self.assertEqual(open(path, encoding="utf-8").read(), "base")

        service = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=0.02,
                                      start_sweeper=False))
        service.set_session("file-control-test")
        self.addCleanup(approvals.shutdown_service)
        expired = self._edit("append", path, content="+2")
        self.assertTrue(expired["approval_required"])
        first_request = service.active_requests()[0]
        time.sleep(0.04)
        again = self._edit("append", path, content="+2")
        self.assertTrue(again["approval_required"])
        second_request = service.active_requests()[0]
        self.assertNotEqual(first_request["approval_id"],
                            second_request["approval_id"])
        self.assertEqual(open(path, encoding="utf-8").read(), "base")

    def test_write_rejects_empty_content(self):
        path = self.path("root", "empty.md")
        result = self._edit("write", path, content="")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "empty_content")
        self.assertFalse(os.path.exists(path))
        self.assertEqual(result["content_length"], 0)

    def test_append_rejects_empty_content(self):
        path = self.make_file("root", "notes.txt", content="keep")
        result = self._edit("append", path, content="")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "empty_content")
        self.assertEqual(open(path, encoding="utf-8").read(), "keep")

    def test_write_preserves_unicode_and_multiline_content(self):
        path = self.path("root", "unicode.md")
        content = "# Отчёт\n\nline one ✓\nline two — 日本語\n"
        result = self._edit("write", path, content=content)
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["characters_written"], len(content))
        with open(path, encoding="utf-8", newline="") as handle:
            self.assertEqual(handle.read(), content)

    def test_declaration_exposes_content_parameters(self):
        info = tool_calling.TOOL_REGISTRY["file_control"]
        properties = info["parameters"]["properties"]
        for name in ("content", "find_text", "replacement_text", "encoding",
                     "sheet", "cell", "row", "column"):
            self.assertIn(name, properties)
        self.assertIn("clear_content", properties["action"]["description"])


class ApprovalResumeTests(TempTree):
    """Approvals resume the exact blocked call once, or not at all."""

    def _blocked_delete(self, target: str, **extra):
        result = fc.file_control("delete", source_path=target,
                                 permanent=True, **extra)
        self.assertTrue(result["approval_required"], result)
        approval_id = result["_approval_id"]
        self.assertTrue(approval_id)
        call = SimpleNamespace(
            id="call-1", name="file_control",
            args={"action": "delete", "source_path": target,
                  "permanent": True, **extra})
        tool_calling.remember_pending_approval(approval_id, call)
        return approval_id

    def test_approved_delete_executes_the_exact_call_once(self):
        target = self.make_file("root", "approved.txt")
        approval_id = self._blocked_delete(target)
        self.approve_next()
        call, result = tool_calling.resume_pending_approval(approval_id)
        self.assertIsNotNone(call)
        self.assertTrue(result["success"])
        self.assertFalse(os.path.exists(target))
        again_call, again_result = tool_calling.resume_pending_approval(
            approval_id)
        self.assertIsNone(again_call)
        self.assertIsNone(again_result)
        self.assertFalse(os.path.exists(target))

    def test_rejected_approval_never_executes(self):
        target = self.make_file("root", "rejected.txt", content="keep")
        approval_id = self._blocked_delete(target)
        request = self.approvals.active_requests()[0]
        self.approvals.reject(request["approval_id"])
        tool_calling.discard_pending_approval(approval_id)
        call, result = tool_calling.resume_pending_approval(approval_id)
        self.assertIsNone(call)
        self.assertIsNone(result)
        self.assertTrue(os.path.exists(target))

    def test_expired_approval_never_executes(self):
        service = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=0.02,
                                      start_sweeper=False))
        service.set_session("file-control-test")
        self.addCleanup(approvals.shutdown_service)
        target = self.make_file("root", "expired.txt", content="keep")
        result = fc.file_control("delete", source_path=target,
                                 permanent=True)
        approval_id = result["_approval_id"]
        call = SimpleNamespace(
            id="call-expired", name="file_control",
            args={"action": "delete", "source_path": target,
                  "permanent": True})
        tool_calling.remember_pending_approval(approval_id, call)
        time.sleep(0.04)
        service.expire()
        tool_calling.discard_pending_approval(approval_id)
        call, resumed = tool_calling.resume_pending_approval(approval_id)
        self.assertIsNone(call)
        self.assertIsNone(resumed)
        self.assertTrue(os.path.exists(target))

    def test_session_change_prevents_stale_approval_execution(self):
        target = self.make_file("root", "session.txt", content="keep")
        approval_id = self._blocked_delete(target)
        self.approvals.set_session("a-different-conversation")
        self.approve_next = lambda: None  # old request is revoked
        request = self.approvals.all_requests()[0]
        self.assertEqual(request["status"], "revoked")
        tool_calling.discard_pending_approval(approval_id)
        call, result = tool_calling.resume_pending_approval(approval_id)
        self.assertIsNone(call)
        self.assertIsNone(result)
        self.assertTrue(os.path.exists(target))

    def test_bulk_and_recursive_deletion_resume(self):
        first = self.make_file("root", "bulk-a.txt")
        second = self.make_file("root", "bulk-b.txt")
        folder = self.path("root", "nested")
        os.makedirs(folder)
        nested = os.path.join(folder, "inside.txt")
        with open(nested, "w", encoding="utf-8") as handle:
            handle.write("x")

        result = fc.file_control(
            "delete", source_paths=[first, second], permanent=True)
        approval_id = result["_approval_id"]
        call = SimpleNamespace(
            id="call-bulk", name="file_control",
            args={"action": "delete", "source_paths": [first, second],
                  "permanent": True})
        tool_calling.remember_pending_approval(approval_id, call)
        self.approve_next()
        _, resumed = tool_calling.resume_pending_approval(approval_id)
        self.assertTrue(resumed["success"])
        self.assertFalse(os.path.exists(first))
        self.assertFalse(os.path.exists(second))

        recursive = fc.file_control(
            "delete", source_path=folder, permanent=True, recursive=True)
        approval_id = recursive["_approval_id"]
        call = SimpleNamespace(
            id="call-folder", name="file_control",
            args={"action": "delete", "source_path": folder,
                  "permanent": True, "recursive": True})
        tool_calling.remember_pending_approval(approval_id, call)
        self.approve_next()
        _, resumed = tool_calling.resume_pending_approval(approval_id)
        self.assertTrue(resumed["success"])
        self.assertFalse(os.path.exists(folder))


class ResolutionTests(unittest.TestCase):
    def test_known_folder_alias_resolves_through_the_shell(self):
        with mock.patch.object(fc, "_known_folder", return_value=r"D:\Docs"):
            path, error = fc._resolve_path(r"Documents\report.pdf")
        self.assertIsNone(error)
        self.assertEqual(os.path.normpath(r"D:\Docs\report.pdf"), path)

    def test_drive_reference(self):
        path, error = fc._resolve_path("D drive")
        self.assertIsNone(error)
        self.assertEqual("D:\\", path)

    def test_bare_name_is_ambiguous(self):
        path, error = fc._resolve_path("AI Projects")
        self.assertEqual("", path)
        self.assertEqual("ambiguous", error["reason"])


if __name__ == "__main__":
    unittest.main()

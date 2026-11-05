"""Tests for the file and folder opening tool.

Every launch is mocked and every search runs inside a temporary directory, so the
suite never opens an application, reveals a real file or reads a private folder.
Run from the project root with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import builtins
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from actions import open_app_tool, open_file_tool, tool_calling
from core import approvals

PROMPT_PATH = Path(__file__).resolve().parents[1] / "core" / "prompt.txt"


def shortcut_app(name, link):
    return {"name": name, "source": "shortcut", "shortcut_path": str(link)}


class ToolTestCase(unittest.TestCase):
    """A disposable folder tree plus the few launch seams worth replacing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        # Known-folder and drive caches are process-wide; each test must start
        # from a clean shell view.
        with open_file_tool._HOME_FOLDER_LOCK:
            open_file_tool._HOME_FOLDER_CACHE.clear()
        with open_file_tool._LOCAL_DRIVES_LOCK:
            open_file_tool._LOCAL_DRIVES_AT = 0.0

        # The shell's association lookup is a real Windows query; tests must not
        # depend on which apps this machine happens to have installed.
        self._patch = mock.patch.object(
            open_file_tool, "_associated_handler",
            return_value=r"C:\Windows\system32\notepad.exe")
        self._patch.start()
        self.addCleanup(self._patch.stop)

        self.approvals = approvals.reset_service(
            approvals.ApprovalService(ttl_seconds=60, start_sweeper=False))
        self.approvals.set_session("open-file-test")
        self.addCleanup(approvals.shutdown_service)

    def approve_next(self) -> str:
        rows = self.approvals.active_requests()
        self.assertTrue(rows, "expected an approval request")
        approval_id = rows[-1]["approval_id"]
        self.approvals.approve(approval_id)
        return approval_id

    def make_file(self, relative, text="content"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def make_dir(self, relative):
        path = self.root / relative
        path.mkdir(parents=True, exist_ok=True)
        return path

    def search(self, **kwargs):
        kwargs.setdefault("location", str(self.root))
        return open_file_tool.search_files(**kwargs)

    def paths(self, result):
        return [item["path"] for item in result["results"]]


class ValidationTests(ToolTestCase):
    def test_absolute_path_is_resolved(self):
        target = self.make_file("notes.txt")
        result = open_file_tool.validate_path(str(target))
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], str(target))
        self.assertEqual(result["item_type"], "file")

    def test_empty_path_is_rejected(self):
        for value in ("", "   ", None, '""'):
            with self.subTest(value=value):
                result = open_file_tool.validate_path(value)
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], "empty")

    def test_control_characters_are_rejected(self):
        result = open_file_tool.validate_path("C:\\bad\x00name.txt")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid")

    def test_missing_path_is_reported(self):
        result = open_file_tool.validate_path(str(self.root / "nope.txt"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing")
        self.assertIn("does not exist", result["error"])

    def test_wrong_target_type_is_reported(self):
        file_path = self.make_file("report.txt")
        folder = self.make_dir("Projects")

        as_folder = open_file_tool.validate_path(str(file_path), expect="folder")
        self.assertFalse(as_folder["ok"])
        self.assertEqual(as_folder["reason"], "wrong_type")
        self.assertIn("file, not a folder", as_folder["error"])

        as_file = open_file_tool.validate_path(str(folder), expect="file")
        self.assertFalse(as_file["ok"])
        self.assertEqual(as_file["reason"], "wrong_type")
        self.assertIn("folder, not a file", as_file["error"])

    def test_validate_path_requires_an_absolute_path(self):
        # The low-level validator no longer guesses from the working directory;
        # user-facing relative paths go through resolve_open_target.
        target = self.make_file("notes.txt")
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            result = open_file_tool.validate_path("notes.txt")
        finally:
            os.chdir(previous)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing")
        self.assertIn("not an absolute path", result["error"])
        self.assertTrue(target.exists())

    def test_two_relative_paths_on_a_drive_are_ambiguous(self):
        first = self.make_dir("one/Projects")
        second = self.make_dir("two/Projects")

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            if roots == ["C:\\"]:
                yield (str(first), True, 0.0)
                yield (str(second), True, 0.0)

        with mock.patch.object(
                open_file_tool, "_home_folder", return_value=""), \
             mock.patch.object(
                open_file_tool, "_local_drives", return_value=["C:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder("Work\\Projects")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "ambiguous")
        self.assertEqual(set(result["candidates"]),
                         {str(first), str(second)})
        startfile.assert_not_called()

    def test_a_misspelled_open_path_returns_candidates_instead_of_opening(self):
        target = self.make_file("homework.docx")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(
                str(self.root / "homewrok.docx"))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "ambiguous")
        self.assertTrue(result.get("clarification_required"))
        self.assertTrue(result.get("similar"))
        self.assertEqual(result["candidates"], [str(target)])
        startfile.assert_not_called()

    def test_a_misspelled_folder_path_returns_candidates(self):
        target = self.make_dir("Projects")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder(
                str(self.root / "Projcts"))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "ambiguous")
        self.assertTrue(result.get("clarification_required"))
        self.assertEqual(result["candidates"], [str(target)])
        startfile.assert_not_called()

    def test_a_relative_path_without_drives_is_reported(self):
        with mock.patch.object(
                open_file_tool, "_home_folder", return_value=""), \
             mock.patch.object(
                open_file_tool, "_local_drives", return_value=[]):
            result = open_file_tool.open_file("sub\\notes.txt")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing")
        self.assertIn("no local drive", result["error"])

    def test_paths_on_other_drives_keep_their_drive(self):
        raw = "D:\\School\\homework.docx"
        result = open_file_tool.validate_path(raw)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing")
        self.assertTrue(result["path"].upper().startswith("D:"))

    def test_permission_denied_is_reported(self):
        target = self.make_file("secret.txt")
        with mock.patch.object(
                open_file_tool, "_can_access", return_value=False):
            result = open_file_tool.validate_path(str(target))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "denied")
        self.assertIn("permission", result["error"].lower())

    def test_can_access_translates_permission_error(self):
        target = self.make_file("locked.txt")
        with mock.patch.object(
                builtins, "open", side_effect=PermissionError("denied")):
            self.assertFalse(open_file_tool._can_access(target, False))
        # A file held open by another program is still worth handing to Windows.
        with mock.patch.object(
                builtins, "open", side_effect=OSError("sharing violation")):
            self.assertTrue(open_file_tool._can_access(target, False))


class SearchTests(ToolTestCase):
    def test_exact_filename_search(self):
        target = self.make_file("homework.docx")
        self.make_file("other.txt")
        result = self.search(query="homework.docx")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.paths(result), [str(target)])
        self.assertEqual(result["results"][0]["type"], "file")

    def test_partial_name_search(self):
        first = self.make_file("maths homework final.docx")
        second = self.make_file("homework notes.txt")
        self.make_file("holiday.jpg")
        result = self.search(query="homework")
        self.assertEqual(result["count"], 2)
        self.assertEqual(set(self.paths(result)), {str(first), str(second)})

    def test_multi_word_query_matches_separated_words(self):
        target = self.make_file("maths_homework_2024.docx")
        result = self.search(query="maths homework")
        self.assertEqual(self.paths(result), [str(target)])

    def test_folder_search(self):
        folder = self.make_dir("Aze project")
        self.make_file("Aze project/main.py")
        result = self.search(query="aze", item_type="folder")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["path"], str(folder))
        self.assertEqual(result["results"][0]["type"], "folder")

    def test_extension_filter(self):
        slide = self.make_file("talk.pptx")
        self.make_file("notes.txt")
        result = self.search(extension="pptx")
        self.assertEqual(self.paths(result), [str(slide)])

    def test_extension_filter_accepts_several_forms(self):
        first = self.make_file("talk.pptx")
        second = self.make_file("old.pptm")
        self.make_file("notes.txt")
        for value in ("pptx,pptm", ".pptx .pptm", "*.pptx, *.pptm"):
            with self.subTest(value=value):
                result = self.search(extension=value)
                self.assertEqual(set(self.paths(result)),
                                 {str(first), str(second)})

    def test_extensionless_file_is_found(self):
        makefile = self.make_file("Makefile")
        result = self.search(query="makefile")
        self.assertEqual(self.paths(result), [str(makefile)])

    def test_spaces_and_non_ascii_paths(self):
        target = self.make_file("Мои документы/математика домашка.docx")
        result = self.search(query="математика")
        self.assertEqual(self.paths(result), [str(target)])

    def test_multiple_matches_are_all_returned(self):
        first = self.make_file("one/notes.txt")
        second = self.make_file("two/notes.txt")
        result = self.search(query="notes.txt")
        self.assertEqual(result["count"], 2)
        self.assertEqual(set(self.paths(result)), {str(first), str(second)})

    def test_modified_within_days(self):
        old = self.make_file("old.pptx")
        new = self.make_file("new.pptx")
        past = time.time() - 10 * 86400
        os.utime(old, (past, past))
        result = self.search(extension="pptx", modified_within_days=2)
        self.assertEqual(self.paths(result), [str(new)])

    def test_newest_first_when_there_is_no_query(self):
        older = self.make_file("older.pptx")
        newer = self.make_file("newer.pptx")
        past = time.time() - 5 * 86400
        os.utime(older, (past, past))
        result = self.search(extension="pptx")
        self.assertEqual(self.paths(result), [str(newer), str(older)])

    def test_descriptive_words_do_not_hide_files_of_that_kind(self):
        older = self.make_file("quarterly review.pptx")
        newer = self.make_file("team update.pptx")
        past = time.time() - 3 * 86400
        os.utime(older, (past, past))

        result = self.search(query="powerpoint presentation", extension="pptx")
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.paths(result), [str(newer), str(older)])
        self.assertIn("No name matched", result["message"])

    def test_a_name_match_is_still_ranked_first(self):
        match = self.make_file("budget.pptx")
        self.make_file("team update.pptx")
        newer = self.make_file("budget final.pptx")
        result = self.search(query="budget", extension="pptx")
        self.assertEqual(self.paths(result)[0], str(match))
        self.assertIn(str(newer), self.paths(result))

    def test_hidden_folders_are_skipped_unless_asked_for(self):
        self.make_file(".config/settings.json")
        hidden_off = self.search(query="settings.json")
        self.assertEqual(hidden_off["count"], 0)

        hidden_on = self.search(query="settings.json", include_hidden=True)
        self.assertEqual(hidden_on["count"], 1)

        string_off = self.search(query="settings.json", include_hidden="false")
        self.assertEqual(string_off["count"], 0)
        string_on = self.search(query="settings.json", include_hidden="true")
        self.assertEqual(string_on["count"], 1)

    def test_no_results_explains_and_suggests(self):
        result = self.search(query="nothing-like-this")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 0)
        self.assertIn("No files or folders matching", result["message"])
        self.assertIn("Try a different name", result["message"])
        self.assertIn("hint", result)

    def test_broad_search_without_name_or_extension_works(self):
        # A location plus an item type is a complete request; no filename or
        # extension is required any more.
        self.make_file("notes.txt")
        result = self.search()
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)

    def test_a_file_is_not_a_search_location(self):
        target = self.make_file("notes.txt")
        result = open_file_tool.search_files(query="notes", location=str(target))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "bad_location")
        self.assertIn("not a folder", result["error"])

    def test_limit_is_respected(self):
        for index in range(5):
            self.make_file(f"slide{index}.pptx")
        result = self.search(extension="pptx", limit=2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["count"], 5)
        self.assertTrue(result["truncated"])

    def test_unreadable_subfolder_does_not_stop_the_search(self):
        target = self.make_file("readable/notes.txt")
        real_scandir = os.scandir

        def guarded(path):
            if os.path.normcase(str(path)).endswith("broken"):
                raise PermissionError("denied")
            return real_scandir(path)

        self.make_dir("broken")
        with mock.patch.object(open_file_tool.os, "scandir", guarded):
            result = self.search(query="notes.txt")
        self.assertEqual(self.paths(result), [str(target)])

    def test_duplicate_items_are_not_reported_twice(self):
        target = self.make_file("notes.txt")
        result = self.search(query="notes.txt")
        self.assertEqual(self.paths(result), [str(target)])
        self.assertEqual(len(result["results"]), len(set(self.paths(result))))

    def test_configured_search_roots_are_used(self):
        extra = self.make_dir("projects")
        (extra / "main.py").write_text("print('hi')", encoding="utf-8")
        empty = self.make_dir("elsewhere")
        with mock.patch.object(
                open_file_tool, "load_api_keys",
                return_value={"file_search_roots": [str(extra)]}), \
             mock.patch.object(
                open_file_tool, "_home_folder", return_value=""), \
             mock.patch.object(
                open_file_tool, "BASE_DIR", empty / "absent"):
            result = open_file_tool.search_files(query="main.py")

        self.assertEqual(result["searched"], [str(extra)])
        self.assertEqual(result["results"][0]["path"], str(extra / "main.py"))

    def test_search_can_be_cancelled_mid_walk(self):
        for index in range(4):
            self.make_file(f"notes-{index}.txt")

        real_score = open_file_tool._score
        calls = []

        def interrupt_on_first_match(name, path, query):
            calls.append(name)
            if len(calls) == 1:
                open_file_tool.request_cancel()
            return real_score(name, path, query)

        try:
            with mock.patch.object(
                    open_file_tool, "_score", interrupt_on_first_match):
                result = self.search(query="notes")
        finally:
            open_file_tool._CANCEL.clear()

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertIn("interrupted", result["message"])


class FuzzyQueryTests(ToolTestCase):
    """Typos and case/separator differences surface as candidates.

    The assistant still asks before opening a non-exact candidate; the tool
    never silently selects among them.
    """

    def test_a_misspelled_filename_returns_a_similar_candidate(self):
        target = self.make_file("homework.docx")
        result = self.search(query="homewrok")
        self.assertTrue(result["success"])
        self.assertEqual(self.paths(result), [str(target)])
        self.assertEqual(result["results"][0]["match"], "similar")
        self.assertIn("closest similar", result["message"])

    def test_case_and_separators_are_treated_as_exact(self):
        target = self.make_file("My Report.docx")
        result = self.search(query="my_report")
        self.assertEqual(self.paths(result), [str(target)])
        self.assertEqual(result["results"][0]["match"], "exact")

    def test_the_assistant_supplies_the_interpreted_name(self):
        target = self.make_file("requirements.txt")

        similar = self.search(query="requirments")
        self.assertEqual(self.paths(similar), [str(target)])
        self.assertEqual(similar["results"][0]["match"], "similar")

        interpreted = self.search(query="requirements.txt")
        self.assertEqual(self.paths(interpreted), [str(target)])
        self.assertEqual(interpreted["results"][0]["match"], "exact")

    def test_exact_outranks_partial_deterministically(self):
        exact = self.make_file("budget.xlsx")
        partial = self.make_file("budget plan.xlsx")
        farther = self.make_file("my budget notes.txt")
        result = self.search(query="budget")
        self.assertEqual(self.paths(result),
                         [str(exact), str(partial), str(farther)])
        self.assertEqual(result["results"][0]["match"], "exact")
        self.assertEqual(result["results"][1]["match"], "partial")
        self.assertEqual(result["results"][2]["match"], "partial")


class MatchLabelTests(ToolTestCase):
    def test_labels_are_deterministic_string_relations(self):
        self.make_file("maths homework final.docx")

        named = self.search(query="maths homework final.docx")
        self.assertEqual(named["results"][0]["match"], "exact")

        part = self.search(query="maths homework")
        self.assertEqual(part["results"][0]["match"], "partial")

        kind = self.search(extension="docx")
        self.assertEqual(kind["results"][0]["match"], "kind")

    def test_multiple_exact_matches_are_all_returned_without_selection(self):
        first = self.make_file("one/notes.txt")
        second = self.make_file("two/notes.txt")
        result = self.search(query="notes.txt")
        self.assertEqual(result["count"], 2)
        self.assertEqual(set(self.paths(result)), {str(first), str(second)})
        for item in result["results"]:
            self.assertEqual(item["match"], "exact")
            self.assertEqual(item["type"], "file")


class FuzzyPolicyTests(unittest.TestCase):
    def test_near_names_score_as_similar_not_exact(self):
        score = open_file_tool._score(
            "homework.docx", r"C:\School\homework.docx", "homewrok")
        self.assertGreater(score, 1.0)
        self.assertLess(score, 45.0)
        self.assertEqual(open_file_tool._match_label(score), "similar")

    def test_case_and_separator_variants_are_exact_names(self):
        score = open_file_tool._score(
            "My Report.docx", r"C:\School\My Report.docx", "my_report")
        self.assertEqual(score, 100.0)
        self.assertEqual(open_file_tool._match_label(score), "exact")

    def test_unrelated_names_do_not_become_similar(self):
        self.assertEqual(
            open_file_tool._score("contract.pdf", r"C:\x\contract.pdf",
                                  "holiday photo"),
            0.0)


class DriveWideningTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        # Hermetic widening: no real drives, no real user folders, an empty
        # project directory — the only "drive" is the temporary tree.
        empty = self.make_dir("empty-base")
        self._patches = [
            mock.patch.object(open_file_tool, "load_api_keys",
                              return_value={}),
            mock.patch.object(open_file_tool, "_home_folder",
                              return_value=""),
            mock.patch.object(open_file_tool, "BASE_DIR", empty),
            mock.patch.object(open_file_tool, "_local_drives",
                              return_value=[str(self.root) + "\\"]),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_widening_finds_a_file_saved_somewhere_new(self):
        target = self.make_file("deep/nested/uniquename123.txt")
        result = open_file_tool.search_files(query="uniquename123")
        self.assertEqual(self.paths(result), [str(target)])
        self.assertTrue(result["widened_to"])
        self.assertIn("widened", result["message"])

    def test_widening_finds_a_new_folder_too(self):
        folder = self.make_dir("fresh-folder-abc")
        result = open_file_tool.search_files(query="fresh-folder-abc",
                                             item_type="folder")
        self.assertEqual(self.paths(result), [str(folder)])
        self.assertTrue(result["widened_to"])

    def test_widening_finds_a_recently_created_file_by_mtime_rank(self):
        target = self.make_file("somewhere/new/brandnew-2026.pptx")
        result = open_file_tool.search_files(extension="pptx")
        self.assertIn(str(target), self.paths(result))
        self.assertTrue(result["widened_to"])

    def test_widening_tries_c_before_d_and_stops_at_the_first_hit(self):
        calls = []

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            calls.append(tuple(roots))
            if roots == ["C:\\"]:
                yield ("C:\\Users\\someone\\target-xyz.txt", False, 0.0)

        with mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "_local_drives",
                               return_value=["C:\\", "D:\\"]):
            result = open_file_tool.search_files(query="target-xyz")

        self.assertEqual(result["widened_to"], ["C:\\"])
        self.assertEqual(self.paths(result),
                         ["C:\\Users\\someone\\target-xyz.txt"])
        self.assertNotIn(("D:\\",), calls)

    def test_widening_moves_on_to_d_when_c_has_nothing(self):
        calls = []

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            calls.append(tuple(roots))
            if roots == ["D:\\"]:
                yield ("D:\\target-xyz.txt", False, 0.0)

        with mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "_local_drives",
                               return_value=["C:\\", "D:\\"]):
            result = open_file_tool.search_files(query="target-xyz")

        self.assertEqual(result["widened_to"], ["C:\\", "D:\\"])
        self.assertEqual(self.paths(result), ["D:\\target-xyz.txt"])

    def test_no_results_after_widening_says_so(self):
        self.make_dir("a/b/c")
        result = open_file_tool.search_files(query="uniquename-nothing-xyz")
        self.assertEqual(result["count"], 0)
        self.assertIn("widened", result["message"])
        self.assertIn("hint", result)

    def test_no_roots_and_no_drives_is_reported(self):
        # The project directory must stop existing too, or it remains a root.
        with mock.patch.object(open_file_tool, "_local_drives",
                               return_value=[]), \
             mock.patch.object(open_file_tool, "BASE_DIR",
                               self.root / "absent"):
            result = open_file_tool.search_files(query="anything-xyz")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_roots")


class SearchRootsTests(ToolTestCase):
    def test_usual_folders_cover_all_six_shell_folders(self):
        seen_keys = []

        def recorder(key):
            seen_keys.append(key)
            return ""

        with mock.patch.object(open_file_tool, "_home_folder", recorder), \
             mock.patch.object(open_file_tool, "_local_drives",
                               return_value=[]), \
             mock.patch.object(open_file_tool, "load_api_keys",
                               return_value={}):
            open_file_tool.search_files(query="uniquename123xyz")
        self.assertEqual(
            set(seen_keys),
            {"desktop", "documents", "downloads", "pictures", "music",
             "videos"})

    def test_local_drives_are_drive_roots(self):
        drives = open_file_tool._local_drives()
        for drive in drives:
            self.assertRegex(drive, r"^[A-Z]:\\$")

    def test_known_folder_resolution_is_cached(self):
        key = "desktop"
        with open_file_tool._HOME_FOLDER_LOCK:
            old = open_file_tool._HOME_FOLDER_CACHE.pop(key, None)
        try:
            with mock.patch.object(
                    open_file_tool, "_csidl_folder",
                    return_value=str(self.root)) as csidl:
                first = open_file_tool._home_folder(key)
                second = open_file_tool._home_folder(key)
            self.assertEqual(first, str(self.root))
            self.assertEqual(second, str(self.root))
            self.assertEqual(csidl.call_count, 1)
        finally:
            with open_file_tool._HOME_FOLDER_LOCK:
                if old is None:
                    open_file_tool._HOME_FOLDER_CACHE.pop(key, None)
                else:
                    open_file_tool._HOME_FOLDER_CACHE[key] = old

    def test_a_named_location_is_not_widened(self):
        self.make_file("uniquename456.txt")
        with mock.patch.object(open_file_tool, "_local_drives",
                               return_value=[str(self.root)]) as drives:
            result = self.search(query="uniquename456")
        self.assertEqual(self.paths(result),
                         [str(self.root / "uniquename456.txt")])
        drives.assert_not_called()
        self.assertNotIn("widened_to", result)


class BroadSearchTests(ToolTestCase):
    """Requests with no filename: a location and/or item type is enough."""

    def test_any_file_in_a_location(self):
        first = self.make_file("notes.txt")
        second = self.make_file("clip.mp4")
        self.make_dir("subfolder")
        result = self.search(item_type="file")
        self.assertTrue(result["success"])
        self.assertEqual(set(self.paths(result)), {str(first), str(second)})

    def test_any_item_includes_folders(self):
        notes = self.make_file("notes.txt")
        folder = self.make_dir("subfolder")
        result = self.search(item_type="any")
        self.assertEqual(set(self.paths(result)), {str(notes), str(folder)})

    def test_shell_folder_name_is_resolved_as_a_location(self):
        tree = self.make_dir("fake-downloads")
        (tree / "a.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(open_file_tool, "_home_folder",
                               return_value=str(tree)):
            result = open_file_tool.search_files(location="Downloads",
                                                 item_type="file")
        self.assertEqual(result["searched"], [str(tree)])
        self.assertEqual(self.paths(result), [str(tree / "a.txt")])

    def test_missing_location_is_reported(self):
        result = open_file_tool.search_files(
            location=str(self.root / "not-here"), item_type="file")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "bad_location")

    def test_empty_results_are_not_an_error(self):
        self.make_dir("empty")
        result = open_file_tool.search_files(
            location=str(self.root / "empty"), item_type="file")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 0)
        self.assertIn("No files or folders matching", result["message"])
        self.assertIn("hint", result)


class CategorySearchTests(ToolTestCase):
    """Category words expand deterministically; no filename is needed."""

    SAMPLES = {
        "image": ("shot.webp", "clip.mkv"),
        "video": ("clip.mkv", "song.flac"),
        "audio": ("song.flac", "shot.webp"),
        "document": ("report.docx", "deck.pptx"),
        "pdf": ("paper.pdf", "report.docx"),
        "spreadsheet": ("budget.csv", "report.docx"),
        "presentation": ("deck.pptx", "report.docx"),
        "text": ("config.json", "shot.webp"),
        "code": ("script.py", "report.docx"),
        "archive": ("bundle.7z", "report.docx"),
        "font": ("face.woff2", "report.docx"),
        "notebook": ("analysis.ipynb", "script.py"),
    }

    def test_each_category_finds_only_its_own_files(self):
        for category, (wanted, other) in self.SAMPLES.items():
            with self.subTest(category=category):
                folder = self.make_dir(category)
                (folder / wanted).write_text("x", encoding="utf-8")
                (folder / other).write_text("x", encoding="utf-8")
                result = open_file_tool.search_files(
                    extension=category, location=str(folder))
                self.assertTrue(result["success"])
                self.assertEqual(self.paths(result), [str(folder / wanted)])
                self.assertEqual(result["categories"], [category])

    def test_category_words_are_not_treated_as_extensions(self):
        self.make_file("meta.video")
        shot = self.make_file("shot.png")
        result = self.search(extension="images")
        self.assertEqual(self.paths(result), [str(shot)])

    def test_plural_and_synonym_category_names(self):
        target = self.make_file("rv/one.mkv")
        for value in ("videos", "video", "movie", "movies"):
            with self.subTest(value=value):
                result = self.search(extension=value)
                self.assertEqual(self.paths(result), [str(target)])

    def test_category_can_be_combined_with_an_extension(self):
        shot = self.make_file("shot.png")
        self.make_file("clip.mkv")
        result = self.search(extension="image, mp4")
        self.assertEqual(self.paths(result), [str(shot)])

    def test_spreadsheet_synonyms(self):
        target = self.make_file("budget.xlsx")
        for value in ("excel", "sheet", "sheets", "spreadsheets"):
            with self.subTest(value=value):
                result = self.search(extension=value)
                self.assertEqual(self.paths(result), [str(target)])

    def test_document_synonyms(self):
        target = self.make_file("letter.docx")
        for value in ("word", "docs"):
            with self.subTest(value=value):
                result = self.search(extension=value)
                self.assertEqual(self.paths(result), [str(target)])

    def test_non_english_category_words_are_left_to_the_assistant(self):
        # Python does not translate: the assistant turns the user's word into
        # an English category ("videos"), and the search stays deterministic.
        self.make_file("ru/clip.mkv")

        literal = self.search(extension="видео")
        self.assertEqual(literal["count"], 0)

        translated = self.search(extension="videos")
        self.assertEqual(self.paths(translated),
                         [str(self.root / "ru" / "clip.mkv")])


class NameResolutionTests(ToolTestCase):
    """Bare names and shell folders resolve where a person expects them.

    "Downloads" means the real Downloads folder; any other bare name is found
    on the drives in C: → D: order. The project folder is never consulted for
    a bare name, and it is no longer a default search root.
    """

    def _no_home(self):
        return mock.patch.object(open_file_tool, "_home_folder",
                                 return_value="")

    def test_a_shell_folder_name_opens_the_real_folder(self):
        downloaded = self.make_dir("real-downloads")
        with mock.patch.object(open_file_tool, "_home_folder",
                               return_value=str(downloaded)), \
             mock.patch.object(open_file_tool, "_local_drives",
                               return_value=[]) as drives, \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder("Downloads")

        self.assertTrue(result["success"])
        self.assertEqual(result["path"], str(downloaded))
        startfile.assert_called_once_with(str(downloaded))
        drives.assert_not_called()

    def test_a_bare_folder_name_is_located_on_c_first(self):
        target = self.make_dir("c-drive/Projects")
        searched = []

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            searched.append(tuple(roots))
            if roots == ["C:\\"]:
                yield (str(target), True, 0.0)

        with self._no_home(), \
             mock.patch.object(open_file_tool, "_local_drives",
                               return_value=["C:\\", "D:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder("Projects")

        self.assertTrue(result["success"])
        self.assertEqual(result["path"], str(target))
        self.assertEqual(searched, [("C:\\",)])
        startfile.assert_called_once_with(str(target))

    def test_a_bare_folder_name_falls_back_to_d_when_c_has_nothing(self):
        target = self.make_dir("d-drive/Projects")
        searched = []

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            searched.append(tuple(roots))
            if roots == ["D:\\"]:
                yield (str(target), True, 0.0)

        with self._no_home(), \
             mock.patch.object(open_file_tool, "_local_drives",
                               return_value=["C:\\", "D:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder("Projects")

        self.assertTrue(result["success"])
        self.assertEqual(result["path"], str(target))
        self.assertEqual(searched, [("C:\\",), ("D:\\",)])
        startfile.assert_called_once_with(str(target))

    def test_a_bare_name_not_on_any_drive_is_reported(self):
        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            if False:
                yield None

        with self._no_home(), \
             mock.patch.object(open_file_tool, "_local_drives",
                               return_value=["C:\\", "D:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder("NoSuchFolderZzz")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing")
        self.assertIn("C:\\", result["error"])
        self.assertIn("D:\\", result["error"])
        startfile.assert_not_called()

    def test_two_folders_with_the_same_name_are_ambiguous(self):
        first = self.make_dir("left/Projects")
        second = self.make_dir("right/Projects")

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            if roots == ["C:\\"]:
                yield (str(first), True, 0.0)
                yield (str(second), True, 0.0)

        with self._no_home(), \
             mock.patch.object(open_file_tool, "_local_drives",
                               return_value=["C:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder("Projects")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "ambiguous")
        self.assertEqual(set(result["candidates"]),
                         {str(first), str(second)})
        startfile.assert_not_called()

    def test_a_bare_file_name_is_located_on_the_drives(self):
        target = self.make_file("c-drive/report.xlsx")

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            if roots == ["C:\\"]:
                yield (str(target), False, 0.0)

        with self._no_home(), \
             mock.patch.object(open_file_tool, "_local_drives",
                               return_value=["C:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file("report.xlsx")

        self.assertTrue(result["success"])
        startfile.assert_called_once_with(str(target))

    def test_a_bare_name_never_resolves_into_the_project_folder(self):
        project = self.make_dir("fake-project")
        (project / "Downloads").mkdir()

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            if False:
                yield None

        with self._no_home(), \
             mock.patch.object(open_file_tool, "_local_drives",
                               return_value=["C:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "BASE_DIR", project), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder("Downloads")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing")
        startfile.assert_not_called()

    def test_a_localized_shell_folder_name_resolves(self):
        music = self.make_dir("music-real")
        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(music) if key == "music" else ""), \
             mock.patch.object(
                open_file_tool, "_display_name_for",
                return_value="Личная папка"):
            self.assertEqual(open_file_tool._known_location("Music"), str(music))
            self.assertEqual(
                open_file_tool._known_location("Личная папка"), str(music))
            self.assertEqual(
                open_file_tool._known_location("личная папка"), str(music))

    def test_search_roots_do_not_include_the_project_folder(self):
        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(self.make_dir(f"home-{key}"))), \
             mock.patch.object(open_file_tool, "load_api_keys",
                               return_value={}):
            roots, problem = open_file_tool._search_roots("")

        self.assertEqual(problem, "")
        self.assertEqual(len(roots), 6)
        self.assertNotIn(str(open_file_tool.BASE_DIR), roots)


class RelativePathResolutionTests(ToolTestCase):
    """Relative user paths resolve under real shell folders or on the drives.

    ``Pictures\\Screenshots`` and ``Downloads\\internova`` must never resolve
    against the project directory or the process working directory.
    """

    def test_search_uses_the_real_downloads_folder(self):
        downloads = self.make_dir("Downloads")
        target = downloads / "internova"
        target.mkdir()
        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(downloads) if key == "downloads"
                else ""):
            result = open_file_tool.search_files(
                location="Downloads", item_type="folder")

        self.assertEqual(result["searched"], [str(downloads)])
        self.assertEqual(self.paths(result), [str(target)])

    def test_pictures_screenshots_resolves_under_the_real_pictures(self):
        pictures = self.make_dir("Pictures")
        shots = pictures / "Screenshots"
        shots.mkdir()
        (shots / "shot.png").write_text("x", encoding="utf-8")
        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(pictures) if key == "pictures"
                else ""):
            result = open_file_tool.search_files(
                location=r"Pictures\Screenshots", extension="images")

        self.assertEqual(result["searched"], [str(shots)])
        self.assertEqual(self.paths(result), [str(shots / "shot.png")])

    def test_the_newest_screenshot_is_first(self):
        pictures = self.make_dir("Pictures")
        shots = pictures / "Screenshots"
        shots.mkdir()
        older = shots / "older.png"
        newer = shots / "newer.png"
        older.write_text("x", encoding="utf-8")
        newer.write_text("x", encoding="utf-8")
        past = time.time() - 3 * 86400
        os.utime(older, (past, past))
        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(pictures) if key == "pictures"
                else ""):
            result = open_file_tool.search_files(
                location=r"Pictures\Screenshots", extension="images")

        self.assertEqual(self.paths(result)[0], str(newer))

    def test_missing_screenshots_under_pictures_is_found_on_c(self):
        pictures = self.make_dir("Pictures")          # no Screenshots inside
        located = self.make_dir("redirected/Pictures/Screenshots")

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            if roots == ["C:\\"]:
                yield (str(located), True, 0.0)

        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(pictures) if key == "pictures"
                else ""), \
             mock.patch.object(
                open_file_tool, "_local_drives",
                return_value=["C:\\", "D:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk):
            result = open_file_tool.search_files(
                location=r"Pictures\Screenshots", item_type="folder")

        self.assertEqual(result["searched"], [str(located)])

    def test_missing_screenshots_falls_back_to_d_when_c_has_nothing(self):
        pictures = self.make_dir("Pictures")
        located = self.make_dir("backup/Pictures/Screenshots")

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            if roots == ["D:\\"]:
                yield (str(located), True, 0.0)

        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(pictures) if key == "pictures"
                else ""), \
             mock.patch.object(
                open_file_tool, "_local_drives",
                return_value=["C:\\", "D:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk):
            result = open_file_tool.search_files(
                location=r"Pictures\Screenshots", item_type="folder")

        self.assertEqual(result["searched"], [str(located)])

    def test_open_folder_resolves_downloads_internova(self):
        downloads = self.make_dir("Downloads")
        target = downloads / "internova"
        target.mkdir()
        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(downloads) if key == "downloads"
                else ""), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder(r"Downloads\internova")

        self.assertTrue(result["success"])
        self.assertEqual(result["path"], str(target))
        startfile.assert_called_once_with(str(target))

    def test_open_folder_finds_a_relocated_subfolder_on_c(self):
        downloads = self.make_dir("Downloads")        # no internova inside
        located = self.make_dir("moved/internova")

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            if roots == ["C:\\"]:
                yield (str(located), True, 0.0)

        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(downloads) if key == "downloads"
                else ""), \
             mock.patch.object(
                open_file_tool, "_local_drives", return_value=["C:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder(r"Downloads\internova")

        self.assertTrue(result["success"])
        self.assertEqual(result["path"], str(located))
        startfile.assert_called_once_with(str(located))

    def test_a_nonexistent_relative_folder_is_reported(self):
        downloads = self.make_dir("Downloads")

        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            if False:
                yield None

        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(downloads) if key == "downloads"
                else ""), \
             mock.patch.object(
                open_file_tool, "_local_drives", return_value=["C:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder(r"Downloads\internova")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing")
        self.assertIn(r"Downloads\internova", result["error"])
        self.assertIn("not found in C:\\", result["error"])
        startfile.assert_not_called()

    def test_a_search_result_path_opens_as_a_folder(self):
        downloads = self.make_dir("Downloads")
        target = downloads / "internova"
        target.mkdir()
        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(downloads) if key == "downloads"
                else ""):
            found = open_file_tool.search_files(
                location="Downloads", item_type="folder")

        path = found["results"][0]["path"]
        self.assertTrue(os.path.isabs(path))
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            opened = open_file_tool.open_folder(path)

        self.assertTrue(opened["success"])
        startfile.assert_called_once_with(path)


class SearchAuditRegressionTests(ToolTestCase):
    """Regressions for the search/path audit findings."""

    # ── known folders ────────────────────────────────────────────────────────
    def test_screenshots_is_derived_from_the_pictures_known_folder(self):
        pictures = self.make_dir("Pictures")
        (pictures / "Screenshots").mkdir()
        with mock.patch.object(
                open_file_tool, "_csidl_folder",
                side_effect=lambda csidl: str(pictures) if csidl == 0x27
                else ""):
            self.assertEqual(open_file_tool._home_folder("screenshots"),
                             str(pictures / "Screenshots"))

    def test_screenshots_is_a_known_location_name(self):
        shots = self.make_dir("Shots")
        with mock.patch.object(
                open_file_tool, "_home_folder",
                side_effect=lambda key: str(shots) if key == "screenshots"
                else ""):
            self.assertEqual(open_file_tool._known_location("Screenshots"),
                             str(shots))
            self.assertEqual(open_file_tool._known_location("screenshot"),
                             str(shots))
            base, rest = open_file_tool._split_known_prefix(
                r"Screenshots\2026")
        self.assertEqual(base, str(shots))
        self.assertEqual(rest, "2026")

    # ── drive forms ──────────────────────────────────────────────────────────
    @unittest.skipUnless(open_file_tool._IS_WIN, "Windows drive letters")
    def test_a_bare_drive_is_its_root(self):
        result = open_file_tool.resolve_open_target("C:", expect="folder")
        self.assertTrue(result["ok"])
        self.assertEqual(os.path.normcase(result["path"]),
                         os.path.normcase("C:\\"))

    @unittest.skipUnless(open_file_tool._IS_WIN, "Windows drive letters")
    def test_a_drive_relative_path_keeps_the_named_drive(self):
        result = open_file_tool.resolve_open_target("C:Users", expect="folder")
        self.assertTrue(result["ok"])
        self.assertEqual(os.path.normcase(result["path"]),
                         os.path.normcase("C:\\Users"))

    def test_a_drive_relative_path_searches_only_that_drive(self):
        with mock.patch.object(
                open_file_tool, "_locate_relative",
                return_value={"ok": False, "path": "", "item_type": "",
                              "reason": "missing", "error": "x"}) as locator:
            open_file_tool.resolve_open_target("C:NoSuchFolderZzz",
                                               expect="folder")
        locator.assert_called_once_with("NoSuchFolderZzz", "folder",
                                        drives=["C:\\"])

    def test_a_root_relative_path_is_rejected_without_a_drive(self):
        with mock.patch.object(open_file_tool, "_local_drives") as drives:
            result = open_file_tool.resolve_open_target(
                r"\folder", expect="folder")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid")
        self.assertIn("does not include a drive", result["error"])
        drives.assert_not_called()

        searched = open_file_tool.search_files(location=r"\folder")
        self.assertFalse(searched["success"])
        self.assertEqual(searched["reason"], "bad_location")

    def test_unc_paths_are_treated_as_absolute(self):
        with mock.patch.object(
                open_file_tool, "validate_path",
                return_value={"ok": False, "path": "", "item_type": "",
                              "reason": "missing", "error": "x"}) as validator:
            open_file_tool.resolve_open_target(
                r"\\server\share\folder", expect="folder")
        validator.assert_called_once_with(r"\\server\share\folder", "folder")

    # ── path normalisation ──────────────────────────────────────────────────
    def test_dot_components_in_an_absolute_path_are_normalised(self):
        target = self.make_file("sub/notes.txt")
        raw = os.path.join(str(self.root), "sub", "..", "sub", "notes.txt")
        result = open_file_tool.validate_path(raw)
        self.assertTrue(result["ok"])
        self.assertEqual(os.path.normcase(result["path"]),
                         os.path.normcase(str(target)))

    def test_relative_dot_components_are_rejected(self):
        result = open_file_tool.open_folder(r".\sub")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid")
        self.assertIn("working directory", result["error"])

    def test_forward_slashes_are_accepted(self):
        folder = self.make_dir("slash/sub")
        raw = str(self.root).replace("\\", "/") + "/slash/sub"
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder(raw)
        self.assertTrue(result["success"])
        startfile.assert_called_once_with(str(folder))

    def test_surrounding_whitespace_is_trimmed(self):
        folder = self.make_dir("With Space")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder(f"  {folder}  ")
        self.assertTrue(result["success"])
        self.assertEqual(result["path"], str(folder))
        startfile.assert_called_once_with(str(folder))

        searched = open_file_tool.search_files(
            location=f"  {folder}  ", item_type="file")
        self.assertEqual(searched["searched"], [str(folder)])

    def test_a_longer_name_inside_a_folder_with_spaces_survives(self):
        folder = self.make_dir("My Projects (2026)")
        target = folder / "Отчёт за 2026 год (final).docx"
        target.write_text("x", encoding="utf-8")
        result = open_file_tool.search_files(
            location=str(folder), item_type="file")
        self.assertEqual([item["path"] for item in result["results"]],
                         [str(target)])
        self.assertTrue(os.path.isabs(result["results"][0]["path"]))

    def test_unavailable_drive_is_reported(self):
        with mock.patch.object(Path, "exists", return_value=False):
            result = open_file_tool.validate_path(r"Q:\NoSuch\Thing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing")
        self.assertIn("drive may not be connected", result["error"])

    # ── search integrity ────────────────────────────────────────────────────
    def test_invalid_item_type_is_reported(self):
        result = open_file_tool.search_files(
            item_type="banana", location=str(self.root))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "bad_item_type")
        self.assertIn("file", result["error"])
        self.assertIn("folder", result["error"])

    def test_extension_filter_accepts_a_list(self):
        target = self.make_file("paper.pdf")
        self.make_file("notes.txt")
        result = self.search(extension=["pdf"])
        self.assertEqual(self.paths(result), [str(target)])

    def test_a_search_reports_when_its_time_ran_out(self):
        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            raise open_file_tool._SearchTimeout()
            yield

        with mock.patch.object(open_file_tool, "_walk", fake_walk):
            result = self.search(query="anything")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 0)
        self.assertTrue(result["timed_out"])
        self.assertIn("time limit", result["message"])

    def test_an_incomplete_relative_scan_is_not_reported_as_absent(self):
        def fake_walk(roots, include_hidden, deadline, cancel, max_depth=6):
            raise open_file_tool._SearchTimeout()
            yield

        with mock.patch.object(
                open_file_tool, "_home_folder", return_value=""), \
             mock.patch.object(
                open_file_tool, "_local_drives", return_value=["C:\\"]), \
             mock.patch.object(open_file_tool, "_walk", fake_walk):
            result = open_file_tool.open_folder("NoSuchFolderZzz")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing")
        self.assertIn("time limit", result["error"])
        self.assertIn("may still be there", result["error"])

    # ── traversal ───────────────────────────────────────────────────────────
    def test_a_junction_is_reported_as_a_folder_but_not_followed(self):
        reparse = open_file_tool._FILE_ATTRIBUTE_REPARSE_POINT
        directory = open_file_tool._FILE_ATTRIBUTE_DIRECTORY

        class FakeEntry:
            def __init__(self, name, path, real_dir, attrs):
                self.name = name
                self.path = path
                self._real_dir = real_dir
                self._attrs = attrs

            def is_dir(self, follow_symlinks=True):
                return self._real_dir

            def is_symlink(self):
                return False

            def stat(self, follow_symlinks=True):
                return SimpleNamespace(st_mtime=1.0,
                                       st_file_attributes=self._attrs)

        link = FakeEntry("linkdir", r"C:\fake\linkdir", False,
                         reparse | directory)
        real = FakeEntry("realdir", r"C:\fake\realdir", True, directory)
        calls = []

        class FakeScandir:
            def __init__(self, entries):
                self.entries = entries

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, *exc):
                return False

        def fake_scandir(path):
            calls.append(path)
            if os.path.normcase(str(path)) == os.path.normcase(r"C:\fake"):
                return FakeScandir([link, real])
            return FakeScandir([])

        import threading
        with mock.patch.object(open_file_tool.os, "scandir", fake_scandir):
            found = list(open_file_tool._walk(
                [r"C:\fake"], False, time.monotonic() + 5,
                threading.Event()))

        by_path = {path: is_dir for path, is_dir, _ in found}
        self.assertTrue(by_path[r"C:\fake\linkdir"])
        self.assertTrue(by_path[r"C:\fake\realdir"])
        self.assertNotIn(os.path.normcase(r"C:\fake\linkdir"),
                         {os.path.normcase(str(call)) for call in calls})
        self.assertIn(os.path.normcase(r"C:\fake\realdir"),
                      {os.path.normcase(str(call)) for call in calls})


class SelectionTests(ToolTestCase):
    """Latest and random requests: the list supports both without asking."""

    def test_latest_video_is_first_and_opens(self):
        older = self.make_file("vids/old.mp4")
        newer = self.make_file("vids/new.mp4")
        past = time.time() - 3 * 86400
        os.utime(older, (past, past))
        result = self.search(extension="videos")
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.paths(result)[0], str(newer))

        with mock.patch.object(
                open_file_tool, "_associated_handler",
                return_value=r"C:\Windows\System32\notepad.exe"), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            opened = tool_calling.execute_tool(
                "open_file", {"path": result["results"][0]["path"]})
        self.assertTrue(opened["success"])
        startfile.assert_called_once_with(str(newer))

    def test_any_candidate_can_be_opened(self):
        for index in range(3):
            self.make_file(f"pics/p{index}.png")
        result = self.search(extension="images")
        self.assertEqual(result["count"], 3)
        # The assistant picks one from the candidates; every returned path is
        # complete and directly openable.
        chosen = result["results"][1]
        with mock.patch.object(
                open_file_tool, "_associated_handler",
                return_value=r"C:\Windows\System32\notepad.exe"), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            opened = tool_calling.execute_tool(
                "open_file", {"path": chosen["path"]})
        self.assertTrue(opened["success"])
        startfile.assert_called_once_with(chosen["path"])

    def test_dangerous_files_found_by_a_broad_search_still_need_confirmation(self):
        exe = self.make_file("tool.exe")
        result = self.search(item_type="file")
        self.assertIn(str(exe), self.paths(result))

        with mock.patch.object(open_file_tool, "_startfile") as startfile, \
             mock.patch.object(open_file_tool, "_popen_launched") as launched:
            opened = tool_calling.execute_tool(
                "open_file", {"path": str(exe)})
        self.assertTrue(opened["approval_required"])
        startfile.assert_not_called()
        launched.assert_not_called()


class WalkOrderTests(unittest.TestCase):
    def test_walk_visits_roots_in_the_order_given(self):
        # The drive that will be widened first depends on this: a stack pops
        # the last item, so roots must be pushed reversed.
        with tempfile.TemporaryDirectory() as first, \
                tempfile.TemporaryDirectory() as second:
            Path(first, "a.txt").write_text("x", encoding="utf-8")
            Path(second, "b.txt").write_text("x", encoding="utf-8")
            paths = [path for path, _, _ in open_file_tool._walk(
                [first, second], False, time.monotonic() + 5,
                open_file_tool.threading.Event())]
        self.assertTrue(paths[0].startswith(first))
        self.assertTrue(paths[-1].startswith(second))


class OpenFileTests(ToolTestCase):
    def test_opens_a_text_file_with_the_default_application(self):
        target = self.make_file("notes.txt")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target))

        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "open_file")
        self.assertEqual(result["opened"], str(target))
        self.assertEqual(result["item_type"], "file")
        startfile.assert_called_once_with(str(target))

    def test_string_false_confirmation_does_not_bypass_the_gate(self):
        target = self.make_file("install.cmd", "@echo off")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target), confirm="false")

        self.assertTrue(result["approval_required"])
        self.assertFalse(result["success"])
        startfile.assert_not_called()

    def test_unfamiliar_extension_is_not_rejected(self):
        target = self.make_file("dataset.weirdformat")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target))
        self.assertTrue(result["success"])
        startfile.assert_called_once_with(str(target))

    def test_extensionless_conventional_file_opens(self):
        target = self.make_file("Dockerfile", "FROM python:3.12")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target))
        self.assertTrue(result["success"])
        startfile.assert_called_once_with(str(target))

    def test_missing_file_is_not_opened(self):
        missing = self.root / "gone.docx"
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(missing))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing")
        self.assertIn(str(missing), result["error"])
        startfile.assert_not_called()

    def test_folder_path_is_rejected_with_guidance(self):
        folder = self.make_dir("Projects")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(folder))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "wrong_type")
        self.assertIn("folder, not a file", result["error"])
        startfile.assert_not_called()

    def test_picker_is_reported_instead_of_claiming_success(self):
        target = self.make_file("notes.aze-unknown-ext")
        with mock.patch.object(
                open_file_tool, "_associated_handler", return_value=""), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_association")
        self.assertIn("no application registered", result["error"])
        startfile.assert_not_called()

    def test_no_registered_application_is_explained(self):
        target = self.make_file("notes.unknown")
        error = OSError(
            1155,
            "No application is associated with the specified file for this "
            "operation.")
        with mock.patch.object(
                open_file_tool, "_startfile", side_effect=error):
            result = open_file_tool.open_file(str(target))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_association")
        self.assertIn("no application registered", result["error"])
        self.assertIn("app=", result["error"])

    def test_paths_with_spaces_and_non_ascii_open(self):
        target = self.make_file("Мои документы/домашка 2.docx")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target))
        self.assertTrue(result["success"])
        startfile.assert_called_once_with(str(target))

    def test_relative_paths_are_never_resolved_from_the_working_directory(self):
        # A file that happens to sit in the process working directory must not
        # be opened from a relative argument; user paths resolve through the
        # known folders and the drives only.
        self.make_file("notes.txt")
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            with mock.patch.object(
                    open_file_tool, "_home_folder", return_value=""), \
                 mock.patch.object(
                    open_file_tool, "_local_drives", return_value=[]), \
                 mock.patch.object(open_file_tool, "_startfile") as startfile:
                result = open_file_tool.open_file("notes.txt")
        finally:
            os.chdir(previous)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing")
        startfile.assert_not_called()

    def test_executables_installers_scripts_and_shortcuts_need_confirmation(self):
        for name in ("app.exe", "setup.msi", "package.msix", "build.bat",
                     "script.ps1", "main.py", "link.lnk", "site.url",
                     "bundle.jar", "install.sh", "console.msc",
                     "deploy.ps2", "start.website", "report.docm",
                     "budget.xlsm", "slides.pptm"):
            with self.subTest(name=name):
                target = self.make_file(name)
                with mock.patch.object(open_file_tool, "_startfile") as startfile:
                    result = open_file_tool.open_file(str(target))
                self.assertFalse(result["success"])
                self.assertTrue(result["approval_required"])
                self.assertIn("must approve", result["message"])
                startfile.assert_not_called()

    def test_confirmation_message_names_the_risk_grammatically(self):
        cases = {
            "setup.msi": "is an installer file",
            "app.exe": "is an executable file",
            "main.py": "is a script file",
            "link.lnk": "is a shortcut file",
        }
        for name, phrase in cases.items():
            with self.subTest(name=name):
                target = self.make_file(name)
                result = open_file_tool.open_file(str(target))
                self.assertIn(phrase, result["message"])

    def test_confirmed_script_is_opened(self):
        target = self.make_file("main.py")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            blocked = open_file_tool.open_file(str(target), confirm=True)
            self.assertTrue(blocked["approval_required"])
            startfile.assert_not_called()
            self.approve_next()
            result = open_file_tool.open_file(str(target), confirm=True)
        self.assertTrue(result["success"])
        startfile.assert_called_once_with(str(target))

    def test_ordinary_document_never_launches_a_process(self):
        target = self.make_file("homework.docx")
        with mock.patch.object(open_file_tool, "_startfile") as startfile, \
             mock.patch.object(open_file_tool.subprocess, "Popen") as popen:
            result = open_file_tool.open_file(str(target))
        self.assertTrue(result["success"])
        startfile.assert_called_once_with(str(target))
        popen.assert_not_called()

    def test_requested_application_is_missing(self):
        target = self.make_file("notes.txt")
        with mock.patch.object(
                open_app_tool, "get_installed_programs", return_value=[]), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target), app="Neverware")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "app_not_installed")
        self.assertIn("list_apps", result["error"])
        startfile.assert_not_called()

    def test_requested_application_receives_the_file(self):
        target = self.make_file("main.py")
        link = self.root / "Visual Studio Code.lnk"
        link.write_text("", encoding="utf-8")
        app = shortcut_app("Visual Studio Code", link)
        with mock.patch.object(
                open_app_tool, "get_installed_programs", return_value=[app]), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(
                str(target), app="Visual Studio Code", confirm=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["via"], "Visual Studio Code")
        startfile.assert_called_once_with(str(link), "open", f'"{target}"')

    def test_a_named_interpreter_does_not_bypass_confirmation(self):
        # Handing an executable to a named launcher runs it; only known
        # viewers/editors may skip the gate.
        target = self.make_file("setup.exe", "MZ")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target), app="Python")
        self.assertTrue(result["approval_required"])
        startfile.assert_not_called()

    def test_store_app_cannot_be_handed_a_file(self):
        target = self.make_file("notes.txt")
        app = {"name": "Notepad", "source": "uwp",
               "launch": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"}
        with mock.patch.object(
                open_app_tool, "get_installed_programs", return_value=[app]), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target), app="Notepad")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "app_argument_unsupported")
        self.assertIn("Store app", result["error"])
        startfile.assert_not_called()


class AssociationQueryTests(unittest.TestCase):
    """The real shell query, so the picker detection is not only a mock."""

    def test_picker_is_recognised(self):
        self.assertEqual(
            open_file_tool._associated_handler("C:\\tmp\\file.aze-unknown-ext"),
            "")

    def test_a_registered_handler_is_found(self):
        handler = open_file_tool._associated_handler("C:\\tmp\\file.txt")
        self.assertTrue(handler)
        self.assertNotIn("openwith", str(handler).casefold())

    def test_store_packaged_progid_counts_as_a_handler(self):
        # Photos and Media Player register a ProgID whose command strings are
        # not exposed; the shell still opens those files.
        def answer(kind, extension):
            if kind == open_file_tool._ASSOCSTR_PROGID:
                return "AppXqj98qxeaynz6dv4459ayz6bnqxbyaqcs"
            return ""

        with mock.patch.object(
                open_file_tool, "_assoc_string", side_effect=answer):
            self.assertTrue(
                open_file_tool._associated_handler("C:\\tmp\\movie.mp4"))

    def test_unknown_progid_is_not_a_handler(self):
        def answer(kind, extension):
            return "Unknown" if kind == open_file_tool._ASSOCSTR_PROGID else ""

        with mock.patch.object(
                open_file_tool, "_assoc_string", side_effect=answer):
            self.assertEqual(
                open_file_tool._associated_handler("C:\\tmp\\photo.png"), "")

    def test_extensionless_file_has_no_registered_handler(self):
        self.assertEqual(
            open_file_tool._associated_handler("C:\\tmp\\Makefile"), "")

    def test_extensionless_text_file_opens_in_an_editor(self):
        # An extensionless file (Makefile, LICENSE) has no association by
        # extension; the layered strategy displays it in an editor instead of
        # refusing or showing the picker. Nothing executes.
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "Makefile"
            target.write_text("all:\n", encoding="utf-8")
            with mock.patch.object(
                    open_file_tool, "_startfile") as startfile, \
                 mock.patch.object(
                    open_file_tool, "_resolve_candidate",
                    return_value="C:\\Windows\\System32\\notepad.exe"), \
                 mock.patch.object(
                    open_file_tool, "_popen_launched",
                    return_value=(True, "")) as launched:
                result = open_file_tool.open_file(str(target))
        self.assertTrue(result["success"])
        self.assertTrue(result["fallback"])
        self.assertEqual(result["via"], "Notepad")
        self.assertIn("no default application", result["message"])
        startfile.assert_not_called()
        launched.assert_called_once()

    def test_extensionless_file_without_an_editor_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "Makefile"
            target.write_text("all:\n", encoding="utf-8")
            with mock.patch.object(
                    open_file_tool, "_resolve_candidate", return_value=None), \
                 mock.patch.object(
                    open_file_tool, "_popen_launched") as launched:
                result = open_file_tool.open_file(str(target))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_usable_application")
        launched.assert_not_called()


class OpenFolderTests(ToolTestCase):
    def test_opens_a_folder(self):
        folder = self.make_dir("Aze project")
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder(str(folder))
        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "open_folder")
        self.assertEqual(result["item_type"], "folder")
        startfile.assert_called_once_with(str(folder))

    def test_missing_folder_is_reported(self):
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder(str(self.root / "Nowhere"))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing")
        startfile.assert_not_called()

    def test_file_path_is_rejected(self):
        target = self.make_file("notes.txt")
        result = open_file_tool.open_folder(str(target))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "wrong_type")
        self.assertIn("file, not a folder", result["error"])

    def test_folder_opens_in_a_named_application(self):
        folder = self.make_dir("Aze project")
        link = self.root / "Visual Studio Code.lnk"
        link.write_text("", encoding="utf-8")
        app = shortcut_app("Visual Studio Code", link)
        with mock.patch.object(
                open_app_tool, "get_installed_programs", return_value=[app]), \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_folder(
                str(folder), app="Visual Studio Code")

        self.assertTrue(result["success"])
        self.assertEqual(result["item_type"], "folder")
        startfile.assert_called_once_with(str(link), "open", f'"{folder}"')


class RevealTests(ToolTestCase):
    def test_reveals_a_file_in_its_folder(self):
        target = self.make_file("School/homework.docx")
        with mock.patch.object(open_file_tool.subprocess, "Popen") as popen:
            result = open_file_tool.open_containing_folder(str(target))

        self.assertTrue(result["success"])
        self.assertEqual(result["revealed"], "homework.docx")
        self.assertEqual(result["folder"], str(target.parent))
        command = popen.call_args.args[0]
        self.assertEqual(command, f'explorer.exe /select,"{target}"')

    def test_reveals_a_folder_in_its_parent(self):
        folder = self.make_dir("School/maths")
        with mock.patch.object(open_file_tool.subprocess, "Popen") as popen:
            result = open_file_tool.open_containing_folder(str(folder))

        self.assertTrue(result["success"])
        self.assertEqual(result["item_type"], "folder")
        self.assertEqual(result["folder"], str(folder.parent))
        self.assertIn("is a folder", result["message"])
        self.assertEqual(popen.call_args.args[0],
                         f'explorer.exe /select,"{folder}"')

    def test_missing_target_is_reported(self):
        with mock.patch.object(open_file_tool.subprocess, "Popen") as popen:
            result = open_file_tool.open_containing_folder(
                str(self.root / "gone.txt"))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing")
        popen.assert_not_called()

    def test_drive_root_opens_itself(self):
        root_path = Path(os.path.abspath(os.sep))
        with mock.patch.object(open_file_tool, "_shell_open") as shell:
            result = open_file_tool.open_containing_folder(str(root_path))
        self.assertTrue(result["success"])
        self.assertIn("drive root", result["message"])
        shell.assert_called_once_with(str(root_path))


class FallbackOpenTests(ToolTestCase):
    """The layered application selection behind the default association.

    Every launch goes through the ``_popen_launched`` seam, so no real
    application starts; the association query is mocked per test because setUp
    points it at Notepad.
    """

    def _no_assoc(self):
        return mock.patch.object(
            open_file_tool, "_associated_handler", return_value="")

    def test_webp_without_association_uses_an_image_app(self):
        target = self.make_file("photo.webp")
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate",
                side_effect=lambda key:
                "C:\\Windows\\System32\\mspaint.exe" if key == "paint"
                else None), \
             mock.patch.object(
                open_file_tool, "_popen_launched",
                return_value=(True, "")) as launched, \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target))

        self.assertTrue(result["success"])
        self.assertTrue(result["fallback"])
        self.assertEqual(result["via"], "Paint")
        self.assertIn("no default application", result["message"])
        startfile.assert_not_called()
        self.assertEqual(launched.call_args.args[1], str(target))

    def test_svg_without_association_uses_a_browser(self):
        target = self.make_file("diagram.svg")
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate",
                side_effect=lambda key:
                "C:\\Browsers\\brave.exe" if key == "brave" else None), \
             mock.patch.object(
                open_file_tool, "_popen_launched",
                return_value=(True, "")) as launched, \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target))

        self.assertTrue(result["success"])
        self.assertEqual(result["via"], "Brave")
        self.assertTrue(result["fallback"])
        startfile.assert_not_called()
        arg = launched.call_args.args[1]
        self.assertTrue(arg.startswith("file:///"), arg)
        self.assertIn("diagram.svg", arg)

    def test_video_without_association_uses_a_media_player(self):
        target = self.make_file("holiday.mkv")
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate",
                side_effect=lambda key:
                "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"
                if key == "vlc" else None), \
             mock.patch.object(
                open_file_tool, "_popen_launched", return_value=(True, "")):
            result = open_file_tool.open_file(str(target))

        self.assertTrue(result["success"])
        self.assertEqual(result["via"], "VLC media player")
        self.assertTrue(result["fallback"])

    def test_audio_without_association_uses_an_audio_player(self):
        target = self.make_file("song.flac")
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate",
                side_effect=lambda key:
                "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"
                if key == "vlc" else None), \
             mock.patch.object(
                open_file_tool, "_popen_launched", return_value=(True, "")):
            result = open_file_tool.open_file(str(target))

        self.assertTrue(result["success"])
        self.assertEqual(result["via"], "VLC media player")
        self.assertTrue(result["fallback"])

    def test_archive_without_association_uses_an_archive_manager(self):
        target = self.make_file("backup.7z")
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate",
                side_effect=lambda key:
                "C:\\Program Files\\7-Zip\\7zFM.exe" if key == "sevenzip"
                else None), \
             mock.patch.object(
                open_file_tool, "_popen_launched",
                return_value=(True, "")) as launched, \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target))

        self.assertTrue(result["success"])
        self.assertEqual(result["via"], "7-Zip")
        self.assertTrue(result["fallback"])
        startfile.assert_not_called()
        # The archive is handed to the manager as a path to DISPLAY; there is
        # no extraction step in the call at all.
        self.assertEqual(launched.call_args.args[1], str(target))

    def test_text_file_without_association_uses_an_editor(self):
        target = self.make_file("settings.json")
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate",
                side_effect=lambda key:
                "C:\\Windows\\System32\\notepad.exe" if key == "notepad"
                else None), \
             mock.patch.object(
                open_file_tool, "_popen_launched", return_value=(True, "")):
            result = open_file_tool.open_file(str(target))

        self.assertTrue(result["success"])
        self.assertEqual(result["via"], "Notepad")
        self.assertTrue(result["fallback"])

    def test_unknown_extension_without_any_compatible_application(self):
        target = self.make_file("data.xyzzy")
        with self._no_assoc(), \
             mock.patch.object(open_file_tool, "_popen_launched") as launched, \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target))

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_association")
        self.assertIn("no application registered", result["error"])
        launched.assert_not_called()
        startfile.assert_not_called()

    def test_next_candidate_is_tried_when_the_first_fails(self):
        target = self.make_file("notes.txt")
        exes = {"notepad": "C:\\Windows\\System32\\notepad.exe",
                "notepadpp": "C:\\Program Files\\Notepad++\\notepad++.exe"}
        outcomes = [(False, "the application exited at once (code 1)"),
                    (True, "")]
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate",
                side_effect=lambda key: exes.get(key)), \
             mock.patch.object(
                open_file_tool, "_popen_launched",
                side_effect=lambda exe, arg: outcomes.pop(0)) as launched:
            result = open_file_tool.open_file(str(target))

        self.assertTrue(result["success"])
        self.assertEqual(result["via"], "Notepad++")
        self.assertEqual(result["attempts"][0]["app"], "Notepad")
        self.assertEqual(result["attempts"][0]["error"],
                         "the application exited at once (code 1)")
        self.assertEqual(launched.call_count, 2)

    def test_when_every_candidate_fails_the_error_holds_the_attempts(self):
        target = self.make_file("notes.txt")
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate",
                side_effect=lambda key:
                "C:\\Windows\\System32\\notepad.exe" if key == "notepad"
                else None), \
             mock.patch.object(
                open_file_tool, "_popen_launched",
                return_value=(False, "the application exited at once (code 3)")):
            result = open_file_tool.open_file(str(target))

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_usable_application")
        self.assertIn("no installed application that can open it",
                      result["error"])
        self.assertEqual(result["attempts"][0]["app"], "Notepad")

    def test_dangerous_files_need_confirmation_even_without_an_association(self):
        for name in ("setup.msi", "tool.exe", "build.bat", "link.lnk"):
            with self.subTest(name=name):
                target = self.make_file(name)
                with self._no_assoc(), \
                     mock.patch.object(
                        open_file_tool, "_startfile") as startfile, \
                     mock.patch.object(
                        open_file_tool, "_popen_launched") as launched:
                    result = open_file_tool.open_file(str(target))
                self.assertTrue(result["approval_required"])
                self.assertEqual(result["reason"], "approval_required")
                startfile.assert_not_called()
                launched.assert_not_called()

    def test_script_without_an_association_is_displayed_not_executed(self):
        target = self.make_file("analyze.py")
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate",
                side_effect=lambda key:
                "C:\\Program Files\\Microsoft VS Code\\Code.exe"
                if key == "vscode" else None), \
             mock.patch.object(
                open_file_tool, "_popen_launched",
                return_value=(True, "")) as launched, \
             mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = open_file_tool.open_file(str(target))

        # No confirmation gate: an editor displays the script, it does not
        # run it.
        self.assertTrue(result["success"])
        self.assertEqual(result["via"], "Visual Studio Code")
        self.assertEqual(launched.call_args.args[1], str(target))
        startfile.assert_not_called()

    def test_script_opened_in_a_named_editor_needs_no_confirmation(self):
        # Handing a script to a named application passes the path as data; it
        # does not go through the shell's execute association. This is the
        # "open a random Python file in VS Code" case.
        target = self.make_file("main.py")
        link = self.root / "Visual Studio Code.lnk"
        link.write_text("", encoding="utf-8")
        app = shortcut_app("Visual Studio Code", link)
        with mock.patch.object(
                open_app_tool, "get_installed_programs", return_value=[app]), \
             mock.patch.object(open_file_tool, "_startfile") as startfile, \
             mock.patch.object(open_file_tool, "_popen_launched") as launched:
            result = open_file_tool.open_file(
                str(target), app="Visual Studio Code")

        self.assertTrue(result["success"])
        self.assertEqual(result["via"], "Visual Studio Code")
        startfile.assert_called_once_with(str(link), "open", f'"{target}"')
        launched.assert_not_called()

    def test_script_with_an_association_still_needs_confirmation(self):
        target = self.make_file("analyze.py")
        # setUp points the association at Notepad, so the default open would
        # hand the script to a registered handler.
        with mock.patch.object(open_file_tool, "_startfile") as startfile, \
             mock.patch.object(open_file_tool, "_popen_launched") as launched:
            result = open_file_tool.open_file(str(target))
        self.assertTrue(result["approval_required"])
        startfile.assert_not_called()
        launched.assert_not_called()

    def test_unicode_and_spaced_paths_reach_the_fallback_unchanged(self):
        target = self.make_file("Мои файлы/видео (черновик).mkv")
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate",
                side_effect=lambda key:
                "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"
                if key == "vlc" else None), \
             mock.patch.object(
                open_file_tool, "_popen_launched",
                return_value=(True, "")) as launched:
            result = open_file_tool.open_file(str(target))

        self.assertTrue(result["success"])
        self.assertEqual(launched.call_args.args[1], str(target))

    def test_fallback_failure_when_the_default_launch_fails(self):
        target = self.make_file("notes.txt")
        with self._no_assoc(), \
             mock.patch.object(
                open_file_tool, "_startfile",
                side_effect=OSError(
                    1155, "No application is associated with the specified "
                          "file for this operation.")), \
             mock.patch.object(
                open_file_tool, "_resolve_candidate", return_value=None), \
             mock.patch.object(open_file_tool, "_popen_launched") as launched:
            result = open_file_tool.open_file(str(target))
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_usable_application")
        launched.assert_not_called()


@unittest.skipUnless(open_file_tool._IS_WIN, "Windows only")
class RealResolutionTests(unittest.TestCase):
    """One real existence check, so the discovery code is not only a mock."""

    def test_notepad_resolves_through_its_known_location(self):
        open_file_tool.refresh_app_cache()
        try:
            exe = open_file_tool._resolve_candidate("notepad")
            self.assertTrue(exe)
            self.assertTrue(exe.casefold().endswith("notepad.exe"))
            self.assertTrue(os.path.isfile(exe))
        finally:
            open_file_tool.refresh_app_cache()


class RegistryIntegrationTests(ToolTestCase):
    def test_tools_are_declared_for_the_model(self):
        declarations = {
            item["name"]: item for item in tool_calling.get_tool_declarations()
        }
        for name in ("search_files", "open_file", "open_folder",
                     "open_containing_folder"):
            with self.subTest(name=name):
                self.assertIn(name, declarations)
                self.assertEqual(
                    declarations[name]["parameters"]["type"], "OBJECT")

        self.assertEqual(
            declarations["open_file"]["parameters"]["required"], ["path"])
        self.assertIn(
            "app", declarations["open_file"]["parameters"]["properties"])
        self.assertIn(
            "extension", declarations["search_files"]["parameters"]["properties"])
        self.assertIn(
            "approval_required", declarations["open_file"]["description"])

    def test_search_declaration_teaches_categories_and_broad_searches(self):
        declarations = {item["name"]: item
                        for item in tool_calling.get_tool_declarations()}
        description = declarations["search_files"]["description"].lower()
        for word in ("optional", "category", "image", "video", "document",
                     "notebook", "location"):
            with self.subTest(word=word):
                self.assertIn(word, description)
        self.assertIn(
            "category",
            declarations["search_files"]["parameters"]["properties"]
            ["extension"]["description"].lower())

    def test_search_then_open_through_execute_tool(self):
        target = self.make_file("School/homework.docx")
        events = []
        found = tool_calling.execute_tool(
            "search_files", {"query": "homework", "location": str(self.root)},
            event_callback=events.append)
        self.assertEqual(found["count"], 1)
        self.assertEqual(events[0]["target"], "homework")

        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            opened = tool_calling.execute_tool(
                "open_file", {"path": found["results"][0]["path"]},
                event_callback=events.append)

        self.assertTrue(opened["success"])
        self.assertEqual(opened["opened"], str(target))
        startfile.assert_called_once_with(str(target))
        self.assertTrue(events[-1]["ok"])
        self.assertTrue(events[-1]["summary"].startswith("opened"))

    def test_confirmation_reaches_the_model_without_launching(self):
        target = self.make_file("setup.exe")
        events = []
        with mock.patch.object(open_file_tool, "_startfile") as startfile:
            result = tool_calling.execute_tool(
                "open_file", {"path": str(target)},
                event_callback=events.append)
        self.assertTrue(result["approval_required"])
        self.assertFalse(events[-1]["ok"])
        self.assertIn("must approve", events[-1]["summary"])
        startfile.assert_not_called()

    def test_reveal_reports_in_the_event_stream(self):
        target = self.make_file("notes.txt")
        events = []
        with mock.patch.object(open_file_tool.subprocess, "Popen"):
            result = tool_calling.execute_tool(
                "open_containing_folder", {"path": str(target)},
                event_callback=events.append)
        self.assertTrue(result["success"])
        self.assertEqual(events[-1]["summary"], "revealed notes.txt")

    def test_bad_arguments_are_reported(self):
        result = tool_calling.execute_tool("open_file", {"file": "notes.txt"})
        self.assertIn("error", result)
        self.assertIn("Bad arguments", result["error"])


class PromptPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def test_prompt_teaches_the_file_workflow(self):
        self.assertIn("# OPENING FILES AND FOLDERS", self.prompt)
        for tool in ("search_files", "open_file", "open_folder",
                     "open_containing_folder"):
            with self.subTest(tool=tool):
                self.assertIn(tool, self.prompt)

    def test_prompt_requires_approval_before_running_code(self):
        self.assertIn("approval_required", self.prompt)
        self.assertIn("Jarvis dashboard", self.prompt)
        self.assertIn("Never supply `confirm`", self.prompt)
        self.assertIn("Never launch an installer", self.prompt)

    def test_prompt_forbids_a_website_fallback_for_files(self):
        self.assertIn("never use `open_web` as a fallback when a file cannot be "
                      "found", self.prompt)

    def test_prompt_asks_before_guessing_between_candidates(self):
        self.assertIn("clarification_required", self.prompt)
        self.assertIn("ask one short question naming the real candidates",
                      self.prompt)
        self.assertIn("never invent a path", self.prompt)

    def test_prompt_acts_on_deliberately_unspecific_requests(self):
        # "Open one of my Word files" must not turn into "which one?" — the
        # user already said they do not care which.
        self.assertIn("deliberately unspecific", self.prompt)
        self.assertIn("Do not ask", self.prompt)
        self.assertIn("newest sensible match", self.prompt)

    def test_prompt_forbids_announcing_an_action_without_calling_it(self):
        self.assertIn("Never end a turn saying you are opening", self.prompt)
        self.assertIn("announces the work, it never announces the result",
                      self.prompt)


if __name__ == "__main__":
    unittest.main()

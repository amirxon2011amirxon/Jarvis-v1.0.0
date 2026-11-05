"""Document reader: format coverage, structure, limits and failure handling.

Fixtures are generated in temporary directories with the real writers
(python-docx, openpyxl, python-pptx, pypdf, odfpy, ebooklib) so the reader is
tested against genuine files.  No network access or user files are touched.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from actions import read_document_tool as rd


HAS_DOCX = True
try:
    import docx  # noqa: F401
except Exception:  # noqa: BLE001
    HAS_DOCX = False
try:
    import openpyxl  # noqa: F401
except Exception:  # noqa: BLE001
    HAS_OPENPYXL = False
else:
    HAS_OPENPYXL = True
try:
    import pptx  # noqa: F401
except Exception:  # noqa: BLE001
    HAS_PPTX = False
else:
    HAS_PPTX = True
try:
    from pypdf import PdfWriter  # noqa: F401
except Exception:  # noqa: BLE001
    HAS_PYPDF = False
else:
    HAS_PYPDF = True
try:
    import odf  # noqa: F401
except Exception:  # noqa: BLE001
    HAS_ODF = False
else:
    HAS_ODF = True
try:
    import ebooklib  # noqa: F401
except Exception:  # noqa: BLE001
    HAS_EBOOKLIB = False
else:
    HAS_EBOOKLIB = True


class TempDocs(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="jarvis-read-doc-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def write(self, name: str, data: bytes | str,
              encoding: str = "utf-8") -> str:
        path = self.path(name)
        mode = "wb" if isinstance(data, bytes) else "w"
        kwargs = {} if isinstance(data, bytes) else {"encoding": encoding}
        with open(path, mode, **kwargs) as handle:
            handle.write(data)
        return path

    def make_pdf(self, texts: list[str], password: str = "") -> str:
        from pypdf import PdfWriter
        from pypdf.generic import (DecodedStreamObject, DictionaryObject,
                                   NameObject)

        writer = PdfWriter()
        for text in texts:
            page = writer.add_blank_page(width=200, height=200)
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(
                ")", "\\)")
            stream = DecodedStreamObject()
            stream.set_data(
                f"BT /F1 12 Tf 20 100 Td ({escaped}) Tj ET".encode("latin-1"))
            page[NameObject("/Contents")] = writer._add_object(stream)
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({
                    NameObject("/F1"): writer._add_object(font)})})
        if password:
            writer.encrypt(password)
        path = self.path("document.pdf")
        with open(path, "wb") as handle:
            writer.write(handle)
        return path


class TextAndMarkupTests(TempDocs):
    def test_known_shell_folder_path_uses_the_shared_resolver(self):
        path = self.write("report.txt", "shell folder content")
        with mock.patch(
                "actions.open_file_tool.resolve_open_target",
                return_value={"ok": True, "path": path}) as resolver:
            result = rd.read_document(r"Downloads\report.txt")
        resolver.assert_called_once_with(r"Downloads\report.txt")
        self.assertEqual(result["format"], "text")
        self.assertIn("shell folder content", result["content"])

    def test_text_encoding_ladder_and_bom(self):
        utf8 = self.write("a.txt", " hello\nworld ")
        result = rd.read_document(utf8)
        self.assertTrue(result["success"])
        self.assertIn("hello", result["content"])
        self.assertEqual(result["format"], "text")

        utf16 = self.write("b.txt", "wide text", encoding="utf-16")
        result = rd.read_document(utf16)
        self.assertTrue(result["success"])
        self.assertEqual(result["metadata"]["encoding"], "utf-16")

        bom = self.write("c.log", b"\xef\xbb\xbflog line\n")
        result = rd.read_document(bom)
        self.assertTrue(result["success"])
        self.assertEqual(result["metadata"]["encoding"], "utf-8-sig")
        self.assertNotIn("\ufeff", result["content"])

    def test_markdown_and_log_are_read_as_text(self):
        path = self.write("notes.md", "# Title\n\n- one\n- two\n")
        result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertIn("# Title", result["content"])
        self.assertEqual(result["format"], "markdown")

    def test_rtf_reader(self):
        path = self.write("note.rtf", r"{\rtf1\ansi Hello {\b RTF} world}")
        result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertIn("Hello", result["content"])
        self.assertIn("RTF", result["content"])

    def test_rtf_missing_dependency_is_clear(self):
        path = self.write("note.rtf", r"{\rtf1 Hello}")
        with mock.patch.dict(sys.modules, {"striprtf": None,
                                           "striprtf.striprtf": None}):
            result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing_dependency")
        self.assertIn("striprtf", result["error"])

    def test_html_drops_scripts_navigation_and_styles(self):
        path = self.write("page.html", """<!doctype html><html><head>
            <title>Example</title><style>body{color:red}</style>
            <script>alert(1)</script></head><body>
            <nav>menu</nav><h1>Heading</h1><p>Readable text.</p>
            <ul><li>Item one</li></ul><footer>footer</footer></body></html>""")
        result = rd.read_document(path)
        self.assertTrue(result["success"])
        content = result["content"]
        self.assertIn("# Example", content)
        self.assertIn("# Heading", content)
        self.assertIn("Readable text.", content)
        self.assertIn("- Item one", content)
        self.assertNotIn("alert(1)", content)
        self.assertNotIn("color:red", content)
        self.assertNotIn("menu", content)
        self.assertNotIn("footer", content)

    def test_xml_structure_and_dtd_rejection(self):
        path = self.write("data.xml", "<root><item id='1'>Hi</item></root>")
        result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertIn("<root>", result["content"])
        self.assertIn("Hi", result["content"])
        self.assertEqual(result["metadata"]["root_tag"], "root")

        unsafe = self.write("unsafe.xml", """<?xml version="1.0"?>
            <!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
            <root>&xxe;</root>""")
        result = rd.read_document(unsafe)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "unsafe_xml")

    def test_json_and_yaml_round_trip(self):
        json_path = self.write("data.json", '{"a": 1, "b": [true, null]}')
        result = rd.read_document(json_path)
        self.assertTrue(result["success"])
        self.assertIn('"a": 1', result["content"])
        self.assertEqual(result["metadata"]["json_type"], "object")

        yaml_path = self.write("data.yaml", "a: 1\nb:\n  - x\n  - y\n")
        result = rd.read_document(yaml_path)
        self.assertTrue(result["success"])
        self.assertIn("a: 1", result["content"])
        self.assertEqual(result["metadata"]["document_count"], 1)

    def test_invalid_json_is_a_structured_error(self):
        path = self.write("bad.json", "{not json")
        result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "corrupted")
        self.assertIn("line", result["error"])

    def test_csv_and_tsv_preserve_headers_and_rows(self):
        csv_path = self.write("table.csv", "name,value\nA,1\nB,2\n")
        result = rd.read_document(csv_path)
        self.assertTrue(result["success"])
        self.assertIn("| name | value |", result["content"])
        self.assertIn("| A | 1 |", result["content"])
        self.assertEqual(result["metadata"]["headers"], ["name", "value"])
        self.assertEqual(result["metadata"]["row_count"], 2)

        tsv_path = self.write("table.tsv", "name\tvalue\nA\t1\n")
        result = rd.read_document(tsv_path)
        self.assertTrue(result["success"])
        self.assertIn("| A | 1 |", result["content"])


class OfficeReaderTests(TempDocs):
    @unittest.skipUnless(HAS_DOCX, "python-docx is not installed")
    def test_docx_paragraphs_headings_and_tables(self):
        from docx import Document

        path = self.path("a.docx")
        document = Document()
        document.add_heading("Main heading", level=1)
        document.add_paragraph("Body paragraph.")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "A"
        table.cell(0, 1).text = "B"
        table.cell(1, 0).text = "1"
        table.cell(1, 1).text = "2"
        document.save(path)

        result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertIn("# Main heading", result["content"])
        self.assertIn("Body paragraph.", result["content"])
        self.assertIn("| A | B |", result["content"])
        self.assertIn("| 1 | 2 |", result["content"])
        self.assertEqual(result["metadata"]["table_count"], 1)
        self.assertGreaterEqual(result["metadata"]["paragraph_count"], 2)

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl is not installed")
    def test_xlsx_sheets_and_selected_sheet(self):
        import openpyxl

        path = self.path("a.xlsx")
        workbook = openpyxl.Workbook()
        data = workbook.active
        data.title = "Data"
        data.append(["Name", "Value"])
        data.append(["A", 1])
        other = workbook.create_sheet("Other")
        other["A1"] = "hidden value"
        workbook.save(path)

        result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertEqual(result["metadata"]["sheet_names"], ["Data", "Other"])
        self.assertIn("[Sheet: Data]", result["content"])
        self.assertIn("hidden value", result["content"])

        selected = rd.read_document(path, sheet="Data")
        self.assertTrue(selected["success"])
        self.assertIn("[Sheet: Data]", selected["content"])
        self.assertNotIn("hidden value", selected["content"])

        missing = rd.read_document(path, sheet="Nope")
        self.assertFalse(missing["success"])
        self.assertEqual(missing["reason"], "invalid_argument")

    def test_xls_reader_through_xlrd_api(self):
        class Cell:
            def __init__(self, ctype, value):
                self.ctype = ctype
                self.value = value

        class Sheet:
            name = "Legacy"
            nrows = 2
            ncols = 2

            def cell(self, row, column):
                values = [[Cell(1, "Name"), Cell(1, "Value")],
                          [Cell(1, "A"), Cell(2, 7)]]
                return values[row][column]

        class Workbook:
            datemode = 0

            def sheet_names(self):
                return ["Legacy"]

            def sheet_by_name(self, name):
                return Sheet()

            def release_resources(self):
                pass

        path = self.write("a.xls", b"not a real xls")
        import xlrd

        with mock.patch.object(xlrd, "open_workbook", return_value=Workbook()):
            result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertIn("[Sheet: Legacy]", result["content"])
        self.assertIn("| A | 7 |", result["content"])

    @unittest.skipUnless(HAS_PPTX, "python-pptx is not installed")
    def test_pptx_slides_tables_and_selected_slide(self):
        from pptx import Presentation

        path = self.path("a.pptx")
        presentation = Presentation()
        first = presentation.slides.add_slide(presentation.slide_layouts[1])
        first.shapes.title.text = "First title"
        first.placeholders[1].text = "First body"
        second = presentation.slides.add_slide(presentation.slide_layouts[5])
        second.shapes.title.text = "Second title"
        presentation.save(path)

        result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertIn("[Slide 1]", result["content"])
        self.assertIn("[Slide 2]", result["content"])
        self.assertEqual(result["metadata"]["slide_count"], 2)

        selected = rd.read_document(path, slide=2)
        self.assertTrue(selected["success"])
        self.assertIn("Second title", selected["content"])
        self.assertNotIn("First title", selected["content"])
        self.assertFalse(selected["truncated"])

    @unittest.skipUnless(HAS_ODF, "odfpy is not installed")
    def test_odt_ods_and_odp_readers(self):
        from odf import draw, table, text
        from odf.opendocument import (OpenDocumentPresentation,
                                      OpenDocumentSpreadsheet,
                                      OpenDocumentText)

        odt_path = self.path("a.odt")
        document = OpenDocumentText()
        document.text.addElement(text.H(outlinelevel=1, text="Heading"))
        document.text.addElement(text.P(text="Paragraph text"))
        document.save(odt_path)
        result = rd.read_document(odt_path)
        self.assertTrue(result["success"])
        self.assertIn("# Heading", result["content"])

        ods_path = self.path("a.ods")
        workbook = OpenDocumentSpreadsheet()
        sheet = table.Table(name="Sheet1")
        row = table.TableRow()
        for value in ("Name", "Value"):
            cell = table.TableCell(valuetype="string")
            cell.addElement(text.P(text=value))
            row.addElement(cell)
        sheet.addElement(row)
        workbook.spreadsheet.addElement(sheet)
        workbook.save(ods_path)
        result = rd.read_document(ods_path)
        self.assertTrue(result["success"])
        self.assertIn("[Sheet: Sheet1]", result["content"])
        self.assertIn("| Name | Value |", result["content"])

        odp_path = self.path("a.odp")
        presentation = OpenDocumentPresentation()
        page = draw.Page(name="Slide 1", masterpagename="Default")
        frame = draw.Frame(width="10cm", height="5cm", x="1cm", y="1cm")
        box = draw.TextBox()
        box.addElement(text.P(text="Slide text"))
        frame.addElement(box)
        page.addElement(frame)
        presentation.presentation.addElement(page)
        presentation.save(odp_path)
        result = rd.read_document(odp_path)
        self.assertTrue(result["success"])
        self.assertIn("[Slide 1]", result["content"])
        self.assertIn("Slide text", result["content"])


class PdfAndEpubTests(TempDocs):
    @unittest.skipUnless(HAS_PYPDF, "pypdf is not installed")
    def test_pdf_page_boundaries_and_selected_page(self):
        path = self.make_pdf(["First page", "Second page"])
        result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertIn("[Page 1]", result["content"])
        self.assertIn("[Page 2]", result["content"])
        self.assertEqual(result["metadata"]["page_count"], 2)

        selected = rd.read_document(path, page=2)
        self.assertTrue(selected["success"])
        self.assertIn("Second page", selected["content"])
        self.assertNotIn("First page", selected["content"])
        self.assertFalse(selected["truncated"])

    @unittest.skipUnless(HAS_PYPDF, "pypdf is not installed")
    def test_pdf_empty_pages_are_reported_not_faked(self):
        from pypdf import PdfWriter

        path = self.path("blank.pdf")
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with open(path, "wb") as handle:
            writer.write(handle)
        result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_text")
        self.assertIn("OCR", result["error"])

    @unittest.skipUnless(HAS_PYPDF, "pypdf is not installed")
    def test_encrypted_pdf_is_never_guessed(self):
        path = self.make_pdf(["secret text"], password="letmein")
        result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "encrypted")

    @unittest.skipUnless(HAS_EBOOKLIB, "ebooklib is not installed")
    def test_epub_chapters_keep_order_and_headings(self):
        from ebooklib import epub

        path = self.path("book.epub")
        book = epub.EpubBook()
        book.set_identifier("id")
        book.set_title("Book")
        book.set_language("en")
        chapters = []
        for index, title in ((1, "Chapter One"), (2, "Chapter Two")):
            chapter = epub.EpubHtml(
                title=title, file_name=f"ch{index}.xhtml", lang="en")
            chapter.content = f"<h1>{title}</h1><p>Body {index}</p>"
            book.add_item(chapter)
            chapters.append(chapter)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav"] + chapters
        epub.write_epub(path, book)

        result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertIn("[Chapter 1: Chapter One]", result["content"])
        self.assertIn("[Chapter 2: Chapter Two]", result["content"])
        self.assertLess(result["content"].index("Chapter One"),
                        result["content"].index("Chapter Two"))

        selected = rd.read_document(path, section=2)
        self.assertTrue(selected["success"])
        self.assertIn("Chapter Two", selected["content"])
        self.assertNotIn("Chapter One", selected["content"])


class SafetyAndFailureTests(TempDocs):
    def test_unsupported_extension_and_ole(self):
        path = self.write("thing.xyz", "plain text")
        result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "unsupported")

    def test_corrupted_zip_document(self):
        path = self.write("broken.docx", b"PK\x03\x04this is not a zip")
        result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertIn(result["reason"], ("corrupted", "unsupported"))

    def test_encrypted_modern_office_is_reported_as_encrypted(self):
        path = self.write("locked.docx",
                          b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")
        result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "encrypted")

    def test_archive_bomb_and_entry_limits(self):
        from docx import Document

        path = self.path("a.docx")
        Document().save(path)
        with mock.patch.object(rd, "MAX_ARCHIVE_ENTRY", 10):
            result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "unsafe_archive")

    def test_file_size_limit(self):
        path = self.write("big.txt", "x" * 500)
        with mock.patch.object(rd, "MAX_FILE_BYTES", 100):
            result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "file_too_large")

    def test_missing_dependency_is_explicit(self):
        path = self.write("a.pdf", b"%PDF-1.4\n")
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing_dependency")
        self.assertIn("pypdf", result["error"])

    def test_legacy_doc_without_converter_is_explicit(self):
        path = self.write("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        with mock.patch.object(rd, "_soffice_path", return_value=""):
            result = rd.read_document(path)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "missing_dependency")
        self.assertIn("LibreOffice", result["error"])

    @unittest.skipUnless(HAS_DOCX, "python-docx is not installed")
    def test_legacy_doc_uses_the_converter_when_available(self):
        from docx import Document

        path = self.write("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

        def convert(command, **_kwargs):
            outdir = command[command.index("--outdir") + 1]
            document = Document()
            document.add_paragraph("Converted legacy text")
            document.save(os.path.join(outdir, "old.docx"))
            return SimpleNamespace(returncode=0)

        with mock.patch.object(rd, "_soffice_path", return_value="soffice"), \
             mock.patch.object(rd.subprocess, "run", side_effect=convert):
            result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertEqual(result["format"], "doc")
        self.assertIn("Converted legacy text", result["content"])
        self.assertEqual(result["metadata"].get("converted_via"), "LibreOffice")

    def test_truncation_metadata(self):
        path = self.write("long.txt", "line\n" * 500)
        result = rd.read_document(path, max_chars=200)
        self.assertTrue(result["success"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"]), 200)
        self.assertTrue(any("truncated" in item.casefold()
                            for item in result["warnings"]))

    def test_cancellation_and_timeout(self):
        path = self.write("cancel.txt", "text")
        event = threading.Event()
        event.set()
        result = rd.read_document(path, cancel_event=event)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "cancelled")

        ctx = rd._Context(100, 1, None)
        with mock.patch.object(rd.time, "monotonic",
                               return_value=ctx.deadline + 1):
            with self.assertRaises(rd.DocumentError) as raised:
                ctx.check()
        self.assertEqual(raised.exception.code, "timeout")

    def test_format_signature_wins_over_wrong_extension(self):
        from pypdf import PdfWriter

        path = self.path("actually-a-pdf.bin")
        writer = PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        from pypdf.generic import (DecodedStreamObject, DictionaryObject,
                                   NameObject)
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 50 Td (Signature text) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): writer._add_object(font)})})
        with open(path, "wb") as handle:
            writer.write(handle)
        result = rd.read_document(path)
        self.assertTrue(result["success"])
        self.assertEqual(result["format"], "pdf")
        self.assertEqual(result["metadata"]["format_source"], "signature")

    def test_selection_warning_for_wrong_format(self):
        path = self.write("a.txt", "text")
        result = rd.read_document(path, page=2)
        self.assertTrue(result["success"])
        self.assertTrue(any("Page selection" in item
                            for item in result["warnings"]))


class RegistrationTests(unittest.TestCase):
    def test_tool_is_registered_and_declared(self):
        from actions import tool_calling

        self.assertIn("read_document", tool_calling.get_tool_names())
        info = tool_calling.TOOL_REGISTRY["read_document"]
        self.assertEqual(info["parameters"]["required"], ["path"])
        self.assertIn("page", info["parameters"]["properties"])
        self.assertIn("sheet", info["parameters"]["properties"])
        self.assertIn("slide", info["parameters"]["properties"])

    def test_execute_tool_keeps_document_content_above_old_string_cap(self):
        from actions import tool_calling

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "long.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("A" * 5000)
            result = tool_calling.execute_tool("read_document", {"path": path})
        self.assertTrue(result["success"])
        self.assertGreater(len(result["content"]), 3000)


if __name__ == "__main__":
    unittest.main()

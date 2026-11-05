"""Local, bounded document reading for Jarvis.

One registered tool reads text, markup, Office, OpenDocument, PDF and ebook
files without uploading anything.  Every parser runs locally; optional
libraries are imported only when their format is requested so a missing
package produces a clear ``missing_dependency`` error instead of breaking the
tool registry.

Safety rules enforced here:

* a hard file-size limit and archive decompression preflight;
* bounded output (characters, pages, sheets, slides, rows);
* deadline checks between pages/sheets/slides/rows, plus an optional
  cooperative ``cancel_event`` for tests and session teardown;
* encrypted PDFs are reported, never guessed at;
* malformed files return a structured error, never empty content;
* no macros, scripts, embedded code or external services are executed.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── limits ───────────────────────────────────────────────────────────────────
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TEXT_READ_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRY = 128 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
MAX_COMPRESSION_RATIO = 500
MAX_CHARS_DEFAULT = 8_000
MAX_CHARS_LIMIT = 20_000
MAX_PAGES_DEFAULT = 50
MAX_PAGES_LIMIT = 1_000
MAX_SLIDES_DEFAULT = 50
MAX_SLIDES_LIMIT = 500
MAX_ROWS_DEFAULT = 200
MAX_ROWS_LIMIT = 10_000
MAX_CHAPTERS_DEFAULT = 50
MAX_CHAPTERS_LIMIT = 500
MAX_COLUMNS = 60
DEFAULT_TIMEOUT_S = 25.0
MAX_TIMEOUT_S = 120.0

# Canonical formats.  ``supported`` is what the reader can actually parse in
# this build; legacy DOC/PPT depend on an optional LibreOffice conversion.
TEXT_FORMATS = frozenset({
    "text", "markdown", "log", "rtf", "html", "xml", "json", "yaml",
    "csv", "tsv",
})
OFFICE_FORMATS = frozenset({
    "docx", "doc", "xlsx", "xls", "pptx", "ppt", "odt", "ods", "odp",
})
PDF_FORMATS = frozenset({"pdf"})
EBOOK_FORMATS = frozenset({"epub"})
SUPPORTED_FORMATS = TEXT_FORMATS | OFFICE_FORMATS | PDF_FORMATS | EBOOK_FORMATS

_FORMAT_LABELS = {
    "text": "plain text",
    "markdown": "Markdown",
    "log": "log text",
    "rtf": "RTF",
    "html": "HTML",
    "xml": "XML",
    "json": "JSON",
    "yaml": "YAML",
    "csv": "CSV",
    "tsv": "TSV",
    "docx": "Word document",
    "doc": "legacy Word document",
    "xlsx": "Excel workbook",
    "xls": "legacy Excel workbook",
    "pptx": "PowerPoint presentation",
    "ppt": "legacy PowerPoint presentation",
    "odt": "OpenDocument text",
    "ods": "OpenDocument spreadsheet",
    "odp": "OpenDocument presentation",
    "pdf": "PDF",
    "epub": "EPUB ebook",
}

# Extension -> canonical format.  Values that are not in SUPPORTED_FORMATS are
# deliberately absent; they get a clear unsupported error.
_EXTENSIONS = {
    ".txt": "text", ".text": "text",
    ".md": "markdown", ".markdown": "markdown", ".mdown": "markdown",
    ".log": "log",
    ".rtf": "rtf",
    ".html": "html", ".htm": "html",
    ".xml": "xml", ".xsd": "xml", ".xsl": "xml", ".svg": "xml",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".csv": "csv", ".tsv": "tsv", ".tab": "tsv",
    ".docx": "docx", ".doc": "doc",
    ".xlsx": "xlsx", ".xls": "xls",
    ".pptx": "pptx", ".ppt": "ppt",
    ".odt": "odt", ".ods": "ods", ".odp": "odp",
    ".pdf": "pdf",
    ".epub": "epub",
    # Practical plain-text documents.  These are read as text, not claimed as
    # rich formats.
    ".rst": "text", ".tex": "text", ".ini": "text", ".cfg": "text",
    ".conf": "text", ".properties": "text", ".sql": "text",
}

_TEXTUAL_FORMATS = frozenset({
    "text", "markdown", "log", "rtf", "html", "xml", "json", "yaml",
    "csv", "tsv",
})
_ZIP_FORMATS = frozenset({
    "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub",
})


class DocumentError(Exception):
    """A structured reader failure carrying a stable code."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = str(code or "read_failed")
        self.message = str(message or "The document could not be read.")
        self.details = dict(details)


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any, default: int, low: int, high: int) -> int:
    text = _clean(value)
    if text in ("", "0", "none", "null"):
        return default
    try:
        number = int(float(text))
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return max(low, min(high, number))


def _positive(value: Any) -> int:
    try:
        number = int(float(_clean(value)))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def extension_of(path: str) -> str:
    return os.path.splitext(path)[1].casefold()


def format_from_extension(path: str) -> str:
    return _EXTENSIONS.get(extension_of(path), "")


def display_format(name: str) -> str:
    return _FORMAT_LABELS.get(name, name.upper() or "unknown")


def _head(path: str, count: int = 32) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(count)
    except OSError:
        return b""


def _zip_kind(path: str) -> str:
    """Identify an OOXML/ODF/EPUB container from its entry names."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "word/document.xml" in names:
                return "docx"
            if "xl/workbook.xml" in names:
                return "xlsx"
            if "ppt/presentation.xml" in names:
                return "pptx"
            if "META-INF/container.xml" in names:
                try:
                    with archive.open("mimetype") as handle:
                        mimetype = handle.read(512).decode(
                            "ascii", "replace").strip().casefold()
                except KeyError:
                    mimetype = ""
                if "epub" in mimetype:
                    return "epub"
                if "opendocument.text" in mimetype:
                    return "odt"
                if "opendocument.spreadsheet" in mimetype:
                    return "ods"
                if "opendocument.presentation" in mimetype:
                    return "odp"
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return ""
    return ""


def detect_format(path: str) -> Tuple[str, str]:
    """Return ``(canonical_format, source)`` using signature then extension."""
    head = _head(path)
    if head.startswith(b"%PDF-"):
        return "pdf", "signature"
    if head.startswith(b"{\\rtf"):
        return "rtf", "signature"
    if head.startswith(b"PK\x03\x04"):
        kind = _zip_kind(path)
        if kind:
            return kind, "signature"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        ext = _EXTENSIONS.get(extension_of(path), "")
        if ext in ("doc", "xls", "ppt"):
            return ext, "signature"
        return "ole", "signature"
    ext = format_from_extension(path)
    if ext:
        return ext, "extension"
    text = head.lstrip().lower()
    if text.startswith(b"<?xml"):
        return "xml", "signature"
    if text.startswith((b"<!doctype html", b"<html")):
        return "html", "signature"
    if text.startswith((b"{", b"[")):
        return "json", "signature"
    return "", ""


def _preflight(path: str) -> Tuple[int, str]:
    """Validate the file and archive safety; return ``(size, format)``."""
    if not os.path.exists(path):
        raise DocumentError("not_found", f"'{path}' does not exist.")
    if not os.path.isfile(path):
        raise DocumentError("not_a_file", f"'{path}' is not a regular file.")
    try:
        size = os.stat(path).st_size
    except OSError as exc:
        raise DocumentError(
            "unreadable", f"Could not inspect '{path}': {exc}") from None
    if size > MAX_FILE_BYTES:
        raise DocumentError(
            "file_too_large",
            f"'{os.path.basename(path)}' is {size / (1024 * 1024):.1f} MB; "
            f"the reader limit is {MAX_FILE_BYTES / (1024 * 1024):.0f} MB.")
    fmt, _source = detect_format(path)
    if fmt in _ZIP_FORMATS:
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ARCHIVE_ENTRIES:
                    raise DocumentError(
                        "unsafe_archive",
                        "The document contains too many archive entries.")
                total = 0
                for info in infos:
                    if info.file_size > MAX_ARCHIVE_ENTRY:
                        raise DocumentError(
                            "unsafe_archive",
                            "The document contains an entry larger than the "
                            "reader allows.")
                    total += info.file_size
                    if total > MAX_ARCHIVE_UNCOMPRESSED:
                        raise DocumentError(
                            "unsafe_archive",
                            "The document expands beyond the reader's safe "
                            "archive limit.")
                    if (info.compress_size > 0 and info.file_size > 8 * 1024 * 1024
                            and info.file_size / info.compress_size
                            > MAX_COMPRESSION_RATIO):
                        raise DocumentError(
                            "unsafe_archive",
                            "The document has an unsafe compression ratio.")
        except zipfile.BadZipFile:
            raise DocumentError(
                "corrupted",
                "The document is not a valid archive; it may be corrupted.") \
                from None
    return size, fmt


def _decode_text(data: bytes, encoding: str = "") -> Tuple[str, str, List[str]]:
    """Decode bytes with an explicit encoding or a conservative ladder."""
    warnings: List[str] = []
    if encoding:
        try:
            return data.decode(encoding), encoding, warnings
        except LookupError:
            raise DocumentError(
                "invalid_encoding",
                f"Unknown text encoding '{encoding}'.") from None
        except UnicodeDecodeError:
            raise DocumentError(
                "encoding_error",
                f"The file is not valid {encoding} text.") from None
    # Respect an explicit byte-order mark instead of always preferring
    # utf-8-sig (which would add a BOM on the next write).
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig", warnings
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16", warnings
        except UnicodeDecodeError:
            pass
    for candidate in ("utf-8", "cp1252"):
        try:
            return data.decode(candidate), candidate, warnings
        except (UnicodeDecodeError, LookupError):
            continue
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None:
            return str(best), best.encoding or "utf-8", warnings
    except Exception:  # noqa: BLE001 - optional detector
        pass
    warnings.append(
        "Some bytes could not be decoded; undecodable characters were "
        "replaced.")
    return data.decode("utf-8", errors="replace"), "utf-8", warnings


def _read_text_file(path: str, encoding: str = ""
                    ) -> Tuple[str, str, bool, List[str]]:
    """Read a bounded text file. Returns ``(text, encoding, file_truncated,
    warnings)``."""
    warnings: List[str] = []
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        data = handle.read(MAX_TEXT_READ_BYTES)
    truncated = size > len(data)
    text, used, decode_warnings = _decode_text(data, encoding)
    warnings.extend(decode_warnings)
    if truncated:
        warnings.append(
            f"Only the first {len(data) // (1024 * 1024)} MB of a larger "
            "file was read.")
    return text, used, truncated, warnings


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:g}"
    if isinstance(value, (int, bool)):
        return str(value)
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _table_lines(rows: List[List[str]], *, max_columns: int = MAX_COLUMNS) -> str:
    rows = [[_cell_text(cell) for cell in row[:max_columns]] for row in rows]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


class _Context:
    """Bounded output collector with deadline and cancellation checks."""

    def __init__(self, max_chars: int, timeout_s: float,
                 cancel_event: Optional[threading.Event] = None) -> None:
        self.max_chars = max(200, int(max_chars))
        self.deadline = time.monotonic() + max(0.5, float(timeout_s))
        self.cancel_event = cancel_event
        self.parts: List[str] = []
        self.chars = 0
        self.truncated = False

    def check(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise DocumentError("cancelled", "The document read was cancelled.")
        if time.monotonic() > self.deadline:
            raise DocumentError(
                "timeout",
                "The document took longer than the configured read timeout.")

    def add(self, text: str) -> None:
        if not text:
            return
        self.check()
        remaining = self.max_chars - self.chars
        if remaining <= 0:
            self.truncated = True
            return
        if len(text) > remaining:
            text = text[:remaining]
            self.truncated = True
        self.parts.append(text)
        self.chars += len(text)

    def add_line(self, text: str = "") -> None:
        self.add(text + "\n")

    def text(self) -> str:
        return "".join(self.parts).strip()


def _option_int(value: Any, default: int, high: int) -> int:
    return _number(value, default, 1, high)


# ── text and markup ──────────────────────────────────────────────────────────
def _read_plain(ctx: _Context, path: str, fmt: str,
                encoding: str = "") -> Tuple[Dict[str, Any], List[str]]:
    text, used, file_truncated, warnings = _read_text_file(path, encoding)
    ctx.add(text)
    if file_truncated:
        ctx.truncated = True
    return {
        "encoding": used,
        "line_count": text.count("\n") + (1 if text else 0),
        "character_count": len(text),
    }, warnings


def _read_rtf(ctx: _Context, path: str, encoding: str = ""
              ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        from striprtf.striprtf import rtf_to_text
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading RTF needs the optional 'striprtf' package. Install it "
            "with: pip install striprtf") from None
    text, used, file_truncated, warnings = _read_text_file(path, encoding)
    try:
        plain = rtf_to_text(text, errors="replace")
    except Exception as exc:  # noqa: BLE001 - malformed RTF
        raise DocumentError(
            "corrupted",
            f"The RTF document could not be parsed: {type(exc).__name__}.") \
            from None
    ctx.add(plain)
    if file_truncated:
        ctx.truncated = True
    return {
        "encoding": used,
        "character_count": len(plain),
    }, warnings


def _read_html(ctx: _Context, path: str, encoding: str = ""
               ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        from bs4 import BeautifulSoup
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading HTML needs the optional 'beautifulsoup4' package. "
            "Install it with: pip install beautifulsoup4") from None
    text, used, file_truncated, warnings = _read_text_file(path, encoding)
    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception as exc:  # noqa: BLE001 - malformed markup
        raise DocumentError(
            "corrupted",
            f"The HTML could not be parsed: {type(exc).__name__}.") from None
    for tag in soup(["script", "style", "noscript", "template", "svg",
                     "canvas", "nav", "header", "footer", "aside", "form",
                     "button", "iframe"]):
        tag.decompose()
    title = _clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title:
        ctx.add_line(f"# {title}")

    block_tags = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre",
                  "blockquote", "tr", "dt", "dd"}
    emitted = 0

    def walk(node: Any) -> None:
        nonlocal emitted
        for child in getattr(node, "children", []):
            name = getattr(child, "name", None)
            if name in block_tags:
                ctx.check()
                if name == "tr":
                    cells = [
                        _clean(cell.get_text(" ", strip=True))
                        for cell in child.find_all(["td", "th"])
                    ]
                    if any(cells):
                        ctx.add_line("| " + " | ".join(cells) + " |")
                        emitted += 1
                elif name in ("pre", "blockquote"):
                    value = child.get_text("\n", strip=True)
                    if value:
                        ctx.add_line(value)
                        emitted += 1
                else:
                    value = _clean(child.get_text(" ", strip=True))
                    if value:
                        if name.startswith("h") and name[1:].isdigit():
                            ctx.add_line("#" * int(name[1:]) + " " + value)
                        elif name == "li":
                            ctx.add_line("- " + value)
                        else:
                            ctx.add_line(value)
                        emitted += 1
            elif getattr(child, "children", None) is not None:
                walk(child)

    body = soup.body or soup
    walk(body)
    if not emitted:
        fallback = _clean(soup.get_text("\n", strip=True))
        if fallback:
            ctx.add(fallback)
    if file_truncated:
        ctx.truncated = True
    return {
        "encoding": used,
        "title": title,
        "block_count": emitted,
    }, warnings


def _read_xml(ctx: _Context, path: str, encoding: str = ""
              ) -> Tuple[Dict[str, Any], List[str]]:
    import xml.etree.ElementTree as ET

    text, used, file_truncated, warnings = _read_text_file(path, encoding)
    upper = text[:65536].upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise DocumentError(
            "unsafe_xml",
            "XML documents with DTD or entity declarations are not read, to "
            "avoid external-entity and entity-expansion attacks.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DocumentError(
            "corrupted", f"The XML document is not well formed: {exc}.") \
            from None
    ET.indent(root, space="  ")
    rendered = ET.tostring(root, encoding="unicode")
    ctx.add(rendered)
    if file_truncated:
        ctx.truncated = True
    return {
        "encoding": used,
        "root_tag": str(root.tag),
        "element_count": sum(1 for _ in root.iter()),
    }, warnings


def _read_json(ctx: _Context, path: str, encoding: str = ""
               ) -> Tuple[Dict[str, Any], List[str]]:
    text, used, file_truncated, warnings = _read_text_file(path, encoding)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DocumentError(
            "corrupted",
            f"The JSON is not valid at line {exc.lineno}, column "
            f"{exc.colno}: {exc.msg}.") from None
    rendered = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    ctx.add(rendered)
    if file_truncated:
        ctx.truncated = True
    kind = ("array" if isinstance(value, list)
            else "object" if isinstance(value, dict) else type(value).__name__)
    return {
        "encoding": used,
        "json_type": kind,
        "item_count": len(value) if isinstance(value, (list, dict)) else 1,
    }, warnings


def _read_yaml(ctx: _Context, path: str, encoding: str = ""
               ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        import yaml
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading YAML needs the optional 'PyYAML' package. Install it "
            "with: pip install pyyaml") from None
    text, used, file_truncated, warnings = _read_text_file(path, encoding)
    try:
        documents = list(yaml.safe_load_all(text))
        rendered = yaml.safe_dump_all(documents, sort_keys=False,
                                      allow_unicode=True)
    except yaml.YAMLError as exc:
        raise DocumentError(
            "corrupted", f"The YAML document is not valid: {exc}.") from None
    ctx.add(rendered)
    if file_truncated:
        ctx.truncated = True
    return {
        "encoding": used,
        "document_count": len(documents),
    }, warnings


def _delimiter(fmt: str) -> str:
    return "\t" if fmt == "tsv" else ","


def _read_delimited(ctx: _Context, path: str, fmt: str, *,
                    encoding: str = "", max_rows: int = MAX_ROWS_DEFAULT
                    ) -> Tuple[Dict[str, Any], List[str]]:
    text, used, file_truncated, warnings = _read_text_file(path, encoding)
    delimiter = _delimiter(fmt)
    rows: List[List[str]] = []
    truncated_rows = False
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        for index, row in enumerate(reader):
            ctx.check()
            if index >= max_rows:
                truncated_rows = True
                break
            rows.append(row[:MAX_COLUMNS])
    except csv.Error as exc:
        raise DocumentError(
            "corrupted", f"The {fmt.upper()} file is not valid: {exc}.") \
            from None
    if file_truncated:
        truncated_rows = True
    headers = rows[0] if rows else []
    body = rows[1:] if rows else []
    if headers:
        ctx.add_line("| " + " | ".join(headers) + " |")
        ctx.add_line("| " + " | ".join("---" for _ in headers) + " |")
        for row in body:
            ctx.add_line("| " + " | ".join(row) + " |")
    if truncated_rows:
        ctx.truncated = True
        warnings.append(
            f"Only the first {len(rows)} rows were read (limit {max_rows}).")
    return {
        "encoding": used,
        "delimiter": "tab" if delimiter == "\t" else "comma",
        "headers": headers,
        "header_count": len(headers),
        "row_count": len(body),
        "truncated_rows": truncated_rows,
    }, warnings


# ── PDF / EPUB ───────────────────────────────────────────────────────────────
def _read_pdf(ctx: _Context, path: str, *, page: int = 0,
              max_pages: int = MAX_PAGES_DEFAULT
              ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading PDF needs the optional 'pypdf' package. Install it with: "
            "pip install pypdf") from None
    warnings: List[str] = []
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 - malformed PDF
        raise DocumentError(
            "corrupted",
            f"The PDF could not be opened: {type(exc).__name__}.") from None
    encrypted = bool(getattr(reader, "is_encrypted", False))
    if encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception:  # noqa: BLE001 - encrypted with a real password
            unlocked = 0
        if getattr(unlocked, "value", unlocked) == 0:
            raise DocumentError(
                "encrypted",
                "The PDF is password-protected. Jarvis does not guess "
                "passwords; remove the password or provide an unlocked copy.")
        warnings.append("The PDF was encrypted with an empty password.")
    try:
        total = len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(
            "corrupted", f"The PDF page tree is invalid: {exc}.") from None
    if total <= 0:
        raise DocumentError("no_text", "The PDF contains no pages.")
    if page:
        if page > total:
            raise DocumentError(
                "invalid_argument",
                f"Page {page} was requested, but the PDF has {total} pages.")
        indexes = [page - 1]
    else:
        indexes = list(range(min(total, max_pages)))
    empty_pages: List[int] = []
    pages_read = 0
    for index in indexes:
        ctx.check()
        try:
            value = reader.pages[index].extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - one bad page is not fatal
            warnings.append(
                f"Page {index + 1} could not be extracted "
                f"({type(exc).__name__}).")
            value = ""
        value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if value:
            ctx.add(f"[Page {index + 1}]\n{value}\n\n")
            pages_read += 1
        else:
            empty_pages.append(index + 1)
    truncated_pages = total > len(indexes) and not page
    if truncated_pages:
        ctx.truncated = True
    if empty_pages:
        warnings.append(
            f"{len(empty_pages)} page(s) contained no extractable text: "
            + ", ".join(str(number) for number in empty_pages[:20])
            + ("…" if len(empty_pages) > 20 else "")
            + ". Scanned pages need OCR, which is not installed.")
    if not ctx.text():
        raise DocumentError(
            "no_text",
            "No extractable text was found in this PDF. It may be a scanned "
            "document; OCR is not available in this installation.")
    return {
        "page_count": total,
        "pages_read": pages_read,
        "empty_pages": empty_pages,
        "encrypted": encrypted,
        "ocr_available": False,
        "truncated_pages": truncated_pages,
    }, warnings


def _read_epub(ctx: _Context, path: str, *, section: int = 0,
               max_chapters: int = MAX_CHAPTERS_DEFAULT
               ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        import ebooklib
        from ebooklib import epub
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading EPUB needs the optional 'ebooklib' package. Install it "
            "with: pip install ebooklib") from None
    try:
        from bs4 import BeautifulSoup
    except Exception:  # noqa: BLE001
        raise DocumentError(
            "missing_dependency",
            "Reading EPUB needs 'beautifulsoup4'. Install it with: "
            "pip install beautifulsoup4") from None
    warnings: List[str] = []
    try:
        book = epub.read_epub(str(path))
    except Exception as exc:  # noqa: BLE001 - malformed archive
        raise DocumentError(
            "corrupted",
            f"The EPUB could not be opened: {type(exc).__name__}.") from None
    items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    if not items:
        raise DocumentError("no_text", "The EPUB contains no readable chapters.")
    # Follow the spine order when available; that is the book's reading order.
    ordered = []
    try:
        for entry in book.spine or []:
            item_id = entry[0] if isinstance(entry, (tuple, list)) else entry
            item = book.get_item_with_id(str(item_id))
            if item is not None and item.get_type() == ebooklib.ITEM_DOCUMENT:
                try:
                    if hasattr(item, "is_chapter") and not item.is_chapter():
                        continue
                except Exception:  # noqa: BLE001
                    pass
                ordered.append(item)
    except Exception:  # noqa: BLE001 - fall back to manifest order
        ordered = []
    if ordered:
        items = ordered
    else:
        chapters = []
        for item in items:
            try:
                if hasattr(item, "is_chapter") and not item.is_chapter():
                    continue
            except Exception:  # noqa: BLE001
                pass
            chapters.append(item)
        if chapters:
            items = chapters
    if section:
        if section > len(items):
            raise DocumentError(
                "invalid_argument",
                f"Chapter {section} was requested, but the EPUB has "
                f"{len(items)} chapters.")
        indexes = [section - 1]
    else:
        indexes = list(range(min(len(items), max_chapters)))
    chapters_read = 0
    for index in indexes:
        ctx.check()
        item = items[index]
        try:
            raw_bytes = item.get_content()
            raw, _used, _warn = _decode_text(raw_bytes)
        except Exception:  # noqa: BLE001
            raw = ""
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        heading = ""
        for level in range(1, 7):
            node = soup.find(f"h{level}")
            if node is not None:
                heading = _clean(node.get_text(" ", strip=True))
                if heading:
                    break
        if not heading:
            heading = os.path.splitext(os.path.basename(item.get_name()))[0]
        body = _clean(soup.get_text("\n", strip=True))
        if not body:
            warnings.append(f"Chapter {index + 1} contained no text.")
            continue
        ctx.add_line(f"[Chapter {index + 1}: {heading}]")
        ctx.add_line(body)
        ctx.add_line()
        chapters_read += 1
    truncated = len(items) > len(indexes) and not section
    if truncated:
        ctx.truncated = True
    if not ctx.text():
        raise DocumentError("no_text", "The EPUB has no readable text chapters.")
    return {
        "chapter_count": len(items),
        "chapters_read": chapters_read,
        "truncated_chapters": truncated,
    }, warnings


# ── Office: DOCX / ODT ───────────────────────────────────────────────────────
def _heading_prefix(style_name: str) -> str:
    lowered = (style_name or "").casefold()
    if lowered == "title":
        return "# "
    match = lowered.startswith("heading") and "".join(
        char for char in lowered if char.isdigit())
    if match:
        try:
            level = max(1, min(6, int(match)))
        except ValueError:
            level = 1
        return "#" * level + " "
    if lowered.startswith("list bullet"):
        return "- "
    return ""


def _read_docx(ctx: _Context, path: str
               ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading DOCX needs the optional 'python-docx' package. Install "
            "it with: pip install python-docx") from None
    warnings: List[str] = []
    try:
        document = Document(str(path))
    except Exception as exc:  # noqa: BLE001 - corrupted zip/document
        raise DocumentError(
            "corrupted",
            f"The DOCX document could not be opened: {type(exc).__name__}.") \
            from None
    paragraphs = 0
    tables = 0
    try:
        blocks = document.iter_inner_content()
    except AttributeError:  # pragma: no cover - very old python-docx
        blocks = list(document.paragraphs) + list(document.tables)
    for block in blocks:
        ctx.check()
        if isinstance(block, Paragraph):
            value = _clean(block.text)
            if not value:
                continue
            prefix = _heading_prefix(
                getattr(getattr(block, "style", None), "name", ""))
            ctx.add_line(prefix + value)
            paragraphs += 1
        elif isinstance(block, Table):
            rows = [[_clean(cell.text) for cell in row.cells]
                    for row in block.rows]
            if any(any(cell for cell in row) for row in rows):
                ctx.add_line()
                ctx.add(_table_lines(rows) + "\n")
                ctx.add_line()
                tables += 1
    if not ctx.text():
        raise DocumentError(
            "no_text",
            "The DOCX document contains no readable paragraphs or tables.")
    return {
        "paragraph_count": paragraphs,
        "table_count": tables,
    }, warnings


def _odf_text(element: Any) -> str:
    try:
        from odf import teletype

        return _clean(teletype.extractText(element))
    except Exception:  # noqa: BLE001
        return ""


def _odf_table_rows(element: Any) -> List[List[str]]:
    try:
        from odf import table

        rows = []
        for row in element.getElementsByType(table.TableRow):
            cells = []
            for cell in row.getElementsByType(table.TableCell):
                value = _odf_text(cell)
                if not value:
                    try:
                        value = _clean(cell.getAttribute("value"))
                    except (ValueError, AttributeError):
                        value = ""
                cells.append(value)
            rows.append(cells)
        return rows
    except Exception:  # noqa: BLE001
        return []


def _read_odt(ctx: _Context, path: str
              ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        from odf.opendocument import load
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading ODT needs the optional 'odfpy' package. Install it with: "
            "pip install odfpy") from None
    warnings: List[str] = []
    try:
        document = load(str(path))
    except Exception as exc:  # noqa: BLE001 - malformed ODF
        raise DocumentError(
            "corrupted",
            f"The ODT document could not be opened: {type(exc).__name__}.") \
            from None
    paragraphs = 0
    tables = 0
    for child in getattr(document.text, "childNodes", []):
        ctx.check()
        tag = getattr(child, "tagName", "")
        if tag == "text:h":
            value = _odf_text(child)
            if value:
                try:
                    level = max(1, min(6, int(child.getAttribute(
                        "outlinelevel") or 1)))
                except (TypeError, ValueError, AttributeError):
                    level = 1
                ctx.add_line("#" * level + " " + value)
                paragraphs += 1
        elif tag == "text:p":
            value = _odf_text(child)
            if value:
                ctx.add_line(value)
                paragraphs += 1
        elif tag == "table:table":
            rows = _odf_table_rows(child)
            if any(any(cell for cell in row) for row in rows):
                ctx.add_line()
                ctx.add(_table_lines(rows) + "\n")
                ctx.add_line()
                tables += 1
        else:
            value = _odf_text(child)
            if value:
                ctx.add_line(value)
    if not ctx.text():
        raise DocumentError(
            "no_text", "The ODT document contains no readable text.")
    return {
        "paragraph_count": paragraphs,
        "table_count": tables,
    }, warnings


# ── Office: XLSX / XLS / ODS ─────────────────────────────────────────────────
def _read_xlsx(ctx: _Context, path: str, *, sheet: str = "",
               max_rows: int = MAX_ROWS_DEFAULT
               ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        import openpyxl
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading XLSX needs the optional 'openpyxl' package. Install it "
            "with: pip install openpyxl") from None
    warnings: List[str] = []
    try:
        workbook = openpyxl.load_workbook(
            str(path), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - malformed workbook
        raise DocumentError(
            "corrupted",
            f"The XLSX workbook could not be opened: {type(exc).__name__}.") \
            from None
    try:
        names = list(getattr(workbook, "sheetnames", []) or [])
        if not names:
            raise DocumentError("no_text", "The workbook has no worksheets.")
        selected = names
        if sheet:
            matches = [name for name in names
                       if str(name).casefold() == sheet.casefold()]
            if not matches:
                raise DocumentError(
                    "invalid_argument",
                    f"Worksheet '{sheet}' was not found. Available sheets: "
                    + ", ".join(names[:20]) + ".")
            selected = [matches[0]]
        sheets_read = 0
        total_rows = 0
        truncated_rows = False
        for name in selected:
            ctx.check()
            worksheet = workbook[name]
            ctx.add_line(f"[Sheet: {name}]")
            rows: List[List[str]] = []
            for index, row in enumerate(worksheet.iter_rows(values_only=True)):
                ctx.check()
                if index >= max_rows:
                    truncated_rows = True
                    break
                values = [_cell_text(value) for value in row]
                while values and values[-1] == "":
                    values.pop()
                if any(values):
                    rows.append(values)
            total_rows += len(rows)
            if rows:
                ctx.add(_table_lines(rows) + "\n")
            else:
                ctx.add_line("(empty sheet)")
            ctx.add_line()
            sheets_read += 1
        if not ctx.text():
            raise DocumentError("no_text", "The workbook contains no cell values.")
        if truncated_rows:
            ctx.truncated = True
            warnings.append(
                f"Only the first {max_rows} rows per sheet were read.")
        return {
            "sheet_names": names,
            "sheet_count": len(names),
            "sheets_read": sheets_read,
            "row_count": total_rows,
            "truncated_rows": truncated_rows,
        }, warnings
    finally:
        try:
            workbook.close()
        except Exception:  # noqa: BLE001
            pass


def _read_xls(ctx: _Context, path: str, *, sheet: str = "",
              max_rows: int = MAX_ROWS_DEFAULT
              ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        import xlrd
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading legacy XLS needs the optional 'xlrd' package. Install it "
            "with: pip install xlrd") from None
    warnings: List[str] = []
    try:
        workbook = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as exc:  # noqa: BLE001 - malformed workbook
        raise DocumentError(
            "corrupted",
            f"The XLS workbook could not be opened: {type(exc).__name__}.") \
            from None
    try:
        names = list(workbook.sheet_names())
        if not names:
            raise DocumentError("no_text", "The workbook has no worksheets.")
        selected = names
        if sheet:
            matches = [name for name in names
                       if str(name).casefold() == sheet.casefold()]
            if not matches:
                raise DocumentError(
                    "invalid_argument",
                    f"Worksheet '{sheet}' was not found. Available sheets: "
                    + ", ".join(names[:20]) + ".")
            selected = [matches[0]]
        sheets_read = 0
        total_rows = 0
        truncated_rows = False
        for name in selected:
            ctx.check()
            worksheet = workbook.sheet_by_name(name)
            ctx.add_line(f"[Sheet: {name}]")
            rows: List[List[str]] = []
            for index in range(min(worksheet.nrows, max_rows)):
                ctx.check()
                values = []
                for column in range(min(worksheet.ncols, MAX_COLUMNS)):
                    cell = worksheet.cell(index, column)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            value = xlrd.xldate_as_datetime(
                                cell.value, workbook.datemode)
                            values.append(value.strftime("%Y-%m-%d %H:%M"))
                        except Exception:  # noqa: BLE001
                            values.append(_cell_text(cell.value))
                    else:
                        values.append(_cell_text(cell.value))
                while values and values[-1] == "":
                    values.pop()
                if any(values):
                    rows.append(values)
            if worksheet.nrows > max_rows:
                truncated_rows = True
            total_rows += len(rows)
            if rows:
                ctx.add(_table_lines(rows) + "\n")
            else:
                ctx.add_line("(empty sheet)")
            ctx.add_line()
            sheets_read += 1
        if not ctx.text():
            raise DocumentError("no_text", "The workbook contains no cell values.")
        if truncated_rows:
            ctx.truncated = True
            warnings.append(
                f"Only the first {max_rows} rows per sheet were read.")
        return {
            "sheet_names": names,
            "sheet_count": len(names),
            "sheets_read": sheets_read,
            "row_count": total_rows,
            "truncated_rows": truncated_rows,
        }, warnings
    finally:
        try:
            workbook.release_resources()
        except Exception:  # noqa: BLE001
            pass


def _read_ods(ctx: _Context, path: str, *, sheet: str = "",
              max_rows: int = MAX_ROWS_DEFAULT
              ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        from odf import table
        from odf.opendocument import load
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading ODS needs the optional 'odfpy' package. Install it with: "
            "pip install odfpy") from None
    warnings: List[str] = []
    try:
        document = load(str(path))
    except Exception as exc:  # noqa: BLE001 - malformed ODF
        raise DocumentError(
            "corrupted",
            f"The ODS spreadsheet could not be opened: {type(exc).__name__}.") \
            from None
    tables = list(document.spreadsheet.getElementsByType(table.Table))
    if not tables:
        raise DocumentError("no_text", "The spreadsheet has no worksheets.")
    names = []
    for item in tables:
        try:
            names.append(_clean(item.getAttribute("name")))
        except (ValueError, AttributeError):
            names.append("")
    selected = list(zip(names, tables))
    if sheet:
        matches = [(name, item) for name, item in selected
                   if name.casefold() == sheet.casefold()]
        if not matches:
            raise DocumentError(
                "invalid_argument",
                f"Worksheet '{sheet}' was not found. Available sheets: "
                + ", ".join(name or "(unnamed)" for name in names[:20]) + ".")
        selected = matches
    sheets_read = 0
    total_rows = 0
    truncated_rows = False
    for name, element in selected:
        ctx.check()
        ctx.add_line(f"[Sheet: {name or '(unnamed)'}]")
        rows = _odf_table_rows(element)
        if len(rows) > max_rows:
            rows = rows[:max_rows]
            truncated_rows = True
        total_rows += len(rows)
        if rows:
            ctx.add(_table_lines(rows) + "\n")
        else:
            ctx.add_line("(empty sheet)")
        ctx.add_line()
        sheets_read += 1
    if not ctx.text():
        raise DocumentError("no_text", "The spreadsheet contains no cell values.")
    if truncated_rows:
        ctx.truncated = True
        warnings.append(f"Only the first {max_rows} rows per sheet were read.")
    return {
        "sheet_names": names,
        "sheet_count": len(names),
        "sheets_read": sheets_read,
        "row_count": total_rows,
        "truncated_rows": truncated_rows,
    }, warnings


# ── Office: PPTX / PPT / ODP ─────────────────────────────────────────────────
def _pptx_shape_text(shape: Any) -> str:
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        return _clean(shape.text_frame.text)
    return ""


def _read_pptx(ctx: _Context, path: str, *, slide: int = 0,
               max_slides: int = MAX_SLIDES_DEFAULT
               ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        from pptx import Presentation
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading PPTX needs the optional 'python-pptx' package. Install "
            "it with: pip install python-pptx") from None
    warnings: List[str] = []
    try:
        presentation = Presentation(str(path))
    except Exception as exc:  # noqa: BLE001 - malformed presentation
        raise DocumentError(
            "corrupted",
            f"The PPTX presentation could not be opened: "
            f"{type(exc).__name__}.") from None
    slides = list(presentation.slides)
    if not slides:
        raise DocumentError("no_text", "The presentation contains no slides.")
    if slide:
        if slide > len(slides):
            raise DocumentError(
                "invalid_argument",
                f"Slide {slide} was requested, but the presentation has "
                f"{len(slides)} slides.")
        indexes = [slide - 1]
    else:
        indexes = list(range(min(len(slides), max_slides)))
    slides_read = 0
    for index in indexes:
        ctx.check()
        current = slides[index]
        parts: List[str] = []
        for shape in current.shapes:
            value = _pptx_shape_text(shape)
            if value:
                parts.append(value)
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [[_clean(cell.text) for cell in row.cells]
                        for row in shape.table.rows]
                if any(any(cell for cell in row) for row in rows):
                    parts.append(_table_lines(rows))
        if parts:
            ctx.add_line(f"[Slide {index + 1}]")
            ctx.add_line("\n".join(parts))
            ctx.add_line()
            slides_read += 1
    truncated = len(slides) > len(indexes) and not slide
    if truncated:
        ctx.truncated = True
    if not ctx.text():
        raise DocumentError("no_text", "The presentation contains no slide text.")
    return {
        "slide_count": len(slides),
        "slides_read": slides_read,
        "truncated_slides": truncated,
    }, warnings


def _read_odp(ctx: _Context, path: str, *, slide: int = 0,
              max_slides: int = MAX_SLIDES_DEFAULT
              ) -> Tuple[Dict[str, Any], List[str]]:
    try:
        from odf.opendocument import load
    except Exception:  # noqa: BLE001 - optional dependency
        raise DocumentError(
            "missing_dependency",
            "Reading ODP needs the optional 'odfpy' package. Install it with: "
            "pip install odfpy") from None
    warnings: List[str] = []
    try:
        document = load(str(path))
    except Exception as exc:  # noqa: BLE001 - malformed ODF
        raise DocumentError(
            "corrupted",
            f"The ODP presentation could not be opened: "
            f"{type(exc).__name__}.") from None
    pages = [child for child in getattr(document.presentation, "childNodes", [])
             if getattr(child, "tagName", "") == "draw:page"]
    if not pages:
        raise DocumentError("no_text", "The presentation contains no slides.")
    if slide:
        if slide > len(pages):
            raise DocumentError(
                "invalid_argument",
                f"Slide {slide} was requested, but the presentation has "
                f"{len(pages)} slides.")
        indexes = [slide - 1]
    else:
        indexes = list(range(min(len(pages), max_slides)))
    slides_read = 0
    for index in indexes:
        ctx.check()
        value = _odf_text(pages[index])
        if value:
            ctx.add_line(f"[Slide {index + 1}]")
            ctx.add_line(value)
            ctx.add_line()
            slides_read += 1
    truncated = len(pages) > len(indexes) and not slide
    if truncated:
        ctx.truncated = True
    if not ctx.text():
        raise DocumentError("no_text", "The presentation contains no slide text.")
    return {
        "slide_count": len(pages),
        "slides_read": slides_read,
        "truncated_slides": truncated,
    }, warnings


# ── Legacy conversion backend (optional) ─────────────────────────────────────
def _soffice_path() -> str:
    candidates = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


def _convert_legacy(path: str, target_ext: str, timeout_s: float
                    ) -> Tuple[str, Callable[[], None]]:
    """Convert DOC/PPT through LibreOffice when one is installed."""
    executable = _soffice_path()
    if not executable:
        raise DocumentError(
            "missing_dependency",
            f"Reading .{os.path.splitext(path)[1].lstrip('.')} needs "
            "LibreOffice's headless converter. Install LibreOffice, or save "
            "the file as its modern format first.")
    folder = tempfile.mkdtemp(prefix="jarvis-doc-convert-")
    target = os.path.join(
        folder, os.path.splitext(os.path.basename(path))[0] + "." + target_ext)
    try:
        subprocess.run(
            [executable, "--headless", "--convert-to", target_ext,
             "--outdir", folder, path],
            timeout=max(5.0, min(MAX_TIMEOUT_S, timeout_s)),
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False)
    except subprocess.TimeoutExpired:
        shutil.rmtree(folder, ignore_errors=True)
        raise DocumentError(
            "timeout",
            "LibreOffice did not finish converting the legacy document in "
            "time.") from None
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(folder, ignore_errors=True)
        raise DocumentError(
            "conversion_failed",
            f"LibreOffice could not convert the document: "
            f"{type(exc).__name__}.") from None
    if not os.path.isfile(target):
        shutil.rmtree(folder, ignore_errors=True)
        raise DocumentError(
            "conversion_failed",
            "LibreOffice did not produce the expected converted file.")

    def cleanup() -> None:
        shutil.rmtree(folder, ignore_errors=True)

    return target, cleanup


# ── public entry point ───────────────────────────────────────────────────────
_READERS = {
    "text": _read_plain,
    "markdown": _read_plain,
    "log": _read_plain,
    "rtf": _read_rtf,
    "html": _read_html,
    "xml": _read_xml,
    "json": _read_json,
    "yaml": _read_yaml,
    "csv": _read_delimited,
    "tsv": _read_delimited,
    "pdf": _read_pdf,
    "epub": _read_epub,
    "docx": _read_docx,
    "odt": _read_odt,
    "xlsx": _read_xlsx,
    "xls": _read_xls,
    "ods": _read_ods,
    "pptx": _read_pptx,
    "odp": _read_odp,
}


def _error_result(code: str, message: str, *, path: str = "", fmt: str = "",
                  **details: Any) -> Dict[str, Any]:
    print(f"[ReadDocument] {code}: {message}")
    result: Dict[str, Any] = {
        "success": False,
        "ok": False,
        "operation": "read_document",
        "reason": code,
        "error": message,
        "format": fmt,
        "supported_formats": sorted(SUPPORTED_FORMATS),
    }
    if path:
        result["path"] = path
        result["name"] = os.path.basename(path)
    result.update(details)
    return result


def _selection_warnings(fmt: str, page: int, sheet: str, slide: int,
                        section: int) -> List[str]:
    warnings: List[str] = []
    if page and fmt not in ("pdf",):
        warnings.append("Page selection only applies to PDFs; the whole "
                        "document was read.")
    if sheet and fmt not in ("xlsx", "xls", "ods"):
        warnings.append("Sheet selection only applies to spreadsheets; the "
                        "whole document was read.")
    if slide and fmt not in ("pptx", "ppt", "odp"):
        warnings.append("Slide selection only applies to presentations; the "
                        "whole document was read.")
    if section and fmt != "epub":
        warnings.append("Section selection only applies to EPUB chapters; the "
                        "whole document was read.")
    return warnings


def _resolve_document_path(raw_path: str) -> str:
    """Resolve a document path the same way the file tools resolve one.

    The tool declaration advertises shell folders such as ``Downloads\\x.pdf``;
    resolving those against the process working directory silently looks in
    the wrong place. Fall back to the absolute form only when the shared
    resolver cannot identify the path.
    """
    try:
        from actions import open_file_tool

        resolved = open_file_tool.resolve_open_target(raw_path)
        if resolved.get("ok") and resolved.get("path"):
            return str(resolved["path"])
    except Exception:  # noqa: BLE001 - resolution is best effort
        pass
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw_path)))


def read_document(
    path: str,
    max_chars: Any = 0,
    page: Any = 0,
    sheet: str = "",
    slide: Any = 0,
    section: Any = 0,
    max_pages: Any = 0,
    max_rows: Any = 0,
    timeout_seconds: Any = 0,
    encoding: str = "",
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Read one local document and return bounded, structured text.

    ``path`` is required.  The limits are optional; every one has a safe
    default and a hard cap.  ``cancel_event`` is an internal hook used by
    session teardown and tests, not a model-facing parameter.
    """
    raw_path = _clean(path)
    if not raw_path:
        return _error_result(
            "invalid_argument", "A document path is required.")
    resolved = _resolve_document_path(raw_path)
    try:
        size, fmt = _preflight(resolved)
    except DocumentError as exc:
        return _error_result(exc.code, exc.message, path=resolved,
                             **exc.details)
    if not fmt or fmt == "ole":
        modern = format_from_extension(resolved)
        if fmt == "ole" and modern in ("docx", "xlsx", "pptx"):
            return _error_result(
                "encrypted",
                "This Office file is password-protected or is an older binary "
                "file with a modern extension. Remove the password or save a "
                "normal DOCX/XLSX/PPTX copy before reading it.",
                path=resolved, fmt=modern)
        return _error_result(
            "unsupported",
            "That file format is not supported. Supported formats: "
            + ", ".join(sorted(SUPPORTED_FORMATS)) + ".",
            path=resolved, fmt=fmt or "unknown")
    if fmt not in SUPPORTED_FORMATS:
        return _error_result(
            "unsupported",
            f"'{os.path.basename(resolved)}' has an unsupported format "
            f"({fmt}). Supported formats: "
            + ", ".join(sorted(SUPPORTED_FORMATS)) + ".",
            path=resolved, fmt=fmt)

    char_limit = _number(max_chars, MAX_CHARS_DEFAULT, 200, MAX_CHARS_LIMIT)
    page_number = _positive(page)
    slide_number = _positive(slide)
    section_number = _positive(section)
    page_limit = _option_int(max_pages, MAX_PAGES_DEFAULT, MAX_PAGES_LIMIT)
    row_limit = _option_int(max_rows, MAX_ROWS_DEFAULT, MAX_ROWS_LIMIT)
    timeout_s = float(_number(
        timeout_seconds, int(DEFAULT_TIMEOUT_S), 1, int(MAX_TIMEOUT_S)))
    ctx = _Context(char_limit, timeout_s, cancel_event)
    warnings = _selection_warnings(
        fmt, page_number, _clean(sheet), slide_number, section_number)
    metadata: Dict[str, Any] = {
        "name": os.path.basename(resolved),
        "path": resolved,
        "format": fmt,
        "format_label": display_format(fmt),
        "format_source": detect_format(resolved)[1],
        "size_bytes": size,
        "size_mb": round(size / (1024 * 1024), 3),
        "limits": {
            "max_chars": char_limit,
            "max_pages": page_limit,
            "max_rows": row_limit,
            "timeout_seconds": timeout_s,
        },
    }
    converted: Optional[Tuple[str, Callable[[], None]]] = None
    try:
        if fmt in ("doc", "ppt"):
            target_ext = "docx" if fmt == "doc" else "pptx"
            converted_path, cleanup = _convert_legacy(
                resolved, target_ext, timeout_s)
            converted = (converted_path, cleanup)
            reader = _read_docx if fmt == "doc" else _read_pptx
            if fmt == "doc":
                extra, reader_warnings = reader(ctx, converted_path)
            else:
                extra, reader_warnings = reader(
                    ctx, converted_path, slide=slide_number,
                    max_slides=page_limit)
            metadata["converted_via"] = "LibreOffice"
        elif fmt == "pdf":
            extra, reader_warnings = _read_pdf(
                ctx, resolved, page=page_number, max_pages=page_limit)
        elif fmt == "epub":
            extra, reader_warnings = _read_epub(
                ctx, resolved, section=section_number,
                max_chapters=page_limit)
        elif fmt in ("xlsx", "xls", "ods"):
            reader = {"xlsx": _read_xlsx, "xls": _read_xls,
                      "ods": _read_ods}[fmt]
            extra, reader_warnings = reader(
                ctx, resolved, sheet=_clean(sheet), max_rows=row_limit)
        elif fmt in ("pptx", "odp"):
            reader = _read_pptx if fmt == "pptx" else _read_odp
            extra, reader_warnings = reader(
                ctx, resolved, slide=slide_number, max_slides=page_limit)
        elif fmt in ("csv", "tsv"):
            extra, reader_warnings = _read_delimited(
                ctx, resolved, fmt, encoding=encoding, max_rows=row_limit)
        elif fmt == "xml":
            extra, reader_warnings = _read_xml(ctx, resolved, encoding)
        elif fmt == "html":
            extra, reader_warnings = _read_html(ctx, resolved, encoding)
        elif fmt == "json":
            extra, reader_warnings = _read_json(ctx, resolved, encoding)
        elif fmt == "yaml":
            extra, reader_warnings = _read_yaml(ctx, resolved, encoding)
        elif fmt == "rtf":
            extra, reader_warnings = _read_rtf(ctx, resolved, encoding)
        elif fmt == "docx":
            extra, reader_warnings = _read_docx(ctx, resolved)
        elif fmt == "odt":
            extra, reader_warnings = _read_odt(ctx, resolved)
        else:
            extra, reader_warnings = _read_plain(ctx, resolved, fmt, encoding)
    except DocumentError as exc:
        return _error_result(exc.code, exc.message, path=resolved, fmt=fmt,
                             **exc.details)
    except Exception as exc:  # noqa: BLE001 - parser bug must not crash a turn
        return _error_result(
            "read_failed",
            f"The document could not be read ({type(exc).__name__}: {exc}).",
            path=resolved, fmt=fmt)
    finally:
        if converted is not None:
            converted[1]()

    metadata.update(extra)
    warnings.extend(reader_warnings)
    content = ctx.text()
    if not content:
        return _error_result(
            "no_text",
            "The document opened, but no readable text was extracted.",
            path=resolved, fmt=fmt, metadata=metadata)
    if ctx.truncated:
        metadata["truncated"] = True
        warnings.append(
            f"Output was truncated to {char_limit} characters. Increase "
            "max_chars or read a specific page/sheet/slide.")
    else:
        metadata["truncated"] = False
    metadata["warnings"] = warnings
    metadata["character_count"] = len(content)
    status = "partial" if (warnings or ctx.truncated) else "success"
    return {
        "success": True,
        "ok": True,
        "operation": "read_document",
        "status": status,
        "message": (f"Read {len(content)} characters from '{metadata['name']}' "
                    f"({metadata['format_label']})."),
        "path": resolved,
        "name": metadata["name"],
        "format": fmt,
        "format_label": metadata["format_label"],
        "content": content,
        "metadata": metadata,
        "truncated": bool(ctx.truncated),
        "warnings": warnings,
        **{key: value for key, value in extra.items()
           if key in ("page_count", "sheet_names", "slide_count",
                      "chapter_count", "row_count", "paragraph_count",
                      "table_count", "encoding", "empty_pages")},
    }

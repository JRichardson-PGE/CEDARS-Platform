"""The text of a document's files, read when a file is stored, so search finds documents by the words inside them (BR-15.2). PDF
through pypdf; Word, PowerPoint and Excel (the 2007 formats) from the text runs of their XML; plain text and CSV as they are. Older
Office formats, images and zip files are not read (a scanned PDF has no text either): their documents are still found by title, summary
and file name. Reading never stops a file being stored: a file whose text cannot be read gets the reason instead."""

from __future__ import annotations

import html
import logging
import re
import zipfile
from pathlib import Path

from django.contrib.postgres.search import SearchVector

log = logging.getLogger(__name__)

MAX_CHARS = 2_000_000  # the text kept per file
MAX_INDEXED = 300_000  # the characters indexed (PostgreSQL's tsvector holds at most 1 MB)
MAX_PDF_PAGES = 2_000
MAX_XML_BYTES = 50 * 1024 * 1024  # per part of an Office file, uncompressed (a zip bomb stops here)

# the XML parts of an Office file that hold its words, and the element each word run sits in
OFFICE_PARTS = {
    ".docx": (re.compile(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml"), "w:t"),
    ".pptx": (re.compile(r"ppt/(slides/slide\d+|notesSlides/notesSlide\d+)\.xml"), "a:t"),
    ".xlsx": (re.compile(r"xl/sharedStrings\.xml"), "t"),
    ".xlsm": (re.compile(r"xl/sharedStrings\.xml"), "t"),
}
TEXT_TYPES = {".txt", ".csv"}
PARAGRAPH_END = re.compile(r"</(?:w:p|a:p|si)>")


class Unreadable(Exception):
    """str(e) is the reason recorded beside the file."""


def readable(name: str) -> bool:
    ext = Path(name).suffix.lower()
    return ext == ".pdf" or ext in OFFICE_PARTS or ext in TEXT_TYPES


def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as e:
            raise Unreadable("The PDF is encrypted.") from e
    out, size = [], 0
    for i, page in enumerate(reader.pages):
        if i >= MAX_PDF_PAGES or size >= MAX_CHARS:
            break
        text = page.extract_text() or ""
        out.append(text)
        size += len(text)
    return "\n".join(out)


def _office(path: Path, ext: str) -> str:
    parts, tag = OFFICE_PARTS[ext]
    run = re.compile(rf"<{re.escape(tag)}(?:\s[^>]*)?>([^<]*)</{re.escape(tag)}>")
    out, size = [], 0
    with zipfile.ZipFile(path) as z:
        for info in sorted(z.infolist(), key=lambda i: _natural(i.filename)):
            if not parts.fullmatch(info.filename):
                continue
            if info.file_size > MAX_XML_BYTES:
                raise Unreadable(f"{info.filename} is too large to read.")
            xml = z.read(info).decode("utf-8", errors="replace")
            # a paragraph, a slide's paragraph or a cell's string is a line; the runs within it are joined as written
            lines = ["".join(html.unescape(m) for m in run.findall(chunk)) for chunk in PARAGRAPH_END.split(xml)]
            text = "\n".join(line for line in lines if line.strip())
            out.append(text)
            size += len(text)
            if size >= MAX_CHARS:
                break
    return "\n".join(out)


def _natural(name: str):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name)]


def _plain(path: Path) -> str:
    raw = path.read_bytes()[: MAX_CHARS * 2]
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read(path: Path, name: str) -> str:
    """The text of a stored file; Unreadable with the reason when there is none."""
    ext = Path(name).suffix.lower()
    if not readable(name):
        raise Unreadable(f"Text is not read from {ext or 'these'} files.")
    try:
        if ext == ".pdf":
            text = _pdf(path)
        elif ext in OFFICE_PARTS:
            text = _office(path, ext)
        else:
            text = _plain(path)
    except Unreadable:
        raise
    except Exception as e:  # a damaged or unusual file: stored all the same, found by its name
        log.info("no text read from %s: %s", name, e)
        raise Unreadable("The file's text could not be read.") from e
    text = text.replace("\x00", "")[:MAX_CHARS]
    if not text.strip():
        raise Unreadable("The file has no text (a scanned PDF needs OCR).")
    return text


def save(file, path: Path) -> None:
    """Read a stored DocumentFile's text from its local copy and keep it with its search index."""
    from .models import FileText

    try:
        text, error = read(path, file.name), ""
    except Unreadable as e:
        text, error = "", str(e)
    row, _ = FileText.objects.update_or_create(file=file, defaults={"text": text, "error": error})
    if text:
        FileText.objects.filter(pk=row.pk).update(vector=SearchVector(_indexed(), config="english"))


def _indexed():
    from django.db.models.functions import Left

    return Left("text", MAX_INDEXED)

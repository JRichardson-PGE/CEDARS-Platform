"""What the document accessibility check looks for in a file (BRD v0.5 BR-10.3, WCAG 2.2 AA for published documents). Pure functions over
a file on disk: no database, no settings, so the same check runs on an upload and on every workbook the platform writes (the tests hold
each generated workbook to no findings at all).

PDF (through pypdf): untagged (no structure tree, or not marked as tagged), image only (no text on any of the first pages: a scan),
no title (neither the document information nor the XMP metadata names one), no language (the catalog has no /Lang).

Workbooks (.xlsx, .xlsm; their XML read directly, so a large workbook costs a few parts, not a loaded workbook): a sheet with data and no
marked header row (no Excel table with a header row, no print titles and no "Title" defined name, the conventions screen readers and
printing use), merged cells, a picture, chart or shape with no alternative text, and sheets left with Excel's default names ("Sheet1").

Other types (Word, PowerPoint, older Excel formats, images, text, zip) are not checked: the result says so and records nothing against
the file. Reading never raises: a file that cannot be read gets a note instead of findings."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

# Every finding the check can make: label (what the admin page and the uploader see), what to do about it, the WCAG 2.2 success
# criteria it bears on, and the kind of file.
CODES: dict[str, dict] = {
    "pdf_untagged": {
        "label": "PDF is not tagged",
        "fix": "Export it again with tags on (in Word: Save as PDF, Options, Document structure tags for accessibility).",
        "wcag": ["1.3.1", "1.3.2", "4.1.2"],
        "kind": "pdf",
    },
    "pdf_image_only": {
        "label": "PDF has no text (scanned pages)",
        "fix": "Run text recognition (OCR) on it, or file the source document as a PDF exported with its text.",
        "wcag": ["1.1.1", "1.4.5"],
        "kind": "pdf",
    },
    "pdf_no_title": {
        "label": "PDF has no title",
        "fix": "Give it a title in its document properties before exporting.",
        "wcag": ["2.4.2"],
        "kind": "pdf",
    },
    "pdf_no_language": {
        "label": "PDF has no language",
        "fix": "Set the document's language (for example English, United States) before exporting.",
        "wcag": ["3.1.1"],
        "kind": "pdf",
    },
    "workbook_no_headers": {
        "label": "Sheet has no marked header row",
        "fix": "Format the data as a table with a header row (Home, Format as Table, My table has headers).",
        "wcag": ["1.3.1"],
        "kind": "workbook",
    },
    "workbook_merged_cells": {
        "label": "Sheet has merged cells",
        "fix": "Unmerge them; use Center Across Selection, or a heading row, for the same look.",
        "wcag": ["1.3.1", "1.3.2"],
        "kind": "workbook",
    },
    "workbook_no_alt_text": {
        "label": "Picture, chart or shape has no alternative text",
        "fix": "Give each one alternative text (right-click it, View Alt Text), or mark a purely decorative one as decorative.",
        "wcag": ["1.1.1"],
        "kind": "workbook",
    },
    "workbook_default_sheet_name": {
        "label": "Sheet keeps a default name",
        "fix": "Rename the sheet to say what it holds.",
        "wcag": ["2.4.6"],
        "kind": "workbook",
    },
}

PDF_TYPES = {".pdf"}
WORKBOOK_TYPES = {".xlsx", ".xlsm"}
TEXT_PAGES = 20  # an image-only PDF has no text on any of its first pages
MAX_XML_BYTES = 50 * 1024 * 1024  # per part of a workbook, uncompressed (a zip bomb stops here)

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "dc": "http://purl.org/dc/elements/1.1/",
}
DEFAULT_SHEET = re.compile(r"(sheet|chart)\d*", re.IGNORECASE)
TITLE_NAME = re.compile(r"(_xlnm\.print_titles|(column|row)?title.*)", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    code: str
    where: str = ""  # a sheet's name, or "" for the whole file
    count: int = 1

    @property
    def label(self) -> str:
        return CODES[self.code]["label"]

    @property
    def fix(self) -> str:
        return CODES[self.code]["fix"]

    def describe(self) -> str:
        where = f" ({self.where})" if self.where else ""
        many = f", {self.count}" if self.count > 1 else ""
        return f"{self.label}{where}{many}"

    def as_dict(self) -> dict:
        return {"code": self.code, "where": self.where, "count": self.count}

    @classmethod
    def from_dict(cls, d: dict) -> Finding:
        return cls(d["code"], d.get("where", ""), int(d.get("count", 1)))


@dataclass
class Result:
    kind: str  # "pdf", "workbook" or "" (a type the check does not read)
    findings: list[Finding] = field(default_factory=list)
    note: str = ""  # why the file was not (fully) checked

    @property
    def checked(self) -> bool:
        return bool(self.kind) and not self.note

    @property
    def clear(self) -> bool:
        return not self.findings

    def codes(self) -> set[str]:
        return {f.code for f in self.findings}


def checkable(name: str) -> bool:
    ext = Path(name).suffix.lower()
    return ext in PDF_TYPES or ext in WORKBOOK_TYPES


def check(path: Path | str, name: str = "") -> Result:
    """The check of one file on disk; `name` (default: the path's) decides its type."""
    path = Path(path)
    ext = Path(name or path.name).suffix.lower()
    if ext in PDF_TYPES:
        return _guarded("pdf", _pdf, path)
    if ext in WORKBOOK_TYPES:
        return _guarded("workbook", _workbook, path)
    return Result("", note=f"{ext or 'These'} files are not checked.")


def check_bytes(data: bytes, name: str) -> Result:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=Path(name).suffix) as tmp:
        tmp.write(data)
        tmp.flush()
        return check(Path(tmp.name), name)


def _guarded(kind, fn, path) -> Result:
    try:
        return Result(kind, fn(path))
    except _Unchecked as e:
        return Result(kind, note=str(e))
    except Exception:  # a damaged or unusual file: stored all the same, and listed as not checked
        return Result(kind, note="The file could not be read, so it was not checked.")


class _Unchecked(Exception):
    pass


# --- PDF


def _pdf(path: Path) -> list[Finding]:
    from pypdf import PdfReader
    from pypdf.generic import NameObject

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise _Unchecked("The PDF is password-protected, so it was not checked.")
        except _Unchecked:
            raise
        except Exception as e:
            raise _Unchecked("The PDF is encrypted, so it was not checked.") from e
    root = reader.trailer["/Root"].get_object()
    out = []
    mark = root.get(NameObject("/MarkInfo"))
    marked = bool(mark and mark.get_object().get("/Marked"))
    if "/StructTreeRoot" not in root or not marked:
        out.append(Finding("pdf_untagged"))
    if not any((page.extract_text() or "").strip() for page in reader.pages[:TEXT_PAGES]):
        out.append(Finding("pdf_image_only"))
    if not _pdf_title(reader, root):
        out.append(Finding("pdf_no_title"))
    lang = root.get("/Lang")
    if not (lang and str(lang.get_object()).strip()):
        out.append(Finding("pdf_no_language"))
    return out


def _pdf_title(reader, root) -> str:
    info = reader.metadata
    title = str(info.title or "").strip() if info else ""
    if title:
        return title
    if "/Metadata" in root:
        try:
            xmp = reader.xmp_metadata
            titles = xmp.dc_title if xmp else None
            if titles:
                return " ".join(str(t) for t in titles.values()).strip()
        except Exception:
            return ""
    return ""


# --- workbooks


def _xml(z: zipfile.ZipFile, part: str):
    info = z.getinfo(part)
    if info.file_size > MAX_XML_BYTES:
        raise _Unchecked(f"The workbook's {part} is too large to check.")
    return ET.fromstring(z.read(info))


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """A part's relationships, id -> the target part's path in the zip."""
    p = PurePosixPath(part)
    rels = str(p.parent / "_rels" / f"{p.name}.rels")
    if rels not in z.namelist():
        return {}
    out = {}
    for rel in _xml(z, rels).findall("pr:Relationship", NS):
        target = rel.get("Target", "")
        if rel.get("TargetMode") == "External":
            continue
        resolved = target.lstrip("/") if target.startswith("/") else _join(p.parent, target)
        out[rel.get("Id")] = resolved
    return out


def _join(base: PurePosixPath, target: str) -> str:
    parts = []
    for piece in (base / target).parts:
        if piece == "..":
            if parts:
                parts.pop()
        elif piece != ".":
            parts.append(piece)
    return "/".join(parts)


def _workbook(path: Path) -> list[Finding]:
    with zipfile.ZipFile(path) as z:
        book = "xl/workbook.xml"
        if book not in z.namelist():
            raise _Unchecked("The file is not an Excel workbook, so it was not checked.")
        wb = _xml(z, book)
        rels = _rels(z, book)
        sheets = [
            (s.get("name", ""), s.get("sheetId", ""), rels.get(s.get(f"{{{NS['r']}}}id"), ""), i)
            for i, s in enumerate(wb.findall("m:sheets/m:sheet", NS))
        ]
        titled = _titled_sheets(wb, [s[0] for s in sheets])
        out = []
        for name, _, part, index in sheets:
            if DEFAULT_SHEET.fullmatch(name.strip()):
                out.append(Finding("workbook_default_sheet_name", name))
            if not part or part not in z.namelist():
                continue
            ws = _xml(z, part)
            ws_rels = _rels(z, part)
            merged = len(ws.findall("m:mergeCells/m:mergeCell", NS))
            if merged:
                out.append(Finding("workbook_merged_cells", name, merged))
            has_data = ws.find("m:sheetData/m:row/m:c/m:v", NS) is not None or ws.find("m:sheetData/m:row/m:c/m:is", NS) is not None
            if has_data and index not in titled and not _has_header_table(z, ws, ws_rels):
                out.append(Finding("workbook_no_headers", name))
            drawings = [ws_rels.get(d.get(f"{{{NS['r']}}}id"), "") for d in ws.findall("m:drawing", NS)]
            missing = sum(_unnamed_drawings(z, d) for d in drawings if d)
            if missing:
                out.append(Finding("workbook_no_alt_text", name, missing))
        return out


def _titled_sheets(wb, names: list[str]) -> set[int]:
    """The sheets (by position) that a print-titles or "Title" defined name marks the header row of."""
    out = set()
    lowered = [n.lower() for n in names]
    for dn in wb.findall("m:definedNames/m:definedName", NS):
        if not TITLE_NAME.fullmatch(dn.get("name", "")):
            continue
        if dn.get("localSheetId") is not None:
            out.add(int(dn.get("localSheetId")))
            continue
        ref = (dn.text or "").strip()
        sheet = ref.rsplit("!", 1)[0].strip("'").replace("''", "'").lower() if "!" in ref else ""
        if sheet in lowered:
            out.add(lowered.index(sheet))
    return out


def _has_header_table(z, ws, ws_rels) -> bool:
    for tp in ws.findall("m:tableParts/m:tablePart", NS):
        part = ws_rels.get(tp.get(f"{{{NS['r']}}}id"))
        if part and part in z.namelist() and _xml(z, part).get("headerRowCount", "1") != "0":
            return True
    return False


def _unnamed_drawings(z, part: str) -> int:
    """How many pictures, charts and shapes in a drawing part have no alternative text and are not marked decorative."""
    if part not in z.namelist():
        return 0
    drawing, n = _xml(z, part), 0
    for element, nv in (("pic", "nvPicPr"), ("graphicFrame", "nvGraphicFramePr"), ("sp", "nvSpPr")):
        for el in drawing.iter(f"{{{NS['xdr']}}}{element}"):
            props = el.find(f"xdr:{nv}/xdr:cNvPr", NS)
            if props is None or (props.get("descr") or "").strip() or _decorative(props):
                continue
            if element == "sp" and el.find("xdr:txBody", NS) is not None:
                continue  # a text box reads as its text
            n += 1
    return n


def _decorative(props) -> bool:
    return any(el.tag.endswith("}decorative") and el.get("val") in ("1", "true") for el in props.iter())

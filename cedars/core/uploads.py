"""Several files in one filing upload (BR-06.3): Budgets, Claims and Monthly reports take more than one file at a time and consolidate
them into the one .zip their own reader takes, before any check or pricing. The engine always gets one job (Jake, 2026-10-31: ReCET
keeps its one job .zip; the filing modules consolidate).

Each file is a .zip of the module's tables as CSV files, an Excel workbook with a sheet per table, or one table's CSV file. A table may
come in parts across the files, or inside one .zip: a part is named after its table with a part label after it (`Measure_1.csv`,
`Measure (2).csv`, `Measure-part3.csv`). The parts of a table must have the same columns (in any order and case); their rows are joined
in the order the files were chosen. One .zip or .xlsx on its own is passed through unchanged, so a single upload is kept exactly as sent.

The module names its tables (`table_of`), and may refuse a name outright (`refuse`: Claims' PII tables, BR-10.1). A file or sheet that
names no table is written into the combined .zip under its own name, so the module's reader refuses it in its own words (a sheet is
skipped, as the readers skip a workbook's other sheets)."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.functional import lazy

SKIPPED = ("__MACOSX/", ".DS_Store", "Thumbs.db", "desktop.ini")
TEXT_SUFFIXES = (".csv", ".txt")
PART_LABEL = re.compile(r"[\s_\-.]*(?:\(\s*\d+\s*\)|(?:part|pt)?[\s_\-.]*\d+)$", re.IGNORECASE)
# The upload limits are settings, shown as they stand on the Limits page (/limits/) and beside every upload field (BR-12.1).
MAX_FILES = settings.CEDARS_UPLOAD_MAX_FILES  # for the API's field declarations; the checks read the setting itself


def max_files() -> int:
    return settings.CEDARS_UPLOAD_MAX_FILES


def max_mb() -> int:
    return settings.CEDARS_UPLOAD_MAX_MB


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB"


def too_large(name: str, size: int) -> str:
    """The refusal for a file over the size limit, or "" when it is within it."""
    if size is None or size <= max_mb() * 1024 * 1024:
        return ""
    return f"{name} is {_mb(size)}; each file may be at most {max_mb()} MB (see Limits, /limits/)."


def check_size(file) -> None:
    """A form or API field validator: refuses an uploaded file over CEDARS_UPLOAD_MAX_MB."""
    problem = too_large(getattr(file, "name", "The file"), getattr(file, "size", None))
    if problem:
        raise ValidationError(problem)


def _limit_text(base: str = "", several: bool = True) -> str:
    limit = f"Up to {max_files()} files of up to {max_mb()} MB each." if several else f"Up to {max_mb()} MB."
    return f"{base} {limit}".strip()


limit_text = lazy(_limit_text, str)  # a field's help text, read when the form is shown (so it follows the settings)


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A file field that takes one or more files; cleaned to a list."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        kwargs["help_text"] = limit_text(kwargs.get("help_text", ""))  # the limits beside the field, as they stand
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        one = super().clean
        files = [one(d, initial) for d in data] if isinstance(data, (list, tuple)) else [one(data, initial)]
        if len(files) > max_files():
            raise forms.ValidationError(f"Choose at most {max_files()} files; put more tables in a .zip.")
        for f in files:
            check_size(f)
        return files


class CombineError(ValueError):
    """The files cannot be combined; the message says why, for the person who uploaded them."""


@dataclass
class Combined:
    data: bytes  # the one upload the module reads: a single file as sent, else a .zip of one CSV per table
    filename: str
    sources: list[dict] = field(default_factory=list)  # each file chosen: name, sha256, bytes (empty for a single file)
    parts: dict[str, list[str]] = field(default_factory=dict)  # each table made of several parts: where each came from


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252")


def _csv_rows(content: bytes) -> list[list[str]]:
    """A CSV file's rows (pipe-, tab- or comma-delimited, as the modules read them), blank lines left out."""
    body = _text(content)
    first = body.split("\n", 1)[0]
    sep = "|" if "|" in first else ("\t" if "\t" in first else ",")
    return [r for r in csv.reader(io.StringIO(body), delimiter=sep) if r and any(c.strip() for c in r)]


def _cell(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "date") and hasattr(value, "hour"):  # a datetime from a date cell
        return value.date().isoformat() if not (value.hour or value.minute or value.second) else value.isoformat()
    return str(value)


def _skipped(name: str) -> bool:
    return any(s in name for s in SKIPPED) or PurePosixPath(name).name.startswith("._")


def table_for(stem: str, table_of: Callable[[str], str | None]) -> str | None:
    """The table a file or sheet name holds: the name itself, or the name with a part label taken off (`Measure_2` -> Measure)."""
    found = table_of(stem.strip())
    if found is None:
        base = PART_LABEL.sub("", stem.strip())
        if base and base != stem.strip():
            found = table_of(base)
    return found


def combine(
    files: list[tuple[str, bytes]],
    table_of: Callable[[str], str | None],
    *,
    refuse: Callable[[str], None] | None = None,
) -> Combined:
    """The files as one upload. `files` is [(file name, content)] in the order chosen; `table_of(name)` gives the table a name holds
    (None for none); `refuse(name)` raises for a name the module never accepts."""
    if not files:
        raise CombineError("Choose a file to upload.")
    if len(files) > max_files():  # the pages' field checks these too; the API reaches here directly
        raise CombineError(f"Choose at most {max_files()} files; put more tables in a .zip.")
    for name, content in files:
        if problem := too_large(name, len(content)):
            raise CombineError(problem)
    if len(files) == 1 and files[0][0].lower().endswith((".zip", ".xlsx")) and not _has_parts(files[0], table_of):
        return Combined(files[0][1], files[0][0])
    pieces: dict[str, list[tuple[str, list[list[str]]]]] = {}
    other: dict[str, bytes] = {}
    for name, content in files:
        lower = name.lower()
        if lower.endswith(".xlsx"):
            _from_workbook(name, content, table_of, refuse, pieces)
        elif lower.endswith(".zip"):
            _from_zip(name, content, table_of, refuse, pieces, other)
        elif lower.endswith(TEXT_SUFFIXES):
            _add(PurePosixPath(name).stem, name, content, table_of, refuse, pieces, other)
        else:
            raise CombineError(f"{name} is not a .zip, .xlsx or .csv file.")
    out = io.BytesIO()
    parts = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for table, chunks in pieces.items():
            z.writestr(f"{table}.csv", _join(table, chunks))
            if len(chunks) > 1:
                parts[table] = [where for where, _ in chunks]
        for name, content in other.items():
            z.writestr(name, content)
    sources = [{"name": n, "sha256": sha256(c), "bytes": len(c)} for n, c in files]
    label = files[0][0] if len(files) == 1 else f"{len(files)} files"
    return Combined(out.getvalue(), f"combined upload ({label}).zip", sources, parts)


def _has_parts(file: tuple[str, bytes], table_of) -> bool:
    """Whether a single .zip holds a table in parts (or a lone CSV named as a part), which then needs joining."""
    name, content = file
    if not name.lower().endswith(".zip"):
        return False
    try:
        members = zipfile.ZipFile(io.BytesIO(content)).namelist()
    except zipfile.BadZipFile:
        return False  # the module's reader says so in its own words
    stems = [PurePosixPath(m).stem for m in members if not m.endswith("/") and not _skipped(m) and m.lower().endswith(TEXT_SUFFIXES)]
    return any(table_of(s.strip()) is None and table_for(s, table_of) is not None for s in stems)


def _refuse(stem: str, refuse) -> None:
    """The module's refusal, on the name and on the name without a part label (PII_Contact_2 is PII_Contact)."""
    if refuse is not None:
        refuse(stem)
        refuse(PART_LABEL.sub("", stem.strip()) or stem)


def _add(stem, where, content, table_of, refuse, pieces, other) -> None:
    _refuse(stem, refuse)
    table = table_for(stem, table_of)
    if table is None:
        other[_unique(PurePosixPath(where).name, other)] = content
        return
    pieces.setdefault(table, []).append((where, _csv_rows(content)))


def _unique(name: str, taken: dict) -> str:
    if name not in taken:
        return name
    p = PurePosixPath(name)
    n = 2
    while f"{p.stem} ({n}){p.suffix}" in taken:
        n += 1
    return f"{p.stem} ({n}){p.suffix}"


def _from_zip(name, content, table_of, refuse, pieces, other) -> None:
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise CombineError(f"{name} is not a valid .zip.") from None
    if any(i.flag_bits & 0x1 for i in z.infolist()):
        raise CombineError(f"{name} is password-protected; upload it without a password.")
    for info in z.infolist():
        member = info.filename
        if info.is_dir() or _skipped(member):
            continue
        p = PurePosixPath(member.replace("\\", "/"))
        if len(p.parts) > 2 or p.suffix.lower() not in TEXT_SUFFIXES:
            other[_unique(member, other)] = z.read(info)
            continue
        _add(p.stem, f"{name}: {member}", z.read(info), table_of, refuse, pieces, other)


def _from_workbook(name, content, table_of, refuse, pieces) -> None:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:  # openpyxl raises several kinds for a file that is not a workbook
        raise CombineError(f"{name} is not a valid Excel workbook (.xlsx).") from None
    try:
        for ws in wb.worksheets:
            _refuse(ws.title, refuse)
            table = table_for(ws.title, table_of)
            if table is None:
                continue  # a workbook's other sheets (instructions, lists) are skipped, as the modules' readers skip them
            rows = []
            for row in ws.iter_rows(values_only=True):
                if row is None or all(c is None or str(c).strip() == "" for c in row):
                    continue
                rows.append(["" if c is None else _cell(c) for c in row])
            pieces.setdefault(table, []).append((f"{name}: sheet {ws.title}", rows))
    finally:
        wb.close()


def _join(table: str, chunks: list[tuple[str, list[list[str]]]]) -> bytes:
    """One table's parts as one comma-delimited UTF-8 CSV under the first part's header; every part must have the same columns."""
    first_where, first_rows = next(((w, r) for w, r in chunks if r), chunks[0])
    header = [h.strip() for h in first_rows[0]] if first_rows else []
    keys = [h.lower() for h in header]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    if header:
        w.writerow(header)
    for where, rows in chunks:
        if not rows:
            continue
        cols = [h.strip().lower() for h in rows[0]]
        if sorted(cols) != sorted(keys):
            missing = [h for h, k in zip(header, keys, strict=True) if k not in cols]
            extra = [h.strip() for h in rows[0] if h.strip().lower() not in keys]
            said = "; ".join(x for x in (f"no {', '.join(missing)}" if missing else "", f"also {', '.join(extra)}" if extra else "") if x)
            raise CombineError(
                f"The {table} parts have different columns: {where} has {said or 'a repeated column'} compared with {first_where}. "
                "Give every part of a table the same columns."
            )
        at = [cols.index(k) for k in keys]
        for r in rows[1:]:
            w.writerow([r[i] if i < len(r) else "" for i in at])
    return out.getvalue().encode("utf-8")

"""A claims upload: a .zip of the template's tables as CSV files (comma-, pipe- or tab-delimited, named after the table: Claim.csv,
Measure.csv, ...) or an Excel workbook with a sheet per table. A quarter's upload holds the records that are new or changed
since the last confirmed quarter, so any of the tables may be left out. Columns are read under their canonical names (CEDARS' names
and CET's are accepted); a column the template does not have is ignored, not rejected.

The tables, their names and columns are the filing's specification version's (`spec.Layout`, passed as `lay`). The PII tables never
come this way: a file named after one of the version's vault tables (PII_Contact, PII_Site) refuses the whole upload."""

import csv
import io
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from cedars.core import rows as compact
from cedars.core import uploads

from . import spec

SKIPPED = ("__MACOSX/", ".DS_Store", "Thumbs.db", "desktop.ini")


class UploadError(ValueError):
    """The upload is not a claims upload; the message says why, in words for the person who uploaded it."""


@dataclass
class Table:
    """One table of an upload, as text: rows keyed by canonical column names, in file order (row i is line i + 2 of the file). Rows
    read from a file are compact rows (cedars.core.rows.Row), which read like dicts."""

    name: str
    columns: list[str]
    rows: list[dict[str, str]]
    ignored: list[str] = field(default_factory=list)

    def get(self, row: dict, column: str) -> str:
        return row.get(column, "")


def combine(files: list[tuple[str, bytes]], lay: spec.Layout) -> uploads.Combined:
    """Several files chosen at once as the one upload read takes (cedars.core.uploads); a PII table in any of them refuses
    the whole upload, as in one file."""
    try:
        return uploads.combine(files, lay.table_name, refuse=lambda name: _no_pii(name, lay))
    except uploads.CombineError as e:
        raise UploadError(str(e)) from None


def read(data: bytes, filename: str, lay: spec.Layout) -> dict[str, Table]:
    """{table name: Table} from an uploaded .zip or .xlsx, read as the specification version `lay` lays the tables out."""
    problem = uploads.too_large_unzipped("The file", data)
    if problem:
        raise UploadError(problem)
    raw = _from_workbook(data, lay) if filename.lower().endswith(".xlsx") else _from_zip(data, lay)
    if not raw:
        raise UploadError(f"The file holds none of the claims tables ({', '.join(lay.TABLES)}).")
    shared = compact.Shared()  # a value repeated across the tables (a ClaimID, a code) is held once
    return {name: _table(name, content, lay, shared) for name, content in raw.items()}


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UploadError(
            "The file is not a valid .zip or .xlsx. Upload a .zip of the claims tables as CSV files, or an Excel workbook with a sheet "
            "per table."
        ) from None


def _from_zip(data: bytes, lay: spec.Layout) -> dict[str, Callable]:
    """{table: a function opening its CSV}: each member is read (streamed) only once the archive is known to hold only the tables."""
    z = _open_zip(data)
    out, extra = {}, []
    for info in z.infolist():
        name = info.filename
        if info.is_dir() or any(s in name for s in SKIPPED) or PurePosixPath(name).name.startswith("._"):
            continue
        p = PurePosixPath(name)
        if len(p.parts) > 2 or p.suffix.lower() not in (".csv", ".txt"):
            extra.append(name)
            continue
        _no_pii(p.stem, lay)
        table = lay.table_name(p.stem)
        if table is None:
            extra.append(name)
        elif table in out:
            raise UploadError(f"The .zip holds two {table} files; keep one.")
        else:
            out[table] = lambda info=info: z.open(info)
    if extra:
        raise UploadError(f"The .zip may hold only the claims tables ({', '.join(lay.TABLES)}); it also has {', '.join(sorted(extra))}.")
    return out


def _no_pii(name: str, lay: spec.Layout) -> None:
    if lay.is_pii_table(name):
        raise UploadError(
            f"{name} holds personal information, which never travels with the claim records. Remove it from this file and send it "
            "through the PII upload; the claim records carry only the SiteID and ContactID keys."
        )


def _from_workbook(data: bytes, lay: spec.Layout) -> dict[str, list[list[str]]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # openpyxl raises several kinds for a file that is not a workbook
        raise UploadError("The file is not a valid Excel workbook (.xlsx).") from None
    out = {}
    for ws in wb.worksheets:
        _no_pii(ws.title, lay)
        table = lay.table_name(ws.title)
        if table is None:
            continue
        rows = []
        for row in ws.iter_rows(values_only=True):
            if row is None or all(c is None or str(c).strip() == "" for c in row):
                continue
            rows.append(["" if c is None else _cell(c) for c in row])
        out[table] = rows
    wb.close()
    return out


def _cell(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "date") and hasattr(value, "hour"):  # a datetime from a date cell
        return value.date().isoformat() if not (value.hour or value.minute or value.second) else value.isoformat()
    return str(value)


def _names(name: str, header: list[str], lay: spec.Layout | None) -> list[str | None]:
    """The header's canonical names (lay: the version's; None for a file this module wrote, read as it is); None for a column the
    template does not have. A field given twice refuses the upload."""
    names = [lay.canonical(name, h) for h in header] if lay is not None else [h or None for h in header]
    seen: set[str] = set()
    for h, n in zip(header, names, strict=True):
        if n is not None and n in seen:
            raise UploadError(f"{name} has the column {n} twice (as {h} and another name); keep one.")
        if n is not None:
            seen.add(n)
    return names


def _table(name: str, raw: "list[list[str]] | Callable", lay: spec.Layout | None, shared: compact.Shared | None = None) -> Table:
    """A table's rows under its fields' canonical names (lay: the version's; None for a file this module wrote, read as it is). `raw`
    is a workbook sheet's rows (the header first) or a function opening a CSV."""
    names: list[str | None] = []

    def named(header: list[str]) -> list[str | None]:
        names[:] = _names(name, header, lay)
        return names

    if callable(raw) or isinstance(raw, bytes):
        header, rows = compact.read_csv(raw, shared, names=named)
    else:
        lines = [r for r in raw if r and any(str(c).strip() for c in r)]
        if not lines:
            return Table(name, [], [])
        header = [h.strip() for h in lines[0]]
        names = _names(name, header, lay)
        rows = compact.rows(names, lines[1:], shared)
    if not header:
        return Table(name, [], [])
    columns = [n for n in names if n is not None]
    return Table(name, columns, rows, ignored=[h for h, n in zip(header, names, strict=True) if n is None])


def table_from_csv(name: str, content: bytes, lay: spec.Layout) -> Table:
    """One table's CSV read as an upload reads it (the pre-upload checker)."""
    return _table(name, content, lay)


def without_system_fields(t: Table, lay: spec.Layout) -> Table:
    """An upload's table without the columns CEDARS fills in (the version's system fields): they are ignored, as an unknown column is."""
    drop = [c for c in lay.SYSTEM_FIELDS.get(t.name, ()) if c in t.columns]
    if not drop:
        return t
    keep = [c for c in t.columns if c not in drop]
    index = {c: i for i, c in enumerate(keep)}
    rows = [compact.Row(index, [r.get(c, "") for c in keep]) for r in t.rows]
    return Table(t.name, keep, rows, ignored=t.ignored + drop)


# --- writing tables


def to_csv(columns: list[str], rows: list[dict[str, str]]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([r.get(c, "") for c in columns])
    return out.getvalue().encode("utf-8")


def lines(columns: list[str], rows: Iterable) -> Iterable[list[str]]:
    """Each row's cells in the columns' order (blank where it has none), as to_csv writes them."""
    return ([r.get(c, "") for c in columns] for r in rows)


def to_zip(tables: dict[str, Table], order=()) -> bytes:
    """Tables as a .zip of comma-delimited CSVs, in `order` (the version's table order) and then as given (an empty table is left
    out). Each CSV is streamed into the archive as it is written."""
    out = io.BytesIO()
    names = [n for n in order if n in tables] + [n for n in tables if n not in order]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            t = tables[name]
            if t.rows:
                compact.write_csv(z, f"{name}.csv", t.columns, lines(t.columns, t.rows))
    return out.getvalue()


def from_zip(data: bytes) -> dict[str, Table]:
    """Read back a .zip this module wrote (to_zip): each file is a table named after it, its columns as written, whatever version
    the filing follows."""
    if not data:
        return {}
    z = _open_zip(data)
    out, shared = {}, compact.Shared()
    for info in z.infolist():
        p = PurePosixPath(info.filename)
        if not info.is_dir() and p.suffix.lower() == ".csv":
            out[p.stem] = _table(p.stem, lambda info=info: z.open(info), None, shared)
    return out

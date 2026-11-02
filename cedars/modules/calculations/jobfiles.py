"""A job's upload: one .zip holding Measure.csv and, optionally, ProgramCost.csv, as CET and ReCET take a job (ReCET 0.1.35), or ReCET's
job workbook (.xlsx / .xlsm: a Measure sheet and the optional ProgramCost, RunParameters, SWAllocation, GasProfiles, Programs and
Profiles8760 sheets; ReCET `recet/workbook.py`), which the worker takes as its `workbook` input. The platform reads the upload once,
streaming, before the job is queued: the files or sheets it holds (the same rules as the engine's `web/jobzip.py` and `workbook.py`, so a
wrong file is refused here and not after a wait in the queue), the number of measure records (the queue lane, the estimate, the record
cap) and the earliest claim year (the statewide allocation version a run naming none uses). The engine checks everything else."""

from __future__ import annotations

import codecs
import csv
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

NAMES = {"measure": "measures", "programcost": "programcost"}
SUFFIXES = (".csv", ".txt")
SKIPPED_NAMES = {"thumbs.db", "desktop.ini"}
YEAR_COLUMNS = ("ClaimYearQuarter",)
# The job workbook's sheets (ReCET workbook.SHEETS), matched as the engine matches them: ignoring case, spaces and underscores. Any other
# sheet (Instructions ...) is ignored, and a sheet with a header row but no data rows counts as not given.
SHEETS = ("Measure", "ProgramCost", "RunParameters", "SWAllocation", "GasProfiles", "Programs", "Profiles8760")
WORKBOOK = "workbook"
ZIP = "zip"


class JobFileError(ValueError):
    """The upload cannot be used; the message says why, for the person who uploaded it."""


@dataclass
class JobInfo:
    measures: str  # the Measure file's name in the zip, or the Measure sheet's name in a workbook
    programcost: str | None
    records: int
    first_year: int | None
    kind: str = ZIP  # ZIP or WORKBOOK
    sheets: tuple[str, ...] = ()  # a workbook's sheets with data rows, by their canonical names (SHEETS)


def _skipped(name: str) -> bool:
    p = PurePosixPath(name)
    return "__MACOSX" in p.parts or p.name.startswith(".") or p.name.lower() in SKIPPED_NAMES


def members(z: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    found: dict[str, zipfile.ZipInfo] = {}
    other: list[str] = []
    for info in z.infolist():
        if info.is_dir() or _skipped(info.filename):
            continue
        p = PurePosixPath(info.filename.replace("\\", "/"))
        key = NAMES.get(p.stem.lower()) if p.suffix.lower() in SUFFIXES else None
        if key is None or len(p.parts) > 2:
            other.append(info.filename)
            continue
        if key in found:
            raise JobFileError(f"The zip has more than one {'Measure' if key == 'measures' else 'ProgramCost'} file.")
        found[key] = info
    if other:
        shown = ", ".join(other[:5]) + (f" and {len(other) - 5} more" if len(other) > 5 else "")
        raise JobFileError(f"The zip may hold only Measure.csv and ProgramCost.csv; it also has {shown}.")
    if "measures" not in found:
        raise JobFileError("The zip has no Measure.csv.")
    if any(i.flag_bits & 0x1 for i in found.values()):
        raise JobFileError("The zip is password-protected; upload it without a password.")
    return found


def _year(value: str) -> int | None:
    value = (value or "").strip()
    return int(value[:4]) if len(value) >= 4 and value[:4].isdigit() else None


def scan_measures(stream) -> tuple[int, int | None]:
    """(records, earliest claim year) of a Measure file (pipe- or comma-delimited, UTF-8 or cp1252), read line by line."""
    reader = codecs.getreader("utf-8")(stream, errors="replace")
    header = reader.readline().lstrip("﻿")
    if not header.strip():
        raise JobFileError("Measure.csv is empty.")
    sep = "|" if header.count("|") > header.count(",") else ","
    columns = next(csv.reader(io.StringIO(header), delimiter=sep))
    at = next((columns.index(c) for c in YEAR_COLUMNS if c in columns), None)
    records, first = 0, None
    for row in csv.reader(reader, delimiter=sep):
        if not row or not any(cell.strip() for cell in row):
            continue
        records += 1
        if at is not None and at < len(row):
            y = _year(row[at])
            if y is not None and (first is None or y < first):
                first = y
    return records, first


def _sheet_key(name: str) -> str:
    return re.sub(r"[\s_]", "", name).lower()


_SHEET_KEYS = {_sheet_key(n): n for n in SHEETS}


def is_workbook(z: zipfile.ZipFile) -> bool:
    """An Excel workbook (an .xlsx is itself a zip), told by its contents, not its file name."""
    names = set(z.namelist())
    return "[Content_Types].xml" in names and "xl/workbook.xml" in names


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def inspect_workbook(path: Path) -> JobInfo:
    """A job workbook: its Measure sheet's records and earliest claim year, and which of the engine's sheets have data, read row by row."""
    from openpyxl import load_workbook

    fh = open(path, "rb")  # a file, not its path: openpyxl refuses a path by its extension, and an upload's temporary file has none
    try:
        wb = load_workbook(fh, read_only=True, data_only=True)
    except Exception:  # openpyxl raises several kinds for a file that is not a workbook
        fh.close()
        raise JobFileError("The job file is not a valid Excel workbook (.xlsx).") from None
    try:
        found = {}
        for name in wb.sheetnames:
            canon = _SHEET_KEYS.get(_sheet_key(name))
            if canon is None:
                continue
            if canon in found:
                raise JobFileError(f"The workbook has more than one {canon} sheet ({found[canon]!r} and {name!r}).")
            found[canon] = name
        if "Measure" not in found:
            raise JobFileError(
                "The workbook has no Measure sheet. Name the sheet of measure records Measure, as in the job workbook template."
            )
        counts, first = {}, None
        for canon, name in found.items():
            rows = wb[name].iter_rows(values_only=True)
            header = None
            for r in rows:
                if r and any(c is not None and str(c).strip() for c in r):
                    header = [_cell(c).strip() for c in r]
                    break
            n = 0
            at = next((header.index(c) for c in YEAR_COLUMNS if c in header), None) if header and canon == "Measure" else None
            for r in rows if header else ():
                if not r or not any(_cell(c) for c in r[: len(header)]):
                    continue
                n += 1
                if at is not None and at < len(r):
                    y = _year(_cell(r[at]))
                    if y is not None and (first is None or y < first):
                        first = y
            counts[canon] = n
    finally:
        wb.close()
        fh.close()
    sheets = tuple(c for c in SHEETS if counts.get(c))
    if "Measure" not in sheets:
        raise JobFileError(f"The workbook's {found['Measure']} sheet has no records.")
    return JobInfo(
        found["Measure"], found.get("ProgramCost") if "ProgramCost" in sheets else None, counts["Measure"], first, WORKBOOK, sheets
    )


def inspect(path: Path) -> JobInfo:
    """A job zip or a job workbook, told apart by its contents."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise JobFileError(
            "The job file is not a valid .zip or Excel workbook. Zip Measure.csv (and ProgramCost.csv) together, or use the job "
            "workbook (.xlsx), and upload that."
        ) from None
    if is_workbook(z):
        z.close()
        return inspect_workbook(path)
    with z:
        found = members(z)
        try:
            with z.open(found["measures"]) as f:
                records, first = scan_measures(f)
        except (zipfile.BadZipFile, EOFError, NotImplementedError, csv.Error) as e:
            raise JobFileError(f"Measure.csv could not be read: {e}") from None
    return JobInfo(found["measures"].filename, found["programcost"].filename if "programcost" in found else None, records, first)


def check_cet_outputs(path: Path) -> None:
    """A CET outputs file to compare a run against (the engine's RunConfig.cet_outputs): a CET run's *_outputs.csv, comma- or
    pipe-delimited, with its CET_ID column (CEInputID also accepted). Only the header is read here; the engine matches the rows."""
    with open(path, "rb") as f:
        head = f.read(65536)
    if not head.strip():
        raise JobFileError("The CET outputs file is empty.")
    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = head.decode("cp1252", errors="replace")
    first = text.splitlines()[0]
    names = {c.strip().strip('"').lower() for c in first.split("|" if first.count("|") > first.count(",") else ",")}
    if not names & {"cet_id", "ceinputid"}:
        raise JobFileError("The CET outputs file has no CET_ID column: upload the outputs CSV of the CET run to compare with.")

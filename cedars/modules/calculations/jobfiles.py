"""A job's upload: one .zip holding Measure.csv and, optionally, ProgramCost.csv, as CET and ReCET take a job (ReCET 0.1.35). The
platform reads it once, streaming, before the job is queued: the files it holds (the same rules as the engine's `web/jobzip.py`, so a
wrong zip is refused here and not after a wait in the queue), the number of measure records (the queue lane, the estimate, the record
cap) and the earliest claim year (the statewide allocation version a run naming none uses). The engine checks everything else."""

from __future__ import annotations

import codecs
import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

NAMES = {"measure": "measures", "programcost": "programcost"}
SUFFIXES = (".csv", ".txt")
SKIPPED_NAMES = {"thumbs.db", "desktop.ini"}
YEAR_COLUMNS = ("ClaimYearQuarter",)


class JobFileError(ValueError):
    """The upload cannot be used; the message says why, for the person who uploaded it."""


@dataclass
class JobInfo:
    measures: str  # the Measure file's name in the zip
    programcost: str | None
    records: int
    first_year: int | None


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


def inspect(path: Path) -> JobInfo:
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise JobFileError("The job file is not a valid .zip. Zip Measure.csv (and ProgramCost.csv) together and upload that.") from None
    with z:
        found = members(z)
        try:
            with z.open(found["measures"]) as f:
                records, first = scan_measures(f)
        except (zipfile.BadZipFile, EOFError, NotImplementedError, csv.Error) as e:
            raise JobFileError(f"Measure.csv could not be read: {e}") from None
    return JobInfo(found["measures"].filename, found["programcost"].filename if "programcost" in found else None, records, first)

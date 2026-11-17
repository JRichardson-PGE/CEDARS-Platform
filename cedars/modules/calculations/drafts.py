"""Edit inputs (small jobs; ReCET's Edit inputs tab): a small job's Measure and ProgramCost tables edited on the platform, row by
row, and checked cell by cell with the pinned engine's own editor code (`recet.editor`, imported unchanged: Jake 2026-10-30, the web app
installs ReCET). The checks are the engine preflight's rules, so what they flag is what a run would; the full input check still runs
when the job is run.

A draft holds at most the engine's MAX_EDIT_ROWS (500) measures: larger jobs are edited offline. End uses are offered and checked
against the catalog of the impact-profile library in force (the registered pack's catalog.parquet: the names with 8,760s for each IOU and
target sector, as ReCET's editor offers them from its own library); without a library pack they are free text and the run checks them.
"Check with the engine" queues a check-only run of the draft as it stands and marks the cells its input check names (ReCET's Validate
inputs). Every change is an audit event. Without the engine installed the pages say so."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.db import transaction
from django.db.models import Q

from cedars.core import storage
from cedars.services import audit
from cedars.services.accounts import permissions as perms
from cedars.services.accounts.service import AccessDenied
from cedars.services.refdata import profiles as packed_profiles
from cedars.services.refdata import service as refdata
from cedars.services.refdata.models import PackVersion

from . import jobfiles, service
from .models import InputDraft, Run

TABLES = ("Measure", "ProgramCost")
FIELD = {"Measure": "measures", "ProgramCost": "programcost"}


class NoEngine(Exception):
    """The ReCET engine is not installed on this server."""


def engine():
    """recet.editor, or None when the engine is not installed."""
    try:
        from recet import editor
    except ImportError:
        return None
    return editor


def _editor():
    ed = engine()
    if ed is None:
        raise NoEngine("Edit inputs needs the ReCET engine package installed on this server (engine/README.md).")
    return ed


def max_rows() -> int:
    return _editor().MAX_EDIT_ROWS


# --- who sees and changes which


def visible(user):
    """The drafts a user may open: their own, and those of organizations where they may start runs."""
    orgs = [o for o in service.readable_organisations(user) if service.can_submit(user, o)]
    return InputDraft.objects.filter(Q(organisation__isnull=True, owner=user) | Q(organisation__in=orgs)).select_related(
        "organisation", "owner"
    )


def can_change(user, draft: InputDraft) -> bool:
    return draft.owner_id == user.pk if draft.organisation is None else service.can_submit(user, draft.organisation)


def _require(user, draft: InputDraft):
    if not can_change(user, draft):
        raise AccessDenied("You cannot change this draft.")


# --- tables


def _pack(df) -> dict:
    return {"columns": [str(c) for c in df.columns], "rows": df.astype(str).values.tolist()}


def frame(draft: InputDraft, table: str):
    """One of the draft's tables as the engine's editor keeps it: all text, every CET column in CET's order, extras after."""
    import pandas as pd

    ed = _editor()
    data = getattr(draft, FIELD[table]) or {}
    df = pd.DataFrame(data.get("rows", []), columns=data.get("columns") or None, dtype=str)
    return ed.conform(df, ed.MEASURE_COLUMNS if table == "Measure" else ed.PROGRAMCOST_COLUMNS)


def frames(draft: InputDraft):
    return frame(draft, "Measure"), frame(draft, "ProgramCost")


def _create(actor, organisation, name: str, source: str, job, *, source_run=None) -> InputDraft:
    if not service.can_submit(actor, organisation):
        raise AccessDenied(f"You need the {perms.label('calculations.submit')} permission in {organisation.code} to edit its jobs.")
    if not name.strip():
        raise AccessDenied("Give the draft a name.")
    draft = InputDraft.objects.create(
        organisation=organisation,
        owner=actor,
        name=name.strip()[:200],
        source=source[:300],
        source_run=source_run,
        measures=_pack(job.measures),
        programcost=_pack(job.programcost),
    )
    audit.record(
        "calculation_draft.created",
        obj=draft,
        organisation=organisation,
        after={"source": draft.source, "measures": draft.measure_count, "program_costs": len(draft.programcost["rows"])},
        actor=actor,
    )
    return draft


def _load(folder: Path, measures: Path, programcost: Path | None):
    ed = _editor()
    try:
        return ed.load_tables(measures=str(measures), programcost=str(programcost) if programcost else None)
    except ed.EditorError as e:
        raise AccessDenied(_editor_message(ed, e)) from None


def _editor_message(ed, e) -> str:
    msg = str(e)
    if "on the Job tab" in msg or "offline (Excel)" in msg:  # the desktop window's wording
        msg = (
            msg.split(";")[0]
            + f"; Edit inputs takes jobs of up to {ed.MAX_EDIT_ROWS:,} measures. Edit larger jobs offline and upload them."
        )
    return msg[:1].upper() + msg[1:]


def _load_file(path: Path, folder: Path):
    """A job zip's or a job workbook's Measure and ProgramCost tables (a workbook's other sheets are not part of a draft)."""
    try:
        with zipfile.ZipFile(path) as z:
            workbook = jobfiles.is_workbook(z)
    except (zipfile.BadZipFile, OSError):
        workbook = False
    if not workbook:
        m, pc = _extract(path, folder)
        return _load(folder, m, pc)
    try:
        jobfiles.inspect_workbook(path)  # the platform's own messages for a workbook with no Measure sheet or no records
    except jobfiles.JobFileError as e:
        raise AccessDenied(str(e)) from None
    book = folder / "job.xlsx"  # the engine's editor reads a workbook by its path, which must end .xlsx
    if path != book:
        shutil.copyfile(path, book)
    ed = _editor()
    try:
        return ed.load_tables(workbook=str(book))
    except ed.EditorError as e:
        raise AccessDenied(_editor_message(ed, e)) from None
    except ValueError as e:  # the engine's WorkbookError
        raise AccessDenied(str(e)[:1].upper() + str(e)[1:]) from None


def _extract(zip_path: Path, folder: Path) -> tuple[Path, Path | None]:
    try:
        z = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise AccessDenied(
            "The job file is not a valid .zip or Excel workbook. Zip Measure.csv (and ProgramCost.csv) together, or use the job "
            "workbook (.xlsx), and upload that."
        ) from None
    with z:
        try:
            found = jobfiles.members(z)
        except jobfiles.JobFileError as e:
            raise AccessDenied(str(e)) from None
        out = {}
        for key, info in found.items():
            if key not in ("measures", "programcost"):  # ReCET's optional input files are not part of a draft (its tables only)
                continue
            dest = folder / f"{key}.csv"
            with z.open(info) as src, open(dest, "wb") as f:
                shutil.copyfileobj(src, f)
            out[key] = dest
    return out["measures"], out.get("programcost")


def create_from_zip(actor, organisation, name: str, zip_path: Path, *, label: str = "") -> InputDraft:
    """A draft from a job zip (Measure.csv and, optionally, ProgramCost.csv) or a job workbook (its Measure and ProgramCost sheets),
    as New run takes a job."""
    with tempfile.TemporaryDirectory() as tmp:
        job = _load_file(Path(zip_path), Path(tmp))
    return _create(actor, organisation, name, f"Uploaded {label}".strip() if label else "Uploaded job", job)


def create_from_upload(actor, organisation, name: str, upload) -> InputDraft:
    path = getattr(upload, "temporary_file_path", None)
    if path is not None:
        return create_from_zip(actor, organisation, name, Path(path()), label=upload.name)
    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        for chunk in upload.chunks():
            tmp.write(chunk)
        tmp.flush()
        return create_from_zip(actor, organisation, name, Path(tmp.name), label=upload.name)


def create_blank(actor, organisation, name: str) -> InputDraft:
    """A new job holding the engine's example measure and program-cost row."""
    return _create(actor, organisation, name, "New job (the template's example rows)", _editor().new_job())


def create_from_run(actor, run: Run) -> InputDraft:
    """A draft from a run's own inputs (Edit inputs and rerun), for the run's organization."""
    if not service.can_read(actor, run):
        raise AccessDenied("You cannot see this run.")
    stored = service.job_input(run)
    if not stored:
        raise AccessDenied("This run has no job file to edit.")
    if run.measure_count and run.measure_count > max_rows():
        raise AccessDenied(f"This run has {run.measure_count:,} measures; Edit inputs takes jobs of up to {max_rows():,}.")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / stored["key"].rsplit("/", 1)[-1]  # job.zip or job.xlsx
        src = storage.store("uploads").open(stored["key"])
        try:
            with open(path, "wb") as f:
                shutil.copyfileobj(src, f)
        finally:
            src.close()
        job = _load_file(path, Path(tmp))
    return _create(actor, run.organisation, f"{run.name} (edited)", f"Inputs of run {run.name}", job, source_run=run)


# --- changes


def _save_table(draft: InputDraft, table: str, df) -> None:
    setattr(draft, FIELD[table], _pack(df))
    draft.save(update_fields=[FIELD[table], "updated_at"])


def _event(actor, draft, action, **kw):
    audit.record(action, obj=draft, organisation=draft.organisation, actor=actor, **kw)


def _row_index(df, index: int) -> int:
    if not 0 <= index < len(df):
        raise AccessDenied("That row is not in the table.")
    return index


@transaction.atomic
def set_row(actor, draft: InputDraft, table: str, index: int, values: dict) -> dict:
    """Change one row's cells (only the columns given). Returns {column: (before, after)} of what changed."""
    _require(actor, draft)
    df = frame(draft, table)
    i = _row_index(df, index)
    changed = {}
    for col, v in values.items():
        if col not in df.columns:
            continue
        v = "" if v is None else str(v).strip()
        if "|" in v:
            raise AccessDenied(f"{col}: CET files are '|'-delimited, so a value may not contain '|'.")
        before = df.at[i, col]
        if before != v:
            df.at[i, col] = v
            changed[col] = (before, v)
    if changed:
        _save_table(draft, table, df)
        _event(
            actor,
            draft,
            "calculation_draft.row_changed",
            before={"table": table, "row": i + 1, **{c: b for c, (b, _a) in changed.items()}},
            after={"table": table, "row": i + 1, **{c: a for c, (_b, a) in changed.items()}},
        )
    return changed


@transaction.atomic
def add_row(actor, draft: InputDraft, table: str, like: int | None = None, *, copy: bool = False) -> int:
    """Add a row at the end: a new measure with the engine's safe defaults (program, IOU, zone, sectors and claim quarter from row
    `like`), a copy of row `like` under a new CEInputID, or a blank program-cost row. Returns the new row's index."""
    import pandas as pd

    _require(actor, draft)
    ed = _editor()
    df = frame(draft, table)
    if table == "Measure" and len(df) >= ed.MAX_EDIT_ROWS:
        raise AccessDenied(f"Edit inputs takes jobs of up to {ed.MAX_EDIT_ROWS:,} measures.")
    base = df.iloc[_row_index(df, like)].to_dict() if like is not None else None
    if table == "Measure":
        ids = df["CEInputID"].tolist()
        row = dict(base, CEInputID=ed.unique_id(base["CEInputID"], ids)) if copy and base else ed.new_measure_row(base, ids)
    else:
        row = dict(base) if copy and base else {c: "" for c in df.columns}
    df = pd.concat([df, pd.DataFrame([row], columns=df.columns).fillna("")], ignore_index=True)
    _save_table(draft, table, df)
    _event(actor, draft, "calculation_draft.row_added", after={"table": table, "row": len(df), "copy_of": like + 1 if copy else None})
    return len(df) - 1


# --- the spreadsheet grid (edit_grid.js, as ReCET's own Edit inputs page): both tables saved whole on every change

MAX_PROGRAMCOST_ROWS = 5000  # as ReCET's own editor page (recet/web/edit_api.py)
MAX_CELL_CHARS = 2000
MAX_COLUMNS = 300


def revision(draft: InputDraft) -> str:
    """The tables' version a grid edits: the SHA-256 of both tables, so a save from a page showing an older version is refused rather
    than overwriting newer changes. The same hash an engine check is held to (content_hash)."""
    return content_hash(draft)


class Conflict(Exception):
    """The draft changed since the page loaded it (another window or person)."""


def _grid_table(table: str, data) -> object:
    """One table as a save sends it ({columns, rows}), checked for shape, size and CET's delimiter, conformed as the engine's editor
    keeps it. Content is the cell checks' job."""
    import pandas as pd

    ed = _editor()
    if not isinstance(data, dict):
        raise AccessDenied(f"{table}: columns and rows are required.")
    cols, rows = data.get("columns"), data.get("rows")
    if not isinstance(cols, list) or not isinstance(rows, list) or not 0 < len(cols) <= MAX_COLUMNS:
        raise AccessDenied(f"{table}: columns and rows are required.")
    if any(not isinstance(c, str) or not c.strip() or len(c) > 100 for c in cols) or len(set(cols)) != len(cols):
        raise AccessDenied(f"{table}: the column names are not usable.")
    limit = ed.MAX_EDIT_ROWS if table == "Measure" else MAX_PROGRAMCOST_ROWS
    if len(rows) > limit:
        raise AccessDenied(f"Edit inputs takes jobs of up to {limit:,} {table} rows. Edit larger jobs offline and upload them.")
    clean = []
    for r in rows:
        if not isinstance(r, list) or len(r) != len(cols):
            raise AccessDenied(f"{table}: every row needs one value per column.")
        vals = ["" if v is None else str(v).strip() for v in r]
        for c, v in zip(cols, vals, strict=True):
            if len(v) > MAX_CELL_CHARS:
                raise AccessDenied(f"{table}, {c}: a value is longer than {MAX_CELL_CHARS:,} characters.")
            if "|" in v:
                raise AccessDenied(f"{c}: CET files are '|'-delimited, so a value may not contain '|'.")
        clean.append(vals)
    return ed.conform(pd.DataFrame(clean, columns=cols, dtype=str), ed.MEASURE_COLUMNS if table == "Measure" else ed.PROGRAMCOST_COLUMNS)


@transaction.atomic
def save_tables(actor, draft: InputDraft, tables: dict, rev: str | None) -> str:
    """Save both tables as the grid holds them (rows added, copied, deleted, pasted or edited in any number). Refuses a save made on
    another version of the tables (Conflict). One audit event says how many rows and cells changed. Returns the new revision."""
    _require(actor, draft)
    draft = InputDraft.objects.select_for_update().get(pk=draft.pk)
    if rev is not None and rev != revision(draft):
        raise Conflict("This draft was changed in another window or by someone else. Reload the page to see the latest version.")
    before = frames(draft)
    after = [_grid_table(t, (tables or {}).get(t)) for t in TABLES]
    summary = {}
    for t, old, new in zip(TABLES, before, after, strict=True):
        cells = 0
        for i in range(min(len(old), len(new))):
            a, b = old.iloc[i], new.iloc[i]
            cells += sum(1 for c in new.columns if (a.get(c, "") if c in old.columns else "") != b[c])
        summary[t] = {"rows_before": len(old), "rows_after": len(new), "cells_changed": cells}
    if all(s["rows_before"] == s["rows_after"] and not s["cells_changed"] for s in summary.values()) and all(
        list(o.columns) == list(n.columns) for o, n in zip(before, after, strict=True)
    ):
        return revision(draft)
    for t, df in zip(TABLES, after, strict=True):
        setattr(draft, FIELD[t], _pack(df))
    draft.save(update_fields=["measures", "programcost", "updated_at"])
    _event(actor, draft, "calculation_draft.tables_saved", before={t: s["rows_before"] for t, s in summary.items()}, after=summary)
    return revision(draft)


def column_choices(src, measures) -> dict[str, dict[str, list[str]]]:
    """The grid's dropdown values per table and column for the claim years in the Measure table (the engine's ChoiceSource), as
    ReCET's own editor page sends them."""
    ed = _editor()
    years = sorted({y for y in (ed.claim_year(v) for v in measures.get("ClaimYearQuarter", [])) if y}) or None
    out: dict[str, dict[str, list[str]]] = {}
    for t, cols in (("Measure", ed.MEASURE_COLUMNS), ("ProgramCost", ed.PROGRAMCOST_COLUMNS)):
        out[t] = {}
        for col in cols:
            if t == "Measure" and col in ed.END_USE_COLUMNS:
                continue
            vals = src.column_choices(t, col, years)
            if vals:
                out[t][col] = ["", *vals]
    return out


@dataclass
class EtrmFill:
    index: int  # the new row's, 0-based
    filled: dict  # {column: value} taken from the permutation
    left: list  # [(column, eTRM column, why it was not filled)]


def units_value(text: str) -> str:
    """The number of units as entered, checked: a number above zero."""
    t = (text or "").strip().replace(",", "")
    try:
        ok = float(t) > 0
    except ValueError:
        ok = False
    if not ok:
        raise AccessDenied("Enter the number of units: a number above zero, in the package's normalized unit.")
    return t


@transaction.atomic
def add_from_etrm(actor, draft: InputDraft, permutation, units: str) -> EtrmFill:
    """Add a measure row filled from an eTRM permutation: the engine's new-row defaults with row 1's program, IOU, zone,
    sectors and claim quarter, then every field the permutation gives (refdata.etrm.measure_row) and the number of units. Every field
    stays editable; the cell checks run on it as on any row."""
    import pandas as pd

    from cedars.services.refdata import etrm

    _require(actor, draft)
    ed = _editor()
    df = frame(draft, "Measure")
    if len(df) >= ed.MAX_EDIT_ROWS:
        raise AccessDenied(f"Edit inputs takes jobs of up to {ed.MAX_EDIT_ROWS:,} measures.")
    n = units_value(units)
    fill, left = etrm.measure_row(permutation, df.columns)
    for col, v in list(fill.items()):
        if "|" in v:  # CET files are '|'-delimited
            del fill[col]
            left.append((col, "", "the eTRM value holds '|', which a CET file cannot carry"))
    row = ed.new_measure_row(df.iloc[0].to_dict() if len(df) else None, df["CEInputID"].tolist())
    row.update(fill)
    row["NumUnits"] = n
    df = pd.concat([df, pd.DataFrame([row], columns=df.columns).fillna("")], ignore_index=True)
    _save_table(draft, "Measure", df)
    _event(
        actor,
        draft,
        "calculation_draft.row_added",
        after={
            "table": "Measure",
            "row": len(df),
            "etrm": {"pack": permutation.pack_id, "measure_detail_id": permutation.measure_detail_id, "units": n},
            "filled": sorted(fill),
        },
    )
    return EtrmFill(len(df) - 1, fill, left)


@transaction.atomic
def delete_row(actor, draft: InputDraft, table: str, index: int) -> None:
    _require(actor, draft)
    df = frame(draft, table)
    i = _row_index(df, index)
    if table == "Measure" and len(df) == 1:
        raise AccessDenied("A job needs at least one measure.")
    before = df.iloc[i].to_dict()
    _save_table(draft, table, df.drop(index=i).reset_index(drop=True))
    _event(actor, draft, "calculation_draft.row_deleted", before={"table": table, "row": i + 1, **before})


@transaction.atomic
def rename(actor, draft: InputDraft, name: str) -> InputDraft:
    _require(actor, draft)
    if not name.strip():
        raise AccessDenied("Give the draft a name.")
    before = draft.name
    draft.name = name.strip()[:200]
    draft.save(update_fields=["name", "updated_at"])
    _event(actor, draft, "calculation_draft.renamed", before={"name": before}, after={"name": draft.name})
    return draft


@transaction.atomic
def delete(actor, draft: InputDraft) -> None:
    _require(actor, draft)
    _event(actor, draft, "calculation_draft.deleted", before={"name": draft.name, "measures": draft.measure_count})
    draft.delete()


# --- checks and files


@dataclass
class Issue:
    table: str
    row: int  # 0-based
    column: str
    severity: str  # error | warning | info
    message: str


# ReCET's editor's words for an end use the library does not hold (recet.editor.ChoiceSource.electric_profile_status)
NO_PROFILE = "no 8,760 profile for this end use, IOU and target sector: the measure's electric savings will NOT be priced"
LOOSE_PROFILE = "INFO:matched the library on end use and IOU only (no profile for this target sector)"


def library_pack() -> PackVersion | None:
    """The impact-profile library in force (the newest published pack), whose catalog the end-use lists and checks read."""
    return refdata.in_force(PackVersion.Kind.IMPACT_PROFILES)


@lru_cache(maxsize=4)
def _library(pack_pk: int, fingerprint: str) -> tuple[dict, dict, frozenset, frozenset]:
    """A library pack's end uses with 8,760s ({(IOU, SECTOR): [names]}, {IOU: [names]}) and its exact and loose keys, read once per
    pack (a pack's files never change)."""
    pack = PackVersion.objects.get(pk=pack_pk)
    ed = _editor()
    by_sector: dict[tuple[str, str], set[str]] = {}
    by_iou: dict[str, set[str]] = {}
    for e in packed_profiles.catalog(pack):
        if e.shape_id is None or not e.name:  # a name with no hourly data is not one a run can price
            continue
        iou, sector = e.territory.strip().upper(), e.target_sector.strip().upper()
        by_sector.setdefault((iou, sector), set()).add(e.name)
        by_iou.setdefault(iou, set()).add(e.name)
    exact = frozenset((n, iou, sec) for (iou, sec), names in by_sector.items() for n in names)
    loose = frozenset((n, iou) for iou, names in by_iou.items() for n in names)
    order = ed._natural  # the editor's own sort, so the lists read as ReCET's do
    return (
        {k: sorted(v, key=order) for k, v in by_sector.items()},
        {k: sorted(v, key=order) for k, v in by_iou.items()},
        exact,
        loose,
    )


def _with_library(ed, src, pack: PackVersion):
    """`src` (a recet.editor.ChoiceSource) offering and checking end uses from a library pack's catalog, as the engine's editor does
    from its own library: the same per-IOU-and-sector lists, the same exact (end use, IOU, sector) then loose (end use, IOU) match."""
    by_sector, by_iou, exact, loose = _library(pack.pk, pack.fingerprint or "")
    src._by_iou_sector, src._by_iou = by_sector, by_iou
    src.catalog_note = f"{len({n for names in by_iou.values() for n in names}):,} end uses with 8,760 profiles in {pack}"
    src.library = pack

    def electric_profile_status(end_use: str, iou: str, sector: str) -> str | None:
        name, iou = str(end_use), str(iou).strip().upper()
        if (name, iou, str(sector).strip().upper()) in exact:
            return None
        return LOOSE_PROFILE if (name, iou) in loose else NO_PROFILE

    src.electric_profile_status = electric_profile_status
    return src


def choices():
    """The engine's dropdown values: the value lists in force (codes active for the claim year, as CPUC administrators keep them:
    dictionary.value_lists), the IOUs it prices, and the end uses of the impact-profile library in force (none without a library pack:
    free text, not checked)."""
    from cedars.services.dictionary import value_lists

    ed = _editor()
    value_lists.apply_to_engine()
    src = ed.ChoiceSource()
    src.library = None
    pack = library_pack()
    if pack is not None:
        try:
            _with_library(ed, src, pack)
        except Exception:  # noqa: BLE001 - an unreadable library must not stop the person editing (the run checks the end uses)
            src.library = None
    return src


class ChecksFailed(Exception):
    """The engine's cell checks raised on these tables; the message says what it said."""


def issues(draft: InputDraft, src=None) -> list[Issue]:
    """Every problem cell, by the engine's own per-cell checks (recet.editor.check_tables: the preflight's rules). Raises ChecksFailed
    if the checks themselves fail on the tables, so the pages can say so rather than fail."""
    ed = _editor()
    m, pc = frames(draft)
    try:
        found = ed.check_tables(m, pc, src or choices())
    except Exception as e:  # noqa: BLE001 - an engine error on odd input must not stop the person editing
        raise ChecksFailed(f"{type(e).__name__}: {e}") from e
    return [Issue(i.table, i.row, i.column, i.severity, i.message) for i in found]


def job_zip_bytes(draft: InputDraft) -> bytes:
    """The draft as a job zip: Measure.csv and ProgramCost.csv in CET's pipe-delimited format under CET's own column names, as the
    engine's editor writes them (entirely blank rows dropped)."""
    ed = _editor()
    m, pc = frames(draft)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            files = ed.write_cet_files(m, pc, tmp)
        except ed.EditorError as e:
            raise AccessDenied(str(e)[:1].upper() + str(e)[1:]) from None
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files.values():
                if p is not None:
                    z.write(p, p.name)
    return buf.getvalue()


def workbook_bytes(draft: InputDraft) -> bytes:
    """The draft as ReCET's job workbook: a Measure and a ProgramCost sheet under ReCET's column names, numbers as numbers, as the
    engine's editor writes it (recet.editor.write_workbook), made the platform's accessible way (cedars.core.workbooks: properties, a
    header row marked as a table header). New run and Edit inputs read it back as they read any job workbook."""
    from cedars.core.workbooks import Book

    ed = _editor()
    numeric = set(ed.MEASURE_NUMERIC) | set(ed.PROGRAMCOST_NUMERIC) | {"PrgYear"}
    book = Book(f"{draft.name}: job inputs", subject="ReCET job workbook (Measure and ProgramCost)")
    for name, df, columns in zip(TABLES, frames(draft), (ed.MEASURE_COLUMNS, ed.PROGRAMCOST_COLUMNS), strict=True):
        df = ed._clean(ed.conform(df, columns))
        cols = list(df.columns)
        book.sheet(name, cols, ([ed._cell_value(c, v, numeric) for c, v in zip(cols, r, strict=True)] for r in df.itertuples(index=False)))
    return book.save()


def start_run(actor, organisation, req: service.RunRequest, draft: InputDraft) -> Run:
    """Run the draft's tables as they stand (New run's "Run these inputs"): its job zip is the run's upload, so the run is the same record,
    spec and checks as one started from a file. The draft stays, to edit and run again."""
    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        tmp.write(job_zip_bytes(draft))
        tmp.flush()
        run = service.create_run(actor, organisation, req, Path(tmp.name), job_name=draft.name)
    audit.record(
        "calculation_draft.run", obj=draft, organisation=draft.organisation, after={"run": str(run.pk), "name": run.name}, actor=actor
    )
    return run


# --- check with the engine (ReCET's Validate inputs)


def content_hash(draft: InputDraft) -> str:
    """The SHA-256 of the draft's tables: a check's findings apply while the tables still have it."""
    data = json.dumps([draft.measures or {}, draft.programcost or {}], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def start_check(actor, draft: InputDraft, req: service.RunRequest) -> Run:
    """Queue a check-only run of the draft as it stands (the engine's whole input check, impact profiles and CEDARS filing rules
    included, with nothing priced) for the draft's organization, or the person's own for their own draft; its findings are marked on
    the cells once it ends (check_findings)."""
    _require(actor, draft)
    req.settings = {**(req.settings or {}), "preflight_only": True}
    run = start_run(actor, draft.organisation, req, draft)
    draft.check_run, draft.check_hash = run, content_hash(draft)
    draft.save(update_fields=["check_run", "check_hash"])  # not updated_at: checking is not a change
    return run


@dataclass
class Check:
    """The latest engine check of a draft, as the draft page shows it."""

    run: Run
    ended: bool
    current: bool  # the tables are still the ones it checked
    report: dict | None  # the engine's preflight.json, once written
    cells: list[Issue]  # its findings on the draft's cells (only while current)
    other: list[dict]  # its findings about the job as a whole, with no cells

    @property
    def stops(self) -> int:
        return int((self.report or {}).get("errors") or 0)


def _preflight(run: Run) -> dict | None:
    try:
        return json.loads(storage.store("results").get_bytes(run.storage_prefix + "out/preflight.json"))
    except (FileNotFoundError, KeyError, OSError, ValueError):
        return None


def check_findings(draft: InputDraft) -> Check | None:
    """The draft's latest engine check, its findings mapped back to the draft's rows: row r of the file the run read is the r-th row
    the job zip kept (recet.editor.kept_rows: entirely blank rows are dropped), and a column filed under CET's name is shown under the
    draft's (recet.fieldnames.recet_name)."""
    run = Run.objects.filter(pk=draft.check_run_id).first() if draft.check_run_id else None  # its status as it stands now
    if run is None:
        return None
    ended = run.status in Run.ENDED
    report = _preflight(run) if ended else None
    current = draft.check_hash == content_hash(draft)
    cells, other = [], []
    if report:
        from recet import fieldnames

        ed = _editor()
        kept = {t: ed.kept_rows(df) for t, df in zip(TABLES, frames(draft), strict=True)}
        columns = {t: set(df.columns) for t, df in zip(TABLES, frames(draft), strict=True)}
        for i in report.get("issues", []):
            sev = i.get("severity") if i.get("severity") in ("error", "warning", "info") else "info"
            found = False
            for table, row, column in i.get("cells") or []:
                table = "ProgramCost" if str(table).lower() == "programcost" else "Measure"
                rows = kept.get(table, [])
                if not (current and 0 <= int(row) < len(rows)):
                    continue
                col = column if column in columns[table] else fieldnames.recet_name(column)
                cells.append(Issue(table, rows[int(row)], col, sev, f"{i.get('message', '')} (the engine's check: {i.get('code', '')})"))
                found = True
            if not found:
                other.append(i)
    return Check(run=run, ended=ended, current=current, report=report, cells=cells, other=other)


def expired_drafts(cutoff):
    """Retention (record class scratch_run): drafts not changed since before the cutoff."""
    for draft in InputDraft.objects.filter(updated_at__lt=cutoff).select_related("organisation"):

        def remove(draft=draft):
            audit.record(
                "calculation_draft.expired",
                obj=draft,
                organisation=draft.organisation,
                before={"name": draft.name, "updated_at": draft.updated_at.isoformat()},
                actor=None,
            )
            draft.delete()

        yield f"calculation_draft:{draft.pk}", remove

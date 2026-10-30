"""Edit inputs (BR-06.15 small jobs; ReCET's Edit inputs tab): a small job's Measure and ProgramCost tables edited on the platform, row by
row, and checked cell by cell with the pinned engine's own editor code (`recet.editor`, imported unchanged: Jake 2026-10-30, the web app
installs ReCET). The checks are the engine preflight's rules, so what they flag is what a run would; the full input check still runs
when the job is run.

A draft holds at most the engine's MAX_EDIT_ROWS (500) measures: larger jobs are edited offline. The platform has no impact-profile
library on the web servers, so end-use names are not matched to profiles here (the run checks them). Every change is an audit event.
Without the engine installed the pages say so."""

from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.db.models import Q

from cedars.core import storage
from cedars.services import audit
from cedars.services.accounts.service import AccessDenied

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
    """The drafts a user may open: their own, and those of organisations where they may start runs."""
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
        raise AccessDenied(f"You need calculations.submit in {organisation.code} to edit its jobs.")
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
        msg = str(e)
        if "on the Job tab" in msg or "offline (Excel)" in msg:  # the desktop window's wording
            msg = (
                msg.split(";")[0]
                + f"; Edit inputs takes jobs of up to {ed.MAX_EDIT_ROWS:,} measures. Edit larger jobs offline and upload them."
            )
        raise AccessDenied(msg[:1].upper() + msg[1:]) from None


def _extract(zip_path: Path, folder: Path) -> tuple[Path, Path | None]:
    try:
        z = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise AccessDenied("The job file is not a valid .zip. Zip Measure.csv (and ProgramCost.csv) together and upload that.") from None
    with z:
        try:
            found = jobfiles.members(z)
        except jobfiles.JobFileError as e:
            raise AccessDenied(str(e)) from None
        out = {}
        for key, info in found.items():
            dest = folder / f"{key}.csv"
            with z.open(info) as src, open(dest, "wb") as f:
                shutil.copyfileobj(src, f)
            out[key] = dest
    return out["measures"], out.get("programcost")


def create_from_zip(actor, organisation, name: str, zip_path: Path, *, label: str = "") -> InputDraft:
    """A draft from a job zip (Measure.csv and, optionally, ProgramCost.csv), as New run takes a job."""
    with tempfile.TemporaryDirectory() as tmp:
        m, pc = _extract(Path(zip_path), Path(tmp))
        job = _load(Path(tmp), m, pc)
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
    """A draft from a run's own inputs (Edit inputs and rerun), for the run's organisation."""
    if not service.can_read(actor, run):
        raise AccessDenied("You cannot see this run.")
    if "job_zip" not in run.inputs:
        raise AccessDenied("This run has no job file to edit.")
    if run.measure_count and run.measure_count > max_rows():
        raise AccessDenied(f"This run has {run.measure_count:,} measures; Edit inputs takes jobs of up to {max_rows():,}.")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "job.zip"
        src = storage.store("uploads").open(run.inputs["job_zip"]["key"])
        try:
            with open(path, "wb") as f:
                shutil.copyfileobj(src, f)
        finally:
            src.close()
        m, pc = _extract(path, Path(tmp))
        job = _load(Path(tmp), m, pc)
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


def choices():
    """The engine's dropdown values: CET's value lists (codes active for the claim year), the IOUs it prices. No profile library."""
    return _editor().ChoiceSource()


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

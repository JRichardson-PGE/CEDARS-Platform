"""A legacy CEDARS budget filing corrected on the platform (submissions.request_legacy_correction): the approved draft starts as the
filing's job rebuilt from the imported records, so the program administrator corrects the records that change (the record editor, or
a partial correction) or uploads the whole filing again, like any budget filing version (each version is the whole filing).

The imported version holds the filing as legacy CEDARS published it (legacy.py): Measure.parquet (the Measure inputs) and
Outputs.parquet (CEDARS' CET outputs, among them each record's share of the ten weighted program cost columns), row for row. `start`
writes them as the draft's job.zip, laid out by the filing's specification version (the correction version of its first program year):

  - Measure: the columns the version has, as text;
  - ProgramCost: one row per program and program year, its costs the weighted program costs summed over the program's records of that
    year (a record's year is its ClaimYearQuarter's, else the download's Year). Pricing spreads each row again across the program's
    records, so a changed program cost row is spread again too.

The draft is then checked when it is first opened for correction, as a reopened version is (service.ensure_checked)."""

import csv
import hashlib
import io
from collections import defaultdict
from decimal import Decimal

from cedars.core import storage
from cedars.services.accounts.service import AccessDenied
from cedars.services.legacy import service as legacy
from cedars.services.specifications import service as specifications
from cedars.services.submissions.models import Deadline, SubmissionVersion

from . import jobfile, service
from .legacy import FILINGS
from .models import PROGRAM_COST_FIELDS

FILES = ("Measure.parquet", "Outputs.parquet")
COSTS = {f"Wtd{f}": f for f, _ in PROGRAM_COST_FIELDS}


def year(deadline: Deadline) -> int | None:
    """The year a legacy budget filing is corrected as: its first program year (an annual budget advice letter's year; 2026 for the
    2026-2027 true-up and mid-cycle letters of the 2024-2027 cycle). None: not a legacy filing."""
    cycle = deadline.cycle
    if cycle is None:
        return None
    for f in FILINGS:
        if f.kind == deadline.kind and f.first_year == cycle.first_year and f.first_year + f.years - 1 == cycle.last_year:
            return f.program_year
    return None


def _file(v: SubmissionVersion, name: str) -> dict | None:
    return next((f for f in v.files or [] if f.get("name") == name), None)


def _read(v: SubmissionVersion, name: str):
    import pyarrow as pa
    import pyarrow.parquet as pq

    return pq.read_table(pa.BufferReader(storage.store(legacy.AREA).get_bytes(_file(v, name)["key"])))


def _text(value) -> str:
    """A value read from the Parquet as the text an upload holds: blank for none, a number without exponent or a trailing .0."""
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        value = Decimal(repr(value))
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else format(value.normalize(), "f")
    return str(value).strip()


def _csv(columns: list[str], rows: list[dict]) -> bytes:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue().encode("utf-8")


def tables(imported: SubmissionVersion, deadline: Deadline) -> dict[str, bytes]:
    """{table: CSV bytes}: the imported filing as the job's Measure and ProgramCost (see the module's docstring)."""
    measure, outputs = (_read(imported, name) for name in FILES)
    lay = specifications.layout_for_filing(deadline, service.MODULE)
    columns = [c for c in lay.table("Measure").upload_columns() if c in measure.column_names]
    data = {c: measure.column(c).to_pylist() for c in columns}
    rows = [{c: _text(data[c][i]) for c in columns} for i in range(measure.num_rows)]
    quarters = measure.column("ClaimYearQuarter").to_pylist() if "ClaimYearQuarter" in measure.column_names else []
    used = outputs.column("Used_Year").to_pylist() if "Used_Year" in outputs.column_names else []
    programs = [(p or "").strip() for p in outputs.column("PrgID").to_pylist()]
    present = [c for c in COSTS if c in outputs.column_names]
    values = {c: outputs.column(c).to_pylist() for c in present}
    sums: dict[tuple[str, int], dict[str, Decimal]] = {}
    for i, prg in enumerate(programs):
        y = jobfile.year_of(_text(quarters[i]) if quarters else "") or jobfile.year_of(_text(used[i]) if used else "")
        if not prg or y is None:
            continue
        row = sums.setdefault((prg, y), defaultdict(Decimal))
        for c in present:
            v = values[c][i]
            if v is not None and v == v:
                row[COSTS[c]] += Decimal(repr(float(v)))
    pa_code = imported.submission.organisation.code
    cost_rows = []
    for prg, y in sorted(sums):
        r = {"PrgID": prg, "PrgYear": str(y), "PA": pa_code, **{f: _text(sums[(prg, y)].get(f, Decimal(0))) for f in COSTS.values()}}
        r["OnBillFinancing"] = "0"
        cost_rows.append(r)
    filled = ("PrgID", "PrgYear", "PA", "OnBillFinancing", *COSTS.values())
    cost_columns = [c for c in lay.table("ProgramCost").upload_columns() if c in filled]
    return {
        "Measure": _csv(columns, rows),
        "ProgramCost": _csv(cost_columns, [{c: r.get(c, "") for c in cost_columns} for r in cost_rows]),
    }


def start(new: SubmissionVersion, old: SubmissionVersion) -> list[dict]:
    """The submissions service's legacy correction starter: the draft's job, rebuilt from the imported records and stored under the
    draft's folder; refused (and so the request left undecided) when the imported version's tables cannot be read back."""
    missing = [name for name in FILES if _file(old, name) is None]
    if missing:
        raise AccessDenied(f"The imported filing has no {', '.join(missing)}, so it cannot be corrected on the platform.")
    job = jobfile.job_zip(tables(old, new.submission.deadline))
    sha = hashlib.sha256(job).hexdigest()
    key = f"{service._prefix(new)}{sha[:12]}/job.zip"
    storage.store("uploads").put_bytes(key, job)
    return [{"name": "job.zip", "uploaded_as": "job.zip", "rebuilt_from_import": True, "key": key, "sha256": sha, "bytes": len(job)}]

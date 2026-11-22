"""A legacy CEDARS claim year corrected on the platform (submissions.request_legacy_correction): the approved draft is the year's annual
claim filed again, compiled over the imported records, so the program administrator files only what changes.

The imported version holds the year as legacy CEDARS published it (legacy.py): Measure.parquet (the Measure inputs), Claim.parquet (the
Claim table's fields) and Outputs.parquet (CEDARS' CET outputs, among them each record's share of the ten weighted program cost
columns), row for row. `base_tables` reads them back as the claim tables a correction is compiled over, laid out by the filing's
specification version (the year's legacy correction version):

  - Claim and Measure: the columns the version has, as text; a ClaimID repeated in the year gets "#2", "#3" ... in file order, as its
    claim record does, so each record keeps its own row;
  - ProgramCost: one row per program, its costs the weighted program costs summed over the program's records (legacy years carry no
    program cost table). A program cost row the program administrator files replaces the program's row, and pricing spreads it again
    across the program's records;
  - Withdrawal: none (a legacy year withdrew nothing the platform can see).

A correction upload goes over these by key like any confirmation's upload over the one before it (service.compile_tables), and is
checked like one (the module's checks and the rule set on its own records)."""

from collections import defaultdict
from decimal import Decimal

from cedars.core import storage
from cedars.services.accounts.service import AccessDenied
from cedars.services.legacy import service as legacy
from cedars.services.submissions.models import SubmissionVersion

from . import spec, upload
from .models import PROGRAM_COST_FIELDS

FILES = ("Measure.parquet", "Claim.parquet", "Outputs.parquet")
COSTS = {f"Wtd{f}": f for f, _ in PROGRAM_COST_FIELDS}


def year(deadline) -> int:
    """The legacy year a deadline's filing is for: an imported claim year is filed on its annual claim, whose period is the year."""
    return deadline.period.year


def start(new: SubmissionVersion, old: SubmissionVersion) -> None:
    """The submissions service's legacy correction starter: refuse (and so leave the request undecided) an imported version whose
    tables cannot be read back, before the draft is handed to the filer."""
    missing = [name for name in FILES if _file(old, name) is None]
    if missing:
        raise AccessDenied(f"The imported claim year has no {', '.join(missing)}, so it cannot be corrected on the platform.")


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


def base_tables(imported: SubmissionVersion, lay: spec.Layout) -> dict[str, upload.Table]:
    """The imported year as the claim tables a correction is compiled over (see the module's docstring)."""
    measure, claim, outputs = (_read(imported, name) for name in FILES)
    ids = [(c or "").strip() for c in claim.column("ClaimID").to_pylist()]
    counts: dict[str, int] = defaultdict(int)
    for c in ids:
        counts[c.lower()] += 1
    seen: dict[str, int] = defaultdict(int)
    keyed = []
    for c in ids:
        if c and counts[c.lower()] > 1:
            seen[c.lower()] += 1
            keyed.append(c if seen[c.lower()] == 1 else f"{c}#{seen[c.lower()]}")
        else:
            keyed.append(c)
    out = {}
    for name, source in (("Claim", claim), ("Measure", measure)):
        columns = [c for c in lay.columns(name) if c in source.column_names]
        data = {c: source.column(c).to_pylist() for c in columns if c != "ClaimID"}
        rows = []
        for i, cid in enumerate(keyed):
            if not cid:
                continue
            rows.append({"ClaimID": cid, **{c: _text(v[i]) for c, v in data.items()}})
        out[name] = upload.Table(name, ["ClaimID", *[c for c in columns if c != "ClaimID"]], rows)
    out["ProgramCost"] = _program_costs(imported, outputs, lay)
    return {name: t for name, t in out.items() if t.rows}


def _program_costs(imported: SubmissionVersion, outputs, lay: spec.Layout) -> upload.Table:
    """One ProgramCost row per program: the weighted program costs summed over its records (exactly, as decimals)."""
    year_ = year(imported.submission.deadline)
    columns = [c for c in COSTS if c in outputs.column_names]
    programs = [(p or "").strip() for p in outputs.column("PrgID").to_pylist()]
    values = {c: outputs.column(c).to_pylist() for c in columns}
    sums: dict[str, dict[str, Decimal]] = {}
    for i, prg in enumerate(programs):
        if not prg:
            continue
        row = sums.setdefault(prg, defaultdict(Decimal))
        for c in columns:
            v = values[c][i]
            if v is not None and v == v:
                row[COSTS[c]] += Decimal(repr(float(v)))
    pa_code = imported.submission.organisation.code
    rows = []
    for prg in sorted(sums):
        row = {"PrgID": prg, "PrgYear": str(year_), "ClaimYearQuarter": f"{year_}Q4", "PA": pa_code}
        for f in COSTS.values():
            row[f] = _text(sums[prg].get(f, Decimal(0)))
        if "OnBillFinancing" in lay.columns("ProgramCost"):
            row["OnBillFinancing"] = "0"
        rows.append(row)
    wanted = [c for c in lay.columns("ProgramCost") if rows and c in rows[0]]
    return upload.Table("ProgramCost", wanted, [{c: r[c] for c in wanted} for r in rows])

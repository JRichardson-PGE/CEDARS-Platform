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
from .models import PROGRAM_COST_FIELDS, LegacyReproduction

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


def _keyed(ids) -> list[str]:
    """The ClaimIDs as the platform keys them: a repeated one gets "#2", "#3" ... in file order, as its claim record does."""
    ids = [(c or "").strip() for c in ids]
    counts: dict[str, int] = defaultdict(int)
    for c in ids:
        counts[c.lower()] += 1
    seen: dict[str, int] = defaultdict(int)
    out = []
    for c in ids:
        if c and counts[c.lower()] > 1:
            seen[c.lower()] += 1
            out.append(c if seen[c.lower()] == 1 else f"{c}#{seen[c.lower()]}")
        else:
            out.append(c)
    return out


def base_tables(imported: SubmissionVersion, lay: spec.Layout) -> dict[str, upload.Table]:
    """The imported year as the claim tables a correction is compiled over (see the module's docstring)."""
    measure, claim, outputs = (_read(imported, name) for name in FILES)
    keyed = _keyed(claim.column("ClaimID").to_pylist())
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


# --- reproducing the import on the replica

# The outputs compared, record by record: what legacy CEDARS published against what the replica prices.
COMPARED = (
    "ElecBen", "GasBen", "OtherBen", "TRCBen", "PACBen", "TRCCost", "PACCost", "TotalSystemBenefit",
    "FirstYearNetKWh", "FirstYearNetKW", "FirstYearNetThm", "LifecycleNetKWh", "LifecycleNetThm",
)  # fmt: skip
TOLERANCE = (1e-6, 0.01)  # relative, absolute: CEDARS' published outputs are rounded
EXAMPLES = 200
SHOWN = 20  # the differing values the correction's page lists; the rest are in the comparison kept on the reproduction


def reproduction(imported: SubmissionVersion, calculation: str) -> LegacyReproduction | None:
    return LegacyReproduction.objects.filter(imported=imported, calculation=calculation).first()


def reproduce(actor, correction: SubmissionVersion, imported: SubmissionVersion) -> LegacyReproduction | None:
    """Start, once per imported version and replica, the run that prices the imported records alone on the replica the correction is
    priced with, for the filing's organization. None when the replica has no release to run on (the correction is still priced; its
    page says the reproduction could not be run). Called when a correction is priced (service.price)."""
    from cedars.services.specifications import service as specifications
    from cedars.services.submissions import service as submissions

    from . import service

    spec_v = specifications.of_filing(correction.submission.deadline)
    calculation = specifications.calculation_of(spec_v)
    if calculation == "recet":
        return None
    done = reproduction(imported, calculation)
    if done is not None:
        return done
    release = specifications.engine_release_of(spec_v) or next((o["release"] for o in submissions.restate_options() if o["default"]), "")
    if not release:
        return None
    lay = spec.for_filing(correction.submission.deadline)
    tables = base_tables(imported, lay)
    job = service.job_zip(tables, None, lay)
    import tempfile
    from pathlib import Path

    org = imported.submission.organisation
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "job.zip"
        path.write_bytes(job)
        try:
            run = submissions.reproduce_legacy(
                actor,
                imported,
                path,
                organisation=org,
                name=f"{org.code} {year(imported.submission.deadline)} claims as imported, on the {calculation} replica",
                release=release,
                acc_version=service.claim_year(imported.submission.deadline).acc_version,
                calculation=calculation,
            )
        except AccessDenied:
            return None
    return LegacyReproduction.objects.create(imported=imported, calculation=calculation, run_id=run)


def compare(rep: LegacyReproduction) -> dict:
    """Once the reproduction's run has finished, compare its outputs with the imported ones, record by record (once; kept on the
    reproduction): {"state": "running" | "failed" | "done", "records", "differ", "fields", "examples": [{claim_id, field, imported,
    replica}]}."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from cedars.services.submissions import service as submissions

    if rep.compared.get("state") in ("done", "failed"):
        return rep.compared
    d = submissions.restated_run(rep.run_id)
    if d is None or (d["ended"] and not d["succeeded"]):
        rep.compared = {"state": "failed", "error": (d or {}).get("error") or "The run is gone."}
        rep.save(update_fields=["compared"])
        return rep.compared
    key = (d.get("results") or {}).get("measure_level")
    if not d["ended"] or not key:
        return {"state": "running"}
    priced = pq.read_table(pa.BufferReader(storage.store("results").get_bytes(key)))
    outputs = _read(rep.imported, "Outputs.parquet")
    fields = [f for f in COMPARED if f in priced.column_names and f in outputs.column_names]
    by_id = {}
    p_ids = priced.column("CEInputID").to_pylist()
    p_vals = {f: priced.column(f).to_pylist() for f in fields}
    for i, cid in enumerate(p_ids):
        by_id[(cid or "").strip().lower()] = {f: p_vals[f][i] for f in fields}
    o_vals = {f: outputs.column(f).to_pylist() for f in fields}
    rel, absolute = TOLERANCE
    records = differ = 0
    examples = []
    for i, cid in enumerate(_keyed(outputs.column("ClaimID").to_pylist())):
        if not cid:
            continue
        records += 1
        got = by_id.get(cid.lower())
        diffs = []
        for f in fields:
            a = float(o_vals[f][i] or 0)
            b = float((got or {}).get(f) or 0) if got is not None else None
            if b is None or abs(a - b) > max(absolute, rel * max(abs(a), abs(b))):
                diffs.append({"claim_id": cid, "field": f, "imported": a, "replica": b})
        if diffs:
            differ += 1
            examples += diffs[: max(0, EXAMPLES - len(examples))]
    rep.compared = {"state": "done", "records": records, "differ": differ, "fields": fields, "examples": examples}
    rep.save(update_fields=["compared"])
    return rep.compared

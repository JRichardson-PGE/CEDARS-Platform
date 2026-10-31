"""Legacy CEDARS budget filings (phase 7): each filing CEDARS publishes as record-level downloads, loaded from the staged copies
(`manage.py stage_legacy_sources`, then `manage.py load_legacy_filings`) as one published, imported version per PA.

For each filing in FILINGS, in order: its deadline and FilingSetup (the CPUC's calendar, with the filing's ACC), then for each PA in the
all-PA files: the records renamed through the crosswalk (`dictionary.legacy`) and written as Parquet (Measure inputs, CEDARS' CET outputs
as filed, and the program columns as filed), the program years missing from the registry added (`registry.add_history`), and the
reconciliation. A PA whose hard checks pass becomes an imported version (`legacy.record_import`) with its budget lines (program costs
from the ten Weighted columns, rebates and incentives from the four incentive outputs) and its results (run "legacy-cedars": CEDARS' CET
outputs summed per program and year, never re-priced). A PA that fails a hard check is not published and the command says why.

Reconciliation per PA (the plan's levels 1 and 2; level 3, the published dashboard totals, is compared outside the platform for now):
  hard  per-PA file rows = the PA's rows in the all-PA file (when the per-PA files are staged)
  hard  rows written = rows read
  hard  every additive output summed exactly (decimal) from the file = its sum in the Parquet written (1e-9 relative)
  hard  the ten Weighted cost columns = Weighted Program Cost, per program and year (1e-9 relative)
  soft  rebuilt TRC / PAC / RIM benefits (ratio x cost) = the file's own benefit columns, where it has them (1e-6 relative)
  soft  budget lines (program costs + rebates and incentives) = the file's Budget column (1e-6 relative)
  info  records whose ratio has no cost to rebuild a numerator from

Layouts without per-test benefit columns (the archived filings) get TRCBen, PACBen and RIMBen rebuilt per record as ratio x cost, so
program ratios come from summed benefits and costs like every other result."""

import hashlib
import io
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import transaction

from cedars.core import storage
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.dictionary import legacy as crosswalk
from cedars.services.legacy import service as legacy
from cedars.services.registry import service as registry
from cedars.services.submissions.models import Deadline

from . import service
from .models import BudgetLine, BudgetResult, FilingSetup

RUN = "legacy-cedars"
IOUS = {"PGE", "SCE", "SCG", "SDGE"}
LAYOUTS = ("filings", "filings_2024_2027", "filings_2026_2027_tual")
COST_COLUMNS = [f"Wtd{f}" for f, _ in service.PROGRAM_COST_FIELDS]
INCENTIVE_OUTPUTS = ["EndUserRebate", "IncentiveToOthers", "DILaborCost", "DIMaterialCost"]
NUMERATORS = {"TRCBen": ("TRCRatio", "TRCCost"), "PACBen": ("PACRatio", "PACCost"), "RIMBen": ("RIMRatio", "RIMCost")}
HARD, SOFT = 1e-9, 1e-6
NOTE = "Legacy CEDARS filing. The due date is nominal (it orders the filings); the downloads do not carry it."


@dataclass(frozen=True)
class LegacyFiling:
    key: str
    kind: str
    first_year: int
    years: int
    acc: str
    due: date
    sources: tuple[tuple[str, str], ...]  # (dataset, year) of the all-PA files, as the manifest names them

    @property
    def label(self) -> str:
        return f"{self.kind}, {self.first_year}-{self.first_year + self.years - 1}" if self.years > 1 else f"{self.kind}, {self.first_year}"


def _abal(year: int, acc: str) -> LegacyFiling:
    return LegacyFiling(str(year), "Annual budget advice letter (ABAL)", year, 1, acc, date(year - 1, 9, 1), (("filings", str(year)),))


# In load order: a later filing of the same deadline is its next version (the archived 2028-2031 snapshot, then the application as
# it stands). ACCs from ReCET's BUDGET_FILING_ACC; the 2017-2019 filings predate it (blank).
FILINGS = [
    *(_abal(y, "") for y in (2017, 2018, 2019)),
    _abal(2020, "2019"),
    *(_abal(y, "2020") for y in (2021, 2022, 2023)),
    LegacyFiling(
        "2024-2027-application", "Business plan application", 2024, 4, "2021", date(2022, 3, 1), (("filings_archive", "2024_2027"),)
    ),
    LegacyFiling("2024-2025-true-up", "True-up advice letter (TUAL)", 2024, 4, "2022", date(2023, 9, 1), (("filings", "2024_to_2025"),)),
    LegacyFiling(
        "2026-2027-tual-archived",
        "True-up advice letter (TUAL) for 2026-2027, archived",
        2024,
        4,
        "2024",
        date(2025, 9, 1),
        (("filings_archive", "2026-2027"),),
    ),
    LegacyFiling(
        "2026-2027-mid-cycle", "Mid-cycle advice letter (MCAL)", 2024, 4, "2024", date(2026, 3, 1), (("filings", "2026_to_2027"),)
    ),
    LegacyFiling(
        "2028-2031-application-snapshot",
        "Business plan application",
        2028,
        4,
        "2024",
        date(2026, 3, 1),
        (("filings_archive", "2028_2031"),),
    ),
    LegacyFiling(
        "2028-2031-application",
        "Business plan application",
        2028,
        4,
        "2024",
        date(2026, 3, 1),
        tuple(("filings", str(y)) for y in (2028, 2029, 2030, 2031)),
    ),
]
BY_KEY = {f.key: f for f in FILINGS}


@dataclass
class Outcome:
    filing: str
    published: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    refused: list[tuple[str, list[str]]] = field(default_factory=list)
    programs_added: int = 0

    def lines(self) -> list[str]:
        out = [
            f"{self.filing}: published {len(self.published)} ({', '.join(self.published)}), unchanged {len(self.unchanged)}, "
            f"refused {len(self.refused)}, program years added {self.programs_added}"
        ]
        out += [f"  {pa} not published: {'; '.join(why)}" for pa, why in self.refused]
        return out


# --- reading


def _is_ratio(name: str) -> bool:
    return name.endswith(("Ratio", "RatioNoAdmin")) or name.startswith("Lev")


def _blank_to_null(col):
    import pyarrow as pa
    import pyarrow.compute as pc

    col = pc.utf8_trim_whitespace(col)
    return pc.if_else(pc.equal(col, ""), pa.scalar(None, pa.string()), col)


def _read(source, member: str):
    """One CSV of a staged zip as text columns under canonical names, with its layout."""
    import pyarrow.csv as pacsv

    layout = source.members[member]
    if layout not in LAYOUTS:
        raise legacy.LegacyError(f"{source.name}: {member} is a {layout} file, not a budget filing")
    names = crosswalk.layouts()[layout]
    with legacy.open_member(source, member, text=False) as f:
        table = pacsv.read_csv(
            f,
            read_options=pacsv.ReadOptions(column_names=names, skip_rows=1, encoding="utf-8"),
            convert_options=pacsv.ConvertOptions(column_types={n: "string" for n in names}, strings_can_be_null=False),
        )
    return table.rename_columns([crosswalk.rename(layout)[n] for n in names]), layout


def _count_rows(source) -> int:
    import csv

    n = 0
    for member in source.members:
        with legacy.open_member(source, member) as f:
            n += sum(1 for _ in csv.reader(f)) - 1
    return n


def _numbers(table, columns: list[str], errors: list[str]):
    """{column: (float64 array, exact sum as a Decimal)} for each column present; a value that is not a number is a hard failure. The
    exact sum adds the text's decimals (decimal256 when every value fits 38 decimal places, else Python's Decimal)."""
    import pyarrow as pa
    import pyarrow.compute as pc

    out = {}
    for c in columns:
        if c not in table.column_names:
            continue
        col = _blank_to_null(table[c])
        try:
            as_float = pc.cast(col, pa.float64())
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
            bad = next((v for v in col.to_pylist() if v is not None and not _is_number(v)), "")
            errors.append(f"{c} holds a value that is not a number ({bad[:40]!r})")
            continue
        try:
            exact = Decimal(pc.sum(pc.cast(col, pa.decimal256(76, 38))).as_py() or 0)
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
            exact = sum((Decimal(v) for v in col.to_pylist() if v is not None), Decimal(0))
        out[c] = (as_float, exact)
    return out


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _dec(value) -> Decimal:
    return Decimal(0) if value is None else Decimal(value)


def _close(a: float, b: float, rel: float) -> bool:
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


# --- loading


def _organisation(code: str) -> Organisation:
    org = Organisation.objects.filter(code=code).first()
    if org is None:
        org = access.create_organisation(None, code=code, name=code, kind="pa", pa_type="IOU" if code in IOUS else "")
    return org


def _deadline(filing: LegacyFiling) -> Deadline:
    cycle = registry.ensure_cycle(filing.first_year, filing.years)
    deadline = Deadline.objects.filter(module=service.MODULE, kind=filing.kind, cycle=cycle).first()
    if deadline is None:
        deadline = service.add_filing(
            None, first_year=filing.first_year, kind=filing.kind, due_at=service.due_at(filing.due), acc_version=filing.acc, notes=NOTE
        )
        if deadline.cycle_id != cycle.pk:  # add_filing ensures a four-year cycle; a one-year cycle already exists, so it is this one
            raise legacy.LegacyError(f"{filing.key}: the filing landed on {deadline.cycle}, not {cycle}")
    elif not hasattr(deadline, "budget_setup"):
        FilingSetup.objects.create(deadline=deadline, acc_version=filing.acc)
    return deadline


def _sources(filing: LegacyFiling) -> tuple[list, dict[str, list]]:
    """The all-PA sources of a filing (every one must be staged) and the per-PA files of each, for the count check."""
    alls, per_pa = [], defaultdict(list)
    for dataset, year in filing.sources:
        found = legacy.sources(dataset, year)
        all_pa = [s for s in found if s.pa == "all"]
        if not all_pa:
            raise legacy.LegacyError(f"{filing.key}: the all-PA file for {dataset} {year} is not staged")
        alls.append(all_pa[0])
        for s in found:
            if s.pa not in ("all", "SW"):
                per_pa[s.pa].append(s)
    return alls, per_pa


def _parquet(table) -> tuple[bytes, str]:
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def load(filing: LegacyFiling, *, only: str = "") -> Outcome:
    """Load one filing for every PA in its all-PA files (or one PA). A system job: the management command."""
    import pyarrow as pa
    import pyarrow.compute as pc

    outcome = Outcome(filing.key)
    alls, per_pa = _sources(filing)
    deadline = _deadline(filing)
    parts = []
    for s in alls:
        for member in s.members:
            parts.append(_read(s, member))
    layouts = sorted({lay for _, lay in parts})
    if len(layouts) > 1:
        raise legacy.LegacyError(f"{filing.key}: its files have different layouts ({', '.join(layouts)})")
    layout = layouts[0]
    table = pa.concat_tables([t for t, _ in parts])
    pas = sorted(set(table["PA"].to_pylist()))
    measure_cols = crosswalk.by_table(layout, "Key") + crosswalk.by_table(layout, "Measure") + crosswalk.by_table(layout, "Legacy")
    program_cols = crosswalk.by_table(layout, "Program")
    output_cols = crosswalk.by_table(layout, "Output")
    numeric_measure = crosswalk.NUMERIC_MEASURE
    for code in pas:
        if only and code != only:
            continue
        org = _organisation(code)
        if legacy.already_imported(deadline, org, alls) is not None:
            outcome.unchanged.append(code)
            continue
        rows = table.filter(pc.equal(table["PA"], code))
        checks, errors = [], []
        if code in per_pa:
            n = sum(_count_rows(s) for s in per_pa[code])
            checks.append({"name": "per-PA file rows", "kind": "hard", "ok": n == rows.num_rows, "expected": rows.num_rows, "got": n})
        nums = _numbers(rows, output_cols, errors)
        measure_nums = _numbers(rows, [c for c in measure_cols if c in numeric_measure], errors)
        budget_nums = _numbers(rows, ["Legacy_Budget"], errors)
        checks.append({"name": "numbers readable", "kind": "hard", "ok": not errors, "detail": errors})
        if errors:
            outcome.refused.append((code, errors))
            continue
        # the tables written: Measure (keys, inputs, Legacy_ columns), Outputs (CEDARS' CET outputs), Programs (as filed)
        measure = pa.table({c: (measure_nums[c][0] if c in measure_nums else rows[c]) for c in measure_cols})
        outputs = pa.table(
            {"CEInputID": rows["CEInputID"], "PrgID": rows["PrgID"], "Used_Year": rows["Used_Year"], **{c: v[0] for c, v in nums.items()}}
        )
        programs = _programs(rows, program_cols)
        written = {}
        tag = hashlib.sha256("".join(sorted(s.sha256 for s in alls)).encode()).hexdigest()[:12]
        folder = f"{legacy.PREFIX}/loaded/budgets/{filing.key}/{code}/{tag}/"
        store = storage.store(legacy.AREA)
        for name, t in (("Measure.parquet", measure), ("Outputs.parquet", outputs), ("Programs.parquet", programs)):
            data, sha = _parquet(t)
            store.put_bytes(folder + name, data)
            written[name] = {"name": name, "key": folder + name, "sha256": sha, "bytes": len(data), "rows": t.num_rows}
        checks.append(
            {
                "name": "rows written",
                "kind": "hard",
                "ok": written["Measure.parquet"]["rows"] == written["Outputs.parquet"]["rows"] == rows.num_rows,
                "expected": rows.num_rows,
            }
        )
        # every additive output: the exact sum of the file's text = the sum of what was written
        differ = []
        for c, (_as_float, exact_sum) in nums.items():
            if _is_ratio(c):
                continue
            exact, got = float(exact_sum), float(pc.sum(outputs[c]).as_py() or 0)
            if not _close(exact, got, HARD):
                differ.append({"column": c, "file": exact, "written": got})
        checks.append({"name": "additive outputs", "kind": "hard", "ok": not differ, "detail": differ})
        lines, results, more = _lines_and_results(rows, {**nums, **budget_nums}, code)
        checks += more
        failed = [c["name"] for c in checks if c["kind"] == "hard" and not c["ok"]]
        reconciliation = {"filing": filing.key, "pa": code, "layout": layout, "records": rows.num_rows, "checks": checks}
        report = json.dumps(reconciliation, indent=1, default=str).encode()
        store.put_bytes(folder + "reconciliation.json", report)
        if failed:
            outcome.refused.append((code, [f"hard check failed: {name}" for name in failed]))
            continue
        outcome.programs_added += _add_program_history(programs, org, filing)
        files = [
            *written.values(),
            {
                "name": "reconciliation.json",
                "key": folder + "reconciliation.json",
                "sha256": hashlib.sha256(report).hexdigest(),
                "bytes": len(report),
            },
            *({"name": s.name, "key": s.key, "sha256": s.sha256, "bytes": s.bytes, "source_url": s.url} for s in alls),
        ]
        with transaction.atomic():
            imp = legacy.record_import(
                None,
                module=service.MODULE,
                period=filing.key,
                deadline=deadline,
                organisation=org,
                files=files,
                sources=alls,
                records=rows.num_rows,
                reconciliation=reconciliation,
                calculation={
                    "run": RUN,
                    "engine": "CET (legacy CEDARS), release not published",
                    "acc": filing.acc,
                    "sources": sorted(s.sha256 for s in alls),
                },
            )
            v = imp.version
            BudgetLine.objects.bulk_create(BudgetLine(version=v, **ln) for ln in lines)
            BudgetResult.objects.bulk_create(BudgetResult(version=v, run=RUN, **r) for r in results)
        outcome.published.append(code)
    return outcome


def _programs(rows, program_cols):
    """One row per (PrgID, program year) with the program columns as filed (the first record's)."""
    import pyarrow as pa

    seen, out = set(), defaultdict(list)
    cols = ["PrgID", "Used_Year", *program_cols]
    for r in rows.select(cols).to_pylist():
        k = (r["PrgID"], r["Used_Year"])
        if k in seen:
            continue
        seen.add(k)
        for c in cols:
            out[c].append(r[c])
    return pa.table({c: pa.array(out[c], pa.string()) for c in cols})


TRUE = {"true", "1", "yes", "y", "t"}
FLAGS = {
    "Prg_Resource_Flag": "Resource_Flag",
    "Prg_NonResource_Flag": "NonResource_Flag",
    "Prg_Deemed_Flag": "Deemed_Flag",
    "Prg_Custom_Flag": "Custom_Flag",
    "Prg_Upstream_Flag": "Upstream_Flag",
    "Prg_Midstream_Flag": "Midstream_Flag",
    "Prg_Downstream_Flag": "Downstream_Flag",
    "Prg_DirectInstall": "DirectInstall",
    "Prg_Audit_Flag": "Audit_Flag",
    "Prg_Financing": "Financing",
}


def _add_program_history(programs, org: Organisation, filing: LegacyFiling) -> int:
    """The filing's program years the registry has no version for (its own history wins)."""
    by_program: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in programs.to_pylist():
        year = r["Used_Year"]
        if not (r["PrgID"] or "").strip() or not str(year).strip().isdigit():
            continue
        by_program[r["PrgID"].strip()][int(year)] = {
            "name": (r.get("Prg_ProgramName") or r["PrgID"]).strip(),
            "status": (r.get("Prg_ProgramStatus") or "").strip(),
            "segment": (r.get("Prg_ProgramSegment") or "").strip(),
            "primary_sector": (r.get("Prg_PrimarySector") or "").strip(),
            "implementer": (r.get("Prg_ProgramImplementer") or "").strip(),
            "category": (r.get("Prg_ProgramCategory") or "").strip(),
            "statewide_program": (r.get("Prg_StatewideProgram") or "").strip(),
            "flags": {flag: (r.get(c) or "").strip() for c, flag in FLAGS.items() if (r.get(c) or "").strip()},
            "exclude_from_budget": (r.get("Prg_Exclude_From_Budget") or "").strip().lower() in TRUE,
            "exclude_from_ce": (r.get("Prg_Exclude_From_CE") or "").strip().lower() in TRUE,
        }
    added = 0
    for prg_id, years in sorted(by_program.items()):
        _, n = registry.add_history(
            None, prg_id=prg_id, administrator=org, statewide=False, years=years, reason=f"legacy CEDARS {filing.label}"
        )
        added += n
    return added


def _lines_and_results(rows, nums, code: str) -> tuple[list[dict], list[dict], list[dict]]:
    """The budget lines and results per (program, year), and their checks."""
    import pyarrow.compute as pc

    prg, years = rows["PrgID"].to_pylist(), rows["Used_Year"].to_pylist()
    names = rows["Prg_ProgramName"].to_pylist() if "Prg_ProgramName" in rows.column_names else [""] * rows.num_rows
    dec = {
        c: [None if t is None or not t.strip() else t.strip() for t in rows[c].to_pylist()]
        for c in nums
        if c in COST_COLUMNS + INCENTIVE_OUTPUTS + ["WeightedProgramCost", "Legacy_Budget"]
    }
    flt = {c: v[0].to_pylist() for c, v in nums.items() if not _is_ratio(c) and not c.startswith("Legacy_")}
    ratios = {c: nums[c][0].to_pylist() for c in ("TRCRatio", "PACRatio", "RIMRatio") if c in nums}
    lines: dict[tuple, dict] = {}
    results: dict[tuple, dict] = {}
    rebuilt_from_ratio = {k: 0.0 for k in NUMERATORS}
    no_cost = defaultdict(int)
    for i in range(rows.num_rows):
        y = str(years[i] or "").strip()
        k = (prg[i] or "", int(y) if y.isdigit() else 0)
        ln = lines.setdefault(
            k,
            {
                "prg_id": k[0],
                "year": k[1],
                "pa": code,
                "costs": defaultdict(Decimal),
                "rebates": Decimal(0),
                "measures": 0,
                "wpc": Decimal(0),
                "budget": Decimal(0),
            },
        )
        for c in COST_COLUMNS:
            if c in dec:
                ln["costs"][c[3:]] += _dec(dec[c][i])
        ln["rebates"] += sum((_dec(dec[c][i]) for c in INCENTIVE_OUTPUTS if c in dec), Decimal(0))
        ln["wpc"] += _dec(dec["WeightedProgramCost"][i]) if "WeightedProgramCost" in dec else Decimal(0)
        ln["budget"] += _dec(dec["Legacy_Budget"][i]) if "Legacy_Budget" in dec else Decimal(0)
        ln["measures"] += 1
        res = results.setdefault(
            k, {"pa": code, "prg_id": k[0], "year": k[1], "program_name": "", "measures": 0, "values": defaultdict(float)}
        )
        res["program_name"] = res["program_name"] or (names[i] or "")
        res["measures"] += 1
        for c, vals in flt.items():
            if vals[i] is not None:
                res["values"][c] += vals[i]
        for ben, (ratio, cost) in NUMERATORS.items():
            if ratio not in ratios or cost not in flt:
                continue
            r, cst = ratios[ratio][i], flt[cost][i]
            if r and not cst:
                no_cost[ben] += 1
            value = (r or 0.0) * (cst or 0.0)
            rebuilt_from_ratio[ben] += value
            if ben not in flt:  # the layout has no benefit column for the test: rebuild it
                res["values"][ben] += value
    checks = []
    differ = [
        {"prg_id": k[0], "year": k[1], "weighted": str(sum(ln["costs"].values(), Decimal(0))), "weighted_program_cost": str(ln["wpc"])}
        for k, ln in lines.items()
        if "WeightedProgramCost" in dec and not _close(float(sum(ln["costs"].values(), Decimal(0))), float(ln["wpc"]), HARD)
    ]
    checks.append({"name": "weighted costs = Weighted Program Cost", "kind": "hard", "ok": not differ, "detail": differ})
    for ben in NUMERATORS:
        if ben in flt:
            filed = float(pc.sum(nums[ben][0]).as_py() or 0)
            checks.append(
                {
                    "name": f"{ben} = ratio x cost",
                    "kind": "soft",
                    "ok": _close(filed, rebuilt_from_ratio[ben], SOFT),
                    "file": filed,
                    "rebuilt": rebuilt_from_ratio[ben],
                }
            )
    if "Legacy_Budget" in dec:
        filed = float(sum((ln["budget"] for ln in lines.values()), Decimal(0)))
        built = float(sum((sum(ln["costs"].values(), Decimal(0)) + ln["rebates"] for ln in lines.values()), Decimal(0)))
        checks.append({"name": "budget lines = Budget", "kind": "soft", "ok": _close(filed, built, SOFT), "file": filed, "lines": built})
    checks.append({"name": "ratios without a cost", "kind": "info", "ok": True, "detail": dict(no_cost)})
    out_lines = [
        {
            "prg_id": ln["prg_id"],
            "year": ln["year"],
            "pa": ln["pa"],
            "program_costs": {f: str(ln["costs"].get(f, Decimal(0))) for f, _ in service.PROGRAM_COST_FIELDS},
            "on_bill_financing": Decimal(0),
            "rebates_and_incentives": ln["rebates"].quantize(Decimal("0.01")),
            "measures": ln["measures"],
        }
        for ln in lines.values()
    ]
    out_results = [{**r, "values": dict(r["values"])} for r in results.values()]
    return out_lines, out_results, checks

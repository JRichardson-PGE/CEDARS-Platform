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
  soft  each test's benefits (filed or from the benefit columns) = ratio x cost (1e-6 relative)
  soft  budget lines (program costs + rebates and incentives) = the file's Budget column (1e-6 relative)
  info  records whose ratio has no cost to rebuild a numerator from

Layouts without per-test benefit columns (TRC in every layout; PAC and RIM in the archived filings) get them built per record as
`legacy.tables.benefits` says: TRCBen = ElecBen + GasBen + OtherBen, PACBen = RIMBen = ElecBen + GasBen, ratio x cost where a record has no
benefit values. Program ratios come from summed benefits and costs like every other result. `rebuild_benefits` corrects the results of
filings loaded before (when every missing benefit was ratio x cost)."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import transaction

from cedars.core import storage
from cedars.services import audit
from cedars.services.dictionary import legacy as crosswalk
from cedars.services.legacy import service as legacy
from cedars.services.legacy import snapshot as legacy_snapshot
from cedars.services.legacy import tables
from cedars.services.registry import service as registry
from cedars.services.submissions.models import Deadline

from . import service
from .models import BudgetLine, BudgetResult, FilingSetup

RUN = "legacy-cedars"
LAYOUTS = ("filings", "filings_2024_2027", "filings_2026_2027_tual")
COST_COLUMNS = [f"Wtd{f}" for f, _ in service.PROGRAM_COST_FIELDS]
INCENTIVE_OUTPUTS = ["EndUserRebate", "IncentiveToOthers", "DILaborCost", "DIMaterialCost"]
NUMERATORS = tables.NUMERATORS
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
    snapshots: int = 0
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"{self.filing}: published {len(self.published)} ({', '.join(self.published)}), unchanged {len(self.unchanged)}, "
            f"refused {len(self.refused)}, program years added {self.programs_added}, dashboard snapshots {self.snapshots}"
        ]
        out += [f"  {pa} not published: {'; '.join(why)}" for pa, why in self.refused]
        out += [f"  {n}" for n in self.notes]
        return out


# --- loading


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
            parts.append(tables.read(s, member, LAYOUTS))
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
        org = tables.organisation(code)
        if legacy.already_imported(deadline, org, alls) is not None:
            outcome.unchanged.append(code)
            continue
        rows = table.filter(pc.equal(table["PA"], code))
        checks, errors = [], []
        if code in per_pa:
            n = sum(tables.count_rows(s, LAYOUTS) for s in per_pa[code])
            checks.append({"name": "per-PA file rows", "kind": "hard", "ok": n == rows.num_rows, "expected": rows.num_rows, "got": n})
        nums = tables.numbers(rows, output_cols, errors)
        measure_nums = tables.numbers(rows, [c for c in measure_cols if c in numeric_measure], errors)
        budget_nums = tables.numbers(rows, ["Legacy_Budget"], errors)
        checks.append({"name": "numbers readable", "kind": "hard", "ok": not errors, "detail": errors})
        if errors:
            outcome.refused.append((code, errors))
            continue
        # the tables written: Measure (keys, inputs, Legacy_ columns), Outputs (CEDARS' CET outputs), Programs (as filed)
        measure = pa.table({c: (measure_nums[c][0] if c in measure_nums else rows[c]) for c in measure_cols})
        outputs = pa.table(
            {"CEInputID": rows["CEInputID"], "PrgID": rows["PrgID"], "Used_Year": rows["Used_Year"], **{c: v[0] for c, v in nums.items()}}
        )
        programs = tables.programs(rows, program_cols)
        written = {}
        tag = hashlib.sha256("".join(sorted(s.sha256 for s in alls)).encode()).hexdigest()[:12]
        folder = f"{legacy.PREFIX}/loaded/budgets/{filing.key}/{code}/{tag}/"
        store = storage.store(legacy.AREA)
        for name, t in (("Measure.parquet", measure), ("Outputs.parquet", outputs), ("Programs.parquet", programs)):
            data, sha = tables.parquet(t)
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
            if tables.is_ratio(c):
                continue
            exact, got = float(exact_sum), float(pc.sum(outputs[c]).as_py() or 0)
            if not tables.close(exact, got, HARD):
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
            if imp.created:
                v = imp.version
                BudgetLine.objects.bulk_create(BudgetLine(version=v, **ln) for ln in lines)
                BudgetResult.objects.bulk_create(BudgetResult(version=v, run=RUN, **r) for r in results)
        # another load of the same files may have published it while this one ran
        (outcome.published if imp.created else outcome.unchanged).append(code)
        if imp.created:
            try:  # the version's dashboard snapshot; a failure leaves it to manage.py snapshot_legacy
                if legacy_snapshot.publish_snapshot(imp):
                    outcome.snapshots += 1
            except Exception as e:
                outcome.notes.append(f"{code}: no dashboard snapshot ({e}); run manage.py snapshot_legacy")
    if outcome.snapshots:
        legacy_snapshot.rebuild(["forecast"])
    return outcome


def _add_program_history(programs, org, filing: LegacyFiling) -> int:
    return tables.add_program_history(programs, org, f"legacy CEDARS {filing.label}")


def _lines_and_results(rows, nums, code: str) -> tuple[list[dict], list[dict], list[dict]]:
    """The budget lines and results per (program, year), and their checks."""
    import pyarrow.compute as pc

    prg, years = rows["PrgID"].to_pylist(), rows["Used_Year"].to_pylist()
    names = rows["Prg_ProgramName"].to_pylist() if "Prg_ProgramName" in rows.column_names else [""] * rows.num_rows
    dec = {c: tables.texts(rows, c) for c in nums if c in COST_COLUMNS + INCENTIVE_OUTPUTS + ["WeightedProgramCost", "Legacy_Budget"]}
    flt = {c: v[0].to_pylist() for c, v in nums.items() if not tables.is_ratio(c) and not c.startswith("Legacy_")}
    ratios = {c: nums[c][0].to_pylist() for c in ("TRCRatio", "PACRatio", "RIMRatio") if c in nums}
    built = tables.benefits({c: v[0] for c, v in nums.items()})
    built_values = {ben: b.values.to_pylist() for ben, b in built.items()}
    from_ratio = {ben: b.from_ratio.to_pylist() for ben, b in built.items() if b.from_ratio is not None}
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
                ln["costs"][c[3:]] += tables.dec(dec[c][i])
        ln["rebates"] += sum((tables.dec(dec[c][i]) for c in INCENTIVE_OUTPUTS if c in dec), Decimal(0))
        ln["wpc"] += tables.dec(dec["WeightedProgramCost"][i]) if "WeightedProgramCost" in dec else Decimal(0)
        ln["budget"] += tables.dec(dec["Legacy_Budget"][i]) if "Legacy_Budget" in dec else Decimal(0)
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
            if ben in from_ratio:
                r, cst = ratios[ratio][i], flt[cost][i]
                if r and not cst:
                    no_cost[ben] += 1
                rebuilt_from_ratio[ben] += from_ratio[ben][i]
            if ben in built_values and ben not in flt:  # no benefit column for the test: built from the benefit columns
                res["values"][ben] += built_values[ben][i] or 0.0
    checks = []
    differ = [
        {"prg_id": k[0], "year": k[1], "weighted": str(sum(ln["costs"].values(), Decimal(0))), "weighted_program_cost": str(ln["wpc"])}
        for k, ln in lines.items()
        if "WeightedProgramCost" in dec and not tables.close(float(sum(ln["costs"].values(), Decimal(0))), float(ln["wpc"]), HARD)
    ]
    checks.append({"name": "weighted costs = Weighted Program Cost", "kind": "hard", "ok": not differ, "detail": differ})
    for ben in NUMERATORS:
        if ben in from_ratio and built[ben].source != "ratio x cost":
            filed = float(pc.sum(built[ben].values).as_py() or 0)
            checks.append(
                {
                    "name": f"{ben} = ratio x cost",
                    "kind": "soft",
                    "ok": tables.close(filed, rebuilt_from_ratio[ben], SOFT),
                    "file": filed,
                    "rebuilt": rebuilt_from_ratio[ben],
                }
            )
    if "Legacy_Budget" in dec:
        filed = float(sum((ln["budget"] for ln in lines.values()), Decimal(0)))
        built = float(sum((sum(ln["costs"].values(), Decimal(0)) + ln["rebates"] for ln in lines.values()), Decimal(0)))
        checks.append(
            {"name": "budget lines = Budget", "kind": "soft", "ok": tables.close(filed, built, SOFT), "file": filed, "lines": built}
        )
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


def rebuild_benefits() -> list[str]:
    """Correct the results of filings loaded when every benefit the layout lacks was ratio x cost (before 2026-10-31): TRCBen, PACBen
    and RIMBen built again from the stored Outputs table as the load now builds them, per program and year. Audited per import; the
    snapshots are replaced separately (`manage.py snapshot_legacy --replace`)."""
    out, checked, corrected, filed = [], 0, 0, 0
    for imp in legacy.LegacyImport.objects.filter(module=service.MODULE).select_related("version__submission__organisation"):
        checked += 1
        sums = legacy_snapshot.stored_benefits(imp, ("PrgID", "Used_Year"))
        if not sums:
            filed += 1
            continue
        by_key = {(prg, int(y) if str(y).strip().isdigit() else 0): v for (prg, y), v in sums.items()}
        before, after = defaultdict(float), defaultdict(float)
        changed = []
        for res in BudgetResult.objects.filter(version=imp.version, run=RUN):
            new = by_key.get((res.prg_id, res.year), {})
            for b, v in new.items():
                before[b] += res.values.get(b) or 0.0
                after[b] += v
            if any(res.values.get(b) != v for b, v in new.items()):
                res.values = {**res.values, **new}
                changed.append(res)
        BudgetResult.objects.bulk_update(changed, ["values"])
        code = imp.version.submission.organisation.code
        if changed:
            audit.record(
                "legacy.benefits_rebuilt",
                obj=imp,
                before=dict(before),
                after=dict(after),
                reason="Benefits built from the benefit columns, not ratio x cost (CEDARS' published ratios use the columns)",
                organisation=imp.version.submission.organisation,
            )
        corrected += bool(changed)
        out.append(f"{imp.period} {code}: {len(changed)} program results corrected")
    out.append(f"{checked} imports checked, {corrected} corrected, {filed} with every benefit filed (nothing to rebuild)")
    return out

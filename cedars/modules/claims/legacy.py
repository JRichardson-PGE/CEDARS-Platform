"""Legacy CEDARS claim years (phase 7): the confirmed claims CEDARS publishes as record-level downloads for 2016-2026, loaded from the
staged copies (`manage.py stage_legacy_sources`, then `manage.py load_legacy_claims`) as one published, imported version per PA per claim
year, on the year's annual (final) claim. Legacy CEDARS keeps no quarter confirmations, so the Q1-Q4 deadlines of a legacy year stay
empty; every record keeps its ClaimYearQuarter.

For each year: the ClaimYear (with the ACC claims of that year use, from the engine's table; blank before 2020) and its annual deadline,
then the all-PA file streamed and split by PA (`legacy.tables.split_by_pa`), and for each PA, read in batches of BATCH_ROWS so no step
holds the whole PA (SCG's 2023 claims, 839,000 records, peaked at 9.2 GB held whole, about 1 GB in batches): the records renamed through
the crosswalk and written as Parquet (Measure inputs, the Claim table's fields, CEDARS' CET outputs as filed, the program columns as
filed and, from 2026, the participant data), the program years missing from the registry added, and the reconciliation. A PA whose hard
checks pass becomes an imported version with its claim records (ClaimRecord: the links between claims), its program lines (program costs
from the ten Weighted columns, rebates and incentives from the four incentive outputs, the claims' first-year and lifecycle totals) and
its results (run "legacy-cedars": CEDARS' CET outputs summed per program, never re-priced).

Statewide claims (Jake, 2026-10-30): there were no co-funded statewide programs before 2021; from 2021 the PA files hold each statewide
claim already allocated to its funding PAs, so the totals come from those rows only. From 2023 CEDARS also publishes the same claims
unallocated (`sw_unallocated_confirmed_claims_records_<year>.zip`, PA "SW"); they load as the statewide (SW) version of the year, with
its records, lines and results, but outside every total: its LegacyImport counts them as outside_totals, and `service.year_totals`
leaves the version out. The 2021-2022 unallocated rows are not published.

Reconciliation per PA (the plan's levels 1 and 2):
  hard  per-PA file rows = the PA's rows in the all-PA file (when the per-PA files are staged)
  hard  rows written = rows read
  hard  every additive output summed exactly (decimal) from the file = its sum in the Parquet written (1e-9 relative)
  hard  the ten Weighted cost columns = Weighted Program Cost, per program (1e-9 relative)
  soft  rebuilt PAC / RIM benefits (ratio x cost) = the file's own benefit columns, where it has them (1e-6 relative)
  soft  program lines' expenditures = the file's Total Expenditures (2023 on) or Budget (2016-2022) (1e-6 relative)
  info  records whose ratio has no cost to rebuild a numerator from; ClaimIDs repeated within the PA-year (the repeats get "#2", "#3"
        on their claim record, the files keep them as filed; the plan's gap 7); records with no ClaimID (no claim record)
  info  (year) participant rows whose program is in no PA's claims

The downloads carry no TRC benefit column, so TRCBen is rebuilt per record as TRC ratio x TRC cost (PACBen and RIMBen too where the
layout lacks them), and program ratios come from summed benefits and costs."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.db import transaction

from cedars.core import storage
from cedars.services import audit
from cedars.services.dictionary import legacy as crosswalk
from cedars.services.legacy import service as legacy
from cedars.services.legacy import snapshot as legacy_snapshot
from cedars.services.legacy import tables
from cedars.services.registry import service as registry
from cedars.services.registry.models import Program, ReportingPeriod
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline

from . import service
from .models import FILED_TOTALS, PROGRAM_COST_FIELDS, ClaimProgramLine, ClaimRecord, ClaimResult, ClaimYear

RUN = "legacy-cedars"
YEARS = range(2016, 2027)
UNALLOCATED_FROM = 2023  # the first year CEDARS publishes the statewide claims unallocated
STATEWIDE = "SW"
LAYOUTS = ("claims_a", "claims_b")
PARTICIPANTS = ("participants",)
KIND = "Annual (final) claim"
COST_COLUMNS = [f"Wtd{f}" for f, _ in PROGRAM_COST_FIELDS]
INCENTIVE_OUTPUTS = ["EndUserRebate", "IncentiveToOthers", "DILaborCost", "DIMaterialCost"]
NUMERATORS = {"TRCBen": ("TRCRatio", "TRCCost"), "PACBen": ("PACRatio", "PACCost"), "RIMBen": ("RIMRatio", "RIMCost")}
# A claim's filed totals (FILED_TOTALS), which the downloads carry only as CEDARS' outputs: GrossIncentive = the four incentive outputs.
FILED = {
    "TotalFirstYearGrosskWh": ["FirstYearGrossKWh"],
    "TotalFirstYearGrosskW": ["FirstYearGrossKW"],
    "TotalFirstYearGrossTherm": ["FirstYearGrossThm"],
    "TotalFirstYearNetkWh": ["FirstYearNetKWh"],
    "TotalFirstYearNetkW": ["FirstYearNetKW"],
    "TotalFirstYearNetTherm": ["FirstYearNetThm"],
    "TotalLifecycleNetkWh": ["LifecycleNetKWh"],
    "TotalLifecycleNetTherm": ["LifecycleNetThm"],
    "TotalGrossMeasureCost": ["GrossMeasureCost"],
    "TotalGrossIncentive": INCENTIVE_OUTPUTS,
}
assert set(FILED) == set(FILED_TOTALS)
HARD, SOFT = 1e-9, 1e-6
BATCH_ROWS = 50_000  # rows per batch: a PA is read, written and summed in batches, never whole
NOTE = (
    "Legacy CEDARS claim year, loaded from the public record-level downloads. Only the annual (final) claim holds versions; the due "
    "date is nominal (the downloads do not carry it)."
)


@dataclass
class Outcome:
    year: int
    published: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    refused: list[tuple[str, list[str]]] = field(default_factory=list)
    programs_added: int = 0
    snapshots: int = 0
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"{self.year}: published {len(self.published)} ({', '.join(self.published)}), unchanged {len(self.unchanged)}, "
            f"refused {len(self.refused)}, program years added {self.programs_added}, dashboard snapshots {self.snapshots}"
        ]
        out += [f"  {pa} not published: {'; '.join(why)}" for pa, why in self.refused]
        out += [f"  {n}" for n in self.notes]
        return out


# --- the claim year and its deadline


def _deadline(year: int) -> Deadline:
    """The year's annual (final) claim deadline, with a ClaimYear (set up here when the CPUC has not: ACC from the engine's table)."""
    if not ClaimYear.objects.filter(year=year).exists():
        cy = ClaimYear.objects.create(year=year, acc_version=service.default_acc(year), notes=NOTE)
        audit.record("claim_year.set_up", obj=cy, after={"year": year, "acc": cy.acc_version, "source": "legacy CEDARS"}, actor=None)
    deadline = Deadline.objects.filter(module=service.MODULE, kind=KIND, period__year=year).first()
    if deadline is None:
        registry.ensure_periods(year)
        period = ReportingPeriod.objects.get(kind="year", year=year, number=0)
        deadline = submissions.add_deadline(
            None, module=service.MODULE, kind=KIND, period=period, due_at=service.due_at(date(year + 1, 3, 1)), notes=NOTE
        )
    return deadline


def _sources(year: int) -> tuple[legacy.LegacySource, legacy.LegacySource | None, dict[str, list]]:
    """The year's all-PA file, its statewide unallocated file (required from 2023) and the per-PA files, for the count check."""
    found = legacy.sources("claims", str(year))
    all_pa = [s for s in found if s.pa == "all"]
    if not all_pa:
        raise legacy.LegacyError(f"{year}: the all-PA claims file is not staged")
    sw = next((s for s in found if s.pa == STATEWIDE), None)
    if year >= UNALLOCATED_FROM and sw is None:
        raise legacy.LegacyError(f"{year}: the statewide unallocated claims file is not staged")
    per_pa = defaultdict(list)
    for s in found:
        if s.pa not in ("all", STATEWIDE):
            per_pa[s.pa].append(s)
    return all_pa[0], sw, per_pa


# --- loading


def load(year: int, *, only: str = "") -> Outcome:
    """Load one claim year for every PA in its all-PA file and, from 2023, the statewide unallocated claims (or one PA). A system job:
    the management command."""
    import pyarrow.parquet as pq

    outcome = Outcome(year)
    all_src, sw_src, per_pa = _sources(year)
    deadline = _deadline(year)
    acc = ClaimYear.objects.get(year=year).acc_version
    with tables.scratch() as tmp:
        groups: dict[str, tuple[list[Path], legacy.LegacySource, str]] = {}
        participants = []
        for src in (all_src, sw_src):
            if src is None:
                continue
            for member, layout in src.members.items():
                if layout in PARTICIPANTS:
                    participants.append((src, tables.read(src, member, PARTICIPANTS)[0]))
                    continue
                folder = Path(tmp) / hashlib.sha256(f"{src.name}/{member}".encode()).hexdigest()[:12]
                folder.mkdir()
                lay, paths = tables.split_by_pa(src, member, LAYOUTS, folder)
                for code, path in paths.items():
                    if src is sw_src and code != STATEWIDE:
                        raise legacy.LegacyError(f"{year}: the statewide unallocated file holds rows of PA {code}")
                    if src is all_src and code == STATEWIDE:
                        raise legacy.LegacyError(f"{year}: the all-PA file holds statewide unallocated rows (PA {STATEWIDE})")
                    prev = groups.get(code)
                    if prev and prev[2] != lay:
                        raise legacy.LegacyError(f"{year}: {code}'s rows come in two layouts ({prev[2]}, {lay})")
                    groups[code] = ([*(prev[0] if prev else []), path], src, lay)
        programs_of = {}  # PrgID -> the PA whose claims name it (the participant data has no PA column)
        for code, (paths, _, _) in groups.items():
            for p in paths:
                for prg in set(pq.read_table(p, columns=["PrgID"])["PrgID"].to_pylist()):
                    programs_of.setdefault(prg.strip().lower(), code)
        by_pa_participants, unplaced = _place_participants(participants, sw_src, programs_of)
        if unplaced:
            outcome.notes.append(f"{unplaced} participant rows name a program in no PA's claims: not loaded")
        for code in sorted(groups):
            if only and code != only:
                continue
            paths, src, layout = groups[code]
            org = tables.organisation(code)
            if legacy.already_imported(deadline, org, [src]) is not None:
                outcome.unchanged.append(code)
                continue
            _load_pa(outcome, year, acc, deadline, org, paths, layout, src, per_pa.get(code, []), by_pa_participants.get(code))
    if outcome.snapshots:
        legacy_snapshot.rebuild(["claims"])
    return outcome


def _place_participants(participants, sw_src, programs_of) -> tuple[dict, int]:
    """Each PA's participant rows (the statewide file's go to SW; the all-PA file's to the PA whose claims name the program, else the
    program's administrator in the registry), and the number placed nowhere."""
    import pyarrow as pa
    import pyarrow.compute as pc

    out, unplaced = defaultdict(list), 0
    admins = {
        p.prg_id.lower(): p.administrator.code for p in Program.objects.filter(administrator__isnull=False).select_related("administrator")
    }
    for src, t in participants:
        if src is sw_src:
            out[STATEWIDE].append(t)
            continue
        codes = [programs_of.get((prg or "").strip().lower()) or admins.get((prg or "").strip().lower()) for prg in t["PrgID"].to_pylist()]
        unplaced += sum(1 for c in codes if c is None)
        arr = pa.array([c or "" for c in codes], pa.string())
        for code in {c for c in codes if c}:
            out[code].append(t.filter(pc.equal(arr, code)))
    return {c: pa.concat_tables(ts) for c, ts in out.items()}, unplaced


def _batches(paths: list[Path], columns: list[str] | None = None):
    """A PA's split rows as tables of at most BATCH_ROWS rows (all columns, or `columns`), so no step holds the whole PA."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    for p in paths:
        for b in pq.ParquetFile(p).iter_batches(batch_size=BATCH_ROWS, columns=columns):
            yield pa.Table.from_batches([b])


def _load_pa(outcome: Outcome, year, acc, deadline, org, paths, layout, src, per_pa_sources, participants) -> None:
    """One PA's claim year, in two passes over its split rows: the first writes the tables and adds up the sums and checks, the second
    (once the hard checks pass) the claim records."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    code = org.code
    statewide = code == STATEWIDE
    measure_cols = crosswalk.by_table(layout, "Key") + crosswalk.by_table(layout, "Measure") + crosswalk.by_table(layout, "Legacy")
    claim_cols = crosswalk.by_table(layout, "Claim")
    program_cols = crosswalk.by_table(layout, "Program")
    output_cols = crosswalk.by_table(layout, "Output")
    numeric_measure = [c for c in measure_cols if c in crosswalk.NUMERIC_MEASURE]
    checks, errors = [], []
    sums, firsts, n_rows = _Sums(), {}, 0
    tag = hashlib.sha256(src.sha256.encode()).hexdigest()[:12]
    folder = f"{legacy.PREFIX}/loaded/claims/{year}/{code}/{tag}/"
    store = storage.store(legacy.AREA)
    with tables.scratch() as tmp:
        local = {name: Path(tmp) / name for name in ("Measure.parquet", "Claim.parquet", "Outputs.parquet")}
        writers: dict[str, pq.ParquetWriter] = {}
        try:
            for rows in _batches(paths):
                nums = tables.numbers(rows, output_cols, errors)
                measure_nums = tables.numbers(rows, numeric_measure, errors)
                if errors:
                    break
                outputs = pa.table(
                    {
                        "ClaimID": rows["ClaimID"],
                        "PrgID": rows["PrgID"],
                        "Used_Year": rows["Used_Year"],
                        **{c: v[0] for c, v in nums.items()},
                    }
                )
                parts = {
                    "Measure.parquet": pa.table({c: (measure_nums[c][0] if c in measure_nums else rows[c]) for c in measure_cols}),
                    "Claim.parquet": rows.select(["ClaimID", *claim_cols]),
                    "Outputs.parquet": outputs,
                }
                for name, t in parts.items():
                    if name not in writers:
                        writers[name] = pq.ParquetWriter(local[name], t.schema, compression="zstd")
                    writers[name].write_table(t)
                sums.add(rows, outputs, nums)
                _first_program_rows(firsts, rows, program_cols)
                n_rows += rows.num_rows
        finally:
            for w in writers.values():
                w.close()
        if per_pa_sources:
            n = sum(tables.count_rows(s, LAYOUTS) for s in per_pa_sources)
            checks.append({"name": "per-PA file rows", "kind": "hard", "ok": n == n_rows, "expected": n_rows, "got": n})
        checks.append({"name": "numbers readable", "kind": "hard", "ok": not errors, "detail": errors})
        if errors:
            outcome.refused.append((code, errors))
            return
        cols = ["PrgID", "Used_Year", *program_cols]
        programs = pa.table({c: pa.array([r[c] for r in firsts.values()], pa.string()) for c in cols})
        written = {}
        for name, path in local.items():
            store.put_file(folder + name, path)
            rows_written = pq.ParquetFile(path).metadata.num_rows
            written[name] = {"name": name, "key": folder + name, "sha256": storage.sha256_of(path), "bytes": path.stat().st_size,
                             "rows": rows_written}  # fmt: skip
        small = {"Programs.parquet": programs}
        if participants is not None:
            small["Participant.parquet"] = participants
        for name, t in small.items():
            data, sha = tables.parquet(t)
            store.put_bytes(folder + name, data)
            written[name] = {"name": name, "key": folder + name, "sha256": sha, "bytes": len(data), "rows": t.num_rows}
        ok = written["Measure.parquet"]["rows"] == written["Claim.parquet"]["rows"] == written["Outputs.parquet"]["rows"] == n_rows
        checks.append({"name": "rows written", "kind": "hard", "ok": ok, "expected": n_rows})
        checks += sums.output_checks()
        lines, results, more = sums.finish(code)
        checks += more
        repeated, repeats, blank = _repeats(local["Claim.parquet"])
    checks.append({"name": "repeated ClaimIDs", "kind": "info", "ok": True, "count": len(repeats), "examples": repeats[:20]})
    checks.append({"name": "records with no ClaimID", "kind": "info", "ok": True, "count": blank})
    reconciliation = {
        "year": year,
        "pa": code,
        "layout": layout,
        "records": n_rows,
        "outside_totals": statewide,
        "quarters": sorted(sums.quarters),
        "checks": checks,
    }
    report = json.dumps(reconciliation, indent=1, default=str).encode()
    store.put_bytes(folder + "reconciliation.json", report)
    failed = legacy.hard_failures(reconciliation)
    if failed:
        outcome.refused.append((code, [f"hard check failed: {name}" for name in failed]))
        return
    outcome.programs_added += tables.add_program_history(programs, org, f"legacy CEDARS {year} claims", statewide=statewide)
    files = [
        *written.values(),
        {"name": "reconciliation.json", "key": folder + "reconciliation.json", "sha256": hashlib.sha256(report).hexdigest(),
         "bytes": len(report)},
        {"name": src.name, "key": src.key, "sha256": src.sha256, "bytes": src.bytes, "source_url": src.url},
    ]  # fmt: skip
    with transaction.atomic():
        imp = legacy.record_import(
            None,
            module=service.MODULE,
            period=str(year),
            deadline=deadline,
            organisation=org,
            files=files,
            sources=[src],
            records=0 if statewide else n_rows,
            outside_totals=n_rows if statewide else 0,
            reconciliation=reconciliation,
            calculation={
                "run": RUN,
                "engine": "CET (legacy CEDARS), release not published",
                "acc": acc,
                "sources": [src.sha256],
                "outside_totals": statewide,
            },
        )
        if not imp.created:  # another load of the same files published it while this one ran
            outcome.unchanged.append(code)
            return
        v = imp.version
        seen: dict[str, int] = defaultdict(int)
        present = set(pq.ParquetFile(paths[0]).schema_arrow.names)
        record_cols = [c for c in RECORD_COLUMNS + [c for cs in FILED.values() for c in cs] if c in present]
        for rows in _batches(paths, columns=list(dict.fromkeys(record_cols))):
            ClaimRecord.objects.bulk_create(ClaimRecord(version=v, **r) for r in _records(rows, repeated, seen))
        ClaimProgramLine.objects.bulk_create(ClaimProgramLine(version=v, **ln) for ln in lines)
        ClaimResult.objects.bulk_create(ClaimResult(version=v, run=RUN, **r) for r in results)
    outcome.published.append(code)
    _snapshot(outcome, imp)


def _snapshot(outcome: Outcome, imp) -> None:
    """The version's dashboard snapshot (statewide unallocated claims get none); a failure leaves it to `manage.py snapshot_legacy`."""
    try:
        if legacy_snapshot.publish_snapshot(imp):
            outcome.snapshots += 1
    except Exception as e:
        outcome.notes.append(f"{imp.version.submission.organisation.code}: no dashboard snapshot ({e}); run manage.py snapshot_legacy")


def _first_program_rows(firsts: dict, rows, program_cols: list[str]) -> None:
    """Adds to `firsts` ((PrgID, program year) -> the program columns as filed) the first row of each pair not seen in an earlier batch."""
    import pyarrow as pa

    cols = ["PrgID", "Used_Year", *program_cols]
    t = rows.select(cols)
    idx = t.append_column("_i", pa.array(range(t.num_rows), pa.int64()))
    first = idx.group_by(["PrgID", "Used_Year"], use_threads=False).aggregate([("_i", "min")])["_i_min"].to_pylist()
    for r in t.take(sorted(first)).to_pylist():
        firsts.setdefault((r["PrgID"], r["Used_Year"]), r)


def _group_sums(keys, columns: dict) -> dict[str, dict[str, float]]:
    """{key: {column: sum}} of float columns grouped on one key column (pyarrow's group_by)."""
    import pyarrow as pa

    if not columns:
        return {}
    t = pa.table({"_k": keys, **columns}).group_by("_k").aggregate([(c, "sum") for c in columns])
    out = {}
    for r in t.to_pylist():
        out[r["_k"] or ""] = {c: r[f"{c}_sum"] or 0.0 for c in columns}
    return out


def _exact_group_sums(keys: list, rows, columns: list[str]) -> dict[str, dict[str, Decimal]]:
    """{key: {column: exact Decimal sum of the text}} (the program lines' dollars)."""
    out: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for c in columns:
        if c not in rows.column_names:
            continue
        for k, t in zip(keys, tables.texts(rows, c), strict=True):
            if t is not None:
                out[k or ""][c] += Decimal(t)
    return out


class _Sums:
    """A PA's running sums over its batches: per program, the float sums of the additive outputs (with the rebuilt benefits), the exact
    sums of the dollar columns, the record count and name; for the whole PA, each additive output's exact sum from the file and its sum
    as written, the rebuilt and filed benefit totals, the ratios with no cost and the quarters."""

    EXACT = [*COST_COLUMNS, *INCENTIVE_OUTPUTS, "WeightedProgramCost", "TotalExpenditures", "Legacy_Budget"]

    def __init__(self):
        self.floats: dict[str, dict[str, float]] = defaultdict(dict)
        self.exact: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        self.counts: dict[str, int] = defaultdict(int)
        self.names: dict[str, str] = {}
        self.file_exact: dict[str, Decimal] = defaultdict(Decimal)
        self.written: dict[str, float] = defaultdict(float)
        self.rebuilt_total: dict[str, float] = defaultdict(float)
        self.filed_total: dict[str, float] = defaultdict(float)
        self.no_cost: dict[str, int] = defaultdict(int)
        self.columns: set[str] = set()
        self.quarters: set[str] = set()

    def add(self, rows, outputs, nums) -> None:
        import pyarrow as pa
        import pyarrow.compute as pc

        self.columns |= set(rows.column_names)
        keys = rows["PrgID"]
        key_list = keys.to_pylist()
        floats = {c: outputs[c] for c in nums if not tables.is_ratio(c)}
        for c, (_as_float, exact_sum) in nums.items():
            if not tables.is_ratio(c):
                self.file_exact[c] += exact_sum
                self.written[c] += float(pc.sum(outputs[c]).as_py() or 0)
        rebuilt = {}
        for ben, (ratio, cost) in NUMERATORS.items():
            if ratio not in nums or cost not in nums:
                continue
            r, cst = pc.fill_null(outputs[ratio], 0.0), pc.fill_null(outputs[cost], 0.0)
            value = pc.multiply(r, cst)
            self.rebuilt_total[ben] += float(pc.sum(value).as_py() or 0)
            self.no_cost[ben] += int(pc.sum(pc.and_(pc.not_equal(r, 0.0), pc.equal(cst, 0.0)).cast(pa.int64())).as_py() or 0)
            if ben in floats:
                self.filed_total[ben] += float(pc.sum(outputs[ben]).as_py() or 0)
            else:  # the layout has no benefit column for the test: rebuild it
                rebuilt[ben] = value
        for prg, s in _group_sums(keys, {**floats, **rebuilt}).items():
            mine = self.floats[prg]
            for c, value in s.items():
                mine[c] = mine.get(c, 0.0) + value
        for prg, e in _exact_group_sums(key_list, rows, self.EXACT).items():
            for c, value in e.items():
                self.exact[prg][c] += value
        for prg, name in zip(key_list, rows["Prg_ProgramName"].to_pylist(), strict=True):
            self.names.setdefault(prg or "", name or "")
            self.counts[prg or ""] += 1
        self.quarters |= {q for q in pc.unique(rows["ClaimYearQuarter"]).to_pylist() if q}

    def output_checks(self) -> list[dict]:
        differ = []
        for c, exact_sum in self.file_exact.items():
            exact, got = float(exact_sum), self.written[c]
            if not tables.close(exact, got, HARD):
                differ.append({"column": c, "file": exact, "written": got})
        return [{"name": "additive outputs", "kind": "hard", "ok": not differ, "detail": differ}]

    def finish(self, code: str) -> tuple[list[dict], list[dict], list[dict]]:
        """The program lines and results per program (a claim year's files hold one year), and their checks."""
        checks, differ = [], []
        for prg, e in self.exact.items():
            weighted = sum((e[c] for c in COST_COLUMNS), Decimal(0))
            if "WeightedProgramCost" in self.columns and not tables.close(float(weighted), float(e["WeightedProgramCost"]), HARD):
                differ.append({"prg_id": prg, "weighted": str(weighted), "weighted_program_cost": str(e["WeightedProgramCost"])})
        checks.append({"name": "weighted costs = Weighted Program Cost", "kind": "hard", "ok": not differ, "detail": differ})
        for ben in NUMERATORS:
            if ben in self.filed_total:
                filed, rebuilt = self.filed_total[ben], self.rebuilt_total[ben]
                ok = tables.close(filed, rebuilt, SOFT)
                checks.append({"name": f"{ben} = ratio x cost", "kind": "soft", "ok": ok, "file": filed, "rebuilt": rebuilt})
        built = float(sum((sum((e[c] for c in COST_COLUMNS + INCENTIVE_OUTPUTS), Decimal(0)) for e in self.exact.values()), Decimal(0)))
        for column, label in (("TotalExpenditures", "Total Expenditures"), ("Legacy_Budget", "Budget")):
            if column in self.columns:
                filed = float(sum((e[column] for e in self.exact.values()), Decimal(0)))
                ok = tables.close(filed, built, SOFT)
                checks.append({"name": f"program lines = {label}", "kind": "soft", "ok": ok, "file": filed, "lines": built})
        checks.append({"name": "ratios without a cost", "kind": "info", "ok": True, "detail": {k: v for k, v in self.no_cost.items() if v}})
        lines, results = [], []
        for prg in sorted(self.counts):
            e, s = self.exact.get(prg, {}), self.floats.get(prg, {})
            lines.append(
                {
                    "prg_id": prg,
                    "pa": code,
                    "program_costs": {f: str(e.get(f"Wtd{f}", Decimal(0))) for f, _ in PROGRAM_COST_FIELDS},
                    "rebates_and_incentives": sum((e.get(c, Decimal(0)) for c in INCENTIVE_OUTPUTS), Decimal(0)).quantize(Decimal("0.01")),
                    "claims": self.counts[prg],
                    "filed": {f: sum(s.get(c, 0.0) for c in cols) for f, cols in FILED.items()},
                }
            )
            results.append(
                {"pa": code, "prg_id": prg, "program_name": self.names.get(prg, ""), "measures": self.counts[prg], "values": dict(s)}
            )
        return lines, results, checks


def _flag(text) -> bool:
    return (text or "").strip().lower() in tables.TRUE


RECORD_COLUMNS = [
    "ClaimID",
    "PrgID",
    "MeasImpactType",
    "ParentClaimID",
    "NMEC_TU_Flag",
    "ParticipantID",
    "SEM_Phase",
    "ProjectID",
    "SiteID",
]


def _repeats(claim_parquet: Path) -> tuple[set[str], list[str], int]:
    """From the written Claim table's ClaimIDs: the repeated ones (lower case), each as filed at its second appearance, in order, and
    the number of records with none. Only the repeated IDs are held, never every ClaimID."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    ids = pc.utf8_lower(pc.utf8_trim_whitespace(pc.fill_null(pq.read_table(claim_parquet, columns=["ClaimID"])["ClaimID"], "")))
    blank = int(pc.sum(pc.equal(ids, "").cast("int64")).as_py() or 0)
    counts = pc.value_counts(ids.filter(pc.not_equal(ids, "")))
    repeated = {v for v, n in zip(counts.field("values").to_pylist(), counts.field("counts").to_pylist(), strict=True) if n > 1}
    del ids, counts
    seen, repeats = defaultdict(int), []
    for b in pq.ParquetFile(claim_parquet).iter_batches(batch_size=BATCH_ROWS, columns=["ClaimID"]):
        for cid in b.column(0).to_pylist():
            cid = (cid or "").strip()
            if cid.lower() in repeated:
                seen[cid.lower()] += 1
                if seen[cid.lower()] == 2:
                    repeats.append(cid)
    return repeated, repeats, blank


def _records(rows, repeated: set[str], seen: dict[str, int]) -> list[dict]:
    """A batch's ClaimRecords (a repeated ClaimID gets "#2", "#3" ... in file order; `seen` carries the counts across batches)."""
    data = {c: rows[c].to_pylist() if c in rows.column_names else [None] * rows.num_rows for c in RECORD_COLUMNS}
    filed_values = {f: _row_sums(rows, cs) for f, cs in FILED.items()}
    out = []
    for i in range(rows.num_rows):
        cid = (data["ClaimID"][i] or "").strip()
        if not cid:
            continue
        n = 1
        if cid.lower() in repeated:
            seen[cid.lower()] += 1
            n = seen[cid.lower()]
        out.append(
            {
                "claim_id": cid if n == 1 else f"{cid}#{n}",
                "prg_id": (data["PrgID"][i] or "").strip()[:60],
                "impact_type": (data["MeasImpactType"][i] or "").strip()[:60],
                "parent_claim_id": (data["ParentClaimID"][i] or "").strip(),
                "true_up": _flag(data["NMEC_TU_Flag"][i]),
                "participant_id": (data["ParticipantID"][i] or "").strip(),
                "sem_phase": (data["SEM_Phase"][i] or "").strip(),
                "project_id": (data["ProjectID"][i] or "").strip(),
                "site_id": (data["SiteID"][i] or "").strip(),
                "filed": {f: filed_values[f][i] for f in FILED},
            }
        )
    return out


def _row_sums(rows, columns: list[str]) -> list[float]:
    """Per record, the sum of the columns present (blanks as 0)."""
    import pyarrow as pa
    import pyarrow.compute as pc

    total = pa.array([0.0] * rows.num_rows, pa.float64())
    for c in columns:
        if c in rows.column_names:
            total = pc.add(total, pc.fill_null(pc.cast(tables.blank_to_null(rows[c]), pa.float64()), 0.0))
    return total.to_pylist()

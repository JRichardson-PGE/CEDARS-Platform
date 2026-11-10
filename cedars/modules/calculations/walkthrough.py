"""A step-by-step walkthrough of how one measure of a finished run was found cost-effective (or not), in plain words.

The person picks any measure of the run (any size of run); the page then follows that measure from its inputs to its cost-test ratios
in ten steps, using the run's own numbers. Everything shown is read from the measure-level table the engine keeps beside the outputs
(`out/[run_acc<ACC>/]_results/measure_level_*.parquet`, every column even when an output template narrowed the downloadable file),
from the run's `run_settings.json` (the First Year and the ACC whose run parameters were used) and from the run's cost-test set
(`cost_tests.yaml`, else the built-in set it names). Where a step shows arithmetic (units x savings x net-to-gross, the sum of a
test's benefits, a ratio), the result is set beside the engine's own value for the same output and the page says in words whether
they agree: the walkthrough explains the engine's numbers, it never prices anything itself. The formulas are the ones in ReCET's
Calculations document (shown under Help), which the engine's tests hold its code to.

Nothing here is needed to price a run; without the engine installed the page says so."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from cedars.core import storage

from . import results as results_mod
from .models import Run

PICK_LIMIT = 25  # measures listed by the picker at a time
TOLERANCE = 1e-6  # relative (and absolute, in the output's units) difference allowed between the arithmetic shown and the output

STEPS = [
    ("measure", "The measure"),
    ("savings", "Energy saved in a year"),
    ("life", "How long the savings last"),
    ("timing", "When the savings happen"),
    ("discounting", "Why later dollars count for less"),
    ("value", "What the savings are worth"),
    ("costs", "What the measure costs"),
    ("program", "Its share of the program's costs"),
    ("tests", "The cost tests"),
    ("summary", "The verdict"),
]
STEP_KEYS = [k for k, _ in STEPS]

# Plain-language names of the value streams a cost-test set may use (a stream not listed is named by its own definition).
STREAM_WORDS = {
    "ElecBen": "Value of the electricity saved",
    "GasBen": "Value of the gas saved",
    "WaterEnergyBen": "Value of the energy embedded in the water saved",
    "OtherBen": "Other benefits (gas infrastructure, refrigerant, other)",
    "TaxCredits": "Tax credits",
    "TaxCreditsGrossPV": "Tax credits, in present value",
    "RebatesandIncentsPV": "Rebates, incentives and direct install paid by the program",
    "NetParticipantCostAdjustedPV": "The customer's net cost of the measure",
    "NetParticipantCostPV": "The customer's net cost of the measure",
    "GrossParticipantCostPV": "The measure's cost before any incentive",
    "ElecSupplyCost": "Cost of supplying added electricity",
    "GasSupplyCost": "Cost of supplying added gas",
    "WaterEnergyCost": "Cost of the energy embedded in added water use",
    "OtherCost": "Other costs (refrigerant, other)",
    "LifecycleBillReducElec": "Electric bill revenue the utility no longer collects",
    "LifecycleBillReducGas": "Gas bill revenue the utility no longer collects",
    "LifecycleBillIncrElec": "Added electric bill revenue",
    "LifecycleBillIncrGas": "Added gas bill revenue",
    "LifecycleBillReducElecGross": "The customer's electric bill savings",
    "LifecycleBillReducGasGross": "The customer's gas bill savings",
    "LifecycleBillIncrElecGross": "The customer's electric bill increases",
    "LifecycleBillIncrGasGross": "The customer's gas bill increases",
}

# The question each familiar test answers, in plain words (a test of a custom set is described by its own definition).
TEST_QUESTIONS = {
    "TRC": "Is the measure worth it for the region as a whole? It weighs the energy system's savings against everyone's costs: what the "
    "program spends and what the customer pays.",
    "PAC": "Is the measure a good use of the program's budget? It weighs the same savings against only what the program administrator "
    "spends: incentives and the program's own costs.",
    "RIM": "What does the measure do to rates? It counts the bill revenue the utility no longer collects as a cost, because the "
    "customers who did not take part make it up through rates.",
    "SCB": "Is the measure worth it for society? The same as the TRC, but on avoided costs that value greenhouse gases and air quality "
    "more fully (the base case), discounted at the lower societal rate.",
    "SCH": "The societal test again, with the higher value of greenhouse gases (the high case).",
    "PCT": "Is the measure worth it for the customer who installs it? Their bill savings, incentives and tax credits against what the "
    "measure costs them.",
}


@dataclass
class Calc:
    """One piece of arithmetic shown on a step: `expr` = `result`, set beside the engine's value of `output` when there is one."""

    label: str
    expr: str
    result: str
    output: str = ""  # the output column the result is checked against
    output_value: str = ""
    ok: bool | None = None  # None: nothing to check against
    known: str = ""  # when it differs: the CET error the replica keeps that explains it


@dataclass
class Fact:
    label: str
    value: str
    column: str = ""


@dataclass
class Term:
    """One stream in a cost test's benefits or costs."""

    label: str
    column: str
    value: float | None
    shown: str


@dataclass
class TestCard:
    id: str
    label: str
    question: str
    benefits: list[Term]
    benefits_zero: list[str]
    costs: list[Term]
    costs_zero: list[str]
    program_part: str  # the measure's share of the program costs in this test's dollars ("" when the test has none)
    calcs: list[Calc]
    ratio: float | None
    ratio_text: str
    verdict: str
    note: str = ""


@dataclass
class Step:
    key: str
    title: str
    number: int
    plain: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    calcs: list[Calc] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    table: dict | None = None  # {"caption", "columns", "rows"} for a small table of numbers
    tests: list[TestCard] = field(default_factory=list)
    charts: list = field(default_factory=list)  # walkchart.Chart: how the step's numbers build up over the life


@dataclass(frozen=True)
class Engine:
    """The calculation that priced the run: ReCET's own ("recet") or an exact replica of a past CET release ("cet-25.1")."""

    key: str = "recet"
    cet_version: str = ""  # the CET Version (avoided-cost version) a replica priced on, from run_settings.json

    @property
    def replica(self) -> bool:
        return self.key.startswith("cet-")

    @property
    def release(self) -> str:
        return self.key.removeprefix("cet-") if self.replica else ""

    @property
    def label(self) -> str:
        return f"CET {self.release} replica" if self.replica else "ReCET"

    @classmethod
    def of(cls, run: Run | None = None, settings: dict | None = None) -> Engine:
        rec = (settings or {}).get("engine") or {}
        key = (run.calculation if run is not None else "") or text(rec.get("key")) or "recet"
        return cls(key, text(rec.get("cet_version")))


RECET = Engine()

# The CET errors a replica keeps that show up in the arithmetic these pages check: output -> (releases, what CET does). A difference on
# one of these outputs, on one of these releases, is labeled as that error instead of a disagreement.
KNOWN_CET_ERRORS = {
    "LevBenElec": (
        {"25.0", "25.1"},
        "CET 25.0 and 25.1 write this column from the wrong place in a column list, so it holds another levelized value",
    ),
    "LifecycleNetKWhSite": ({"24.1"}, "CET 24.1 multiplies an early replacement's lifetime kWh by the life a second time"),
}

# A replica writes some outputs under the names its release used. Each name the steps read -> the release names, first match wins.
ALIASES = {
    "IOU_AC_Territory": ["PA"],
    "LifecycleNetKWhSite": ["LifecycleNetkWhSite", "LifecycleNetKWh"],
    "AnnualNetKWhSite": ["AnnualNetkWhSite"],
    "FirstYearNetKWhSite": ["FirstYearNetkWhSite", "FirstYearNetKWh"],
    "FirstYearNetKW": ["FirstYearNetkW"],
}


class NoWalkthrough(Exception):
    """The run has no measure-level table to walk through; the message says why."""


# ------------------------------------------------------------------------------------------------------------------- formatting


def num(v) -> float | None:
    """A cell as a float, None for a blank, NaN or text."""
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    return str(v).strip()


def dollars(v) -> str:
    v = num(v)
    if v is None:
        return "–"
    sign = "−" if v < 0 else ""
    a = abs(v)
    return f"{sign}${a:,.0f}" if a >= 100 else f"{sign}${a:,.2f}"


def units(q) -> str:
    return f"{quantity(q)} unit" + ("" if num(q) == 1 else "s")


def quantity(v, unit: str = "") -> str:
    v = num(v)
    if v is None:
        return "–"
    a = abs(v)
    s = f"{v:,.0f}" if a >= 1000 else (f"{v:,.2f}" if a >= 10 else f"{v:,.4g}")
    return f"{s} {unit}".strip().replace("-", "−")


def factor(v) -> str:
    v = num(v)
    if v is None:
        return "–"
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s.replace("-", "−")


def ratio_text(v) -> str:
    v = num(v)
    return "–" if v is None else f"{v:,.2f}"


def percent(v, places: int = 1) -> str:
    v = num(v)
    return "–" if v is None else f"{v * 100:.{places}f}%"


def years(v) -> str:
    v = num(v)
    if v is None:
        return "–"
    s = factor(v)
    return f"{s} year" + ("" if v == 1 else "s")


def agrees(computed: float | None, output: float | None) -> bool | None:
    if computed is None or output is None:
        return None
    return abs(computed - output) <= TOLERANCE * max(1.0, abs(output))


# ------------------------------------------------------------------------------------------------------------- reading the run


def _measure_key(run: Run, part: str) -> str:
    key = results_mod.sources(run, part)["measures"] if results_mod.engine() else None
    if not key:
        raise NoWalkthrough("This run kept no measure-level table, so there is no measure to walk through.")
    return key


def _check(run: Run):
    if results_mod.engine() is None:
        raise NoWalkthrough(
            "The walkthrough reads the run's measures with the ReCET engine package, which is not installed on this server."
        )
    if run.status != Run.Status.SUCCEEDED:
        raise NoWalkthrough("The walkthrough is offered once the run has finished.")


def measure_path(run: Run, acc: str | None):
    """A local copy of the run's kept measure-level parquet (fetched once, as the Results pages do)."""
    _check(run)
    part = results_mod._part(run, acc)
    return results_mod._fetch(run, _measure_key(run, part))


def choices(run: Run, acc: str | None, q: str = "", limit: int = PICK_LIMIT) -> tuple[list[dict], int]:
    """The measures the picker lists: those whose ID, description, program or measure code contain `q` (all when blank), the first
    `limit` of them in file order, and how many match."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    path = measure_path(run, acc)
    have = set(pq.read_schema(path).names)
    if "CEInputID" not in have:
        raise NoWalkthrough("This run's measure-level table has no CEInputID column.")
    cols = [
        c for c in ("CEInputID", "MeasDescription", "PrgID", "MeasCode", "ClaimYearQuarter", "IOU_AC_Territory", "TRCRatio") if c in have
    ]
    t = pq.read_table(path, columns=cols)
    q = q.strip().lower()
    if q:
        mask = None
        for c in ("CEInputID", "MeasDescription", "PrgID", "MeasCode"):
            if c in cols:
                m = pc.match_substring(pc.utf8_lower(pc.cast(t[c], "string")), q)
                m = pc.fill_null(m, False)
                mask = m if mask is None else pc.or_(mask, m)
        t = t.filter(mask)
    total = t.num_rows
    rows = t.slice(0, limit).to_pylist()
    out = []
    for r in rows:
        out.append(
            {
                "id": text(r.get("CEInputID")),
                "description": text(r.get("MeasDescription")),
                "program": text(r.get("PrgID")),
                "quarter": text(r.get("ClaimYearQuarter")),
                "pa": text(r.get("IOU_AC_Territory")),
                "trc": ratio_text(r.get("TRCRatio")) if "TRCRatio" in r else "",
            }
        )
    return out, total


def measure_row(run: Run, acc: str | None, measure: str) -> tuple[dict, int] | None:
    """The measure's row of the kept measure-level table, and how many rows carry its ID (one, except in a run on a CET replica fed a
    repeated CEInputID). None when no row has it."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    path = measure_path(run, acc)
    f = pq.ParquetFile(path)
    found, n = None, 0
    for i in range(f.num_row_groups):
        ids = f.read_row_group(i, columns=["CEInputID"])["CEInputID"]
        hits = pc.indices_nonzero(pc.fill_null(pc.equal(pc.cast(ids, "string"), measure), False))
        if len(hits):
            n += len(hits)
            if found is None:
                found = f.read_row_group(i).slice(hits[0].as_py(), 1).to_pylist()[0]
    return (found, n) if found is not None else None


def run_settings(run: Run, acc: str | None) -> dict:
    """The run's run_settings.json ({} when it has none)."""
    raw = results_mod._out(run, results_mod._part(run, acc) + "run_settings.json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def cost_test_set(run: Run, acc: str | None, row: dict):
    """The run's cost-test set: its own cost_tests.yaml, else the built-in set its Used_CostTestSet names, else the set in force for the
    measure's program year. None when none can be read (a CET replica's run has no set)."""
    from recet import cost_test_sets

    part = results_mod._part(run, acc)
    key = run.storage_prefix + "out/" + part + "cost_tests.yaml"
    if storage.store("results").exists(key):
        try:
            return cost_test_sets.read_file(results_mod._fetch(run, key), source=text(row.get("Used_CostTestSet")) or "run")
        except Exception:  # noqa: BLE001 - an unreadable file falls back to the named set
            pass
    named = text(row.get("Used_CostTestSet")).removesuffix(cost_test_sets.DRAFT_LABEL)
    try:
        if named in cost_test_sets.builtin():
            return cost_test_sets.load(named)
        year = num(row.get("Used_Year"))
        return cost_test_sets.in_force(int(year) if year else None)
    except Exception:  # noqa: BLE001
        return None


def replica_rates(row: dict, engine: Engine) -> dict | None:
    """The discount rates a CET replica priced the measure at: its release's E3Settings row for the measure's PA at the run's CET Version
    (shipped with the engine). None when there is none: the release then priced it as CET does, without settings."""
    if not engine.cet_version:
        return None
    pa = text(row.get("IOU_AC_Territory") or row.get("PA")).casefold()
    try:
        import csv
        from importlib import resources

        path = resources.files("recet") / "data" / "cet_versions" / engine.release / "E3Settings.csv"
        with path.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if text(r.get("PA")).casefold() == pa and text(r.get("Version")) == engine.cet_version:
                    out = {k: num(r.get(k)) for k in ("DiscountRateAnnual", "DiscountRateQtr", "DiscountRateAnnual_SCT")}
                    return out if out["DiscountRateAnnual"] and out["DiscountRateQtr"] else None
    except (OSError, ModuleNotFoundError):
        return None
    return None


def discount_rates(row: dict, settings: dict) -> dict | None:
    """The discount rates the measure was priced at: the built-in run parameters of the run's ACC for the measure's territory. None
    when the run supplied its own run-parameters file (its rates are in that file) or the rates cannot be found."""
    if (settings.get("supplied_files") or {}).get("run_parameters"):
        return None
    acc = (settings.get("acc") or {}).get("settings_acc_version")
    pa = text(row.get("IOU_AC_Territory"))
    if not acc or not pa:
        return None
    try:
        from recet.pipeline import discount_settings

        return discount_settings.get_settings(pa, int(acc))
    except Exception:  # noqa: BLE001 - an unknown PA or ACC: the page explains without the numbers
        return None


# -------------------------------------------------------------------------------------------------------------------- the steps


class Walk:
    """The ten steps for one measure (`row`: its measure-level row)."""

    def __init__(self, row: dict, settings: dict, cset=None, repeated: int = 1, engine: Engine = RECET):
        self.engine = engine
        self.r = dict(row)
        self.names: dict[str, str] = {}  # a name the steps read -> the column the replica wrote it under
        for name, alts in ALIASES.items():
            if num(self.r.get(name)) is None and not text(self.r.get(name)):
                for a in alts:
                    if a in self.r and (num(self.r[a]) is not None or text(self.r[a])):
                        self.r[name] = self.r[a]
                        self.names[name] = a
                        break
        self.settings = settings
        self.cset = cset
        self.repeated = repeated
        self.rates = replica_rates(self.r, engine) if engine.replica else discount_rates(self.r, settings)
        fy = (settings.get("inputs") or {}).get("first_year")
        self.first_year = int(fy) if num(fy) is not None else None

    # cell access
    def g(self, col: str) -> float | None:
        return num(self.r.get(col))

    def has(self, col: str) -> bool:
        return col in self.r and self.g(col) is not None

    def s(self, col: str) -> str:
        return text(self.r.get(col))

    def z(self, col: str) -> float:
        return self.g(col) or 0.0

    def check(self, label: str, expr: str, value: float | None, result: str, output: str) -> Calc:
        out = self.g(output) if output else None
        c = Calc(label, expr, result, output, "" if out is None else self._like(result, out), agrees(value, out))
        return self.label_known(c)

    def label_known(self, c: Calc) -> Calc:
        releases, words = KNOWN_CET_ERRORS.get(c.output, (set(), ""))
        if c.ok is False and self.engine.release in releases:
            c.known = words
        return c

    @staticmethod
    def _like(result: str, out: float) -> str:
        """The output value formatted as the result is (dollars, a ratio, a quantity)."""
        if result.lstrip("−").startswith("$"):
            return dollars(out)
        unit = result.split(" ", 1)[1] if " " in result else ""
        if not unit and "." in result and len(result.split(".")[-1]) == 2:
            return ratio_text(out)
        return quantity(out, unit)

    # facts
    @property
    def rul(self) -> float:
        """The RUL the pricing uses: 0 when it equals or exceeds the EUL (a single baseline)."""
        eul, rul = self.z("EUL_Yrs"), self.z("RUL_Yrs")
        return 0.0 if rul >= eul else rul

    @property
    def dual(self) -> bool:
        return self.rul > 0

    @property
    def claim(self) -> tuple[int, int] | None:
        cq = self.s("ClaimYearQuarter")
        if len(cq) >= 6 and cq[:4].isdigit() and cq[-1].isdigit():
            return int(cq[:4]), int(cq[-1])
        return None

    # 1
    def step_measure(self, st: Step) -> Step:
        desc = self.s("MeasDescription") or self.s("MeasCode") or "this measure"
        program = self.s("Prg_ProgramName") or self.s("PrgID")
        st.plain = [
            f"This walkthrough follows one record of the run, {self.s('CEInputID')}: {desc}. Every number on these pages is the run's own "
            "number for this measure. Where a step shows a sum or a product, the page sets the result beside the value the engine wrote "
            "and says whether they agree.",
            "A cost-effectiveness calculation asks one question several ways: over the measure's whole life, are the costs it avoids "
            "for the energy system worth more than what it costs? The steps that follow build the answer up: how much energy the measure "
            "saves, for how long, at which hours, what that energy is worth, what the measure and its program cost, and finally the "
            "ratios of benefits to costs that the cost tests report.",
        ]
        f = [
            Fact("Measure", desc, "MeasDescription"),
            Fact("Record ID", self.s("CEInputID"), "CEInputID"),
            Fact("Program", f"{program} ({self.s('PrgID')})" if program != self.s("PrgID") else program, "PrgID"),
            Fact("Program administrator credited", self.s("CreditedPA") or self.s("Prg_PA"), "CreditedPA"),
            Fact("Utility territory (avoided costs and rates)", self.s("IOU_AC_Territory"), "IOU_AC_Territory"),
            Fact("Sector", self.s("Sector"), "Sector"),
            Fact("Claimed in", self.s("ClaimYearQuarter"), "ClaimYearQuarter"),
            Fact("Units installed", f"{quantity(self.g('NumUnits'))} {self.s('NormUnit')}".strip(), "NumUnits"),
            Fact("Avoided costs used", self.s("Used_ACCVersion"), "Used_ACCVersion"),
            Fact("Cost-test set", self.s("Used_CostTestSet"), "Used_CostTestSet"),
            Fact("Calculation engine", self.engine.label + (f", CET Version {self.engine.cet_version}" if self.engine.cet_version else "")),
        ]
        st.facts = [x for x in f if x.value and x.value != "–"]
        if self.engine.replica:
            st.plain.append(
                f"This run was priced by the {self.engine.label}: CET {self.engine.release}'s own calculation reproduced exactly, on that "
                "release's quarterly avoided-cost tables, its errors included, so that past results can be re-run and compared. The steps "
                "follow what that release does, and where it differs from ReCET's own calculation they say so."
            )
            errors = [w for out, (rels, w) in KNOWN_CET_ERRORS.items() if self.engine.release in rels]
            if errors:
                st.notes.append(
                    f"Where a step's arithmetic differs from the replica's value because of an error CET {self.engine.release} makes, the "
                    "step names the error instead of marking a disagreement. Every error the replicas keep is listed in ReCET's "
                    "Calculations document, under prior CET releases."
                )
        if self.repeated > 1:
            st.notes.append(f"{self.repeated} rows of this run carry the ID {self.s('CEInputID')}; this walkthrough follows the first.")
        return st

    # 2
    def step_savings(self, st: Step) -> Step:
        st.plain = [
            "The inputs give the savings of one unit, before any adjustment: the gross savings, as an engineer would estimate them. "
            "Three adjustments turn them into the savings the program is credited with:",
        ]
        st.facts = [
            Fact("Net-to-gross ratio (kWh)", factor(self.g("NTGRkWh")), "NTGRkWh"),
            Fact("Installation rate (kWh)", factor(self.g("InstallationRatekWh")), "InstallationRatekWh"),
            Fact("Realization rate (kWh)", factor(self.g("RealizationRatekWh")), "RealizationRatekWh"),
        ]
        st.notes.append(
            "Net-to-gross: the share of the savings the program caused. Some customers would have installed the measure anyway "
            "(free riders), so only the rest counts."
        )
        st.notes.append("Installation rate: the share of units that were actually installed and kept.")
        st.notes.append("Realization rate: the share of the claimed savings that evaluation confirms.")
        q = self.z("NumUnits")
        scaled = bool(self.r.get("Scaled_Profile_Flag"))
        for fuel, unit, per, ntg, ir, rr, out, first_year in (
            ("Electricity", "kWh", "UnitkWh", "NTGRkWh", "InstallationRatekWh", "RealizationRatekWh", "AC_Net_kWh", "FirstYearNetKWhSite"),
            (
                "Gas",
                "therms",
                "UnitTherm",
                "NTGRTherm",
                "InstallationRateTherm",
                "RealizationRateTherm",
                "AC_Net_Therms",
                "FirstYearNetThm",
            ),
        ):
            for b, name in (("1st", "first"), ("2nd", "second")):
                if b == "2nd" and not self.dual:
                    continue
                col = f"{out}_{b}_Baseline"
                if self.engine.replica:
                    if not self.g(f"{per}{b}Baseline"):
                        continue
                elif not self.has(col) or (not self.g(col) and not self.g(f"{per}{b}Baseline")):
                    continue
                unit_saving = self.g(f"Used_{per}{b}Baseline") if scaled and fuel == "Electricity" else self.g(f"{per}{b}Baseline")
                parts = [self.z(x) for x in (ntg, ir, rr)]
                ntg_words = "net-to-gross"
                if self.engine.replica and not self.has(col) and self.market_effects and parts[0]:
                    # a replica's first-year savings carry the market effects: CET adds them to the net-to-gross ratio
                    parts[0] += self.market_effects
                    ntg_words = "net-to-gross with market effects"
                value = q * (unit_saving or 0.0) * parts[0] * parts[1] * parts[2]
                label = f"{fuel}, net savings in a year" + (f" ({name} baseline)" if self.dual else "")
                expr = (
                    f"{units(q)} × {quantity(unit_saving, unit)} a unit × {factor(parts[0])} {ntg_words} × "
                    f"{factor(parts[1])} installed × {factor(parts[2])} realized"
                )
                if self.has(col):
                    st.calcs.append(self.check(label, expr, value, quantity(value, unit), col))
                elif b == "1st" and self.has(first_year):
                    st.calcs.append(self.check(label, expr, value, quantity(value, unit), first_year))
                else:
                    st.calcs.append(Calc(label, expr, quantity(value, unit)))
        if self.has("AC_Net_kWh_Water_1st_Baseline") and self.g("AC_Net_kWh_Water_1st_Baseline"):
            st.facts.append(
                Fact(
                    "Electricity embedded in the water saved, net, a year",
                    quantity(self.g("AC_Net_kWh_Water_1st_Baseline"), "kWh"),
                    "AC_Net_kWh_Water_1st_Baseline",
                )
            )
            st.notes.append(
                "Saving water also saves the electricity used to pump, treat and heat it; the inputs give it per unit, and it is "
                "adjusted the same way."
            )
        if self.has("FirstYearNetKW") and self.g("FirstYearNetKW"):
            st.facts.append(Fact("Peak demand reduction, net", quantity(self.g("FirstYearNetKW"), "kW"), "FirstYearNetKW"))
        if scaled:
            st.notes.append(
                "This measure's load shape holds the kWh saved per unit in each hour, so the savings a unit are the shape's own total "
                "(Used_UnitkWh1stBaseline), not the input's UnitkWh1stBaseline."
            )
        if self.dual:
            st.notes.append(
                "This is an early replacement: the old equipment still worked. For its remaining useful life the savings are measured "
                "against the old equipment (the first baseline); after that, against what the customer would have had to buy anyway "
                "(the second baseline)."
            )
        if bool(self.r.get("Fuel_Sub_Flag")):
            st.notes.append(
                "This is a fuel-substitution measure: it switches from one fuel to another, so one fuel's savings can be negative "
                "(added load)."
            )
        if not st.calcs:
            st.notes.append("This measure has no electric or gas savings in its inputs.")
        return st

    # 3
    def step_life(self, st: Step) -> Step:
        eul = self.g("EUL_Yrs")
        st.plain = [
            "Savings continue for as long as the measure keeps working: its effective useful life (EUL). The lifetime savings are a "
            "year's savings times the years of life.",
        ]
        st.facts = [Fact("Effective useful life", years(eul), "EUL_Yrs")]
        if self.dual:
            st.facts.append(Fact("Remaining useful life of the old equipment", years(self.rul), "RUL_Yrs"))
        claim = self.claim
        if claim:
            st.facts.append(Fact("Savings start", f"in the claim quarter, Q{claim[1]} {claim[0]}", "ClaimYearQuarter"))
        for unit, _fuel, first, second, out, lab in (
            (
                "kWh",
                "Electricity",
                "AC_Net_kWh_1st_Baseline",
                "AC_Net_kWh_2nd_Baseline",
                "LifecycleNetKWhSite",
                "Electricity saved over the measure's life",
            ),
            (
                "therms",
                "Gas",
                "AC_Net_Therms_1st_Baseline",
                "AC_Net_Therms_2nd_Baseline",
                "LifecycleNetThm",
                "Gas saved over the measure's life",
            ),
        ):
            if not self.has(out) or not self.g(out):
                continue
            # LifecycleNet = annual net (with any market effects) x the years of life; the annual net is AnnualNet<x> = LifecycleNet / EUL.
            electric = first.startswith("AC_Net_kWh")
            meb = self._market_factor(electric)
            a1, a2 = self._annual_net(first, electric, "1st") * meb, self._annual_net(second, electric, "2nd") * meb
            raw_rul = self.z("RUL_Yrs")
            if self.engine.replica and raw_rul > self.z("EUL_Yrs") > 0:
                # CET counts the first baseline for the whole RUL when the RUL is longer than the EUL
                value = a1 * raw_rul
                expr = f"{quantity(a1, unit)} a year × {years(raw_rul)} (the remaining useful life, longer than the EUL)"
            elif self.dual:
                value = a1 * self.rul + a2 * (self.z("EUL_Yrs") - self.rul)
                expr = (
                    f"{quantity(a1, unit)} a year × {years(self.rul)} + {quantity(a2, unit)} a year × {years(self.z('EUL_Yrs') - self.rul)}"
                )
            else:
                value = a1 * self.z("EUL_Yrs")
                expr = f"{quantity(a1, unit)} a year × {years(eul)}"
            st.calcs.append(self.check(lab, expr, value, quantity(value, unit), out))
        if self.market_effects:
            st.notes.append(
                f"This run adds market effects of {percent(self.market_effects, 0)} to the net-to-gross ratio for the lifetime savings and "
                "the benefits (savings the program causes beyond its own participants)."
            )
        if eul is not None and eul != int(eul):
            st.notes.append(
                "A life that is not a whole number of years ends part-way through its last quarter; that quarter counts only its share."
            )
        return st

    def _annual_net(self, col: str, electric: bool, b: str) -> float:
        """A year's net savings for one baseline: the engine's AC_Net_* output, else (a replica writes none) units x the unit savings x
        net-to-gross x installation rate x realization rate."""
        if self.has(col):
            return self.z(col)
        f = (
            ("UnitkWh", "NTGRkWh", "InstallationRatekWh", "RealizationRatekWh")
            if electric
            else ("UnitTherm", "NTGRTherm", "InstallationRateTherm", "RealizationRateTherm")
        )
        return self.z("NumUnits") * self.z(f"{f[0]}{b}Baseline") * self.z(f[1]) * self.z(f[2]) * self.z(f[3])

    @property
    def market_effects(self) -> float:
        cfg = (self.settings.get("config") or {}) if self.settings else {}
        return num(cfg.get("market_effects")) or 0.0

    def _market_factor(self, electric: bool) -> float:
        ntg = self.z("NTGRkWh" if electric else "NTGRTherm")
        return (ntg + self.market_effects) / ntg if ntg and self.market_effects else 1.0

    # 4
    def step_timing(self, st: Step) -> Step:
        profile = self.s("AC_Elec_Impact_Profile")
        st.plain = [
            "A kilowatt-hour saved on a hot summer evening is worth far more than one saved on a mild spring night, because that is "
            "when the grid is strained and power is most expensive to produce and deliver. So the engine spreads the year's savings "
            "over all 8,760 hours of the year, using a load shape (an impact profile) that says which share of the savings falls in "
            "each hour.",
            "Lighting saves mostly in the evening and on dark winter days; air conditioning saves on summer afternoons. The profile "
            "is picked by the measure's end use, utility territory and sector, and the climate zone picks which version of it.",
        ]
        if self.engine.replica:
            st.plain[0] = (
                "A kilowatt-hour saved on a hot summer evening is worth far more than one saved on a mild spring night, because that is "
                "when the grid is strained and power is most expensive to produce and deliver. CET does not work hour by hour: its "
                "quarterly avoided-cost tables already hold, for each load shape (impact profile), territory and climate zone, the "
                "avoided cost of a kWh saved in each quarter with the shape's hours weighted in."
            )
            st.plain[1] = (
                "So the load shape still decides what the savings are worth, but through the release's tables: the replica looks up "
                "the row for the measure's shape, territory and climate zone for each quarter of its life."
            )
        if profile:
            st.facts.append(Fact("Electric load shape", profile, "AC_Elec_Impact_Profile"))
            st.facts.append(Fact("Target sector of the shape", self.s("AC_Elec_Target_Sector"), "AC_Elec_Target_Sector"))
        zone = self.s("AC_Climate_Zone")
        if zone:
            st.facts.append(Fact("Climate zone", zone, "AC_Climate_Zone"))
        if self.s("SystemClimateZone") and self.s("SystemClimateZone") != "None":
            st.facts.append(Fact("Zone used for a System measure", self.s("SystemClimateZone"), "SystemClimateZone"))
            st.notes.append(
                "A measure filed for the System zone is priced on the profile's real climate zone with the lowest avoided costs, so it is "
                "never overvalued."
            )
        src = self.s("ProfileSource")
        if src:
            words = {
                "library": "the CEDARS impact-profile library, matched on end use, territory and sector",
                "library (sector-loose match)": "the library, matched on end use and territory (no profile for the sector)",
                "user profile": "an 8,760 profile supplied with the run",
            }
            st.facts.append(Fact("Where the shape came from", words.get(src, src), "ProfileSource"))
        status = self.s("ElecPricingStatus")
        if status:
            st.facts.append(Fact("Electric pricing", status, "ElecPricingStatus"))
            if status.upper().startswith("UNPRICED"):
                st.notes.append(
                    "No load shape could be found for this measure, so its electricity was not priced: its electric benefits are zero."
                )
        gas = self.s("AC_Gas_Impact_Profile")
        if gas and self.z("AC_Net_Therms_1st_Baseline"):
            st.facts.append(Fact("Gas shape (months)", gas, "AC_Gas_Impact_Profile"))
            st.notes.append(
                "Gas is priced by month, not hour: the gas shape says which share of the year's gas savings falls in each month."
            )
        if self.has("ElecBen") and self.has("AC_Net_kWh_1st_Baseline") and self.z("AC_Net_kWh_1st_Baseline"):
            st.notes.append(
                "The value of the shape shows up in the next steps: the same kWh would be worth more on a shape that saves at expensive "
                "hours, and less on one that saves at cheap ones."
            )
        return st

    # 5
    def step_discounting(self, st: Step) -> Step:
        st.plain = [
            "A dollar of savings ten years from now is worth less than a dollar today: money in hand can be invested, and the utility "
            "pays to borrow. So every future dollar is discounted back to the run's First Year, at the utility's discount rate. The "
            "further away a dollar is, the less it counts. Adding up discounted dollars gives a present value (PV).",
        ]
        rates = self.rates
        if self.first_year:
            st.facts.append(Fact("First Year (dollars are counted at its start)", str(self.first_year)))
        if rates:
            r = rates["DiscountRateAnnual"]
            st.facts.append(Fact(f"Discount rate for {self.s('IOU_AC_Territory')}, a year", percent(r, 2), ""))
            st.facts.append(Fact("Societal discount rate (societal tests), a year", percent(rates["DiscountRateAnnual_SCT"], 2), ""))
            rows = []
            eul = self.z("EUL_Yrs")
            marks = sorted({1, 5, 10, 20} | ({int(math.ceil(eul))} if eul else set()))
            for n in marks:
                if eul and n > math.ceil(eul) and n != 1:
                    continue
                rows.append([f"{n} year" + ("" if n == 1 else "s"), f"{1 / (1 + r) ** n:.3f}", dollars(100 / (1 + r) ** n)])
            st.table = {
                "caption": f"What $100 is worth today when it comes later, at {percent(r, 2)} a year",
                "columns": ["Received after", "Discount factor", "$100 is worth"],
                "rows": rows,
            }
        elif (self.settings.get("supplied_files") or {}).get("run_parameters"):
            st.notes.append("This run supplied its own run parameters, so its discount rates are the ones in that file.")
        # discounted savings: the same kWh, each year discounted (C19.5), shown against the lifetime total
        if self.has("DiscountedSavingsNetkWh") and self.has("LifecycleNetKWhSite") and self.z("LifecycleNetKWhSite"):
            life, disc = self.z("LifecycleNetKWhSite"), self.z("DiscountedSavingsNetkWh")
            st.calcs.append(
                Calc(
                    "Lifetime electricity savings, discounted the same way",
                    f"{quantity(life, 'kWh')} over the life, each year's kWh discounted",
                    quantity(disc, "kWh"),
                    "DiscountedSavingsNetkWh",
                    quantity(disc, "kWh"),
                    None,
                )
            )
            check = self._discounted_check()
            if check:
                st.calcs.append(check)
            st.notes.append(
                f"So each kWh of the lifetime counts for {disc / life:.2f} of a kWh in present-value terms. Dividing a present-value "
                "benefit by these discounted kWh gives a value per kWh (next step)."
            )
        claim = self.claim
        if claim and self.first_year and rates:
            qm = (claim[0] - self.first_year) * 4 + (claim[1] - 1)
            if qm:
                st.notes.append(
                    f"One-time costs are paid in the claim quarter, {qm} quarter{'s' if qm != 1 else ''} after the start of the First "
                    f"Year, so they are discounted by {(1 + rates['DiscountRateQtr']) ** qm:.4f} (the quarterly rate, "
                    f"{percent(rates['DiscountRateQtr'], 3)})."
                )
            else:
                st.notes.append(
                    "The measure is claimed in the first quarter of the First Year, so its one-time costs are not discounted at all."
                )
        return st

    def _discounted_check(self) -> Calc | None:
        """DiscountedSavingsNetkWh from the annual net kWh and the rates, for a single-baseline measure (CALCULATIONS C19.5)."""
        if not self.rates or self.dual or not self.first_year or not self.claim:
            return None
        eul = self.z("EUL_Yrs")
        if not eul or (self.engine.replica and self.z("RUL_Yrs") > eul):
            return None
        annual = self.g("AnnualNetKWhSite")
        if annual is None and self.has("LifecycleNetKWhSite"):  # a replica of CET 21.0 or 22.1 writes no average year
            annual = self.z("LifecycleNetKWhSite") / eul
        if annual is None:
            return None
        r = self.rates["DiscountRateAnnual"]
        raf = 1 + r
        a = 1 + (1 - raf ** -(eul - 1)) / r
        y0, q0 = self.claim
        qm = (y0 - self.first_year) * 4 + (q0 - 1)
        if self.engine.release == "21.0":
            qm += 1  # CET 21.0 counts the claim quarter from 1, not 0
        pvf = (1 + self.rates["DiscountRateQtr"]) ** qm
        value = annual * a / pvf
        expr = f"{quantity(annual, 'kWh')} a year × {a:.4f} (the sum of each year's discount factor over {years(eul)})"
        if pvf != 1:
            expr += f" ÷ {pvf:.4f} (from the claim quarter back to the First Year"
            expr += ", counting the claim quarter from 1 as CET 21.0 does)" if self.engine.release == "21.0" else ")"
        c = self.check("Checked from the discount rate", expr, value, quantity(value, "kWh"), "DiscountedSavingsNetkWh")
        if self.engine.replica and c.ok is False:
            # CET rounds each discount factor to six decimal places (SQL Server's decimal arithmetic), so a replica agrees to rounding
            out = self.g("DiscountedSavingsNetkWh")
            if out is not None and abs(value - out) <= 1e-5 * max(1.0, abs(out)):
                c.ok = True
                c.expr += "; CET rounds each discount factor to six decimal places, so the two agree to that rounding"
        return c

    # 6
    def step_value(self, st: Step) -> Step:
        acc = self.s("Used_ACCVersion")
        st.plain = [
            f"The value of each saved kWh comes from the Avoided Cost Calculator (ACC){' (' + acc + ')' if acc else ''}: the cost the "
            "utility no longer has to pay to generate, buy and deliver that energy in that hour, including the value of the greenhouse "
            "gases and losses it avoids. The ACC has a price for every hour of every future year, for each utility and climate zone.",
            "For every quarter of the measure's life, the engine multiplies the kWh saved in each hour (the year's savings spread by "
            "the load shape) by that hour's avoided cost, adds them up, and discounts the quarter back to the First Year. The sum over "
            "the whole life is the electric benefit.",
        ]
        if self.engine.replica:
            st.plain = [
                f"The value of each saved kWh comes from the avoided costs in CET {self.engine.release}'s tables"
                + (f" (CET Version {self.engine.cet_version})" if self.engine.cet_version else "")
                + ": the cost the utility no longer has to pay to generate, buy and deliver that energy, including the value of the "
                "greenhouse gases and losses it avoids, for each quarter of every future year.",
                "For every quarter of the measure's life, the replica multiplies a quarter's share of the year's savings by that "
                "quarter's avoided cost for the measure's load shape, territory and climate zone, and discounts the quarter back to the "
                "First Year. The sum over the whole life is the electric benefit.",
            ]
        if self.has("ElecBen"):
            st.facts.append(Fact("Electric benefit over the life (present value)", dollars(self.g("ElecBen")), "ElecBen"))
            if self.has("DiscountedSavingsNetkWh") and self.z("DiscountedSavingsNetkWh"):
                v = self.z("ElecBen") / self.z("DiscountedSavingsNetkWh")
                st.calcs.append(
                    self.check(
                        "Average value of a saved kWh",
                        f"{dollars(self.g('ElecBen'))} ÷ {quantity(self.g('DiscountedSavingsNetkWh'), 'discounted kWh')}",
                        v,
                        f"${v:,.4f} per kWh",
                        "LevBenElec",
                    )
                )
                st.calcs[-1].output_value = f"${self.z('LevBenElec'):,.4f} per kWh" if self.has("LevBenElec") else ""
                st.notes.append(f"About {v * 100:.1f} cents for each kWh this measure saves, in present-value terms.")
        if self.has("GasBen") and self.g("GasBen"):
            st.facts.append(Fact("Gas benefit over the life (present value)", dollars(self.g("GasBen")), "GasBen"))
            if self.has("DiscountedSavingsNetThm") and self.z("DiscountedSavingsNetThm"):
                v = self.z("GasBen") / self.z("DiscountedSavingsNetThm")
                st.calcs.append(
                    self.check(
                        "Average value of a saved therm",
                        f"{dollars(self.g('GasBen'))} ÷ {quantity(self.g('DiscountedSavingsNetThm'), 'discounted therms')}",
                        v,
                        f"${v:,.4f} per therm",
                        "LevBenGas",
                    )
                )
                st.calcs[-1].output_value = f"${self.z('LevBenGas'):,.4f} per therm" if self.has("LevBenGas") else ""
            st.notes.append("Gas is valued the same way, month by month, on the ACC's gas avoided costs.")
        for col, words in (
            ("WaterEnergyBen", "Value of the energy embedded in the water saved"),
            ("ElecSupplyCost", "Cost of supplying added electricity"),
            ("GasSupplyCost", "Cost of supplying added gas"),
            ("WaterEnergyCost", "Cost of the energy embedded in added water use"),
        ):
            if self.has(col) and self.g(col):
                st.facts.append(Fact(words, dollars(self.g(col)), col))
        if self.z("ElecSupplyCost") or self.z("GasSupplyCost"):
            st.notes.append(
                "A fuel-substitution measure adds load on the fuel it switches to; that added load is priced the same way and counted "
                "as a supply cost, not a negative benefit."
            )
        sb, sh = self.g("ElecBen_SB"), self.g("ElecBen_SH")
        if sb is not None or sh is not None:
            st.facts.append(Fact("Electric benefit, societal base case", dollars(sb), "ElecBen_SB"))
            st.facts.append(Fact("Electric benefit, societal high case", dollars(sh), "ElecBen_SH"))
            st.notes.append(
                "The societal tests price the same savings on the ACC's societal avoided costs, which value greenhouse gases and air "
                "quality more fully, and discount them at the lower societal rate, so their benefits are usually higher."
            )
        elif self.has("ElecBen"):
            st.notes.append("This ACC has no societal avoided costs, so the societal tests are not priced.")
        if self.has("TotalSystemBenefit"):
            st.calcs.append(self._aggregate_calc("TotalSystemBenefit", "Total system benefit"))
        return st

    def _aggregate_calc(self, name: str, label: str) -> Calc:
        cset = self.cset
        if cset is None or name not in cset.aggregates:
            return Calc(label, "the benefits less the supply and other costs", dollars(self.g(name)), name, dollars(self.g(name)), None)
        a = cset.aggregates[name]
        plus = [cset.column(s, a.pricing) for s in a.plus]
        minus = [cset.column(s, a.pricing) for s in a.minus]
        shown = [c for c in plus + minus if self.z(c)]
        value = sum(self.z(c) for c in plus) - sum(self.z(c) for c in minus)
        parts = [("− " if c in minus else "+ ") + dollars(self.g(c)) + f" ({self._words(c)})" for c in shown]
        expr = " ".join(parts).removeprefix("+ ") if parts else "nothing: every stream is zero"
        return self.check(label, expr, value, dollars(value), name)

    def _words(self, column: str) -> str:
        cset = self.cset
        if cset is not None:
            for s in cset.streams.values():
                for p in s.pricings or (next(iter(cset.pricings)),):
                    try:
                        if cset.column(s.name, p) == column:
                            return STREAM_WORDS.get(s.name, s.definition or s.name).rstrip(".").lower()
                    except KeyError:
                        continue
        return column

    # 7
    def step_costs(self, st: Step) -> Step:
        st.plain = [
            "On the cost side, the starting point is what the measure costs to buy and install. The program pays part of it as rebates "
            "and incentives; the customer pays the rest.",
        ]
        q = self.z("NumUnits")
        if self.has("GrossMeasureCost"):
            unit_col = "UnitMeaCost2ndBaseline" if self.dual else "UnitMeaCost1stBaseline"
            value = q * self.z(unit_col)
            st.calcs.append(
                self.check(
                    "Cost of the measure",
                    f"{units(q)} × {dollars(self.g(unit_col))} a unit",
                    value,
                    dollars(value),
                    "GrossMeasureCost",
                )
            )
            if self.dual:
                st.notes.append(
                    "For an early replacement the cost counted is the cost of the equipment the customer would have bought anyway at the "
                    "end of the old one's life (the second baseline), less the value of buying it early."
                )
        if self.has("RebatesandIncents"):
            cols = ["UnitEndUserRebate", "UnitIncentiveToOthers", "UnitDirectInstallLab", "UnitDirectInstallMat"]
            per = sum(self.z(c) for c in cols)
            value = q * per
            named = [
                f"{dollars(self.g(c))} {w}"
                for c, w in zip(cols, ("rebate", "to others", "install labor", "install materials"), strict=True)
                if self.z(c)
            ]
            expr = f"{units(q)} × ({' + '.join(named) if named else '$0'}) a unit"
            st.calcs.append(self.check("Rebates, incentives and direct install", expr, value, dollars(value), "RebatesandIncents"))
        if self.has("GrossParticipantCost"):
            value = self.z("GrossMeasureCost") - self.z("RebatesandIncents")
            st.calcs.append(
                self.check(
                    "What the customer pays",
                    f"{dollars(self.g('GrossMeasureCost'))} − {dollars(self.g('RebatesandIncents'))}",
                    value,
                    dollars(value),
                    "GrossParticipantCost",
                )
            )
        if self.z("ExcessIncentives"):
            st.facts.append(Fact("Incentives above the measure's cost", dollars(self.g("ExcessIncentives")), "ExcessIncentives"))
            st.notes.append("Incentives paid to others above the measure's cost count as a cost to the customer side too.")
        if self.has("NetParticipantCost"):
            ntgc = self.z("NTGRCost")
            value = ntgc * (self.z("GrossParticipantCost") + self.z("ExcessIncentives"))
            expr = f"{factor(ntgc)} cost net-to-gross × {dollars(self.z('GrossParticipantCost') + self.z('ExcessIncentives'))}"
            st.calcs.append(self.check("The customer's net cost", expr, value, dollars(value), "NetParticipantCost"))
            st.notes.append(
                "Only the part of the customer's cost the program caused counts, so it is multiplied by the cost net-to-gross ratio: "
                "a free "
                "rider would have paid it anyway."
            )
        pv = [
            ("RebatesandIncentsPV", "Rebates and incentives, present value"),
            ("NetParticipantCostAdjustedPV", "The customer's net cost, present value"),
        ]
        for col, words in pv:
            if self.has(col):
                st.facts.append(Fact(words, dollars(self.g(col)), col))
        if self.z("MarkEffectPlusExcessIncPV"):
            st.facts.append(Fact("Market effects on the cost", dollars(self.g("MarkEffectPlusExcessIncPV")), "MarkEffectPlusExcessIncPV"))
        if self.has("RebatesandIncentsPV"):
            st.notes.append(
                "These costs are paid once, in the claim quarter, and discounted to the First Year as the previous steps describe."
            )
        return st

    # 8
    def step_program(self, st: Step) -> Step:
        share = self.g("WeightedBenefits")
        program = self.s("Prg_ProgramName") or self.s("PrgID")
        st.plain = [
            "Programs also spend money that belongs to no single measure: administration, marketing, inspections, evaluation. Those "
            "costs are filed for the program as a whole, so each of the program's measures carries a share of them, in proportion to "
            "the benefits it brings. A measure that brings a third of the program's benefits carries a third of its costs.",
        ]
        if share is not None:
            st.facts.append(Fact(f"This measure's share of {program}'s costs", percent(share, 2), "WeightedBenefits"))
            pos = sum(max(self.z(c), 0.0) for c in ("ElecBen", "GasBen", "WaterEnergyBen", "OtherBen"))
            if share and pos:
                st.calcs.append(
                    Calc(
                        "Its share, from the benefits",
                        f"{dollars(pos)} of this measure's benefits ÷ {dollars(pos / share)} of the program's",
                        percent(share, 2),
                        "WeightedBenefits",
                        percent(share, 2),
                        None,
                    )
                )
        if self.has("WeightedProgramCost"):
            st.facts.append(
                Fact(
                    "Its share of the program's costs, as filed (undiscounted)",
                    dollars(self.g("WeightedProgramCost")),
                    "WeightedProgramCost",
                )
            )
            rows = []
            for c in sorted(k for k in self.r if k.startswith("Wtd") and self.z(k)):
                rows.append([_program_field_words(c), dollars(self.g(c))])
            if rows:
                st.table = {"caption": "What that share is made of", "columns": ["Program cost", "This measure's share"], "rows": rows}
        if self.has("TRCCost") and self.has("TRCCostNoAdmin"):
            part = self.z("TRCCost") - self.z("TRCCostNoAdmin")
            st.facts.append(Fact("The same share in present value (used by the tests)", dollars(part), "TRCCost"))
        if self.has("TotalExpenditures"):
            # a replica adds the program costs as CET's ProgramCosts column has them (CET 25 weights its WeightedProgramCost otherwise)
            prog = "ProgramCosts" if self.engine.replica and self.has("ProgramCosts") else "WeightedProgramCost"
            value = self.z("RebatesandIncents") + self.z(prog)
            st.calcs.append(
                self.check(
                    "The budget this measure accounts for",
                    f"{dollars(self.g('RebatesandIncents'))} rebates and incentives + {dollars(self.g(prog))} program costs",
                    value,
                    dollars(value),
                    "TotalExpenditures",
                )
            )
        if share is None:
            st.notes.append("This run's outputs do not carry the program-cost share.")
        elif not self.z("WeightedProgramCost"):
            st.notes.append("The run has no program costs for this measure's program, so the tests count none.")
        return st

    # 9
    def step_tests(self, st: Step) -> Step:
        st.plain = [
            "Each cost test asks the question from a different point of view, so each counts different benefits and costs. Each adds "
            "its benefits, adds its costs, and divides one by the other. A ratio of 1 or more means the benefits are at least as "
            "large as the costs: the measure passes that test.",
        ]
        if self.engine.replica:
            return self._replica_tests(st)
        cset = self.cset
        if cset is None:
            st.notes.append("This run's cost-test set could not be read, so the tests are shown from the outputs alone.")
            for t in ("TRC", "PAC", "RIM"):
                if self.has(f"{t}Ratio"):
                    st.tests.append(self._plain_card(t))
            return st
        for t in cset.tests.values():
            card = self._card(t)
            if card:
                st.tests.append(card)
        return st

    def _replica_tests(self, st: Step) -> Step:
        """A replica prices CET's own tests (TRC and PAC on every release; RIM, SCB and SCH ratios from CET 25.0) and writes no column
        for a test's benefits. The streams each test counts are the same as in the CPUC's published definitions (the built-in EE set),
        so they are listed from it, limited to the columns the release writes, and their sum is checked through the ratio."""
        tmpl = _published_ee_set()
        for tid in ("TRC", "PAC", "RIM", "SCB", "SCH"):
            if f"{tid}Ratio" in self.r or (tid == "RIM" and "RIMCost" in self.r):
                st.tests.append(self._replica_card(tid, tmpl.tests.get(tid) if tmpl else None, tmpl))
        st.notes.append(
            f"The {self.engine.label} writes each test's costs and ratio but no column for its benefits, so the benefits shown are the "
            "streams the test counts, and the ratio checks their sum."
        )
        return st

    def _replica_card(self, tid: str, t, tmpl) -> TestCard:
        label = t.label if t is not None else tid
        ratio_col, cost_col = f"{tid}Ratio", f"{tid}Cost"
        if not self.has(ratio_col):
            note = (
                f"The {self.engine.label} writes no ratio for this test."
                if ratio_col not in self.r
                else "This test is blank for this measure: its avoided costs are not in these tables."
            )
            return TestCard(tid, label, TEST_QUESTIONS.get(tid, ""), [], [], [], [], "", [], None, "–", "not priced", note=note)

        base = tmpl.tests["TRC"].pricing if tmpl is not None and "TRC" in tmpl.tests else None

        def terms(names):
            """The streams' terms, and whether a stream the release prices has no column in this test's pricing (its value is then
            inside the test's total but cannot be shown)."""
            shown, zero, incomplete = [], [], False
            for s in names:
                stream = tmpl.streams[s]
                if stream.source == "program" or stream.internal:
                    continue
                col = tmpl.column(s, t.pricing)
                words = STREAM_WORDS.get(s, stream.definition or s)
                if col not in self.r:
                    try:
                        priced = base is not None and tmpl.column(s, base) in self.r
                    except KeyError:
                        priced = False
                    if priced:  # e.g. the societal participant costs: CET prices them in the test but writes no column
                        shown.append(Term(words, col, None, "not written as a column"))
                        incomplete = True
                    continue
                if self.z(col):
                    shown.append(Term(words, col, self.z(col), dollars(self.g(col))))
                else:
                    zero.append(words)
            return shown, zero, incomplete

        benefits, ben_zero, ben_gap = terms(list(t.benefits) + list(t.numerator_adders)) if t is not None else ([], [], True)
        costs, cost_zero, cost_gap = terms(t.costs) if t is not None else ([], [], True)
        ben = sum(x.value for x in benefits if x.value is not None)
        cost_terms = sum(x.value for x in costs if x.value is not None)
        listed = [x for x in benefits if x.value is not None]
        if ben_gap and self.has(cost_col):
            implied = self.z(ratio_col) * self.z(cost_col)
            calcs = [
                Calc(
                    "Benefits",
                    f"{ratio_text(self.g(ratio_col))} ratio × {dollars(self.g(cost_col))} costs "
                    "(a stream it counts has no column of its own)",
                    dollars(implied),
                )
            ]
        else:
            calcs = [Calc("Benefits", " + ".join(x.shown for x in listed) or "$0", dollars(ben))]
        program_part = ""
        if self.has(f"{tid}CostNoAdmin") and not cost_gap:
            calcs.append(
                self.check(
                    "Costs without the program's own costs",
                    " + ".join(x.shown for x in costs) or "$0",
                    cost_terms,
                    dollars(cost_terms),
                    f"{tid}CostNoAdmin",
                )
            )
        if self.has(cost_col):
            if cost_gap and self.has(f"{tid}CostNoAdmin"):
                program_part = dollars(self.z(cost_col) - self.z(f"{tid}CostNoAdmin"))
                expr = f"{program_part} share of the program's costs + {dollars(self.g(f'{tid}CostNoAdmin'))} the measure's own costs"
            elif cost_gap:
                expr = "the program's share and the measure's own costs"
            else:
                program_part = dollars(self.z(cost_col) - cost_terms)
                expr = f"{program_part} share of the program's costs" + "".join(f" + {x.shown}" for x in costs)
            calcs.append(Calc("Costs", expr, dollars(self.g(cost_col)), cost_col, dollars(self.g(cost_col)), None))
            if not ben_gap:
                value = _div(ben, self.g(cost_col))
                calcs.append(self.check("Ratio", f"{dollars(ben)} ÷ {dollars(self.g(cost_col))}", value, ratio_text(value), ratio_col))
        ratio = self.g(ratio_col)
        note = ""
        if self.has(f"{tid}RatioNoAdmin") and self.z(f"{tid}CostNoAdmin"):
            note = (
                f"Without the program's own costs the ratio would be {ratio_text(self.g(f'{tid}RatioNoAdmin'))}: the measure itself, "
                "before its share of administration, marketing and the rest."
            )
        return TestCard(
            tid,
            label,
            TEST_QUESTIONS.get(tid, ""),
            benefits,
            ben_zero,
            costs,
            cost_zero,
            program_part,
            calcs,
            ratio,
            ratio_text(ratio),
            _verdict(ratio),
            note,
        )

    def _plain_card(self, t: str) -> TestCard:
        ratio = self.g(f"{t}Ratio")
        calc = self.check(
            "Ratio",
            f"{dollars(self.g(f'{t}Ben'))} benefits ÷ {dollars(self.g(f'{t}Cost'))} costs",
            _div(self.g(f"{t}Ben"), self.g(f"{t}Cost")),
            ratio_text(_div(self.g(f"{t}Ben"), self.g(f"{t}Cost"))),
            f"{t}Ratio",
        )
        return TestCard(t, t, TEST_QUESTIONS.get(t, ""), [], [], [], [], "", [calc], ratio, ratio_text(ratio), _verdict(ratio))

    def _card(self, t) -> TestCard | None:
        cset = self.cset
        ben_col, cost_col, ratio_col = f"{t.id}Ben", f"{t.id}Cost", f"{t.id}Ratio"
        if not self.has(ratio_col) and not self.has(ben_col):
            if ratio_col in self.r or ben_col in self.r:
                return TestCard(
                    t.id,
                    t.label,
                    TEST_QUESTIONS.get(t.id, t.definition),
                    [],
                    [],
                    [],
                    [],
                    "",
                    [],
                    None,
                    "–",
                    "not priced",
                    note="This test is blank for this run: its avoided costs are not in this ACC.",
                )
            return None

        def terms(names):
            shown, zero = [], []
            for s in names:
                stream = cset.streams[s]
                if stream.source == "program":
                    continue
                col = cset.column(s, t.pricing)
                words = STREAM_WORDS.get(s, stream.definition or s)
                if col not in self.r:
                    shown.append(Term(words, col, None, "not written as a column"))
                elif self.z(col):
                    shown.append(Term(words, col, self.z(col), dollars(self.g(col))))
                else:
                    zero.append(words)
            return shown, zero

        benefits, ben_zero = terms(list(t.benefits) + list(t.numerator_adders))
        costs, cost_zero = terms(t.costs)
        calcs = []
        known = sum(x.value for x in benefits if x.value is not None)
        missing = [x for x in benefits if x.value is None]
        if missing and self.has(ben_col):  # an internal stream (PCT's tax credits), never written: the rest of <T>Ben
            rest = self.z(ben_col) - known
            if agrees(rest, 0.0):
                benefits.remove(missing[0])
                ben_zero.append(missing[0].label)
            else:
                missing[0].value = rest
                missing[0].shown = f"{dollars(rest)} (the rest of the test's benefits)"
                known += rest
        if self.has(ben_col):
            expr = " + ".join(x.shown.split(" (")[0] for x in benefits) or "$0"
            calcs.append(self.check("Benefits", expr, known, dollars(known), ben_col))
        cost_terms = sum(x.value for x in costs if x.value is not None)
        program_part = ""
        if t.share is not None and self.has(cost_col):
            part = self.z(cost_col) - cost_terms
            program_part = dollars(part)
            if "NoAdmin" in t.variants and self.has(f"{t.id}CostNoAdmin"):
                expr = " + ".join(x.shown for x in costs) or "$0"
                calcs.append(
                    self.check("Costs without the program's own costs", expr, cost_terms, dollars(cost_terms), f"{t.id}CostNoAdmin")
                )
            expr = f"{program_part} share of the program's costs" + "".join(f" + {x.shown}" for x in costs)
            calcs.append(Calc("Costs", expr, dollars(self.g(cost_col)), cost_col, dollars(self.g(cost_col)), None))
        elif self.has(cost_col):
            expr = " + ".join(x.shown for x in costs) or "$0"
            calcs.append(self.check("Costs", expr, cost_terms, dollars(cost_terms), cost_col))
        ratio = self.g(ratio_col)
        if self.has(ben_col) and self.has(cost_col):
            value = _div(self.g(ben_col), self.g(cost_col))
            calcs.append(
                self.check("Ratio", f"{dollars(self.g(ben_col))} ÷ {dollars(self.g(cost_col))}", value, ratio_text(value), ratio_col)
            )
        note = ""
        if "NoAdmin" in t.variants and self.has(f"{t.id}RatioNoAdmin") and t.share is not None and self.z(f"{t.id}CostNoAdmin"):
            note = (
                f"Without the program's own costs the ratio would be {ratio_text(self.g(f'{t.id}RatioNoAdmin'))}: the measure itself, "
                "before its share of administration, marketing and the rest."
            )
        return TestCard(
            t.id,
            t.label,
            TEST_QUESTIONS.get(t.id, t.definition),
            benefits,
            ben_zero,
            costs,
            cost_zero,
            program_part,
            calcs,
            ratio,
            ratio_text(ratio),
            _verdict(ratio),
            note,
        )

    # 10
    def step_summary(self, st: Step, calcs: list[Calc] = ()) -> Step:
        rows = []
        if self.cset is not None:
            tests = [(t.id, t.label) for t in self.cset.tests.values()]
        else:
            tests = [(t, t) for t in ("TRC", "PAC", "RIM")]
        for tid, label in tests:
            if f"{tid}Ratio" in self.r:
                ratio = self.g(f"{tid}Ratio")
                rows.append([f"{label} ({tid})", ratio_text(ratio), _verdict(ratio)])
        if rows:
            st.table = {"caption": "Every test's ratio for this measure", "columns": ["Test", "Ratio", "Result"], "rows": rows}
        trc, trc_na = self.g("TRCRatio"), self.g("TRCRatioNoAdmin")
        st.plain = []
        if trc is not None:
            st.plain.append(
                f"On the Total Resource Cost test, the test filings are mainly judged on, every dollar this measure costs brings back "
                f"{dollars(trc) if trc < 100 else ratio_text(trc)} of benefits"
                + (", so it passes." if trc >= 1 else ", so on its own it does not pay for itself.")
            )
            if trc_na is not None and trc < 1 <= trc_na:
                st.plain.append(
                    f"The measure itself pays off (a ratio of {ratio_text(trc_na)} before program costs); it is its share of the program's "
                    "administration, marketing and other costs that pulls it below 1."
                )
            elif trc_na is not None and trc_na < 1:
                st.plain.append(
                    f"Even before the program's own costs its ratio is {ratio_text(trc_na)}: the value of the energy it saves is less than "
                    "what the measure and its incentives cost."
                )
        if self.has("TRCLifecycleNetBen"):
            st.facts.append(
                Fact("Net benefit over the life, TRC (benefits less costs)", dollars(self.g("TRCLifecycleNetBen")), "TRCLifecycleNetBen")
            )
        if self.has("TotalSystemBenefit"):
            st.facts.append(Fact("Total system benefit", dollars(self.g("TotalSystemBenefit")), "TotalSystemBenefit"))
        st.plain.append(
            "Cost-effectiveness is judged for whole programs and portfolios, not single measures: a measure below 1 can belong in a "
            "portfolio that passes. The Results page adds up every measure the same way."
        )
        checks = [c for c in calcs if c.ok is not None]
        if checks:
            ok = sum(1 for c in checks if c.ok)
            known = sum(1 for c in checks if c.ok is False and c.known)
            bad = sum(1 for c in checks if c.ok is False and not c.known)
            tail = []
            if known:
                s_ = "s" if known == 1 else ""
                tail.append(f"{known} differ{s_} because of an error CET {self.engine.release} makes, which the step names")
            if bad:
                tail.append(f"{bad} differ{'s' if bad == 1 else ''}, which the step marks")
            st.notes.append(
                f"{ok} of the {ok + known + bad} pieces of arithmetic on these pages agree with the engine's own values"
                + ("; " + "; ".join(tail) + "." if tail else ".")
            )
        return st


def _published_ee_set():
    """The CPUC's published energy-efficiency cost-test set the engine ships (its tests are CET's, written out), used to name the streams
    a CET replica's tests count. None when the engine has none."""
    try:
        from recet import cost_test_sets

        ids = sorted(i for i in cost_test_sets.builtin() if i.startswith("EE-"))
        return cost_test_sets.load(ids[0]) if ids else None
    except Exception:  # noqa: BLE001
        return None


def _div(n, d) -> float | None:
    n, d = num(n), num(d)
    if n is None or d is None:
        return None
    return n / d if d else 0.0


def _verdict(ratio) -> str:
    r = num(ratio)
    if r is None:
        return "not priced"
    return "passes (benefits at least equal costs)" if r >= 1 else "does not pass (benefits below costs)"


PROGRAM_FIELDS = {
    "WtdAdminCostsOverheadAndGA": "Administration: overhead and general",
    "WtdAdminCostsOther": "Administration: other",
    "WtdMarketingOutreach": "Marketing and outreach",
    "WtdDIActivity": "Direct implementation: activity",
    "WtdDIInstallation": "Direct implementation: installation",
    "WtdDIHardwareAndMaterials": "Direct implementation: hardware and materials",
    "WtdDIRebateAndInspection": "Direct implementation: rebates and inspection",
    "WtdEMV": "Evaluation, measurement and verification",
    "WtdUserInputIncentive": "Incentives filed at the program level",
    "WtdCostsRecoveredFromOtherSources": "Costs recovered from other sources",
}


def _program_field_words(col: str) -> str:
    return PROGRAM_FIELDS.get(col, col.removeprefix("Wtd"))


def build(row: dict, settings: dict, cset=None, repeated: int = 1, engine: Engine | None = None, packs: dict | None = None) -> list[Step]:
    """The ten steps for one measure, with a count of the arithmetic that agrees on the last. `engine`: the calculation that priced the
    run (default: the one run_settings.json names, else ReCET's own). `packs`: the numbers read from the run's packs for the load-shape
    and value charts (walkchart.pack_series), None to leave those two charts out."""
    from . import walkchart

    w = Walk(row, settings, cset, repeated, engine or Engine.of(None, settings))
    steps = [getattr(w, f"step_{k}")(Step(k, title, i + 1)) for i, (k, title) in enumerate(STEPS) if k != "summary"]
    for s in steps:
        if s.key == "life":
            s.charts = [c for c in [walkchart.life_chart(w)] if c]
        elif s.key == "timing":
            s.charts = walkchart.shape_charts(packs)
        elif s.key == "discounting":
            s.charts = [c for c in [walkchart.discount_chart(w)] if c]
        elif s.key == "value":
            s.charts = walkchart.value_charts(w, packs)
    calcs = [c for s in steps for c in s.calcs] + [c for s in steps for t in s.tests for c in t.calcs]
    steps.append(w.step_summary(Step("summary", dict(STEPS)["summary"], len(STEPS)), calcs))
    if w.names:  # name each column as the replica wrote it
        for s in steps:
            for f in s.facts:
                f.column = w.names.get(f.column, f.column)
            for c in s.calcs + [c for t in s.tests for c in t.calcs]:
                c.output = w.names.get(c.output, c.output)
    return steps


def checks(steps: list[Step]) -> list[Calc]:
    """Every piece of arithmetic on the pages that is set beside an output."""
    return [c for s in steps for c in s.calcs + [c for t in s.tests for c in t.calcs] if c.ok is not None]


def file_name(run: Run, acc: str | None) -> str:
    """The kept measure-level file's name, for the page's note on where the numbers come from."""
    try:
        return PurePosixPath(_measure_key(run, results_mod._part(run, acc))).name
    except NoWalkthrough:
        return ""

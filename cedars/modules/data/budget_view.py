"""The budget and spending page (spec page 5, BR-07.3): budget against actual for the PAs chosen, by year and by program.

Four series, each from where it is kept:
  operating budget     the standing budget filing plus the fund shifts in effect (registry.operating_budget, filled by Budgets)
  forecast budget      the chosen filing's TotalExpenditures (the forecast snapshots)
  monthly reported     each PA's latest published monthly report of the year, year to date (registry.reported_spend); estimates
                       not yet validated (BR-14.5)
  claimed              confirmed claims' TotalExpenditures (the claims snapshots)
The budget lines are filtered by PA and program only: sector, segment and the other filters apply to forecast and claims."""

from collections import defaultdict
from dataclasses import dataclass, field, replace

from cedars.services.accounts.models import Organisation
from cedars.services.registry import service as registry

from . import metrics as catalog
from . import query as dq
from .dashboard import SERIES, View

BUDGET_SERIES = {"budget": "Operating budget", "monthly": "Monthly reported (unvalidated)"}


@dataclass
class SpendRow:
    key: object  # year or PrgID
    budget: float | None = None
    monthly: float | None = None
    values: dict = field(default_factory=dict)  # forecast / claims expenditure
    cumulative_budget: float | None = None
    cumulative_claims: float | None = None
    drill: str = ""
    through: str = ""  # the monthly report's month

    @property
    def claimed_share(self):
        c = self.values.get("claims")
        return None if c is None or not self.budget else 100 * c / self.budget

    @property
    def monthly_share(self):
        return None if self.monthly is None or not self.budget else 100 * self.monthly / self.budget


@dataclass
class Spending:
    view: View
    results: dict  # series -> query.Result (expenditure by year and program)
    by_year: list[SpendRow]
    by_program: list[SpendRow]
    elements: dq.Result | None  # cost elements by sector, for the last series shown
    organisations: list[str]
    notes: list[str]

    def series_label(self, s: str) -> str:
        as_of = self.results[s].as_of
        return f"{SERIES[s][0]} (as of {as_of:%Y-%m-%d %H:%M %Z})" if as_of else f"{SERIES[s][0]} (nothing published)"

    def downloads(self) -> list[dict]:
        out = []
        for s, r in self.results.items():
            qs = r.query.query_string(self.view.page.exclude)
            out.append(
                {"series": SERIES[s][0], "links": [(fmt.upper(), f"/data/download.{fmt}?{qs}") for fmt in ("csv", "xlsx", "parquet")]}
            )
        return out


ELEMENTS = ("incentives", "direct_implementation", "admin_overhead", "admin_other", "marketing", "emv", "costs_recovered")


def organisations(view: View) -> list[Organisation]:
    codes = view.query.filter_map.get("pa")
    pas = Organisation.objects.filter(kind="pa").order_by("code")
    return list(pas.filter(code__in=[c.upper() for c in codes]) if codes else pas)


def compare(view: View) -> Spending:
    q = view.query
    results = {s: dq.run(replace(view.for_series(s), metrics=("expenditure",), by=("year", "program"))) for s in view.series}
    years = sorted({r["year"] for res in results.values() for r in res.rows if r["year"] is not None})
    lo = q.year_from or (years[0] if years else None)
    hi = q.year_to or (years[-1] if years else None)
    span = list(range(lo, hi + 1)) if lo and hi else []
    programs = set(q.filter_map.get("program", ()))
    orgs = organisations(view)

    budget: dict[tuple[int, str], float] = defaultdict(float)
    cycles = {c for c in (registry.cycle_for_year(y) for y in span) if c is not None}
    for org in orgs:
        for cycle in cycles:
            for line in registry.operating_budget(org, cycle):
                if line["year"] in span and (not programs or line["prg_id"] in programs):
                    budget[(line["year"], line["prg_id"])] += float(line["operating"] or 0)
    monthly: dict[tuple[int, str], float] = defaultdict(float)
    through: dict[int, set] = defaultdict(set)
    for org in orgs:
        for y in span:
            for line in registry.reported_spend(org, y):
                if not programs or line["prg_id"] in programs:
                    monthly[(y, line["prg_id"])] += float(line["ytd"])
                    through[y].add(f"{org.code} {line['through']}")

    actual: dict[str, dict[tuple[int, str], float]] = {s: {} for s in results}
    for s, res in results.items():
        for r in res.rows:
            if r["year"] in span:
                actual[s][(r["year"], r["program"])] = (actual[s].get((r["year"], r["program"])) or 0) + (r["expenditure"] or 0)

    def total(cells, keep):
        picked = [v for k, v in cells.items() if keep(k)]
        return sum(picked) if picked else None

    by_year, run_budget, run_claims = [], 0.0, 0.0
    for y in span:
        row = SpendRow(
            y,
            budget=total(budget, lambda k, y=y: k[0] == y),
            monthly=total(monthly, lambda k, y=y: k[0] == y),
            values={s: total(actual[s], lambda k, y=y: k[0] == y) for s in results},
            through=", ".join(sorted(through[y])),
        )
        run_budget += row.budget or 0
        run_claims += row.values.get("claims") or 0
        row.cumulative_budget, row.cumulative_claims = run_budget, (run_claims if "claims" in results else None)
        by_year.append(row)

    keys = {k[1] for k in budget} | {k[1] for k in monthly} | {k[1] for cells in actual.values() for k in cells}
    by_program = []
    for p in keys:
        row = SpendRow(
            p,
            budget=total(budget, lambda k, p=p: k[1] == p),
            monthly=total(monthly, lambda k, p=p: k[1] == p),
            values={s: total(actual[s], lambda k, p=p: k[1] == p) for s in results},
        )
        if p is not None:
            row.drill = f"/programs/{p}/"
        by_program.append(row)
    by_program.sort(key=lambda r: (-(r.budget or 0), str(r.key)))

    elements = None
    if view.series:
        last = view.series[-1]
        elements = dq.run(replace(view.for_series(last), metrics=ELEMENTS + ("expenditure",), by=("sector",)))
    notes = []
    for res in results.values():
        notes += [n for n in res.notes if n not in notes]
    if not cycles:
        notes.append("No filing cycle covers these years, so there is no operating budget to show.")
    notes.append("Monthly reported spending is the PAs' own year-to-date estimate, not yet validated.")
    return Spending(view, results, by_year, by_program, elements, [o.code for o in orgs], notes)


def chart(s: Spending) -> dict:
    """Bars of forecast and claimed expenditure for each year, with the operating budget as the marker."""
    series = list(s.results)
    values = [v for r in s.by_year for v in [*r.values.values(), r.budget] if v is not None]
    high = max([0, *values]) or 1
    groups = []
    for r in s.by_year:
        bars = []
        for i, sr in enumerate(series):
            v = r.values.get(sr)
            bars.append({"series": sr, "x": 0, "w": round(100 * max(v or 0, 0) / high, 3), "y": 14 * i, "missing": v is None})
        groups.append({"label": r.key, "bars": bars, "goal": None if r.budget is None else round(100 * r.budget / high, 3)})
    return {
        "series": [{"code": sr, "label": SERIES[sr][0]} for sr in series],
        "groups": groups,
        "zero": 0,
        "negative": False,
        "height": 14 * len(series),
        "goal": "Operating budget",
    }


def element_metrics() -> list[catalog.Metric]:
    return [catalog.get(c) for c in ELEMENTS]

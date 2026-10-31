"""The goals page (spec page 1): forecast and claimed results against each IOU territory's goals, by year (annual targets) or by
cycle (cumulative goals), from the goal set in force (`registry.goals`).

A PA's results count toward a territory as the counting rules of the set in force say: an IOU counts fully toward its own territory
unless a rule says otherwise; a CCA or REN counts only where a rule names it, at the rule's share. TSB is compared with the incentive
programs' goal and leaves codes and standards out; the codes-and-standards metrics read only codes-and-standards programs."""

from collections import defaultdict
from dataclasses import dataclass, replace

from cedars.services.registry import goals as goal_sets

from . import query as dq
from .dashboard import GOAL_METRICS, SERIES, View

CS_VALUES = ("codes_and_standards", "Codes and Standards")


@dataclass
class GoalRow:
    territory: str
    period: str  # "2028" or "2028-2031"
    first_year: int
    last_year: int
    goal: float | None
    values: dict  # series -> value counted toward the territory, in the goal's unit
    drill: str = ""

    @property
    def key(self) -> str:
        return f"{self.territory} {self.period}"

    def percent(self, series):
        v = self.values.get(series)
        return None if v is None or not self.goal else 100 * v / self.goal


@dataclass
class GoalComparison:
    view: View
    metric: object  # GoalMetric
    period: str  # annual | cycle
    results: dict  # series -> query.Result
    rows: list[GoalRow]
    rules: list
    shares: dict  # pa -> [(territory, share)]

    @property
    def notes(self) -> list[str]:
        seen = []
        for r in self.results.values():
            for n in r.notes:
                if n not in seen:
                    seen.append(n)
        return seen

    def series_label(self, s: str) -> str:
        as_of = self.results[s].as_of
        return f"{SERIES[s][0]} (as of {as_of:%Y-%m-%d %H:%M %Z})" if as_of else f"{SERIES[s][0]} (nothing published)"

    def downloads(self) -> list[dict]:
        out = []
        for s in self.results:
            qs = self.results[s].query.query_string(self.view.page.exclude)
            out.append(
                {"series": SERIES[s][0], "links": [(fmt.upper(), f"/data/download.{fmt}?{qs}") for fmt in ("csv", "xlsx", "parquet")]}
            )
        return out


def shares(rules, applies_to: str) -> dict[str, list[tuple[str, float]]]:
    """Which territories each PA's results count toward, and at what share."""
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in rules:
        if r.applies_to in ("all", applies_to):
            out[r.pa].append((r.territory, float(r.share)))
    for iou in goal_sets.UTILITIES:
        out.setdefault(iou, [(iou, 1.0)])
    return dict(out)


def query_for(view: View, series: str, gm) -> dq.Query:
    q = replace(view.for_series(series), by=("pa", "year"))
    if gm.applies_to == "cs":
        filters = dict(q.filters) | {"segment": CS_VALUES}
        q = replace(q, filters=tuple(filters.items()), exclude=tuple(e for e in q.exclude if e != "cs"))
    elif "cs" not in q.exclude:
        q = replace(q, exclude=(*q.exclude, "cs"))  # TSB goals leave codes and standards out
    return q


def compare(view: View, period: str = "annual") -> GoalComparison:
    gm = GOAL_METRICS[view.metric_choice]
    rules = goal_sets.counting_rules()
    pa_shares = shares(rules, gm.applies_to)
    results = {s: dq.run(query_for(view, s, gm)) for s in view.series}
    counted: dict[str, dict[tuple[str, int], float]] = {}  # series -> (territory, year) -> value in the goal's unit
    years: set[int] = set()
    for s, r in results.items():
        cells: dict[tuple[str, int], float] = defaultdict(float)
        for row in r.rows:
            year, value = row["year"], row[gm.metric]
            if year is None or value is None:
                continue
            years.add(year)
            for territory, share in pa_shares.get(str(row["pa"]).upper(), []):
                cells[(territory, year)] += share * value / gm.scale
        counted[s] = cells
    q = view.query
    span = range(q.year_from or min(years, default=0), (q.year_to or max(years, default=-1)) + 1)
    rows: list[GoalRow] = []
    if period == "cycle":
        cycles = {}
        for y in span:
            for t, g in goal_sets.goals("cycle", y).items():
                cycles[(t, g.first_year)] = g
        for (t, _), g in sorted(cycles.items()):
            vals = {s: sum(v for (tt, y), v in counted[s].items() if tt == t and g.first_year <= y <= g.last_year) for s in results}
            rows.append(GoalRow(t, f"{g.first_year}-{g.last_year}", g.first_year, g.last_year, _num(getattr(g, gm.goal_field)), vals))
    else:
        for y in span:
            targets = goal_sets.goals("annual", y)
            for t in sorted(set(targets) | {tt for s in counted for (tt, yy) in counted[s] if yy == y}):
                g = targets.get(t)
                vals = {s: counted[s].get((t, y)) for s in results}
                if g is None and all(v is None for v in vals.values()):
                    continue
                rows.append(GoalRow(t, str(y), y, y, _num(getattr(g, gm.goal_field)) if g else None, vals))
    shown = next(iter(results.values())).query if results else view.query
    toggles = {code: "exclude" if code in shown.exclude else "include" for code in dq.EXCLUSIONS}
    for row in rows:  # to the overview, by PA, for the PAs counted toward the territory (then sector, program, measure)
        pas = sorted(pa for pa, ts in pa_shares.items() if any(t == row.territory for t, _ in ts))
        changes = toggles | {"pa": pas, "by": "pa", "year_from": str(row.first_year), "year_to": str(row.last_year)}
        changes |= {"metric": gm.metric.split("_")[0], "period": None}
        if gm.applies_to == "cs":
            changes["segment"] = CS_VALUES[0]
        row.drill = "/data/overview/" + view.link(**changes)
    return GoalComparison(view, gm, period, results, rows, rules, pa_shares)


def _num(v):
    return None if v is None else float(v)


def chart(c: GoalComparison) -> dict:
    """The goal page's bars: forecast and claimed for each territory and period, with the goal as a marker."""
    series = list(c.results)
    values = [v for r in c.rows for v in [*r.values.values(), r.goal] if v is not None]
    low, high = min([0, *values]), max([0, *values])
    span = (high - low) or 1
    zero = 100 * (0 - low) / span
    groups = []
    for r in c.rows:
        bars = []
        for i, s in enumerate(series):
            v = r.values.get(s)
            if v is None:
                bars.append({"series": s, "x": zero, "w": 0, "y": 14 * i, "missing": True})
            else:
                bars.append(
                    {
                        "series": s,
                        "x": round(100 * (min(v, 0) - low) / span, 3),
                        "w": round(100 * abs(v) / span, 3),
                        "y": 14 * i,
                        "missing": False,
                    }
                )
        goal = None if r.goal is None else round(100 * (r.goal - low) / span, 3)
        groups.append({"label": r.key, "bars": bars, "goal": goal})
    return {
        "series": [{"code": s, "label": SERIES[s][0]} for s in series],
        "groups": groups,
        "zero": round(zero, 3),
        "negative": low < 0,
        "height": 14 * len(series),
        "goal": True,
    }

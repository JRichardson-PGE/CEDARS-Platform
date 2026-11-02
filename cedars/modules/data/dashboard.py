"""The dashboard framework (spec "Shared controls"): a page's view state from GET parameters, forecast and claims side by side from one
query each, drill links, the applied-filter list and the chart geometry. Pages differ only in their `Page` entry; the view and the
templates are shared.

Every series is one `query.run` on its dataset with the same parameters, so the chart, the table and each series' download agree."""

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from urllib.parse import urlencode

from django.utils import timezone

from cedars.services.registry.models import FilingCycle, Program, ProgramVersion
from cedars.services.snapshots import service as snapshots

from . import metrics as catalog
from . import query as dq

CHART_LIMIT = 20  # bars in a ranked chart; the table lists every row


@dataclass(frozen=True)
class Section:
    """One chart-and-table block of a page: grouped by `by`, or a share matrix of `by` rows across `columns`."""

    by: str
    title: str
    chart: bool = True
    columns: str = ""  # a matrix: each row's metric split across this dimension's values, as shares of the row
    limit: int = 0  # the chart shows only the first rows (the table keeps them all)
    drill: tuple[str, str] = ("", "")  # (page slug, by) a row drills to, instead of the overview one level down


@dataclass(frozen=True)
class Page:
    slug: str
    title: str
    description: str
    metrics: tuple[str, ...]  # the metric control's choices (catalog codes or view-control bases); the first is the default
    exclude: tuple[str, ...] = dq.DEFAULT_EXCLUDE
    series: tuple[str, ...] = ("forecast", "claims")
    sections: tuple[Section, ...] = ()  # none: one section grouped by the view-by control
    kind: str = "compare"  # or "goals" (goal_view.py)
    short: str = ""  # the sub-navigation's label when the title is long (UI review 2, #22: the strip fits one row at 1280 px)

    @property
    def nav_title(self) -> str:
        return self.short or self.title

    @property
    def view_by(self) -> bool:
        """Whether the page offers the view-by control (a page with fixed sections does not)."""
        return not self.sections and self.kind == "compare"


PAGES = {
    p.slug: p
    for p in [
        Page(
            "overview",
            "Overview",
            "Forecast and claimed results by PA, segment, sector, implementer type or measure use category, with the share of the "
            "forecast achieved.",
            ("tsb", "kwh", "kw", "therms", "expenditure"),
        ),
        Page(
            "goals",
            "Goals",
            "Forecast and claimed results against the CPUC's energy efficiency goals for each IOU territory, by year or by cycle. CCA "
            "and REN results count toward a territory as the goal decision's counting rules say.",
            ("tsb", "cs_gwh", "cs_mw", "cs_mmtherms"),
            exclude=("pension", "esa", "cs"),
            kind="goals",
        ),
        Page(
            "total-system-benefit",
            "Total system benefit",
            "Total system benefit by sector and year, each PA's split across sectors, and every program.",
            ("tsb",),
            sections=(
                Section("sector", "by primary sector"),
                Section("year", "by year"),
                Section("pa", "share of each PA's total by primary sector", chart=False, columns="sector"),
                Section("program", "by program", chart=False),
            ),
        ),
        Page(
            "programs",
            "Program detail",
            "Programs ranked by the metric, each linked to its program page and to its measures by use category.",
            ("tsb", "kwh", "kw", "therms", "expenditure", "trc"),
            sections=(Section("program", "by program", limit=CHART_LIMIT, drill=("measures", "use_category")),),
            short="Programs",
        ),
        Page(
            "measures",
            "Measure detail",
            "Results by measure use category and by measure. Each measure links to the claim or filing records behind it.",
            ("tsb", "kwh", "kw", "therms", "expenditure"),
            sections=(
                Section("use_category", "by measure use category", drill=("measures", "measure")),
                Section("measure", "by measure", chart=False),
            ),
            short="Measures",
        ),
        Page(
            "budget",
            "Budget and spending",
            "The operating budget (the approved filing with fund shifts) against the forecast budget, monthly-reported spending and "
            "claimed expenditure, by year and by program, and the cost elements by sector. Choose PAs under Filters.",
            ("expenditure",),
            kind="budget",
            short="Budget",
        ),
        Page(
            "cost-effectiveness",
            "Cost effectiveness",
            "Cost-test ratios by sector, by year and by program, each computed from summed benefits and costs (never an average of "
            "ratios). Without admin leaves administration costs out of the cost. Programs flagged as excluded from cost-effectiveness "
            "are left out unless included.",
            tuple(f"{t}{v}" for t in catalog.TESTS for v in ("", "_no_admin")),
            exclude=(*dq.DEFAULT_EXCLUDE, "no_ce"),
            sections=(Section("sector", "by primary sector"), Section("year", "by year"), Section("program", "by program", chart=False)),
        ),
        Page(
            "energy-savings",
            "Energy savings",
            "Electric energy, demand and gas savings by sector, by year and by program, gross or net, first year or lifecycle "
            "(demand savings are first year only).",
            ("kwh", "therms", "kw"),
            sections=(Section("sector", "by primary sector"), Section("year", "by year"), Section("program", "by program", chart=False)),
        ),
        Page(
            "environmental-impact",
            "Environmental impact",
            "Emissions avoided, in metric tons, by sector, by year and by program, gross or net, first year or lifecycle.",
            tuple(catalog.EMISSIONS),
            sections=(Section("sector", "by primary sector"), Section("year", "by year"), Section("program", "by program", chart=False)),
            short="Environment",
        ),
    ]
}


@dataclass(frozen=True)
class GoalMetric:
    label: str
    metric: str  # the catalog metric compared with the goal
    goal_field: str  # registry Goal field
    applies_to: str  # the counting rules that apply: programs (incentive programs) or cs
    unit: str
    scale: float = 1.0  # the metric's shown value divided by this is in the goal's unit


GOAL_METRICS = {
    "tsb": GoalMetric("Total system benefit (excludes codes and standards)", "tsb", "program_tsb", "programs", "$"),
    "cs_gwh": GoalMetric("Codes and standards electric savings", "kwh_first_year_net", "cs_gwh", "cs", "GWh", 1_000),
    "cs_mw": GoalMetric("Codes and standards demand savings", "kw_first_year_net", "cs_mw", "cs", "MW"),
    "cs_mmtherms": GoalMetric("Codes and standards gas savings", "therms_first_year_net", "cs_mmtherms", "cs", "MMTherms"),
}

SERIES = {"forecast": ("Forecast", "forecast"), "claims": ("Claimed", "claims")}
TEXT_FILTERS = ("use_category", "program", "measure")
FILTERS = ("pa_type", "pa", "sector", "segment", "implementer_type", "use_category", "program", "measure", "measure_type")
# One level down from each view-by (spec "Drill"): PA type > PA > sector > program > measure.
DRILL = {
    "pa_type": "pa",
    "pa": "sector",
    "sector": "program",
    "segment": "program",
    "implementer_type": "program",
    "use_category": "measure",
    "program": "measure",
}
VIEW_PARAMS = (
    "series",
    "filing",
    "year_from",
    "year_to",
    "by",
    "metric",
    "period",
    "gross",
    "lifecycle",
    "cs",
    "esa",
    "pension",
    "no_ce",
) + FILTERS


def current_cycle(today: date | None = None) -> FilingCycle | None:
    year = (today or date.today()).year
    return FilingCycle.objects.filter(first_year__lte=year, last_year__gte=year).order_by("-first_year").first()


def default_cycle(today: date | None = None) -> FilingCycle | None:
    """The years a page opens on (BR-07.7): the current cycle, unless the forecast release holds no filing for it, when it is the newest
    cycle the forecast release does hold (a demo or a new cycle's first months would otherwise open on years with no forecast)."""
    cycle = current_cycle(today) if today else current_cycle()
    release = snapshots.current_release("forecast")
    if release is None:
        return cycle
    filed = {f["cycle"] for f in snapshots.release_files(release) if f["cycle"]}
    if not filed or (cycle is not None and str(cycle) in filed):
        return cycle
    first_years = [int(c.split("-")[0]) for c in filed if c.split("-")[0].isdigit()]
    return FilingCycle.objects.filter(first_year__in=first_years).order_by("-first_year").first() or cycle


@dataclass
class View:
    page: Page
    series: tuple[str, ...]
    query: dq.Query  # the claims query; the forecast one differs only in dataset
    metric_choice: str  # the metric control's value (a base such as "kwh" or a catalog code)
    params: dict = field(default_factory=dict)  # the view's own GET parameters, as given (for links)

    @property
    def by(self) -> str:
        return self.query.by[0]

    @property
    def metric(self) -> catalog.Metric:
        return catalog.get(self.query.metrics[0])

    def for_series(self, series: str) -> dq.Query:
        return replace(self.query, dataset=SERIES[series][1], filing=self.query.filing if series == "forecast" else "")

    def link(self, **changes) -> str:
        """This view's URL with parameters changed (a value of None removes the parameter)."""
        p = {k: list(v) for k, v in self.params.items()}
        for k, v in changes.items():
            if v is None:
                p.pop(k, None)
            else:
                p[k] = [v] if isinstance(v, str | int) else list(v)
        return "?" + urlencode({k: v for k, v in p.items() if v}, doseq=True)


def parse(page: Page, params) -> View:
    """The view from GET parameters, with the page's defaults: both series, the current cycle's years, view by PA, the page's first
    metric, its exclusions. Raises query.QueryError for a parameter that cannot be used."""
    get = params.getlist if hasattr(params, "getlist") else (lambda k: params.get(k, []))
    raw = {k: [v for v in get(k) if v] for k in VIEW_PARAMS}
    for k in TEXT_FILTERS:  # typed as a comma-separated list
        raw[k] = [part.strip() for v in raw.get(k, []) for part in v.split(",") if part.strip()]
    raw = {k: v for k, v in raw.items() if v}
    series = tuple(s for s in SERIES if s in raw.get("series", [])) or page.series
    by = raw.get("by", ["pa"])[-1]
    if by not in dq.VIEW_BY and by not in DRILL.values():
        raise dq.QueryError(f"cannot view by {by!r}")
    choice = raw.get("metric", [page.metrics[0]])[-1]
    if choice not in page.metrics:
        raise dq.QueryError(f"this page does not show {choice!r}")
    qparams = dict(raw) | {"metric": [GOAL_METRICS[choice].metric if page.kind == "goals" else choice], "by": [by]}
    if "year_from" not in raw and "year_to" not in raw and (cycle := default_cycle()):
        qparams |= {"year_from": [str(cycle.first_year)], "year_to": [str(cycle.last_year)]}
    q = dq.from_params(qparams, dataset="claims", exclude=page.exclude)
    return View(page, series, q, choice, raw)


# --- results


@dataclass
class Row:
    key: object  # the view-by value
    values: dict  # series -> metric value
    drill: str = ""  # the link one level down, or "" at the bottom
    links: list = field(default_factory=list)  # (text, href): the program page, each series' records
    shared: dict | None = None  # series -> value over the years both series cover, when those are not all the years

    def value(self, series):
        return self.values.get(series)

    @property
    def difference(self):
        c, f = self.values.get("claims"), self.values.get("forecast")
        return None if c is None or f is None else c - f

    @property
    def percent(self):
        """Claims as a share of the forecast, over the years that have both (a claim year with no forecast, or a forecast year with no
        claims yet, would otherwise be compared with nothing)."""
        values = self.shared if self.shared is not None else self.values
        c, f = values.get("claims"), values.get("forecast")
        return None if c is None or not f else 100 * c / f


@dataclass
class Comparison:
    view: View
    results: dict  # series -> query.Result
    rows: list[Row]
    totals: Row
    by: str = ""
    shared_years: list[int] | None = None  # the years both series cover, when they differ from the years shown
    series_years: dict = field(default_factory=dict)  # series -> its years with data

    @property
    def both(self) -> bool:
        return len(self.results) == 2

    @property
    def percent_note(self) -> str:
        """What "% of forecast" compares, when the two series do not cover the same years."""
        if not self.both or self.shared_years is None:
            return ""
        span = _span(self.series_years.get("claims", []))
        fspan = _span(self.series_years.get("forecast", []))
        if not self.shared_years:
            return f"% of forecast is blank: no year has both claims ({span}) and a forecast ({fspan})."
        return f"% of forecast compares the years both series cover, {_span(self.shared_years)} (claims {span}, forecast {fspan})."

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
        return f"{SERIES[s][0]} (as of {timezone.localtime(as_of):%Y-%m-%d %H:%M %Z})" if as_of else f"{SERIES[s][0]} (nothing published)"

    def downloads(self) -> list[dict]:
        """The rows as shown, every series in one file (BR-07.2; Jake, 2026-11-02)."""
        from . import downloads

        by = (self.by or self.view.by,)
        return downloads.links([replace(self.view.for_series(s), by=by) for s in self.results], self.view.page.exclude)


def compare(view: View, by: str | None = None, drill: tuple[str, str] = ("", "")) -> Comparison:
    """Each series grouped by the view-by dimension (or `by`, for a page's fixed section). `drill` sends rows to another page."""
    by = by or view.by
    results = {s: dq.run(replace(view.for_series(s), by=(by,))) for s in view.series}
    code = view.metric.code
    keyed: dict[object, Row] = {}
    for s, r in results.items():
        for row in r.rows:
            key = row[by]
            keyed.setdefault(key, Row(key, {})).values[s] = row[code]
    target, next_by = drill if drill[0] else ("" if view.page.view_by else "overview", DRILL.get(by))
    registered = set()
    if by == "program":  # a PrgID not in the registry has no program page (UAT 2, U06)
        registered = set(Program.objects.filter(prg_id__in=[str(k) for k in keyed if k is not None]).values_list("prg_id", flat=True))
    for row in keyed.values():
        if next_by and row.key is not None and by != "year":
            changes = {by: str(row.key), "by": next_by}
            if target != view.page.slug and target:
                changes |= {"metric": view.metric_choice if view.metric_choice in PAGES[target].metrics else None}
            row.drill = (f"/data/{target}/" if target and target != view.page.slug else "") + view.link(**changes)
        if row.key is not None and by == "program" and str(row.key) in registered:
            row.links.append(("Program page", f"/programs/{row.key}/"))
        if row.key is not None and by == "measure":
            for s in results:
                q = view.for_series(s)
                filters = dict(q.filters) | {"measure": (str(row.key),)}
                # every exclusion stated: the records page leaves nothing out unless asked
                qs = replace(q, by=(), filters=tuple(filters.items())).query_string(defaults_exclude=())
                row.links.append((f"{SERIES[s][0]} records", f"/data/records/?{qs}"))
    first = view.series[-1]  # claims when shown, else forecast: the chart's and the table's order
    if by == "year":
        rows = sorted(keyed.values(), key=lambda r: (r.key is None, r.key or 0))
    else:
        rows = sorted(keyed.values(), key=lambda r: (r.values.get(first) is None, -(r.values.get(first) or 0), str(r.key)))
    totals = Row("Total", {s: r.totals.get(code) for s, r in results.items()})
    comparison = Comparison(view, results, rows, totals, by)
    if len(results) == 2 and by != "year" and view.metric.adds_up:
        _share_years(view, by, comparison)
    return comparison


def code_label(value):
    """A registry code shown as words: "codes_and_standards" -> "Codes and standards"; anything else as it is."""
    if isinstance(value, str) and "_" in value and value == value.lower():
        return value.replace("_", " ").capitalize()
    return value


def _span(years) -> str:
    years = sorted(y for y in years if y is not None)
    if not years:
        return "no years"
    return str(years[0]) if years[0] == years[-1] else f"{years[0]}-{years[-1]}"


def _share_years(view: View, by: str, comparison: Comparison) -> None:
    """Restrict "% of forecast" to the years both series have data for: each series again by (row, year), the rows' and total's
    values over the shared years only. Nothing changes when the series cover the same years."""
    code = view.metric.code
    metric = catalog.get(code)
    by_year = {s: dq.run(replace(view.for_series(s), by=(by, "year"))) for s in comparison.results}
    years = {s: {r["year"] for r in res.rows} for s, res in by_year.items()}
    comparison.series_years = {s: sorted(y for y in ys if y is not None) for s, ys in years.items()}
    shared = years["claims"] & years["forecast"]
    if shared == years["claims"] | years["forecast"]:
        return
    comparison.shared_years = sorted(y for y in shared if y is not None)
    per_row: dict[object, dict] = {}
    total: dict[str, dict] = {}
    for s, res in by_year.items():
        for r in res.rows:
            if r["year"] not in shared:
                continue
            for sums in (per_row.setdefault(r[by], {}).setdefault(s, {}), total.setdefault(s, {})):
                for k, v in r["_sums"].items():
                    sums[k] = sums.get(k, 0) + v
    for row in comparison.rows:
        row.shared = {s: (metric.value(per_row.get(row.key, {}).get(s)) if per_row.get(row.key, {}).get(s) else None) for s in by_year}
    comparison.totals.shared = {s: (metric.value(total[s]) if total.get(s) else None) for s in by_year}


@dataclass
class Matrix:
    """Each row's metric split across a dimension's values, for one series: the shares (of the row's total) and the values."""

    series: str
    result: dq.Result
    by: str
    columns_by: str
    columns: list
    rows: list[dict]  # {"key", "total", "cells": [{"value", "share"}]}

    @property
    def label(self) -> str:
        return SERIES[self.series][0]


def matrix(view: View, rows: str, columns: str) -> Matrix:
    """For the series on the right of the comparison (claims when shown): a share bar's values kept in a table (spec page 3)."""
    series = view.series[-1]
    result = dq.run(replace(view.for_series(series), by=(rows, columns)))
    code = view.metric.code
    grid: dict[object, dict] = {}
    cols: list = []
    for r in result.rows:
        grid.setdefault(r[rows], {})[r[columns]] = r[code]
        if r[columns] not in cols:
            cols.append(r[columns])
    cols.sort(key=lambda c: (c is None, str(c)))
    out = []
    for key, cells in sorted(grid.items(), key=lambda kv: (kv[0] is None, str(kv[0]))):
        total = sum(v or 0 for v in cells.values())
        out.append(
            {
                "key": key,
                "total": total,
                "cells": [
                    {"value": cells.get(c), "share": (100 * cells[c] / total) if total and cells.get(c) is not None else None} for c in cols
                ],
            }
        )
    return Matrix(series, result, rows, columns, cols, out)


def applied_filters(view: View) -> list[dict]:
    """Each applied filter value with a link that removes it (the breadcrumb that drills back up)."""
    out = []
    for d, values in view.query.filters:
        for v in values:
            rest = [x for x in values if x != v]
            out.append({"label": dq.DIMENSIONS[d].label, "value": v, "remove": view.link(**{d: rest or None})})
    return out


def filter_options() -> dict[str, list[str]]:
    """The filter lists come from the registry, not the data, so each value appears once (spec "Filters"). The PAs are pa_picker's."""
    versions = ProgramVersion.objects.all()

    def distinct(f):
        # One choice per value whatever its case or spaces (the filters match on lower(trim()), query.py): filed data spell some
        # values two ways ("Resource Acquisition", "Resource acquisition"). The choice shows the spelling most programs use; the records
        # keep what was filed.
        counts = Counter(v.strip() for v in versions.values_list(f, flat=True) if v and v.strip())
        best: dict[str, str] = {}
        for v, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            best.setdefault(v.casefold(), v)
        return sorted(best.values(), key=str.casefold)

    return {
        "sector": distinct("primary_sector"),
        "segment": distinct("segment"),
        "implementer_type": distinct("implementer"),
        "measure_type": ["Energy efficiency", "Fuel substitution"],
    }


def pa_picker(selected: dict) -> dict:
    """The PA picker for a query's filter map (accounts.service.pa_picker: All PAs > PA types > PAs, then statewide programs)."""
    from cedars.services.accounts import service as access

    return access.pa_picker(selected.get("pa", ()), selected.get("pa_type", ()))


def filings() -> list[tuple[str, str]]:
    """The filing selector's choices: each cycle and kind in the current forecast release, newest first."""
    release = snapshots.current_release("forecast")
    if release is None:
        return []
    seen = {}
    for f in sorted(snapshots.release_files(release), key=lambda f: f["due_at"], reverse=True):
        seen.setdefault(f"{f['cycle']}|{f['kind']}", f"{f['cycle']} {f['kind']}".strip())
    return list(seen.items())


# --- chart geometry (server-rendered SVG, no script)


def chart(comparison: Comparison, limit: int = 0) -> dict:
    """Horizontal bars, one group per row and one bar per series, as percentages of the plot width: a zero line where values change
    sign, each bar from zero to its value. The labels and values are HTML beside the bars (they reflow at 320 px); the table below
    is the text alternative."""
    series = list(comparison.results)
    values = [
        r.values.get(s) for r in (comparison.rows[:limit] if limit else comparison.rows) for s in series if r.values.get(s) is not None
    ]
    low, high = min([0, *values]), max([0, *values])
    span = (high - low) or 1
    zero = 100 * (0 - low) / span
    groups = []
    shown = comparison.rows[:limit] if limit else comparison.rows
    for r in shown:
        bars = []
        for i, s in enumerate(series):
            v = r.values.get(s)
            if v is None:
                bars.append({"series": s, "x": zero, "w": 0, "y": 14 * i, "missing": True, "value": None})
                continue
            x = 100 * (min(v, 0) - low) / span
            bars.append({"series": s, "x": round(x, 3), "w": round(100 * abs(v) / span, 3), "y": 14 * i, "missing": False, "value": v})
        groups.append({"label": code_label(r.key), "bars": bars})
    return {
        "series": [{"code": s, "label": SERIES[s][0]} for s in series],
        "groups": groups,
        "zero": round(zero, 3),
        "negative": low < 0,
        "height": 14 * len(series),
        "limited": len(shown) < len(comparison.rows),
        "shown": len(shown),
    }


def describe(chart: dict, fmt) -> dict:
    """Give each of a chart's groups its values in words (`tip`: series code or "goal", label, value), shown when the pointer is over
    the group or it has the keyboard focus, and read by screen readers once the page's script makes the chart navigable
    (data/dashboard.js). `fmt` writes one value with its unit; a series with no value reads "none" (blank is not zero)."""
    labels = {s["code"]: s["label"] for s in chart["series"]}
    for g in chart["groups"]:
        tip = [(b["series"], labels[b["series"]], "none" if b["missing"] else fmt(b["value"])) for b in g["bars"]]
        if chart.get("goal") and g.get("goal_value") is not None:
            tip.append(("goal", chart["goal"], fmt(g["goal_value"])))
        g["tip"] = tip
    return chart

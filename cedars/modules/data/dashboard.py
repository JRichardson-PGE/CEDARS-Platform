"""The dashboard framework (spec "Shared controls"): a page's view state from GET parameters, forecast and claims side by side from one
query each, drill links, the applied-filter list and the chart geometry. Pages differ only in their `Page` entry; the view and the
templates are shared.

Every series is one `query.run` on its dataset with the same parameters, so the chart, the table and each series' download agree."""

from dataclasses import dataclass, field, replace
from datetime import date
from urllib.parse import urlencode

from cedars.services.registry.models import FilingCycle, ProgramVersion
from cedars.services.snapshots import service as snapshots

from . import metrics as catalog
from . import query as dq


@dataclass(frozen=True)
class Page:
    slug: str
    title: str
    description: str
    metrics: tuple[str, ...]  # the metric control's choices (catalog codes or view-control bases); the first is the default
    exclude: tuple[str, ...] = dq.DEFAULT_EXCLUDE
    series: tuple[str, ...] = ("forecast", "claims")


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
    ]
}

SERIES = {"forecast": ("Forecast", "forecast"), "claims": ("Claimed", "claims")}
TEXT_FILTERS = ("use_category", "program", "measure")
FILTERS = ("pa", "sector", "segment", "implementer_type", "use_category", "program", "measure", "measure_type")
# One level down from each view-by (spec "Drill"): PA > sector > program > measure.
DRILL = {
    "pa": "sector",
    "sector": "program",
    "segment": "program",
    "implementer_type": "program",
    "use_category": "measure",
    "program": "measure",
}
VIEW_PARAMS = ("series", "filing", "year_from", "year_to", "by", "metric", "gross", "lifecycle", "cs", "esa", "pension") + FILTERS


def current_cycle(today: date | None = None) -> FilingCycle | None:
    year = (today or date.today()).year
    return FilingCycle.objects.filter(first_year__lte=year, last_year__gte=year).order_by("-first_year").first()


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
    qparams = dict(raw) | {"metric": [choice], "by": [by]}
    if "year_from" not in raw and "year_to" not in raw and (cycle := current_cycle()):
        qparams |= {"year_from": [str(cycle.first_year)], "year_to": [str(cycle.last_year)]}
    q = dq.from_params(qparams, dataset="claims", exclude=page.exclude)
    return View(page, series, q, choice, raw)


# --- results


@dataclass
class Row:
    key: object  # the view-by value
    values: dict  # series -> metric value
    drill: str = ""  # the link one level down, or "" at the bottom

    def value(self, series):
        return self.values.get(series)

    @property
    def difference(self):
        c, f = self.values.get("claims"), self.values.get("forecast")
        return None if c is None or f is None else c - f

    @property
    def percent(self):
        c, f = self.values.get("claims"), self.values.get("forecast")
        return None if c is None or not f else 100 * c / f


@dataclass
class Comparison:
    view: View
    results: dict  # series -> query.Result
    rows: list[Row]
    totals: Row

    @property
    def both(self) -> bool:
        return len(self.results) == 2

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
        """One download per series, each the series' own query (BR-07.2)."""
        out = []
        for s in self.results:
            qs = self.view.for_series(s).query_string(self.view.page.exclude)
            out.append(
                {"series": SERIES[s][0], "links": [(fmt.upper(), f"/data/download.{fmt}?{qs}") for fmt in ("csv", "xlsx", "parquet")]}
            )
        return out


def compare(view: View) -> Comparison:
    results = {s: dq.run(view.for_series(s)) for s in view.series}
    code = view.metric.code
    keyed: dict[object, Row] = {}
    for s, r in results.items():
        for row in r.rows:
            key = row[view.by]
            keyed.setdefault(key, Row(key, {})).values[s] = row[code]
    next_by = DRILL.get(view.by)
    for row in keyed.values():
        if next_by and row.key is not None:
            row.drill = view.link(**{view.by: str(row.key), "by": next_by})
    first = view.series[-1]  # claims when shown, else forecast: the chart's and the table's order
    rows = sorted(keyed.values(), key=lambda r: (r.values.get(first) is None, -(r.values.get(first) or 0), str(r.key)))
    totals = Row("Total", {s: r.totals.get(code) for s, r in results.items()})
    return Comparison(view, results, rows, totals)


def applied_filters(view: View) -> list[dict]:
    """Each applied filter value with a link that removes it (the breadcrumb that drills back up)."""
    out = []
    for d, values in view.query.filters:
        for v in values:
            rest = [x for x in values if x != v]
            out.append({"label": dq.DIMENSIONS[d].label, "value": v, "remove": view.link(**{d: rest or None})})
    return out


def filter_options() -> dict[str, list[str]]:
    """The filter lists come from the registry, not the data, so each value appears once (spec "Filters")."""
    from cedars.services.accounts.models import Organisation

    versions = ProgramVersion.objects.all()

    def distinct(f):
        return sorted({v for v in versions.values_list(f, flat=True).distinct() if v})

    return {
        "pa": list(Organisation.objects.filter(kind="pa").order_by("code").values_list("code", flat=True)),
        "sector": distinct("primary_sector"),
        "segment": distinct("segment"),
        "implementer_type": distinct("implementer"),
        "measure_type": ["Energy efficiency", "Fuel substitution"],
    }


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


def chart(comparison: Comparison) -> dict:
    """Horizontal bars, one group per row and one bar per series, as percentages of the plot width: a zero line where values change
    sign, each bar from zero to its value. The labels and values are HTML beside the bars (they reflow at 320 px); the table below
    is the text alternative."""
    series = list(comparison.results)
    values = [r.values.get(s) for r in comparison.rows for s in series if r.values.get(s) is not None]
    low, high = min([0, *values]), max([0, *values])
    span = (high - low) or 1
    zero = 100 * (0 - low) / span
    groups = []
    for r in comparison.rows:
        bars = []
        for i, s in enumerate(series):
            v = r.values.get(s)
            if v is None:
                bars.append({"series": s, "x": zero, "w": 0, "y": 14 * i, "missing": True})
                continue
            x = 100 * (min(v, 0) - low) / span
            bars.append({"series": s, "x": round(x, 3), "w": round(100 * abs(v) / span, 3), "y": 14 * i, "missing": False})
        groups.append({"label": r.key, "bars": bars})
    return {
        "series": [{"code": s, "label": SERIES[s][0]} for s in series],
        "groups": groups,
        "zero": round(zero, 3),
        "negative": low < 0,
        "height": 14 * len(series),
    }

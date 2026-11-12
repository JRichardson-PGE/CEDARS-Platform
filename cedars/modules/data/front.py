"""The front page's portfolio trends (Jake, 2026-11-11): how California's energy efficiency portfolio is doing, year by
year, from the published claims, drawn as charts that each lead to a dashboard page.

One metric is chosen at a time (total system benefit, electricity saved, peak demand avoided, gas saved or spending), for every PA or one
PA type, and one claim year (the latest by default). The page shows:

  tiles      every metric in the chosen year, its change from the year before and a sparkline of every year; a tile chooses its metric
  by year    the chosen metric for every claim year, the change from the year before under each column, the average of the years with
             data, the current program cycle shaded, and the years before the metric was first published said in words
  indexed    the metric and spending (total system benefit when the metric is spending), each as a share of its value in the first year
             both were published (= 100): one axis for two measures of different scale
  by PA      the metric for the largest PAs (and the rest together) in the first published year and the chosen year
  per $1     total system benefit over claimed spending, each year both are published

Every number is summed from the claims release with the dashboards' default exclusions (query.DEFAULT_EXCLUDE), so each chart agrees with
the dashboard page it links to. The page reads one query: every metric by year, PA type and PA, cached (below); the choices are applied to
it in the request.

The charts are server-rendered SVG with a table of the same values under each (in a <details>); data/front.js adds values on hover and
from the keyboard. No inline styles: every size is an SVG attribute."""

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils.html import escape

from cedars.services.snapshots import service as snapshots

from . import dashboard
from . import metrics as catalog
from . import query as dq

log = logging.getLogger(__name__)

# The query reads every published claims file, too slow to make while a visitor waits (Jake, 2026-11-06: the front page was sometimes very
# slow). The rows are kept for KEEP_SECONDS; a copy older than FRESH_SECONDS is still shown while new rows are made after the response. A
# new claims release changes the fingerprint, and the first visit after it reads them in the request, so nobody sees the previous release.
# `manage.py warm_front_page` reads them before anyone asks (at start-up, after a deploy or a publication).
FRESH_SECONDS = 600
KEEP_SECONDS = 7 * 24 * 3600
REFRESH_LOCK_SECONDS = 300  # one rebuild at a time; a rebuild that dies frees the lock after this
CACHE_VERSION = 6  # the cached entry's shape: bump when it changes, so a deploy never reads an old pickle
PA_TYPES = ("IOU", "REN", "CCA")
TOP_PAS = 4  # the PAs named in the by-PA chart; the rest are added up as "Other PAs"
PHONE_YEARS = 5  # the by-year chart on a narrow screen shows the last few years only


@dataclass(frozen=True)
class Metric:
    key: str  # the page's ?metric= value and the dashboard's metric code
    title: str
    field: str  # the query catalog's metric, read in its fields' own units ($, kWh, kW, therms; `_raw`)
    scale: float  # the value is divided by this for display (kWh / 1e6 = GWh)
    unit: str  # after the number ("GWh"); money is "$" (before it, scaled to K, M or B)
    link: str  # the dashboard page that breaks it down
    noun: str  # in a sentence: "electricity saved"


METRICS = [
    Metric("tsb", "Total system benefit", "tsb", 1, "$", "/data/total-system-benefit/", "total system benefit"),
    Metric("kwh", "Electricity saved", "kwh_first_year_net", 1e6, "GWh", "/data/overview/?metric=kwh", "electricity saved"),
    Metric("kw", "Peak demand avoided", "kw_first_year_net", 1e3, "MW", "/data/overview/?metric=kw", "peak demand avoided"),
    Metric("therms", "Gas saved", "therms_first_year_net", 1e6, "million therms", "/data/overview/?metric=therms", "gas saved"),
    Metric("expenditure", "Spending", "expenditure", 1, "$", "/data/budget/", "spending"),
]
BY_KEY = {m.key: m for m in METRICS}
SUBTITLES = {
    "tsb": "Lifetime benefit to the electric and gas systems of each year's claims",
    "kwh": "Net first-year savings",
    "kw": "Net first-year peak demand avoided",
    "therms": "Net first-year savings",
    "expenditure": "Claimed expenditure",
}


# --- numbers in words


def _sig(x: float) -> str:
    """A number at three significant figures or whole: 3,137; 12.3; 1.23."""
    a = abs(x)
    if a >= 100 or a == 0:
        return f"{x:,.0f}"
    return f"{x:,.1f}" if a >= 10 else f"{x:,.2f}"


def money(value: float) -> str:
    """Dollars, short: $3.75B, $313M, $48.2K, $480."""
    sign, a = ("-" if value < 0 else ""), abs(value)
    for size, letter in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= size:
            return f"{sign}${_sig(a / size)}{letter}"
    return f"{sign}${a:,.0f}"


def amount(metric: Metric, value: float | None, unit: bool = True) -> str:
    if value is None:
        return "none published"
    if metric.unit == "$":
        return money(value)
    shown = _sig(value / metric.scale)
    return f"{shown} {metric.unit}" if unit else shown


def tick(metric: Metric, value: float) -> str:
    """A grid line's label: as round as the line ($6B, $250M, 1,500, 0.5)."""
    if metric.unit == "$":
        a = abs(value)
        for size, letter in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if a >= size:
                return f"{'-' if value < 0 else ''}${a / size:,.4g}{letter}"
        return f"${value:,.0f}"
    return f"{value / metric.scale:,.4g}"


def split_amount(metric: Metric, value: float) -> tuple[str, str]:
    """The tile's number and its unit, apart: ("$3.75", "billion"), ("3,137", "GWh")."""
    if metric.unit != "$":
        return _sig(value / metric.scale), metric.unit
    a = abs(value)
    for size, word in ((1e9, "billion"), (1e6, "million"), (1e3, "thousand")):
        if a >= size:
            return ("-" if value < 0 else "") + "$" + _sig(a / size), word
    return money(value), ""


def change(now: float | None, before: float | None, year: int) -> dict | None:
    """The change from the year before: {"dir": up/down/same, "text": "up 4% from 2024"}; None without both years (or from 0)."""
    if now is None or before is None or before == 0:
        return None
    pct = 100 * (now - before) / abs(before)
    if abs(pct) < 0.5:
        return {"dir": "same", "pct": pct, "text": f"about the same as {year}"}
    word = "up" if pct > 0 else "down"
    return {"dir": word, "pct": pct, "text": f"{word} {abs(pct):.0f}% from {year}"}


def span(years) -> str:
    years = sorted(years)
    if not years:
        return ""
    return str(years[0]) if years[0] == years[-1] else f"{years[0]}-{years[-1]}"


# --- the cached rows


@dataclass
class Rows:
    """Every metric by year, PA type and PA, from the current claims release."""

    rows: list[tuple]  # (year, pa_type or "", pa, {field: value or None})
    as_of: object = None
    notes: list[str] = field(default_factory=list)


def _cache_key() -> str:
    return f"front{CACHE_VERSION}:{'-'.join(dq.DEFAULT_EXCLUDE)}"


def _fingerprint() -> str:
    release = snapshots.current_release("claims")
    return str(release.pk) if release else "0"


def _raw(r: dict, code: str) -> float | None:
    """A query row's metric in its fields' own units ($, kWh, kW, therms), or None when no record of the row carries the field. The
    query's own value is already divided by the catalog's display scale (kWh to MWh, therms to MMTherms) and counts a field no file
    has as 0; the front page scales once, by its own Metric.scale, and says "none published" for a field nobody filed."""
    m = catalog.get(code)
    filled = [f for f in m.numerator if f in r.get("_filled", ())]
    return sum(r["_sums"][f] for f in filled) if filled else None


def _read() -> Rows:
    fields = tuple(m.field for m in METRICS)
    result = dq.run(dq.Query("claims", fields, by=("year", "pa_type", "pa")))
    rows = [
        (r["year"], (r.get("pa_type") or "").upper(), r.get("pa") or "", {f: _raw(r, f) for f in fields})
        for r in result.rows
        if r.get("year") is not None
    ]
    return Rows(rows, as_of=result.as_of, notes=[n for n in result.notes if n.startswith("Nothing")])


def _store(fingerprint: str) -> Rows:
    rows = _read()
    cache.set(_cache_key(), {"fingerprint": fingerprint, "built": time.time(), "rows": rows}, KEEP_SECONDS)
    return rows


def _refresh(fingerprint: str, lock: str) -> None:
    try:
        _store(fingerprint)
    except Exception:  # the old rows stay; the next visit after the lock expires tries again
        log.exception("front page: rereading the claims failed")
    finally:
        cache.delete(lock)
        if threading.current_thread() is not threading.main_thread():
            connection.close()  # this thread's own database connection


def _refresh_later(fingerprint: str) -> None:
    """Read the rows again after this response, unless another request already is (FRONT_PAGE_REFRESH_IN_BACKGROUND off, as in the
    tests: in this request)."""
    lock = _cache_key() + ":refreshing"
    if not cache.add(lock, 1, REFRESH_LOCK_SECONDS):
        return
    if not settings.FRONT_PAGE_REFRESH_IN_BACKGROUND:
        _refresh(fingerprint, lock)
        return
    threading.Thread(target=_refresh, args=(fingerprint, lock), name="front-page-refresh", daemon=True).start()


def rows() -> Rows:
    fingerprint = _fingerprint()
    entry = cache.get(_cache_key())
    if not entry or entry.get("fingerprint") != fingerprint:
        return _store(fingerprint)  # nothing kept, or read from another release
    if time.time() - entry["built"] > FRESH_SECONDS:
        _refresh_later(fingerprint)
    return entry["rows"]


def warm() -> float:
    """Read the rows and keep them; returns the seconds it took."""
    t = time.time()
    _store(_fingerprint())
    return time.time() - t


# --- the page


@dataclass
class Trends:
    metric: Metric
    pa_type: str  # "" = every PA
    year: int | None  # the chosen claim year
    years: list[int]  # every claim year published
    pa_types: list[str]  # the PA types that have rows
    as_of: object = None
    tiles: list[dict] = field(default_factory=list)
    charts: dict = field(default_factory=dict)  # by_year, by_year_recent, indexed, by_pa, per_dollar: {"svg", "table", "tips", ...}
    notes: list[str] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)

    def link(self, **changes) -> str:
        params = {"metric": self.metric.key, "type": self.pa_type, "year": self.year} | changes
        default = {"metric": "tsb", "type": "", "year": self.years[-1] if self.years else None}
        kept = {k: v for k, v in params.items() if v not in (None, "") and v != default[k]}
        return "?" + urlencode(kept) if kept else "?"

    @property
    def metric_links(self) -> list[dict]:
        return [{"key": m.key, "title": m.title, "href": self.link(metric=m.key), "current": m is self.metric} for m in METRICS]

    @property
    def type_links(self) -> list[dict]:
        return [{"label": t or "All", "href": self.link(type=t), "current": t == self.pa_type} for t in ("", *self.pa_types)]

    @property
    def who(self) -> str:
        return {"": "every PA", "IOU": "the IOUs", "REN": "the RENs", "CCA": "the CCAs"}.get(self.pa_type, self.pa_type)

    @property
    def subtitle(self) -> str:
        return SUBTITLES[self.metric.key]

    @property
    def metric_href(self) -> str:
        return self.dashboard(self.metric.link)

    def dashboard(self, base: str, **extra) -> str:
        """A dashboard page's link, narrowed to the chosen PA type."""
        params = ({"pa_type": self.pa_type} if self.pa_type else {}) | extra
        if not params:
            return base
        return base + ("&" if "?" in base else "?") + urlencode(params)


def _choice(params, name: str, allowed) -> str:
    value = (params.get(name) or "").strip()
    return value if value in allowed else ""


def trends(params=None) -> Trends:
    params = params or {}
    data = rows()
    metric = BY_KEY.get(_choice(params, "metric", BY_KEY)) or METRICS[0]
    pa_types = [t for t in PA_TYPES if any(r[1] == t for r in data.rows)]
    pa_type = _choice(params, "type", pa_types)
    years = sorted({r[0] for r in data.rows})
    year_param = (params.get("year") or "").strip()
    year = int(year_param) if year_param.isdigit() and int(year_param) in years else (years[-1] if years else None)
    t = Trends(metric, pa_type, year, years, pa_types, as_of=data.as_of, notes=list(data.notes))
    if not years:
        t.notes = t.notes or ["No claims are published yet, so there is nothing to chart."]
        return t
    kept = [r for r in data.rows if not pa_type or r[1] == pa_type]

    def series(m: Metric) -> dict[int, float]:
        out: dict[int, float] = {}
        for y, _, _, values in kept:
            v = values.get(m.field)
            if v is not None:
                out[y] = out.get(y, 0) + v
        return out

    all_series = {m.key: series(m) for m in METRICS}
    t.tiles = [_tile(t, m, all_series[m.key]) for m in METRICS]
    s = all_series[metric.key]
    t.charts["by_year"] = _by_year(t, s, years)
    if len(years) > PHONE_YEARS:
        t.charts["by_year_recent"] = _by_year(t, s, years[-PHONE_YEARS:], recent=True)
    other = BY_KEY["tsb" if metric.key == "expenditure" else "expenditure"]
    t.charts["indexed"] = _indexed(t, metric, s, other, all_series[other.key])
    t.charts["by_pa"] = _by_pa(t, kept, s)
    t.charts["per_dollar"] = _per_dollar(t, all_series["tsb"], all_series["expenditure"])
    labels = [dq.EXCLUSIONS[e].label for e in dq.DEFAULT_EXCLUDE]
    t.footnotes.append(
        f"Claims as published, adding up {t.who}. Like the dashboards, the totals leave out {_words(labels)}; each dashboard "
        "page can put them back."
    )
    return t


def _words(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _tile(t: Trends, m: Metric, s: dict[int, float]) -> dict:
    value = s.get(t.year)
    number, unit = split_amount(m, value) if value is not None else ("None", "published")
    points = [(y, s[y]) for y in t.years if y in s]
    return {
        "key": m.key,
        "title": m.title,
        "href": t.link(metric=m.key),
        "current": m is t.metric,
        "number": number,
        "unit": unit,
        "change": change(value, s.get(t.year - 1), t.year - 1),
        "spark": _spark(points, t.years),
        "since": min(s) if s else None,
    }


def _spark(points, years, w=200, h=44) -> dict | None:
    if len(points) < 2:
        return None
    lo = min(0, *(v for _, v in points))
    hi = max(v for _, v in points)
    hi = hi if hi > lo else lo + 1
    first, last = years[0], years[-1]

    def x(y):
        return 4 + (w - 8) * (y - first) / max(last - first, 1)

    def yv(v):
        return h - 2 - (h - 8) * (v - lo) / (hi - lo)

    line = " ".join(f"{x(y):.1f},{yv(v):.1f}" for y, v in points)
    area = f"{x(points[0][0]):.1f},{h} {line} {x(points[-1][0]):.1f},{h}"
    return {"w": w, "h": h, "line": line, "area": area, "dot": (round(x(points[-1][0]), 1), round(yv(points[-1][1]), 1))}


def _ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Round grid lines from low (0 or below) to past high."""
    rough = (high - low) / max(count - 1, 1) or 1
    power = 10 ** math.floor(math.log10(rough))
    step = next(power * k for k in (1, 2, 2.5, 5, 10) if power * k >= rough)
    start = math.floor(low / step) * step
    out, v = [], start
    while v < high + step * 0.999:
        out.append(round(v, 10))
        v += step
    return out


def _svg_open(name: str, title: str, w: int, h: int) -> str:
    return (
        f'<svg class="trend-svg" viewBox="0 0 {w} {h}" role="img" aria-labelledby="{name}-t" focusable="false">'
        f'<title id="{name}-t">{escape(title)}</title>'
    )


def _column(x: float, top: float, base: float, width: float, cls: str) -> str:
    """A column with rounded top corners (4 px), anchored to the baseline; a negative one hangs below it with its rounded end down."""
    r = min(4.0, width / 2, abs(base - top) / 2)
    if top <= base:
        return (
            f'<path class="{cls}" d="M{x:.1f},{base:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} '
            f'H{x + width - r:.1f} Q{x + width:.1f},{top:.1f} {x + width:.1f},{top + r:.1f} V{base:.1f} Z"/>'
        )
    return (
        f'<path class="{cls}" d="M{x:.1f},{base:.1f} V{top - r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} '
        f'H{x + width - r:.1f} Q{x + width:.1f},{top:.1f} {x + width:.1f},{top - r:.1f} V{base:.1f} Z"/>'
    )


def _by_year(t: Trends, s: dict[int, float], years: list[int], recent: bool = False) -> dict:
    m = t.metric
    w, h = (820, 330) if not recent else (420, 300)
    pad_l, pad_b, pad_t, pad_r = 58, 46, 26, 8
    values = [s[y] for y in years if y in s]
    name = "by-year-recent" if recent else "by-year"
    title = f"{m.title} by claim year, {span(years)}, {t.who}"
    out = {"name": name, "title": f"{m.title} by year", "years": years, "tips": [], "table": []}
    if not values:
        out["empty"] = f"No {m.noun} is published for {span(years)}."
        return out
    low, high = min(0, *values), max(0, *values)
    ticks = _ticks(low, high * 1.08 if high > 0 else high)
    lo, hi = ticks[0], ticks[-1]
    plot_h = h - pad_b - pad_t
    bw = (w - pad_l - pad_r) / len(years)

    def sy(v):
        return pad_t + plot_h * (hi - v) / ((hi - lo) or 1)

    parts = [_svg_open(name, title, w, h)]
    for v in ticks:
        parts.append(f'<line class="grid" x1="{pad_l}" x2="{w - pad_r}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>')
        parts.append(f'<text class="tick" x="{pad_l - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{escape(tick(m, v))}</text>')
    cycle = dashboard.current_cycle()
    if cycle:
        shaded = [i for i, y in enumerate(years) if cycle.first_year <= y <= cycle.last_year]
        if shaded:
            x0 = pad_l + bw * shaded[0]
            parts.append(
                f'<rect class="band" x="{x0:.1f}" y="{pad_t - 20}" width="{bw * len(shaded):.1f}" height="{plot_h + 20}"/>'
                f'<text class="note" x="{x0 + 6:.1f}" y="{pad_t - 6}">{escape(str(cycle))} cycle</text>'
            )
    missing = [i for i, y in enumerate(years) if y not in s and y < min(s)]
    if len(missing) >= 2:
        x0, x1 = pad_l + bw * missing[0] + 4, pad_l + bw * (missing[-1] + 1) - 4
        mid = (x0 + x1) / 2
        parts.append(f'<rect class="missing" x="{x0:.1f}" y="{pad_t + 20}" width="{x1 - x0:.1f}" height="{plot_h - 20}" rx="4"/>')
        parts.append(
            f'<text class="note" x="{mid:.1f}" y="{pad_t + plot_h / 2:.1f}" text-anchor="middle">'
            f"No {escape(m.noun)} published before {min(s)}</text>"
        )
    zero = sy(0)
    for i, y in enumerate(years):
        x = pad_l + bw * i + bw * 0.18
        cx = x + bw * 0.32
        parts.append(f'<text class="tick" x="{cx:.1f}" y="{h - pad_b + 16}" text-anchor="middle">{y}</text>')
        v = s.get(y)
        if v is None:
            continue
        before = s.get(y - 1)
        ch = change(v, before, y - 1)
        tip = f"{y}: {amount(m, v)}" + (f", {ch['text']}" if ch else "")
        cls = "col" + (" chosen" if y == t.year else "") + (" negative" if v < 0 else "")
        parts.append(f'<g class="pt" data-tip="{escape(tip)}">')
        parts.append(f'<rect class="hit" x="{pad_l + bw * i:.1f}" y="{pad_t - 20}" width="{bw:.1f}" height="{h - pad_t + 20}"/>')
        parts.append(_column(x, sy(v), zero, bw * 0.64, cls))
        label_y = sy(v) - 6 if v >= 0 else sy(v) + 14
        parts.append(f'<text class="val" x="{cx:.1f}" y="{label_y:.1f}" text-anchor="middle">{escape(amount(m, v, unit=False))}</text>')
        if ch:
            arrow = {"up": "▲", "down": "▼", "same": "●"}[ch["dir"]]
            shown = f"{arrow} {abs(ch['pct']):.0f}%" if ch["dir"] != "same" else "● 0%"
            parts.append(f'<text class="chg {ch["dir"]}" x="{cx:.1f}" y="{h - pad_b + 34}" text-anchor="middle">{shown}</text>')
        parts.append("</g>")
        out["tips"].append(tip)
        out["table"].append({"year": y, "value": amount(m, v), "change": ch["text"] if ch else ""})
    shown_values = [s[y] for y in years if y in s]
    if len(shown_values) >= 2:
        avg = sum(shown_values) / len(shown_values)
        first_x = pad_l + bw * min(i for i, y in enumerate(years) if y in s)
        parts.append(f'<line class="ref" x1="{first_x:.1f}" x2="{w - pad_r}" y1="{sy(avg):.1f}" y2="{sy(avg):.1f}"/>')
        n = len(shown_values)
        label_x, anchor = (first_x - 8, "end") if first_x > pad_l + 140 else (w - pad_r - 4, "end")
        parts.append(
            f'<text class="note" x="{label_x:.1f}" y="{sy(avg) + (4 if anchor == "end" and first_x > pad_l + 140 else -6):.1f}" '
            f'text-anchor="{anchor}">{n}-year average {escape(amount(m, avg, unit=False))}</text>'
        )
        out["average"] = amount(m, avg)
    parts.append(f'<line class="axis" x1="{pad_l}" x2="{w - pad_r}" y1="{zero:.1f}" y2="{zero:.1f}"/></svg>')
    out["svg"] = "".join(parts)
    return out


def _indexed(t: Trends, m: Metric, s: dict[int, float], other: Metric, o: dict[int, float]) -> dict:
    both = [y for y in t.years if s.get(y) and o.get(y)]
    out = {"name": "indexed", "title": f"{m.title} and {other.noun}", "tips": [], "table": [], "metric": m.title, "other": other.title}
    if len(both) < 2:
        out["empty"] = f"{m.title} and {other.noun} need two years published together to compare."
        return out
    base = both[0]
    out["base"] = base
    a = {y: 100 * s[y] / s[base] for y in both}
    b = {y: 100 * o[y] / o[base] for y in both}
    w, h, pad_l, pad_b, pad_t, pad_r = 420, 260, 40, 26, 14, 104
    values = [*a.values(), *b.values(), 100]
    ticks = _ticks(min(values) * 0.95 if min(values) > 0 else min(values), max(values) * 1.03, 5)
    lo, hi = ticks[0], ticks[-1]

    def sx(y):
        return pad_l + (w - pad_l - pad_r) * (y - both[0]) / (both[-1] - both[0])

    def sy(v):
        return h - pad_b - (h - pad_b - pad_t) * (v - lo) / ((hi - lo) or 1)

    parts = [_svg_open("indexed", f"{m.title} and {other.noun}, each as a share of {base} ({base} = 100), {t.who}", w, h)]
    for v in ticks:
        parts.append(f'<line class="grid" x1="{pad_l}" x2="{w - pad_r}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>')
        parts.append(f'<text class="tick" x="{pad_l - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
    parts.append(f'<line class="ref" x1="{pad_l}" x2="{w - pad_r}" y1="{sy(100):.1f}" y2="{sy(100):.1f}"/>')
    step = max(1, math.ceil(len(both) / 5))
    for y in both[::step] if both[-1] in both[::step] else [*both[::step], both[-1]]:
        parts.append(f'<text class="tick" x="{sx(y):.1f}" y="{h - 8}" text-anchor="middle">{y}</text>')
    parts.append('<polyline class="line-a" points="' + " ".join(f"{sx(y):.1f},{sy(a[y]):.1f}" for y in both) + '"/>')
    parts.append('<polyline class="line-b" points="' + " ".join(f"{sx(y):.1f},{sy(b[y]):.1f}" for y in both) + '"/>')
    width = (w - pad_l - pad_r) / max(len(both) - 1, 1)
    for y in both:
        tip = f"{y}: {m.title} {a[y]:.0f} ({amount(m, s[y])}), {other.noun} {b[y]:.0f} ({amount(other, o[y])})"
        parts.append(f'<g class="pt" data-tip="{escape(tip)}">')
        parts.append(f'<rect class="hit" x="{sx(y) - width / 2:.1f}" y="{pad_t}" width="{width:.1f}" height="{h - pad_b - pad_t}"/>')
        parts.append(f'<circle class="dot-a" cx="{sx(y):.1f}" cy="{sy(a[y]):.1f}" r="4"/>')
        parts.append(f'<circle class="dot-b" cx="{sx(y):.1f}" cy="{sy(b[y]):.1f}" r="4"/></g>')
        out["tips"].append(tip)
        out["table"].append({"year": y, "a": f"{a[y]:.0f}", "b": f"{b[y]:.0f}"})
    last = both[-1]
    ya, yb = sy(a[last]), sy(b[last])
    if abs(ya - yb) < 14:  # labels apart
        ya, yb = (ya - 7, yb + 7) if ya <= yb else (ya + 7, yb - 7)
    parts.append(f'<text class="val" x="{w - pad_r + 8}" y="{ya + 4:.1f}">{escape(_short(m.title))} {a[last]:.0f}</text>')
    parts.append(f'<text class="val" x="{w - pad_r + 8}" y="{yb + 4:.1f}">{escape(_short(other.title))} {b[last]:.0f}</text></svg>')
    out["svg"] = "".join(parts)
    out["first"], out["last"] = base, last
    return out


def _short(title: str) -> str:
    return {"Total system benefit": "Benefit", "Electricity saved": "Electricity", "Peak demand avoided": "Demand", "Gas saved": "Gas"}.get(
        title, title
    )


def _by_pa(t: Trends, kept: list[tuple], s: dict[int, float]) -> dict:
    m = t.metric
    out = {"name": "by-pa", "tips": [], "table": []}
    published = sorted(s)
    if not published or t.year not in s:
        out["empty"] = f"No {m.noun} is published for {t.year}."
        out["title"] = f"{m.title} by PA"
        return out
    start = published[0] if published[0] < t.year else None
    out["title"] = f"{start} to {t.year}, by PA" if start else f"{t.year}, by PA"
    out["start"], out["end"] = start, t.year
    per: dict[str, dict[int, float]] = {}
    for y, _, pa, values in kept:
        v = values.get(m.field)
        if v is not None and y in (start, t.year):
            per.setdefault(pa, {})[y] = per.setdefault(pa, {}).get(y, 0) + v
    ranked = sorted(per, key=lambda pa: -(per[pa].get(t.year) or 0))
    groups = [(pa, pa, per[pa]) for pa in ranked[:TOP_PAS]]
    rest = ranked[TOP_PAS:]
    if rest:
        summed: dict[int, float] = {}
        for pa in rest:
            for y, v in per[pa].items():
                summed[y] = summed.get(y, 0) + v
        groups.append(("", f"Other PAs ({len(rest)})", summed))
    w, row_h, pad_l, pad_r, pad_t = 760, 34, 130, 150, 24
    h = pad_t + row_h * len(groups) + 6
    vals = [v for _, _, d in groups for v in d.values()]
    lo, hi = min(0, *vals), max(0, *vals) * 1.04 or 1

    def sx(v):
        return pad_l + (w - pad_l - pad_r) * (v - lo) / ((hi - lo) or 1)

    parts = [_svg_open("by-pa", f"{m.title} by PA, {start} and {t.year}" if start else f"{m.title} by PA, {t.year}", w, h)]
    if start:
        parts.append(f'<circle class="dot-start" cx="{pad_l + 6}" cy="10" r="5"/><text class="tick" x="{pad_l + 16}" y="14">{start}</text>')
        parts.append(f'<circle class="dot-end" cx="{pad_l + 70}" cy="10" r="6"/><text class="tick" x="{pad_l + 81}" y="14">{t.year}</text>')
    for i, (code, label, d) in enumerate(groups):
        yy = pad_t + row_h * i + row_h / 2
        a, b = d.get(start) if start else None, d.get(t.year)
        ch = change(b, a, start) if start else None
        tip = f"{label}: {amount(m, b)} in {t.year}" + (f", {amount(m, a)} in {start}" if a is not None else "")
        parts.append(f'<g class="pt" data-tip="{escape(tip)}">')
        parts.append(f'<rect class="hit" x="0" y="{yy - row_h / 2:.1f}" width="{w}" height="{row_h}"/>')
        parts.append(f'<text class="label" x="{pad_l - 10}" y="{yy + 4:.1f}" text-anchor="end">{escape(label)}</text>')
        if a is not None and b is not None:
            parts.append(f'<line class="span" x1="{sx(a):.1f}" x2="{sx(b):.1f}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        if a is not None:
            parts.append(f'<circle class="dot-start" cx="{sx(a):.1f}" cy="{yy:.1f}" r="5"/>')
        if b is not None:
            parts.append(f'<circle class="dot-end" cx="{sx(b):.1f}" cy="{yy:.1f}" r="6"/>')
            right = max(sx(b), sx(a) if a is not None else 0)
            parts.append(f'<text class="val" x="{right + 12:.1f}" y="{yy + 4:.1f}">{escape(amount(m, b, unit=False))}</text>')
        if ch:
            sign = "+" if ch["pct"] >= 0 else "−"
            parts.append(f'<text class="tick" x="{w - 4}" y="{yy + 4:.1f}" text-anchor="end">{sign}{abs(ch["pct"]):.0f}%</text>')
        parts.append("</g>")
        out["tips"].append(tip)
        out["table"].append(
            {
                "code": code,
                "label": label,
                "start": amount(m, a) if start else "",
                "end": amount(m, b),
                "change": ch["text"] if ch else "",
                "href": t.dashboard(m.link, pa=code) if code else t.dashboard(m.link, by="pa"),
            }
        )
    parts.append("</svg>")
    out["svg"] = "".join(parts)
    return out


def _per_dollar(t: Trends, tsb: dict[int, float], spent: dict[int, float]) -> dict:
    ys = [y for y in t.years if y in tsb and spent.get(y)]
    out = {"name": "per-dollar", "title": "Benefit per $1 spent", "tips": [], "table": []}
    if not ys:
        out["empty"] = "Total system benefit and spending are not published together for any year yet."
        return out
    vals = {y: tsb[y] / spent[y] for y in ys}
    w, h, pad_b, pad_t = 420, 230, 26, 24
    bw = w / len(ys)
    low, high = min(0, *vals.values()), max(0, *vals.values()) * 1.15 or 1

    def sy(v):
        return pad_t + (h - pad_b - pad_t) * (high - v) / ((high - low) or 1)

    parts = [_svg_open("per-dollar", f"Total system benefit per dollar of claimed spending, {span(ys)}, {t.who}", w, h)]
    for i, y in enumerate(ys):
        x = bw * i + bw * 0.2
        tip = f"{y}: ${vals[y]:.2f} of total system benefit per $1 spent"
        parts.append(f'<g class="pt" data-tip="{escape(tip)}">')
        parts.append(f'<rect class="hit" x="{bw * i:.1f}" y="0" width="{bw:.1f}" height="{h}"/>')
        parts.append(_column(x, sy(vals[y]), sy(0), bw * 0.6, "col" + (" chosen" if y == t.year else "")))
        parts.append(f'<text class="val" x="{x + bw * 0.3:.1f}" y="{sy(vals[y]) - 6:.1f}" text-anchor="middle">${vals[y]:.2f}</text>')
        parts.append(f'<text class="tick" x="{x + bw * 0.3:.1f}" y="{h - 8}" text-anchor="middle">{y}</text></g>')
        out["tips"].append(tip)
        out["table"].append({"year": y, "value": f"${vals[y]:.2f}"})
    parts.append(f'<line class="axis" x1="0" x2="{w}" y1="{sy(0):.1f}" y2="{sy(0):.1f}"/></svg>')
    out["svg"] = "".join(parts)
    return out

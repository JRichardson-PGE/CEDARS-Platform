"""The walkthrough's pictures: how a measure's savings, their discounting and their value build up over its life (steps 3 to 6).

Each chart is drawn on the server as an SVG (the page works without scripts) with its numbers in a table beside it, and is shown only
when its numbers add up to the engine's own output for the measure:

- Step 3, savings year by year: each year of life's net savings (the first baseline for the remaining useful life, the second after it,
  a part year at the end), stacked on the years before; the last column is the lifetime savings (LifecycleNet<fuel>).
- Step 4, when in the year: the load shape's share of the year's savings by month and by hour of the day, read from the run's
  impact-profile library pack (ReCET runs only: a CET replica prices on its release's quarterly tables).
- Step 5, discounting year by year: the running total of the savings as they come and as counted today, which ends at the discounted
  savings (DiscountedSavingsNet<fuel>), year k counted at the annual rate (each year's discount factor, A(k + 1) - A(k)) and brought
  back from the claim quarter to the First Year.
- Step 6, value building up: each year's electric benefit in present value, stacked on the years before, ending at ElecBen; and what a
  kWh saved on the measure's load shape is worth in each year. The engine's own pricing function (ReCET
  engine.price_real_hourly_per_measure, imported unchanged) prices the measure quarter by quarter on its load shape from the run's
  impact-profile pack and its utility and zone's hourly avoided costs from the run's ACC pack; nothing of the engine's arithmetic is
  copied here. The quarters are kept only when they add up to the engine's ElecBen, so a measure the pricing here cannot rebuild (a
  supplied profile, a second-baseline profile, SCG's routing, a blended zone) shows the step without the picture.

The pack-based numbers are kept beside the run's outputs (walkthrough/<ACC>/<measure>.json in the run's storage) once worked out.
"""

from __future__ import annotations

import calendar
import datetime
import hashlib
import json
import math
from dataclasses import dataclass, field

from cedars.core import storage

TOLERANCE = 1e-6  # relative: a chart is drawn only when its numbers add up to the engine's output this closely
REPLICA_TOLERANCE = 1e-5  # CET rounds each discount factor to six decimal places
CACHE_VERSION = 1
MONTHS = [calendar.month_abbr[m] for m in range(1, 13)]

# --- the numbers


def _close(value: float, output: float | None, tol: float) -> bool:
    return output is not None and abs(value - output) <= tol * max(1.0, abs(output))


def _fuel(w) -> tuple[str, str, str, str, str] | None:
    """(unit, 1st-baseline column, 2nd-baseline column, lifecycle output, discounted output) of the measure's main fuel: electricity
    when it saves any, else gas."""
    if w.z("LifecycleNetKWhSite"):
        return "kWh", "AC_Net_kWh_1st_Baseline", "AC_Net_kWh_2nd_Baseline", "LifecycleNetKWhSite", "DiscountedSavingsNetkWh"
    if w.z("LifecycleNetThm"):
        return "therms", "AC_Net_Therms_1st_Baseline", "AC_Net_Therms_2nd_Baseline", "LifecycleNetThm", "DiscountedSavingsNetThm"
    return None


def _quarter_label(w, k: int) -> str:
    """The quarter year k of the life starts in (k = 0: the claim quarter)."""
    if not w.claim:
        return ""
    y, q = w.claim
    n = (q - 1) + 4 * k
    return f"Q{n % 4 + 1} {y + n // 4}"


def _life(w) -> tuple[float, float, float, float] | None:
    """(years of life, years on the first baseline, a year's savings on the first baseline, on the second) of the main fuel, as the
    lifetime savings count them (step 3)."""
    fuel = _fuel(w)
    if fuel is None:
        return None
    unit, first, second, _out, _disc = fuel
    electric = unit == "kWh"
    meb = w._market_factor(electric)
    a1, a2 = w._annual_net(first, electric, "1st") * meb, w._annual_net(second, electric, "2nd") * meb
    eul, raw_rul = w.z("EUL_Yrs"), w.z("RUL_Yrs")
    if w.engine.replica and raw_rul > eul > 0:
        return raw_rul, raw_rul, a1, a2  # CET counts the first baseline for the whole RUL when it is longer than the EUL
    return eul, (w.rul if w.dual else eul), a1, a2


def life_years(w) -> list[dict] | None:
    """Each year of the life: {year (1 on), starts, share (of a whole year), first (years on the first baseline), saved}; None when
    they do not add up to the engine's lifetime savings."""
    fuel, life = _fuel(w), _life(w)
    if fuel is None or life is None or not w.claim:
        return None
    n, on_first, a1, a2 = life
    if n <= 0:
        return None
    rows = []
    for k in range(math.ceil(n - 1e-9)):
        lo, hi = k, min(k + 1.0, n)
        first = max(0.0, min(hi, on_first) - lo)
        second = (hi - lo) - first
        rows.append({"year": k + 1, "starts": _quarter_label(w, k), "share": hi - lo, "first": first, "saved": a1 * first + a2 * second})
    total = sum(r["saved"] for r in rows)
    return rows if _close(total, w.g(fuel[3]), REPLICA_TOLERANCE if w.engine.replica else TOLERANCE) else None


def _a(x: float, r: float) -> float:
    """The sum of the annual discount factors over the first x years of life, the first year undiscounted (ReCET discounted_savings'
    A(n) = 1 + (1 - (1 + r)^-(n - 1)) / r; A(0) = 0)."""
    if x <= 0:
        return 0.0
    return 1 + (1 - (1 + r) ** -(x - 1)) / r if r else x


def discount_years(w) -> list[dict] | None:
    """Each year of the life with its savings as they come and as counted today: {year, starts, saved, factor, counted}; None when the
    counted savings do not add up to the engine's discounted savings (or there are no rates or no First Year)."""
    fuel, life, years = _fuel(w), _life(w), life_years(w)
    if fuel is None or life is None or years is None or not w.rates or not w.first_year or not w.claim:
        return None
    n, on_first, a1, a2 = life
    r = w.rates["DiscountRateAnnual"]
    y0, q0 = w.claim
    qm = (y0 - w.first_year) * 4 + (q0 - 1)
    if w.engine.release == "21.0":
        qm += 1  # CET 21.0 counts the claim quarter from 1
    pvf = (1 + w.rates["DiscountRateQtr"]) ** qm
    rows = []
    for row in years:
        k = row["year"] - 1
        hi = min(k + 1.0, n)
        on1 = min(hi, on_first)
        counted = a1 * max(0.0, _a(on1, r) - _a(k, r)) if on1 > k else 0.0
        if hi > on_first:
            counted += a2 * (_a(hi, r) - _a(max(k, on_first), r))
        counted /= pvf
        rows.append(
            {
                "year": row["year"],
                "starts": row["starts"],
                "saved": row["saved"],
                "factor": counted / row["saved"] if row["saved"] else None,
                "counted": counted,
            }
        )
    total = sum(r["counted"] for r in rows)
    return rows if _close(total, w.g(fuel[4]), REPLICA_TOLERANCE if w.engine.replica else TOLERANCE) else None


# --- the numbers read from the run's packs (steps 4 and 6)


def _zone(v: str) -> str:
    v = str(v or "").strip().upper().removeprefix("CZ")
    return v.lstrip("0") or v


def _pinned(run, kind: str, acc: str):
    """The run's pack of `kind` (for the ACC: the pack of the part's ACC year): the one its job spec pinned, else the newest one not
    withdrawn (a run priced before packs were registered); the totals check decides whether it matches what the run read."""
    from cedars.services.refdata.models import PackVersion

    pins = ((run.spec or {}).get("context") or {}).get("packs") or {}
    if kind == PackVersion.Kind.ACC_ELECTRIC:
        pin = pins.get(kind) if acc == run.acc_version else pins.get(f"{kind}:{acc}")
    else:
        pin = pins.get(kind)
    qs = PackVersion.objects.filter(kind=kind)
    if pin and pin.get("id"):
        found = qs.filter(pk=pin["id"]).first()
        if found is not None:
            return found
    if kind == PackVersion.Kind.ACC_ELECTRIC:
        if not str(acc).isdigit():
            return None
        qs = qs.filter(acc_year=int(acc))
    qs = qs.exclude(status=PackVersion.Status.WITHDRAWN)
    return qs.filter(status=PackVersion.Status.PUBLISHED).order_by("-published_at").first() or qs.order_by("-registered_at").first()


def _cache_key(run, acc: str, measure: str) -> str:
    digest = hashlib.sha256(str(measure).encode()).hexdigest()[:24]
    return f"{run.storage_prefix}walkthrough/{acc or run.acc_version}/{digest}.json"


def pack_series(run, acc: str, row: dict, settings: dict, engine) -> dict:
    """The numbers steps 4 and 6 draw for one measure, read from the run's packs once and kept beside the run's outputs:
    {"shape": {"months": [...], "hours": [...], "zone", "profile", "calendar"} or None, "priced": {...} or None, "why": "..."}."""
    if engine.replica:
        return {"shape": None, "priced": None, "why": "replica"}
    measure = str(row.get("CEInputID") or "")
    store = storage.store("results")
    key = _cache_key(run, acc, measure)
    try:
        kept = json.loads(store.get_bytes(key))
        if kept.get("v") == CACHE_VERSION:
            return kept
    except Exception:  # noqa: BLE001 - not kept yet (or unreadable): work it out again
        pass
    try:
        out = _from_packs(run, acc or run.acc_version, row, settings)
    except Exception as e:  # noqa: BLE001 - a pack that cannot be read leaves the step without its picture, never the page broken
        out = {"shape": None, "priced": None, "why": f"unreadable: {type(e).__name__}"}
    out["v"] = CACHE_VERSION
    try:
        store.put_bytes(key, json.dumps(out).encode())
    except Exception:  # noqa: BLE001 - kept only to save the next view the reading
        pass
    return out


def _shape_summary(shape, represents_year: int) -> dict:
    """A load shape's share of the year's savings by month and by hour of the day, on the calendar it is drawn on."""
    import numpy as np

    total = float(np.sum(shape)) or 1.0
    start = datetime.datetime(represents_year, 1, 1)
    months = [0.0] * 12
    hours = [0.0] * 24
    days = np.asarray(shape, dtype=float).reshape(-1, 24)
    for d, day in enumerate(days):
        months[(start + datetime.timedelta(days=d)).month - 1] += float(day.sum())
    for h in range(24):
        hours[h] = float(days[:, h].sum())
    return {"months": [m / total for m in months], "hours": [h / total for h in hours]}


def _from_packs(run, acc: str, row: dict, settings: dict) -> dict:
    from cedars.services.refdata import profiles as packed_profiles
    from cedars.services.refdata.models import PackVersion

    from .walkthrough import text

    out = {"shape": None, "priced": None, "why": ""}
    name, territory = text(row.get("AC_Elec_Impact_Profile")), text(row.get("IOU_AC_Territory"))
    sector = text(row.get("AC_Elec_Target_Sector"))
    zone = _zone(row.get("AC_Climate_Zone"))
    if zone == "SYSTEM":
        zone = _zone(row.get("SystemClimateZone"))
        if not zone or zone in ("SYSTEM", "NONE"):
            out["why"] = "system zone"
            return out
    library = _pinned(run, PackVersion.Kind.IMPACT_PROFILES, acc)
    if not name or library is None:
        out["why"] = "no library pack" if name else "no profile"
        return out
    source = text(row.get("ProfileSource"))
    if source.startswith("user"):
        out["why"] = "supplied profile"
        return out
    entries = [e for e in packed_profiles.catalog(library) if e.shape_id is not None]
    entry = packed_profiles.find(entries, name, territory, sector)
    if entry is None:
        out["why"] = "profile not in pack"
        return out
    zones = packed_profiles.shapes(library, [entry.shape_id]).get(entry.shape_id, {})
    values = zones.get(zone) or zones.get("System")
    if not values:
        out["why"] = "zone not in profile"
        return out
    represents = entry.represents_year or packed_profiles.DEFAULT_REPRESENTS_YEAR
    shown_zone = zone if zone in zones else "System"
    out["shape"] = {"profile": name, "zone": shown_zone, "calendar": represents, **_shape_summary(values, represents)}

    # step 6: the electric benefit quarter by quarter, by the engine's own per-measure pricing
    out["priced"], out["why"] = _priced(run, acc, row, settings, values, represents, zone)
    return out


def _priced(run, acc, row, settings, values, represents, zone) -> tuple[dict | None, str]:
    import numpy as np
    import pandas as pd
    from recet.pipeline import engine as recet_engine
    from recet.pipeline import real_impact_profiles
    from recet.pipeline.real_avoided_cost_v2 import MISSING_ZONE_SUBSTITUTE, PA_TO_IOU_LABEL

    from cedars.services.refdata import acc as packed_acc
    from cedars.services.refdata.models import PackVersion

    from .walkthrough import discount_rates, num, text

    if text(row.get("AC_Elec_Impact_Profile_2nd_Baseline")) or str(row.get("Scaled_Profile_Flag")).lower() in ("true", "1"):
        return None, "second-baseline or kWh-per-unit profile"
    territory = text(row.get("IOU_AC_Territory")).upper().replace("&", "")
    utility = PA_TO_IOU_LABEL.get(territory)
    if utility is None or territory == "SCG":
        return None, "territory"
    utility, cz = MISSING_ZONE_SUBSTITUTE.get((utility, f"CZ{zone}"), (utility, f"CZ{zone}"))
    pack = _pinned(run, PackVersion.Kind.ACC_ELECTRIC, acc)
    if pack is None:
        return None, "no ACC pack"
    series = packed_acc.hourly(pack, utility, cz, "TRC")
    if series is None:
        return None, "zone not in ACC"
    rates = discount_rates(row, settings)
    fy = num((settings.get("inputs") or {}).get("first_year"))
    year, quarter = num(row.get("Used_Year")), num(row.get("Used_Quarter"))
    if not rates or fy is None or year is None or quarter is None:
        return None, "no rates"
    ntg = num(row.get("NTGRkWh")) or 0.0
    meb = num((settings.get("config") or {}).get("market_effects")) or 0.0
    factor = (ntg + meb) / ntg if meb and ntg > 0 else 1.0
    measure = pd.DataFrame(
        [
            {
                "CEInputID": str(row.get("CEInputID") or ""),
                "Used_Year": int(year),
                "Used_Quarter": int(quarter),
                "EUL_Yrs": num(row.get("EUL_Yrs")) or 0.0,
                "RUL_Yrs": num(row.get("RUL_Yrs")) or 0.0,
                "Priced_Quantity_1st_Baseline": (num(row.get("AC_Net_kWh_1st_Baseline")) or 0.0) * factor,
                "Priced_Quantity_2nd_Baseline": (num(row.get("AC_Net_kWh_2nd_Baseline")) or 0.0) * factor,
            }
        ]
    )
    shape = np.asarray(values, dtype=float)
    result = recet_engine.price_real_hourly_per_measure(
        measure,
        lambda m: (shape, represents),
        lambda m: series.years,
        lambda s, y, rep: real_impact_profiles.shift_to_acc(s, rep, series.calendar_year),
        lambda m: rates["DiscountRateQtr"],
        int(fy),
    )
    q = result.values
    total = float(q["QuarterValuePV"].sum()) if len(q) else 0.0
    # Added load (a negative total): a fuel-substitution measure's is counted as a supply cost (ElecSupplyCost), any other's as a
    # negative electric benefit.
    cost = total < 0 and not _close(total, num(row.get("ElecBen")), TOLERANCE)
    if not total or not _close(-total if cost else total, num(row.get("ElecSupplyCost" if cost else "ElecBen")), TOLERANCE):
        return None, "does not add up"
    rq = 1 + rates["DiscountRateQtr"]
    quarters = []
    for y, qu, pv in zip(q["Year"], q["Quarter"], q["QuarterValuePV"], strict=True):
        undiscounted = float(pv) * rq ** ((int(y) - int(fy)) * 4 + int(qu) - 1)
        quarters.append({"year": int(y), "quarter": int(qu), "pv": float(pv), "value": undiscounted})
    aligned = real_impact_profiles.shift_to_acc(shape, represents, series.calendar_year)
    weight = float(aligned.sum()) or 1.0
    acc_years = sorted(series.years)
    first_year, last_year = quarters[0]["year"], quarters[-1]["year"]
    per_kwh = []
    for y in range(first_year, last_year + 1):
        nearest = min(acc_years, key=lambda a: abs(a - y))
        per_kwh.append({"year": y, "acc_year": nearest, "value": float((aligned * series.years[nearest]).sum()) / weight})
    return {"quarters": quarters, "per_kwh": per_kwh, "acc_last_year": acc_years[-1], "total": total, "cost": cost}, ""


def value_years(w, priced: dict | None) -> list[dict] | None:
    """Each year of the life's electric benefit, in present value and before discounting: {year, starts, pv, value}."""
    if not priced or not w.claim:
        return None
    y0, q0 = w.claim
    rows: dict[int, dict] = {}
    for q in priced["quarters"]:
        k = ((q["year"] - y0) * 4 + (q["quarter"] - q0)) // 4
        r = rows.setdefault(k, {"year": k + 1, "starts": _quarter_label(w, k), "pv": 0.0, "value": 0.0})
        r["pv"] += q["pv"]
        r["value"] += q["value"]
    return [rows[k] for k in sorted(rows)]


# --- the pictures

WIDTH, HEIGHT = 640, 240
LEFT, RIGHT, TOP, BOTTOM = 72, 20, 14, 40


@dataclass
class Chart:
    key: str
    title: str
    summary: str  # the picture in a sentence (its accessible name)
    legend: list[tuple[str, str]]  # (series class, label); empty for one series
    y_unit: str
    width: int = WIDTH
    height: int = HEIGHT
    yticks: list[tuple[float, str]] = field(default_factory=list)
    xlabels: list[tuple[float, str]] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)  # {"d": path, "cls": series}
    lines: list[dict] = field(default_factory=list)  # {"d": path, "cls"}
    dots: list[dict] = field(default_factory=list)  # {"x", "y", "cls", "label", "anchor"}
    table: dict = field(default_factory=dict)  # {"caption", "columns", "rows"}
    plot: tuple[float, float, float, float] = (LEFT, TOP, WIDTH - RIGHT, HEIGHT - BOTTOM)
    note: str = ""
    # One per column or point along the x axis: its band ({"x", "w"}), a title ("Year 3 (Q1 2030)") and its values as rows
    # [{"cls": series or "", "text"}], plus "text" (all of it in one line) and, on a line chart, the points to mark ({"x", "y", "cls"}).
    # The page shows them as a tooltip for the year under the pointer or picked with the arrow keys.
    slots: list[dict] = field(default_factory=list)
    slot_word: str = "year"  # what one slot is, for the page's hint ("year", "month", "hour")

    @property
    def plot_height(self) -> float:
        return self.plot[3] - self.plot[1]


def _nice(hi: float) -> tuple[float, list[float]]:
    """A clean axis top and its ticks (0 and four or five round steps) for values up to `hi`."""
    if hi <= 0:
        return 1.0, [0.0, 0.5, 1.0]
    raw = hi / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    top = step * math.ceil(hi / step - 1e-9)
    return top, [i * step for i in range(int(round(top / step)) + 1)]


def _tick(v: float, unit: str) -> str:
    if unit == "share":
        return f"{v * 100:.0f}%" if v * 100 == int(v * 100) else f"{v * 100:.1f}%"
    money = unit.startswith("$")
    a = abs(v)
    if unit == "$/kWh":
        return f"${v:,.2f}" if a < 10 else f"${v:,.0f}"
    if a >= 1e9:
        s = f"{v / 1e9:,.3g}B"
    elif a >= 1e6:
        s = f"{v / 1e6:,.3g}M"
    elif a >= 1e4:
        s = f"{v / 1e3:,.3g}K"
    else:
        s = f"{v:,.0f}" if a >= 10 or v == int(v) else f"{v:,.2g}"
    return ("$" + s) if money else s


def _frame(chart: Chart, n: int, top: float, ticks: list[float], labels: list[str]) -> tuple:
    x0, y0, x1, y1 = chart.plot
    band = (x1 - x0) / max(n, 1)

    def y(v: float) -> float:
        return y1 - (v / top) * (y1 - y0) if top else y1

    chart.yticks = [(round(y(t), 2), _tick(t, chart.y_unit)) for t in ticks]
    every = max(1, math.ceil(n / 12))
    chart.xlabels = [(round(x0 + band * (i + 0.5), 2), labels[i]) for i in range(n) if i % every == 0 or i == n - 1]
    if len(chart.xlabels) > 1 and chart.xlabels[-1][0] - chart.xlabels[-2][0] < band * every * 0.6:
        chart.xlabels.pop(-2)  # the last label wins when it would crowd the one before
    return band, y


def tip(title: str, *rows: tuple[str, str]) -> dict:
    """A slot's words: its title and its rows, each (series class or "", text)."""
    return {"title": title, "rows": [{"cls": c, "text": t} for c, t in rows]}


def _slots(chart: Chart, band: float, tips: list[dict], points: list[list[dict]] | None = None) -> None:
    x0 = chart.plot[0]
    chart.slots = [
        {
            **t,
            "x": round(x0 + band * i, 2),
            "w": round(band, 2),
            "cx": round(x0 + band * (i + 0.5), 2),
            "text": t["title"] + ": " + "; ".join(r["text"] for r in t["rows"]) + ".",
            "points": (points[i] if points else []),
        }
        for i, t in enumerate(tips)
    ]


def _column(x: float, w: float, y_top: float, y_base: float, rounded: bool) -> str:
    """A column from y_base up to y_top: 4 px rounded at its data end, square at the baseline."""
    h = y_base - y_top
    if h <= 0:
        return ""
    r = min(4.0, w / 2, h) if rounded else 0
    if not r:
        return f"M{x:.2f},{y_base:.2f}V{y_top:.2f}H{x + w:.2f}V{y_base:.2f}Z"
    return (
        f"M{x:.2f},{y_base:.2f}V{y_top + r:.2f}Q{x:.2f},{y_top:.2f} {x + r:.2f},{y_top:.2f}"
        f"H{x + w - r:.2f}Q{x + w:.2f},{y_top:.2f} {x + w:.2f},{y_top + r:.2f}V{y_base:.2f}Z"
    )


def stacked_columns(key, title, summary, unit, labels, below, this, tips, legend, table) -> Chart:
    """Columns that pile each year on the years before: `below` (the running total before the year) under `this` (the year's own)."""
    chart = Chart(key, title, summary, legend, unit, table=table)
    top, ticks = _nice(max((b + t for b, t in zip(below, this, strict=True)), default=0))
    band, y = _frame(chart, len(labels), top, ticks, labels)
    w = min(24.0, band * 0.7)
    base = chart.plot[3]
    for i, (b, t) in enumerate(zip(below, this, strict=True)):
        x = chart.plot[0] + band * i + (band - w) / 2
        yb, yt = y(b), y(b + t)
        if b > 0:
            d = _column(x, w, yb, base, rounded=t <= 0)
            if d:
                chart.marks.append({"d": d, "cls": "s1"})
        if t > 0:
            gap = 2.0 if b > 0 and yb - yt > 3 else 0.0
            d = _column(x, w, yt, yb - gap, rounded=True)
            if d:
                chart.marks.append({"d": d, "cls": "s2"})
    _slots(chart, band, tips)
    return chart


def columns(key, title, summary, unit, labels, values, tips, table) -> Chart:
    chart = Chart(key, title, summary, [], unit, table=table)
    top, ticks = _nice(max(values, default=0))
    band, y = _frame(chart, len(labels), top, ticks, labels)
    w = min(24.0, band * 0.7)
    for i, v in enumerate(values):
        x = chart.plot[0] + band * i + (band - w) / 2
        d = _column(x, w, y(v), chart.plot[3], rounded=True)
        if d:
            chart.marks.append({"d": d, "cls": "s1"})
    _slots(chart, band, tips)
    return chart


def lines(key, title, summary, unit, labels, series, legend, table, end_labels, tips) -> Chart:
    """One line per series ([(class, values)]), each with a dot and its value at its end."""
    chart = Chart(key, title, summary, legend, unit, table=table)
    top, ticks = _nice(max((v for _, vals in series for v in vals), default=0))
    band, y = _frame(chart, len(labels), top, ticks, labels)
    ends, marked = [], [[] for _ in labels]
    for (cls, vals), text in zip(series, end_labels, strict=True):
        pts = [(chart.plot[0] + band * (i + 0.5), y(v)) for i, v in enumerate(vals)]
        for i, (px, py) in enumerate(pts):
            marked[i].append({"x": round(px, 2), "y": round(py, 2), "cls": cls})
        if not pts:
            continue
        chart.lines.append({"d": "M" + " L".join(f"{px:.2f},{py:.2f}" for px, py in pts), "cls": cls})
        ends.append({"x": round(pts[-1][0], 2), "y": round(pts[-1][1], 2), "cls": cls, "label": text})
    ends.sort(key=lambda e: e["y"])
    for i in range(1, len(ends)):  # two end labels never sit on each other
        if ends[i]["y"] - ends[i - 1]["y"] < 14:
            ends[i]["label_y"] = ends[i - 1].get("label_y", ends[i - 1]["y"]) + 14
    for e in ends:
        e.setdefault("label_y", e["y"])
        e["label_y"] = round(e["label_y"] - 8, 2)
    chart.dots = ends
    _slots(chart, band, tips, marked)
    return chart


# --- the charts each step shows


def _life_table(w, rows, unit, dual):
    from .walkthrough import quantity

    cols = ["Year of life", "Starts", f"Saved that year ({unit})", f"Running total ({unit})"]
    if dual:
        cols.insert(2, "Baseline")
    out, running = [], 0.0
    for r in rows:
        running += r["saved"]
        cells = [str(r["year"]) + ("" if r["share"] >= 1 - 1e-9 else f" (part: {r['share']:.2f} of a year)"), r["starts"]]
        if dual:
            cells.append("first" if r["first"] >= r["share"] - 1e-9 else ("second" if r["first"] <= 1e-9 else "both"))
        cells += [quantity(r["saved"]), quantity(running)]
        out.append(cells)
    return cols, out


def life_chart(w) -> Chart | None:
    from .walkthrough import quantity

    rows, fuel = life_years(w), _fuel(w)
    sign = _sign([r["saved"] for r in rows or []])
    if not rows or fuel is None or not sign:
        return None
    unit = fuel[0]
    what = "electricity" if unit == "kWh" else "gas"
    word = "saved" if sign > 0 else "added"
    running, below, this, tips = 0.0, [], [], []
    for r in rows:
        below.append(running)
        this.append(r["saved"] * sign)
        running += r["saved"] * sign
        part = "" if r["share"] >= 1 - 1e-9 else f", part of a year ({r['share']:.2f})"
        tips.append(
            tip(
                f"Year {r['year']} (starts {r['starts']}{part})",
                ("s2", f"{word.capitalize()} that year: {quantity(r['saved'] * sign, unit)}"),
                ("s1", f"{word.capitalize()} in the years before: {quantity(below[-1], unit)}"),
                ("", f"Running total: {quantity(running, unit)}"),
            )
        )
    cols, table = _life_table(w, rows, unit, w.dual)
    title = f"Net {what} savings piling up, year by year" if sign > 0 else f"Added {what} use piling up, year by year (a negative saving)"
    return stacked_columns(
        "life",
        title,
        f"Column chart over the {len(rows)} years of the measure's life. Each column stacks that year's {what} {word} on the years "
        f"before, so the last column is the lifetime total, {quantity(running, unit)}.",
        unit,
        [str(r["year"]) for r in rows],
        below,
        this,
        tips,
        [("s1", f"{word.capitalize()} in the years before"), ("s2", f"{word.capitalize()} that year")],
        {"caption": f"Each year's net {what} savings and the running total", "columns": cols, "rows": table},
    )


def _sign(values: list[float]) -> int:
    """1 when every value is zero or more (and one is above zero), -1 when every one is zero or less: a chart draws one sign only."""
    if values and all(v >= 0 for v in values) and any(v > 0 for v in values):
        return 1
    if values and all(v <= 0 for v in values) and any(v < 0 for v in values):
        return -1
    return 0


def discount_chart(w) -> Chart | None:
    from .walkthrough import quantity

    rows, fuel = discount_years(w), _fuel(w)
    sign = _sign([r["saved"] for r in rows or []])
    if not rows or fuel is None or not sign:
        return None
    unit = fuel[0]
    saved, counted, s, c, table, tips = [], [], 0.0, 0.0, [], []
    for r in rows:
        s += r["saved"] * sign
        c += r["counted"] * sign
        saved.append(s)
        counted.append(c)
        tips.append(
            tip(
                f"Year {r['year']} (starts {r['starts']})",
                ("s1", f"Running total, as the savings come: {quantity(s, unit)}"),
                ("s2", f"Running total, counted in today's terms: {quantity(c, unit)}"),
                ("", f"That year: {quantity(r['saved'] * sign, unit)} saved, counted as {quantity(r['counted'] * sign, unit)}")
                if r["factor"] is None
                else (
                    "",
                    f"That year: {quantity(r['saved'] * sign, unit)} × {r['factor']:.4f} = {quantity(r['counted'] * sign, unit)} today",
                ),
            )
        )
        table.append(
            [
                str(r["year"]),
                r["starts"],
                quantity(r["saved"]),
                "" if r["factor"] is None else f"{r['factor']:.4f}",
                quantity(r["counted"]),
                quantity(c),
            ]
        )
    chart = lines(
        "discounting",
        "The same savings, as they come and counted in today's terms",
        f"Line chart over the {len(rows)} years of the measure's life. The running total of the savings as they come reaches "
        f"{quantity(s, unit)}; counted in today's terms, each year discounted, it reaches {quantity(c, unit)}.",
        unit,
        [str(r["year"]) for r in rows],
        [("s1", saved), ("s2", counted)],
        [("s1", "Running total, as the savings come"), ("s2", "Running total, counted in today's terms")],
        {
            "caption": f"Each year's savings, its discount factor and what it counts for today ({unit})",
            "columns": ["Year of life", "Starts", "Saved that year", "Discount factor", "Counted today", "Running total counted today"],
            "rows": table,
        },
        [quantity(s, unit), quantity(c, unit)],
        tips,
    )
    chart.note = "The gap between the two lines is what discounting takes off: it grows with every year that passes."
    return chart


def shape_charts(data: dict | None) -> list[Chart]:
    shape = (data or {}).get("shape")
    if not shape:
        return []
    months, hours = shape["months"], shape["hours"]
    pct = lambda v: f"{v * 100:.1f}%"  # noqa: E731
    peak_m = max(range(12), key=lambda i: months[i])
    peak_h = max(range(24), key=lambda i: hours[i])
    hour_name = lambda h: f"{h}:00 to {h + 1}:00"  # noqa: E731
    by_month = columns(
        "shape-months",
        "Share of the year's savings in each month",
        f"Column chart of the 12 months. The most savings fall in {calendar.month_name[peak_m + 1]}, {pct(months[peak_m])} of the year's.",
        "share",
        MONTHS,
        months,
        [tip(calendar.month_name[i + 1], ("s1", f"{pct(v)} of the year's savings")) for i, v in enumerate(months)],
        {
            "caption": "Share of the year's savings by month",
            "columns": ["Month", "Share"],
            "rows": [[calendar.month_name[i + 1], pct(v)] for i, v in enumerate(months)],
        },
    )
    by_hour = columns(
        "shape-hours",
        "Share of the year's savings in each hour of the day",
        f"Column chart of the 24 hours of the day, every day of the year added together. The most savings fall from {hour_name(peak_h)}, "
        f"{pct(hours[peak_h])} of the year's.",
        "share",
        [str(h) for h in range(24)],
        hours,
        [tip(f"Hour {h}, {hour_name(h)}", ("s1", f"{pct(v)} of the year's savings")) for h, v in enumerate(hours)],
        {
            "caption": "Share of the year's savings by hour of the day",
            "columns": ["Hour", "Share"],
            "rows": [[hour_name(h), pct(v)] for h, v in enumerate(hours)],
        },
    )
    by_month.slot_word, by_hour.slot_word = "month", "hour"
    by_hour.note = "Hour 0 is midnight to 1 am, hour 17 is 5 pm to 6 pm (standard time, as the profile's 8,760 hours run)."
    return [by_month, by_hour]


def value_charts(w, data: dict | None) -> list[Chart]:
    from .walkthrough import dollars

    priced = (data or {}).get("priced")
    rows = value_years(w, priced)
    sign = -1 if priced and priced.get("total", 0) < 0 else 1
    if not rows or _sign([r["pv"] * sign for r in rows]) != 1:
        return []
    if sign > 0:
        what = "the electric benefit"
    elif priced.get("cost"):
        what = "the cost of supplying the added electricity"
    else:
        what = "the value of the added electricity use (a negative electric benefit)"
    running, below, this, tips, table = 0.0, [], [], [], []
    for r in rows:
        pv, value = r["pv"] * sign, r["value"] * sign
        below.append(running)
        this.append(pv)
        running += pv
        tips.append(
            tip(
                f"Year {r['year']} (starts {r['starts']})",
                ("s2", f"Value that year: {dollars(pv)} in today's dollars ({dollars(value)} as paid)"),
                ("s1", f"Value in the years before: {dollars(below[-1])}"),
                ("", f"Running total: {dollars(running)}"),
            )
        )
        table.append([str(r["year"]), r["starts"], dollars(value), dollars(pv), dollars(running)])
    built = stacked_columns(
        "value",
        f"{what[0].upper()}{what[1:]} building up, year by year (in today's dollars)",
        f"Column chart over the {len(rows)} years of the measure's life. Each column stacks that year's part of {what}, in present "
        f"value, on the years before, so the last column is {what}, {dollars(running)}.",
        "$",
        [str(r["year"]) for r in rows],
        below,
        this,
        tips,
        [("s1", "Value in the years before"), ("s2", "Value that year")],
        {
            "caption": f"Each year's part of {what}, before discounting and in today's dollars, and the running total",
            "columns": [
                "Year of life",
                "Starts",
                "Value that year, as paid",
                "Value that year, today's dollars",
                "Running total, today's dollars",
            ],
            "rows": table,
        },
    )
    built.note = (
        "Priced here quarter by quarter on the measure's load shape and its utility and climate zone's hourly avoided costs, as the "
        "engine does; the years add up to the engine's " + ("supply cost (ElecSupplyCost)." if sign < 0 else "electric benefit (ElecBen).")
    )
    out = [built]
    per = priced.get("per_kwh") or []
    if per:
        last = priced.get("acc_last_year")
        worth = lines(
            "per-kwh",
            "What a kWh saved in each year is worth on this measure's load shape (before discounting)",
            f"Line chart of the calendar years {per[0]['year']} to {per[-1]['year']}: the avoided cost of a kWh saved at this load "
            f"shape's hours, from ${per[0]['value']:,.4f} in {per[0]['year']} to ${per[-1]['value']:,.4f} in {per[-1]['year']}.",
            "$/kWh",
            [str(p["year"]) for p in per],
            [("s1", [p["value"] for p in per])],
            [],
            {
                "caption": "Avoided cost of a kWh saved on this load shape, by year",
                "columns": ["Year", "Value of a kWh", "ACC year used"],
                "rows": [[str(p["year"]), f"${p['value']:,.4f}", str(p["acc_year"])] for p in per],
            },
            [f"${per[-1]['value']:,.4f}"],
            [
                tip(
                    str(p["year"]),
                    ("s1", f"A kWh saved is worth ${p['value']:,.4f}"),
                    (
                        "",
                        f"Priced on the {p['acc_year']} prices of the ACC"
                        if p["acc_year"] != p["year"]
                        else "Priced on that year's ACC prices",
                    ),
                )
                for p in per
            ],
        )
        if last and per[-1]["year"] > last:
            worth.note = f"The ACC's prices run to {last}; a later year is priced at {last}'s, as the engine does."
        out.append(worth)
    return out

"""The front page's program-cycle summary (Jake, 2026-10-31; compact cards 2026-11-03): seven small cards for one program cycle, from
the platform's published data: spending, total system benefit and energy saved, each with a gauge of its progress, then demand and gas
savings and the statewide TRC and PAC ratios.

Every number is the one a dashboard page shows for the same years and its default exclusions, so each card links to the page that breaks
it down, and the two agree. The gauges' denominators:
  spending           the budget page (budget_view): claimed expenditure against the operating budget (the standing filings plus fund
                     shifts), or against the latest filings' forecast when no operating budget is recorded
  total system benefit  the goals page's exclusions (codes and standards, ESA, pension and benefits out): what counts toward the IOU
                     territories' cycle goals (goal_view, the goal set in force) against those goals; with no goal set loaded for the
                     cycle, the claimed TSB against the latest filings' forecast TSB, with the same exclusions
  energy saved       the energy savings page: net first-year MWh claimed against the latest filings' forecast (the CPUC's program
                     goals are TSB goals; its energy goals are for codes and standards only, which the card leaves out)
Demand (MW) and gas (MMTherms) savings show the forecast beside the claimed figure; the cost-effectiveness page's TRC and PAC ratios
show the forecast ratio beside the claimed one.

The goal comes from the confirmed goal set in force (Administration > Goals). What the cards leave out, and where a denominator came
from, is said once under the cards (`Summary.footnotes`), so the cards themselves stay small."""

from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.core.cache import cache

from cedars.services.registry import goals as goal_sets
from cedars.services.registry.models import FilingCycle
from cedars.services.snapshots import service as snapshots

from . import dashboard
from . import query as dq

NONE = "None yet"  # a figure with nothing published behind it, in words
CACHE_SECONDS = 600  # the numbers change only with a new release, a goal set or a budget; the key carries the first two
CACHE_VERSION = 2  # the cached Summary's shape: bump when Card or Summary changes, so a deploy never reads an old pickle


@dataclass
class Card:
    key: str  # spending, tsb, energy, demand, gas, trc, pac: the tests and the page's data-card attributes name a card by it
    title: str
    # the card's numbers, the headline first: {"label", "value" (text), "unit" (beside the value), "detail" (one short line)}
    figures: list[dict] = field(default_factory=list)
    percent: float | None = None  # the gauge: spent of budget, TSB of goal, MWh of forecast (0-100, may exceed 100)
    percent_label: str = ""  # what the percent is of, in words, after it: "of the operating budget"
    link: str = ""
    empty: str = ""  # the card's message when it has no numbers

    @property
    def bar(self) -> float | None:
        """The gauge's filled share, 0 to 100 (past 100 the gauge is full and the words say how far past)."""
        return None if self.percent is None else round(max(0.0, min(self.percent, 100.0)), 2)

    @property
    def percent_text(self) -> str:
        return "" if self.percent is None else f"{self.percent:.0f}%"


@dataclass
class Summary:
    cycle: FilingCycle | None
    cycles: list[FilingCycle]
    cards: list[Card]
    claimed_years: list[int]
    as_of: object = None  # the claims release's time
    notes: list[str] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)  # under the cards: what they leave out and what each gauge measures against

    @property
    def claimed_span(self) -> str:
        return span(self.claimed_years)


def span(years) -> str:
    years = sorted(y for y in years if y is not None)
    if not years:
        return ""
    return str(years[0]) if years[0] == years[-1] else f"{years[0]}-{years[-1]}"


def money(value) -> str:
    """Dollars as the cards print them: in millions ("$412.3M"), or in whole dollars under a million ("$48,250")."""
    if value is None:
        return NONE
    sign = "-" if value < 0 else ""
    if abs(value) < 1_000_000:
        return f"{sign}${abs(value):,.0f}"
    return f"{sign}${abs(value) / 1_000_000:,.1f}M"


def number(value, decimals: int = 0) -> str:
    return NONE if value is None else f"{value:,.{decimals}f}"


def ratio(value) -> str:
    return NONE if value is None else f"{value:.2f}"


def pick_cycle(choice: str = "") -> tuple[FilingCycle | None, list[FilingCycle]]:
    """The cycle asked for ("2024-2027"), else the dashboards' default (the current cycle, or the newest with a forecast)."""
    cycles = list(FilingCycle.objects.order_by("-first_year"))
    first = choice.split("-")[0].strip()
    if first.isdigit():
        for c in cycles:
            if c.first_year == int(first):
                return c, cycles
    return dashboard.default_cycle(), cycles


def _years(cycle: FilingCycle) -> dict[str, list[str]]:
    return {"year_from": [str(cycle.first_year)], "year_to": [str(cycle.last_year)]}


def _link(slug: str, cycle: FilingCycle, **extra) -> str:
    return f"/data/{slug}/?" + urlencode({"year_from": cycle.first_year, "year_to": cycle.last_year, **extra})


def _view(slug: str, cycle: FilingCycle, **params) -> dashboard.View:
    return dashboard.parse(dashboard.PAGES[slug], _years(cycle) | {k: [v] for k, v in params.items()})


def _cache_key(cycle: FilingCycle) -> str:
    releases = [snapshots.current_release(d) for d in ("claims", "forecast", "monthly")]
    numbers = "-".join(str(r.pk) if r else "0" for r in releases)
    sets = "-".join(str(pk) for pk in goal_sets._confirmed().values_list("pk", flat=True)[:5])
    return f"front{CACHE_VERSION}:{'-'.join(dq.DEFAULT_EXCLUDE)}:{cycle.first_year}-{cycle.last_year}:{numbers}:{sets}"


def summary(choice: str = "") -> Summary:
    cycle, cycles = pick_cycle(choice)
    if cycle is None:
        return Summary(None, cycles, [], [], notes=["No program cycle is set up yet, so there is nothing to summarize."])
    key = _cache_key(cycle)
    cached = cache.get(key)
    if cached is not None:
        cached.cycles = cycles
        return cached
    s = _build(cycle, cycles)
    cache.set(key, s, CACHE_SECONDS)
    return s


def _percent(part, whole) -> float | None:
    return None if part is None or not whole else 100 * part / whole


def _build(cycle: FilingCycle, cycles: list[FilingCycle]) -> Summary:
    from . import budget_view, goal_view

    # savings, by year (the claimed years come from here): the energy savings page's exclusions
    savings_metrics = ("kwh_first_year_net", "kw_first_year_net", "therms_first_year_net")
    saved = {
        s: dq.run(dq.Query(s, savings_metrics, by=("year",), year_from=cycle.first_year, year_to=cycle.last_year))
        for s in ("claims", "forecast")
    }
    claimed_years = sorted(r["year"] for r in saved["claims"].rows if r["year"] is not None)
    out = Summary(cycle, cycles, [], claimed_years, as_of=saved["claims"].as_of)
    claimed = span(claimed_years)
    so_far = f"Claimed {claimed}" if claimed else "Claimed so far"
    forecast_label = f"Forecast, {cycle}"

    # 1. spending against the budget
    spending = budget_view.compare(_view("budget", cycle))
    budget = sum(r.budget for r in spending.by_year if r.budget is not None) if any(r.budget for r in spending.by_year) else None
    forecast_spend = spending.results["forecast"].totals.get("expenditure") if "forecast" in spending.results else None
    spent = spending.results["claims"].totals.get("expenditure") if "claims" in spending.results else None
    unclaimed_monthly = [r for r in spending.by_year if r.values.get("claims") is None and r.monthly]
    card = Card("spending", "Spending", link=_link("budget", cycle))
    base, base_words = (budget, "operating budget") if budget else (forecast_spend, "budget in the latest filings")
    if spent is None and base is None:
        card.empty = "No budget or claimed expenditure is published yet."
    else:
        # with no claims in the cycle, the claimed figures are left out: the intro says so once (UI review 2, #23)
        if spent is not None or claimed_years:
            detail = f"of {money(base)} {base_words}" if base is not None else ""
            card.figures.append({"label": so_far, "value": money(spent), "detail": detail})
        else:
            card.figures.append({"label": f"{base_words.capitalize()}, {cycle}", "value": money(base)})
        if spent is not None and base:
            card.percent, card.percent_label = _percent(spent, base), "of budget"
        out.footnotes.append(
            "Spending is claimed expenditure against the operating budget (the filings plus fund shifts)."
            if budget
            else "Spending is claimed expenditure against the budget in the PAs' latest filings; no operating budget is recorded."
        )
        if unclaimed_monthly:
            reported = sum(r.monthly for r in unclaimed_monthly)
            years = span([r.key for r in unclaimed_monthly])
            out.footnotes.append(f"Monthly reports add {money(reported)} of spending for {years}, not yet claimed or validated.")
    out.cards.append(card)

    # 2. total system benefit against the goal (or, with no goal loaded, against the forecast)
    exclude = dashboard.PAGES["goals"].exclude
    tsb = {
        s: dq.run(dq.Query(s, ("tsb",), year_from=cycle.first_year, year_to=cycle.last_year, exclude=exclude)).totals.get("tsb")
        for s in ("claims", "forecast")
    }
    card = Card("tsb", "Total system benefit", link=_link("goals", cycle, period="cycle"))
    comparison = goal_view.compare(_view("goals", cycle, metric="tsb"), "cycle")
    rows = [r for r in comparison.rows if r.first_year <= cycle.last_year and r.last_year >= cycle.first_year]
    goal = sum(r.goal for r in rows if r.goal is not None) if any(r.goal is not None for r in rows) else None
    counted = sum(r.values.get("claims") or 0 for r in rows) if rows else None
    claims_shown = tsb["claims"] is not None or bool(claimed_years)
    if tsb["claims"] is None and goal is None and tsb["forecast"] is None:
        card.empty = "No claims are published yet."
    else:
        if goal is not None:
            goal_years = span([y for r in rows for y in (r.first_year, r.last_year)])
            if claims_shown:
                card.figures.append({"label": so_far, "value": money(tsb["claims"]), "detail": f"Goal {money(goal)}, {goal_years}"})
                card.percent, card.percent_label = _percent(counted or 0, goal), "of IOU goals"
            else:
                card.figures.append({"label": f"Goal, {goal_years}", "value": money(goal)})
            out.footnotes.append(
                f"Total system benefit is the voluntary programs' (no codes and standards, Energy Savings Assistance, or pension and "
                f"benefits); {money(counted)} of it counts toward the IOU territories' goals."
            )
        else:
            if claims_shown:
                detail = f"of {money(tsb['forecast'])} forecast" if tsb["forecast"] is not None else ""
                card.figures.append({"label": so_far, "value": money(tsb["claims"]), "detail": detail})
            else:
                card.figures.append({"label": forecast_label, "value": money(tsb["forecast"])})
            if tsb["claims"] is not None and tsb["forecast"]:
                card.percent, card.percent_label = _percent(tsb["claims"], tsb["forecast"]), "of forecast"
            out.footnotes.append(
                "No CPUC goal is loaded for this cycle yet, so total system benefit is measured against the latest filings' forecast."
            )
    out.cards.append(card)

    # 3-5. energy, demand and gas saved: net first year against the latest forecast
    c, f = saved["claims"].totals, saved["forecast"].totals
    units = (
        ("energy", "Energy saved", "kwh_first_year_net", "MWh", 0, "kwh"),
        ("demand", "Demand saved", "kw_first_year_net", "MW", 1, "kw"),
        ("gas", "Gas saved", "therms_first_year_net", "MMTherms", 2, "therms"),
    )
    for key, title, code, unit, decimals, metric in units:
        link = _link("energy-savings", cycle) if key == "energy" else _link("energy-savings", cycle, metric=metric)
        card = Card(key, title, link=link)
        claimed_value, forecast = c.get(code), f.get(code)
        if claimed_value is None and not claimed_years and forecast is None:
            card.empty = "No savings are published yet."
        elif claimed_value is None and not claimed_years:  # nothing claimed in the cycle yet: the forecast alone
            card.figures.append({"label": forecast_label, "value": number(forecast, decimals), "unit": unit})
        else:
            card.figures.append(
                {
                    "label": so_far,
                    "value": number(claimed_value, decimals),
                    "unit": unit,
                    "detail": f"of {number(forecast, decimals)} {unit} forecast" if forecast is not None else "",
                }
            )
            if key == "energy" and forecast:
                card.percent, card.percent_label = _percent(claimed_value or 0, forecast), "of forecast"
        out.cards.append(card)
    if any(c.get(code) is not None or f.get(code) is not None for _, _, code, *_ in units):
        out.footnotes.append(
            "Savings are net first-year savings against the latest filings' forecast, and leave out codes and standards, Energy Savings "
            "Assistance, and pension and benefits."
        )

    # 6-7. cost effectiveness, statewide
    exclude = dashboard.PAGES["cost-effectiveness"].exclude
    tests = {
        s: dq.run(dq.Query(s, ("trc", "pac"), year_from=cycle.first_year, year_to=cycle.last_year, exclude=exclude)).totals
        for s in ("claims", "forecast")
    }
    for test in ("trc", "pac"):
        extra = {} if test == "trc" else {"metric": test}
        card = Card(test, f"{test.upper()} ratio", link=_link("cost-effectiveness", cycle, **extra))
        claimed_value, forecast = tests["claims"].get(test), tests["forecast"].get(test)
        if claimed_value is None and not claimed_years and forecast is None:
            card.empty = "No cost-effectiveness results are published yet."
        elif claimed_value is None and not claimed_years:  # nothing claimed in the cycle yet: the forecast alone
            card.figures.append({"label": forecast_label, "value": ratio(forecast)})
        else:
            card.figures.append(
                {
                    "label": so_far,
                    "value": ratio(claimed_value),
                    "detail": f"{ratio(forecast)} forecast" if forecast is not None else "",
                }
            )
        out.cards.append(card)
    if any(tests[s].get(t) is not None for s in tests for t in ("trc", "pac")):
        out.footnotes.append(
            "TRC and PAC are statewide benefits divided by statewide costs, and also leave out programs excluded from cost effectiveness."
        )

    for r in (saved["claims"], saved["forecast"]):
        out.notes += [n for n in r.notes if n.startswith("Nothing") and n not in out.notes]
    return out

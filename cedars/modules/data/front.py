"""The front page's program-cycle summary (Jake, 2026-10-31): spending against the budget, total system benefit against the CPUC's goal,
the energy saved and the statewide TRC and PAC ratios, for one program cycle, from the platform's published data.

Every number is the one a dashboard page shows for the same years and its default exclusions, so each card links to the page that breaks
it down, and the two agree:
  spending           the budget page (budget_view): claimed expenditure against the operating budget (the standing filings plus fund
                     shifts), or against the latest filings' forecast when no operating budget is recorded
  total system benefit  the goals page's exclusions (codes and standards, ESA, pension and benefits out): the statewide claimed TSB of
                     the voluntary programs, and what counts toward the IOU territories' cycle goals (goal_view, the goal set in force)
  savings            the energy savings page: net first-year MWh, MW and MMTherms
  cost effectiveness  the cost-effectiveness page: TRC and PAC ratios over every PA's summed benefits and costs

The goal comes from the confirmed goal set in force (Administration > Goals); when none covers the cycle, the card says so."""

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


@dataclass
class Card:
    key: str  # spending, tsb, savings, cost_effectiveness: the tour and the tests name a card by it
    title: str
    figures: list[dict] = field(default_factory=list)  # {"label", "value" (text), "detail"}: the card's numbers, biggest first
    lines: list[str] = field(default_factory=list)  # what the numbers are, in words
    percent: float | None = None  # the progress bar: spent of budget, TSB of goal (0-100, may exceed 100)
    percent_label: str = ""
    link: str = ""
    link_text: str = ""
    empty: str = ""  # the card's message when it has no numbers

    @property
    def bar(self) -> float | None:
        """The progress bar's filled share, 0 to 100 (past 100 the bar is full and the words say how far past)."""
        return None if self.percent is None else round(max(0.0, min(self.percent, 100.0)), 2)


@dataclass
class Summary:
    cycle: FilingCycle | None
    cycles: list[FilingCycle]
    cards: list[Card]
    claimed_years: list[int]
    as_of: object = None  # the claims release's time
    notes: list[str] = field(default_factory=list)

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
    return f"front:{cycle.first_year}-{cycle.last_year}:{numbers}:{sets}"


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
    so_far = f"claimed {claimed}" if claimed else "claimed so far"

    # 1. spending against the budget
    spending = budget_view.compare(_view("budget", cycle))
    budget = sum(r.budget for r in spending.by_year if r.budget is not None) if any(r.budget for r in spending.by_year) else None
    forecast_spend = spending.results["forecast"].totals.get("expenditure") if "forecast" in spending.results else None
    spent = spending.results["claims"].totals.get("expenditure") if "claims" in spending.results else None
    unclaimed_monthly = [r for r in spending.by_year if r.values.get("claims") is None and r.monthly]
    card = Card("spending", "Spending", link=_link("budget", cycle), link_text="Budget and spending by year and program")
    base, base_words = (budget, "operating budget") if budget else (forecast_spend, "budget in the latest filings")
    if spent is None and base is None:
        card.empty = "No budget or claimed expenditure is published for this cycle yet."
    else:
        # with no claims in the cycle, the claimed figures are left out: the intro says so once (UI review 2, #23)
        if spent is not None or claimed_years:
            card.figures.append({"label": f"Spent, {so_far}", "value": money(spent)})
        card.figures.append({"label": f"{base_words.capitalize()}, {cycle}", "value": money(base)})
        if spent is not None and base:
            card.percent = 100 * spent / base
            card.percent_label = f"{card.percent:.0f}% of the {base_words} spent"
        card.lines.append(
            "Claimed spending against the operating budget (filings plus fund shifts)."
            if budget
            else "Claimed spending against the budget in the PAs' latest filings."
        )
        if unclaimed_monthly:
            reported = sum(r.monthly for r in unclaimed_monthly)
            years = span([r.key for r in unclaimed_monthly])
            card.lines.append(f"Monthly reports add {money(reported)} for {years}, not yet claimed or validated.")
    out.cards.append(card)

    # 2. total system benefit against the goal
    tsb_query = dq.Query("claims", ("tsb",), year_from=cycle.first_year, year_to=cycle.last_year, exclude=dashboard.PAGES["goals"].exclude)
    tsb = dq.run(tsb_query).totals.get("tsb")
    card = Card("tsb", "Total system benefit", link=_link("goals", cycle, period="cycle"), link_text="Goals by IOU territory")
    comparison = goal_view.compare(_view("goals", cycle, metric="tsb"), "cycle")
    rows = [r for r in comparison.rows if r.first_year <= cycle.last_year and r.last_year >= cycle.first_year]
    goal = sum(r.goal for r in rows if r.goal is not None) if any(r.goal is not None for r in rows) else None
    counted = sum(r.values.get("claims") or 0 for r in rows) if rows else None
    if tsb is None and goal is None:
        card.empty = "No claims are published for this cycle yet."
    if tsb is not None or claimed_years:
        card.figures.append({"label": f"Voluntary programs, {so_far}", "value": money(tsb)})
    if goal is not None:
        goal_years = span([y for r in rows for y in (r.first_year, r.last_year)])
        card.figures.append({"label": f"Goal, {goal_years}", "value": money(goal)})
        card.percent = 100 * (counted or 0) / goal if goal and (tsb is not None or claimed_years) else None
        if card.percent is not None:
            card.percent_label = f"{card.percent:.0f}% of the goal"
        card.lines.append(f"{money(counted)} counts toward the IOU territories' goals.")
    else:
        card.lines.append("No CPUC goal is loaded for this cycle yet.")
    card.lines.append("Leaves out codes and standards, Energy Savings Assistance, and pension and benefits.")
    out.cards.append(card)

    # 3. energy saved
    card = Card("savings", "Energy saved", link=_link("energy-savings", cycle), link_text="Savings by sector, year and program")
    c, f = saved["claims"].totals, saved["forecast"].totals
    if c.get("kwh_first_year_net") is None and f.get("kwh_first_year_net") is None:
        card.empty = "No savings are published for this cycle yet."
    else:
        units = (("kwh_first_year_net", "MWh", 0), ("kw_first_year_net", "MW", 1), ("therms_first_year_net", "MMTherms", 2))
        for code, unit, decimals in units:
            if c.get(code) is None and not claimed_years:  # nothing claimed in the cycle yet: the forecast alone
                if f.get(code) is not None:
                    card.figures.append({"label": f"{unit} forecast", "value": number(f.get(code), decimals)})
                continue
            card.figures.append(
                {
                    "label": unit,
                    "value": number(c.get(code), decimals),
                    "detail": f"of {number(f.get(code), decimals)} forecast" if f.get(code) is not None else "",
                }
            )
        card.lines.append("Net first-year savings against the latest forecast. Leaves out codes and standards, and pension and benefits.")
    out.cards.append(card)

    # 4. cost effectiveness, statewide
    card = Card(
        "cost_effectiveness",
        "Cost effectiveness",
        link=_link("cost-effectiveness", cycle),
        link_text="Cost-effectiveness by sector, year and program",
    )
    exclude = dashboard.PAGES["cost-effectiveness"].exclude
    tests = {
        s: dq.run(dq.Query(s, ("trc", "pac"), year_from=cycle.first_year, year_to=cycle.last_year, exclude=exclude)).totals
        for s in ("claims", "forecast")
    }
    if all(tests[s].get("trc") is None and tests[s].get("pac") is None for s in tests):
        card.empty = "No cost-effectiveness results are published for this cycle yet."
    else:
        for test in ("trc", "pac"):
            forecast = tests["forecast"].get(test)
            if tests["claims"].get(test) is None and not claimed_years:  # nothing claimed in the cycle yet: the forecast alone
                if forecast is not None:
                    card.figures.append({"label": f"{test.upper()} ratio forecast", "value": ratio(forecast)})
                continue
            card.figures.append(
                {
                    "label": f"{test.upper()} ratio",
                    "value": ratio(tests["claims"].get(test)),
                    "detail": f"{ratio(forecast)} forecast" if forecast is not None else "",
                }
            )
        card.lines.append(
            "Statewide benefits divided by statewide costs. Leaves out codes and standards, pension and benefits, and "
            "programs excluded from cost effectiveness."
        )
    out.cards.append(card)

    for r in (saved["claims"], saved["forecast"]):
        out.notes += [n for n in r.notes if n.startswith("Nothing") and n not in out.notes]
    return out

"""What a dashboard section shows, said in one sentence: the totals, the share of the forecast claimed, which rows are above it and which
is furthest behind. The sentence opens each page (and each later section) so nobody has to work the result out from the table, and it
is the chart's summary for screen readers too. Numbers are written short ("$1.50B", "12.3K MWh") by `compact`, which the tiles and the
bar labels use as well.

Every sentence is built from the rows the page already shows; nothing here queries again."""

from .dashboard import SERIES, Comparison, code_label
from .front import _sig, money
from .query import DIMENSIONS


def compact(metric, value) -> str:
    """A metric's value short, with its unit: $1.50B, $313M, 1.19 (a ratio), 12.3K MWh, 4.21 MMTherms. A dash when blank."""
    if value is None:
        return "–"
    if metric.unit.startswith("$"):
        return money(value)
    if metric.unit == "ratio":
        return f"{value:,.2f}"
    a = abs(value)
    if a >= 1e6:
        shown = f"{_sig(value / 1e6)}M"
    elif a >= 1e4:
        shown = f"{_sig(value / 1e3)}K"
    else:
        shown = f"{value:,.{metric.decimals}f}"
    return f"{shown} {metric.unit}".strip()


def _noun(label: str) -> str:
    """A metric's label inside a sentence: "total system benefit", but "TRC ratio" and "CO2 avoided" keep their capitals."""
    first, _, rest = label.partition(" ")
    keep = len(first) > 1 and (first[1].isupper() or first[1].isdigit())
    return f"{first if keep else first.lower()}{' ' if rest else ''}{rest}"


def _plural(by: str) -> str:
    words = DIMENSIONS[by].in_sentence
    if words.endswith("y") and not words.endswith("ey"):
        return words[:-1] + "ies"
    return words + "s"


def _name(row) -> str:
    return "(blank)" if row.key is None else str(code_label(row.key))


def comparison(c: Comparison) -> str:
    """One sentence for a chart-and-table section (blank when there is nothing to say)."""
    metric = c.view.metric
    noun = _noun(metric.label)
    rows = [r for r in c.rows if r.key is not None]
    if c.both and metric.adds_up:
        claimed, forecast = c.totals.values.get("claims"), c.totals.values.get("forecast")
        if claimed is None and forecast is None:
            return ""
        if claimed is None:
            return f"The forecast {noun} is {compact(metric, forecast)}; nothing is claimed for these years yet."
        if forecast is None:
            return f"Claimed {noun} is {compact(metric, claimed)}; no forecast covers these years."
        pct = c.totals.percent
        if pct is None:
            return f"Claimed {noun} is {compact(metric, claimed)} and the forecast {compact(metric, forecast)}, over different years."
        text = f"Claimed {noun} is {compact(metric, claimed)}, {pct:,.0f}% of the {compact(metric, forecast)} forecast."
        measured = [r for r in rows if r.percent is not None]
        if len(measured) < 2 or c.by == "year":
            return text
        reached = [r for r in measured if r.percent >= 99.5]
        lowest = min(measured, key=lambda r: r.percent)
        noun_rows = _plural(c.by)
        if len(reached) == len(measured):
            return f"{text} All {len(measured)} {noun_rows} claimed at least their forecast."
        behind = f"{_name(lowest)} is lowest at {lowest.percent:,.0f}%."
        if not reached:
            return f"{text} None of the {len(measured)} {noun_rows} has claimed its forecast yet; {behind}"
        return f"{text} {len(reached)} of {len(measured)} {noun_rows} claimed at least their forecast; {behind}"
    series = c.view.series[-1]
    label = SERIES[series][0]
    total = c.totals.values.get(series)
    if total is None:
        return ""
    valued = [r for r in rows if r.values.get(series) is not None]
    if metric.adds_up:
        text = f"{label} {noun} totals {compact(metric, total)}."
        if len(valued) >= 2 and total > 0 and all(r.values[series] >= 0 for r in valued):
            top = max(valued, key=lambda r: r.values[series])
            share = 100 * top.values[series] / total
            text += f" {_name(top)} is the largest at {compact(metric, top.values[series])} ({share:,.0f}%)."
        return text
    text = f"{label} {noun} is {compact(metric, total)} overall"
    other = c.totals.values.get("forecast") if series == "claims" and c.both else None
    text += f" (forecast {compact(metric, other)})." if other is not None else "."
    if len(valued) >= 2:
        high = max(valued, key=lambda r: r.values[series])
        low = min(valued, key=lambda r: r.values[series])
        text += (
            f" {_name(high)} is highest at {compact(metric, high.values[series])}; "
            f"{_name(low)} is lowest at {compact(metric, low.values[series])}."
        )
    return text


def goals(g) -> str:
    """The goals page: how many territory periods have claimed their goal, and which is furthest from it."""
    measured = [r for r in g.rows if r.goal and r.values.get("claims") is not None]
    if not measured:
        return ""
    unit = "period" if g.period == "cycle" else "year"
    reached = [r for r in measured if r.percent("claims") >= 99.5]
    lowest = min(measured, key=lambda r: r.percent("claims"))
    pct = f"{lowest.percent('claims'):,.0f}%"
    if len(measured) == 1:
        return f"{lowest.territory} {lowest.period} has claimed {pct} of its goal so far."
    verb = "has" if len(reached) == 1 else "have"
    return (
        f"{len(reached)} of {len(measured)} territory {unit}s shown {verb} claimed the goal so far; "
        f"{lowest.territory} {lowest.period} is lowest at {pct} of its goal."
    )


def spending(s) -> str:
    """The budget page: claimed expenditure against the operating budget over the years that have both."""
    both = [r for r in s.by_year if r.budget and r.values.get("claims") is not None]
    if not both:
        budget = sum(r.budget for r in s.by_year if r.budget)
        return f"The operating budget is {money(budget)}; no expenditure is claimed against it yet." if budget else ""
    claimed = sum(r.values["claims"] for r in both)
    budget = sum(r.budget for r in both)
    years = sorted(r.key for r in both if r.key is not None)
    span = str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}"
    return f"Claimed expenditure is {money(claimed)}, {100 * claimed / budget:,.0f}% of the {money(budget)} operating budget for {span}."


TEST_WORDS = {
    "trc": "the total resource cost test (TRC), which counts the costs to participants and the program administrator",
    "pac": "the program administrator cost test (PAC), which counts the program administrator's costs only",
    "rim": "the ratepayer impact measure (RIM), which also counts the revenue utilities no longer collect",
    "scb": "the societal cost test (SCT) at its base values",
    "sch": "the societal cost test (SCT) at its high values",
}
BASIS_WORDS = (
    "Net counts only what the programs caused (after the net-to-gross ratio); gross counts everything installed. First year is the "
    "first year's amount; lifecycle adds up every year of the measures' lives."
)


def about(metric) -> str:
    """A metric in plain words, before its catalog definition (which names the fields it sums)."""
    code = metric.code
    if code.startswith("tsb"):
        return (
            "The lifecycle value of the savings to the electric and gas systems, in present-value dollars: the CPUC's goal metric for "
            "incentive programs."
        )
    if code == "expenditure":
        return "What the programs spent, in nominal dollars."
    test = code.split("_")[0]
    if metric.group == "Cost tests" and test in TEST_WORDS:
        if metric.unit == "ratio":
            return f"Benefits divided by costs under {TEST_WORDS[test]}. Above 1.0, the benefits exceed the costs."
        return f"Present-value {'benefits' if code.endswith('benefit') else 'costs'} under {TEST_WORDS[test]}."
    if metric.group == "Savings":
        return BASIS_WORDS
    if metric.group == "Emissions":
        return f"Emissions avoided by the electricity and gas saved. {BASIS_WORDS}"
    if metric.group == "Cost elements":
        return "One part of what the programs spent, in nominal dollars."
    return ""


def question(metric) -> str:
    return f"What is {_noun(metric.label)}?"

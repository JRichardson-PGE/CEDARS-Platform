"""The metric catalog: one definition for each number a dashboard page or download shows (spec "Metric definitions"). A metric sums
canonical output fields (decision L) over the rows a query selects; a ratio divides two such sums after summing, never averages
row ratios, and is blank when its denominator is 0. Display scaling (kWh to MWh, pounds to metric tons) happens after summing.

Percent of goal, percent of forecast and program share compare two results, so the pages compute them (`DERIVED` documents them)."""

from dataclasses import dataclass

LB_PER_METRIC_TON = 2204.62262


@dataclass(frozen=True)
class Metric:
    code: str
    label: str
    group: str
    numerator: tuple[str, ...]  # canonical fields, summed
    denominator: tuple[str, ...] = ()  # a ratio when set
    unit: str = ""  # the unit shown, after `scale`
    scale: float = 1.0  # the summed fields are divided by this for display
    decimals: int = 0
    definition: str = ""

    @property
    def adds_up(self) -> bool:
        return not self.denominator

    @property
    def fields(self) -> tuple[str, ...]:
        return self.numerator + self.denominator

    def value(self, sums: dict):
        """The metric from its fields' sums (missing sums count as 0). None for a ratio over a zero cost."""
        top = sum(sums.get(f) or 0 for f in self.numerator)
        if not self.denominator:
            return top / self.scale
        bottom = sum(sums.get(f) or 0 for f in self.denominator)
        return None if bottom == 0 else top / bottom


CATALOG: dict[str, Metric] = {}


def _add(m: Metric) -> Metric:
    assert m.code not in CATALOG, m.code
    CATALOG[m.code] = m
    return m


# --- benefits and spending

_add(
    Metric(
        "tsb",
        "Total system benefit",
        "Benefits",
        ("TotalSystemBenefit",),
        unit="$",
        definition="Net lifecycle system benefit (PV): all benefits less supply-type costs. Codes and standards count toward their own "
        "savings goals, not the TSB goal.",
    )
)
_add(Metric("tsb_gross", "Total system benefit (gross)", "Benefits", ("TotalSystemBenefitGross",), unit="$", definition="TSB before NTG."))
_add(
    Metric(
        "expenditure",
        "Expenditure",
        "Spending",
        ("TotalExpenditures",),
        unit="$ nominal",
        definition="What the programs spend on the measures, nominal dollars.",
    )
)
for code, label, fields_ in [
    ("admin_overhead", "Administration: overhead and G&A", ("WtdAdminCostsOverheadAndGA",)),
    ("admin_other", "Administration: other", ("WtdAdminCostsOther",)),
    ("marketing", "Marketing and outreach", ("WtdMarketingOutreach",)),
    (
        "direct_implementation",
        "Direct implementation (non-incentive)",
        ("WtdDIActivity", "WtdDIInstallation", "WtdDIHardwareAndMaterials", "WtdDIRebateAndInspection"),
    ),
    ("emv", "EM&V", ("WtdEMV",)),
    ("incentives", "Rebates and incentives", ("RebatesandIncents",)),
    ("costs_recovered", "Costs recovered from other sources", ("WtdCostsRecoveredFromOtherSources",)),
]:
    _add(Metric(code, label, "Cost elements", fields_, unit="$ nominal", definition=f"Sum of {', '.join(fields_)}."))

# --- cost tests: benefits, costs and their ratios

TESTS = {"trc": "TRC", "pac": "PAC", "rim": "RIM", "scb": "SCT base", "sch": "SCT high"}
for code, name in TESTS.items():
    f = code.upper()
    _add(Metric(f"{code}_benefit", f"{name} benefits", "Cost tests", (f"{f}Ben",), unit="$", definition=f"Sum of {f}Ben (PV)."))
    _add(Metric(f"{code}_cost", f"{name} costs", "Cost tests", (f"{f}Cost",), unit="$", definition=f"Sum of {f}Cost (PV)."))
    _add(
        Metric(
            code,
            f"{name} ratio",
            "Cost tests",
            (f"{f}Ben",),
            (f"{f}Cost",),
            unit="ratio",
            decimals=2,
            definition=f"Sum of {f}Ben divided by sum of {f}Cost; blank when the cost is 0.",
        )
    )
    _add(
        Metric(
            f"{code}_no_admin",
            f"{name} ratio without admin",
            "Cost tests",
            (f"{f}Ben",),
            (f"{f}CostNoAdmin",),
            unit="ratio",
            decimals=2,
            definition=f"Sum of {f}Ben divided by sum of {f}CostNoAdmin; blank when the cost is 0.",
        )
    )

# --- savings and emissions: one metric per gross / net and first year / lifecycle

SAVINGS = {
    "kwh": ("Electric energy savings", "KWh", "MWh", 1_000, 0),
    "kw": ("Electric demand savings", "KW", "MW", 1_000, 1),
    "therms": ("Gas savings", "Thm", "MMTherms", 1_000_000, 2),
}
EMISSIONS = {
    "co2": ("CO2 avoided", ("ElecCO2", "GasCO2"), 1),  # metric tons already
    "nox": ("NOx avoided", ("ElecNOx", "GasNOx"), LB_PER_METRIC_TON),  # pounds
    "pm10": ("PM10 avoided", ("ElecPM10",), LB_PER_METRIC_TON),  # pounds
}
PERIODS = {"first_year": ("FirstYear", "first year"), "lifecycle": ("Lifecycle", "lifecycle")}
BASES = {"net": "Net", "gross": "Gross"}

for base, (label, suffix, unit, scale, decimals) in SAVINGS.items():
    for period, (prefix, period_label) in PERIODS.items():
        if base == "kw" and period == "lifecycle":
            continue  # demand savings are first-year only
        for basis, word in BASES.items():
            f = f"{prefix}{word}{suffix}"
            _add(
                Metric(
                    f"{base}_{period}_{basis}",
                    f"{label}, {period_label} {basis}",
                    "Savings",
                    (f,),
                    unit=unit,
                    scale=scale,
                    decimals=decimals,
                    definition=f"Sum of {f}, shown in {unit}.",
                )
            )
for base, (label, parts, scale) in EMISSIONS.items():
    for period, (prefix, period_label) in PERIODS.items():
        for basis, word in BASES.items():
            fs = tuple(f"{prefix}{word}{p}" for p in parts)
            source = "metric tons" if scale == 1 else "pounds, shown in metric tons"
            _add(
                Metric(
                    f"{base}_{period}_{basis}",
                    f"{label}, {period_label} {basis}",
                    "Emissions",
                    fs,
                    unit="metric tons",
                    scale=scale,
                    decimals=1,
                    definition=f"Sum of {' + '.join(fs)} ({source}).",
                )
            )

DERIVED = [
    (
        "percent_of_goal",
        "% of goal",
        "A territory's metric (counted PAs, after the counting rules' shares) divided by its goal for the year.",
    ),
    ("percent_of_forecast", "% of forecast", "Claimed divided by forecast for the same filters, from the chosen filing."),
    ("claimed_less_forecast", "Claimed less forecast", "Claimed minus forecast for the same filters."),
    ("share", "Share", "A group's metric divided by the filtered total."),
]


def get(code: str) -> Metric:
    try:
        return CATALOG[code]
    except KeyError as e:
        raise ValueError(f"unknown metric {code!r}") from e


def resolve(base: str, *, gross: bool = False, lifecycle: bool = False) -> Metric:
    """The view controls' metric: e.g. resolve("kwh", lifecycle=True) is kwh_lifecycle_net. TSB has a gross form but no period; kW
    has no lifecycle form (the first-year one is used); the rest ignore both switches."""
    if base == "tsb":
        return get("tsb_gross" if gross else "tsb")
    if base in SAVINGS or base in EMISSIONS:
        period = "lifecycle" if lifecycle and base != "kw" else "first_year"
        return get(f"{base}_{period}_{'gross' if gross else 'net'}")
    return get(base)


def catalog_rows() -> list[dict]:
    """The catalog as rows, for the dictionary and its download."""
    rows = [
        {
            "code": m.code,
            "label": m.label,
            "group": m.group,
            "numerator": " + ".join(m.numerator),
            "denominator": " + ".join(m.denominator),
            "adds_up": "yes" if m.adds_up else "no",
            "unit": m.unit,
            "scale": m.scale,
            "decimals": m.decimals,
            "definition": m.definition,
        }
        for m in CATALOG.values()
    ]
    rows += [
        {
            "code": c,
            "label": lbl,
            "group": "Comparisons",
            "adds_up": "no",
            "unit": "%" if "percent" in c or c == "share" else "",
            "definition": d,
        }
        for c, lbl, d in DERIVED
    ]
    return rows

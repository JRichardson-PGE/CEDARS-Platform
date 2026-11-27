"""One finding per problem (rules.repeats): a rule testing a field that already fails its own constraint, the other half of a rule CEDARS
states both ways, and the rule set repeating a module's own check are left out, and the list says so; which records are rejected does
not change."""

import pytest

from cedars.services.rules import repeats
from cedars.services.rules import service as rules

from .test_budgets import filer, filing, job, programs  # noqa: F401
from .test_rule_set_fixes import budget_rules  # noqa: F401

pytestmark = pytest.mark.django_db
pytest.importorskip("recet.filing_rules", reason="the rule library runs on the pinned ReCET engine")


def measure(rs, *rows):
    import pandas as pd

    cols = rules.spec_columns(rs, "Measure")
    return pd.DataFrame([{c: r.get(c, "") for c in cols} | {k: v for k, v in r.items() if k not in cols} for r in rows]).astype(str)


def by_field(found, field):
    return sorted(f.rule_id for f in found if f.field == field)


def ids(rs, field, rule) -> set[str]:
    return {r.rule_id for r in rs.rules.filter(kind="rule") if rules.canonical_field(r.field) == field and r.definition.get("rule") == rule}


def test_a_blank_field_is_its_own_constraint_once(budget_rules):
    rs = budget_rules
    df = measure(rs, {"CEInputID": "PGE-2028-1", "ClaimYearQuarter": "2028Q1", "RUL_Yrs": "", "EUL_Yrs": "10"})
    found, notes = rules.check(rs, "Measure", df, filing_year=2028)
    rul = [f for f in found if f.field == "RUL_Yrs"]
    assert [f.message for f in rul] == ["RUL_Yrs should not be empty"]  # not that and three rules that fail on the blank
    assert repeats.NOTE in notes
    assert {f.severity for f in found} == {"reject"}


def test_a_rule_stated_both_ways_is_one_finding(budget_rules):
    rs = budget_rules
    pairs = repeats.pairs(list(rs.rules.filter(table="Measure")))
    both_ways = next(iter(ids(rs, "RUL_Yrs", "does not equal")))
    assert both_ways in pairs  # "RUL_Yrs should be different from 0 when any 2nd-baseline field is different from 0"
    base = {"CEInputID": "PGE-2028-1", "ClaimYearQuarter": "2028Q1", "RUL_Yrs": "0", "EUL_Yrs": "10", "MeasAppType": "NR"}
    base |= dict.fromkeys(["UnitkW2ndBaseline", "UnitkWh2ndBaseline", "UnitTherm2ndBaseline", "UnitMeaCost2ndBaseline",
                           "UnitkWhIOUWater2ndBaseline"], "0")  # fmt: skip
    therm = base | {"UnitTherm2ndBaseline": "5"}
    cost = base | {"UnitTherm2ndBaseline": "5", "UnitMeaCost2ndBaseline": "5", "CEInputID": "PGE-2028-2"}
    found, _notes = rules.check(rs, "Measure", measure(rs, therm, cost), filing_year=2028)
    first = [f for f in found if f.row == 2]
    # the therm rule ("UnitTherm2ndBaseline should equal 0 when RUL_Yrs equals 0") explains the RUL_Yrs one: only it is listed
    assert "UnitTherm2ndBaseline" in {f.field for f in first} and both_ways not in {f.rule_id for f in first}
    # the measure cost has no turned-round rule of its own, so the RUL_Yrs finding still says something new: it stays
    assert both_ways in {f.rule_id for f in found if f.row == 3}


def test_rates_that_exclude_each_other_are_one_finding(budget_rules):
    rs = budget_rules
    row = {"CEInputID": "PGE-2028-1", "ClaimYearQuarter": "2028Q1"}
    row |= dict.fromkeys(["InstallationRatekW", "InstallationRatekWh", "InstallationRateTherm", "RealizationRatekW",
                          "RealizationRatekWh", "RealizationRateTherm"], "1")  # fmt: skip
    row |= {"InstallationRatekW": "0.9", "RealizationRatekWh": "0.8"}
    found, _notes = rules.check(rs, "Measure", measure(rs, row), filing_year=2028)
    rate = [f for f in found if f.field in ("InstallationRatekW", "RealizationRatekWh") and f.rule_id.startswith("V")]
    assert len(rate) == 1  # before: both "InstallationRatekW should equal 1 when ..." and "RealizationRatekWh should equal 1 when ..."


def test_the_module_check_is_kept_and_the_rule_sets_repeat_left_out(filer, filing, pge, programs, budget_rules):  # noqa: F811
    from cedars.modules.budgets import service as budgets

    v = budgets.upload(filer, filing, pge, job(measures=["M1|PGE_Res_HVAC|2028Q1|abc|20|30|5|5"]), "num.zip")
    on_units = [f for f in budgets.findings(v) if f["field"] == "NumUnits"]
    assert [f["rule_id"] for f in on_units] == ["BUD-NUM"]
    assert repeats.NOTE in v.findings["notes"]

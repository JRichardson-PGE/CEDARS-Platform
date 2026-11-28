# ruff: noqa: F811 - fixtures imported from other test files
"""What a filing's rule set and the module checks beside it get right together: a PrgID is checked against the program registry, never
the engine's shipped copy of CEDARS' program list; a measure filed twice is refused before pricing, which would refuse it; a budget
CEInputID starts with the filer's own PA code; and a worker checks with the same PA codes and programs as this website."""

import pytest

from cedars.modules.budgets import jobfile
from cedars.modules.budgets import service as budgets
from cedars.modules.claims import service as claims
from cedars.services.dictionary import value_lists as vls
from cedars.services.rules import jobs, precheck
from cedars.services.rules import service as rules
from cedars.services.rules.models import CheckJob

from .test_budgets import filer as budget_filer  # noqa: F401
from .test_budgets import filing, job  # noqa: F401
from .test_budgets import programs as budget_programs  # noqa: F401
from .test_claims import claim, claims_zip, deadline, filer, programs, row, tables, year  # noqa: F401
from .test_rules_checker import claims_rules  # noqa: F401
from .test_rules_in_worker import at_once, here, worker_mode  # noqa: F401

pytestmark = pytest.mark.django_db
pytest.importorskip("recet.filing_rules", reason="the rule library runs on the pinned ReCET engine")


@pytest.fixture(autouse=True)
def fresh_engine():
    """Each test starts with the engine's shipped lists, whatever an earlier test handed it."""
    from recet import editor

    vls.reset_engine()
    editor.set_value_list_changes({})
    yield
    vls.reset_engine()
    editor.set_value_list_changes({})


@pytest.fixture
def budget_rules(cpuc_admin):
    return rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "budget_filing"))


def prgid_rules(rs) -> set[str]:
    return {r.rule_id for r in rs.rules.filter(kind="spec") if (r.definition or {}).get("check") == "value_list"
            and str((r.definition or {}).get("list", "")).lower() == "prgid"}  # fmt: skip


def full_tables(rs, measures=None) -> dict[str, bytes]:
    """The test filing's ProgramCost and Measure tables with every column of the rule set's specification."""
    raw, _ = jobfile.read_upload(job(measures=measures), "j.zip")
    out = {}
    for table in ("ProgramCost", "Measure"):
        rows = jobfile.rows(raw[table])[1]
        cols = list(dict.fromkeys(list(rows[0]) + rules.spec_columns(rs, table)))
        out[table] = ("\n".join(["|".join(cols)] + ["|".join(r.get(c, "") for c in cols) for r in rows]) + "\n").encode()
    return out


def test_prgid_rules_read_the_program_registry(budget_programs, budget_rules):
    """PGE_Res_HVAC is in the registry but not in the engine's 2026-10-03 copy of CEDARS' programs: before, every row of it was refused."""
    import pandas as pd

    ids = prgid_rules(budget_rules)
    assert ids
    df = pd.DataFrame({"PrgID": ["PGE_Res_HVAC", "NOPE_Not_Registered"], "PrgYear": ["2028", "2028"]})
    found, _ = rules.check(budget_rules, "ProgramCost", df, filing_year=2028, id_column=["PrgID", "PrgYear"])
    assert [f.record_id for f in found if f.rule_id in ids] == ["NOPE_Not_Registered / 2028"]
    # the whole filing: no PrgID value-list finding on a registered program's rows
    found, _notes, _label = budgets._rule_findings(full_tables(budget_rules), budgets_cycle(), budget_rules)
    assert not [f for f in found if f.rule_id in ids]


def budgets_cycle():
    return budgets._ExampleCycle(2028, 2031)  # the years 2028 to 2031, as a cycle filing covers them


def test_a_budget_measure_filed_twice_is_refused(budget_filer, filing, pge, budget_programs):
    v = budgets.upload(budget_filer, filing, pge, job(measures=["M1|PGE_Res_HVAC|2028Q1|10|20|30|5|5", "m1|PGE_Res_HVAC|2028Q2|1|1|0|0|0"]),
                       "dup.zip")  # fmt: skip
    dup = [f for f in budgets.findings(v) if f["rule_id"] == "BUD-DUP"]
    assert [(f["table"], f["row"], f["field"]) for f in dup] == [("Measure", 3, "CEInputID")] and "line 2" in dup[0]["message"]
    assert v.status == "draft"


def test_a_budget_program_year_filed_twice_is_refused(budget_filer, filing, pge, budget_programs):
    costs = ["PGE_Res_HVAC|2028|2028Q1|PGE|1000|0|0|0|0|0|0|0|0|0|0", "PGE_Res_HVAC|2028|2028Q2|PGE|5|0|0|0|0|0|0|0|0|0|0",
             "PGE_Res_HVAC|2029|2029Q1|PGE|1100|0|0|0|0|0|0|0|0|0|0"]  # fmt: skip
    v = budgets.upload(budget_filer, filing, pge, job(costs=costs, measures=["M1|PGE_Res_HVAC|2028Q1|10|20|30|5|5"]), "dup.zip")
    dup = [f for f in budgets.findings(v) if f["rule_id"] == "BUD-DUP"]
    assert [(f["table"], f["row"], f["field"], f["severity"]) for f in dup] == [("ProgramCost", 3, "PrgYear", "reject")]
    assert "two ProgramCost rows for 2028" in dup[0]["message"] and v.status == "draft"
    # the first row is the one kept: the filing's 2028 costs are its own
    assert v.budget_lines.get(prg_id="PGE_Res_HVAC", year=2028).program_costs["AdminCostsOverheadAndGA"] == "1000"


def test_a_claim_measure_filed_twice_is_refused_under_a_rule_set(filer, year, pge, programs, claims_rules):
    """The rule set checks keys of two or more columns only: Measure's one-column key is the module's to check, as pricing refuses it."""
    t = tables(
        claim("PGE-2028-001"),
        Measure=[row("Measure", ClaimID="PGE-2028-001", PrgID="PGE_Res", ClaimYearQuarter="2028Q1")],
        ProgramCost=[row("ProgramCost", PrgID="PGE_Res")],
        Site=[row("Site", SiteID="PGE-S1")],
    )
    v = claims.upload_claims(filer, deadline(), pge, claims_zip(t, full=True), "dup.zip")
    key = [f for f in claims.findings(v) if f["rule_id"] == "CLM-KEY"]
    assert [(f["table"], f["row"]) for f in key] == [("Measure", 3)]
    assert v.status != "validated"


def test_a_budget_ceinputid_starts_with_the_filers_pa(pge, budget_programs, budget_rules):
    measures = ["PGE-2028-1|PGE_Res_HVAC|2028Q1|1|1|0|0|0", "SCE-2028-2|PGE_Res_HVAC|2028Q1|1|1|0|0|0",
                "PGE-20283|PGE_Res_HVAC|2028Q1|1|1|0|0|0", "SW-2028-4|PGE_Res_HVAC|2028Q1|1|1|0|0|0"]  # fmt: skip
    prefix = {r.rule_id for r in budget_rules.rules.filter(kind="spec") if (r.definition or {}).get("check") == "starts_with"}
    found, _notes, _label = budgets._rule_findings(full_tables(budget_rules, measures), budgets_cycle(), budget_rules, pge)
    assert sorted(f.record_id for f in found if f.rule_id in prefix) == ["PGE-20283", "SCE-2028-2"]


def test_a_worker_checks_with_this_websites_pas_and_programs(cpuc_admin, budget_programs, budget_rules, at_once, settings):
    """A PA code added here and a registered program are accepted by the worker as they are here (before, the worker read its own PA list
    and the engine's copy of CEDARS' programs)."""
    pa = vls.get_list("pa")
    d = vls.draft_of(pa) or vls.start_draft(cpuc_admin, pa)  # adding an organization opens a draft of the PA list
    vls.add_code(cpuc_admin, d, "NEWC", description="A new community choice aggregator", start_year=2028, reason="New PA")
    vls.publish(cpuc_admin, d, budgets_today(), "New PA")
    measures = ["NEWC-2028-1|PGE_Res_HVAC|2028Q1|1|1|0|0|0", "XYZ-2028-2|PGE_Res_HVAC|2028Q1|1|1|0|0|0"]
    content = full_tables(budget_rules, measures)["Measure"]
    worker = precheck.check("budgets", "Measure", content, rule_set=budget_rules, filing_year=2028)
    assert CheckJob.objects.filter(purpose="pre-upload check", status=CheckJob.Status.SUCCEEDED).count() == 1
    settings.RULES_RUN_IN_WORKER = False
    here_ = precheck.check("budgets", "Measure", content, rule_set=budget_rules, filing_year=2028)
    pick = {r.rule_id for r in budget_rules.rules.filter(kind="spec") if (r.definition or {}).get("check") == "starts_with"}
    pick |= prgid_rules(budget_rules)
    assert sorted((f.record_id, f.rule_id) for f in worker.findings if f.rule_id in pick) == sorted(
        (f.record_id, f.rule_id) for f in here_.findings if f.rule_id in pick
    )
    assert [f.record_id for f in here_.findings if f.rule_id in pick] == ["XYZ-2028-2"]


def test_the_worker_is_given_the_registry_programs_and_the_pa_list(budget_programs, budget_rules):
    import pandas as pd

    defs = [{"field": r.field, "definition": r.definition or {}} for r in budget_rules.rules.filter(table="Measure")]
    df = pd.DataFrame({"PrgID": ["PGE_Res_HVAC", "NOPE"], "CEInputID": ["PGE-2028-1", "PGE-2028-2"]})
    got = jobs.program_list(defs, df)
    assert got and all(rows == [{"code": "PGE_Res_HVAC", "start_year": None, "end_year": None}] for rows in got.values())
    assert jobs.program_list([{"definition": {"check": "not_empty"}}], df) == {}


def budgets_today():
    from django.utils import timezone

    return timezone.localdate()

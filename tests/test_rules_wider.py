"""The wider rule language on the platform: a rule set named by a specification version may test any of its tables, and a rule's
conditions may read the linked record of another table of the filing, the program's record or who files, alone or in groups; the
rule form writes them; a claims filing is checked with them; a rule that cannot run is refused before it is saved."""

from datetime import date

import pytest
from django.urls import reverse

from cedars.modules.claims import service as claims
from cedars.services.rules.models import RuleSet
from cedars.services.submissions.models import Deadline

from .conftest import sign_in
from .test_claims import claim, claims_zip, deadline, filer, programs, row, tables, year  # noqa: F401 - fixtures
from .test_specification_filings import version_of

pytestmark = pytest.mark.django_db


@pytest.fixture
def rules():
    pytest.importorskip("recet.filing_rules", reason="the rule library runs on the pinned ReCET engine")
    from cedars.services.rules import service

    return service


@pytest.fixture
def draft(rules, cpuc_admin):
    """A draft claims rule set (CEDARS' own rules) named by a published claims version, so its tables are the version's."""
    rs = rules.import_from_engine(cpuc_admin, "claims")
    version = version_of("claims", "Claims with wider rules", rule_set=rs)
    return rs, version


def add(rules, admin, rs, table, field, rule, value="", conds=(), connector="all", groups=None, severity="reject"):
    d = rules.rule_definition(field, rule, value, conds, connector, table, groups=groups)
    return rules.add_rule(admin, rs, kind="rule", severity=severity, table=table, field=field, definition=d, reason="test")


def test_a_versions_rule_set_may_test_any_of_its_tables(rules, draft):
    rs, _version = draft
    assert set(rules.tables_for(rs)) >= {"Claim", "Measure", "ProgramCost", "Site", "Withdrawal"}
    assert "PII_Site" not in rules.tables_for(rs)  # vault tables are never in a claims file
    assert rules.layout_of(rs)["keys"]["Measure"] == ["ClaimID"]
    assert rules.condition_tables(rs)[-2:] == ["Program", "Filer"]
    loose = RuleSet.objects.create(code="other", label="Other", effective_from=date(2030, 1, 1))
    assert rules.tables_for(loose) == ["Measure", "ProgramCost"]  # a set no version names: CEDARS' two tables


def test_conditions_on_another_table_the_program_and_the_filer_and_groups(rules, draft, cpuc_admin):
    rs, _ = draft
    r = add(rules, cpuc_admin, rs, "Claim", "ParticipantID", "is not null",
            conds=[("Measure", "MeasImpactType", "in", "Cust-SEM, Cust-SEM-FuelSub", "")])  # fmt: skip
    assert r.definition["conds"] == [["Measure", "MeasImpactType", "in", "['Cust-SEM', 'Cust-SEM-FuelSub']"]]
    assert r.message.startswith("When Measure's MeasImpactType in")
    g = add(rules, cpuc_admin, rs, "Claim", "CCAREN_Flag", "is true", connector="all", groups={"1": "any"},
            conds=[("Filer", "PAType", "equals", "'CCA'", "1"), ("Filer", "PAType", "equals", "'REN'", "1"),
                   ("Claim", "ClaimType", "is not null", "", "")])  # fmt: skip
    assert g.definition["conds"][0] == {
        "connector": "any",
        "conds": [["Filer", "PAType", "equals", "'CCA'"], ["Filer", "PAType", "equals", "'REN'"]],
    }
    assert "(Filer's PAType equals 'CCA' or Filer's PAType equals 'REN')" in g.message
    p = add(rules, cpuc_admin, rs, "Measure", "NumUnits", "is greater than", "5", conds=[("Program", "PA", "equals", "'PGE'", "")])
    assert rules.columns_needed(p) == {"NumUnits"}  # a condition on the program reads no column of the file
    for bad, why in [
        (dict(table="Claim", field="ParticipantID", rule="is not null", conds=[("Nowhere", "X", "is null", "", "")]), "not a table"),
        (dict(table="Claim", field="ParticipantID", rule="is not null", conds=[("Program", "Colour", "is null", "", "")]),
         "not a field of the program"),
        (dict(table="Claim", field="ParticipantID", rule="is not null", conds=[("Filer", "Name", "is null", "", "")]), "who files"),
        (dict(table="Claim", field="InstallationDate", rule="within years of upload", value="nine"), "whole number"),
        (dict(table="Claim", field="ParticipantID", rule="is not null",
              conds=[("Claim", "InstallationDate", "within years of upload", "9", "")]), "is a test, not a condition"),
    ]:  # fmt: skip
        with pytest.raises(rules.RuleError, match=why):
            add(rules, cpuc_admin, rs, **bad)
    with pytest.raises(rules.RuleError, match="plain conditions"):
        nested = [{"connector": "any", "conds": [{"connector": "all", "conds": [["Claim", "A", "is null", ""]]}]}]
        d = {"field": "ParticipantID", "rule": "is not null", "value": "", "connector": "all", "conds": nested}
        rules.validate("rule", "reject", "Claim", "ParticipantID", d, rs)


def test_a_claims_filing_is_checked_with_them(rules, draft, cpuc_admin, filer, year, pge, programs):  # noqa: F811
    rs, version = draft
    sem = add(rules, cpuc_admin, rs, "Claim", "ParticipantID", "is not null", conds=[("Measure", "MeasImpactType", "in", "Cust-SEM", "")])
    cca = add(rules, cpuc_admin, rs, "Claim", "CCAREN_Flag", "is true", conds=[("Filer", "PAType", "in", "CCA, REN", "")])
    big = add(rules, cpuc_admin, rs, "Measure", "NumUnits", "is less than", "5", conds=[("Program", "PrgID", "equals", "'PGE_Com'", "")],
              severity="warning")  # fmt: skip
    rules.publish(cpuc_admin, rs)
    d = deadline()
    Deadline.objects.filter(pk=d.pk).update(specification_version=version)
    data = claims_zip(
        full=True,
        t=tables(
            claim("PGE-2028-001", MeasImpactType="Cust-SEM"),
            claim("PGE-2028-002", prg="PGE_Com", NumUnits="7"),
            ProgramCost=[row("ProgramCost", PrgID="PGE_Res"), row("ProgramCost", PrgID="PGE_Com")],
            Site=[row("Site", SiteID="PGE-S1")],
        ),
    )
    v = claims.upload_claims(filer, deadline(), pge, data, "q1.zip")
    found = claims.findings(v)
    by = {f["rule_id"]: f for f in found}
    assert sem.rule_id in by and by[sem.rule_id]["table"] == "Claim" and by[sem.rule_id]["record_id"] == "PGE-2028-001"
    assert [f["record_id"] for f in found if f["rule_id"] == sem.rule_id] == ["PGE-2028-001"]  # 002's measure is not SEM
    assert cca.rule_id not in by  # PG&E is an IOU
    assert [f["record_id"] for f in found if f["rule_id"] == big.rule_id] == ["PGE-2028-002"]
    assert by[big.rule_id]["severity"] == "warning"


def test_the_rule_form_writes_them(client, rules, draft, cpuc_admin):
    rs, _ = draft
    sign_in(client, cpuc_admin)
    url = reverse("rules:add_rule", args=[rs.pk])
    page = client.get(url).content.decode()
    assert 'value="Withdrawal"' in page and "Its program (the registry&#x27;s record)" in page and "Condition 1: group" in page
    post = {
        "kind": "rule", "severity": "reject", "table": "Claim", "field": "ParticipantID", "rule": "is not null", "connector": "all",
        "cond1_table": "Measure", "cond1_field": "MeasImpactType", "cond1_rule": "contains", "cond1_value": "SEM", "cond1_group": "1",
        "cond2_table": "Program", "cond2_field": "Category", "cond2_rule": "equals", "cond2_value": "'Strategic energy management'",
        "cond2_group": "1", "group1_connector": "any", "reason": "SEM claims name their participant",
    }  # fmt: skip
    r = client.post(url, post)
    assert r.status_code == 302, r.content.decode()[:3000]
    rule = rs.rules.get(table="Claim")
    assert rule.definition["conds"] == [
        {
            "connector": "any",
            "conds": [["Measure", "MeasImpactType", "contains", "SEM"], ["Program", "Category", "equals", "'Strategic energy management'"]],
        }
    ]
    page = client.get(reverse("rules:edit_rule", args=[rs.pk, rule.rule_id])).content.decode()
    assert 'name="cond2_field"' in page and "Strategic energy management" in page
    assert '<option value="1" selected>Group 1</option>' in page


def test_condition_met_reads_the_context(rules, draft, cpuc_admin):
    rs, _ = draft
    r = add(rules, cpuc_admin, rs, "Claim", "ParticipantID", "is not null", conds=[("Measure", "MeasImpactType", "contains", "SEM", "")])
    import pandas as pd

    claim_df = pd.DataFrame({"ClaimID": ["A-1"], "ParticipantID": [""]})
    ctx = rules.CheckContext(
        tables={"Measure": pd.DataFrame({"ClaimID": ["a-1"], "MeasImpactType": ["Cust-SEM"]})}, keys={"Measure": ["ClaimID"]}
    )
    assert rules.condition_met(r, claim_df, context=ctx)
    assert not rules.condition_met(r, claim_df)  # without the Measure table the condition never holds

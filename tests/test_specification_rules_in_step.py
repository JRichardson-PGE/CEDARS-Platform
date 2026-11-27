"""A specification's fields and its rule set's field constraints in step: on the tables the rules check field by field (Measure and
ProgramCost), a field's requirement, type, largest length and value list are checked by the rule set, so where the two disagree is
listed, a draft cannot be published until they agree, the fields decide when the rules are brought in line, and a field added, changed
or removed takes its field constraints with it."""

import pytest
from django.urls import reverse

from cedars.services.audit.models import AuditEvent
from cedars.services.rules import service as rules
from cedars.services.specifications import editor
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification

from .conftest import sign_in

pytestmark = pytest.mark.django_db
pytest.importorskip("recet.filing_rules", reason="the rule library runs on the pinned ReCET engine")

# Where the starting versions and CEDARS' rules disagree today (the CEDARS specification and its rule files differ). Each is the
# CPUC's to settle, by changing the field or bringing the rules in line in the next version; a new difference fails this test.
KNOWN = {
    "claims": {
        ("Measure", "ClaimID", "not_empty"), ("Measure", "ClaimYearQuarter", "year_quarter"),
        ("Measure", "ClaimYearQuarter", "max_length"), ("Measure", "DeliveryType", "max_length"),
        ("Measure", "MeasDescription", "max_length"), ("Measure", "MeasImpactType", "max_length"),
        ("Measure", "FuelID", "max_length"), ("Measure", "MeasureID", "max_length"), ("Measure", "TechGroup", "not_empty"),
        ("Measure", "TechGroup", "max_length"), ("Measure", "TechType", "max_length"), ("Measure", "UseCategory", "not_empty"),
        ("Measure", "UseCategory", "max_length"), ("Measure", "UseSubCategory", "max_length"), ("Measure", "WaterUse", "max_length"),
        ("Measure", "PreDesc", "max_length"), ("Measure", "StdDesc", "max_length"), ("Measure", "SourceDesc", "max_length"),
        ("Measure", "Version", "max_length"), ("Measure", "UnitkWhIOUWater1stBaseline", "not_empty"),
        ("Measure", "UnitkWhIOUWater2ndBaseline", "not_empty"), ("Measure", "NTG_ID", "max_length"),
        ("Measure", "EUL_ID", "max_length"), ("Measure", "RUL_ID", "max_length"), ("Measure", "GSIA_ID", "max_length"),
        ("Measure", "MiscCostsDesc", "max_length"), ("Measure", "MiscBensDesc", "max_length"), ("Measure", "Comments", "max_length"),
        ("ProgramCost", "PrgYear", "numeric"), ("ProgramCost", "PrgYear", "max_length"),
        ("ProgramCost", "ClaimYearQuarter", "year_quarter"), ("ProgramCost", "ClaimYearQuarter", "max_length"),
    },
    "budget_filing": {
        ("Measure", "CEInputID", "not_empty"), ("Measure", "Sector", "not_empty"), ("Measure", "DeliveryType", "not_empty"),
        ("Measure", "BldgType", "not_empty"), ("Measure", "AC_Gas_Impact_Profile", "not_empty"),
        ("Measure", "AC_Gas_Target_Sector", "not_empty"), ("Measure", "AC_Elec_Impact_Profile", "not_empty"),
        ("Measure", "AC_Elec_Target_Sector", "not_empty"), ("Measure", "MeasAppType", "not_empty"),
        ("Measure", "MeasDescription", "not_empty"), ("Measure", "MeasImpactType", "not_empty"), ("Measure", "TechType", "not_empty"),
        ("Measure", "UseSubCategory", "not_empty"), ("Measure", "NormUnit", "not_empty"),
        ("Measure", "UnitDirectInstallLab", "not_empty"), ("Measure", "UnitDirectInstallMat", "not_empty"),
        ("Measure", "UnitEndUserRebate", "not_empty"), ("Measure", "UnitIncentiveToOthers", "not_empty"),
        ("Measure", "UnitTaxCredits", "not_empty"), ("Measure", "MeasInflation", "not_empty"),
        ("ProgramCost", "ClaimYearQuarter", "not_empty"), ("ProgramCost", "AdminCostsOverheadAndGA", "not_empty"),
        ("ProgramCost", "AdminCostsOther", "not_empty"), ("ProgramCost", "MarketingOutreach", "not_empty"),
        ("ProgramCost", "DIActivity", "not_empty"), ("ProgramCost", "DIInstallation", "not_empty"),
        ("ProgramCost", "DIHardwareAndMaterials", "not_empty"), ("ProgramCost", "DIRebateAndInspection", "not_empty"),
        ("ProgramCost", "EMV", "not_empty"), ("ProgramCost", "UserInputIncentive", "not_empty"),
        ("ProgramCost", "OnBillFinancing", "not_empty"), ("ProgramCost", "CostsRecoveredFromOtherSources", "not_empty"),
    },
}  # fmt: skip


def current(code: str):
    return specifications.current(Specification.objects.get(code=code))


def field(v, table: str, name: str):
    return v.tables.get(name=table).fields.get(name=name)


def found(v, table: str, name: str) -> dict[str, list[dict]]:
    """The draft's field constraints on a field, by check."""
    out: dict[str, list[dict]] = {}
    for r in v.rule_set.rules.filter(kind="spec", table=table):
        if rules.canonical_field(r.field) == name:
            out.setdefault(r.definition["check"], []).append(r.definition)
    return out


@pytest.fixture
def claims_rules(cpuc_admin):
    return rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "claims"))


@pytest.fixture
def draft(cpuc_admin, claims_rules):
    return editor.start_draft(cpuc_admin, current("claims"), "Claims 2029", "The 2029 claim changes")


@pytest.mark.parametrize("code", ["claims", "budget_filing"])
def test_the_starting_versions_differ_from_cedars_rules_only_where_known(cpuc_admin, code):
    rs = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, code))
    got = {(d.table, d.field, d.check) for d in specifications.rule_differences(current(code), rs)}
    assert got == KNOWN[code]


def test_a_draft_copies_the_rules_its_version_is_checked_against(draft, claims_rules):
    assert current("claims").rule_set_id is None  # a starting version names none: its filings take the claims rules in force
    assert draft.rule_set.based_on == claims_rules and draft.rule_set.status == "draft"


def test_a_draft_whose_fields_and_rules_disagree_cannot_be_published_until_brought_in_line(cpuc_admin, draft):
    texts = [p.text for p in editor.problems(draft) if p.part == "rules"]
    assert len(texts) == len(KNOWN["claims"])
    assert "The field says Measure.DeliveryType should be at most 255 characters; the rule set checks it is at most 20 characters." in (
        " ".join(texts)
    )
    with pytest.raises(editor.EditError, match="Say why"):
        editor.align_rules(cpuc_admin, draft, "")
    added, removed = editor.align_rules(cpuc_admin, draft, "The specification decides")
    assert specifications.rule_differences(draft, draft.rule_set) == []
    assert not [p for p in editor.problems(draft) if p.part == "rules"]
    assert found(draft, "Measure", "DeliveryType")["max_length"] == [{"check": "max_length", "n": 255}]
    assert found(draft, "Measure", "TechGroup")["not_empty"] == [{"check": "not_empty"}]
    assert "max_length" not in found(draft, "Measure", "MeasDescription")  # the field gives no largest length
    assert found(draft, "ProgramCost", "PrgYear").get("numeric") == [{"check": "numeric"}]
    assert (len(added), len(removed)) == (22, 24)  # 32 differences: 14 rules changed, 8 added only, 10 taken out only
    event = AuditEvent.objects.filter(action="specification.rules_aligned").get()
    assert event.reason == "The specification decides" and len(event.after["rules"]) == 22
    # the published claims rules, and the filings checked against them, do not change
    assert specifications.rule_differences(current("claims"))


def test_a_rule_brought_in_line_is_checked(cpuc_admin, draft):
    import pandas as pd

    editor.align_rules(cpuc_admin, draft, "The specification decides")
    rs = draft.rule_set
    cols = rules.spec_columns(rs, "Measure")
    df = pd.DataFrame([dict.fromkeys(cols, "") | {"ClaimID": "C1", "ClaimYearQuarter": "2028Q1", "TechGroup": ""}]).astype(str)
    found_, _notes = rules.check(rs, "Measure", df, filing_year=2028)
    assert "TechGroup should not be empty" in {f.message for f in found_ if f.field == "TechGroup"}


def test_a_changed_field_takes_its_field_constraints_with_it(cpuc_admin, draft):
    before = {(d.table, d.field, d.check) for d in specifications.rule_differences(draft, draft.rule_set)}
    editor.change_field(cpuc_admin, field(draft, "Measure", "DeliveryType"), max_length=30, reason="A shorter code")
    assert found(draft, "Measure", "DeliveryType")["max_length"] == [{"check": "max_length", "n": 30}]
    editor.change_field(cpuc_admin, field(draft, "Measure", "Sector"), requirement="optional", reason="Not every measure has one")
    assert "not_empty" not in found(draft, "Measure", "Sector")
    editor.change_field(cpuc_admin, field(draft, "Measure", "TechGroup"), guidance="Use the eTRM's technology group.", reason="Clearer")
    after = {(d.table, d.field, d.check) for d in specifications.rule_differences(draft, draft.rule_set)}
    assert after == before - {("Measure", "DeliveryType", "max_length")}  # a change of guidance leaves the rules alone


def test_a_field_added_or_removed_takes_its_field_constraints_with_it(cpuc_admin, draft):
    f = editor.add_field(cpuc_admin, draft, "Measure", "UnitWaterGallons", type="number", requirement="required",
                         definition="Water saved per unit, in gallons", reason="Water savings")  # fmt: skip
    assert set(found(draft, "Measure", "UnitWaterGallons")) == {"numeric", "not_empty"}
    editor.remove_field(cpuc_admin, f, reason="Not this year")
    assert found(draft, "Measure", "UnitWaterGallons") == {}
    editor.remove_field(cpuc_admin, field(draft, "Measure", "Comments"), reason="Not used")
    assert found(draft, "Measure", "Comments") == {}


def test_the_rules_page_lists_the_differences_and_brings_the_rules_in_line(cpuc_admin, draft, client):
    sign_in(client, cpuc_admin)
    url = reverse("specifications:edit_rules", args=["claims", draft.slug])
    page = client.get(url).content.decode()
    assert "The fields and the rules disagree in 32 places." in page and 'id="align-form"' in page
    align = reverse("specifications:align_rules", args=["claims", draft.slug])
    r = client.post(align, {"reason": ""}, follow=True)
    assert "Say why the rules are brought in line" in r.content.decode()
    r = client.post(align, {"reason": "The specification decides"}, follow=True)
    page = r.content.decode()
    assert "The rules follow the fields: 22 added, 24 taken out." in page and "The fields and the rules agree." in page

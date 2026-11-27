"""Sets of rules kept as data (rules.packs): CEDARS' 2026 claims data rules on measures, added to a claims draft's rules in one step from
its Rules tab. Adding the set takes out the rules of CEDARS' files it replaces and adds its own, each audited; the rules then check a
filing like any other; the published rules do not change."""

import pandas as pd
import pytest
from django.urls import reverse

from cedars.services.audit.models import AuditEvent
from cedars.services.rules import packs
from cedars.services.rules import service as rules
from cedars.services.specifications import editor
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification

from .conftest import sign_in

pytestmark = pytest.mark.django_db
pytest.importorskip("recet.filing_rules", reason="the rule library runs on the pinned ReCET engine")

SLUG = "claims-cedars-2026"


@pytest.fixture
def claims_rules(cpuc_admin):
    return rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "claims"))


@pytest.fixture
def draft(cpuc_admin, claims_rules):
    v = specifications.current(Specification.objects.get(code="claims"))
    return editor.start_draft(cpuc_admin, v, "Claims 2029", "The 2029 claim changes")


def test_every_pack_is_well_formed_and_its_rules_are_valid(draft):
    assert packs.packs(), "the packs folder is empty"
    for p in packs.packs():
        assert p.name and p.about and p.code and p.rules
        ids = [packs.rule_id_of(d) for d in p.rules]
        assert len(set(ids)) == len(ids), f"{p.slug} repeats a rule"
        assert not set(ids) & set(p.replaces), f"{p.slug} replaces a rule it adds"
        for d in p.rules:
            assert d["source"], f"{p.slug}: every rule says where it comes from"
            if p.code == "claims":  # a claims draft's layout holds the Claim table its conditions read
                rules.validate("rule", d["severity"], d["table"], d["field"], {**d["definition"], "field": d["field"]}, draft.rule_set)


def test_the_2026_claims_pack_replaces_three_of_cedars_rules_and_adds_fourteen(cpuc_admin, draft, claims_rules):
    p = packs.pack(SLUG)
    st = editor.rule_packs(draft)[0]
    assert st.pack == p and not st.added
    assert st.replaced == sorted(p.replaces) == ["V398e39", "Vc9f441", "Vefbcee"]  # all three are in CEDARS' claims rules
    assert len(st.missing) == len(p.rules) == 14
    with pytest.raises(editor.EditError, match="Say why"):
        editor.add_rule_pack(cpuc_admin, draft, SLUG, "")
    with pytest.raises(editor.EditError, match="no such set"):
        editor.add_rule_pack(cpuc_admin, draft, "budget-nothing", "CEDARS' 2026 rules")
    before = draft.rule_set.rules.count()
    editor.add_rule_pack(cpuc_admin, draft, SLUG, "CEDARS' 2026 claims data rules")
    have = set(draft.rule_set.rules.values_list("rule_id", flat=True))
    assert not have & set(p.replaces) and {packs.rule_id_of(d) for d in p.rules} <= have
    assert draft.rule_set.rules.count() == before - 3 + 14
    assert editor.rule_packs(draft)[0].added
    assert AuditEvent.objects.filter(action="rule_set.pack_added").get().after == {"pack": SLUG, "added": 14, "removed": st.replaced}
    assert AuditEvent.objects.filter(action="rule.added").count() == 14
    assert AuditEvent.objects.filter(action="rule.removed").count() == 3
    ntg = draft.rule_set.rules.get(rule_id=packs.rule_id_of(next(d for d in p.rules if d["field"] == "NTG_ID")))
    assert ntg.source == "CEDARS' 2026 claims data rules, line 45"
    # the published rules, which filings on the current version are checked against, do not change
    assert set(p.replaces) <= set(claims_rules.rules.values_list("rule_id", flat=True))
    # adding it again changes nothing
    editor.add_rule_pack(cpuc_admin, draft, SLUG, "Again")
    assert draft.rule_set.rules.count() == before - 3 + 14


def test_the_pack_s_rules_check_a_claim(cpuc_admin, draft):
    editor.add_rule_pack(cpuc_admin, draft, SLUG, "CEDARS' 2026 claims data rules")
    rs = draft.rule_set
    cols = rules.spec_columns(rs, "Measure")
    row = dict.fromkeys(cols, "") | {"ClaimID": "C1", "ClaimYearQuarter": "2028Q1", "DeliveryType": "DnDeemDI", "IOU_AC_Territory": "XYZ"}
    found, _notes = rules.check(rs, "Measure", pd.DataFrame([row]).astype(str), filing_year=2028)
    fields = {f.field for f in found}
    assert {"IOU_AC_Territory", "NTG_ID"} <= fields  # territory not an IOU; NTG_ID blank outside codes and standards
    row |= {"DeliveryType": "C&S", "IOU_AC_Territory": "PGE"}
    found, _notes = rules.check(rs, "Measure", pd.DataFrame([row]).astype(str), filing_year=2028)
    assert not {"IOU_AC_Territory", "NTG_ID"} & {f.field for f in found}


def test_the_rules_tab_adds_the_set(cpuc_admin, draft, client):
    sign_in(client, cpuc_admin)
    page = client.get(reverse("specifications:edit_rules", args=["claims", draft.slug])).content.decode()
    assert 'id="rule-packs"' in page and "Adding it adds 14 rules and takes out V398e39, Vc9f441, Vefbcee" in page
    url = reverse("specifications:add_rule_pack", args=["claims", draft.slug])
    r = client.post(url, {"pack": SLUG, "reason": ""}, follow=True)
    assert "Say why the rules are added" in r.content.decode()
    r = client.post(url, {"pack": SLUG, "reason": "CEDARS' 2026 claims data rules"}, follow=True)
    page = r.content.decode()
    assert "14 rules added, 3 replaced." in page and "The draft has these rules." in page


def test_a_budget_draft_offers_no_claims_pack(cpuc_admin):
    rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "budget_filing"))
    v = specifications.current(Specification.objects.get(code="budget_filing"))
    d = editor.start_draft(cpuc_admin, v, "Budget 2029", "The 2029 budget changes")
    assert all(st.pack.code == "budget_filing" for st in editor.rule_packs(d))

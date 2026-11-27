"""Sets of value lists kept as data (specifications.list_packs): CEDARS' 2026 claims value lists, added to a claims draft in one step
from its Tables and fields page. Each list is made in the data dictionary as a list made in the draft, its field takes its codes from
it, each change is audited, and the claims module's checks then hold the field to it."""

import csv
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.services.audit.models import AuditEvent
from cedars.services.specifications import editor, examples, list_packs
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification

from .conftest import sign_in

pytestmark = pytest.mark.django_db
SLUG = "claims-cedars-2026"
FIELDS = ["Claim.FundingCycle", "Claim.BldgLoc", "Claim.BldgVint", "Claim.BldgHVAC", "Claim.DEER_Sector", "Claim.DEER_MeasureID",
          "Claim.MeasImpactCalcType", "Claim.SEM_Phase", "Participant.ParticipantType"]  # fmt: skip


def current(code: str):
    return specifications.current(Specification.objects.get(code=code))


@pytest.fixture
def draft(cpuc_admin):
    return editor.start_draft(cpuc_admin, current("claims"), "Claims 2029", "The 2029 claim changes")


def test_every_list_of_a_set_is_well_formed():
    assert list_packs.packs(), "the list_packs folder is empty"
    for p in list_packs.packs():
        assert p.name and p.about and p.code and p.lists
        assert len({x.name for x in p.lists}) == len(p.lists) == len({(x.table, x.field) for x in p.lists})
        for x in p.lists:
            with (list_packs.FOLDER / p.slug / x.file).open(encoding="utf-8") as f:
                reader = csv.DictReader(f)
                assert reader.fieldnames == ["code", "description", "start_year", "end_year"], x.file
                rows = list(reader)
            assert rows and all(r["code"] and r["code"] == r["code"].strip() for r in rows), x.file
            assert len({r["code"].lower() for r in rows}) == len(rows), f"{x.file} repeats a code"
            for r in rows:
                start, end = (int(r[k]) if r[k] else None for k in ("start_year", "end_year"))
                assert start is None or end is None or start <= end, (x.file, r["code"])


def test_the_2026_claims_set_holds_the_fixed_lists_only():
    p = list_packs.pack(SLUG)
    assert [f"{x.table}.{x.field}" for x in p.lists] == FIELDS
    # the CMPA tool's lists change as projects go into it outside CEDARS, so they are never kept as fixed data
    assert not {"CMPA_ApplicationID", "CMPA_MeasureCode"} & {x.field for x in p.lists}
    assert not list((list_packs.FOLDER / SLUG).glob("*cmpa*")) and not list((list_packs.FOLDER / SLUG).glob("*application*"))
    assert p.lists[5].codes(SLUG) == 5277  # DEER_MeasureID
    # the starting claims version gives none of these fields a list
    v = current("claims")
    lists = {f"{t.name}.{f.name}": f.value_list for t in v.tables.all() for f in t.fields.all()}
    assert all(lists[name] == "" for name in FIELDS)


def test_adding_the_set_makes_each_list_in_the_draft(cpuc_admin, draft):
    st = editor.value_list_packs(draft)[0]
    assert st.pack.slug == SLUG and not st.added and len(st.missing) == 9 and not st.absent
    with pytest.raises(editor.EditError, match="Say why"):
        editor.add_value_list_pack(cpuc_admin, draft, SLUG, "")
    with pytest.raises(editor.EditError, match="no such set"):
        editor.add_value_list_pack(cpuc_admin, draft, "budget-nothing", "CEDARS' 2026 lists")
    editor.add_value_list_pack(cpuc_admin, draft, SLUG, "CEDARS' 2026 claims value lists")
    v = draft
    named = {f"{t.name}.{f.name}": f.value_list for t in v.tables.all() for f in t.fields.all()}
    assert [named[n] for n in FIELDS] == [x.name for x in list_packs.pack(SLUG).lists]
    made = {m.value_list.name: m for m in editor.made_lists(v)}
    assert set(made) == {x.name for x in list_packs.pack(SLUG).lists}
    assert made["deer_measure_id"].codes == 5277 and made["funding_cycle"].fields == ["Claim.FundingCycle"]
    assert editor.value_list_packs(v)[0].added
    e = AuditEvent.objects.get(action="specification.value_list_pack_added")
    assert e.after["pack"] == SLUG and len(e.after["made"]) == 9 and e.after["named"] == []
    assert AuditEvent.objects.filter(action="specification.value_list_made").count() == 9
    # adding it again changes nothing
    editor.add_value_list_pack(cpuc_admin, v, SLUG, "Again")
    assert AuditEvent.objects.filter(action="specification.value_list_pack_added").count() == 1


def test_the_claims_checks_hold_a_field_to_its_new_list(cpuc_admin, draft):
    editor.add_value_list_pack(cpuc_admin, draft, SLUG, "CEDARS' 2026 claims value lists")
    e = examples.generate(cpuc_admin, draft, "Written for the lists")
    claim = e.rows["Claim"][0]
    assert claim["FundingCycle"] and claim["DEER_Sector"] and not examples.rejects(e.report)  # required: a code of its new list
    rows = e.rows
    rows["Claim"][0]["FundingCycle"] = "1999-2001"
    examples.replace_rows(cpuc_admin, e, rows, "A cycle CEDARS does not list")
    e.refresh_from_db()
    bad = examples.rejects(e.report)
    assert [(f["table"], f["field"]) for f in bad] == [("Claim", "FundingCycle")] and "funding_cycle" in bad[0]["message"]
    examples.generate(cpuc_admin, draft, "Valid again")
    v = editor.publish(cpuc_admin, draft, effective_on=timezone.localdate() + timedelta(days=20))
    # published with the version: the module's list check (CLM-LIST) reads them, each code in its own years
    retired = "C-In-LFLmpBlst-T8-48in-30w+El-IS-RLO(45w)-dWP7-dWC3"  # DEER_MeasureID in force in 2018 only
    records = [{"FundingCycle": "1999-2001"}, {"FundingCycle": "2010-2012", "DEER_MeasureID": retired}]
    found = specifications.list_problems(specifications.layout(v), "Claim", records, year=2026)
    assert [(p["index"], p["field"], p["value"]) for p in found] == [(0, "FundingCycle", "1999-2001"), (1, "DEER_MeasureID", retired)]
    assert specifications.list_problems(specifications.layout(v), "Claim", [{"DEER_MeasureID": retired}], year=2018) == []


def test_a_budget_draft_offers_no_claims_lists(cpuc_admin):
    d = editor.start_draft(cpuc_admin, current("budget_filing"), "Budget 2029", "The 2029 budget changes")
    assert editor.value_list_packs(d) == []
    with pytest.raises(editor.EditError, match="no such set"):
        editor.add_value_list_pack(cpuc_admin, d, SLUG, "Wrong kind of filing")


def test_the_fields_page_adds_the_set(cpuc_admin, draft, client):
    sign_in(client, cpuc_admin)
    url = reverse("specifications:edit", args=["claims", draft.slug])
    page = client.get(url).content.decode()
    assert 'id="list-packs"' in page and "Adding it gives 9 fields a value list" in page and "Claim.FundingCycle (funding_cycle)" in page
    post = reverse("specifications:add_value_list_pack", args=["claims", draft.slug])
    r = client.post(post, {"pack": SLUG, "reason": ""}, follow=True)
    assert "Say why the value lists are added" in r.content.decode()
    r = client.post(post, {"pack": SLUG, "reason": "CEDARS' 2026 claims value lists"}, follow=True)
    page = r.content.decode()
    assert "9 value lists given to the draft" in page and "The draft's fields take their codes from these lists." in page

"""New value lists made in a draft specification version: a new field's new codes, in a list the data dictionary keeps and the engine
does not have. It is published with the version, dropped with the draft, and the filing modules check the field against it."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.modules.budgets import service as budgets
from cedars.modules.claims import service as claims
from cedars.modules.claims import spec as claims_spec
from cedars.modules.claims import upload as claims_upload
from cedars.services.audit.models import AuditEvent
from cedars.services.dictionary import value_lists
from cedars.services.dictionary.models import ValueList
from cedars.services.specifications import editor
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification
from cedars.services.submissions.models import Deadline

from .conftest import sign_in
from .test_monthly import COM, filer, month, programs, report, year  # noqa: F401 - fixtures
from .test_monthly import service as monthly

pytestmark = pytest.mark.django_db


def current(code: str):
    return specifications.current(Specification.objects.get(code=code))


def field(v, table: str, name: str):
    return v.tables.get(name=table).fields.get(name=name)


def soon(days: int):
    return timezone.localdate() + timedelta(days=days)


@pytest.fixture
def draft(cpuc_admin):
    return editor.start_draft(cpuc_admin, current("monthly"), "Monthly reports 2029", "The 2029 reporting changes")


CODES = 'A, Alpha\nB, "Beta, the second", 2028, 2030\n'


def test_a_new_list_is_made_in_the_draft_and_named_by_the_field(cpuc_admin, draft):
    vl = editor.new_value_list(cpuc_admin, field(draft, "Measure", "PreDesc"), "pre_desc_kind", codes=CODES, reason="New codes")
    assert vl.made_for == "Monthly reports: Monthly reports 2029" and vl.label == "PreDesc"
    assert field(draft, "Measure", "PreDesc").value_list == "pre_desc_kind"
    d = value_lists.draft_of(vl)
    assert [(c.code, c.description, c.start_year, c.end_year) for c in d.codes.order_by("code")] == [
        ("A", "Alpha", None, None),
        ("B", "Beta, the second", 2028, 2030),
    ]
    assert value_lists.made_here("pre_desc_kind") and "pre_desc_kind" not in value_lists.engine_changes()  # never handed to the engine
    assert AuditEvent.objects.filter(action="specification.value_list_made").exists()
    assert AuditEvent.objects.filter(action="value_list.created").exists()
    [m] = editor.made_lists(draft)
    assert m.value_list == vl and m.codes == 2 and m.fields == ["Measure.PreDesc"]
    assert "New value list pre_desc_kind (2 codes) for Measure.PreDesc: a value not in it is rejected." in editor.change_lines(draft)
    editor.update_settings(cpuc_admin, draft, name="Monthly reports 2030", summary="s", engine_release="", reason="renamed")
    vl.refresh_from_db()
    assert vl.made_for == "Monthly reports: Monthly reports 2030"


def test_what_a_new_list_refuses(cpuc_admin, draft):
    f = field(draft, "Measure", "PreDesc")
    with pytest.raises(editor.EditError, match="locked"):
        editor.new_value_list(cpuc_admin, field(draft, "Measure", "PrgID"), "programs_new", codes="A", reason="why")
    with pytest.raises(editor.EditError, match="lower-case letters"):
        editor.new_value_list(cpuc_admin, f, "Bad Name", codes="A", reason="why")
    with pytest.raises(editor.EditError, match="already a value list called sector"):
        editor.new_value_list(cpuc_admin, f, "sector", codes="A", reason="why")
    with pytest.raises(editor.EditError, match="Say why"):
        editor.new_value_list(cpuc_admin, f, "pre_desc_kind", codes="A", reason="")
    with pytest.raises(editor.EditError, match="Line 1 has more than four parts"):
        editor.new_value_list(cpuc_admin, f, "pre_desc_kind", codes="A, b, c, 2028, 2029", reason="why")
    with pytest.raises(editor.EditError, match="Line 2: a is on line 1 too"):
        editor.new_value_list(cpuc_admin, f, "pre_desc_kind", codes="A\na", reason="why")
    assert not ValueList.objects.filter(name="pre_desc_kind").exists()  # nothing is kept from a refused list


def test_its_first_version_waits_for_the_specification(cpuc_admin, draft):
    vl = editor.new_value_list(cpuc_admin, field(draft, "Measure", "PreDesc"), "pre_desc_kind", reason="New codes")
    d = value_lists.draft_of(vl)
    with pytest.raises(value_lists.ValueListError, match="published with that specification version"):
        value_lists.publish(cpuc_admin, d, soon(20), "on its own")
    with pytest.raises(value_lists.ValueListError, match="published with that specification version"):
        value_lists.discard_draft(cpuc_admin, d, "on its own")
    assert any("pre_desc_kind" in p.text and "no codes yet" in p.text for p in editor.problems(draft))
    value_lists.add_code(cpuc_admin, d, "A", reason="the first code")
    assert not any("pre_desc_kind" in p.text for p in editor.problems(draft))
    assert value_lists.codes_for_check("pre_desc_kind") is None  # nothing published yet


def test_publishing_publishes_the_list_and_drops_one_no_field_names(cpuc_admin, draft):
    editor.new_value_list(cpuc_admin, field(draft, "Measure", "PreDesc"), "pre_desc_kind", codes=CODES, reason="New codes")
    editor.new_value_list(cpuc_admin, field(draft, "Measure", "StdDesc"), "std_desc_kind", codes="X", reason="New codes")
    editor.change_field(cpuc_admin, field(draft, "Measure", "StdDesc"), value_list="", reason="Not needed after all")
    editor.publish(cpuc_admin, draft, effective_on=soon(20))
    vl = ValueList.objects.get(name="pre_desc_kind")
    v = value_lists.latest_published(vl)
    assert v.number == 1 and v.effective_from == soon(20) and "Monthly reports 2029" in v.reason
    assert {c["code"] for c in value_lists.codes_for_check("pre_desc_kind")} == {"A", "B"}  # before it takes effect: its version
    assert not ValueList.objects.filter(name="std_desc_kind").exists()
    page = value_lists.start_draft(cpuc_admin, vl)  # later versions are kept on the value-list pages, as any list's
    assert page.codes.count() == 2


def test_discarding_the_draft_drops_its_lists(cpuc_admin, draft):
    editor.new_value_list(cpuc_admin, field(draft, "Measure", "PreDesc"), "pre_desc_kind", codes=CODES, reason="New codes")
    editor.discard(cpuc_admin, draft, "Not this year")
    assert not ValueList.objects.filter(name="pre_desc_kind").exists()
    assert AuditEvent.objects.filter(action="value_list.deleted").exists()


def test_a_field_is_checked_against_its_list_in_the_records_year(cpuc_admin, draft):
    editor.new_value_list(cpuc_admin, field(draft, "Measure", "PreDesc"), "pre_desc_kind", codes=CODES, reason="New codes")
    v = editor.publish(cpuc_admin, draft, effective_on=soon(20))
    lay = specifications.layout(v)
    rows = [{"PreDesc": "a"}, {"PreDesc": "Z"}, {"PreDesc": "B"}, {"PreDesc": ""}]
    found = specifications.list_problems(lay, "Measure", rows, year=2027)
    assert [(p["index"], p["value"]) for p in found] == [(1, "Z"), (2, "B")]  # B is accepted from 2028; codes match ignoring case
    assert found[0]["message"] == "PreDesc Z is not a code of the pre_desc_kind value list accepted for 2027"
    assert [p["index"] for p in specifications.list_problems(lay, "Measure", rows, year=2029)] == [1]
    assert specifications.list_problems(lay, "ProgramCost", [{"PrgID": "x"}], year=2029) == []


def test_a_monthly_report_is_checked_against_a_new_list(cpuc_admin, draft, filer, year, pge, programs):  # noqa: F811
    editor.add_field(cpuc_admin, draft, "Commitments", "DeliveryChannel", definition="How the program reaches customers", reason="why")
    editor.new_value_list(cpuc_admin, field(draft, "Commitments", "DeliveryChannel"), "delivery_channel", codes="Direct\nUpstream",
                          reason="New codes")  # fmt: skip
    v = editor.publish(cpuc_admin, draft, effective_on=soon(20))
    Deadline.objects.filter(pk=month(2).pk).update(specification_version=v)
    row = "PGE|PGE_Res_HVAC|202802|0|0|5000|100|1|0|80|1|0|waiting on permits|{}"
    data = report("202802", commitments=[row.format("Midstream")], com_header=COM + "|DeliveryChannel")
    s = monthly.upload(filer, month(2), pge, data, "february.zip")
    found = [f for f in monthly.findings(s) if f["rule_id"] == "MON-LIST"]
    assert [(f["table"], f["field"], f["value"], f["severity"]) for f in found] == [
        ("Commitments", "DeliveryChannel", "Midstream", "reject")
    ]
    data = report("202802", commitments=[row.format("upstream")], com_header=COM + "|DeliveryChannel")
    s = monthly.upload(filer, month(2), pge, data, "february.zip")
    assert "MON-LIST" not in {f["rule_id"] for f in monthly.findings(s)}


def test_a_budget_filing_checks_each_record_in_its_own_year(cpuc_admin):
    d = editor.start_draft(cpuc_admin, current("budget_filing"), "Budget filings 2030", "why")
    editor.new_value_list(cpuc_admin, field(d, "Measure", "PreDesc"), "pre_desc_kind", codes=CODES, reason="New codes")
    v = editor.publish(cpuc_admin, d, effective_on=soon(20))
    filing = SimpleNamespace(specification_version_id=v.pk, specification_version=v, module="budgets")
    measure = b"CEInputID|PrgID|ClaimYearQuarter|PreDesc\nM1|P|2027Q1|B\nM2|P|2029Q1|B\nM3|P|2029Q1|Q\n"
    found = budgets.list_problems({"Measure": measure, "ProgramCost": b"PrgID|PrgYear\nP|2029\n"}, filing)
    # B is accepted from 2028 on, Q never
    assert [(p.row, p.record_id, p.rule_id, p.value) for p in found] == [(2, "M1", "BUD-LIST", "B"), (4, "M3", "BUD-LIST", "Q")]


def test_a_claim_is_checked_against_a_new_list(cpuc_admin, pge):
    d = editor.start_draft(cpuc_admin, current("claims"), "Claims 2030", "why")
    lock = editor.locks(d)
    f = next(f for f in d.tables.get(name="Claim").fields.all() if ("Claim", f.name) not in lock and f.type == "text" and not f.value_list)
    editor.new_value_list(cpuc_admin, f, "claim_kind", codes="K1\nK2", reason="New codes")
    v = editor.publish(cpuc_admin, d, effective_on=soon(20))
    lay = claims_spec.Layout(specifications.layout(v))
    columns = lay.columns("Claim")
    row = {c: "" for c in columns} | {"ClaimID": "PGE-2028-1", f.name: "K3"}
    own = {"Claim": claims_upload.Table("Claim", columns, [row, {**row, "ClaimID": "PGE-2028-2", f.name: "k2"}])}
    found = [p for p in claims.derive_checks(own, own, pge, 2028, 4, lay=lay) if p.rule_id == "CLM-LIST"]
    assert [(p.row, p.field, p.value) for p in found] == [(2, f.name, "K3")]


def test_the_new_list_pages(client, cpuc_admin, draft):
    sign_in(client, cpuc_admin)
    url = reverse("specifications:edit_field", args=["monthly", draft.slug, "Measure", "PreDesc"])
    page = client.get(url).content.decode()
    assert 'id="new-value-list"' in page and 'name="codes"' in page
    locked = client.get(reverse("specifications:edit_field", args=["monthly", draft.slug, "Measure", "PrgID"])).content.decode()
    assert 'id="new-value-list"' not in locked
    post = reverse("specifications:new_value_list", args=["monthly", draft.slug, "Measure", "PreDesc"])
    r = client.post(post, {"name": "pre_desc_kind", "codes": CODES, "reason": ""})
    assert r.status_code == 302 and not ValueList.objects.filter(name="pre_desc_kind").exists()
    r = client.post(post, {"name": "pre_desc_kind", "codes": CODES, "reason": "New codes"})
    assert r.status_code == 302 and r.url.endswith("#made-lists")
    fields = client.get(reverse("specifications:edit", args=["monthly", draft.slug])).content.decode()
    assert 'id="made-lists"' in fields and "for Measure.PreDesc" in fields
    assert 'id="field-list"' in client.get(url).content.decode()
    list_draft = client.get(reverse("dictionary:draft", args=["pre_desc_kind"])).content.decode()
    assert 'id="made-for"' in list_draft and 'action="/dictionary/lists/pre_desc_kind/publish' not in list_draft
    lists = client.get(reverse("dictionary:lists")).content.decode()
    assert "None yet: made for Monthly reports: Monthly reports 2029" in lists
    shown = client.get(reverse("dictionary:list", args=["pre_desc_kind"])).content.decode()
    assert "the new list made for Monthly reports: Monthly reports 2029" in shown

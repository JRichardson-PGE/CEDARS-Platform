"""The impact test: a draft is run against the latest confirmed filings, each checked under the version it was checked against and under
the draft, and the report says per organization and per rule what would newly be rejected or warned, what would pass, and which
required fields the filings lack. Nothing it tests changes; publishing keeps the test of the draft as published."""

import pytest
from django.urls import reverse

from cedars.services.audit.models import AuditEvent
from cedars.services.specifications import editor, impact
from cedars.services.submissions.models import SubmissionVersion

from .conftest import sign_in
from .test_monthly import COM, filer, month, programs, report, year  # noqa: F401 - fixtures
from .test_monthly import service as monthly
from .test_specification_examples import current, field, soon

pytestmark = pytest.mark.django_db
ROW = "PGE|PGE_Res_HVAC|{ym}|0|0|5000|100|1|0|80|1|0|waiting on permits|{channel}"


def confirmed(filer, pge, n=1, channel="Midstream"):  # noqa: F811
    """PGE's report for month `n` of 2028, with a DeliveryChannel column the version does not have, confirmed (nothing to price)."""
    ym = f"2028{n:02d}"
    data = report(ym, commitments=[ROW.format(ym=ym, channel=channel)], measures=[], costs=[], com_header=COM + "|DeliveryChannel")
    v = monthly.upload(filer, month(n), pge, data, "r.zip")
    monthly.price(filer, v)
    v.refresh_from_db()
    return monthly.confirm(filer, v)


@pytest.fixture
def draft(cpuc_admin):
    """A monthly draft that adds DeliveryChannel with a new list of two codes, and a new required field."""
    d = editor.start_draft(cpuc_admin, current("monthly"), "Monthly reports 2029", "The 2029 reporting changes")
    editor.add_field(cpuc_admin, d, "Commitments", "DeliveryChannel", definition="How the program reaches customers", reason="why")
    editor.new_value_list(cpuc_admin, field(d, "Commitments", "DeliveryChannel"), "delivery_channel", codes="Direct\nUpstream",
                          reason="New codes")  # fmt: skip
    editor.add_field(cpuc_admin, d, "Commitments", "ReportingNote", requirement="required", definition="A note", reason="why")
    return d


def test_each_pas_latest_confirmed_filing_is_checked_under_its_version_and_the_draft(cpuc_admin, draft, filer, year, pge, programs):  # noqa: F811
    confirmed(filer, pge, 1)
    latest = confirmed(filer, pge, 2)
    monthly.upload(filer, month(3), pge, report("202803"), "march.zip")  # validated, not confirmed: not tested
    assert impact.filings(draft) == [latest]
    test = impact.run(cpuc_admin, draft)
    r = test.report
    [line] = r["filings"]
    assert line["pa"] == "PGE" and line["submission_version"] == latest.pk and line["checked_with"] == current("monthly").name
    assert line["before"] == {"rejected": 0, "warned": 0} and line["after"]["rejected"] == 1
    assert (line["newly_rejected"], line["newly_warned"], line["cleared"]) == (1, 0, 0)
    assert line["absent"] == ["Commitments.ReportingNote"] and not line["note"]
    [rule] = r["rules"]
    assert (rule["rule_id"], rule["severity"], rule["field"], rule["records"], rule["by_pa"]) == (
        "MON-LIST", "reject", "DeliveryChannel", 1, {"PGE": 1}
    )  # fmt: skip
    assert rule["examples"][0]["row"] == 2 and "Midstream" in rule["examples"][0]["message"]
    assert r["fields"] == [{"field": "Commitments.ReportingNote", "absent": ["PGE"], "blank": {}}]
    assert (r["records"], r["newly_rejected"]) == (1, 1)
    latest.refresh_from_db()
    assert latest.status == SubmissionVersion.Status.PUBLISHED and latest.specification_version == current("monthly")  # unchanged
    assert AuditEvent.objects.filter(action="specification.impact_tested").exists()
    assert not impact.stale(draft)


def test_a_filing_that_passes_under_the_draft_no_longer_counts_as_rejected(cpuc_admin, draft, filer, year, pge, programs):  # noqa: F811
    confirmed(filer, pge, 1, channel="Upstream")
    r = impact.run(cpuc_admin, draft).report
    assert r["newly_rejected"] == 0 and r["rules"] == []


def test_a_renamed_field_is_read_under_its_new_name_and_unknown_columns_dropped(cpuc_admin, draft):
    editor.rename_field(cpuc_admin, field(draft, "Measure", "PreDesc"), "PreDescription", "The CPUC's name")
    rows = {"Measure": (["CEInputID", "PreDesc", "Bogus"], [{"CEInputID": "M1", "PreDesc": "Old lamps", "Bogus": "x"}]), "Other": ([], [])}
    assert impact.refit(draft, rows) == {"Measure": (["CEInputID", "PreDescription"], [{"CEInputID": "M1", "PreDescription": "Old lamps"}])}


def test_with_no_confirmed_filing_there_is_nothing_to_test(cpuc_admin, draft):
    r = impact.run(cpuc_admin, draft).report
    assert r["filings"] == [] and r["records"] == 0


def test_publishing_keeps_the_test_of_the_draft_as_published(cpuc_admin, draft, filer, year, pge, programs):  # noqa: F811
    confirmed(filer, pge, 1, channel="Upstream")
    first = impact.run(cpuc_admin, draft)
    editor.change_field(cpuc_admin, field(draft, "Commitments", "Comments"), guidance="Say what changed.", reason="Clearer")
    assert impact.stale(draft)
    v = editor.publish(cpuc_admin, draft, effective_on=soon(20))
    last = impact.latest(v)
    assert last != first and last.state == impact.state(v) and not impact.stale(v)


def test_only_a_cpuc_system_administrator_runs_it(draft, pge_admin):
    with pytest.raises(editor.AccessDenied):
        impact.run(pge_admin, draft)


def test_the_impact_test_page(client, cpuc_admin, draft, filer, year, pge, programs):  # noqa: F811
    confirmed(filer, pge, 1)
    sign_in(client, cpuc_admin)
    url = reverse("specifications:edit_impact", args=["monthly", draft.slug])
    page = client.get(url).content.decode()
    assert 'id="impact-run-h"' in page and "Not run yet" in page and "It checks 1 filing: PGE" in page
    r = client.post(reverse("specifications:run_impact", args=["monthly", draft.slug]))
    assert r.status_code == 302 and r.url.endswith("#impact-result")
    page = client.get(url).content.decode()
    assert "Current" in page and "MON-LIST" in page and "Commitments.ReportingNote" in page and 'id="impact-filings"' in page
    publish = client.get(reverse("specifications:edit_publish", args=["monthly", draft.slug])).content.decode()
    assert 'id="publish-impact"' in publish and "1 would newly be rejected" in publish and "Out of date" not in publish

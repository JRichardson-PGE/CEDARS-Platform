"""Correcting a monthly report's records in place (BR-02.4): the submissions service's corrections with the Monthly reports module's
editor."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.core import storage
from cedars.modules.calculations.models import Run
from cedars.modules.monthly import service
from cedars.modules.monthly import upload as up
from cedars.modules.monthly.models import ChangeLetter, ReportLine
from cedars.services.audit.models import AuditEvent
from cedars.services.submissions import corrections
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from .conftest import page_only, sign_in
from .test_budgets import finish_run
from .test_monthly import filer, month, priced, programs, release, report, year  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


def spend(v, prg) -> Decimal:
    return Decimal(ReportLine.objects.get(version=v, prg_id=prg, table=ReportLine.Table.SPEND).values["YTDExpenditures"])


def test_a_rejected_commitment_is_corrected_and_the_report_checked_again(filer, year, pge, programs):  # noqa: F811
    data = report(commitments=["PGE|PGE_Res_HVAC|202802|0|0|5000|100|1|0|80|1|0|waiting on permits"])
    v = service.upload(filer, month(1), pge, data, "jan.zip")
    assert v.status == SubmissionVersion.Status.DRAFT
    assert {(f["table"], f["row"], f["rule_id"]) for f in service.findings(v)} == {("Commitments", 2, "MON-MONTH")}
    v, scope = corrections.correct(filer, v, "Commitments", ["PGE_Res_HVAC"], {"YearMonth": "202801"}, reason="wrong month")
    assert v.status == SubmissionVersion.Status.VALIDATED, service.findings(v)
    assert scope.changed == {"Commitments": {2}}
    f = service.report_file(v)
    assert f["corrected"] and f["uploaded_as"] == "jan.zip" and v.findings["checked_again"]["records"] == 4
    tables = up.read_upload(storage.store("uploads").get_bytes(f["key"]), "report.zip")
    assert up.rows(tables["Commitments"])[1][0]["Comments"] == "waiting on permits"
    assert service.job_file(v)["measures"] == 1
    ev = AuditEvent.objects.get(action=corrections.ACTION)
    assert ev.after["table"] == "Commitments" and ev.after["fields"] == ["YearMonth"] and ev.before["values"]["YearMonth"] == "202802"


def test_spend_follows_a_corrected_cost_and_a_partial_correction(filer, year, pge, programs):  # noqa: F811
    v = service.upload(filer, month(1), pge, report(), "jan.zip")
    assert spend(v, "PGE_Res_HVAC") == Decimal(1000)  # 600 of program costs plus 10 x 40 of rebates
    v, _ = corrections.correct(filer, v, "ProgramCost", ["PGE_Res_HVAC", "2028"], {"AdminCostsOverheadAndGA": "700"})
    assert spend(v, "PGE_Res_HVAC") == Decimal(1100)
    p = corrections.preview(filer, v, b"CEInputID|NumUnits\nM1|20\n", "Measure.csv")
    assert p.counts == {"Measure": {"edit": 1}}
    v, _ = corrections.apply_preview(filer, v, p.token, expected=p.state)
    assert spend(v, "PGE_Res_HVAC") == Decimal(1500)
    assert corrections.summary(v)["partial"] == 1 and corrections.summary(v)["edit"] == 2


def test_the_report_page_offers_the_editor(client, filer, year, pge, programs):  # noqa: F811
    v = service.upload(filer, month(1), pge, report(), "jan.zip")
    sign_in(client, filer)
    page = page_only(client.get(reverse("monthly:report", args=[month(1).pk])).content.decode())
    assert "Correct records" in page and reverse("submissions:records", args=[v.pk]) in page
    url = reverse("submissions:record", args=[v.pk, "Commitments"]) + "?k=PGE_Res_HVAC"
    page = page_only(client.get(url).content.decode())
    assert "Funds committed, year to date" in page and "Comments" in page


def test_a_reopened_report_is_corrected_and_its_letter_says_so(client, filer, year, pge, programs, release, cpuc_admin):  # noqa: F811
    v = priced(filer, pge)
    v = service.confirm(filer, v)
    submissions.move_deadline(cpuc_admin, month(1), timezone.now() - timedelta(minutes=1), "closed")
    submissions.set_policy(cpuc_admin, "monthly", reopen_after_due="open")
    v2 = submissions.request_reopen(filer, v.submission, "a late invoice")
    sign_in(client, filer)
    assert client.get(reverse("submissions:records", args=[v2.pk])).status_code == 200
    v2.refresh_from_db()
    assert v2.findings and v2.status == SubmissionVersion.Status.VALIDATED
    v2, _ = corrections.correct(filer, v2, "ProgramCost", ["PGE_Com_Ltg", "2028"], {"AdminCostsOverheadAndGA": "250"})
    assert all(storage.store("uploads").exists(f["key"]) for f in v.files)
    run_id = service.price(filer, v2)
    if run_id != "none":
        finish_run(Run.objects.get(pk=run_id))
    v2.refresh_from_db()
    v2 = service.confirm(filer, v2)
    letter = ChangeLetter.objects.get(version=v2)
    assert letter.changes["in_place"]["edit"] == 1
    assert "1 changed" in page_only(client.get(reverse("monthly:change_letter", args=[letter.pk])).content.decode())
    body = service.notice(v2, SimpleNamespace(action="submission.published")).body
    assert "Corrected record by record: 1 record (1 changed)." in body

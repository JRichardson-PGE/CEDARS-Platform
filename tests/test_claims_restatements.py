"""Restated results for confirmed claims (BR-06.17): analysis runs on a published version's job that never change the filed record, the
change split into the program and engine effect and the ACC effect, an ACC per claim (BR-06.5), and another year beside it."""

import io
import zipfile
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.core import storage
from cedars.modules.calculations import service as calculations
from cedars.modules.calculations.models import Run
from cedars.modules.claims import restatements, service
from cedars.modules.claims.models import ClaimResult, Restatement
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.submissions.models import SubmissionVersion

from .conftest import sign_in
from .test_budgets import release  # noqa: F401 - the engine release fixture
from .test_claims import claim, claims_zip, confirmed, filer, finish_run, programs, q1, row, tables, year  # noqa: F401

pytestmark = pytest.mark.django_db


def runs(rs: Restatement) -> dict[str, Run]:
    return {role: Run.objects.get(pk=pk) for role, pk in rs.runs.items()}


def measure_rows(run: Run) -> list[dict]:
    import csv

    with zipfile.ZipFile(io.BytesIO(storage.store("uploads").get_bytes(run.inputs["job_zip"]["key"]))) as z:
        return list(csv.DictReader(io.StringIO(z.read("Measure.csv").decode())))


def frozen(v: SubmissionVersion) -> tuple:
    v.refresh_from_db()
    return (v.status, v.number, v.calculation, v.pack_versions, v.published_at, list(ClaimResult.objects.filter(version=v).values()))


def test_a_restatement_on_another_acc_splits_the_change_and_never_touches_the_filing(filer, year, pge, programs, release):  # noqa: F811
    v = confirmed(filer, pge, q1())  # filed on the 2026 ACC: TSB 4,500 (PGE_Res) + 400 (PGE_Com)
    before = frozen(v)
    rs = restatements.start(filer, v, organisation=None, release="0.1.80", acc_version="2024", reason="ACC sensitivity")
    r = runs(rs)
    assert set(r) == {"restated", "bridge"} and rs.acc_changes
    assert (r["restated"].acc_version, r["bridge"].acc_version) == ("2024", "2026")  # the bridge is on the filed ACC
    assert all(run.submission_version is None and run.organisation is None and run.owner == filer for run in r.values())
    assert r["bridge"].inputs["job_zip"]["sha256"] == v.calculation["job_sha256"]  # the filed job, byte for byte
    assert r["restated"].settings["acc_purpose"] == "claims"  # ReCET flags records off the year's ACC (BR-06.5)
    assert rs.filed["acc"] == "2026" and rs.acc_records == {"2024": 2}
    assert restatements.state(rs) == "running"

    finish_run(r["bridge"], scale=1.2)
    finish_run(r["restated"], scale=1.5)
    described, state = restatements.refresh(rs)
    assert state == "done" and described["restated"]["succeeded"]
    assert frozen(v) == before  # the filed record is exactly as confirmed
    events = AuditEvent.objects.filter(object_type="submissions.submissionversion", object_id=str(v.pk))
    actions = set(events.values_list("action", flat=True))
    assert "submission.restatement_started" in actions and "submission.priced" in actions

    c = restatements.comparison(rs)
    tsb = c.total.values("TotalSystemBenefit")
    assert tsb["filed"] == pytest.approx(4900) and tsb["bridge"] == pytest.approx(5800) and tsb["restated"] == pytest.approx(7150)
    assert tsb["engine"] == pytest.approx(900) and tsb["acc"] == pytest.approx(1350) and tsb["total"] == pytest.approx(2250)
    res = next(p for p in c.programs if p.prg_id == "PGE_Res").values("TRC")
    assert res["filed"] == pytest.approx(4500 / 3000) and res["restated"] == pytest.approx(6750 / 3000)
    assert res["engine"] + res["acc"] == pytest.approx(res["total"])
    text = restatements.comparison_csv(rs)
    assert "Total,,TotalSystemBenefit,4900.0,5800.0,7150.0,900.0,1350.0,2250.0" in text


def test_on_the_filed_acc_one_run_carries_the_whole_change(filer, year, pge, programs, release):  # noqa: F811
    v = confirmed(filer, pge, q1())
    rs = restatements.start(filer, v, organisation=None, release="0.1.80", acc_version="2026")
    assert list(rs.runs) == ["restated"] and not rs.acc_changes
    finish_run(runs(rs)["restated"], scale=2.0)
    _, state = restatements.refresh(rs)
    tsb = restatements.comparison(rs).total.values("TotalSystemBenefit")
    assert state == "done" and tsb["bridge"] == tsb["restated"] == pytest.approx(9400)
    assert tsb["acc"] == 0 and tsb["engine"] == pytest.approx(4500)


def test_an_acc_per_claim_goes_in_the_jobs_accversion_column(filer, year, pge, programs, release):  # noqa: F811
    v = confirmed(filer, pge, q1())
    rs = restatements.start(
        filer, v, organisation=None, release="0.1.80", acc_version="2026", overrides=b"ClaimID,ACC\nPGE-2028-001,2024 ACC\n"
    )
    r = runs(rs)
    assert rs.acc_changes and rs.acc_records == {"2024": 1, "2026": 1}  # one record off the filed ACC: the bridge splits the change
    rows = {m["CEInputID"]: m["ACCVersion"] for m in measure_rows(r["restated"])}
    assert rows == {"PGE-2028-001": "2024", "PGE-2028-002": ""}  # blank = the run's ACC
    assert "ACCVersion" not in measure_rows(r["bridge"])[0]
    assert storage.store("uploads").get_bytes(rs.overrides_key).startswith(b"ClaimID,ACC")
    bad = [
        (b"ClaimID,ACC\nPGE-2028-999,2024\n", "not a priced claim"),
        (b"ClaimID,ACC\nPGE-2028-001,2019\n", "not an ACC this release has"),
        (b"Claim,Year\nPGE-2028-001,2024\n", "needs a ClaimID column"),
        (b"ClaimID|ACC\nPGE-2028-001|2024\nPGE-2028-001|2026\n", "two ACCs"),
    ]
    for data, message in bad:
        with pytest.raises(AccessDenied, match=message):
            restatements.start(filer, v, organisation=None, release="0.1.80", acc_version="2026", overrides=data)


def test_what_cannot_be_restated(filer, year, pge, programs, release, cpuc_admin):  # noqa: F811
    v = service.upload_claims(filer, next(d for d in service.periods(2028) if d.kind == "Q1 claim"), pge, q1(), "claims.zip")
    with pytest.raises(AccessDenied, match="Only confirmed claims"):
        restatements.start(filer, v, organisation=None, release="0.1.80", acc_version="2026")
    finish_run(Run.objects.get(pk=service.price(filer, v)))
    v.refresh_from_db()
    v = service.confirm(filer, v)
    with pytest.raises(AccessDenied, match="calculations.submit in PGE"):
        restatements.start(filer, v, organisation=pge, release="0.1.80", acc_version="2026")
    calculations.add_release(cpuc_admin, release="0.1.90", calculations=["recet"], accs={"recet": ["2024"]})
    with pytest.raises(AccessDenied, match="no 2026 ACC, which these claims were filed on"):
        restatements.start(filer, v, organisation=None, release="0.1.90", acc_version="2024")
    with pytest.raises(AccessDenied, match="has no 2022 ACC"):
        restatements.start(filer, v, organisation=None, release="0.1.80", acc_version="2022")
    assert not Restatement.objects.exists() and not Run.objects.filter(submission_version__isnull=True).exists()


def test_a_record_acc_the_release_lacks_is_refused_by_calculations(release, filer, tmp_path):  # noqa: F811
    req = calculations.RunRequest(name="x", release=release, calculation="recet", acc_version="2026", record_accs=("2021",))
    with pytest.raises(AccessDenied, match="Records name the 2021 ACC"):
        calculations._check(req)


def test_the_organisations_runs_and_restatement_belong_to_it(filer, year, pge, programs, release, pge_admin, make_user):  # noqa: F811
    access.grant(pge_admin, filer, pge, "calculations.submit")
    v = confirmed(filer, pge, q1())
    rs = restatements.start(filer, v, organisation=pge, release="0.1.80", acc_version="2024")
    assert all(run.organisation == pge for run in runs(rs).values())
    colleague, outsider = make_user("analyst@pge.com", "Ana"), make_user("someone@example.org", "Som")
    access.grant(pge_admin, colleague, pge, "calculations.read")
    assert restatements.can_read(colleague, rs) and not restatements.can_read(outsider, rs)
    assert list(restatements.visible(colleague)) == [rs] and not restatements.visible(outsider).exists()


def test_the_pages(client, filer, year, pge, programs, release, make_user):  # noqa: F811
    v = confirmed(filer, pge, q1())
    sign_in(client, filer)
    page = client.get(reverse("claims:version", args=[v.pk])).content.decode()
    assert "Restated results" in page and reverse("claims:restate", args=[v.pk]) in page
    form = client.get(reverse("claims:restate", args=[v.pk])).content.decode()
    assert "2026 ACC (as filed)" in form and "My own analysis" in form and "ReCET 0.1.80 (default, the current engine)" in form
    r = client.post(reverse("claims:restate", args=[v.pk]), {"organisation": "", "release": "0.1.80", "acc_version": "2024", "name": ""})
    rs = Restatement.objects.get()
    assert r.status_code == 302 and r["Location"] == reverse("claims:restatement", args=[rs.pk])
    assert rs.name == "PGE 2028 Q1 claim, version 1 restated on ReCET 0.1.80, 2024 ACC"
    page = client.get(r["Location"]).content.decode()
    assert "Running" in page and "Check again" in page and 'id="summary-h"' not in page
    finish_run(runs(rs)["bridge"], scale=1.2)
    finish_run(runs(rs)["restated"], scale=1.5)
    page = client.get(r["Location"]).content.decode()
    assert 'id="summary-h"' in page and "+$1,350" in page and "+$900" in page and "+$2,250" in page
    page = client.get(r["Location"] + "?metric=TRC").content.decode()
    assert "TRC ratio by program" in page
    csv_text = client.get(reverse("claims:restatement_csv", args=[rs.pk])).content.decode()
    assert csv_text.startswith("PrgID,ProgramName,Figure,Filed,CurrentEngineFiledACC,Restated")
    assert rs.name in client.get(reverse("claims:restatements")).content.decode()
    assert rs.name in client.get(reverse("claims:version", args=[v.pk])).content.decode()
    other = make_user("someone@example.org", "Som")
    sign_in(client, other)
    assert client.get(r["Location"]).status_code == 404
    assert rs.name not in client.get(reverse("claims:version", args=[v.pk])).content.decode()
    errors = client.post(reverse("claims:restate", args=[v.pk]), {"organisation": "", "release": "0.1.80", "acc_version": "2022"})
    assert errors.status_code == 200 and Restatement.objects.count() == 1


def test_beside_another_claim_year(filer, year, pge, programs, release, cpuc_admin):  # noqa: F811
    now = timezone.now()
    due = {k: now + timedelta(days=30 * (i + 1)) for i, (k, _) in enumerate(service.PERIODS)}
    service.add_year(cpuc_admin, year=2029, acc_version="2024", due=due)
    v28 = confirmed(filer, pge, q1())
    d29 = next(d for d in service.periods(2029) if d.kind == "Q1 claim")
    data = claims_zip(
        tables(
            claim("PGE-2029-001", quarter="2029Q1"),
            ProgramCost=[row("ProgramCost", PrgID="PGE_Res", PrgYear="2029", ClaimYearQuarter="2029Q1")],
            Site=[row("Site", SiteID="PGE-S1")],
        )
    )
    v29 = service.upload_claims(filer, d29, pge, data, "claims.zip")
    assert v29.status == SubmissionVersion.Status.VALIDATED, service.findings(v29)
    finish_run(Run.objects.get(pk=service.price(filer, v29)), scale=2.0)
    v29.refresh_from_db()
    v29 = service.confirm(filer, v29)

    rs = restatements.start(filer, v28, organisation=None, release="0.1.80", acc_version="2024")
    for run in runs(rs).values():
        finish_run(run, scale=1.5)
    restatements.refresh(rs)
    assert [y for y, _ in restatements.other_years(rs)] == [2029]
    rows = {r["prg_id"]: r for r in restatements.year_comparison(rs, v29, "TotalSystemBenefit")}
    assert rows["PGE_Res"]["restated"] == pytest.approx(6750) and rows["PGE_Res"]["other"] == pytest.approx(9000)
    assert rows["PGE_Res"]["change"] == pytest.approx(2250) and rows["Total"]["change"] == pytest.approx(9400 - 7150)

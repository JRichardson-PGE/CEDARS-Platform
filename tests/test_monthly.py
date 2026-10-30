import io
import zipfile
from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.core import storage
from cedars.modules.calculations.models import Run
from cedars.modules.monthly import service
from cedars.modules.monthly import upload as up
from cedars.modules.monthly.models import MonthlyResult, ReportLine
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline, SubmissionVersion

from .conftest import sign_in
from .test_budgets import COSTS, MEASURE, finish_run, release  # noqa: F401 (release is a fixture)

pytestmark = pytest.mark.django_db
EXP = "PA|PrgID|YearMonth|MonthExpenditures|YTDExpenditures|MonthIncentivesPaid|YTDIncentivesPaid|Carryover|Comments"
COM = (
    "PA|PrgID|YearMonth|PriorCommittedFundsBalance|PriorYTDCommittedExpenditures|YTDCommittedFunds|YTDIncentivesCommitted|"
    "YTDCommittedGrosskWh|YTDCommittedGrosskW|YTDCommittedGrossTherm|YTDCommittedNetkWh|YTDCommittedNetkW|YTDCommittedNetTherm|Comments"
)


def report(ym="202801", expenditures=None, commitments=None, costs=None, measures=None, extra=None, exp_header=EXP) -> bytes:
    q = f"{ym[:4]}Q{(int(ym[4:]) - 1) // 3 + 1}"
    expenditures = expenditures if expenditures is not None else [
        f"PGE|PGE_Res_HVAC|{ym}|1000|1000|400|400|0|",
        f"PGE|PGE_Com_Ltg|{ym}|200|200|0|0|0|slow start",
    ]  # fmt: skip
    commitments = commitments if commitments is not None else [f"PGE|PGE_Res_HVAC|{ym}|0|0|5000|2500|100|1|0|80|1|0|"]
    costs = costs if costs is not None else [f"PGE_Res_HVAC|{ym[:4]}|{q}|PGE|600|0|0|0|0|0|0|0|0|0|0"]
    measures = measures if measures is not None else [f"M1|PGE_Res_HVAC|{q}|10|40|0|0|0"]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Expenditures.csv", "\n".join([exp_header] + expenditures) + "\n")
        z.writestr("Commitments.csv", "\n".join([COM] + commitments) + "\n")
        z.writestr("ProgramCost.csv", "\n".join([COSTS] + costs) + "\n")
        z.writestr("Measure.csv", "\n".join([MEASURE] + measures) + "\n")
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return out.getvalue()


@pytest.fixture
def programs(pge_admin, pge, cpuc_admin):
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    registry.create_program(pge_admin, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1), name="Residential HVAC")
    registry.create_program(pge_admin, prg_id="PGE_Com_Ltg", administrator=pge, valid_from=date(2024, 1, 1), name="Commercial lighting")
    registry.create_program(cpuc_admin, prg_id="SCE_Ind", administrator=sce, valid_from=date(2024, 1, 1), name="Industrial")
    return sce


@pytest.fixture
def filer(make_user, pge, pge_admin):
    u = make_user("monthly@pge.com", "Mo Nthly")
    for p in ("monthly.submit", "monthly.confirm"):
        access.grant(pge_admin, u, pge, p)
    return u


@pytest.fixture
def year(cpuc_admin):
    """2028 set up, every month open now and due in a month (the real ones open when their month ends)."""
    s = service.set_up_year(cpuc_admin, year=2028, acc_version="2026", due_day=20)
    Deadline.objects.filter(module="monthly").update(opens_at=None, due_at=timezone.now() + timedelta(days=30))
    return s


def month(n: int) -> Deadline:
    return service.months(2028).get(period__number=n)


def priced(filer, pge, n=1, data=None):
    v = service.upload(filer, month(n), pge, data or report(f"2028{n:02d}"), "r.zip")
    run_id = service.price(filer, v)
    if run_id != "none":
        finish_run(Run.objects.get(pk=run_id))
    v.refresh_from_db()
    return v


# --- the upload


def test_a_zip_or_a_workbook_holds_the_four_tables():
    tables = up.read_upload(report(extra={"__MACOSX/._Measure.csv": "x"}), "r.zip")
    assert set(tables) == set(up.TABLES)
    assert zipfile.ZipFile(io.BytesIO(up.job_zip(tables))).namelist() == ["Measure.csv", "ProgramCost.csv"]
    assert up.job_zip(tables) == up.job_zip(tables)  # the same tables, the same SHA-256
    with pytest.raises(up.UploadError, match="it also has notes.docx"):
        up.read_upload(report(extra={"notes.docx": "x"}), "r.zip")
    with pytest.raises(up.UploadError, match="Expenditures has no YTDIncentivesPaid"):
        up.read_upload(report(exp_header=EXP.replace("|YTDIncentivesPaid", "")), "r.zip")
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, lines in (("Expenditures", [EXP, "PGE|PGE_Res_HVAC|202801|1|1|0|0|0|"]), ("Commitments", [COM]), ("ProgramCost", [COSTS]),
                        ("Measure", [MEASURE])):  # fmt: skip
        ws = wb.create_sheet(name)
        for line in lines:
            ws.append([float(c) if c.isdigit() else c for c in line.split("|")])
    buf = io.BytesIO()
    wb.save(buf)
    tables = up.read_upload(buf.getvalue(), "r.xlsx")
    assert up.rows(tables["Expenditures"])[1][0]["YearMonth"] == "202801"
    del wb["Commitments"]
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(up.UploadError, match="no Commitments sheet"):
        up.read_upload(buf.getvalue(), "r.xlsx")


def test_a_cpuc_system_administrator_sets_up_a_year(cpuc_admin, pge_admin):
    with pytest.raises(AccessDenied, match="system administrator"):
        service.set_up_year(pge_admin, year=2028, acc_version="2026", due_day=20)
    s = service.set_up_year(cpuc_admin, year=2028, acc_version="2026", due_day=31)
    deadlines = list(service.months(2028))
    assert len(deadlines) == 12 and all(d.kind == "Monthly report" for d in deadlines)
    assert timezone.localtime(deadlines[0].due_at).date() == date(2028, 2, 29)  # day 31 of a 29-day month: its last day
    assert timezone.localtime(deadlines[11].due_at).date() == date(2029, 1, 31)
    assert timezone.localtime(deadlines[2].opens_at).date() == date(2028, 4, 1)  # March's report opens when March ends
    with pytest.raises(AccessDenied, match="already set up"):
        service.set_up_year(cpuc_admin, year=2028, acc_version="2026", due_day=20)
    service.change_setup(cpuc_admin, s, acc_version="2024", reason="claims ACC for 2028")
    assert AuditEvent.objects.filter(action="monthly_reports.setup_changed").exists()


def test_an_upload_gives_the_report_by_program(filer, year, pge, programs):
    v = service.upload(filer, month(1), pge, report(), "january.zip")
    assert v.status == SubmissionVersion.Status.VALIDATED and v.findings["rejected"] == 0 and v.findings["records"] == 5
    lines = {(ln.table, ln.prg_id): ln for ln in ReportLine.objects.filter(version=v)}
    assert lines[("Expenditures", "PGE_Res_HVAC")].values["YTDIncentivesPaid"] == "400"
    assert lines[("Commitments", "PGE_Res_HVAC")].values["YTDIncentivesCommitted"] == "2500"
    assert lines[("Expenditures", "PGE_Com_Ltg")].comments == "slow start"
    assert any("BCA-level validation only" in n for n in v.findings["notes"])
    r = service.report(v)
    assert r.total.get("YTDExpenditures") == 1200 and r.total.get("YTDIncentivesCommitted") == 2500 and not r.estimated
    assert {f["name"] for f in v.files} == {"report.zip", "job.zip"}


def test_module_checks(filer, year, pge, programs):
    bad = report(
        "202802",
        expenditures=["SCE|PGE_Res_HVAC|202802|1|1|0|0|0|", "PGE|SCE_Ind|202802|1|1|0|0|0|", "PGE|PGE_Com_Ltg|202801|x|1|0|0|0|",
                      "PGE|NOPE|202802|1|1|0|0|0|"],
        commitments=[],
        costs=["PGE_Res_HVAC|2027|2027Q1|PGE|1|0|0|0|0|0|0|0|0|0|0"],
        measures=["M1|PGE_Res_HVAC|2028Q3|1|0|0|0|0", "M2|PGE_Com_Ltg|2028Q1|1|0|0|0|0"],
    )  # fmt: skip
    v = service.upload(filer, month(2), pge, bad, "february.zip")
    assert v.status == SubmissionVersion.Status.DRAFT
    found = {(f["table"], f["row"], f["rule_id"], f["severity"]) for f in service.findings(v)}
    assert {("Expenditures", 2, "MON-PA", "reject"), ("Expenditures", 3, "MON-ORG", "reject"), ("Expenditures", 4, "MON-MONTH", "reject"),
            ("Expenditures", 4, "MON-NUM", "reject"), ("Expenditures", 5, "MON-PRG", "reject"), ("ProgramCost", 2, "MON-YEAR", "reject"),
            ("Measure", 2, "MON-YEAR", "reject"), ("Measure", 0, "MON-NOCOST", "warning")} <= found  # fmt: skip
    assert ("Expenditures", 0, "MON-NOEXP", "warning") in found  # PGE_Com_Ltg: measures, no accepted expenditures row
    with pytest.raises(AccessDenied, match="validated"):
        service.price(filer, v)
    dup = report(expenditures=["PGE|PGE_Res_HVAC|202801|1|1|0|0|0|", "PGE|PGE_Res_HVAC|202801|1|1|0|0|0|"])
    v = service.upload(filer, month(1), pge, dup, "january.zip")
    assert "MON-DUP" in {f["rule_id"] for f in service.findings(v)}


def test_cedars_retired_columns_are_named_not_read(filer, year, pge, programs):
    header = EXP.replace("|Carryover", "|MonthFundShifts|Carryover")
    data = report(exp_header=header, expenditures=["PGE|PGE_Res_HVAC|202801|1|1|0|0|99|0|"])
    v = service.upload(filer, month(1), pge, data, "old.zip")
    assert any("MonthFundShifts is not read (fund shifts are made in Budgets)" in n for n in v.findings["notes"])
    assert "MonthFundShifts" not in ReportLine.objects.get(version=v, table="Expenditures").values


def test_year_to_date_figures_are_checked_against_the_previous_month(filer, year, pge, programs, release):  # noqa: F811
    jan = service.confirm(filer, priced(filer, pge, 1))
    assert jan.status == SubmissionVersion.Status.PUBLISHED
    good = report("202802", expenditures=["PGE|PGE_Res_HVAC|202802|500|1500|100|500|0|", "PGE|PGE_Com_Ltg|202802|0|200|0|0|0|"])
    v = service.upload(filer, month(2), pge, good, "february.zip")
    assert "MON-YTD" not in {f["rule_id"] for f in service.findings(v)}
    off = report("202802", expenditures=["PGE|PGE_Res_HVAC|202802|500|1400|100|500|0|"])
    v = service.upload(filer, month(2), pge, off, "february.zip")
    ytd = [f for f in service.findings(v) if f["rule_id"] == "MON-YTD"]
    assert [f["field"] for f in ytd] == ["YTDExpenditures"] and v.status == SubmissionVersion.Status.VALIDATED  # a warning only
    march = service.upload(filer, month(3), pge, report("202803"), "march.zip")
    assert any("previous month's report is not confirmed" in n for n in march.findings["notes"])


# --- estimates, confirm, the year


def test_priced_estimates_then_confirmed_and_public(filer, year, pge, programs, release, make_user):  # noqa: F811
    v = service.upload(filer, month(1), pge, report(), "january.zip")
    run = Run.objects.get(pk=service.price(filer, v))
    assert run.acc_version == "2026" and run.submission_version_id == v.pk and "(estimate)" in run.name
    assert not service.priced(v)
    with pytest.raises(AccessDenied, match="Price this version first"):
        service.confirm(filer, v)
    finish_run(run)
    v.refresh_from_db()
    assert service.priced(v)
    r = service.report(v)
    hvac = next(x for x in r.rows if x.prg_id == "PGE_Res_HVAC")
    assert hvac.estimate("TotalSystemBenefit") == 4500 and hvac.estimate("TRC") == pytest.approx(4500 / 4000) and r.estimated
    community = make_user("someone@example.com")
    assert not service.can_read(community, v)
    v = service.confirm(filer, v)
    assert v.status == SubmissionVersion.Status.PUBLISHED and service.can_read(community, v)
    assert MonthlyResult.objects.filter(version=v).exists()
    assert [label.split(" (")[0] for label, *_ in service.published_files(v)] == [
        "The report",
        "Estimate run settings",
        "Estimate run receipt",
    ]
    with pytest.raises(AccessDenied, match="already confirmed"):
        service.upload(filer, month(1), pge, report(), "again.zip")


def test_a_report_with_no_measures_has_nothing_to_estimate(filer, year, pge, programs, make_user):
    v = service.upload(filer, month(1), pge, report(measures=[], costs=[]), "january.zip")
    assert service.price(filer, v) == "none"
    v.refresh_from_db()
    assert service.priced(v) and v.calculation["nothing_priced"] and not Run.objects.exists()
    assert service.confirm(filer, v).status == SubmissionVersion.Status.PUBLISHED
    v2 = service.upload(filer, month(2), pge, report("202802", measures=[], costs=[]), "february.zip")
    with pytest.raises(AccessDenied, match="monthly.submit"):
        service.price(make_user("stranger@example.com"), v2)


def test_unconfirm_before_the_due_date_and_reopen_after(filer, year, pge, programs, cpuc_admin):
    v = priced(filer, pge, 1, report(measures=[], costs=[]))
    v = service.confirm(filer, v)
    v = submissions.unconfirm(filer, v, "January's lighting costs were wrong")
    assert v.status == SubmissionVersion.Status.VALIDATED and v.number == 1  # BR-14.3: the same version, back to validated
    v = service.confirm(filer, v)
    submissions.move_deadline(cpuc_admin, month(1), timezone.now() - timedelta(minutes=1), "closed")
    with pytest.raises(AccessDenied, match="reopening"):
        submissions.unconfirm(filer, v)
    submissions.set_policy(cpuc_admin, "monthly", reopen_after_due="open")
    v2 = submissions.request_reopen(filer, v.submission, "a late invoice")
    assert v2.number == 2 and v2.files == v.files
    service.ensure_lines(v2)
    assert v2.monthly_lines.count() == v.monthly_lines.count()


def test_the_operating_budget_comes_from_budgets(filer, year, pge, programs, cpuc_admin, pge_admin):
    from cedars.modules.budgets import service as budgets

    from .test_budgets import group_sums, job

    for p in ("budgets.submit", "budgets.confirm"):
        access.grant(pge_admin, filer, pge, p)
    tual = budgets.add_filing(
        cpuc_admin, first_year=2028, kind="True-up advice letter (TUAL)", due_at=timezone.now() + timedelta(days=30), acc_version="2026"
    )
    b = budgets.upload(filer, tual, pge, job(), "tual.zip")
    key = "runs/x/out/_results/group_sums2_measure_level_run.parquet"
    storage.store("results").put_bytes(key, group_sums())
    b = submissions.record_calculation(
        None, b, run="x", input_hash="h", details={"job_sha256": budgets.job_file(b)["sha256"], "results": {"group_sums": key}}
    )
    budgets.confirm(filer, b)
    rows = {(r["prg_id"], r["year"]): r for r in registry.operating_budget(pge, registry.cycle_for_year(2029))}
    assert rows[("PGE_Res_HVAC", 2028)]["operating"] == 1800 + 600  # program costs plus measure incentives, as Budgets counts them
    v = service.upload(filer, month(1), pge, report(measures=[], costs=[]), "january.zip")
    r = service.report(v)
    hvac = next(x for x in r.rows if x.prg_id == "PGE_Res_HVAC")
    assert r.budget and hvac.operating == 2400 and hvac.spent_share == pytest.approx(1000 / 2400)
    assert registry.operating_budget(pge, None) == []


def test_the_year_in_one_view_adds_up_the_months(filer, year, pge, programs):
    service.confirm(filer, priced(filer, pge, 1, report(measures=[], costs=[])))
    feb = report("202802", expenditures=["PGE|PGE_Res_HVAC|202802|500|1600|0|400|0|", "PGE|PGE_Com_Ltg|202802|0|200|0|0|0|"],
                 measures=[], costs=[])  # fmt: skip
    service.confirm(filer, priced(filer, pge, 2, feb))
    months = service.year_view(pge, 2028)
    assert [m.version is not None for m in months[:3]] == [True, True, False]
    assert months[0].consistent and months[1].running == 1700 and months[1].gap == 100 and not months[1].consistent


# --- pages


def test_the_pages(client, filer, year, pge, programs, release, cpuc_admin, make_user):  # noqa: F811
    from django.core.files.uploadedfile import SimpleUploadedFile

    sign_in(client, filer)
    assert client.get(reverse("monthly:index")).status_code == 200
    jan = month(1)
    page = client.get(reverse("monthly:report", args=[jan.pk]))
    assert page.status_code == 200 and b"Upload the report" in page.content
    client.post(reverse("monthly:upload", args=[jan.pk]), {"file": SimpleUploadedFile("january.zip", report())})
    v = SubmissionVersion.objects.get()
    assert v.status == "validated" and b"Price the estimates" in client.get(reverse("monthly:report", args=[jan.pk])).content
    client.post(reverse("monthly:price", args=[jan.pk]))
    run = Run.objects.get()
    assert b"the run's page" in client.get(reverse("monthly:report", args=[jan.pk])).content
    finish_run(run)
    page = client.get(reverse("monthly:report", args=[jan.pk]))
    assert b"Confirm and publish" in page.content and b"Estimated TSB" in page.content
    client.post(reverse("monthly:confirm", args=[jan.pk]))
    v.refresh_from_db()
    assert v.status == "published"
    page = client.get(reverse("monthly:version", args=[v.pk]))
    assert page.status_code == 200 and b"Unvalidated estimates" in page.content
    csv = client.get(reverse("monthly:version_csv", args=[v.pk])).content.decode()
    assert csv.splitlines()[0].startswith("PrgID,ProgramName,Reported") and "EstimatedTotalSystemBenefit" in csv
    assert client.get(reverse("monthly:file", args=[v.pk, 0])).status_code == 200
    assert client.get(reverse("monthly:year", args=[2028])).status_code == 200
    assert client.get(reverse("monthly:year_csv", args=[2028])).content.decode().splitlines()[1].startswith("202801,1,published")
    community = make_user("someone@example.com")
    client.logout()
    sign_in(client, community)
    assert client.get(reverse("monthly:version", args=[v.pk])).status_code == 200
    assert client.get(reverse("monthly:findings_csv", args=[v.pk])).status_code == 404
    assert client.get(reverse("monthly:report", args=[jan.pk]) + "?org=PGE").status_code == 404
    assert b"Version 1: published" in client.get(reverse("monthly:year", args=[2028]) + "?org=PGE").content
    assert client.get(reverse("monthly:year_new")).status_code == 404
    client.logout()
    sign_in(client, cpuc_admin)
    assert client.get(reverse("monthly:report", args=[jan.pk]) + "?org=PGE").status_code == 200
    assert client.get(reverse("monthly:setup", args=[2028])).status_code == 200
    r = client.post(reverse("monthly:year_new"), {"year": 2029, "due_day": 15, "acc_version": "2026"})
    assert r.status_code == 302 and service.months(2029).count() == 12

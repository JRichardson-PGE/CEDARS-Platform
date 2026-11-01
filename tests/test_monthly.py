import io
import zipfile
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

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

from .conftest import page_only, sign_in
from .test_budgets import COSTS, MEASURE, finish_run, release  # noqa: F401 (release is a fixture)
from .test_uploads import split

pytestmark = pytest.mark.django_db
COM = (
    "PA|PrgID|YearMonth|PriorCommittedFundsBalance|PriorYTDCommittedExpenditures|YTDCommittedFunds|"
    "YTDCommittedGrosskWh|YTDCommittedGrosskW|YTDCommittedGrossTherm|YTDCommittedNetkWh|YTDCommittedNetkW|YTDCommittedNetTherm|Comments"
)


def cost(prg="PGE_Res_HVAC", year="2028", admin=600, obf=0) -> str:
    return f"{prg}|{year}|{year}Q1|PGE|{admin}|0|0|0|0|0|0|0|0|{obf}|0"


def report(ym="202801", commitments=None, costs=None, measures=None, extra=None, com_header=COM) -> bytes:
    """January by default: PGE_Res_HVAC spends 600 of program costs plus 10 x 40 of rebates, PGE_Com_Ltg 200 of program costs."""
    q = f"{ym[:4]}Q{(int(ym[4:]) - 1) // 3 + 1}"
    commitments = commitments if commitments is not None else [f"PGE|PGE_Res_HVAC|{ym}|0|0|5000|100|1|0|80|1|0|waiting on permits"]
    costs = costs if costs is not None else [cost(year=ym[:4]), cost("PGE_Com_Ltg", ym[:4], 200, obf=50)]
    measures = measures if measures is not None else [f"M1|PGE_Res_HVAC|{q}|10|40|0|0|0"]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Commitments.csv", "\n".join([com_header] + commitments) + "\n")
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


def test_a_zip_or_a_workbook_holds_the_three_tables():
    tables = up.read_upload(report(extra={"__MACOSX/._Measure.csv": "x"}), "r.zip")
    assert set(tables) == set(up.TABLES) == {"Commitments", "Measure", "ProgramCost"}
    assert zipfile.ZipFile(io.BytesIO(up.job_zip(tables))).namelist() == ["Measure.csv", "ProgramCost.csv"]
    assert up.job_zip(tables) == up.job_zip(tables)  # the same tables, the same SHA-256
    with pytest.raises(up.UploadError, match="it also has notes.docx"):
        up.read_upload(report(extra={"notes.docx": "x"}), "r.zip")
    with pytest.raises(up.UploadError, match="Spend is read from the BCA inputs"):
        up.read_upload(report(extra={"Expenditures.csv": "PA|PrgID"}), "r.zip")
    with pytest.raises(up.UploadError, match="Commitments has no YTDCommittedFunds"):
        up.read_upload(report(com_header=COM.replace("|YTDCommittedFunds", "")), "r.zip")
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, lines in (("Commitments", [COM, "PGE|PGE_Res_HVAC|202801|0|0|1|0|0|0|0|0|0|"]), ("ProgramCost", [COSTS]),
                        ("Measure", [MEASURE]), ("Expenditures", ["PA|PrgID"])):  # fmt: skip
        ws = wb.create_sheet(name)
        for line in lines:
            ws.append([float(c) if c.isdigit() else c for c in line.split("|")])
    buf = io.BytesIO()
    wb.save(buf)
    tables = up.read_upload(buf.getvalue(), "r.xlsx")  # an old Expenditures sheet is passed over
    assert set(tables) == set(up.TABLES) and up.rows(tables["Commitments"])[1][0]["YearMonth"] == "202801"
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


def test_spend_is_read_from_the_bca_inputs(filer, year, pge, programs):
    v = service.upload(filer, month(1), pge, report(), "january.zip")
    assert v.status == SubmissionVersion.Status.VALIDATED and v.findings["rejected"] == 0 and v.findings["records"] == 4
    lines = {(ln.table, ln.prg_id): ln for ln in ReportLine.objects.filter(version=v)}
    hvac = lines[("Spend", "PGE_Res_HVAC")].values
    assert (hvac["AdminCostsOverheadAndGA"], hvac["RebatesAndIncentives"], hvac["YTDExpenditures"]) == ("600", "400", "1000")
    ltg = lines[("Spend", "PGE_Com_Ltg")].values
    assert ltg["YTDExpenditures"] == "200" and ltg["OnBillFinancing"] == "50"  # on-bill financing is kept apart, as in Budgets
    assert lines[("Commitments", "PGE_Res_HVAC")].values["YTDCommittedFunds"] == "5000"
    assert lines[("Commitments", "PGE_Res_HVAC")].comments == "waiting on permits"
    assert any("BCA-level validation only" in n for n in v.findings["notes"])
    r = service.report(v)
    assert r.month_known and not r.estimated
    assert r.total.get("YTDExpenditures") == 1200 and r.total.get("MonthExpenditures") == 1200  # January: the year to date
    assert r.total.get("RebatesAndIncentives") == 400 and r.total.get("YTDCommittedFunds") == 5000
    assert {f["name"] for f in v.files} == {"report.zip", "job.zip"}


def test_paid_and_committed_incentives_are_separate_figures(client, filer, year, pge, programs):
    """BR-14.6: paid incentives (the measures' rebates and incentives, part of spend) and committed incentives (Commitments, to be paid
    in the future) are each shown on their own, labeled so neither reads as a share of the other, and never set against each other."""
    v = service.upload(filer, month(1), pge, report(), "january.zip")
    sign_in(client, filer)
    page = client.get(reverse("monthly:version", args=[v.pk])).content.decode()
    assert service.PAID in page and service.COMMITTED in page and "Approved budget (filed)" in page
    assert escape(service.PAID_VS_COMMITTED) in page
    assert "$400" in page and "$5,000" in page and "$1,200" in page  # paid incentives, committed, spend (which includes the $400)
    year_page = client.get(reverse("monthly:year", args=[2028])).content.decode()
    assert service.PAID in year_page and service.COMMITTED in year_page and escape(service.PAID_VS_COMMITTED) in year_page
    for csv in (
        client.get(reverse("monthly:version_csv", args=[v.pk])).content.decode(),
        client.get(reverse("monthly:year_csv", args=[2028])).content.decode(),
    ):
        header = csv.splitlines()[0].split(",")
        assert "RebatesAndIncentives" in header and "YTDCommittedFunds" in header and "YTDExpenditures" in header
    header = client.get(reverse("monthly:version_csv", args=[v.pk])).content.decode().splitlines()[0].split(",")
    assert header[3:5] == ["ApprovedBudget", "OperatingBudget"]
    every = page + year_page + "".join(header)
    for word in ("paid vs", "paid versus", "share of committed", "committed paid", "PaidToCommitted", "CommittedShare"):
        assert word.lower() not in every.lower()  # no difference, ratio or flag between the two


def test_module_checks(filer, year, pge, programs):
    bad = report(
        "202802",
        commitments=["SCE|PGE_Res_HVAC|202802|0|0|1|0|0|0|0|0|0|", "PGE|SCE_Ind|202802|0|0|1|0|0|0|0|0|0|",
                     "PGE|PGE_Com_Ltg|202801|0|0|x|0|0|0|0|0|0|", "PGE|NOPE|202802|0|0|1|0|0|0|0|0|0|"],
        costs=[cost(year="2027"), cost("PGE_Com_Ltg").replace("|600|", "|lots|")],
        measures=["M1|PGE_Res_HVAC|2028Q3|1|0|0|0|0", "M2|PGE_Com_Ltg|2028Q1|1|0|0|0|0", "M3|PGE_Res_HVAC|2028Q1|1|x|0|0|0"],
    )  # fmt: skip
    v = service.upload(filer, month(2), pge, bad, "february.zip")
    assert v.status == SubmissionVersion.Status.DRAFT
    found = {(f["table"], f["row"], f["field"], f["rule_id"], f["severity"]) for f in service.findings(v)}
    assert {("Commitments", 2, "PA", "MON-PA", "reject"), ("Commitments", 3, "PrgID", "MON-ORG", "reject"),
            ("Commitments", 4, "YearMonth", "MON-MONTH", "reject"), ("Commitments", 4, "YTDCommittedFunds", "MON-NUM", "reject"),
            ("Commitments", 5, "PrgID", "MON-PRG", "reject"), ("ProgramCost", 2, "PrgYear", "MON-YEAR", "reject"),
            ("ProgramCost", 3, "AdminCostsOverheadAndGA", "MON-NUM", "reject"), ("Measure", 2, "ClaimYearQuarter", "MON-YEAR", "reject"),
            ("Measure", 4, "UnitEndUserRebate", "MON-NUM", "reject"),
            ("Measure", 0, "PrgID", "MON-NOCOST", "warning")} <= found  # fmt: skip
    with pytest.raises(AccessDenied, match="validated"):
        service.price(filer, v)
    dup = report(commitments=["PGE|PGE_Res_HVAC|202801|0|0|1|0|0|0|0|0|0|"] * 2, costs=[cost(), cost()])
    v = service.upload(filer, month(1), pge, dup, "january.zip")
    assert {("Commitments", "MON-DUP"), ("ProgramCost", "MON-DUP")} <= {(f["table"], f["rule_id"]) for f in service.findings(v)}


def test_cedars_retired_columns_are_named_not_read(filer, year, pge, programs):
    header = COM.replace("|Comments", "|MonthFundShifts|YTDExpenditures|Comments")
    data = report(com_header=header, commitments=["PGE|PGE_Res_HVAC|202801|0|0|1|0|0|0|0|0|0|99|5|"])
    v = service.upload(filer, month(1), pge, data, "old.zip")
    assert any("MonthFundShifts, YTDExpenditures are not read (fund shifts are made in Budgets)" in n for n in v.findings["notes"])
    assert "MonthFundShifts" not in ReportLine.objects.get(version=v, table="Commitments").values
    assert ReportLine.objects.get(version=v, table="Spend", prg_id="PGE_Res_HVAC").values["YTDExpenditures"] == "1000"


def test_the_months_spend_is_the_change_in_the_year_to_date(filer, year, pge, programs, release):  # noqa: F811
    assert service.report(service.upload(filer, month(2), pge, report("202802"), "february.zip")).month_known is False
    jan = service.confirm(filer, priced(filer, pge, 1))
    assert jan.status == SubmissionVersion.Status.PUBLISHED
    more = ["M1|PGE_Res_HVAC|2028Q1|10|40|0|0|0", "M2|PGE_Res_HVAC|2028Q1|5|20|0|0|0"]
    feb = report("202802", costs=[cost(admin=700), cost("PGE_Com_Ltg", admin=200)], measures=more)
    v = service.upload(filer, month(2), pge, feb, "february.zip")
    assert "MON-YTD" not in {f["rule_id"] for f in service.findings(v)}
    r = service.report(v)
    hvac = next(x for x in r.rows if x.prg_id == "PGE_Res_HVAC")
    assert hvac.get("YTDExpenditures") == 1200 and hvac.get("MonthExpenditures") == 200 and r.total.get("MonthExpenditures") == 200
    v = service.upload(filer, month(2), pge, report("202802", costs=[cost(admin=100)]), "february.zip")
    ytd = {f["record_id"]: f["message"] for f in service.findings(v) if f["rule_id"] == "MON-YTD"}
    assert "below last month's 1,000.00" in ytd["PGE_Res_HVAC"] and "not in this report's BCA inputs" in ytd["PGE_Com_Ltg"]
    assert v.status == SubmissionVersion.Status.VALIDATED  # warnings only
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


def test_unconfirm_before_the_due_date_and_reopen_after(filer, year, pge, programs, cpuc_admin, django_capture_on_commit_callbacks):
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
    with django_capture_on_commit_callbacks(execute=True):  # a correction's upload leaves the published version's files alone
        service.upload(filer, month(1), pge, report(measures=[], costs=[]), "correction.zip")
    assert all(storage.store("uploads").exists(f["key"]) for f in v.files)


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
    v = service.upload(filer, month(1), pge, report(), "january.zip")
    r = service.report(v)
    hvac = next(x for x in r.rows if x.prg_id == "PGE_Res_HVAC")
    assert r.budget and hvac.operating == 2400 and hvac.spent_share == pytest.approx(1000 / 2400)
    assert registry.operating_budget(pge, None) == []


def test_the_year_in_one_view_flags_a_falling_year_to_date(filer, year, pge, programs, release):  # noqa: F811
    service.confirm(filer, priced(filer, pge, 1))
    service.confirm(filer, priced(filer, pge, 2, report("202802", costs=[cost(admin=500), cost("PGE_Com_Ltg", admin=200)])))
    months = service.year_view(pge, 2028)
    assert [m.version is not None for m in months[:3]] == [True, True, False]
    assert months[0].spend == 1200 and months[0].consistent
    assert months[1].spend == -100 and not months[1].consistent and months[2].spend is None


# --- the 13th-month report


def fake_claims(totals):
    def claims_totals(organisation, year):
        return [dict(t, year=year) for t in totals] if organisation.code == "PGE" and year == 2028 else []

    return [claims_totals]


def claimed(prg, expenditures, quarters=(1, 2, 3, 4), final=True, tsb=900.0):
    return {
        "prg_id": prg, "pa": "PGE", "quarters": list(quarters), "final": final, "records": 3, "expenditures": Decimal(expenditures),
        "incentives": Decimal(0), "gross_kwh": 120.0, "gross_kw": 0.1, "gross_therm": 0.0, "net_kwh": 100.0, "net_kw": 0.1,
        "net_therm": 0.0, "tsb": tsb,
    }  # fmt: skip


def test_the_13th_month_report_is_the_confirmed_claims(filer, year, pge, programs, monkeypatch):
    assert not service.closing(pge, 2028).claimed  # no confirmed claims yet
    monkeypatch.setattr(submissions, "_claims_totals", fake_claims([claimed("PGE_Res_HVAC", 600, quarters=(1, 2), final=False)]))
    c = service.closing(pge, 2028)
    assert c.claimed and not c.final and c.quarters == [1, 2] and c.december is None and not c.unreconciled
    december = report("202812", measures=[], costs=[cost(), cost("PGE_Com_Ltg", admin=200)])
    service.confirm(filer, priced(filer, pge, 12, december))
    totals = [claimed("PGE_Res_HVAC", 600), claimed("PGE_Com_Ltg", 250), claimed("PGE_Com_Ltg", 10)]  # a program on two PAs' rows
    monkeypatch.setattr(submissions, "_claims_totals", fake_claims(totals))
    c = service.closing(pge, 2028)
    assert c.final and c.december is not None
    rows = {r.prg_id: r for r in c.rows}
    assert rows["PGE_Res_HVAC"].reconciles and rows["PGE_Com_Ltg"].get("expenditures") == 260
    assert rows["PGE_Com_Ltg"].spend_difference == 60 and [r.prg_id for r in c.unreconciled] == ["PGE_Com_Ltg"]
    assert c.total.get("expenditures") == 860 and c.total.get("tsb") == 2700 and c.total.records == 9  # matches the claims' totals
    assert service.closing(pge, 2027).rows == []


def test_the_13th_month_report_reads_the_claims_module(filer, year, pge, programs, pge_admin, cpuc_admin):
    """Claims' own totals through the submissions service: a confirmed Q1 gives a provisional report."""
    from unittest import mock

    from cedars.modules.claims import service as claims

    from .test_budgets import group_sums
    from .test_claims import claim, claims_zip, row, tables

    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, filer, pge, p)
    due = {kind: timezone.now() + timedelta(days=30 * (i + 1)) for i, (kind, _) in enumerate(claims.PERIODS)}
    claims.add_year(cpuc_admin, year=2028, acc_version="2026", due=due)
    first = tables(
        claim("PGE-2028-001", prg="PGE_Res_HVAC"),
        ProgramCost=[row("ProgramCost", PrgID="PGE_Res_HVAC")],
        Site=[row("Site", SiteID="PGE-S1")],
        ContactClaim=[row("ContactClaim", ClaimID="PGE-2028-001", ContactID="PGE-C1")],
    )
    with mock.patch.object(claims, "_rule_findings", return_value=([], [], "")):
        v = claims.upload_claims(filer, claims.periods(2028)[0], pge, claims_zip(first, full=True), "q1.zip")
    key = "runs/claims/out/_results/group_sums2_measure_level_run.parquet"
    storage.store("results").put_bytes(key, group_sums())
    details = {"job_sha256": v.findings["job_sha256"], "acc": "2026", "results": {"group_sums": key}}
    claims.confirm(filer, submissions.record_calculation(None, v, run="claims", input_hash="h", details=details))
    c = service.closing(pge, 2028)
    assert c.quarters == [1] and not c.final
    expected = {t["prg_id"]: t["expenditures"] for t in submissions.claims_totals(pge, 2028)}
    assert {r.prg_id: r.get("expenditures") for r in c.rows if r.claimed} == expected and expected


# --- pages


def test_the_pages(client, filer, year, pge, programs, release, cpuc_admin, make_user):  # noqa: F811

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
    assert csv.splitlines()[0].startswith("PrgID,ProgramName,InBCAInputs") and "EstimatedTotalSystemBenefit" in csv
    assert client.get(reverse("monthly:file", args=[v.pk, 0])).status_code == 200
    assert client.get(reverse("monthly:year", args=[2028])).status_code == 200
    assert b"Not available yet" in client.get(reverse("monthly:closing", args=[2028])).content
    assert client.get(reverse("monthly:closing_csv", args=[2028])).content.decode().startswith("PrgID,ProgramName,Final,Records")
    assert client.get(reverse("monthly:year_csv", args=[2028])).content.decode().splitlines()[1].startswith("202801,1,published,1200")
    community = make_user("someone@example.com")
    client.logout()
    sign_in(client, community)
    assert client.get(reverse("monthly:version", args=[v.pk])).status_code == 200
    assert client.get(reverse("monthly:findings_csv", args=[v.pk])).status_code == 404
    assert client.get(reverse("monthly:report", args=[jan.pk]) + "?org=PGE").status_code == 404
    year_page = client.get(reverse("monthly:year", args=[2028]) + "?org=PGE").content.decode()
    assert 'class="status ok">Published</span>' in year_page and "version 1</a>" in year_page
    assert client.get(reverse("monthly:year_new")).status_code == 404
    client.logout()
    sign_in(client, cpuc_admin)
    assert client.get(reverse("monthly:report", args=[jan.pk]) + "?org=PGE").status_code == 200
    assert client.get(reverse("monthly:setup", args=[2028])).status_code == 200
    r = client.post(reverse("monthly:year_new"), {"year": 2029, "due_day": 15, "acc_version": "2026"})
    assert r.status_code == 302 and service.months(2029).count() == 12


def test_a_monthly_report_in_several_files(client, filer, year, pge, programs):
    sign_in(client, filer)
    jan = month(1)
    measures = ["M1|PGE_Res_HVAC|2028Q1|10|40|0|0|0", "M2|PGE_Res_HVAC|2028Q1|1|0|0|0|0"]
    client.post(reverse("monthly:upload", args=[jan.pk]), {"file": split(report(measures=measures))})
    v = SubmissionVersion.objects.get()
    assert v.status == "validated", v.findings
    assert v.files[0]["uploaded_as"] == "combined upload (4 files).zip" and len(v.files[0]["sources"]) == 4
    assert v.files[1]["measures"] == 2


def test_a_pricing_run_that_ends_without_pricing_lets_the_report_be_priced_again(client, filer, year, pge, programs, release):  # noqa: F811
    from cedars.modules.calculations import service as calculations

    v = service.upload(filer, month(1), pge, report(), "january.zip")
    run = Run.objects.get(pk=service.price(filer, v))
    sign_in(client, filer)
    url = reverse("monthly:report", args=[month(1).pk])
    html = client.get(url).content.decode()
    assert "the run's page" in html and "Price the estimates" not in page_only(html)
    calculations.finish(
        run, exit_code=2, error="recet worker: job refused: engine.release 0.1.80 was asked for; this worker is recet 0.1.83"
    )
    html = client.get(url).content.decode()
    assert "refused by the engine" in html and "this worker is recet 0.1.83" in html and "Price the estimates" in html
    assert submissions.pricing_run(v)["ended"] and not service.priced(v)
    again = Run.objects.get(pk=service.price(filer, v))
    assert again.pk != run.pk and not submissions.pricing_run(v)["ended"]

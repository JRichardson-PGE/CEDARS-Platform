from datetime import date
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from cedars.modules.budgets import legacy
from cedars.modules.budgets import service as budgets
from cedars.modules.budgets.models import BudgetLine, BudgetResult
from cedars.services.accounts.models import Organisation
from cedars.services.legacy import service as staging
from cedars.services.legacy.models import LegacyImport
from cedars.services.registry import service as registry
from cedars.services.registry.models import Program
from cedars.services.submissions.models import SubmissionVersion

from .legacy_fixtures import csv_text, filing_row, manifest_row, manifest_text, write_zip

pytestmark = pytest.mark.django_db


def _application_rows(years=(2028, 2029, 2030, 2031), **override):
    rows = {}
    for y in years:
        rows[y] = [
            filing_row(f"PGE-{y}-PGE_Res_001", "PGE", "PGE_Res_001", y),
            filing_row(f"PGE-{y}-PGE_Res_001-2", "PGE", "PGE_Res_001", y, units=3.0, admin=20.0, marketing=5.0),
            filing_row(f"BAY-{y}-BAY_Res_01", "BAY", "BAY_Res_01", y, **override),
        ]
    return rows


@pytest.fixture
def downloads(tmp_path):
    """The 2028-2031 application as it stands (four all-PA files, with PG&E's per-PA files) and its archived snapshot."""

    def make(**override):
        folder = tmp_path / "downloads"
        files = []
        for y, rows in _application_rows(**override).items():
            files.append(
                (
                    "filings",
                    str(y),
                    "all",
                    write_zip(folder / f"filings/{y}/all_pas_filings_records_{y}.zip", {f"all_{y}.csv": csv_text("filings", rows)}),
                )
            )
            pge = [r for r in rows if r["PA"] == "PGE"]
            files.append(
                (
                    "filings",
                    str(y),
                    "PGE",
                    write_zip(folder / f"filings/{y}/pge_filings_records_{y}.zip", {f"pge_{y}.csv": csv_text("filings", pge)}),
                )
            )
        snapshot = {f"archived_{y}.csv": csv_text("filings", rows[:1]) for y, rows in _application_rows().items()}
        files.append(
            (
                "filings_archive",
                "2028_2031",
                "all",
                write_zip(folder / "filings_archive/2028_2031_archived_filings_record.zip", snapshot, macos_junk=True),
            )
        )
        manifest = manifest_text([manifest_row(*f) for f in files])
        report = staging.stage(None, manifest, folder)
        assert report.ok, report.lines()
        return report

    return make


def _version(code, kind="Business plan application", number=None):
    qs = SubmissionVersion.objects.filter(submission__organisation__code=code, submission__deadline__kind=kind).order_by("-number")
    return qs.get(number=number) if number else qs.first()


def test_the_application_loads_as_imported_versions_with_lines_and_results(downloads):
    downloads()
    snap = legacy.load(legacy.BY_KEY["2028-2031-application-snapshot"])
    assert snap.published == ["PGE"] and snap.refused == []  # the snapshot holds PG&E's first rows only
    out = legacy.load(legacy.BY_KEY["2028-2031-application"])
    assert out.published == ["BAY", "PGE"] and out.refused == [] and out.programs_added == 1  # BAY_Res_01; PG&E's came with the snapshot

    v1, v2 = _version("PGE", number=1), _version("PGE", number=2)
    assert v1.imported and v2.imported and v2.reason.startswith("Re-imported: the legacy CEDARS download changed.")
    deadline = v2.submission.deadline
    assert (str(deadline.cycle), deadline.budget_setup.acc_version, timezone.localtime(deadline.due_at).date()) == (
        "2028-2031",
        "2024",
        date(2026, 3, 1),
    )
    assert {f["name"] for f in v2.files} >= {"Measure.parquet", "Outputs.parquet", "Programs.parquet", "reconciliation.json"}
    assert v2.calculation["run"] == "legacy-cedars" and v2.findings["records"] == 8

    line = BudgetLine.objects.get(version=v2, prg_id="PGE_Res_001", year=2029)
    assert line.program_costs["AdminCostsOverheadAndGA"] == "120.0"
    assert Decimal(line.program_costs["MarketingOutreach"]) == 55 and Decimal(line.program_costs["EMV"]) == 0
    assert line.rebates_and_incentives == Decimal("50.00") and line.measures == 2 and line.pa == "PGE"
    result = BudgetResult.objects.get(version=v2, prg_id="PGE_Res_001", year=2029)
    assert result.run == "legacy-cedars" and result.values["TotalSystemBenefit"] == 1001.0
    assert result.values["TRCBen"] == 1000.0 and "TRCRatio" not in result.values and "LevTRCCost" not in result.values

    t = budgets.table(v2, years=[2029])
    row = next(r for r in t.rows if r.prg_id == "PGE_Res_001")
    assert row.budget == Decimal("225.00")  # 175 program costs + 50 rebates
    assert row.value("TRC") == pytest.approx(2.5) and t.total.value("TotalSystemBenefit") == pytest.approx(1001.0)

    rec = LegacyImport.objects.get(version=v2).reconciliation
    assert {c["name"]: c["ok"] for c in rec["checks"]} == {
        "per-PA file rows": True,
        "numbers readable": True,
        "rows written": True,
        "additive outputs": True,
        "weighted costs = Weighted Program Cost": True,
        "TRCBen = ratio x cost": True,
        "PACBen = ratio x cost": True,
        "RIMBen = ratio x cost": True,
        "budget lines = Budget": True,
        "ratios without a cost": True,
    }
    # programs: the registry got a version per run of years
    p = Program.objects.get(prg_id="BAY_Res_01")
    assert p.administrator.code == "BAY" and Organisation.objects.get(code="BAY").pa_type == "REN"
    (pv,) = p.versions.all()
    assert (pv.valid_from, pv.valid_to, pv.segment, pv.flags) == (
        date(2028, 1, 1),
        date(2031, 12, 31),
        "Resource Acquisition",
        {"Resource_Flag": "True"},
    )

    # re-running loads nothing new
    again = legacy.load(legacy.BY_KEY["2028-2031-application"])
    assert again.unchanged == ["BAY", "PGE"] and again.published == []
    assert legacy.load(legacy.BY_KEY["2028-2031-application-snapshot"]).unchanged == ["PGE"]
    assert SubmissionVersion.objects.count() == 3


def test_the_registrys_own_history_is_kept(downloads, pge):
    registry.create_program(None, prg_id="PGE_Res_001", administrator=pge, valid_from=date(2030, 1, 1), name="Today's name", reason="test")
    downloads()
    legacy.load(legacy.BY_KEY["2028-2031-application"], only="PGE")
    versions = list(Program.objects.get(prg_id="PGE_Res_001").versions.order_by("valid_from"))
    assert [(v.valid_from, v.valid_to, v.name) for v in versions] == [
        (date(2028, 1, 1), date(2029, 12, 31), "Example program PGE_Res_001"),
        (date(2030, 1, 1), None, "Today's name"),
    ]
    assert not SubmissionVersion.objects.filter(submission__organisation__code="BAY").exists()


def test_a_pa_failing_a_hard_check_is_not_published(downloads, tmp_path):
    downloads(**{"Weighted Program Cost": "999.0"})
    out = legacy.load(legacy.BY_KEY["2028-2031-application"])
    assert out.published == ["PGE"]
    assert out.refused == [("BAY", ["hard check failed: weighted costs = Weighted Program Cost"])]
    assert not SubmissionVersion.objects.filter(submission__organisation__code="BAY").exists()
    with pytest.raises(CommandError):
        call_command("load_legacy_filings", "--filing", "2028-2031-application")


def test_text_where_a_number_belongs_is_refused(downloads):
    downloads(**{"Total System Benefit": "n/a"})
    out = legacy.load(legacy.BY_KEY["2028-2031-application"], only="BAY")
    assert out.refused[0][0] == "BAY" and "TotalSystemBenefit holds a value that is not a number" in out.refused[0][1][0]


def test_archived_layouts_rebuild_the_benefits_from_ratio_and_cost(tmp_path):
    folder = tmp_path / "d"
    rows = [
        filing_row("SCE-2024-SCE_1", "SCE", "SCE_1", 2024),
        filing_row("SCE-2024-SCE_2", "SCE", "SCE_1", 2024, **{"TRC Cost": "0", "TRC": "1.5"}),
    ]
    z = write_zip(folder / "filings_archive/2024_2027_application_filings_record.zip", {"f.csv": csv_text("filings_2024_2027", rows)})
    staging.stage(None, manifest_text([manifest_row("filings_archive", "2024_2027", "all", z)]), folder)
    out = legacy.load(legacy.BY_KEY["2024-2027-application"])
    assert out.published == ["SCE"]
    v = _version("SCE")
    assert str(v.submission.deadline.cycle) == "2024-2027" and v.submission.deadline.budget_setup.acc_version == "2021"
    result = BudgetResult.objects.get(version=v)
    assert result.values["TRCBen"] == 500.0 and result.values["PACBen"] == 800.0  # 2.5 x 200 (+ 1.5 x 0); 4 x 100 twice
    checks = {c["name"]: c for c in LegacyImport.objects.get(version=v).reconciliation["checks"]}
    assert checks["ratios without a cost"]["detail"] == {"TRCBen": 1} and "TRCBen = ratio x cost" not in checks


def test_annual_filings_get_one_year_cycles_and_a_filing_not_staged_is_reported(tmp_path, capsys):
    folder = tmp_path / "d"
    z = write_zip(
        folder / "filings/2019/all_pas_filings_records_2019.zip",
        {"f.csv": csv_text("filings", [filing_row("SCG-2019-1", "SCG", "SCG_1", 2019)])},
    )
    staging.stage(None, manifest_text([manifest_row("filings", "2019", "all", z)]), folder)
    call_command("load_legacy_filings", "--filing", "2019")
    assert "2019: published 1 (SCG)" in capsys.readouterr().out
    v = _version("SCG", kind="Annual budget advice letter (ABAL)")
    assert str(v.submission.deadline.cycle) == "2019-2019" and v.submission.deadline.budget_setup.acc_version == ""
    assert Organisation.objects.get(code="SCG").pa_type == "IOU"
    with pytest.raises(CommandError):
        call_command("load_legacy_filings", "--filing", "2020")
    assert "2020: not loaded: 2020: the all-PA file for filings 2020 is not staged" in capsys.readouterr().out


def test_the_pages_show_a_legacy_filing_as_imported_and_read_only(downloads, signed_in, pge_admin, cpuc_admin):
    downloads()
    legacy.load(legacy.BY_KEY["2028-2031-application"], only="PGE")
    v = _version("PGE")
    client = signed_in(pge_admin)
    page = client.get(f"/budgets/versions/{v.pk}/").content.decode()
    assert "Imported from legacy CEDARS" in page and "CEDARS' own CET outputs, as filed (2024 ACC)" in page
    assert "https://cedars.example.org/reports/download-record-level-report/all/2028/filings/" in page
    assert "Reconciliation report (JSON)" in page and "TRCBen = ratio x cost" in page
    filing = signed_in(cpuc_admin).get(f"/budgets/filings/{v.submission.deadline.pk}/?org=PGE").content.decode()
    assert "Imported from legacy CEDARS, 8 records" in filing and "Ask to reopen" not in filing and "records checked" not in filing
    index = client.get("/budgets/").content.decode()
    assert "Imported from legacy CEDARS" in index and "imported from legacy CEDARS" in index

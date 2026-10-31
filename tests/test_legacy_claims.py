from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from cedars.modules.claims import legacy
from cedars.modules.claims import service as claims
from cedars.modules.claims.models import ClaimProgramLine, ClaimRecord, ClaimResult, ClaimYear
from cedars.services.accounts.models import Organisation
from cedars.services.legacy import service as staging
from cedars.services.legacy.models import LegacyImport
from cedars.services.registry.models import Program
from cedars.services.submissions.models import SubmissionVersion

from .legacy_fixtures import csv_text, legacy_claim, manifest_row, manifest_text, write_zip

pytestmark = pytest.mark.django_db


def _stage(folder, files):
    report = staging.stage(None, manifest_text([manifest_row(*f) for f in files]), folder)
    assert report.ok, report.lines()


def _year(tmp_path, year=2023, *, layout="claims_b", pge=None, sw=True, participants=None, sw_participants=None):
    """A claim year's downloads: the all-PA file (PG&E's two claims and SCE's one, unless `pge` replaces PG&E's), PG&E's per-PA file
    and, with `sw`, the statewide unallocated file."""
    folder = tmp_path / "downloads"
    pge = pge or [
        legacy_claim(f"PGE-{year}-0001", "PGE", "PGE_Com_001", year),
        legacy_claim(f"PGE-{year}-0002", "PGE", "PGE_Com_001", year, **{"Claim Year Quarter": f"{year}Q3"}),
    ]
    rows = [*pge, legacy_claim(f"SCE-{year}-0001", "SCE", "SCE_Res_001", year)]
    members = {f"all_pas_confirmed_claims_records_{year}.csv": csv_text(layout, rows)}
    if participants:
        members[f"all_pas_participant_data_{year}.csv"] = csv_text("participants", participants)
    files = [("claims", str(year), "all", write_zip(folder / f"claims/{year}/all_pas_confirmed_claims_records_{year}.zip", members))]
    files.append(
        (
            "claims",
            str(year),
            "PGE",
            write_zip(folder / f"claims/{year}/pge_confirmed_claims_records_{year}.zip", {f"pge_{year}.csv": csv_text(layout, pge)}),
        )
    )
    if sw:
        sw_members = {
            f"sw_unallocated_{year}.csv": csv_text(layout, [legacy_claim(f"SW-{year}-0001", "SW", "SW_HVAC_Up", year, admin=90.0)])
        }
        if sw_participants:
            sw_members[f"sw_participant_data_{year}.csv"] = csv_text("participants", sw_participants)
        z = write_zip(folder / f"claims/{year}/sw_unallocated_confirmed_claims_records_{year}.zip", sw_members)
        files.append(("claims", str(year), "SW", z))
    _stage(folder, files)


def _version(code, year=2023):
    return SubmissionVersion.objects.get(
        submission__organisation__code=code, submission__deadline__module="claims", submission__deadline__period__year=year
    )


@pytest.fixture(params=[legacy.BATCH_ROWS, 1], ids=["one batch", "a batch per record"])
def batch_rows(request, monkeypatch):
    """Each PA is read in batches: the results must not depend on where the batches split."""
    monkeypatch.setattr(legacy, "BATCH_ROWS", request.param)
    return request.param


def test_a_claim_year_loads_as_imported_annual_claims_with_the_statewide_claims_outside_the_totals(tmp_path, batch_rows):
    _year(tmp_path)
    out = legacy.load(2023)
    assert out.published == ["PGE", "SCE", "SW"] and out.refused == []
    assert ClaimYear.objects.get(year=2023).acc_version == claims.default_acc(2023)

    v = _version("PGE")
    assert v.imported and v.status == SubmissionVersion.Status.PUBLISHED and v.submission.deadline.kind == "Annual (final) claim"
    assert [d.kind for d in claims.periods(2023)] == ["Annual (final) claim"]  # quarters are not rebuilt
    names = {f["name"] for f in v.files}
    assert names >= {"Measure.parquet", "Claim.parquet", "Outputs.parquet", "Programs.parquet", "reconciliation.json"}
    assert v.calculation["run"] == "legacy-cedars" and not v.calculation["outside_totals"]

    assert sorted(ClaimRecord.objects.filter(version=v).values_list("claim_id", flat=True)) == ["PGE-2023-0001", "PGE-2023-0002"]
    rec = ClaimRecord.objects.get(version=v, claim_id="PGE-2023-0001")
    assert rec.prg_id == "PGE_Com_001" and rec.impact_type == "Deem-DEER" and rec.filed["TotalFirstYearNetkWh"] == 100.0
    assert rec.filed["TotalGrossIncentive"] == 20.0
    line = ClaimProgramLine.objects.get(version=v, prg_id="PGE_Com_001")
    assert Decimal(line.program_costs["AdminCostsOverheadAndGA"]) == 60 and line.rebates_and_incentives == Decimal("40.00")
    assert line.claims == 2 and line.pa == "PGE" and line.filed["TotalLifecycleNetkWh"] == 2000.0
    result = ClaimResult.objects.get(version=v, prg_id="PGE_Com_001")
    assert result.run == "legacy-cedars" and result.measures == 2 and result.values["TotalSystemBenefit"] == 100.5
    assert result.values["TRCBen"] == 100.0 and result.values["PACBen"] == 100.0 and "TRCRatio" not in result.values

    t = claims.table(v)
    (row,) = t.rows
    assert row.expenditures == Decimal("100.00") and row.value("TRC") == pytest.approx(1.25) and row.value("PAC") == pytest.approx(2.5)
    assert row.name == "Example program PGE_Com_001"
    totals = claims.year_totals(Organisation.objects.get(code="PGE"), 2023)
    assert [(r["prg_id"], r["records"], r["tsb"], r["final"]) for r in totals] == [("PGE_Com_001", 2, 100.5, True)]

    # the statewide unallocated claims: loaded, with their records and results, but outside every total
    sw = _version("SW")
    imp = LegacyImport.objects.get(version=sw)
    assert (imp.records, imp.outside_totals) == (0, 1) and sw.calculation["outside_totals"]
    assert ClaimRecord.objects.filter(version=sw, claim_id="SW-2023-0001").exists()
    assert claims.table(sw).total.value("TotalSystemBenefit") == pytest.approx(50.25)
    assert claims.year_totals(Organisation.objects.get(code="SW"), 2023) == []
    p = Program.objects.get(prg_id="SW_HVAC_Up")
    assert p.statewide and p.administrator is None

    rec = LegacyImport.objects.get(version=v).reconciliation
    assert rec["quarters"] == ["2023Q2", "2023Q3"]
    assert {c["name"]: c["ok"] for c in rec["checks"]} == {
        "per-PA file rows": True,
        "numbers readable": True,
        "rows written": True,
        "additive outputs": True,
        "weighted costs = Weighted Program Cost": True,
        "TRCBen = ratio x cost": True,
        "PACBen = ratio x cost": True,
        "RIMBen = ratio x cost": True,
        "program lines = Total Expenditures": True,
        "ratios without a cost": True,
        "repeated ClaimIDs": True,
        "records with no ClaimID": True,
    }

    again = legacy.load(2023)
    assert again.unchanged == ["PGE", "SCE", "SW"] and again.published == []
    assert SubmissionVersion.objects.filter(origin="imported").count() == 3


def test_repeated_claim_ids_get_a_suffix_on_their_claim_record(tmp_path, batch_rows):
    rows = [legacy_claim("PGE-2019-1", "PGE", "PGE_1", 2019) for _ in range(3)] + [legacy_claim("", "PGE", "PGE_1", 2019)]
    rows.insert(1, legacy_claim("PGE-2019-2", "PGE", "PGE_2", 2019))
    _year(tmp_path, 2019, layout="claims_a", pge=rows, sw=False)
    assert legacy.load(2019, only="PGE").published == ["PGE"]
    v = _version("PGE", 2019)
    ids = sorted(ClaimRecord.objects.filter(version=v).values_list("claim_id", flat=True))
    assert ids == ["PGE-2019-1", "PGE-2019-1#2", "PGE-2019-1#3", "PGE-2019-2"]
    checks = {c["name"]: c for c in LegacyImport.objects.get(version=v).reconciliation["checks"]}
    assert checks["repeated ClaimIDs"]["count"] == 1 and checks["records with no ClaimID"]["count"] == 1
    assert checks["repeated ClaimIDs"]["examples"] == ["PGE-2019-1"]
    assert {r.prg_id: r.measures for r in ClaimResult.objects.filter(version=v)} == {"PGE_1": 4, "PGE_2": 1}
    programs = next(f for f in v.files if f["name"] == "Programs.parquet")
    assert programs["rows"] == 2
    assert checks["program lines = Budget"]["ok"]  # the 2016-2022 layout's Budget column
    assert "PACBen = ratio x cost" not in checks  # no PAC benefit column before 2023: rebuilt instead
    assert ClaimResult.objects.get(version=v, prg_id="PGE_1").values["PACBen"] == 200.0  # 2.5 x 20, four records
    assert not SubmissionVersion.objects.filter(submission__organisation__code="SCE").exists()


def test_a_load_that_finds_its_pa_published_meanwhile_adds_nothing(tmp_path, monkeypatch):
    """Two loads of the same year at once: the second to reach record_import finds the first's import (record_import serializes them on
    a lock) and adds no second version and no records to the first's."""
    _year(tmp_path)
    legacy.load(2023)
    records = ClaimRecord.objects.count()
    real, asked = staging.already_imported, set()

    def not_yet(deadline, org, sources):  # the load's own first look: as if the first load had not finished when this one began
        if org.pk not in asked:
            asked.add(org.pk)
            return None
        return real(deadline, org, sources)  # record_import's look, under its lock

    monkeypatch.setattr(staging, "already_imported", not_yet)
    out = legacy.load(2023)
    assert out.published == [] and out.unchanged == ["PGE", "SCE", "SW"]
    assert SubmissionVersion.objects.filter(origin="imported").count() == 3 and ClaimRecord.objects.count() == records


def test_a_pa_failing_a_hard_check_is_not_published(tmp_path):
    _year(tmp_path, pge=[legacy_claim("PGE-2023-1", "PGE", "PGE_1", 2023, **{"Weighted Program Cost": "999"})])
    out = legacy.load(2023)
    assert out.published == ["SCE", "SW"]
    assert out.refused == [("PGE", ["hard check failed: weighted costs = Weighted Program Cost"])]
    with pytest.raises(CommandError):
        call_command("load_legacy_claims", "--year", "2023")


def test_from_2023_the_statewide_file_is_required(tmp_path, capsys):
    _year(tmp_path, sw=False)
    with pytest.raises(CommandError):
        call_command("load_legacy_claims", "--year", "2023")
    assert "2023: not loaded: 2023: the statewide unallocated claims file is not staged" in capsys.readouterr().out
    assert not SubmissionVersion.objects.exists()


def test_participant_data_goes_to_the_pa_whose_claims_name_the_program(tmp_path):
    def part(prg, n):
        return {"PrgID": prg, "PrgYear": "2026", "ParticipantType": "Business", "ParticipantCount": str(n), "Comments": "example"}

    _year(
        tmp_path,
        2026,
        participants=[part("PGE_Com_001", 3), part("sce_res_001", 1), part("Nobody_1", 9)],
        sw_participants=[part("SW_HVAC_Up", 2)],
    )
    out = legacy.load(2026)
    assert out.published == ["PGE", "SCE", "SW"] and out.notes == ["1 participant rows name a program in no PA's claims: not loaded"]
    for code, rows in (("PGE", 1), ("SCE", 1), ("SW", 1)):
        f = next(f for f in _version(code, 2026).files if f["name"] == "Participant.parquet")
        assert f["rows"] == rows


def test_the_pages_show_a_legacy_year_as_imported_and_read_only(tmp_path, signed_in, pge_admin, cpuc_admin):
    _year(tmp_path)
    legacy.load(2023)
    v, sw = _version("PGE"), _version("SW")
    client = signed_in(pge_admin)

    page = client.get(f"/claims/versions/{v.pk}/").content.decode()
    assert "Imported from legacy CEDARS" in page and "CEDARS' own CET outputs, as filed" in page
    assert "https://cedars.example.org/reports/download-record-level-report/all/2023/claims/" in page
    assert "weighted costs = Weighted Program Cost" in page and "Passed" in page
    assert "Reconciliation report (JSON)" in page and "Run receipt" not in page
    assert "Outside every total" not in page
    report = client.get(f"/claims/versions/{v.pk}/files/{len(claims.published_files(v)) - 1}")
    assert report.status_code == 200 and b"weighted costs" in b"".join(report.streaming_content)

    assert "Outside every total" in client.get(f"/claims/versions/{sw.pk}/").content.decode()

    period = signed_in(cpuc_admin).get(f"/claims/periods/{v.submission.deadline.pk}/?org=PGE").content.decode()
    assert "Imported from legacy CEDARS, 2 records" in period
    assert "Ask to reopen" not in period and "Un-confirm" not in period and "records checked" not in period
    assert "Imported from legacy CEDARS: each PA's year" in client.get("/claims/").content.decode()

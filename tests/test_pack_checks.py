"""Pack acceptance checks on a page: adding and retiring checks, recording a result from its report, and uploading eTRM permutations."""

import json
from datetime import date
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.refdata import service
from cedars.services.refdata.models import PackVersion, TieOutRequirement

from .conftest import page_only

pytestmark = pytest.mark.django_db
SAMPLE = Path(__file__).parent / "data" / "etrm_permutations_export.csv"


@pytest.fixture
def acc(cpuc_admin, tmp_path):
    (tmp_path / "electric_2026.parquet").write_bytes(b"x" * 100)
    return service.register_pack(
        cpuc_admin,
        kind="acc_electric",
        label="2026 ACC",
        version="v1b",
        acc_year=2026,
        manifest=service.manifest_for(tmp_path),
        storage_uri="file:///packs/acc/",
    )


def report(**kw) -> bytes:
    return json.dumps(kw).encode()


def test_adding_and_retiring_checks_changes_what_drafts_need(cpuc_admin, acc):
    assert [r.name for r in service.outstanding(acc)] == ["reference_measures", "source_workbook"]
    r = service.add_requirement(
        cpuc_admin, kind="acc_electric", name="hour_count", description="Every year has 8,760 hours.", reason="new tie-out step"
    )
    assert "hour_count" in [x.name for x in service.outstanding(acc)] and r.added_by == cpuc_admin
    old = TieOutRequirement.objects.get(kind="acc_electric", name="source_workbook")
    service.retire_requirement(cpuc_admin, old, "replaced by control totals")
    assert [x.name for x in service.outstanding(acc)] == ["hour_count", "reference_measures"]
    assert set(AuditEvent.objects.filter(action__startswith="pack_check.").values_list("action", flat=True)) == {
        "pack_check.added",
        "pack_check.retired",
    }
    with pytest.raises(AccessDenied, match="retired"):
        service.add_requirement(cpuc_admin, kind="acc_electric", name="source_workbook", description="x", reason="y")
    with pytest.raises(AccessDenied, match="already retired"):
        service.retire_requirement(cpuc_admin, old, "again")


def test_a_published_pack_keeps_the_checks_it_was_published_under(cpuc_admin, acc):
    for check in ("source_workbook", "reference_measures"):
        service.record_tie_out(cpuc_admin, acc, check=check, passed=True)
    service.publish_pack(cpuc_admin, acc)
    acc.refresh_from_db()
    assert acc.checks_at_publication == ["reference_measures", "source_workbook"]
    service.add_requirement(cpuc_admin, kind="acc_electric", name="hour_count", description="8,760 hours.", reason="new")
    service.retire_requirement(cpuc_admin, TieOutRequirement.objects.get(kind="acc_electric", name="source_workbook"), "gone")
    assert service.outstanding(acc) == []
    assert [r.name for r in service.required_checks(acc)] == ["reference_measures", "source_workbook"]


def test_only_system_administrators_change_checks(pge_admin):
    with pytest.raises(AccessDenied):
        service.add_requirement(pge_admin, kind="deer", name="x_check", description="x", reason="y")
    with pytest.raises(AccessDenied, match="lower-case"):
        service.add_requirement(None, kind="deer", name="Bad Name", description="x", reason="y")


def test_a_report_gives_the_result(cpuc_admin, acc):
    sha = next(iter(acc.manifest.values()))["sha256"]
    engine = {
        "sha256": sha,
        "engine_release": "0.1.93",
        "status": "PASS",
        "parts": [
            {"name": "structure", "status": "PASS"},
            {"name": "reference measures", "status": "PASS", "values": [{"relative_difference": 2e-13}, {"relative_difference": -5e-13}]},
        ],
    }
    t = service.record_report(cpuc_admin, acc, check="reference_measures", data=json.dumps(engine).encode(), file_name="tieout.json")
    assert t.passed and t.engine_release == "0.1.93" and t.largest_difference == 5e-13
    assert t.summary == "structure: PASS; reference measures: PASS" and t.report["_file"] == "tieout.json"
    t = service.record_report(cpuc_admin, acc, check="source_workbook", data=report(passed=False, summary="2031 short"))
    assert not t.passed and t.summary == "2031 short"
    t = service.record_report(cpuc_admin, acc, check="source_workbook", data=report(passed=True, precision=1e-9, largest_difference=1e-6))
    assert not t.passed  # over its own precision
    with pytest.raises(AccessDenied, match="does not say whether"):
        service.record_report(cpuc_admin, acc, check="source_workbook", data=report(result="ok"))
    with pytest.raises(AccessDenied, match="not a JSON"):
        service.record_report(cpuc_admin, acc, check="source_workbook", data=b"PASS")
    with pytest.raises(AccessDenied, match="not in this pack"):
        service.record_report(cpuc_admin, acc, check="source_workbook", data=report(passed=True, sha256="0" * 64))
    with pytest.raises(AccessDenied, match="not a check this pack needs"):
        service.record_report(cpuc_admin, acc, check="profile_checks", data=report(passed=True))


def test_the_pages(signed_in, cpuc_admin, pge_admin, acc):
    member = signed_in(pge_admin)
    page = member.get("/reference-data/packs/checks/").content.decode()
    assert "source_workbook" in page and "Add a check" not in page_only(page)
    assert (
        member.post("/reference-data/packs/checks/", {"kind": "deer", "name": "x_y", "description": "d", "reason": "r"}).status_code == 200
    )
    assert not TieOutRequirement.objects.filter(name="x_y").exists()
    c = signed_in(cpuc_admin)
    r = c.post("/reference-data/packs/checks/", {"kind": "deer", "name": "x_y", "description": "Matches DEER.", "reason": "new"})
    assert r.status_code == 302 and TieOutRequirement.objects.filter(kind="deer", name="x_y").exists()
    req = TieOutRequirement.objects.get(name="x_y")
    assert c.post(f"/reference-data/packs/checks/{req.pk}/retire", {"reason": "not needed"}).status_code == 302
    req.refresh_from_db()
    assert req.retired_reason == "not needed"
    page = c.get(f"/reference-data/{acc.pk}/").content.decode()
    assert "Record a check" in page
    f = SimpleUploadedFile("r.json", report(passed=True, summary="all years"), content_type="application/json")
    r = c.post(f"/reference-data/{acc.pk}/record-check", {"check": "source_workbook", "report": f})
    assert r.status_code == 302 and acc.tie_outs.get().summary == "all years"


def test_uploading_etrm_permutations(signed_in, cpuc_admin, pge_admin):
    assert signed_in(pge_admin).get("/reference-data/packs/etrm-permutations/").status_code == 404
    c = signed_in(cpuc_admin)
    assert c.get("/reference-data/packs/etrm-permutations/").status_code == 200
    data = SAMPLE.read_bytes()
    r = c.post(
        "/reference-data/packs/etrm-permutations/",
        {
            "files": [SimpleUploadedFile("SWHC052.csv", data, content_type="text/csv")],
            "label": "eTRM",
            "version": "2026-11-10",
            "valid_from": "2027-01-01",
        },
    )
    pack = PackVersion.objects.get(kind="etrm_permutations")
    assert r.status_code == 302 and r.url == f"/reference-data/{pack.pk}/"
    assert pack.etrm_permutations.count() == 3 and pack.valid_from == date(2027, 1, 1) and "SWHC052.csv" in pack.manifest
    r = c.post(
        "/reference-data/packs/etrm-permutations/",
        {"files": [SimpleUploadedFile("x.xlsx", b"x")], "label": "eTRM", "version": "again"},
    )
    assert r.status_code == 200 and "is not a CSV file" in r.content.decode()

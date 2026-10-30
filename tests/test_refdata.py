from datetime import date

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.refdata import service
from cedars.services.refdata.models import PackVersion

pytestmark = pytest.mark.django_db


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "hourly").mkdir()
    (tmp_path / "hourly" / "electric_2026.parquet").write_bytes(b"x" * 1000)
    (tmp_path / "gas_2026.csv").write_text("year,month,value\n2028,1,1.0\n")
    return tmp_path


@pytest.fixture
def acc(cpuc_admin, folder):
    return service.register_pack(
        cpuc_admin,
        kind="acc_electric",
        label="2026 ACC",
        version="v1b",
        acc_year=2026,
        valid_from=date(2027, 1, 1),
        manifest=service.manifest_for(folder),
        storage_uri="s3://cedars-refdata/acc/2026-v1b/",
        source="2026 ACC Electric Model v1b.xlsb",
    )


def _tie_outs(actor, pack):
    service.record_tie_out(actor, pack, check="source_workbook", passed=True, precision=1e-9, largest_difference=0)
    service.record_tie_out(actor, pack, check="reference_measures", passed=True, precision=1e-9, largest_difference=3e-12)


def test_manifest_and_fingerprint(folder, acc):
    assert sorted(acc.manifest) == ["gas_2026.csv", "hourly/electric_2026.parquet"]
    assert acc.manifest["hourly/electric_2026.parquet"]["bytes"] == 1000 and acc.bytes == 1000 + 28
    assert acc.fingerprint == service.fingerprint(dict(reversed(list(acc.manifest.items()))))  # order does not matter
    assert service.verify(acc, folder) == []
    (folder / "gas_2026.csv").write_text("changed")
    (folder / "extra.txt").write_text("x")
    assert service.verify(acc, folder) == ["gas_2026.csv has changed (SHA-256 differs)", "extra.txt is not in the manifest"]


def test_a_draft_is_for_analysis_only_until_its_tie_outs_pass(cpuc_admin, acc):
    assert service.usable(acc, "analysis") == (True, "")
    ok, why = service.usable(acc, "filing")
    assert not ok and "draft" in why
    assert service.in_force("acc_electric", date(2028, 1, 1)) is None
    assert service.in_force("acc_electric", date(2028, 1, 1), include_drafts=True) == acc
    with pytest.raises(AccessDenied, match="reference_measures, source_workbook"):
        service.publish_pack(cpuc_admin, acc)
    service.record_tie_out(cpuc_admin, acc, check="source_workbook", passed=True)
    t = service.record_tie_out(cpuc_admin, acc, check="reference_measures", passed=True, precision=1e-9, largest_difference=2e-6)
    assert not t.passed  # over the stated precision is a failure whatever the script said
    with pytest.raises(AccessDenied, match="reference_measures"):
        service.publish_pack(cpuc_admin, acc)
    service.record_tie_out(cpuc_admin, acc, check="reference_measures", passed=True, precision=1e-9, largest_difference=1e-12)
    service.publish_pack(cpuc_admin, acc)
    acc.refresh_from_db()
    assert acc.status == "published" and service.usable(acc, "filing") == (True, "")
    assert service.in_force("acc_electric", date(2028, 1, 1)) == acc
    assert service.in_force("acc_electric", date(2026, 6, 1)) is None  # not yet in force
    assert list(AuditEvent.objects.filter(object_type="refdata.packversion").order_by("id").values_list("action", flat=True)) == [
        "pack.registered",
        "pack.tie_out",
        "pack.tie_out",
        "pack.tie_out",
        "pack.published",
    ]


def test_a_published_pack_never_changes_and_is_withdrawn_not_deleted(cpuc_admin, acc):
    _tie_outs(cpuc_admin, acc)
    service.publish_pack(cpuc_admin, acc)
    with pytest.raises(DatabaseError, match="does not change"), transaction.atomic():
        PackVersion.objects.filter(pk=acc.pk).update(manifest={"other": {"sha256": "0"}})
    with pytest.raises(DatabaseError, match="never deleted"), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM refdata_tieout WHERE pack_id = %s", [acc.pk])  # tie-outs may go; the pack may not
        cursor.execute("DELETE FROM refdata_packversion WHERE id = %s", [acc.pk])
    with pytest.raises(AccessDenied, match="kept for good"):
        service.discard_draft(cpuc_admin, acc)
    with pytest.raises(AccessDenied, match="reason"):
        service.withdraw_pack(cpuc_admin, acc, "")
    service.withdraw_pack(cpuc_admin, acc, "superseded by v2 after the Resolution's correction")
    acc.refresh_from_db()
    ok, why = service.usable(acc, "analysis")
    assert not ok and "superseded" in why
    assert service.pin([acc]) == {
        "acc_electric": {"id": acc.pk, "label": "2026 ACC", "version": "v1b", "fingerprint": acc.fingerprint, "status": "withdrawn"}
    }


def test_registering_needs_a_manifest_and_a_new_version(cpuc_admin, acc):
    with pytest.raises(AccessDenied, match="already registered"):
        service.register_pack(cpuc_admin, kind="acc_electric", label="2026 ACC", version="v1b", manifest=acc.manifest, storage_uri="x")
    with pytest.raises(AccessDenied, match="SHA-256"):
        service.register_pack(cpuc_admin, kind="acc_electric", label="2026 ACC", version="v2", manifest={}, storage_uri="x")
    draft = service.register_pack(cpuc_admin, kind="acc_electric", label="2026 ACC", version="v2", manifest=acc.manifest, storage_uri="x")
    service.discard_draft(cpuc_admin, draft, "loaded by mistake")
    assert not PackVersion.objects.filter(version="v2").exists()


def test_who_manages_packs(cpuc_admin, cpuc, pge_admin, make_user, acc):
    with pytest.raises(AccessDenied, match="system administrator"):
        service.register_pack(pge_admin, kind="value_lists", label="lists", manifest=acc.manifest, storage_uri="x")
    profiles_admin = make_user("profiles@cpuc.ca.gov")
    access.grant(cpuc_admin, profiles_admin, cpuc, "impact_profiles.admin")
    library = service.register_pack(
        profiles_admin, kind="impact_profiles", label="Impact profiles", version="export 12", manifest=acc.manifest, storage_uri="x"
    )
    with pytest.raises(AccessDenied):
        service.publish_pack(profiles_admin, acc)  # an ACC is not the Impact profiles module's
    service.record_tie_out(None, library, check="profile_checks", passed=True)  # a system job
    service.publish_pack(profiles_admin, library)
    assert service.in_force("impact_profiles") == library


def test_commands(folder, cpuc_admin, tmp_path_factory):
    report = tmp_path_factory.mktemp("reports") / "report.json"
    report.write_text('{"rows": 23300000}')
    call_command("register_pack", str(folder), kind="run_parameters", label="Run parameters", pack_version="2026-10")
    pack = PackVersion.objects.get(kind="run_parameters")
    assert pack.storage_uri.startswith("file://") and pack.files == 2
    call_command("register_pack", str(folder / "gas_2026.csv"), kind="acc_gas", label="2026 gas ACC")
    assert list(PackVersion.objects.get(kind="acc_gas").manifest) == ["gas_2026.csv"]
    call_command("record_tie_out", str(pack.pk), "--check", "anything", "--passed", "--report", str(report))
    assert pack.tie_outs.get().report == {"rows": 23300000}


def test_pages(signed_in, cpuc_admin, pge_admin, acc):
    page = signed_in(pge_admin).get("/reference-data/").content.decode()
    assert "2026 ACC" in page and "Draft (analysis only)" in page
    detail = signed_in(pge_admin).get(f"/reference-data/{acc.pk}/").content.decode()
    assert "hourly/electric_2026.parquet" in detail and "Publish" not in detail
    signed_in(pge_admin).post(f"/reference-data/{acc.pk}/", {"action": "discard"})
    assert PackVersion.objects.filter(pk=acc.pk).exists()  # not theirs to manage
    c = signed_in(cpuc_admin)
    assert (
        "Publishing needs a passed tie-out for: reference_measures, source_workbook" in c.get(f"/reference-data/{acc.pk}/").content.decode()
    )
    _tie_outs(cpuc_admin, acc)
    c.post(f"/reference-data/{acc.pk}/", {"action": "publish"})
    acc.refresh_from_db()
    assert acc.status == "published"
    assert "2026 ACC v1b" in c.get("/reference-data/?kind=acc_electric").content.decode()

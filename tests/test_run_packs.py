import pytest
from django.test import Client

from cedars.modules.calculations import service
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.refdata import service as refdata

from .conftest import sign_in
from .test_calculations import analyst, job_zip, release, request  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
SHA = {n: c * 64 for n, c in (("a", "a"), ("b", "b"), ("c", "c"), ("d", "d"), ("e", "e"))}


def pack(admin, kind, label, version, manifest, *, acc_year=None, publish=True):
    p = refdata.register_pack(
        admin, kind=kind, label=label, version=version, acc_year=acc_year, manifest=manifest, storage_uri=f"s3://refdata/{label}/{version}/"
    )
    if publish:
        for r in refdata.outstanding(p):
            refdata.record_tie_out(admin, p, check=r.name, passed=True)
        p = refdata.publish_pack(admin, p)
    return p


def acc_2024(admin, version="v1", publish=True):
    manifest = {
        "acc/2024/electric_hourly.parquet": {"sha256": SHA["a"], "bytes": 10},
        "acc/2024/README.md": {"sha256": SHA["b"], "bytes": 1},
        "hourly/electric.parquet": {"sha256": SHA["c"], "bytes": 10},  # not a reference-folder path: recorded, not pinned
    }
    return pack(admin, "acc_electric", "2024 ACC", version, manifest, acc_year=2024, publish=publish)


def test_without_registered_packs_a_run_uses_the_workers_folder_unpinned(analyst, pge, release, tmp_path):  # noqa: F811
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    assert "packs" not in run.spec and "packs" not in run.spec["context"]
    c = Client()
    sign_in(c, analyst)
    assert "no data pack registered to pin" in c.get(f"/calculations/runs/{run.pk}/").content.decode()


def test_a_run_is_pinned_to_its_acc_pack_and_the_profile_library(analyst, pge, release, cpuc_admin, tmp_path):  # noqa: F811
    before = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    acc = acc_2024(cpuc_admin)
    pack(cpuc_admin, "acc_electric", "2026 ACC", "v1b", {"acc/2026/electric_hourly.parquet": {"sha256": SHA["d"]}}, acc_year=2026)
    profiles = pack(
        cpuc_admin, "impact_profiles", "Impact-profile library", "2026-10", {"impact_profiles/hourly.parquet": {"sha256": SHA["e"]}}
    )
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    assert run.spec["packs"] == {
        "files": {
            "acc/2024/README.md": {"sha256": SHA["b"], "version": "2024 ACC v1"},
            "acc/2024/electric_hourly.parquet": {"sha256": SHA["a"], "version": "2024 ACC v1"},
            "impact_profiles/hourly.parquet": {"sha256": SHA["e"], "version": "Impact-profile library 2026-10"},
        }
    }
    pins = run.spec["context"]["packs"]
    assert pins["acc_electric"]["fingerprint"] == acc.fingerprint and pins["impact_profiles"]["id"] == profiles.pk
    assert run.input_hash != before.input_hash  # the same job on pinned data is not the same job
    e = AuditEvent.objects.filter(action="calculation_run.queued").order_by("pk").last()
    assert e.after["packs"] == {"acc_electric": "2024 ACC v1", "impact_profiles": "Impact-profile library 2026-10"}

    try:
        from recet import jobspec
    except ImportError:
        pass
    else:
        assert jobspec.validate(run.spec) == []  # the engine's own rules for packs.files

    c = Client()
    sign_in(c, analyst)
    page = c.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "2024 ACC v1" in page and "Impact-profile library 2026-10" in page


def test_drafts_price_analysis_runs_and_never_filings(analyst, pge, release, cpuc_admin, tmp_path):  # noqa: F811
    draft = acc_2024(cpuc_admin, publish=False)
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    assert run.spec["context"]["packs"]["acc_electric"]["status"] == "draft"
    with pytest.raises(AccessDenied, match="is a draft"):
        service.packs_for(request(release), filing=True)

    published = acc_2024(cpuc_admin, version="v2")  # once one is published, it wins over the newer-registered draft
    newest_draft = acc_2024(cpuc_admin, version="v3", publish=False)
    assert [p.pk for p in service.packs_for(request(release), filing=True)] == [published.pk]
    assert [p.pk for p in service.packs_for(request(release), filing=False)] == [published.pk]

    refdata.withdraw_pack(cpuc_admin, published, "wrong weather year")
    assert [p.pk for p in service.packs_for(request(release), filing=False)] == [newest_draft.pk]  # withdrawn: never used again
    assert draft.pk != newest_draft.pk


def test_a_replica_is_pinned_to_none(analyst, pge, release, cpuc_admin, tmp_path):  # noqa: F811
    acc_2024(cpuc_admin)
    run = service.create_run(analyst, pge, request(release, calculation="cet-25.1"), job_zip(tmp_path))
    assert "packs" not in run.spec and "packs" not in run.spec["context"]

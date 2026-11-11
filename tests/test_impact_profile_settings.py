"""Impact profiles' policy settings: the IOU territories and the load-shape start date, set by CPUC administrators on a page."""

from datetime import date

import pyarrow.parquet as pq
import pytest
from django.core.files.storage import default_storage

from cedars.modules.impact_profiles import custom, etrm, generation, library, policy
from cedars.modules.impact_profiles.models import LibrarySettings, MeasurePackage
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent

from .test_impact_profiles import FIXTURE, leds_series, load_shape_csv, profile_file, upload

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.IMPACT_PROFILES_INLINE_JOBS = False


@pytest.fixture
def developer(make_user, pge, pge_admin):
    from cedars.services.accounts import service as access

    u = make_user("dev@pge.example.org", "Dee Veloper")
    access.grant(pge_admin, u, pge, "impact_profiles.submit")
    return u


def test_the_server_settings_are_the_defaults_until_an_administrator_saves(settings):
    settings.IMPACT_PROFILE_TERRITORIES = ["PGE", "SCE"]
    settings.LOAD_SHAPE_FIRST_START = "2028-01-01"
    now = policy.current()
    assert now.territories == ("PGE", "SCE") and now.load_shape_first_start == date(2028, 1, 1) and now.record is None


def test_a_change_is_a_new_row_with_its_reason_and_audit_event(cpuc_admin):
    policy.change(
        cpuc_admin, territories=["PGE", "SCE", "SCG", "SDGE", "PAC"], load_shape_first_start=date(2027, 1, 1), reason="PacifiCorp"
    )
    policy.change(cpuc_admin, territories=["PGE", "SCE", "SCG", "SDGE", "PAC"], load_shape_first_start=date(2028, 1, 1), reason="later")
    assert LibrarySettings.objects.count() == 2
    assert policy.territories()[-1] == "PAC" and policy.load_shape_first_start() == date(2028, 1, 1)
    e = AuditEvent.objects.filter(action="impact_profile_settings.changed").first()
    assert e.reason == "later" and e.before["load_shape_first_start"] == "2027-01-01" and e.after["load_shape_first_start"] == "2028-01-01"


def test_only_cpuc_administrators_change_them_with_a_reason_and_a_real_change(cpuc_admin, developer):
    with pytest.raises(AccessDenied):
        policy.change(developer, territories=["PGE"], load_shape_first_start=date(2027, 1, 1), reason="x")
    with pytest.raises(ValueError, match="reason"):
        policy.change(cpuc_admin, territories=["PGE"], load_shape_first_start=date(2027, 1, 1), reason=" ")
    with pytest.raises(ValueError, match="at least one"):
        policy.change(cpuc_admin, territories=[], load_shape_first_start=date(2027, 1, 1), reason="x")
    with pytest.raises(ValueError, match="Not a territory code: P-G"):
        policy.change(cpuc_admin, territories=["P-G"], load_shape_first_start=date(2027, 1, 1), reason="x")
    now = policy.current()
    with pytest.raises(ValueError, match="Nothing changed"):
        policy.change(cpuc_admin, territories=list(now.territories), load_shape_first_start=now.load_shape_first_start, reason="x")
    assert policy.parse_territories(" pge, sce;\nSCE  pac ") == ["PGE", "SCE", "PAC"]


def test_the_start_date_decides_what_the_next_sync_takes(cpuc_admin):
    policy.change(cpuc_admin, territories=["PGE", "SCE", "SCG", "SDGE"], load_shape_first_start=date(2030, 1, 1), reason="later start")
    run = etrm.sync(cpuc_admin, etrm.FileSource(FIXTURE), trigger="manual")
    assert run.packages_seen == 0 and not MeasurePackage.objects.exists()


def test_the_territories_decide_the_library_rows_and_custom_profiles(cpuc_admin, developer, pge):
    etrm.sync(cpuc_admin, etrm.FileSource(FIXTURE), trigger="manual")
    leds = MeasurePackage.objects.get(measure_version_id="SWLG999-01-draft")
    upload(developer, pge, leds, load_shape_csv(leds_series()), name="v1.csv")
    generation.generate(developer, leds)
    policy.change(cpuc_admin, territories=["PGE", "PAC"], load_shape_first_start=date(2027, 1, 1), reason="two territories")
    export = library.export(cpuc_admin)
    cat = pq.read_table(default_storage.path(export.catalog_name)).to_pylist()
    assert {r["iou_ac_territory"] for r in cat} == {"PGE", "PAC"}
    with pytest.raises(AccessDenied, match="one of PGE, PAC"):
        custom.create_custom(
            developer, pge, profile_key="X", target_sector="Commercial", territory="SCE", data=profile_file(), file_name="x.csv"
        )


def test_the_settings_page(signed_in, cpuc_admin, developer):
    assert signed_in(developer).get("/impact-profiles/settings/").status_code == 404
    c = signed_in(cpuc_admin)
    page = c.get("/impact-profiles/settings/").content.decode()
    assert "Not changed on this page yet" in page and "PGE, SCE, SCG, SDGE" in page
    r = c.post(
        "/impact-profiles/settings/",
        {"territories": "PGE, SCE, SCG, SDGE, PAC", "load_shape_first_start": "2027-01-01", "reason": "Add PacifiCorp"},
    )
    assert r.status_code == 302
    page = c.get("/impact-profiles/settings/").content.decode()
    assert "PGE, SCE, SCG, SDGE, PAC" in page and "Add PacifiCorp" in page
    r = c.post(
        "/impact-profiles/settings/", {"territories": "PGE, SCE, SCG, SDGE, PAC", "load_shape_first_start": "2027-01-01", "reason": "x"}
    )
    assert r.status_code == 200 and "Nothing changed" in r.content.decode()

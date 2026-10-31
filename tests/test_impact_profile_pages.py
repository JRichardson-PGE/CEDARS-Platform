import numpy as np
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from cedars.modules.impact_profiles import etrm, generation, jobs
from cedars.modules.impact_profiles.models import ImpactProfile, LoadShapeUpload, MeasurePackage, ProcessingJob
from cedars.services.accounts import service as access

from .conftest import page_only
from .test_impact_profiles import FIXTURE, leds_series, load_shape_csv, profile_file, zipped

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.IMPACT_PROFILES_INLINE_JOBS = False


@pytest.fixture
def developer(make_user, pge, pge_admin):
    u = make_user("dev@pge.com", "Dee Veloper")
    access.grant(pge_admin, u, pge, "impact_profiles.submit")
    return u


@pytest.fixture
def synced(cpuc_admin):
    return etrm.sync(cpuc_admin, etrm.FileSource(FIXTURE))


def test_everyone_signed_in_sees_the_libraries(signed_in, make_user, synced):
    c = signed_in(make_user("someone@example.com"))
    for path in (
        "/impact-profiles/",
        "/impact-profiles/packages/",
        "/impact-profiles/etrm/",
        "/impact-profiles/library/",
        "/impact-profiles/uploads/",
    ):
        assert c.get(path).status_code == 200, path
    page = c.get("/impact-profiles/etrm/").content.decode()
    assert "UECkWhMeas is zero" in page and "Sync now" not in page_only(page)
    assert c.get("/impact-profiles/packages/SWLG999-01-draft/").status_code == 200
    assert c.get("/impact-profiles/custom/new/").status_code == 404  # no submit permission
    assert c.post("/impact-profiles/etrm/").status_code == 302 and not ProcessingJob.objects.exists()


def test_upload_check_and_generate_through_the_pages(signed_in, developer, synced):
    c = signed_in(developer)
    page = c.get("/impact-profiles/packages/SWLG999-01-draft/").content.decode()
    assert "Upload load shapes" in page
    f = SimpleUploadedFile("shapes.zip", zipped(load_shape_csv(leds_series())), content_type="application/zip")
    r = c.post("/impact-profiles/packages/SWLG999-01-draft/", {"file": f})
    u = LoadShapeUpload.objects.get()
    assert r.status_code == 302 and r.url == f"/impact-profiles/uploads/{u.pk}/"
    assert "Waiting to be checked" in c.get(r.url).content.decode()
    jobs.run_pending()
    page = c.get(r.url).content.decode()
    assert "Accepted" in page and "Generate impact profiles" in page
    assert c.post(f"/impact-profiles/uploads/{u.pk}/generate").status_code == 302
    jobs.run_pending()
    page = c.get(r.url).content.decode()
    assert "Generation result" in page and "OfL*Ex*rDXGF*Msr-LED*Std-Fluor" in page
    p = ImpactProfile.objects.get(profile_key="OfL*Ex*rDXGF*Msr-LED*Std-Fluor")
    page = c.get(f"/impact-profiles/{p.pk}/").content.decode()
    assert "Current version by climate zone" in page and "1.000000" in page
    csv_text = c.get(f"/impact-profiles/{p.pk}/v1.csv").content.decode()
    assert csv_text.startswith("Climate Zone,Hour of Year,Impact\n12,1,") and csv_text.count("\n") == 8761


def test_a_rejected_upload_lists_its_findings_and_offers_them_as_csv(signed_in, developer, synced):
    c = signed_in(developer)
    bad = load_shape_csv([({"TechID": "Msr-LED"}, np.full(8760, 1 / 8000))])
    c.post("/impact-profiles/packages/SWLG999-01-draft/", {"file": SimpleUploadedFile("bad.csv", bad, content_type="text/csv")})
    jobs.run_pending()
    u = LoadShapeUpload.objects.get()
    page = c.get(f"/impact-profiles/uploads/{u.pk}/").content.decode()
    assert "Rejected: nothing saved" in page and "LS-S04" in page and "Generate impact profiles" not in page_only(page)
    rows = c.get(f"/impact-profiles/uploads/{u.pk}/findings.csv").content.decode().splitlines()
    assert rows[0].startswith("stage,rule_id") and rows[1].startswith("S,LS-S04,reject,2,")


def test_another_pas_member_cannot_upload_or_see_the_upload(signed_in, developer, synced, make_user, cpuc_admin):
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    other, admin = make_user("dev@sce.com"), make_user("admin@sce.com")
    access.grant(cpuc_admin, admin, sce, "org.admin")
    access.grant(admin, other, sce, "impact_profiles.submit")
    c = signed_in(developer)
    c.post("/impact-profiles/packages/SWLG999-01-draft/", {"file": SimpleUploadedFile("s.csv", load_shape_csv(leds_series()))})
    u = LoadShapeUpload.objects.get()
    c2 = signed_in(other)
    assert "Upload load shapes" not in page_only(c2.get("/impact-profiles/packages/SWLG999-01-draft/").content.decode())
    r = c2.post("/impact-profiles/packages/SWLG999-01-draft/", {"file": SimpleUploadedFile("s.csv", b"x")})
    assert r.status_code == 302 and LoadShapeUpload.objects.count() == 1
    assert c2.get(f"/impact-profiles/uploads/{u.pk}/").status_code == 404


def test_custom_profile_pages(signed_in, developer, pge):
    c = signed_in(developer)
    r = c.post(
        "/impact-profiles/custom/new/",
        {
            "profile_key": "PGE Office NMEC",
            "target_sector": "Commercial",
            "territory": "PGE",
            "file": SimpleUploadedFile("p.csv", profile_file()),
        },
    )
    p = ImpactProfile.objects.get(profile_key="PGE Office NMEC")
    assert r.status_code == 302 and r.url == f"/impact-profiles/{p.pk}/"
    page = c.get(r.url).content.decode()
    assert "Replace with a new version" in page and "Retire this profile" in page
    r = c.post(f"/impact-profiles/{p.pk}/replace", {"file": SimpleUploadedFile("b.csv", profile_file(scale=2)), "reason": "true-up"})
    assert r.status_code == 400 and "LS-S04" in r.content.decode()
    r = c.post(f"/impact-profiles/{p.pk}/replace", {"file": SimpleUploadedFile("c.csv", profile_file(zones=("12",))), "reason": "true-up"})
    assert r.status_code == 302 and p.versions.count() == 2
    assert c.post(f"/impact-profiles/{p.pk}/retire", {"reason": "closed"}).status_code == 302
    p.refresh_from_db()
    assert p.retired
    bad = c.post(
        "/impact-profiles/custom/new/",
        {"profile_key": "X", "target_sector": "Commercial", "file": SimpleUploadedFile("x.csv", b"Zone,Hour\n")},
    )
    assert bad.status_code == 400 and "LS-T01" in bad.content.decode()


def test_cpuc_admin_syncs_and_writes_the_library(signed_in, cpuc_admin, synced, settings, django_capture_on_commit_callbacks):
    settings.IMPACT_PROFILES_INLINE_JOBS = True
    c = signed_in(cpuc_admin)
    assert "Sync now" in c.get("/impact-profiles/etrm/").content.decode()
    with django_capture_on_commit_callbacks(execute=True):
        c.post("/impact-profiles/etrm/")
    assert ProcessingJob.objects.get(kind="sync").status == "done"
    with django_capture_on_commit_callbacks(execute=True):
        c.post("/impact-profiles/library/")
    page = c.get("/impact-profiles/library/").content.decode()
    assert "catalog.parquet" in page
    r = c.get("/impact-profiles/library/1/catalog.parquet")
    assert r.status_code == 200 and b"".join(r.streaming_content)[:4] == b"PAR1"


def test_templates_download(signed_in, make_user):
    c = signed_in(make_user("x@example.com"))
    t = c.get("/impact-profiles/load-shapes/template.csv").content.decode()
    assert (
        t.splitlines()[0]
        == "Sector,BldgType,BldgVint,BldgHVAC,BldgLoc,NormUnit,Type,Source Year,TechGroup,TechType,TechID,Hour of Year,UECproportion"
    )
    assert len(t.splitlines()) == 8761
    assert c.get("/impact-profiles/custom/template.csv").content.decode().startswith("Climate Zone,Hour of Year,Impact")


def test_mapping_page_flow(signed_in, developer, synced):
    c = signed_in(developer)
    leds = MeasurePackage.objects.get(measure_version_id="SWLG999-01-draft")
    c.post("/impact-profiles/packages/SWLG999-01-draft/", {"file": SimpleUploadedFile("s.csv", load_shape_csv(leds_series()))})
    jobs.run_pending()
    generation.generate(developer, leds)
    target = ImpactProfile.objects.get(profile_key="OfL*Ex*rDXGF*Msr-LED*Std-Fluor")
    r = c.post(
        "/impact-profiles/packages/SWLG999-01-draft/mappings",
        {"alias_key": "OfL*New*rDXGF*Msr-LED*Std-Fluor", "target_sector": "Com", "target": target.pk, "reason": "NC reuses Ex"},
    )
    assert r.status_code == 302
    page = c.get("/impact-profiles/packages/SWLG999-01-draft/").content.decode()
    assert "OfL*New*rDXGF*Msr-LED*Std-Fluor / Com uses" in page

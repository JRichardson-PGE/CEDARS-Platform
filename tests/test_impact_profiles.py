import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from cedars.modules.impact_profiles import custom, etrm, generation, identical, jobs, library, loadshapes, shapes
from cedars.modules.impact_profiles.models import (
    EtrmSyncRun,
    ImpactProfile,
    LoadShape,
    LoadShapeUpload,
    MeasurePackage,
    ProcessingJob,
    ProfileMapping,
)
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent

pytestmark = pytest.mark.django_db
DATA = Path(__file__).parent / "data"
FIXTURE = DATA / "etrm_fixture.json"
H = np.arange(8760)


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.IMPACT_PROFILES_INLINE_JOBS = False


@pytest.fixture
def synced(cpuc_admin):
    return etrm.sync(cpuc_admin, etrm.FileSource(FIXTURE), trigger="manual")


@pytest.fixture
def developer(make_user, pge, pge_admin):
    u = make_user("dev@pge.example.org", "Dee Veloper")
    access.grant(pge_admin, u, pge, "impact_profiles.submit")
    return u


@pytest.fixture
def leds(synced):
    return MeasurePackage.objects.get(measure_version_id="SWLG999-01-draft")


def unit(a):
    a = np.asarray(a, dtype=float)
    return a / a.sum()


SHAPE = {
    "Msr-LED": unit(1 + 0.5 * np.sin(2 * np.pi * H / 24)),
    "Std-Fluor": unit(1 + 0.8 * np.sin(2 * np.pi * H / 24) + 0.1 * np.cos(2 * np.pi * H / 168)),
}


def load_shape_csv(series, *, header=None, rows=None) -> bytes:
    """series: [(key dict, values)]; header defaults to the layout's columns."""
    out = io.StringIO()
    out.write(",".join(header or loadshapes.COLUMNS) + "\n")
    for key, values in series:
        for h, v in enumerate(values, start=1):
            if rows is not None and not rows(h):
                continue
            out.write(
                ",".join(
                    [
                        key.get("Sector", "Com"),
                        key.get("BldgType", "OfL"),
                        key.get("BldgVint", "Ex"),
                        key.get("BldgHVAC", "rDXGF"),
                        key.get("BldgLoc", "CZ12"),
                        "Each",
                        "Whole Building",
                        str(key.get("Source Year", 2009)),
                        key.get("TechGroup", "Lighting"),
                        key.get("TechType", "LED"),
                        key["TechID"],
                        str(h),
                        repr(float(v)),
                    ]
                )
                + "\n"
            )
    return out.getvalue().encode()


def zipped(data: bytes, name="shapes.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return buf.getvalue()


def upload(user, org, package, data: bytes, name="shapes.zip", accept_partial=False) -> LoadShapeUpload:
    f = ContentFile(data, name=name)
    u = loadshapes.create_upload(user, org, package, f, accept_partial=accept_partial)
    jobs.run_pending()
    u.refresh_from_db()
    return u


# --- eTRM sync


def test_sync_keeps_drafts_from_2027_and_lists_exclusions(synced):
    assert synced.status == EtrmSyncRun.Status.SUCCEEDED
    assert (synced.packages_seen, synced.imported, synced.unchanged, synced.excluded) == (5, 2, 0, 3)
    reasons = dict(synced.exclusions.values_list("measure_version_id", "reason"))
    assert reasons["SWWH997-01-draft"] == "UECkWhMeas is zero"
    assert reasons["SWWH996-01-draft"] == "Missing permutations"
    assert reasons["SWWH995-01-draft"].startswith("Permutations contain unexpected data: non-ASCII")
    assert set(MeasurePackage.objects.values_list("measure_version_id", flat=True)) == {"SWLG999-01-draft", "SWHC998-02-draft"}
    leds = MeasurePackage.objects.get(measure_version_id="SWLG999-01-draft")
    p = leds.current.permutations.first()
    assert p.uec_kwh_meas == 50.123456789  # full precision kept
    assert AuditEvent.objects.filter(action="etrm_sync.finished").exists()


def test_an_unchanged_package_keeps_its_version_and_a_changed_one_gets_a_new_one(synced, cpuc_admin, tmp_path):
    again = etrm.sync(cpuc_admin, etrm.FileSource(FIXTURE))
    assert (again.imported, again.unchanged) == (0, 2)
    data = json.loads(FIXTURE.read_text())
    data["packages"][0]["permutations"][0]["uec_kwh_base1"] = 101.0
    data["packages"] = data["packages"][:1]
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(data))
    third = etrm.sync(cpuc_admin, etrm.FileSource(changed))
    assert third.imported == 1
    leds = MeasurePackage.objects.get(measure_version_id="SWLG999-01-draft")
    assert leds.current.number == 2 and leds.versions.count() == 2
    assert not MeasurePackage.objects.get(measure_version_id="SWHC998-02-draft").in_last_sync


def test_a_source_that_cannot_be_read_fails_the_sync_and_changes_nothing(cpuc_admin, tmp_path):
    run = etrm.sync(cpuc_admin, etrm.FileSource(tmp_path / "missing.json"))
    assert run.status == EtrmSyncRun.Status.FAILED and "could not read" in run.error
    assert not MeasurePackage.objects.exists()


def test_only_cpuc_administrators_start_a_sync(developer):
    with pytest.raises(AccessDenied, match="CPUC administrator"):
        etrm.sync(developer, etrm.FileSource(FIXTURE))


@pytest.mark.django_db(transaction=True)
def test_the_database_source_reads_the_fixture_schema(cpuc_admin, settings):
    from django.db import connection

    sql = (Path(etrm.__file__).parent / "etrm_fixture_schema.sql").read_text().format(schema="etrm_test")
    data = json.loads(FIXTURE.read_text())
    cols = etrm.TEXT_FIELDS + etrm.NUMBER_FIELDS
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS etrm_test CASCADE")
        cur.execute(sql)
        for p in data["packages"][:2]:
            cur.execute(
                "INSERT INTO etrm_test.measure_versions VALUES (%s, %s, %s, %s, %s, true)",
                [p["measure_version_id"], p["statewide_id"], p["name"], p["pa_lead"], p["start_date"]],
            )
            for perm in p["permutations"]:
                marks = ", ".join(["%s"] * (len(cols) + 1))
                cur.execute(
                    f"INSERT INTO etrm_test.permutations (measure_version_id, {', '.join(cols)}) VALUES ({marks})",
                    [p["measure_version_id"]] + [perm.get(c) for c in cols],
                )
    try:
        db = settings.DATABASES["default"]
        url = f"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST'] or 'localhost'}:{db['PORT'] or 5432}/{db['NAME']}"
        run = etrm.sync(cpuc_admin, etrm.DatabaseSource(url, "etrm_test"), trigger="manual")
        assert run.source == "eTRM database (etrm_test)" and (run.imported, run.excluded) == (2, 0)
        assert MeasurePackage.objects.get(measure_version_id="SWLG999-01-draft").current.permutation_count == 3
    finally:
        with connection.cursor() as cur:
            cur.execute("DROP SCHEMA etrm_test CASCADE")


# --- the four validation stages


def leds_series(**extra):
    return [({"TechID": t, **extra}, SHAPE[t]) for t in ("Msr-LED", "Std-Fluor")]


def check(data: bytes, package, name="shapes.csv"):
    return loadshapes.check_file(io.BytesIO(data), name, len(data), package.current.permutations.all())


def test_a_good_file_passes_every_stage(leds):
    r = check(zipped(load_shape_csv(leds_series())), leds, "shapes.zip")
    assert r.stages == {"F": "passed", "T": "passed", "S": "passed", "E": "passed"} and not r.findings
    assert len(r.passed) == 2


def test_file_stage(leds):
    assert check(b"not a zip", leds, "shapes.zip").findings[0].rule_id == "LS-F04"
    assert check(b"x", leds, "shapes.xlsx").findings[0].rule_id == "LS-F01"
    two = io.BytesIO()
    with zipfile.ZipFile(two, "w") as z:
        z.writestr("a.csv", "x")
        z.writestr("b.csv", "y")
    assert check(two.getvalue(), leds, "two.zip").findings[0].rule_id == "LS-F03"
    assert check(b"", leds).findings[0].rule_id == "LS-F05"
    header_only = (",".join(loadshapes.COLUMNS) + "\n").encode()
    assert check(header_only, leds).findings[0].rule_id == "LS-F05"


def test_table_stage(leds):
    cols = [c for c in loadshapes.COLUMNS if c != "TechID"] + ["Notes"]
    r = check(load_shape_csv([], header=cols), leds)
    assert r.stages["T"] == "failed" and r.stages["S"] == "not run"
    assert [f.rule_id for f in r.findings] == ["LS-T01", "LS-T02"]
    assert "TechID" in r.findings[0].message
    # headers are matched without regard to case, spaces or underscores
    loose = [
        "sector",
        "bldg_type",
        "BLDGVINT",
        "BldgHVAC",
        "Bldg Loc",
        "NormUnit",
        "Type",
        "SourceYear",
        "TechGroup",
        "TechType",
        "TechID",
        "hour_of_year",
        "UECProportion",
    ]
    assert check(load_shape_csv(leds_series(), header=loose), leds).stages["T"] == "passed"


def test_series_stage(leds):
    bad = SHAPE["Msr-LED"] * 1.01
    r = check(load_shape_csv([({"TechID": "Msr-LED"}, bad)]), leds)
    assert [f.rule_id for f in r.findings] == ["LS-S04"]
    gap = check(load_shape_csv([({"TechID": "Msr-LED"}, SHAPE["Msr-LED"])], rows=lambda h: h != 59), leds)
    assert gap.findings[0].rule_id == "LS-S02" and "hour 59" in gap.findings[0].message
    leap = load_shape_csv([({"TechID": "Msr-LED"}, np.concatenate([SHAPE["Msr-LED"], np.zeros(24)]))])
    assert "leap day" in check(leap, leds).findings[0].message
    anytype = check(load_shape_csv([({"TechID": "Msr-LED", "BldgType": "Any"}, SHAPE["Msr-LED"])]), leds)
    assert "LS-S07" in [f.rule_id for f in anytype.findings]
    zone = check(load_shape_csv([({"TechID": "Msr-LED", "BldgLoc": "Coastal"}, SHAPE["Msr-LED"])]), leds)
    assert "LS-S08" in [f.rule_id for f in zone.findings]
    text = load_shape_csv([({"TechID": "Msr-LED"}, SHAPE["Msr-LED"])]).replace(b",12,", b",twelve,", 1)
    assert check(text, leds).findings[0].rule_id == "LS-S01"


def test_etrm_stage(leds):
    r = check(load_shape_csv([({"TechID": "Msr-Unknown"}, SHAPE["Msr-LED"])]), leds)
    assert r.findings[0].rule_id == "LS-E02" and r.findings[0].field == "TechID"
    # every value exists in the package but not in this combination (an HVAC type the Ex OfL permutation does not have)
    r = check(load_shape_csv([({"TechID": "Msr-LED", "BldgVint": "New", "BldgHVAC": "Any"}, SHAPE["Msr-LED"])]), leds)
    assert r.findings[0].rule_id == "LS-E01"
    # "Any" in the permutation matches explicit values: Com stands in for BldgType, any zone, any HVAC
    r = check(load_shape_csv([({"TechID": "Msr-LED", "BldgType": "Com", "BldgHVAC": "Any", "BldgLoc": "3"}, SHAPE["Msr-LED"])]), leds)
    assert not r.findings


# --- uploads and generation


def test_upload_rejects_the_whole_file_by_default(leds, developer, pge):
    series = leds_series() + [({"TechID": "Msr-LED", "BldgLoc": "CZ03"}, SHAPE["Msr-LED"] * 2)]
    u = upload(developer, pge, leds, zipped(load_shape_csv(series)))
    assert u.status == LoadShapeUpload.Status.REJECTED and u.series_count == 3 and u.accepted_count == 0
    assert not LoadShape.objects.exists()
    assert u.findings.get().rule_id == "LS-S04"
    partial = upload(developer, pge, leds, zipped(load_shape_csv(series)), accept_partial=True)
    assert partial.status == LoadShapeUpload.Status.ACCEPTED and partial.accepted_count == 2
    assert LoadShape.objects.count() == 2


def test_only_the_package_lead_uploads(leds, make_user, cpuc_admin, pge_admin, pge):
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    u = make_user("dev@sce.example.org")
    sce_admin = make_user("admin@sce.example.org")
    access.grant(cpuc_admin, sce_admin, sce, "org.admin")
    access.grant(sce_admin, u, sce, "impact_profiles.submit")
    with pytest.raises(AccessDenied, match="led by PGE"):
        loadshapes.create_upload(u, sce, leds, ContentFile(b"x", name="a.csv"))
    with pytest.raises(AccessDenied, match="submit"):
        loadshapes.create_upload(pge_admin, pge, leds, ContentFile(b"x", name="a.csv"))


def test_generation_uses_cedars_formula_and_sums_to_one(leds, developer, pge):
    u = upload(developer, pge, leds, zipped(load_shape_csv(leds_series())))
    summary = generation.generate(developer, leds, u)
    # the OfL permutation, and the Any permutation, whose only uploaded building type is OfL (with its own UECs)
    assert summary["created"] == 2
    p = ImpactProfile.objects.get(profile_key="OfL*Ex*rDXGF*Msr-LED*Std-Fluor")
    assert (p.family, p.target_sector, p.owner.code, p.package) == ("deemed", "Com", "PGE", leds)
    v = p.current_version()
    assert v.represents_year == 2009 and [z.climate_zone for z in v.zones.all()] == ["12"]
    got = shapes.load(v.zones.get().shape)
    um, ub = 50.123456789, 100.5
    expected = (SHAPE["Std-Fluor"] * ub - SHAPE["Msr-LED"] * um) / (ub - um)
    assert np.allclose(got, expected, rtol=0, atol=1e-15) and abs(got.sum() - 1) < 1e-12
    # the NC permutation (vintage New) has no shapes of its own
    assert [r for _, r in summary["skipped"]] == ["no load shapes uploaded for it"]
    # nothing changed: regenerating adds no version
    assert generation.generate(developer, leds, None)["unchanged"] == 2


def test_any_permutations_use_the_stand_in_and_every_zone_uploaded(leds, developer, pge):
    series = [({"TechID": t, "BldgType": "Com", "BldgHVAC": "Any", "BldgLoc": z}, SHAPE[t]) for t in SHAPE for z in ("3", "CZ12")]
    upload(developer, pge, leds, load_shape_csv(series), name="any.csv")
    generation.generate(developer, leds)
    p = ImpactProfile.objects.get(profile_key="Any*Ex*Any*Msr-LED*Std-Fluor")
    assert [z.climate_zone for z in p.current_version().zones.all()] == ["12", "3"] or {
        z.climate_zone for z in p.current_version().zones.all()
    } == {"3", "12"}
    # one array per distinct shape: both zones carry the same values, stored once
    assert len({z.shape_id for z in p.current_version().zones.all()}) == 1


EXPORT = DATA / "etrm_permutations_export.csv"  # the first 3 rows of eTRM's SWHC052-02 permutations download


def test_export_source_reads_etrms_permutations_download(cpuc_admin):
    (rec,) = etrm.ExportSource(EXPORT).packages()
    assert (rec.measure_version_id, rec.statewide_id, rec.name, rec.pa_lead) == (
        "SWHC052-02",
        "SWHC052",
        "Air-Cooled Chiller, Path B",
        "SDGE",
    )
    assert rec.start_date.isoformat() == "2023-01-01" and len(rec.permutations) == 3
    p = etrm._clean(rec.permutations[0])
    assert (p["sector"], p["bldg_type"], p["bldg_vint"], p["bldg_hvac"], p["bldg_loc"]) == ("Com", "Com", "Ex", "cWtd", "CZ01")
    technology = (p["tech_group"], p["tech_type"], p["standard_tech_group"], p["measure_application_type"])
    assert technology == ("Chiller", "Screw", "Chiller", "NR")
    assert (p["uec_kwh_meas"], p["uec_kwh_base1"], p["uec_kwh_base2"]) == (152.0, 215.0, 0.0)
    assert p["electric_impact_profile_id"] == "DEER:HVAC_Chillers" and p["elec_target_sector"] == ""
    run = etrm.sync(cpuc_admin, etrm.ExportSource(EXPORT))
    assert run.status == EtrmSyncRun.Status.SUCCEEDED and run.packages_seen == 0  # published, and before 2027: not a load-shape package


def test_an_export_that_is_not_a_permutations_download_fails_the_sync(cpuc_admin, tmp_path):
    bad = tmp_path / "other.csv"
    bad.write_text("a,b\n1,2\n")
    run = etrm.sync(cpuc_admin, etrm.ExportSource([bad]))
    assert run.status == EtrmSyncRun.Status.FAILED and "not an eTRM permutations download" in run.error


def draft_export(path, rows: list[dict]) -> Path:
    """An eTRM download in the sample's layout, each row the sample's first with these labels changed."""
    import csv

    lines = EXPORT.read_text(encoding="utf-8").splitlines()
    reader = csv.DictReader(lines[1:])
    base = next(reader)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(lines[0] + "\r\n")
        w = csv.DictWriter(f, fieldnames=reader.fieldnames, quoting=csv.QUOTE_ALL)
        w.writeheader()
        for r in rows:
            w.writerow({**base, "Measure Version ID": "SWHC052-03-draft", "Effective Start Date": "2027-01-01", **r})
    return path


def test_a_draft_export_syncs_and_pairs_each_base_case_on_its_own_technology(cpuc_admin, make_user, tmp_path):
    sdge = access.create_organisation(cpuc_admin, code="SDGE", name="San Diego Gas & Electric", kind="pa", pa_type="IOU")
    admin, dev = make_user("admin@sdge.example.org"), make_user("dev@sdge.example.org")
    access.grant(cpuc_admin, admin, sdge, "org.admin")
    access.grant(admin, dev, sdge, "impact_profiles.submit")
    ar = {
        "Building Location": "CZ10",
        "Measure Application Type": "AR",
        "Measure Technology ID": "Msr-Screw",
        "Pre-Existing Technology ID": "Pre-Recip",
        "Pre-Existing Technology Type": "Reciprocating",  # the pre-existing chiller is not a screw chiller
        "Standard Technology ID": "Std-Screw",
        "Measure UEC kWh": "150",
        "First Baseline - UEC kWh": "250",
        "Second Baseline - UEC kWh": "200",
    }
    path = draft_export(
        tmp_path / "SWHC052-03-draft.csv",
        [ar, {**ar, "Delivery Type": "UpDeemed"}, {**ar, "Delivery Type": "DnDeemed", "Offering ID": "B"}],
    )
    run = etrm.sync(cpuc_admin, etrm.ExportSource([path]))
    assert (run.packages_seen, run.imported, run.excluded) == (1, 1, 0)
    package = MeasurePackage.objects.get(measure_version_id="SWHC052-03-draft")
    assert package.current.permutation_count == 1  # three rows differing only in delivery type and offering
    p = package.current.permutations.get()
    assert p.baselines() == [("1st", ("Chiller", "Reciprocating", "Pre-Recip"), 250.0), ("2nd", ("Chiller", "Screw", "Std-Screw"), 200.0)]

    key = {"Sector": "Com", "BldgType": "Com", "BldgHVAC": "cWtd", "BldgLoc": "CZ10", "TechGroup": "Chiller"}
    vals = {"Msr-Screw": SHAPE["Msr-LED"], "Pre-Recip": SHAPE["Std-Fluor"], "Std-Screw": unit(1 + 0.3 * np.cos(2 * np.pi * H / 8760))}
    series = [({**key, "TechType": "Reciprocating" if t == "Pre-Recip" else "Screw", "TechID": t}, v) for t, v in vals.items()]
    u = upload(dev, sdge, package, load_shape_csv(series), name="chillers.csv")
    assert u.status == LoadShapeUpload.Status.ACCEPTED, list(u.findings.values_list("message", flat=True))[:3]
    s = generation.generate(dev, package)
    assert s["created"] == 2, s
    first = ImpactProfile.objects.get(profile_key="Com*Ex*cWtd*Msr-Screw*Pre-Recip")
    assert first.target_sector == "Com"  # the download has no electric target sector
    a = shapes.load(first.current_version().zones.get().shape)
    assert np.allclose(a, (vals["Pre-Recip"] * 250 - vals["Msr-Screw"] * 150) / 100)

    # a base-case shape under the measure's TechType does not stand in for the pre-existing reciprocating chiller
    wrong = [({**key, "TechType": "Screw", "TechID": t}, v) for t, v in vals.items()]
    u = upload(dev, sdge, package, load_shape_csv(wrong), name="wrong.csv")
    assert u.status == LoadShapeUpload.Status.REJECTED
    assert u.findings.filter(rule_id="LS-E01").exists() or u.findings.filter(rule_id="LS-E02").exists()


def test_a_download_without_technology_ids_is_excluded(cpuc_admin, tmp_path):
    path = draft_export(tmp_path / "no_ids.csv", [{}])
    run = etrm.sync(cpuc_admin, etrm.ExportSource([path]))
    assert run.excluded == 1
    assert "MeasTechID" in run.exclusions.get().reason and "base case TechID" in run.exclusions.get().reason


def test_accelerated_replacement_gets_a_profile_per_baseline(synced, cpuc_admin, make_user):
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    admin, dev = make_user("admin@sce.example.org"), make_user("dev@sce.example.org")
    access.grant(cpuc_admin, admin, sce, "org.admin")
    access.grant(admin, dev, sce, "impact_profiles.submit")
    hp = MeasurePackage.objects.get(measure_version_id="SWHC998-02-draft")
    key = {"Sector": "Res", "BldgType": "SFm", "TechGroup": "HVAC", "TechType": "HeatPump"}
    vals = {"Msr-HP": SHAPE["Msr-LED"], "Pre-AC": SHAPE["Std-Fluor"], "Std-HP": unit(1 + 0.3 * np.cos(2 * np.pi * H / 8760))}
    upload(dev, sce, hp, load_shape_csv([({**key, "TechID": t}, v) for t, v in vals.items()]), name="hp.csv")
    s = generation.generate(dev, hp)
    assert s["created"] == 2
    first = ImpactProfile.objects.get(profile_key="SFm*Ex*rDXGF*Msr-HP*Pre-AC")
    second = ImpactProfile.objects.get(profile_key="SFm*Ex*rDXGF*Msr-HP*Std-HP")
    assert first.target_sector == "Res"
    a = shapes.load(second.current_version().zones.get().shape)
    assert np.allclose(a, (vals["Std-HP"] * 2500 - vals["Msr-HP"] * 2000) / 500)


def test_replacing_a_load_shape_regenerates_a_new_profile_version(leds, developer, pge):
    upload(developer, pge, leds, load_shape_csv(leds_series()), name="v1.csv")
    generation.generate(developer, leds)
    new = unit(1 + 0.2 * np.sin(2 * np.pi * H / 24))
    u2 = upload(developer, pge, leds, load_shape_csv([({"TechID": "Msr-LED"}, new)]), name="v2.csv")
    assert u2.status == "accepted"
    ls = LoadShape.objects.get(tech_id="Msr-LED")
    assert [v.number for v in ls.versions.all()] == [2, 1] and ls.versions.get(number=1).superseded_at is not None
    s = generation.generate(developer, leds, u2)
    assert s["new_version"] == 2
    p = ImpactProfile.objects.get(profile_key="OfL*Ex*rDXGF*Msr-LED*Std-Fluor")
    assert [v.number for v in p.versions.all()] == [2, 1]
    assert p.versions.get(number=1).superseded_at is not None  # kept for runs and claims that named it
    assert AuditEvent.objects.filter(action="impact_profile.generated", object_id=str(p.pk)).count() == 2


def test_upload_and_generate_jobs_run_on_the_pas_queue(leds, developer, pge):
    f = ContentFile(load_shape_csv(leds_series()), name="q.csv")
    u = loadshapes.create_upload(developer, pge, leds, f)
    job = u.jobs.get()
    assert (job.kind, job.queue, job.status, jobs.queue_position(job)) == ("check", "PGE", "queued", 0)
    assert jobs.run_pending() == 1
    job.refresh_from_db()
    assert job.status == ProcessingJob.Status.DONE and "2 of 2" in job.message


# --- mappings, custom profiles, the library


def test_mapping_lets_the_nc_permutation_use_the_existing_vintage_profile(leds, developer, pge, cpuc_admin):
    upload(developer, pge, leds, load_shape_csv(leds_series()), name="v1.csv")
    generation.generate(developer, leds)
    target = ImpactProfile.objects.get(profile_key="OfL*Ex*rDXGF*Msr-LED*Std-Fluor")
    m = custom.create_mapping(
        developer,
        pge,
        leds,
        alias_key="OfL*New*rDXGF*Msr-LED*Std-Fluor",
        target_sector="Com",
        target=target,
        reason="NC reuses the Ex shapes",
    )
    with pytest.raises(AccessDenied, match="already points"):
        custom.create_mapping(developer, pge, leds, alias_key=m.alias_key, target_sector="Com", target=target, reason="again")
    export = library.export(cpuc_admin)
    cat = pq.read_table(default_storage.path(export.catalog_name)).to_pylist()
    alias = [r for r in cat if r["end_use_name"] == m.alias_key]
    assert len(alias) == 4 and {r["id"] for r in alias} == {target.pk}  # one row per IOU territory, pointing at the same shapes


def profile_file(zones=("12", "System"), scale=1.0, shape=None) -> bytes:
    out = io.StringIO()
    out.write("Climate Zone,Hour of Year,Impact\n")
    for z in zones:
        for h, v in enumerate((SHAPE["Msr-LED"] if shape is None else shape) * scale, start=1):
            out.write(f"{z},{h},{float(v)!r}\n")
    return out.getvalue().encode()


def test_custom_profiles_are_versioned_owned_and_retired(developer, pge, cpuc_admin, make_user):
    p = custom.create_custom(
        developer,
        pge,
        profile_key="PGE NMEC Office 2028",
        target_sector="Commercial",
        territory="PGE",
        data=profile_file(),
        file_name="p.csv",
    )
    assert (p.family, p.owner, p.current_version().number) == ("custom", pge, 1)
    assert {z.climate_zone for z in p.current_version().zones.all()} == {"12", "System"}
    with pytest.raises(custom.ProfileRejected) as e:
        custom.replace_custom(developer, pge, p, data=profile_file(scale=1.1), file_name="bad.csv", reason="true-up")
    assert e.value.findings[0].rule_id == "LS-S04"
    v2 = custom.replace_custom(developer, pge, p, data=profile_file(zones=("12",)), file_name="p2.csv", reason="NMEC true-up")
    assert v2.number == 2 and p.versions.get(number=1).superseded_at is not None
    with pytest.raises(AccessDenied, match="already exists"):
        custom.create_custom(
            developer,
            pge,
            profile_key="PGE NMEC Office 2028",
            target_sector="Commercial",
            territory="PGE",
            data=profile_file(),
            file_name="p.csv",
        )
    custom.retire(developer, pge, p, "program closed")
    export = library.export(cpuc_admin)
    assert export.profiles == 0  # retired profiles stay in the catalog but leave the engine's library


def test_export_is_the_engine_library_format(leds, developer, pge, cpuc_admin):
    upload(developer, pge, leds, load_shape_csv(leds_series()), name="v1.csv")
    generation.generate(developer, leds)
    custom.create_custom(
        developer, pge, profile_key="Custom A", target_sector="Commercial", territory="PGE", data=profile_file(), file_name="a.csv"
    )
    export = library.export(cpuc_admin)
    assert (export.number, export.profiles, export.stored_profiles, export.catalog_rows) == (
        1,
        3,
        3,
        9,
    )  # 2 deemed x 4 territories + 1 custom
    cat = pq.read_table(default_storage.path(export.catalog_name))
    assert {"id", "end_use_name", "iou_ac_territory", "target_sector", "year_represented", "source_impact_type"} <= set(cat.column_names)
    pf = pq.ParquetFile(default_storage.path(export.hourly_name))
    assert pf.metadata.num_row_groups == 3
    assert pf.schema_arrow == library.HOURLY_SCHEMA
    for gi in range(3):
        stats = pf.metadata.row_group(gi).column(0).statistics
        assert stats.min == stats.max  # one profile per row group, as the engine indexes them
        encodings = {c.path_in_schema: set(c.encodings) for c in (pf.metadata.row_group(gi).column(i) for i in range(4))}
        assert "DELTA_BINARY_PACKED" in encodings["impact_profile_id"] and "DELTA_BINARY_PACKED" in encodings["hour_of_year"]
        assert not any(e.endswith("DICTIONARY") for e in encodings["impact"])  # as the scraped library: no dictionary of distinct doubles
    rows = pf.read_row_group(2).to_pylist()  # the custom profile: zones 12 and System
    for z in ("12", "System"):
        g = [r for r in rows if r["climate_zone"] == z]
        assert [r["hour_of_year"] for r in g] == list(range(1, 8761)) and abs(sum(r["impact"] for r in g) - 1) < 1e-9
    assert (export.pack.kind, export.pack.version, export.pack.status) == ("impact_profiles", "export 1", "draft")
    assert export.pack.manifest["hourly.parquet"]["sha256"] == export.hourly_sha256 and export.pack.storage_uri.endswith("/0001")
    assert library.export(cpuc_admin).number == 2


def test_engine_reads_the_export(leds, developer, pge, cpuc_admin, tmp_path, monkeypatch):
    rip = pytest.importorskip("recet.pipeline.real_impact_profiles")
    upload(developer, pge, leds, load_shape_csv(leds_series()), name="v1.csv")
    generation.generate(developer, leds)
    export = library.export(cpuc_admin)
    folder = tmp_path / "ref" / "impact_profiles"
    folder.mkdir(parents=True)
    (folder / "catalog.parquet").write_bytes(Path(default_storage.path(export.catalog_name)).read_bytes())
    (folder / "hourly.parquet").write_bytes(Path(default_storage.path(export.hourly_name)).read_bytes())
    monkeypatch.setattr(
        rip.paths, "ref_path", lambda name: folder / {"impact_catalog": "catalog.parquet", "impact_hourly": "hourly.parquet"}[name]
    )
    from recet import runtime

    runtime.clear_caches()
    monkeypatch.setattr(rip, "_read_cached_shapes", lambda pid: None)
    monkeypatch.setattr(rip, "_write_cached_shapes", lambda pid, s: None)
    try:
        shape, year, source = rip.resolve_shape("OfL*Ex*rDXGF*Msr-LED*Std-Fluor", "SCE", "Com", "12")
    finally:
        runtime.clear_caches()
    assert source == "library" and year == 2009 and abs(shape.sum() - 1) < 1e-12


def engine_folder(export, tmp_path) -> Path:
    folder = tmp_path / "ref" / "impact_profiles"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "catalog.parquet").write_bytes(Path(default_storage.path(export.catalog_name)).read_bytes())
    (folder / "hourly.parquet").write_bytes(Path(default_storage.path(export.hourly_name)).read_bytes())
    return folder


def custom_profile(user, org, key, territory="PGE", data=None, **kw):
    return custom.create_custom(
        user,
        org,
        profile_key=key,
        target_sector="Commercial",
        territory=territory,
        data=data or profile_file(),
        file_name=f"{key}.csv",
        **kw,
    )


def test_identical_profiles_are_one_profile_with_several_names(developer, pge, cpuc_admin, tmp_path, monkeypatch):
    a = custom_profile(developer, pge, "Office A")
    b = custom_profile(developer, pge, "Office B renamed", territory="SCE")  # the same file under another Impact Profile Name
    year = custom_profile(developer, pge, "Office C 2018", represents_year=2018)  # the same values on another calendar
    off = SHAPE["Msr-LED"].copy()
    off[100], off[101] = np.nextafter(off[100], 1.0), off[101] - (np.nextafter(off[100], 1.0) - off[100])
    one_value = custom_profile(developer, pge, "Office D", data=profile_file(shape=off))  # one hour moved by one ulp
    fewer_zones = custom_profile(developer, pge, "Office E", data=profile_file(zones=("12",)))
    assert [str(p) for p in identical.same_profile(b.current_version().content_sha256, exclude=b)] == [str(a)]
    for p in (year, one_value, fewer_zones):
        assert not identical.same_profile(p.current_version().content_sha256, exclude=p).exists(), p.profile_key
    event = AuditEvent.objects.filter(action="impact_profile.created", object_repr=str(b)).get()
    assert event.after["identical_to"] == [str(a)]

    export = library.export(cpuc_admin)
    assert (export.profiles, export.stored_profiles, export.catalog_rows) == (5, 4, 5)
    cat = {r["end_use_name"]: r for r in pq.read_table(default_storage.path(export.catalog_name)).to_pylist()}
    assert cat["Office A"]["shape_id"] == cat["Office B renamed"]["shape_id"] == a.pk  # both names point at the one stored copy
    assert {cat[k]["shape_id"] for k in ("Office C 2018", "Office D", "Office E")} == {year.pk, one_value.pk, fewer_zones.pk}
    assert [r["id"] for r in (cat["Office A"], cat["Office B renamed"])] == [a.pk, b.pk]  # each name keeps its own id
    pf = pq.ParquetFile(default_storage.path(export.hourly_name))
    assert sorted(pf.metadata.row_group(g).column(0).statistics.min for g in range(pf.metadata.num_row_groups)) == sorted(
        [a.pk, year.pk, one_value.pk, fewer_zones.pk]
    )

    rip = pytest.importorskip("recet.pipeline.real_impact_profiles")
    if not hasattr(rip, "shape_id_of"):
        pytest.skip("the pinned engine predates the consolidated library (ReCET 0.1.87)")
    from recet import runtime

    folder = engine_folder(export, tmp_path)
    monkeypatch.setattr(
        rip.paths, "ref_path", lambda name: folder / {"impact_catalog": "catalog.parquet", "impact_hourly": "hourly.parquet"}[name]
    )
    monkeypatch.setattr(rip, "_read_cached_shapes", lambda pid: None)
    monkeypatch.setattr(rip, "_write_cached_shapes", lambda pid, s: None)
    runtime.clear_caches()
    try:
        got_a = rip.resolve_shape("Office A", "PGE", "Commercial", "12")
        got_b = rip.resolve_shape("Office B renamed", "SCE", "Commercial", "12")
        got_d = rip.resolve_shape("Office D", "PGE", "Commercial", "12")
    finally:
        runtime.clear_caches()
    assert got_a[0].tobytes() == got_b[0].tobytes() == SHAPE["Msr-LED"].tobytes() and got_b[2] == "library"
    assert got_d[0].tobytes() == off.tobytes()


def test_replacing_one_name_leaves_the_others(developer, pge, cpuc_admin):
    a = custom_profile(developer, pge, "Office A")
    b = custom_profile(developer, pge, "Office B", territory="SCE")
    assert library.export(cpuc_admin).stored_profiles == 1
    custom.replace_custom(developer, pge, b, data=profile_file(zones=("12",)), file_name="b2.csv", reason="true-up")
    assert not identical.same_profile(a.current_version().content_sha256, exclude=a).exists()
    assert a.current_version().number == 1 and {z.climate_zone for z in a.current_version().zones.all()} == {"12", "System"}
    assert library.export(cpuc_admin).stored_profiles == 2
    custom.retire(developer, pge, b, "closed")
    c = custom_profile(developer, pge, "Office C", territory="SDGE")
    assert [p.pk for p in identical.same_profile(c.current_version().content_sha256, exclude=c)] == [a.pk]  # a retired name is not listed


def test_legacy_library_import(tmp_path, cpuc_admin):
    folder = tmp_path / "legacy"
    folder.mkdir()
    cat = [
        {
            "id": 2,
            "end_use_name": "DEER:HVAC_Chillers",
            "iou_ac_territory": "PGE",
            "target_sector": "Non_Res",
            "year_represented": None,
            "source_impact_type": "calculated",
            "start_year": 2016.0,
        },
        {
            "id": 7,
            "end_use_name": "21 = Res. Wtr. Heating",
            "iou_ac_territory": "PGE",
            "target_sector": "Residential",
            "year_represented": 2018.0,
            "source_impact_type": "calculated",
            "start_year": 2016.0,
        },
        {
            "id": 9,
            "end_use_name": "Broken",
            "iou_ac_territory": "PGE",
            "target_sector": "Commercial",
            "year_represented": 2024.0,
            "source_impact_type": "metered",
            "start_year": 2024.0,
        },
    ]
    pq.write_table(pa.Table.from_pylist(cat), folder / "catalog.parquet")
    with pq.ParquetWriter(str(folder / "hourly.parquet"), library.HOURLY_SCHEMA) as w:
        for pid, zones in ((2, ("1", "13")), (7, ("System",)), (9, ("3",))):
            hours = np.arange(1, 8761, dtype=np.int16)
            if pid == 9:
                hours = hours.copy()
                hours[100] = 100  # a duplicated hour and a missing one, as the old scrape had
            t = pa.table(
                {
                    "impact_profile_id": pa.array(np.full(8760 * len(zones), pid, dtype=np.int32)),
                    "climate_zone": pa.array(np.repeat(zones, 8760)),
                    "hour_of_year": pa.array(np.tile(hours, len(zones))),
                    "impact": pa.array(np.tile(SHAPE["Msr-LED"], len(zones))),
                },
                schema=library.HOURLY_SCHEMA,
            )
            w.write_table(t, row_group_size=len(t))
    s = library.import_library(folder / "catalog.parquet", folder / "hourly.parquet", file_label="test")
    assert s["imported"] == 2 and s["no complete zone"] == 1 and s["zones_left_out"] == 1
    assert ImpactProfile.objects.get(legacy_id=2).family == "deer"
    tou = ImpactProfile.objects.get(legacy_id=7)
    assert tou.family == "tou" and tou.current_version().represents_year == 2018
    assert library.import_library(folder / "catalog.parquet", folder / "hourly.parquet")["unchanged"] == 2
    assert not ProfileMapping.objects.exists()


def test_importing_a_consolidated_library_keeps_every_name(tmp_path, cpuc_admin):
    folder = tmp_path / "consolidated"
    folder.mkdir()
    rows = [
        {
            "id": 2,
            "end_use_name": "SWXX001 Res A",
            "iou_ac_territory": "PGE",
            "target_sector": "Res",
            "year_represented": None,
            "shape_id": 2,
        },
        {
            "id": 5,
            "end_use_name": "SWXX001 Res A",
            "iou_ac_territory": "SCE",
            "target_sector": "Res",
            "year_represented": None,
            "shape_id": 2,
        },
        {
            "id": 9,
            "end_use_name": "SWXX002 Res B",
            "iou_ac_territory": "SDGE",
            "target_sector": "Res",
            "year_represented": None,
            "shape_id": 2,
        },
    ]
    pq.write_table(pa.Table.from_pylist(rows), folder / "catalog.parquet")
    t = pa.table(
        {
            "impact_profile_id": pa.array(np.full(8760, 2, dtype=np.int32)),
            "climate_zone": pa.array(np.repeat(["13"], 8760)),
            "hour_of_year": pa.array(np.arange(1, 8761, dtype=np.int16)),
            "impact": pa.array(SHAPE["Msr-LED"]),
        },
        schema=library.HOURLY_SCHEMA,
    )
    pq.write_table(t, folder / "hourly.parquet", row_group_size=8760)
    assert library.import_library(folder / "catalog.parquet", folder / "hourly.parquet")["imported"] == 3
    first = ImpactProfile.objects.get(legacy_id=2)
    assert [p.legacy_id for p in identical.same_profile(first.current_version().content_sha256)] == [2, 5, 9]
    export = library.export(cpuc_admin)
    assert (export.profiles, export.stored_profiles) == (3, 1)

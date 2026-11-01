"""A NMEC true-up priced against the profile its parent claim was priced on (BR-05.6): Claims names the parent's profile, pinned to the
version its run's library held, in ReCET's AC_Elec_Impact_Profile_Parent column, supplies those 8,760s to the run, and shows the
incremental benefits the engine writes on the version and chain pages."""

# ruff: noqa: F811 - pytest fixtures imported by name

import io
import zipfile

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from django.urls import reverse

from cedars.core import storage
from cedars.modules.calculations import service as calculations
from cedars.modules.calculations.models import Run
from cedars.modules.claims import restatements, service
from cedars.modules.impact_profiles import custom, library
from cedars.services.refdata import profiles as packed_profiles
from cedars.services.refdata import service as refdata
from cedars.services.submissions.models import SubmissionVersion

from .conftest import sign_in
from .test_budgets import release  # noqa: F401 - the engine release fixture
from .test_claims import claims_zip, deadline, filer, linked, programs, row, tables, year  # noqa: F401
from .test_impact_profiles import developer  # noqa: F401

KEY = "PGE NMEC Office 2028"
H = np.arange(8760)
ESTIMATED = (1 + 0.5 * np.sin(2 * np.pi * H / 24)) / (1 + 0.5 * np.sin(2 * np.pi * H / 24)).sum()
METERED = (1 + 0.9 * np.cos(2 * np.pi * H / 24)) / (1 + 0.9 * np.cos(2 * np.pi * H / 24)).sum()


def profile_file(shape) -> bytes:
    out = io.StringIO()
    out.write("Climate Zone,Hour of Year,Impact\n")
    for z in ("12", "System"):
        for h, v in enumerate(shape, start=1):
            out.write(f"{z},{h},{float(v)!r}\n")
    return out.getvalue().encode()


def published_library(cpuc_admin):
    pack = library.export(cpuc_admin).pack
    refdata.record_tie_out(cpuc_admin, pack, check="profile_checks", passed=True)
    return refdata.publish_pack(cpuc_admin, pack)


def nmec(cid, quarter="2028Q1", parent="", profile=KEY, **claim_fields):
    t = linked(cid, quarter, parent=parent, **claim_fields)
    t["Measure"][0].update(AC_Elec_Impact_Profile=profile, AC_Elec_Target_Sector="Commercial")
    return t


def finish(run: Run, incremental: dict[str, float] | None = None) -> Run:
    """A worker's end with the measure-level results kept: TSB 1,000 per claim, and each true-up's incremental columns."""
    measures = [r["CEInputID"] for r in measure_rows(run)]
    inc = incremental or {}
    level = [
        {"CEInputID": cid, "PrgID": "PGE_Res", "TotalSystemBenefit": 1000.0,
         "TotalSystemBenefitIncremental": inc.get(cid), "ElecBenIncremental": inc.get(cid), "TRCBenIncremental": inc.get(cid),
         "PACBenIncremental": inc.get(cid), "ParentElecPricingStatus": "priced: hourly profile" if cid in inc else None}
        for cid in measures
    ]  # fmt: skip
    sums = [
        {"_pa": "PGE", "_prg": "PGE_Res", "_prgname": "Res", "_sector": "Com", "_segment": "RA", "_year": "2028", "_n": len(measures),
         "TotalSystemBenefit": 1000.0 * len(measures), "TRCBen": 1000.0 * len(measures), "TRCCost": 500.0,
         "TotalSystemBenefitIncremental": sum(inc.values()), "ElecBenIncremental": sum(inc.values()),
         "TRCBenIncremental": sum(inc.values()), "PACBenIncremental": sum(inc.values())}
    ]  # fmt: skip
    out = run.storage_prefix + "out/_results/"
    for name, rows in (("group_sums2_measure_level_run.parquet", sums), ("measure_level_run.parquet", level)):
        buf = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(rows), buf)
        storage.store("results").put_bytes(out + name, buf.getvalue())
    calculations.mark_started(run, worker="test")
    storage.store("results").put_bytes(run.storage_prefix + "out/receipt.json", b"{}")
    return calculations.finish(run, exit_code=0, receipt={"packs": {"files": {}}, "totals": {}})


def measure_rows(run: Run) -> list[dict]:
    import csv

    data = storage.store("uploads").get_bytes(run.inputs["job_zip"]["key"])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return list(csv.DictReader(io.StringIO(z.read("Measure.csv").decode())))


def confirm(filer, v, incremental=None) -> SubmissionVersion:  # noqa: F811
    assert v.status == SubmissionVersion.Status.VALIDATED, service.findings(v)
    finish(Run.objects.get(pk=service.price(filer, v)), incremental)
    v.refresh_from_db()
    return service.confirm(filer, v)


COST = {"ProgramCost": [row("ProgramCost", PrgID="PGE_Res")], "Site": [row("Site", SiteID="PGE-S1")]}


def test_a_true_up_is_priced_against_the_profile_version_its_parent_was_priced_on(
    client,
    filer,
    year,
    pge,
    programs,
    release,
    developer,
    cpuc_admin,
    make_user,  # noqa: F811
):
    custom.create_custom(
        developer, pge, profile_key=KEY, target_sector="Commercial", territory="PGE", data=profile_file(ESTIMATED), file_name="est.csv"
    )
    first = published_library(cpuc_admin)
    v1 = confirm(filer, service.upload_claims(filer, deadline(), pge, claims_zip(tables(nmec("PGE-2028-001"), COST)), "q1.zip"))
    assert v1.calculation["packs"]["impact_profiles"]["id"] == first.pk  # the run's pinned library, kept with the filing
    assert "true_ups" not in v1.findings or not v1.findings["true_ups"]

    # the PA replaces its custom profile with the metered 8,760 and files the true-up on it: same profile ID, now version 2
    profile = custom.ImpactProfile.objects.get(profile_key=KEY)
    custom.replace_custom(developer, pge, profile, data=profile_file(METERED), file_name="metered.csv", reason="NMEC true-up")
    published_library(cpuc_admin)
    up = tables(nmec("PGE-2028-010", "2028Q2", parent="PGE-2028-001", TotalFirstYearNetkWh="1000"))
    v2 = service.upload_claims(filer, deadline("Q2 claim"), pge, claims_zip(up), "q2.zip")
    s = v2.findings
    assert s["true_ups_count"] == 1
    entry = s["true_ups"][0]
    assert (entry["claim"], entry["parent"], entry["profile"], entry["version"], entry["library"]) == (
        "PGE-2028-010", "PGE-2028-001", KEY, 1, str(first)
    )  # fmt: skip

    # the job names the pinned version for the true-up only, and the supplied file holds version 1's 8,760s bit for bit
    with zipfile.ZipFile(io.BytesIO(storage.store("uploads").get_bytes(s["job_key"]))) as z:
        assert set(z.namelist()) == {"Measure.csv", "ProgramCost.csv"}
    run = Run.objects.get(pk=service.price(filer, v2))
    rows = {r["CEInputID"]: r for r in measure_rows(run)}
    assert rows["PGE-2028-010"]["AC_Elec_Impact_Profile_Parent"] == f"{KEY} (v1)"
    assert rows["PGE-2028-010"]["AC_Elec_Impact_Profile"] == KEY
    assert rows["PGE-2028-001"]["AC_Elec_Impact_Profile_Parent"] == ""
    supplied = pq.read_table(pa.BufferReader(storage.store("uploads").get_bytes(run.inputs["profiles_8760"]["key"]))).to_pandas()
    assert set(supplied["end_use_name"]) == {f"{KEY} (v1)"} and set(supplied["pa"]) == {"PGE"} and set(supplied["target_sector"]) == {""}
    for zone, g in supplied.groupby("climate_zone"):
        assert zone in ("12", "System") and list(g["hour_of_year"]) == list(range(1, 8761))
        assert np.array_equal(g["impact"].to_numpy(), ESTIMATED)
    assert run.spec["inputs"]["profiles_8760"]["sha256"] == s["parent_profiles_sha256"]

    # the engine reads the file as a supplied 8,760 profile, every impact exactly as stored
    overrides = pytest.importorskip("recet.overrides")
    path = storage.store("uploads").path(run.inputs["profiles_8760"]["key"]) if hasattr(storage.store("uploads"), "path") else None
    if path:
        loaded = overrides.load_user_profiles(path, normalize=False)
        shape, year_ = loaded.lookup(f"{KEY} (v1)", "PGE", "Commercial", "12")
        assert np.array_equal(shape, ESTIMATED) and year_ == 2009

    # priced: the version page shows the change by program and the parent profile; the chain shows the true-up's change
    finish(run, {"PGE-2028-010": 250.0})
    v2.refresh_from_db()
    v2 = service.confirm(filer, v2)
    sign_in(client, make_user("someone@example.com"))
    page = client.get(reverse("claims:version", args=[v2.pk])).content.decode()
    assert "True-ups priced against their parents&#x27; profiles" in page or "True-ups priced against their parents' profiles" in page
    assert "$250" in page and f"{KEY}" in page and str(first) in page
    chain = client.get(reverse("claims:links") + "?kind=claim&q=PGE-2028-010").content.decode()
    assert "+$250" in chain and f"{KEY}, version 1" in chain

    # a restatement prices the true-up against the same pinned 8,760s
    rs = restatements.start(filer, v2, organisation=None, release="0.1.80", acc_version="2026")
    restated = Run.objects.get(pk=rs.runs["restated"])
    assert restated.inputs["profiles_8760"]["sha256"] == s["parent_profiles_sha256"]


def test_a_parent_filed_in_the_same_claims_is_named_as_it_is(filer, year, pge, programs, release, developer, cpuc_admin):  # noqa: F811
    custom.create_custom(
        developer, pge, profile_key=KEY, target_sector="Commercial", territory="PGE", data=profile_file(ESTIMATED), file_name="est.csv"
    )
    published_library(cpuc_admin)
    both = tables(nmec("PGE-2028-001"), nmec("PGE-2028-002", parent="PGE-2028-001", profile="Other metered profile"), COST)
    v = service.upload_claims(filer, deadline(), pge, claims_zip(both), "q1.zip")
    entry = v.findings["true_ups"][0]
    assert entry["version"] is None and "same run" in entry["note"] and not v.findings["parent_profiles_key"]
    run = Run.objects.get(pk=service.price(filer, v))
    rows = {r["CEInputID"]: r for r in measure_rows(run)}
    assert rows["PGE-2028-002"]["AC_Elec_Impact_Profile_Parent"] == KEY and "profiles_8760" not in run.inputs


def test_claims_without_true_ups_carry_no_parent_columns(filer, year, pge, programs, release):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, claims_zip(tables(nmec("PGE-2028-001"), COST)), "q1.zip")
    run = Run.objects.get(pk=service.price(filer, v))
    assert "AC_Elec_Impact_Profile_Parent" not in measure_rows(run)[0] and "profiles_8760" not in run.inputs


def test_a_catalog_row_is_found_by_name_territory_and_sector():
    rows = [
        packed_profiles.PackedProfile("A", "PGE", "Residential", 1, None, 1),
        packed_profiles.PackedProfile("a", "PGE", "Commercial", 2, None, 2),
        packed_profiles.PackedProfile("A", "SCE", "Commercial", 1, None, 3),
    ]
    assert packed_profiles.find(rows, "A", "PGE", "Commercial").shape_id == 2
    assert packed_profiles.find(rows, "A", "PGE", "Industrial").shape_id == 1  # the name in the territory
    assert packed_profiles.find(rows, "A", "SDGE", "Commercial").shape_id == 2  # not in the territory: the name in its sector anywhere
    assert packed_profiles.find(rows, "B", "PGE", "Commercial") is None

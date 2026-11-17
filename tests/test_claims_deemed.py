import csv
import io
import zipfile
from pathlib import Path

import pytest
from django.core.management import call_command
from django.urls import reverse

from cedars.modules.claims import service
from cedars.services.audit.models import AuditEvent
from cedars.services.refdata import etrm
from cedars.services.refdata import service as refdata
from cedars.services.submissions.models import SubmissionVersion

from .conftest import sign_in
from .test_claims import claims_zip, deadline, filer, programs, row, tables, year  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

SAMPLE = Path(__file__).parent / "data" / "etrm_permutations_export.csv"
DI = "SWHC052-02-A-NR-DnDeemDI-Com-Com-Ex-cWtd-CZ01-Com-Default>2yrs-Def-GSIA-Any-Any"
DEEMED = "SWHC052-02-A-NR-DnDeemed-Com-Com-Ex-cWtd-CZ01-Com-Default>2yrs-Def-GSIA-Any-Any"
UPSTREAM = "SWHC052-02-A-NR-UpDeemed-Com-Com-Ex-cWtd-CZ01-Com-Default>2yrs-Def-GSIA-Any-Any"
ANY = "SWHC052-02-A-NR-DnDeemed-Com-Any-Ex-Any-CZ01-Com-Default>2yrs-Def-GSIA-Any-Any"


def download(tmp_path, ended=(UPSTREAM,), extra=()) -> Path:
    """The eTRM sample download with every permutation still in effect except `ended` (which ended in February 2024)."""
    lines = SAMPLE.read_text(encoding="utf-8-sig").splitlines()
    rows = list(csv.DictReader(lines[1:]))
    for r in rows:
        if r["Measure Detail ID"] not in ended:
            r["End Date"] = ""
    rows += [dict(rows[0], **e) for e in extra]
    out = io.StringIO()
    out.write(lines[0] + "\n")
    w = csv.DictWriter(out, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL)
    w.writeheader()
    w.writerows(rows)
    p = tmp_path / "SWHC052-02_permutations.csv"
    p.write_text(out.getvalue(), encoding="utf-8")
    return p


@pytest.fixture
def pack(cpuc_admin, tmp_path):
    p = download(tmp_path, extra=[{"Measure Detail ID": ANY, "Building Type": "Any", "Building HVAC": "Any"}])
    return refdata.publish_pack(cpuc_admin, etrm.load(cpuc_admin, [p], label="eTRM published permutations", version="1"))


def deemed_claim(cid: str, mdid: str, **measure) -> dict:
    """A deemed claim with every field eTRM gives left blank, then `measure`'s values."""
    claim = row("Claim", ClaimID=cid, SiteID="PGE-S1", ClaimType="Deemed", MeasDetailID=mdid, ProjectID="PGE-P1")
    for name in etrm.CLAIM_FIELDS:
        claim.pop(name, None)
    m = row("Measure", ClaimID=cid, PrgID="PGE_Res")
    for name in [*etrm.MEASURE_FIELDS, *etrm.MEASURE_DESCRIPTIONS]:
        m.pop(name, None)
    m.update(measure)
    return {"Claim": [claim], "Measure": [m]}


def upload(filer, pge, *claims):  # noqa: F811
    t = tables(*claims, ProgramCost=[row("ProgramCost", PrgID="PGE_Res")], Site=[row("Site", SiteID="PGE-S1")])
    return service.upload_claims(filer, deadline(), pge, claims_zip(t), "q1.zip")


def measure(v: SubmissionVersion, cid: str) -> dict:
    return next(r for r in service.compiled_tables(v)["Measure"].rows if r["ClaimID"] == cid)


def etrm_findings(v):
    return [(f["record_id"], f["field"], f["severity"]) for f in service.findings(v) if f["rule_id"] == "CLM-ETRM"]


def test_blank_deemed_values_are_filled_from_etrm_and_differences_rejected(filer, year, pge, programs, pack):  # noqa: F811
    same = {"NTGRkWh": "0.6", "EUL_Yrs": "20.000", "Sector": "com", "UnitkWh1stBaseline": "63.30"}  # eTRM's 0.6000, 20.00, Com, 63.3
    v = upload(filer, pge, deemed_claim("PGE-2028-001", DI), deemed_claim("PGE-2028-002", DEEMED.lower(), **same))
    assert v.status == SubmissionVersion.Status.VALIDATED, service.findings(v)
    filled = measure(v, "PGE-2028-001")
    assert filled["UnitkWh1stBaseline"] == "63.3" and filled["NTGRkWh"] == "0.6000" and filled["EUL_Yrs"] == "20.00"
    assert filled["AC_Elec_Impact_Profile"] == "DEER:HVAC_Chillers" and filled["InstallationRatekWh"] == "1.0000"
    assert filled["MeasDescription"].startswith("Air-cooled screw chiller")
    claim = next(r for r in service.compiled_tables(v)["Claim"].rows if r["ClaimID"] == "PGE-2028-001")
    assert claim["BldgLoc"] == "CZ01" and claim["MeasImpactCalcType"] == "Standard"
    assert measure(v, "PGE-2028-002")["UnitkWh1stBaseline"] == "63.30" and not etrm_findings(v)
    stored = service.own_tables(v)["Measure"].rows[0]  # the stored upload stays as filed
    assert stored["ClaimID"] == "PGE-2028-001" and not stored.get("UnitkWh1stBaseline") and not stored.get("NTGRkWh")
    s = v.findings["etrm"]
    assert s["deemed"] == 2 and s["checked"] == 2 and s["differ"] == 0 and s["filled"]["UnitkWh1stBaseline"] == 1
    assert s["pack"]["id"] == pack.pk and any("2 of 2 deemed claims checked" in n for n in v.findings["notes"])
    job = zipfile.ZipFile(io.BytesIO(service.job_zip(service.compiled_tables(v)))).read("Measure.csv").decode()
    assert next(r for r in csv.DictReader(io.StringIO(job)) if r["CEInputID"] == "PGE-2028-001")["NTGRkWh"] == "0.6000"


def test_a_value_that_differs_from_etrm_rejects_the_record(filer, year, pge, programs, pack):  # noqa: F811
    v = upload(filer, pge, deemed_claim("PGE-2028-001", DI, UnitkWh1stBaseline="70", MeasDescription="Our own words"))
    assert v.status == SubmissionVersion.Status.DRAFT
    assert etrm_findings(v) == [("PGE-2028-001", "UnitkWh1stBaseline", "reject")]  # descriptions are not compared
    f = next(f for f in service.findings(v) if f["rule_id"] == "CLM-ETRM")
    assert f["message"] == "UnitkWh1stBaseline 70 differs from eTRM's 63.3 (First Baseline - Electric Savings)" and f["value"] == "70"


def test_ntg_differences_are_warnings_and_any_is_the_pas_to_fill(filer, year, pge, programs, pack):  # noqa: F811
    v = upload(
        filer,
        pge,
        deemed_claim("PGE-2028-001", DI, NTGRkWh="0.85", NTG_ID="Com-Custom"),
        deemed_claim("PGE-2028-002", ANY, BldgType="Asm"),
        deemed_claim("PGE-2028-003", ANY),
    )
    assert sorted(etrm_findings(v)) == [("PGE-2028-001", "NTGRkWh", "warning"), ("PGE-2028-001", "NTG_ID", "warning")]
    assert measure(v, "PGE-2028-002")["BldgType"] == "Asm" and not measure(v, "PGE-2028-003")["BldgType"]
    assert ("Measure", "PGE-2028-003", "BldgType", "CLM-REQ") in {
        (f["table"], f["record_id"], f["field"], f["rule_id"]) for f in service.findings(v)
    }  # "Any": the PA supplies it, so a blank one is still required
    claim = next(r for r in service.compiled_tables(v)["Claim"].rows if r["ClaimID"] == "PGE-2028-003")
    assert not claim["BldgHVAC"] and claim["BldgLoc"] == "CZ01"
    assert v.findings["etrm"]["ntg_differ"] == 2 and v.findings["etrm"]["differ"] == 0


def test_unknown_permutations_and_ended_ones(filer, year, pge, programs, pack):  # noqa: F811
    v = upload(
        filer,
        pge,
        deemed_claim("PGE-2028-001", "SWHC052-02-A-NR-Nope"),
        deemed_claim("PGE-2028-002", "SWXX999-01-A-NR-Other"),
        deemed_claim("PGE-2028-003", UPSTREAM),
    )
    assert set(etrm_findings(v)) == {
        ("PGE-2028-001", "MeasDetailID", "reject"),
        ("PGE-2028-002", "MeasDetailID", "warning"),
        ("PGE-2028-003", "ClaimYearQuarter", "warning"),
    }
    assert v.status == SubmissionVersion.Status.DRAFT and v.findings["etrm"]["checked"] == 1


def test_without_a_published_pack_deemed_claims_are_not_checked(filer, year, pge, programs, cpuc_admin, tmp_path):  # noqa: F811
    etrm.load(cpuc_admin, [download(tmp_path)], label="eTRM published permutations", version="draft")  # a draft is not used for filings
    v = upload(filer, pge, deemed_claim("PGE-2028-001", DI, UnitkWh1stBaseline="70"))
    assert not etrm_findings(v) and v.findings["etrm"] == {
        "deemed": 1,
        "note": "Deemed claims were not checked against eTRM: no published eTRM permutations pack.",
    }


def test_loading_a_download(cpuc_admin, tmp_path, make_user, pge):
    p = download(tmp_path, extra=[{"Measure Detail ID": DI}])
    pack = etrm.load(cpuc_admin, [p], label="eTRM published permutations", version="2026-10-30")
    assert pack.status == "draft" and pack.notes == "3 permutations; 1 repeated Measure Detail IDs dropped"
    perm = etrm.lookup(pack, [DI.upper()])[DI.lower()]
    assert perm.measure_version_id == "swhc052-02" and perm.values["NTG kWh"] == "0.6000" and "Capacity Range (tons)" not in perm.values
    assert AuditEvent.objects.filter(action="pack.etrm_indexed").get().after == {"permutations": 3, "duplicates": 1}
    bad = tmp_path / "other.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(etrm.EtrmError, match="not an eTRM permutations download"):
        etrm.load(cpuc_admin, [bad], label="x", version="1")
    with pytest.raises(refdata.AccessDenied):
        etrm.load(make_user("someone@pge.example.org"), [p], label="x", version="2")
    out = io.StringIO()
    call_command("load_etrm_permutations", str(p), "--pack-version", "cmd", stdout=out)
    assert "3 permutations" in out.getvalue()


def test_the_pack_page_looks_up_a_permutation(client, cpuc_admin, pack, make_user):
    sign_in(client, make_user("someone@pge.example.org"))
    url = reverse("refdata:detail", args=[pack.pk])
    r = client.get(url)
    assert r.status_code == 200 and b"4 published permutations" in r.content
    r = client.get(url, {"id": DI})
    assert b"First Baseline - Electric Savings" in r.content and b"Measure.UnitkWh1stBaseline" in r.content
    assert b"Measure.InstallationRatekW, Measure.InstallationRatekWh, Measure.InstallationRateTherm" in r.content
    assert b"has no permutation" in client.get(url, {"id": "nope"}).content

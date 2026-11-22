"""A legacy claim year corrected on the platform (claims.legacy_corrections): the claims module lets an imported year from 2024 on be
corrected; the approved draft is compiled over the imported records, read back as claim tables, with one program cost row per program
from the weighted program costs, so the program administrator files only what changes."""

import pytest

from cedars.modules.claims import legacy, legacy_corrections, spec, upload
from cedars.modules.claims import service as claims
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied
from cedars.services.submissions import service as submissions

from .legacy_fixtures import legacy_claim
from .test_legacy_claims import _version, _year

pytestmark = pytest.mark.django_db


@pytest.fixture
def filer(make_user, pge_admin):
    u = make_user("filer@pge.example.org")
    for p in ("claims.submit", "claims.confirm"):
        access.grant(pge_admin, u, Organisation.objects.get(code="PGE"), p)
    return u


@pytest.fixture
def approver(make_user, cpuc_admin, cpuc):
    u = make_user("approver@cpuc.example.org")
    access.grant(cpuc_admin, u, cpuc, "claims.approve")
    return u


def _loaded(tmp_path, year=2025):
    pge = [
        legacy_claim(f"PGE-{year}-0001", "PGE", "PGE_Com_001", year, admin=30.0),
        legacy_claim(f"PGE-{year}-0001", "PGE", "PGE_Com_001", year, admin=10.0),  # a ClaimID legacy CEDARS repeated
        legacy_claim(f"PGE-{year}-0002", "PGE", "PGE_Res_001", year, admin=5.5),
    ]
    _year(tmp_path, year, pge=pge)
    assert legacy.load(year, only="PGE").published == ["PGE"]
    return _version("PGE", year)


def _corrected(filer, approver, v1):
    req = submissions.request_legacy_correction(filer, v1.submission, "Units were double counted")
    return submissions.decide_reopen(approver, req, True, "Go ahead")


def test_the_imported_year_reads_back_as_claim_tables(tmp_path, filer, approver):
    v1 = _loaded(tmp_path)
    assert submissions.legacy_correction_refusal(filer, v1.submission) == ""  # claims registered: the year may be corrected
    v2 = _corrected(filer, approver, v1)
    lay = spec.for_filing(v2.submission.deadline)
    base = legacy_corrections.base_tables(v1, lay)
    assert set(base) == {"Claim", "Measure", "ProgramCost"}
    assert [r["ClaimID"] for r in base["Measure"].rows] == ["PGE-2025-0001", "PGE-2025-0001#2", "PGE-2025-0002"]
    assert [r["ClaimID"] for r in base["Claim"].rows] == ["PGE-2025-0001", "PGE-2025-0001#2", "PGE-2025-0002"]
    assert set(base["Measure"].columns) <= set(lay.columns("Measure")) and "PrgID" in base["Measure"].columns
    costs = {r["PrgID"]: r for r in base["ProgramCost"].rows}
    assert costs["PGE_Com_001"]["AdminCostsOverheadAndGA"] == "40" and costs["PGE_Res_001"]["AdminCostsOverheadAndGA"] == "5.5"
    assert costs["PGE_Com_001"]["PrgYear"] == "2025" and costs["PGE_Com_001"]["PA"] == "PGE"


def test_a_correction_is_compiled_over_the_import(tmp_path, filer, approver):
    v1 = _loaded(tmp_path)
    v2 = _corrected(filer, approver, v1)
    deadline = v2.submission.deadline
    lay = spec.for_filing(deadline)
    base = legacy_corrections.base_tables(v1, lay)
    measure = {**base["Measure"].rows[2], "NumUnits": "3"}
    cost = {**base["ProgramCost"].rows[0], "AdminCostsOverheadAndGA": "55"}
    claim = base["Claim"].rows[2]
    tables = {
        "Claim": upload.Table("Claim", base["Claim"].columns, [claim]),
        "Measure": upload.Table("Measure", base["Measure"].columns, [measure]),
        "ProgramCost": upload.Table("ProgramCost", base["ProgramCost"].columns, [cost]),
    }
    v = claims.upload_claims(filer, deadline, v1.submission.organisation, upload.to_zip(tables, lay.TABLES), "correction.zip")
    assert v.pk == v2.pk
    f = v.findings
    assert f["tables"] == {"Claim": 1, "Measure": 1, "ProgramCost": 1}
    assert f["compiled"] == {"Claim": 3, "Measure": 3, "ProgramCost": 2}  # the whole year, the upload's rows over the import's
    assert f["replaced"] == {"Claim": 1, "Measure": 1, "ProgramCost": 1} and not f.get("added")
    compiled = claims.compiled_tables(v)
    assert {r["ClaimID"]: r["NumUnits"] for r in compiled["Measure"].rows}["PGE-2025-0002"] == "3"
    assert {r["PrgID"]: r["AdminCostsOverheadAndGA"] for r in compiled["ProgramCost"].rows}["PGE_Com_001"] == "55"
    assert submissions.current(v1.submission) == v1  # the import stands until the correction is confirmed


def test_an_earlier_year_is_not_offered(tmp_path, filer):
    v1 = _loaded(tmp_path, 2023)
    assert "cannot be corrected" in submissions.legacy_correction_refusal(filer, v1.submission)


def test_an_import_without_its_tables_is_refused_at_approval(tmp_path, filer, approver, monkeypatch):
    v1 = _loaded(tmp_path)
    monkeypatch.setattr(legacy_corrections, "FILES", (*legacy_corrections.FILES, "Inputs.parquet"))  # one the import does not hold
    req = submissions.request_legacy_correction(filer, v1.submission, "r")
    with pytest.raises(AccessDenied, match="Inputs.parquet"):
        submissions.decide_reopen(approver, req, True, "Go ahead")
    assert v1.submission.versions.count() == 1

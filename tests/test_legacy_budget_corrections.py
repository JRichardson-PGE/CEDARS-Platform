"""A legacy budget filing corrected on the platform (budgets.legacy_corrections): a filing whose first program year is 2024 or later
may be corrected; it is checked against the budget filing correction version of that year and priced on the same CET replica as that
year's claims, and the approved draft starts as the filing's job rebuilt from the imported records, with one program cost row per
program and year from the weighted program costs."""

import pytest

from cedars.modules.budgets import jobfile, legacy, legacy_corrections
from cedars.modules.budgets import service as budgets
from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.specifications import service as specifications
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Deadline

from .test_legacy_filings import _version, downloads  # noqa: F401 (the fixture)

pytestmark = pytest.mark.django_db


@pytest.fixture
def filer(make_user, pge_admin):
    u = make_user("budget-filer@pge.example.org")
    for p in ("budgets.submit", "budgets.confirm"):
        access.grant(pge_admin, u, Organisation.objects.get(code="PGE"), p)
    return u


@pytest.fixture
def approver(make_user, cpuc_admin, cpuc):
    u = make_user("budget-approver@cpuc.example.org")
    access.grant(cpuc_admin, u, cpuc, "budgets.approve")
    return u


def _loaded(downloads):  # noqa: F811
    downloads()
    assert legacy.load(legacy.BY_KEY["2028-2031-application"], only="PGE").published == ["PGE"]
    return _version("PGE")


def test_each_legacy_filing_is_corrected_as_its_first_program_year(downloads):  # noqa: F811
    _loaded(downloads)
    deadline = Deadline.objects.get(module="budgets", kind="Business plan application", cycle__first_year=2028)
    assert legacy_corrections.year(deadline) == 2028
    assert {f.key: f.program_year for f in legacy.FILINGS if f.first_year >= 2024} == {
        "2024-2027-application": 2024,
        "2024-2025-true-up": 2024,
        "2026-2027-tual-archived": 2026,
        "2026-2027-mid-cycle": 2026,
        "2028-2031-application-snapshot": 2028,
        "2028-2031-application": 2028,
    }
    v = specifications.for_legacy("budgets", 2028)
    assert (v.legacy_first_year, v.legacy_last_year, specifications.calculation_of(v)) == (2026, 2035, "cet-25.1")
    assert specifications.calculation_of(specifications.for_legacy("budgets", 2025)) == "cet-24.3"  # as the year's claims
    assert specifications.for_legacy("budgets", 2023) is None


def test_the_approved_draft_starts_as_the_imported_filings_job(downloads, filer, approver):  # noqa: F811
    v1 = _loaded(downloads)
    assert submissions.legacy_correction_refusal(filer, v1.submission) == ""
    req = submissions.request_legacy_correction(filer, v1.submission, "A program's costs were entered twice")
    v2 = submissions.decide_reopen(approver, req, True, "Go ahead")
    assert v2.specification_version == specifications.for_legacy("budgets", 2028)
    f = budgets.job_file(v2)
    assert f is not None and f["rebuilt_from_import"]
    from cedars.core import storage

    tables, _ = jobfile.read_upload(storage.store("uploads").get_bytes(f["key"]), "job.zip")
    _, measures = jobfile.rows(tables["Measure"])
    assert len(measures) == 8 and {jobfile.get(r, "PrgID") for r in measures} == {"PGE_Res_001"}
    _, costs = jobfile.rows(tables["ProgramCost"])
    by_year = {jobfile.get(r, "PrgYear"): r for r in costs}
    assert sorted(by_year) == ["2028", "2029", "2030", "2031"]
    assert jobfile.get(by_year["2029"], "AdminCostsOverheadAndGA") == "120"  # 100 + 20, the program's two records that year
    assert jobfile.get(by_year["2029"], "MarketingOutreach") == "55" and jobfile.get(by_year["2029"], "PA") == "PGE"
    budgets.ensure_checked(v2)  # checked when first opened for correction, as a reopened version is
    v2.refresh_from_db()
    assert v2.findings.get("records") == 12  # 8 measures and 4 program cost rows
    assert submissions.current(v1.submission) == v1  # the import stands until the correction is confirmed


def test_an_annual_filing_before_2024_is_not_offered(tmp_path, filer):
    deadline = budgets.add_filing(
        None, first_year=2023, last_year=2023, kind=legacy.FILINGS[6].kind, due_at=budgets.due_at(legacy.FILINGS[6].due), acc_version="2020"
    )
    assert legacy.FILINGS[6].key == "2023" and legacy_corrections.year(deadline) == 2023
    assert specifications.for_legacy("budgets", 2023) is None


def test_the_correction_is_priced_on_the_claims_replica_of_its_year(downloads, filer, approver, cpuc_admin):  # noqa: F811
    from cedars.modules.calculations import service as calculations
    from cedars.modules.calculations.models import Run

    calculations.add_release(
        cpuc_admin, release="0.1.90", calculations=["recet", "cet-25.1"], accs={"recet": ["2024"], "cet-25.1": ["2024"]}, default=True
    )
    v1 = _loaded(downloads)
    req = submissions.request_legacy_correction(filer, v1.submission, "A program's costs were entered twice")
    v2 = submissions.decide_reopen(approver, req, True, "Go ahead")
    v2 = submissions.record_validation(filer, v2, None, {"records": 12, "rejected": 0, "warnings": 0})
    run = Run.objects.get(pk=budgets.price(filer, v2))
    assert run.calculation == "cet-25.1" and run.submission_version_id == v2.pk and run.acc_version == "2024"
    assert not (run.settings or {}).get("allocate_statewide") and run.allocation_version_id is None

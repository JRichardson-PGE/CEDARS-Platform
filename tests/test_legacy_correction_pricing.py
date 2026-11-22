"""A legacy correction is priced on the CET replica its specification version names (cet-24.3 for 2024-2025 claims), the way legacy
CEDARS priced the year: no statewide allocation, cost-test set or supplied profiles, which a replica does not take. Every other filing
is still priced with ReCET's own calculation."""

import pytest

from cedars.modules.calculations import service as calculations
from cedars.modules.calculations.models import Run
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.submissions import service as submissions

from .test_calculations import job_zip
from .test_legacy_correction_requests import _imported, approver, filer, started  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


@pytest.fixture
def replica_release(cpuc_admin):
    return calculations.add_release(
        cpuc_admin, release="0.1.90", calculations=["recet", "cet-24.3"], accs={"recet": ["2024"], "cet-24.3": ["2022"]}, default=True
    )


def _validated_correction(filer, approver, pge):  # noqa: F811
    v1 = _imported(pge)
    req = submissions.request_legacy_correction(filer, v1.submission, "Units double counted")
    v2 = submissions.decide_reopen(approver, req, True, "Go ahead")
    return submissions.record_validation(filer, v2, None, {"records": 3, "rejected": 0, "warnings": 0})


def test_a_correction_is_priced_on_the_years_replica(tmp_path, filer, approver, pge, started, replica_release):  # noqa: F811
    v = _validated_correction(filer, approver, pge)
    run_id = submissions.price(
        filer, v, job_zip(tmp_path, year=2025), name="PGE 2025 correction", acc_version="2022", settings={"allocate_statewide": True}
    )
    run = Run.objects.get(pk=run_id)
    assert run.calculation == "cet-24.3" and run.submission_version_id == v.pk
    assert not (run.settings or {}).get("allocate_statewide") and run.allocation_version_id is None
    started_ = AuditEvent.objects.get(action="submission.pricing_started", object_id=str(v.pk))
    assert started_.after["calculation"] == "cet-24.3"


def test_a_release_without_the_replica_or_its_acc_cannot_price_it(tmp_path, filer, approver, pge, started, cpuc_admin):  # noqa: F811
    calculations.add_release(cpuc_admin, release="0.1.91", calculations=["recet"], accs={"recet": ["2022"]}, default=True)
    v = _validated_correction(filer, approver, pge)
    with pytest.raises(AccessDenied):
        submissions.price(filer, v, job_zip(tmp_path, year=2025), name="x", acc_version="2022")
    reason = AuditEvent.objects.filter(action="submission.pricing_unavailable").latest("pk").after["reason"]
    assert "does not offer CET 24.3 replica" in reason


def test_any_other_filing_is_never_priced_on_a_replica(tmp_path, filer, pge, replica_release):  # noqa: F811
    from cedars.modules.calculations.service import RunRequest

    v = _imported(pge)
    req = RunRequest(name="x", release=replica_release, calculation="cet-24.3", acc_version="2022")
    with pytest.raises(AccessDenied, match="never another filing"):
        calculations.create_run(filer, pge, req, job_zip(tmp_path), submission_version=v)

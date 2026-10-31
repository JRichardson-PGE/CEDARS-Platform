import io
from datetime import timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from django.core.management import call_command
from django.utils import timezone

from cedars.core import storage
from cedars.services.accounts import service as access
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry
from cedars.services.registry.models import FilingCycle
from cedars.services.snapshots import service as snapshots
from cedars.services.snapshots.models import Release, Snapshot
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

pytestmark = pytest.mark.django_db


@pytest.fixture
def filer(make_user, pge, pge_admin):
    u = make_user("analyst@pge.com")
    for p in ("claims.submit", "claims.confirm", "budgets.submit", "budgets.confirm"):
        access.grant(pge_admin, u, pge, p)
    return u


def quarter(year, number):
    return next(p for p in registry.ensure_periods(year) if p.kind == "quarter" and p.number == number)


def claims_deadline(cpuc_admin, number, year=2028, days=10):
    kind = f"Q{number} claim"
    return submissions.add_deadline(
        cpuc_admin, module="claims", kind=kind, period=quarter(year, number), due_at=timezone.now() + timedelta(days=days)
    )


def results(rows: int, scale: float = 1.0) -> bytes:
    table = pa.table(
        {
            "CEInputID": [f"M{i}" for i in range(rows)],
            "PrgID": ["PGE_Res_HVAC"] * rows,
            "TotalSystemBenefit": [scale * (i + 1) for i in range(rows)],
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def priced(filer, deadline, pge, *, rows=3, scale=1.0, write=True, run="run-1"):
    v = submissions.start(filer, deadline, pge)
    submissions.record_validation(filer, v, None, {"records": rows, "rejected": 0, "warnings": 0})
    key = f"runs/{run}/out/_results/measure_level_run.parquet"
    if write:
        storage.store("results").put_bytes(key, results(rows, scale))
    submissions.record_calculation(None, v, run=run, input_hash="h", details={"results": {"measure_level": key, "group_sums": None}})
    return SubmissionVersion.objects.get(pk=v.pk)


def confirm(filer, v, capture):
    with capture(execute=True):
        return submissions.confirm(filer, v, current_input_hash="h")


def test_a_published_version_is_frozen_and_released(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, rows=4), django_capture_on_commit_callbacks)
    snap = Snapshot.objects.get(version=v)
    assert (snap.dataset, snap.year, snap.rows, snap.run) == ("claims", 2028, 4, "run-1")
    frozen = storage.store("snapshots").get_bytes(snap.key)
    assert frozen == storage.store("results").get_bytes(snap.source_key)
    assert snap.sha256 == storage.sha256_of(storage.store("snapshots").path(snap.key)) and snap.bytes == len(frozen)
    release = snapshots.current_release("claims")
    assert release.number == 1 and list(release.snapshots.all()) == [snap]
    assert snapshots.as_of("claims") == release.created_at and snapshots.as_of("forecast") is None
    [f] = snapshots.release_files(release)
    assert f["organisation"] == "PGE" and f["year"] == 2028 and f["kind"] == "Q1 claim" and f["uri"].startswith("file://")
    actions = set(AuditEvent.objects.filter(action__startswith="snapshot.").values_list("action", flat=True))
    assert actions == {"snapshot.created", "snapshot.released"}


def test_a_later_quarter_replaces_the_earlier_one(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):
    """Claims are compiled year to date, so a release never holds two quarters of one PA's year (they would add up twice)."""
    q1 = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, run="r1"), django_capture_on_commit_callbacks)
    q2 = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 2), pge, run="r2"), django_capture_on_commit_callbacks)
    release = snapshots.current_release("claims")
    assert release.number == 2 and [s.version_id for s in release.snapshots.all()] == [q2.pk]
    assert Snapshot.objects.filter(version=q1).exists()  # kept, just not in force
    other_year = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1, year=2029), pge, run="r3"), django_capture_on_commit_callbacks)
    assert {s.version_id for s in snapshots.current_release("claims").snapshots.all()} == {q2.pk, other_year.pk}


def test_every_filing_stands_until_a_new_version_replaces_it(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):
    cycle = FilingCycle.objects.create(first_year=2028, last_year=2031)
    due = timezone.now() - timedelta(days=1)  # past due: a correction is a new version
    tual = submissions.add_deadline(cpuc_admin, module="budgets", kind="TUAL", cycle=cycle, due_at=due)
    mcal = submissions.add_deadline(cpuc_admin, module="budgets", kind="MCAL", cycle=cycle, due_at=due + timedelta(days=2))
    submissions.set_policy(cpuc_admin, "budgets", late_confirmation=True)
    a = confirm(filer, priced(filer, tual, pge, run="t1"), django_capture_on_commit_callbacks)
    b = confirm(filer, priced(filer, mcal, pge, run="m1"), django_capture_on_commit_callbacks)
    assert {s.version_id for s in snapshots.current_release("forecast").snapshots.all()} == {a.pk, b.pk}
    [f, _] = snapshots.release_files(snapshots.current_release("forecast"))
    assert f["cycle"] == "2028-2031" and f["year"] is None
    submissions.set_policy(cpuc_admin, "budgets", reopen_after_due="open")
    a2 = submissions.request_reopen(filer, a.submission, "corrected incentives")
    submissions.record_validation(filer, a2, None, {"records": 3, "rejected": 0, "warnings": 0})
    storage.store("results").put_bytes("runs/t2/out/_results/measure_level_run.parquet", results(3, 2.0))
    details = {"results": {"measure_level": "runs/t2/out/_results/measure_level_run.parquet"}}
    submissions.record_calculation(None, a2, run="t2", input_hash="h", details=details)
    a2 = confirm(filer, SubmissionVersion.objects.get(pk=a2.pk), django_capture_on_commit_callbacks)
    assert {s.version_id for s in snapshots.current_release("forecast").snapshots.all()} == {a2.pk, b.pk}


def test_unconfirming_withdraws_the_snapshot(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge), django_capture_on_commit_callbacks)
    snap = Snapshot.objects.get(version=v)
    with django_capture_on_commit_callbacks(execute=True):
        submissions.unconfirm(filer, SubmissionVersion.objects.get(pk=v.pk), "wrong file")
    assert not Snapshot.objects.exists() and not storage.store("snapshots").exists(snap.key)
    release = snapshots.current_release("claims")
    assert release.number == 2 and not release.snapshots.exists() and "withdrawn" in release.reason
    assert AuditEvent.objects.filter(action="snapshot.withdrawn", before__key=snap.key).exists()


def test_no_results_no_snapshot(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):
    v = submissions.start(filer, claims_deadline(cpuc_admin, 1), pge)
    submissions.record_validation(filer, v, None, {"records": 1, "rejected": 0, "warnings": 0})
    submissions.record_calculation(None, v, run="r", input_hash="h")
    confirm(filer, SubmissionVersion.objects.get(pk=v.pk), django_capture_on_commit_callbacks)
    assert not Snapshot.objects.exists() and not Release.objects.exists()


def test_a_failed_copy_leaves_the_version_published_and_the_command_repairs_it(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, write=False), django_capture_on_commit_callbacks)
    assert v.status == "published" and not Snapshot.objects.exists()
    assert AuditEvent.objects.filter(action="snapshot.failed", object_id=str(v.pk)).exists()
    storage.store("results").put_bytes(v.calculation["results"]["measure_level"], results(3))
    out = io.StringIO()
    call_command("snapshot_published", stdout=out)
    assert Snapshot.objects.filter(version=v).exists() and "1 snapshot" in out.getvalue()
    call_command("snapshot_published", stdout=out)  # nothing left to do, no new release
    assert Release.objects.filter(dataset="claims").count() == 1


def test_a_release_is_made_only_when_what_stands_changes():
    assert snapshots.rebuild("claims", "nothing published") is None  # no empty first release
    assert snapshots.current_release("claims") is None

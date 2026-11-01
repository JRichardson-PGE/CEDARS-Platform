"""The scan for personal information before publication (BR-08.5): a snapshot whose text columns hold what look like emails, phone
numbers, street addresses, Social Security numbers or account numbers is held out of the public release, its owner and the CPUC system
administrators are told, and a system administrator releases it with a reason; every step is audited. No value found is kept."""

import io
import json
from datetime import timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from django.urls import reverse
from django.utils import timezone

from cedars.core import storage
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications.models import Notification
from cedars.services.registry.models import FilingCycle
from cedars.services.snapshots import pii
from cedars.services.snapshots import service as snapshots
from cedars.services.snapshots.models import PIIScan, Snapshot
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import SubmissionVersion

from .conftest import page_only, sign_in
from .test_snapshots import claims_deadline, confirm, filer, quarter  # noqa: F401 - fixtures and helpers

pytestmark = pytest.mark.django_db

EMAIL, PHONE, ADDRESS, SSN, ACCOUNT = "jane@example.com", "415-555-0123", "742 Evergreen Ter", "123-45-6789", "9988776655"
RAW = (EMAIL, PHONE, ADDRESS, SSN, ACCOUNT, "jane", "Evergreen", "0123", "6789", "8776655")
CLEAN = [
    "LED retrofit",
    "Res Furnace 80 AFUE",
    "PGE-2028-000123456",
    "0.123456789012",
    "2028Q1",
    "4 ft T8 lamp",
    "2 Way valve",
    "SWLG009-02",
]


def table(descriptions: list[str], *, extra: dict | None = None) -> bytes:
    n = len(descriptions)
    t = pa.table(
        {
            "CEInputID": [f"M{i}" for i in range(n)],
            "PrgID": ["PGE_Res_HVAC"] * n,
            "MeasDescription": descriptions,
            "TotalSystemBenefit": [float(i + 1) for i in range(n)],
            **(extra or {}),
        }
    )
    buf = io.BytesIO()
    pq.write_table(t, buf, row_group_size=4)
    return buf.getvalue()


def priced(filer, deadline, pge, descriptions, *, run="run-1", submission_version=None, extra=None):  # noqa: F811
    v = submission_version or submissions.start(filer, deadline, pge)
    submissions.record_validation(filer, v, None, {"records": len(descriptions), "rejected": 0, "warnings": 0})
    key = f"runs/{run}/out/_results/measure_level_run.parquet"
    storage.store("results").put_bytes(key, table(descriptions, extra=extra))
    submissions.record_calculation(None, v, run=run, input_hash="h", details={"results": {"measure_level": key, "group_sums": None}})
    return SubmissionVersion.objects.get(pk=v.pk)


def in_release(dataset="claims") -> set[int]:
    r = snapshots.current_release(dataset)
    return {s.version_id for s in r.snapshots.all()} if r else set()


def no_raw(text: str) -> bool:
    return not any(x in text for x in RAW)


# --- the scanner


def test_the_scanner_finds_each_kind_and_keeps_no_value(tmp_path):
    path = tmp_path / "r.parquet"
    descriptions = [
        *CLEAN,
        f"Contact {EMAIL}",
        f"Call {PHONE}",
        f"Site at {ADDRESS}, SF",
        f"SSN {SSN}",
        f"acct {ACCOUNT}",
        "(415) 555 0199",
    ]
    path.write_bytes(table(descriptions))
    r = pii.scan(path)
    found = {(f.column, f.pattern): f for f in r.findings}
    assert set(found) == {("MeasDescription", p) for p in ("email", "phone", "street_address", "ssn", "account_number")}
    assert found[("MeasDescription", "phone")].records == 2 and found[("MeasDescription", "phone")].record_ids == ["M9", "M13"]
    assert found[("MeasDescription", "email")].examples == ["j***@e******.c**"]
    assert found[("MeasDescription", "phone")].examples[0] == "4**-***-****"
    assert r.rows == len(descriptions) and r.columns == ["MeasDescription"] and r.skipped == ["CEInputID", "PrgID"]
    assert no_raw(json.dumps([f.as_dict() for f in r.findings]))


def test_ordinary_measure_text_is_clear(tmp_path):
    path = tmp_path / "r.parquet"
    path.write_bytes(table(CLEAN))
    assert pii.scan(path).clear


def test_settings_choose_patterns_and_columns(tmp_path):
    path = tmp_path / "r.parquet"
    path.write_bytes(table([f"acct {ACCOUNT}", f"Contact {EMAIL}"], extra={"MeasureCode": [ACCOUNT, "X"]}))
    r = pii.scan(path, patterns=["email"])
    assert [(f.column, f.pattern) for f in r.findings] == [("MeasDescription", "email")]
    r = pii.scan(path, exempt=["measdescription", "CEInputID", "PrgID"])
    assert [(f.column, f.pattern) for f in r.findings] == [("MeasureCode", "account_number")]
    assert r.skipped == ["CEInputID", "PrgID", "MeasDescription"]


def test_dictionary_columns_and_ids_from_later_row_groups(tmp_path):
    path = tmp_path / "r.parquet"
    descriptions = CLEAN + [f"Contact {EMAIL}"]
    t = pa.table({"CEInputID": [f"M{i}" for i in range(len(descriptions))], "MeasDescription": pa.array(descriptions).dictionary_encode()})
    pq.write_table(t, path, row_group_size=3)
    [f] = pii.scan(path).findings
    assert f.record_ids == [f"M{len(CLEAN)}"] and f.records == 1


# --- the hold


def test_a_hit_holds_the_publication_and_tells_the_owner(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, [*CLEAN, f"Contact {EMAIL}"]), django_capture_on_commit_callbacks)
    assert v.status == "published"  # the filing is confirmed and published; only its public data wait
    snap = Snapshot.objects.get(version=v)
    scan = snap.pii_scan
    assert scan.held and scan.records == 1 and scan.alerted == 2
    assert snapshots.current_release("claims") is None
    held = AuditEvent.objects.get(action="snapshot.held")
    assert held.after["findings"] == [{"column": "MeasDescription", "pattern": "email", "records": 1}]
    assert AuditEvent.objects.get(action="snapshot.created").after["pii_scan"] == "held"
    told = Notification.objects.filter(title__startswith="Publication held")
    assert {n.user for n in told} == {filer, cpuc_admin}
    assert all(no_raw(n.body) and n.link == f"/publication/holds/{snap.pk}/" for n in told)
    assert no_raw(json.dumps(scan.findings))


def test_a_clean_publication_is_released_as_before(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, CLEAN), django_capture_on_commit_callbacks)
    assert Snapshot.objects.get(version=v).pii_scan.outcome == "clear" and in_release() == {v.pk}
    assert not Notification.objects.filter(title__startswith="Publication held").exists()


def test_a_held_quarter_leaves_the_previous_quarter_standing(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    q1 = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, CLEAN, run="r1"), django_capture_on_commit_callbacks)
    q2 = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 2), pge, [f"Call {PHONE}"], run="r2"), django_capture_on_commit_callbacks)
    assert in_release() == {q1.pk} and snapshots.current_release("claims").number == 1
    snapshots.release_hold(cpuc_admin, Snapshot.objects.get(version=q2), "a test of the release")
    assert in_release() == {q2.pk}


def test_a_held_correction_leaves_the_filed_version_standing(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    cycle = FilingCycle.objects.create(first_year=2028, last_year=2031)
    tual = submissions.add_deadline(cpuc_admin, module="budgets", kind="TUAL", cycle=cycle, due_at=timezone.now() - timedelta(days=1))
    submissions.set_policy(cpuc_admin, "budgets", late_confirmation=True, reopen_after_due="open")
    a = confirm(filer, priced(filer, tual, pge, CLEAN, run="t1"), django_capture_on_commit_callbacks)
    a2 = submissions.request_reopen(filer, a.submission, "corrected incentives")
    a2 = confirm(
        filer, priced(filer, tual, pge, [f"Site at {ADDRESS}"], run="t2", submission_version=a2), django_capture_on_commit_callbacks
    )
    assert in_release("forecast") == {a.pk}
    assert snapshots.superseded(Snapshot.objects.get(version=a)) and not snapshots.superseded(Snapshot.objects.get(version=a2))


def test_only_a_system_administrator_releases_and_only_with_a_reason(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, [f"SSN {SSN}"]), django_capture_on_commit_callbacks)
    snap = Snapshot.objects.get(version=v)
    with pytest.raises(AccessDenied):
        snapshots.release_hold(filer, snap, "mine")
    with pytest.raises(AccessDenied):
        snapshots.release_hold(cpuc_admin, snap, "  ")
    scan = snapshots.release_hold(cpuc_admin, snap, "the numbers are test fixtures")
    assert scan.outcome == "released" and scan.released_by == cpuc_admin and in_release() == {v.pk}
    event = AuditEvent.objects.get(action="snapshot.hold_released")
    assert event.reason == "the numbers are test fixtures" and event.actor == cpuc_admin
    assert Notification.objects.filter(user=filer, title__startswith="Publication released").exists()
    with pytest.raises(AccessDenied):
        snapshots.release_hold(cpuc_admin, snap, "again")


def test_a_settings_change_and_a_new_scan_clear_a_false_hit(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    descriptions = CLEAN
    extra = {"MeasureCode": [ACCOUNT] * len(CLEAN)}
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, descriptions, extra=extra), django_capture_on_commit_callbacks)
    snap = Snapshot.objects.get(version=v)
    assert snap.pii_scan.held and in_release() == set()
    with pytest.raises(AccessDenied):
        snapshots.set_scan_settings(filer, patterns=list(pii.BY_CODE), exempt_columns=["MeasureCode"], reason="codes")
    with pytest.raises(AccessDenied):
        snapshots.set_scan_settings(cpuc_admin, patterns=["nope"], exempt_columns=[], reason="codes")
    snapshots.set_scan_settings(
        cpuc_admin, patterns=list(pii.BY_CODE), exempt_columns=["CEInputID", "PrgID", "MeasureCode"], reason="codes"
    )
    changed = AuditEvent.objects.get(action="snapshot.scan_settings_changed")
    assert changed.before["exempt_columns"] == ["CEInputID", "PrgID"] and changed.after["exempt_columns"][-1] == "MeasureCode"
    scan = snapshots.rescan(cpuc_admin, snap)
    assert scan.outcome == "clear" and scan.skipped == ["CEInputID", "PrgID", "MeasureCode"] and in_release() == {v.pk}
    assert AuditEvent.objects.get(action="snapshot.rescanned").after["outcome"] == "clear"
    with pytest.raises(AccessDenied):
        snapshots.rescan(cpuc_admin, snap)


def test_a_file_that_cannot_be_scanned_is_held(filer, cpuc_admin, pge, django_capture_on_commit_callbacks, monkeypatch):  # noqa: F811
    def broken(*a, **k):
        raise OSError("bad file")

    monkeypatch.setattr(pii, "scan", broken)
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, CLEAN), django_capture_on_commit_callbacks)
    scan = Snapshot.objects.get(version=v).pii_scan
    assert scan.held and "bad file" in scan.error and in_release() == set()


def test_a_snapshot_made_before_the_scan_stands(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, [f"Contact {EMAIL}"]), django_capture_on_commit_callbacks)
    PIIScan.objects.all().delete()
    snapshots.rebuild("claims", "older snapshot")
    assert in_release() == {v.pk}


def test_unconfirming_a_held_filing_removes_its_scan(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, [f"Contact {EMAIL}"]), django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        submissions.unconfirm(filer, SubmissionVersion.objects.get(pk=v.pk), "remove the email")
    assert not PIIScan.objects.exists() and not Snapshot.objects.exists()


# --- the pages


@pytest.fixture
def held_snap(filer, cpuc_admin, pge, django_capture_on_commit_callbacks):  # noqa: F811
    v = confirm(filer, priced(filer, claims_deadline(cpuc_admin, 1), pge, [*CLEAN, f"Call {PHONE}"]), django_capture_on_commit_callbacks)
    return Snapshot.objects.get(version=v)


def test_the_owner_sees_the_hold_and_cannot_release_it(client, filer, held_snap):  # noqa: F811
    sign_in(client, filer)
    page = page_only(client.get(reverse("snapshots:holds")).content.decode())
    assert "PGE" in page and "Waiting" in page and no_raw(page)
    page = page_only(client.get(reverse("snapshots:hold", args=[held_snap.pk])).content.decode())
    assert "MeasDescription" in page and "4**-***-****" in page and "M8" in page and no_raw(page)
    assert "Release to the public data" not in page
    r = client.post(reverse("snapshots:hold", args=[held_snap.pk]), {"action": "release", "reason": "mine"})
    assert r.status_code == 302 and PIIScan.objects.get(snapshot=held_snap).held
    assert client.get(reverse("snapshots:scan_settings")).status_code == 403


def test_another_organization_cannot_see_the_hold(client, make_user, cpuc_admin, held_snap):
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    other = make_user("analyst@sce.com")
    access.ensure_membership(cpuc_admin, other, sce)
    sign_in(client, other)
    assert client.get(reverse("snapshots:hold", args=[held_snap.pk])).status_code == 404
    assert "Nothing is held." in client.get(reverse("snapshots:holds")).content.decode()


def test_a_system_administrator_releases_from_the_page(client, cpuc_admin, held_snap):
    sign_in(client, cpuc_admin)
    page = client.get(reverse("snapshots:hold", args=[held_snap.pk])).content.decode()
    assert "Release to the public data" in page
    r = client.post(reverse("snapshots:hold", args=[held_snap.pk]), {"action": "release", "reason": "a sample phone number"})
    assert r.status_code == 302 and PIIScan.objects.get(snapshot=held_snap).outcome == "released"
    page = client.get(reverse("snapshots:holds")).content.decode()
    assert "Released" in page and "a sample phone number" in page


def test_the_settings_page_saves_with_a_reason(client, cpuc_admin):
    sign_in(client, cpuc_admin)
    assert client.get(reverse("snapshots:scan_settings")).status_code == 200
    r = client.post(
        reverse("snapshots:scan_settings"),
        {"patterns": ["email", "phone"], "exempt_columns": "CEInputID\nPrgID, MeasureCode", "reason": "test"},
    )
    assert r.status_code == 302
    cfg = snapshots.scan_settings()
    assert (
        cfg.patterns == ["email", "phone"] and cfg.exempt_columns == ["CEInputID", "PrgID", "MeasureCode"] and cfg.changed_by == cpuc_admin
    )

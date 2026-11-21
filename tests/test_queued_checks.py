"""Queued checks (cedars/services/submissions/queued.py): an upload too large to check while the page waits is kept, shown in the queue
on its filing's page, and checked by a worker exactly as the request would have checked it."""

import io
import json
import zipfile
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from cedars.core import storage
from cedars.core import uploads as core_uploads
from cedars.modules.claims import service
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.audit.models import AuditEvent
from cedars.services.releases import service as releases
from cedars.services.submissions import queued
from cedars.services.submissions.models import QueuedCheck, SubmissionVersion
from cedars.services.vault.models import PIIUpload

from .conftest import sign_in
from .test_claims import deadline, filer, programs, q1, year  # noqa: F401 - fixtures
from .test_claims_pii import pii_zip

pytestmark = pytest.mark.django_db(databases=["default", "vault"])


@pytest.fixture
def small_limit(settings):
    """Anything over three records is queued, and a local worker would take it (the test runs it with queued.execute)."""
    settings.QUEUED_CHECK_RECORDS = 3
    settings.QUEUED_CHECKS_BACKEND = "local"
    return settings


def upload(client, data=None, name="q1.zip"):
    return client.post(reverse("claims:upload", args=[deadline().pk]), {"file": SimpleUploadedFile(name, data or q1())}, follow=True)


def test_an_upload_is_measured_without_reading_its_tables():
    data = q1()
    size = core_uploads.measure([("q1.zip", data)])
    assert size.records == 8  # the lines after each CSV's header
    assert size.bytes == sum(i.file_size for i in zipfile.ZipFile(io.BytesIO(data)).infolist())
    assert core_uploads.measure([("Measure.csv", b"a,b\n1,2\n3,4\n")]) == core_uploads.Size(2, 12)
    assert core_uploads.measure([("q1.zip", data)], enough=1).records > 1  # stops counting once over


def test_a_large_upload_is_queued_shown_in_the_queue_then_checked_by_a_worker(client, filer, year, pge, programs, small_limit):  # noqa: F811
    sign_in(client, filer)
    r = upload(client)
    assert b"checked by a worker instead of while you wait" in r.content
    q = QueuedCheck.objects.get()
    assert (q.kind, q.status, q.created_by) == ("claims.upload", "queued", filer) and q.records > 3  # counted until over the limit
    assert storage.store("uploads").exists(q.files[0]["key"])
    assert b"is waiting in the queue to be checked: it is next" in r.content
    assert b"No worker is taking queued checks just now" in r.content  # none has polled
    assert json.loads(client.get(reverse("submissions:queued_status", args=[q.pk])).content)["ended"] is False
    v = SubmissionVersion.objects.get()  # the filing was started (who may file is checked before queuing), not checked yet
    assert not v.files and not v.findings

    r = upload(client)  # a second upload waits for the first
    assert b"An upload of this filing is waiting in the queue" in r.content and QueuedCheck.objects.count() == 1

    q = queued.execute(q.pk, "test-worker")  # what the worker's child process runs
    assert q.status == "succeeded" and q.result["version"] == v.pk and "Checked 8 records: none rejected" in q.result["text"]
    assert not storage.store("uploads").exists(q.files[0]["key"])  # the kept files go once checked
    v.refresh_from_db()
    assert v.status == SubmissionVersion.Status.VALIDATED and v.created_by == filer
    assert AuditEvent.objects.filter(action="queued_check.finished", actor=filer).exists()
    status = json.loads(client.get(reverse("submissions:queued_status", args=[q.pk])).content)
    assert status["ended"] is True and "Checked 8 records" in status["text"]

    page = client.get(reverse("claims:period", args=[deadline().pk])).content
    assert b"Your large upload was checked" in page and b"Checked 8 records: none rejected" in page
    assert b"Your large upload was checked" not in client.get(reverse("claims:period", args=[deadline().pk])).content  # shown once


def test_a_small_upload_is_still_checked_while_the_page_waits(client, filer, year, pge, programs):  # noqa: F811
    sign_in(client, filer)
    assert b"Checked 8 records: none rejected" in upload(client).content
    assert not QueuedCheck.objects.exists()


def test_the_year_to_date_counts_toward_the_limit(filer, year, pge, programs, settings):  # noqa: F811
    v = service.upload_claims(filer, deadline(), pge, q1(), "q1.zip")
    assert service.year_records(pge, deadline()) == 0  # Q1 goes over nothing
    assert service.version_records(v) == sum(v.findings["compiled"].values())
    settings.QUEUED_CHECK_RECORDS = service.version_records(v) - 1
    q = queued.queue_if_large(
        filer, "claims.check", deadline=deadline(), organisation=pge, version=v, extra_records=service.version_records(v)
    )
    assert q is not None and q.records == service.version_records(v)


def test_a_refused_upload_says_why_on_the_page(client, filer, year, pge, programs, small_limit):  # noqa: F811
    small_limit.QUEUED_CHECK_RECORDS = -1  # everything is queued
    sign_in(client, filer)
    upload(client, b"nope")
    q = queued.execute(QueuedCheck.objects.get().pk)
    assert q.status == "failed" and "not a valid .zip" in q.error
    page = client.get(reverse("claims:period", args=[deadline().pk])).content
    assert b"Your large upload was not checked" in page and b"not a valid .zip" in page


def test_who_may_not_file_is_refused_before_anything_is_queued(client, make_user, pge, pge_admin, year, programs, small_limit):  # noqa: F811
    reader = make_user("reader@pge.example.org", "Rea Der")
    access.grant(pge_admin, reader, pge, "claims.read")
    sign_in(client, reader)
    upload(client)
    assert not QueuedCheck.objects.exists()


def test_check_again_and_a_pii_upload_are_queued_too(client, filer, year, pge, programs, pge_admin, cpuc_admin, small_limit):  # noqa: F811
    access.approve_pii(cpuc_admin, access.grant(pge_admin, filer, pge, perms.PII))
    v = service.upload_claims(filer, deadline(), pge, q1(), "q1.zip")
    sign_in(client, filer)
    client.post(reverse("claims:check", args=[deadline().pk]), follow=True)
    q = QueuedCheck.objects.get(kind="claims.check")
    assert q.version == v and queued.execute(q.pk).status == "succeeded"
    small_limit.QUEUED_CHECK_RECORDS = 1
    r = client.post(reverse("claims:pii_upload", args=[deadline().pk]), {"file": SimpleUploadedFile("p.zip", pii_zip())}, follow=True)
    assert b"checked by a worker" in r.content
    q = QueuedCheck.objects.get(kind="claims.pii")
    q = queued.execute(q.pk)
    assert q.status == "succeeded" and "kept in the vault" in q.result["text"]
    assert PIIUpload.objects.get().status == PIIUpload.Status.ACCEPTED


def test_the_api_answers_202_with_the_queued_check(client, filer, year, pge, programs, small_limit):  # noqa: F811
    from cedars.services.accounts import tokens

    secret = tokens.create_token(filer, pge, name="claims system", scopes=["claims"], days=30)[1]
    auth = {"HTTP_AUTHORIZATION": f"Bearer {secret}"}
    r = client.post(reverse("claims-api:upload", args=[deadline().pk]), {"file": SimpleUploadedFile("q1.zip", q1())}, **auth)
    assert r.status_code == 202, r.content
    body = r.json()
    assert body["status"] == "queued" and body["ahead"] == 0 and body["records"] > 3
    queued.execute(body["id"])
    body = client.get(body["url"], **auth).json()
    assert body["ended"] and body["status"] == "succeeded" and body["version"].endswith(f"/versions/{SubmissionVersion.objects.get().pk}")


def _queued(who, pge, n=1) -> list[QueuedCheck]:
    files = [("q1.zip", q1())]
    return [queued.enqueue(who, "claims.upload", deadline=deadline(), organisation=pge, files=files, records=8) for _ in range(n)]


def test_checks_run_oldest_first_and_no_more_than_the_limit_at_once(filer, year, pge, programs, settings, cpuc_admin):  # noqa: F811
    settings.QUEUED_CHECKS_BACKEND = "local"
    a, b, c = _queued(filer, pge, 3)
    assert [queued.position(x) for x in (a, b, c)] == [0, 1, 2]
    assert queued.claim_next("w1") == a
    assert queued.claim_next("w2") is None  # one at a time by default (queued_checks_at_once)
    releases.set_limit(cpuc_admin, "queued_checks_at_once", 2, "two workers' worth")
    assert queued.claim_next("w2") == b
    assert "being checked" in queued.state(QueuedCheck.objects.get(pk=a.pk))["text"]
    assert "it is next" in queued.state(c)["text"]


def test_a_worker_that_stops_puts_its_check_back_once_then_fails_it(filer, year, pge, programs, settings):  # noqa: F811
    settings.QUEUED_CHECKS_BACKEND = "local"
    (a,) = _queued(filer, pge)
    queued.claim_next("gone:1")
    assert queued.recover() == 1 and QueuedCheck.objects.get(pk=a.pk).status == "queued"
    queued.claim_next("gone:2")
    queued.recover()
    a.refresh_from_db()
    assert a.status == "failed" and "stopped twice" in a.error
    (b,) = _queued(filer, pge)
    queued.claim_next("w")
    queued._after_child(b.pk, -9)
    b.refresh_from_db()
    assert b.status == "failed" and "ran out of memory" in b.error


def test_the_local_worker_runs_each_check_in_a_process_of_its_own(filer, year, pge, programs, settings, monkeypatch):  # noqa: F811
    settings.QUEUED_CHECKS_BACKEND = "local"
    a, b = _queued(filer, pge, 2)
    started = []

    class Done:
        def __init__(self, q):
            started.append(q.pk)
            queued.execute(q.pk, "child")

        def poll(self):
            return 0

    monkeypatch.setattr(queued, "_child", Done)
    monkeypatch.setattr(queued, "close_old_connections", lambda: None)  # the test's own connection
    assert queued.work(once=True, idle=0) == 2
    assert started == [a.pk, b.pk] and set(QueuedCheck.objects.values_list("status", flat=True)) == {"succeeded"}
    assert queued.workers_running()


def test_on_aws_batch_each_check_is_a_job(filer, year, pge, programs, settings):  # noqa: F811
    settings.QUEUED_CHECKS_BACKEND = "batch"
    settings.QUEUED_CHECKS_BATCH_QUEUE, settings.QUEUED_CHECKS_BATCH_JOB_DEFINITION = "checks", "cedars-checks"
    a, b = _queued(filer, pge, 2)

    class Batch:
        submitted, jobs = [], {}

        def submit_job(self, **kw):
            self.submitted.append(kw)
            return {"jobId": f"job-{len(self.submitted)}"}

        def describe_jobs(self, jobs):
            return {"jobs": [{"jobId": j, **self.jobs.get(j, {"status": "RUNNABLE"})} for j in jobs]}

    batch = Batch()
    assert queued.submit_waiting(batch) == 1  # one at a time
    assert batch.submitted[0]["parameters"] == {"check_id": str(a.pk)} and batch.submitted[0]["jobQueue"] == "checks"
    batch.jobs["job-1"] = {"status": "FAILED", "statusReason": "OutOfMemoryError: Container killed due to memory usage"}
    assert queued.sync_batch(batch) == 1
    a.refresh_from_db()
    assert a.status == "failed" and "ran out of memory" in a.error
    assert batch.submitted[1]["parameters"] == {"check_id": str(b.pk)}  # the next one took its place
    assert queued.execute(b.pk).status == "succeeded"  # what the job's container runs


def test_the_command_runs_one_check(filer, year, pge, programs, settings):  # noqa: F811
    settings.QUEUED_CHECKS_BACKEND = "local"
    (a,) = _queued(filer, pge)
    call_command("run_queued_checks", "--check", str(a.pk))
    assert QueuedCheck.objects.get(pk=a.pk).status == "succeeded"


def test_inline_checks_run_once_the_request_commits(filer, year, pge, programs, django_capture_on_commit_callbacks):  # noqa: F811
    with django_capture_on_commit_callbacks(execute=True):
        (a,) = _queued(filer, pge)
    assert QueuedCheck.objects.get(pk=a.pk).status == "succeeded"


def test_ended_checks_are_purged_and_a_long_wait_is_a_health_failure(filer, year, pge, programs, settings):  # noqa: F811
    settings.QUEUED_CHECKS_BACKEND = "local"
    a, b = _queued(filer, pge, 2)
    QueuedCheck.objects.filter(pk=a.pk).update(created_at=timezone.now() - timedelta(hours=2))
    assert queued.stalled().status == "failing"
    queued.execute(a.pk)
    assert [k for k, _ in queued.expired(timezone.now() + timedelta(days=1))] == [f"queued_check:{a.pk}"]

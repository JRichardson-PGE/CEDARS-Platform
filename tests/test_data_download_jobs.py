import csv
import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from cedars.core import storage
from cedars.modules.data import jobs, records
from cedars.modules.data.models import DownloadJob
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications.models import Notification
from cedars.services.retention import service as retention
from cedars.services.retention.models import RetentionPolicy

from .test_data_records import rq, sce_filer, two_pas  # noqa: F401 - fixtures
from .test_snapshots import filer  # noqa: F401 - filer is a fixture

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("two_pas", "small_limit")]
QUERY = "dataset=claims&columns=none&col=CEInputID&col=TotalSystemBenefit"


@pytest.fixture
def small_limit(settings):
    settings.DATA_DOWNLOAD_MAX_ROWS = 3  # the 7 records of two_pas are "too many" to make in a request


@pytest.fixture
def person(make_user):
    return make_user("reader@example.org")


def body(response):
    return b"".join(response.streaming_content).decode()


def test_a_large_download_is_queued_made_and_announced(person, client, signed_in):
    assert client.get(f"/data/records/download.csv?{QUERY}").status_code == 413
    page = client.get(f"/data/records/?{QUERY}").content.decode()
    assert "made in the background" in page and "Sign in" in page and "Make this download" not in page

    signed_in(person)
    page = client.get(f"/data/records/?{QUERY}").content.decode()
    assert "Make this download" in page and "Your downloads" in page
    response = client.post("/data/downloads/request", {"query": QUERY, "format": "csv"})
    job = DownloadJob.objects.get()
    assert response.status_code == 302 and response["Location"] == f"/data/downloads/{job.pk}/"
    assert (job.status, job.records, job.fmt, job.dataset) == ("queued", 7, "csv", "claims") and "release=2" in job.query
    assert "Queued" in client.get(f"/data/downloads/{job.pk}/").content.decode()
    assert client.get(f"/data/downloads/{job.pk}/file").status_code == 404  # not made yet

    call_command("run_data_downloads")
    job.refresh_from_db()
    assert job.status == "done" and job.rows == 7 and job.size and len(job.sha256) == 64
    assert storage.store("results").exists(job.key) and job.key.startswith(f"downloads/{job.pk}/")
    n = Notification.objects.get(user=person)
    assert "7 claimed records is ready" in n.title and n.link == f"/data/downloads/{job.pk}/" and "kept for 7 days" in n.body
    assert {e.action for e in AuditEvent.objects.filter(object_id=str(job.pk), action__startswith="data_download")} == {
        "data_download.requested",
        "data_download.made",
    }

    text = body(client.get(f"/data/downloads/{job.pk}/file"))
    rows = list(csv.reader(line for line in io.StringIO(text) if not line.startswith("# ")))
    assert rows[0] == ["CEInputID", "TotalSystemBenefit"] and len(rows) == 8
    listing = client.get("/data/downloads/").content.decode()
    assert job.filename in listing and "Ready" in listing


def test_someone_elses_download_is_not_found(person, make_user, client, signed_in):
    job = jobs.request(person, records.page(rq(dataset="claims"), limit=0), "parquet")
    jobs.run_pending()
    job.refresh_from_db()
    signed_in(make_user("other@example.org"))
    assert client.get(f"/data/downloads/{job.pk}/").status_code == 404
    assert client.get(f"/data/downloads/{job.pk}/file").status_code == 404
    assert job.filename not in client.get("/data/downloads/").content.decode()


def test_signed_out_people_are_sent_to_sign_in(client):
    assert client.post("/data/downloads/request", {"query": QUERY}).status_code == 302
    assert "/accounts/login/" in client.get("/data/downloads/")["Location"]


def test_requests_are_refused_when_they_cannot_be_made(person, settings, client, signed_in, monkeypatch):
    page = records.page(rq(dataset="claims"), limit=0)
    with pytest.raises(jobs.JobRefused, match="at least one column"):
        jobs.request(person, records.page(rq(dataset="claims", columns="none"), limit=0), "csv")
    with pytest.raises(jobs.JobRefused, match="No records"):
        jobs.request(person, records.page(rq(dataset="claims", pa="NOPE"), limit=0), "csv")
    monkeypatch.setattr(records, "XLSX_MAX_ROWS", 5)
    with pytest.raises(jobs.JobRefused, match="Excel"):
        jobs.request(person, page, "xlsx")
    settings.DATA_DOWNLOAD_JOBS_PER_USER = 1
    jobs.request(person, page, "csv")
    with pytest.raises(jobs.JobRefused, match="waiting"):
        jobs.request(person, page, "csv")
    signed_in(person)
    response = client.post("/data/downloads/request", {"query": QUERY, "format": "csv"}, follow=True)
    assert "could not be queued" in response.content.decode()


def test_a_failure_is_kept_and_told(person, monkeypatch, client, signed_in):
    job = jobs.request(person, records.page(rq(dataset="claims"), limit=0), "csv")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(records, "write", broken)
    jobs.run_pending()
    job.refresh_from_db()
    assert job.status == "failed" and "disk full" in job.message
    assert "could not be made" in Notification.objects.get(user=person).title
    signed_in(person)
    assert "disk full" in client.get(f"/data/downloads/{job.pk}/").content.decode()


def test_inline_jobs_run_when_the_request_commits(person, settings, django_capture_on_commit_callbacks):
    settings.DATA_DOWNLOAD_INLINE_JOBS = True
    with django_capture_on_commit_callbacks(execute=True):
        job = jobs.request(person, records.page(rq(dataset="claims"), limit=0), "csv")
    job.refresh_from_db()
    assert job.status == "done"


def test_finished_downloads_expire_with_their_files(person):
    assert RetentionPolicy.objects.get(record_class="data_download").keep_days == 7
    job = jobs.request(person, records.page(rq(dataset="claims"), limit=0), "csv")
    queued = jobs.request(person, records.page(rq(dataset="claims", pa="SCE"), limit=0), "csv")
    jobs.run(jobs._claim())
    job.refresh_from_db()
    DownloadJob.objects.filter(pk=job.pk).update(finished_at=timezone.now() - timedelta(days=8))
    run = retention.purge()
    assert run.counts["data_download"]["deleted"] == 1
    assert not DownloadJob.objects.filter(pk=job.pk).exists() and not storage.store("results").exists(job.key)
    assert DownloadJob.objects.filter(pk=queued.pk).exists()


def test_queue_position(person, make_user):
    page = records.page(rq(dataset="claims"), limit=0)
    first = jobs.request(person, page, "csv")
    second = jobs.request(make_user("second@example.org"), page, "csv")
    assert jobs.queue_position(first) == 0 and jobs.queue_position(second) == 1

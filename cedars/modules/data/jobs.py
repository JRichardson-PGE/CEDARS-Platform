"""Background record downloads (BR-07.2): a download of more records than DATA_DOWNLOAD_MAX_ROWS is queued instead of made in the
request, written by `manage.py run_data_downloads` (several workers can share the queue: a job is claimed with SKIP LOCKED), kept in
the results storage area, and announced to the person through the notification service (on the page, and by email unless they turned
direct messages off). With settings.DATA_DOWNLOAD_INLINE_JOBS (development) a job runs once the request that queued it commits."""

import hashlib
import logging
import tempfile
from urllib.parse import parse_qs

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cedars.core import storage
from cedars.services import audit
from cedars.services.notifications import service as notifications

from . import records
from .models import DownloadJob

log = logging.getLogger(__name__)
AREA = "results"
ACTIVE = (DownloadJob.Status.QUEUED, DownloadJob.Status.RUNNING)


class JobRefused(ValueError):
    """A download that cannot be queued; the message says why and is safe to show."""


def request(user, page: records.Page, fmt: str) -> DownloadJob:
    """Queue a download of every record `page` matches, with its columns."""
    if fmt not in records.FORMATS:
        raise JobRefused(f"unknown format {fmt!r}")
    if not page.columns:
        raise JobRefused("Choose at least one column to download.")
    if not page.count:
        raise JobRefused("No records match.")
    if fmt == "xlsx" and page.count > records.XLSX_MAX_ROWS:
        raise JobRefused("These are more records than an Excel worksheet holds. Download them as CSV or Parquet.")
    if DownloadJob.objects.filter(user=user, status__in=ACTIVE).count() >= settings.DATA_DOWNLOAD_JOBS_PER_USER:
        raise JobRefused(
            f"You have {settings.DATA_DOWNLOAD_JOBS_PER_USER} downloads waiting to be made. Ask for another once one of them is ready."
        )
    q = page.result.query
    if page.result.release and not q.release:  # made from the release the person saw, even if a newer one appears meanwhile
        q = q.but(release=page.result.release.number)
    query = records.query_string(q, page.catalog, page.columns)
    with transaction.atomic():
        job = DownloadJob.objects.create(user=user, dataset=page.result.query.dataset, query=query, fmt=fmt, records=page.count)
        audit.record("data_download.requested", obj=job, after={"query": query, "format": fmt, "records": page.count}, actor=user)
        if settings.DATA_DOWNLOAD_INLINE_JOBS:
            transaction.on_commit(lambda: run_pending())
    return job


def _claim() -> DownloadJob | None:
    with transaction.atomic():
        job = (
            DownloadJob.objects.filter(status=DownloadJob.Status.QUEUED)
            .order_by("created_at", "pk")
            .select_for_update(skip_locked=True)
            .first()
        )
        if job is None:
            return None
        job.status, job.started_at = DownloadJob.Status.RUNNING, timezone.now()
        job.save(update_fields=["status", "started_at"])
        return job


def run(job: DownloadJob) -> DownloadJob:
    """Make one queued download: the records as they stand now (the current release, unless the query names one), written to a
    temporary file, then stored. A failure is kept on the job and told to the person."""
    try:
        page = records.page(records.from_params(parse_qs(job.query)), limit=0)
        name = records.filename(page, job.fmt)
        key = f"downloads/{job.pk}/{name}"
        with tempfile.NamedTemporaryFile(suffix=f".{job.fmt}") as tmp:
            rows = records.write(page, job.fmt, tmp)
            tmp.flush()
            tmp.seek(0)
            h = hashlib.sha256()
            while block := tmp.read(1 << 22):
                h.update(block)
            stored = storage.store(AREA).put_file(key, tmp.name)
        job.key, job.filename, job.size, job.sha256, job.rows = key, name, stored.size, h.hexdigest(), rows
        job.status, job.message = DownloadJob.Status.DONE, ""
    except Exception as e:  # noqa: BLE001 - a job's failure is recorded on the job and told to the person, never lost
        log.exception("data download %s failed", job.pk)
        job.status, job.message = DownloadJob.Status.FAILED, f"{type(e).__name__}: {e}"[:2000]
    job.finished_at = timezone.now()
    job.save()
    audit.record(
        "data_download.made" if job.status == DownloadJob.Status.DONE else "data_download.failed",
        obj=job,
        after={"rows": job.rows, "bytes": job.size, "sha256": job.sha256} if job.rows is not None else {},
        reason=job.message[:500],
        actor=None,
    )
    _tell(job)
    return job


def _tell(job: DownloadJob) -> None:
    label = records.DATASETS.get(job.dataset, job.dataset).lower()
    if job.status == DownloadJob.Status.DONE:
        title = f"Your download of {job.rows:,} {label} records is ready"
        body = f"{job.filename}, kept for {retention_days()} days." if retention_days() else job.filename
    else:
        title = f"Your download of {label} records could not be made"
        body = "Try again, or narrow the filters. The error is on the download's page."
    notifications.notify(job.user, title, body, f"/data/downloads/{job.pk}/", key=f"data_download:{job.pk}:{job.status}")


def run_pending(limit: int | None = None) -> int:
    """Make queued downloads until none is left (or `limit` have run). Returns how many ran."""
    n = 0
    while limit is None or n < limit:
        job = _claim()
        if job is None:
            break
        run(job)
        n += 1
    return n


def queue_position(job: DownloadJob) -> int:
    """How many downloads will be made before this one (0 = next, or being made)."""
    if job.status != DownloadJob.Status.QUEUED:
        return 0
    return DownloadJob.objects.filter(status__in=ACTIVE, created_at__lt=job.created_at).count()


def retention_days() -> int | None:
    from cedars.services.retention.models import RetentionPolicy

    policy = RetentionPolicy.objects.filter(record_class="data_download").first()
    return policy.keep_days if policy else None


def expired(cutoff):
    """Downloads finished before the cutoff, for the retention job (record class "data_download"): the file, then the record."""
    for job in DownloadJob.objects.filter(finished_at__lt=cutoff).iterator():

        def delete(job=job):
            if job.key:
                storage.store(AREA).delete_prefix(f"downloads/{job.pk}")
            job.delete()

        yield f"data_download:{job.pk}", delete

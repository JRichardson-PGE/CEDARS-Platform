"""The module's queued work, one queue per PA (plus "system" for the sync and library writes), as the Load Shape Tool keeps an
independent queue per PA. `manage.py run_impact_profile_jobs` runs them; a queue runs one job at a time, oldest first, and several
workers can share the table (a job is claimed with SKIP LOCKED). This stands in for the platform's shared dispatch until it exists.

With settings.IMPACT_PROFILES_INLINE_JOBS (development), a queued job runs at once in the request that queued it.
"""

import logging
import traceback

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from cedars.services import audit

from .models import LoadShapeUpload, MeasurePackage, ProcessingJob

log = logging.getLogger(__name__)
SYSTEM = "system"


def enqueue(kind: str, queue: str, *, actor=None, upload: LoadShapeUpload | None = None, params: dict | None = None) -> ProcessingJob:
    job = ProcessingJob.objects.create(kind=kind, queue=queue, created_by=actor, upload=upload, params=params or {})
    if settings.IMPACT_PROFILES_INLINE_JOBS:
        transaction.on_commit(lambda: run_pending(queue=queue))
    return job


def _claim(queue: str | None = None) -> ProcessingJob | None:
    with transaction.atomic():
        busy = ProcessingJob.objects.filter(queue=OuterRef("queue"), status=ProcessingJob.Status.RUNNING)
        qs = ProcessingJob.objects.filter(status=ProcessingJob.Status.QUEUED).exclude(Exists(busy))
        if queue is not None:
            qs = qs.filter(queue=queue)
        job = qs.order_by("created_at", "pk").select_for_update(skip_locked=True).first()
        if job is None:
            return None
        job.status, job.started_at = ProcessingJob.Status.RUNNING, timezone.now()
        job.save(update_fields=["status", "started_at"])
        return job


def run(job: ProcessingJob) -> ProcessingJob:
    from . import etrm, generation, library, loadshapes

    try:
        if job.kind == ProcessingJob.Kind.CHECK:
            upload = loadshapes.run_check(job.upload)
            job.message = f"{upload.get_status_display()}: {upload.accepted_count} of {upload.series_count} series kept."
        elif job.kind == ProcessingJob.Kind.GENERATE:
            package = MeasurePackage.objects.get(pk=job.params["package"])
            s = generation.generate(job.created_by, package, job.upload)
            job.message = (
                f"{s['created']} new profiles, {s['new_version']} new versions, {s['unchanged']} unchanged, {len(s['skipped'])} skipped."
            )
        elif job.kind == ProcessingJob.Kind.SYNC:
            r = etrm.sync(None, trigger=job.params.get("trigger", "automatic"))
            r.started_by = job.created_by
            r.save(update_fields=["started_by"])
            job.message = (
                f"{r.get_status_display()}: {r.imported} imported, {r.unchanged} unchanged, {r.excluded} excluded. {r.error}".strip()
            )
        elif job.kind == ProcessingJob.Kind.EXPORT:
            e = library.export(job.created_by)
            job.message = f"Export {e.number}: {e.profiles} profiles, {e.catalog_rows} catalog rows."
        job.status = ProcessingJob.Status.DONE
    except Exception as e:  # noqa: BLE001 - a job's failure is recorded on the job, never lost
        log.exception("impact profiles job %s failed", job.pk)
        job.status, job.message = ProcessingJob.Status.FAILED, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}"
        if job.upload_id:
            LoadShapeUpload.objects.filter(pk=job.upload_id).update(status=LoadShapeUpload.Status.FAILED)
        audit.record("impact_profiles_job.failed", obj=job, reason=str(e)[:500], actor=None)
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "message", "finished_at"])
    return job


def run_pending(limit: int | None = None, queue: str | None = None) -> int:
    """Run queued jobs until none can start (or `limit` have run). Returns how many ran."""
    n = 0
    while limit is None or n < limit:
        job = _claim(queue)
        if job is None:
            break
        run(job)
        n += 1
    return n


def queue_position(job: ProcessingJob) -> int:
    """How many jobs are ahead of this one in its queue (0 = next or running)."""
    return (
        ProcessingJob.objects.filter(queue=job.queue, status=ProcessingJob.Status.QUEUED, created_at__lt=job.created_at).count()
        + ProcessingJob.objects.filter(queue=job.queue, status=ProcessingJob.Status.RUNNING).exclude(pk=job.pk).count()
    )

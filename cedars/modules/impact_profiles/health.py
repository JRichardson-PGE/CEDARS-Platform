"""The Impact profiles module's health checks (cedars/core/health.py): jobs that wait or run too long, failed jobs, and the eTRM sync."""

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from cedars.core import health

from .models import EtrmSyncRun, ProcessingJob


def _minutes(name: str) -> int:
    return int(health.limit(name).total_seconds() // 60)


def _item(job: ProcessingJob, since, detail: str) -> health.Item:
    link = f"/impact-profiles/uploads/{job.upload_id}/" if job.upload_id else "/impact-profiles/etrm/" if job.kind == job.Kind.SYNC else ""
    return health.Item(f"profile-job:{job.pk}", f"{job.get_kind_display()} (queue {job.queue}, job {job.pk})", since, detail, link)


def stalled() -> health.Result:
    now = timezone.now()
    jobs = ProcessingJob.objects.filter(
        Q(status=ProcessingJob.Status.QUEUED, created_at__lt=now - health.limit("queued"))
        | Q(status=ProcessingJob.Status.RUNNING, started_at__lt=now - health.limit("job_running"))
    ).order_by("created_at")
    items = [
        _item(j, j.created_at, f"queued {health.ago(j.created_at)}, not started")
        if j.status == ProcessingJob.Status.QUEUED
        else _item(j, j.started_at, f"running {health.ago(j.started_at)}")
        for j in jobs
    ]
    result = health.Result(
        "impact_profiles.stalled", "Impact profiles", "Jobs waiting or running too long", items=items, link="/impact-profiles/uploads/"
    )
    if items:
        result.status = health.FAILING
        result.summary = (
            f"{len(items)} job{'s' if len(items) != 1 else ''} queued more than {_minutes('queued')} minutes or running more than "
            f"{_minutes('job_running')} minutes. Check that the Impact profiles worker (run_impact_profile_jobs --loop) is running."
        )
    else:
        result.summary = "No upload check, generation, sync or library write is waiting or running longer than expected."
    return result


def failed() -> health.Result:
    since = timezone.now() - health.limit("failures")
    jobs = ProcessingJob.objects.filter(status=ProcessingJob.Status.FAILED, finished_at__gte=since).order_by("-finished_at")
    items = [_item(j, j.finished_at, (j.message or "").splitlines()[0][:200] if j.message else "") for j in jobs[:50]]
    n = jobs.count()
    hours = _minutes("failures") // 60
    result = health.Result("impact_profiles.failed", "Impact profiles", "Jobs that failed", items=items, link="/impact-profiles/uploads/")
    result.status = health.ATTENTION if n else health.OK
    result.summary = (
        f"{n} job{'s' if n != 1 else ''} failed in the last {hours} hours." if n else f"No job failed in the last {hours} hours."
    )
    return result


def etrm_sync() -> health.Result:
    """The nightly eTRM sync (BR-11.3): failing when its last attempt failed or nothing has succeeded for HEALTH etrm minutes."""
    from . import etrm

    result = health.Result("impact_profiles.etrm", "Impact profiles", "eTRM sync", link="/impact-profiles/etrm/")
    last, latest = etrm.last_success(), EtrmSyncRun.objects.exclude(status=EtrmSyncRun.Status.RUNNING).first()
    if not (settings.ETRM_DATABASE_URL or settings.ETRM_FIXTURE) and last is None:
        result.status = health.ATTENTION
        result.summary = (
            "No eTRM source is set up (ETRM_DATABASE_URL), so nothing syncs; uploads are checked against the packages loaded by hand."
        )
        return result
    if latest is not None and latest.status == EtrmSyncRun.Status.FAILED:
        result.status = health.FAILING
        result.items = [
            health.Item(
                f"etrm-sync:{latest.pk}",
                f"Sync of {timezone.localtime(latest.started_at):%Y-%m-%d %H:%M}",
                latest.started_at,
                latest.error[:300],
                "/impact-profiles/etrm/",
            )
        ]
        result.summary = "The latest eTRM sync failed." + (
            f" The last success was {health.ago(last.finished_at)} ago." if last else " No sync has succeeded yet."
        )
        return result
    if last is None or last.finished_at < timezone.now() - health.limit("etrm"):
        result.status = health.FAILING
        result.summary = (
            f"No eTRM sync has succeeded for {health.ago(last.finished_at)}." if last else "No eTRM sync has succeeded yet."
        ) + " Check that the nightly sync is scheduled."
        return result
    result.summary = f"Last successful sync {health.ago(last.finished_at)} ago: {last.imported} imported, {last.excluded} excluded."
    return result


def register() -> None:
    health.register("impact_profiles.stalled", stalled)
    health.register("impact_profiles.failed", failed)
    health.register("impact_profiles.etrm", etrm_sync)

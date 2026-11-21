"""The Calculations module's health checks (cedars/core/health.py): runs that wait or run too long, and runs that failed."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from cedars.core import health

from . import service
from .models import Run


def _item(run: Run, since, detail: str) -> health.Item:
    return health.Item(
        f"run:{run.pk}",
        f"{run.name} ({run.organisation.code if run.organisation else 'own run'})",
        since,
        detail,
        f"/calculations/runs/{run.pk}/",
    )


def stalled() -> health.Result:
    now = timezone.now()
    queued_before, running_before = now - health.limit("queued"), now - health.limit("run_running")
    runs = (
        Run.objects.filter(
            Q(status=Run.Status.QUEUED, created_at__lt=queued_before) | Q(status=Run.Status.RUNNING, started_at__lt=running_before)
        )
        .select_related("organisation")
        .order_by("created_at")
    )
    items = []
    for r in runs:
        if r.status == Run.Status.QUEUED:
            items.append(_item(r, r.created_at, f"queued {health.ago(r.created_at)} on the {r.lane} queue ({r.backend}), not started"))
        else:
            items.append(
                _item(
                    r,
                    r.started_at,
                    f"running {health.ago(r.started_at)} ({r.backend}{', ' + r.backend_job_id if r.backend_job_id else ''})",
                )
            )
    result = health.Result("calculations.stalled", "Calculations", "Runs waiting or running too long", link="/calculations/runs/")
    if items:
        result.status = health.FAILING
        result.summary = (
            f"{len(items)} run{'s' if len(items) != 1 else ''} queued more than {settings_minutes('queued')} minutes or running more than "
            f"{settings_minutes('run_running')} minutes. Check that the workers and the calculations sync are running."
        )
    else:
        result.summary = "No run is waiting or running longer than expected."
    result.items = items
    return result


def failed() -> health.Result:
    since = timezone.now() - health.limit("failures")
    runs = Run.objects.filter(status=Run.Status.FAILED, finished_at__gte=since).select_related("organisation").order_by("-finished_at")
    items = [_item(r, r.finished_at, service.plain_error(r.error) or "no error recorded") for r in runs[:50]]
    n = runs.count()
    hours = settings_minutes("failures") // 60
    result = health.Result("calculations.failed", "Calculations", "Runs that failed", items=items, link="/calculations/runs/")
    if n:
        result.status = health.ATTENTION
        result.summary = f"{n} run{'s' if n != 1 else ''} failed in the last {hours} hours (refused inputs are not counted)."
    else:
        result.summary = f"No run failed in the last {hours} hours."
    return result


def workers() -> health.Result:
    """Local backend: the default release and every release runs are queued on each need a worker running it that has polled lately
    (otherwise those runs wait for good). On AWS Batch a worker of the release's image starts per run, so nothing is checked."""
    from . import dispatch, service

    result = health.Result("calculations.workers", "Calculations", "Workers for each engine release", link="/calculations/engines/")
    if dispatch.default_backend_name() != "local":
        result.summary = "Runs go to AWS Batch, which starts a worker of the release's image for each run."
        return result
    minutes = settings_minutes("worker_seen")
    live = list(dispatch.live_workers())
    needed: dict[str, str] = {}
    default = service.default_release()
    if default is not None:
        needed[default.release] = "the default for new runs"
    queued = Run.objects.filter(status=Run.Status.QUEUED, backend="local").values("engine_release").annotate(n=Count("id"))
    for row in queued:
        needed[row["engine_release"]] = f"{row['n']} run{'s' if row['n'] != 1 else ''} queued"
    items = [
        health.Item(f"release:{rel}", f"ReCET {rel}", None, f"{why}; no worker running it has polled in the last {minutes} minutes")
        for rel, why in sorted(needed.items())
        if not any(w.release in (rel, "") for w in live)
    ]
    result.items = items
    if items:
        result.status = health.FAILING
        result.summary = (
            f"{len(items)} release{'s' if len(items) != 1 else ''} with no worker polling in the last {minutes} minutes. Runs on "
            "them wait until a worker of that release starts (manage.py calculations_worker)."
        )
    elif not live:
        result.status = health.ATTENTION
        result.summary = f"No worker has polled the queue in the last {minutes} minutes, and nothing is queued."
    else:
        releases = sorted({w.release or "unknown release" for w in live})
        result.summary = (
            f"{len(live)} worker{'s' if len(live) != 1 else ''} polled in the last {minutes} minutes: ReCET {', '.join(releases)}."
        )
    return result


def fast_lane() -> health.Result:
    """With the fast lane on: a fast worker of the default release is ready (otherwise small checks and runs wait for AWS Batch to
    start a worker, a minute or two, which is slower but not broken)."""
    from . import dispatch

    result = health.Result("calculations.fast_lane", "Calculations", "Fast lane for small checks and runs", link="/calculations/engines/")
    if not dispatch.fast_lane_on():
        result.summary = "The fast lane is off here: every check job and run goes to the platform's backend."
        return result
    ready = list(dispatch.fast_workers())
    default = service.default_release()
    want = default.release if default is not None else ""
    if not ready:
        result.status = health.ATTENTION
        result.summary = (
            "No fast worker is ready, so small checks and runs go to AWS Batch and wait a minute or two for it to start a worker. "
            "Check the calculations-sync service's fast-lane container and its log (it warms up on each ACC of the reference folder)."
        )
    elif want and not any(w.release == want for w in ready):
        result.status = health.ATTENTION
        result.summary = (
            f"The fast worker runs ReCET {', '.join(sorted({w.release for w in ready}))}, not the default release {want}: runs on "
            f"{want} go to AWS Batch. Deploy the platform image of that release."
        )
    else:
        result.summary = (
            f"{len(ready)} fast worker{'s' if len(ready) != 1 else ''} ready (ReCET {', '.join(sorted({w.release for w in ready}))}): "
            f"check jobs and runs of up to {dispatch.fast_records():,} records start within seconds."
        )
    return result


def reference_folder_health() -> health.Result:
    """Where this deployment keeps the workers' shared reference folder: the last sync found every file a new run would be pinned to,
    and it ran lately."""
    from datetime import datetime

    from . import reference_folder

    result = health.Result("calculations.reference_folder", "Calculations", "Workers' reference folder", link="/reference-data/packs/")
    found = reference_folder.state()
    if found is None:
        result.summary = "This deployment keeps no shared reference folder (each worker has its own)."
        return result
    checked = datetime.fromisoformat(found["checked"])
    every = int(getattr(settings, "CALCULATIONS_REFERENCE_SYNC_SECONDS", 300))
    missing, failed = found.get("behind") or [], found.get("failed") or {}
    result.items = [
        health.Item(f"reference:{rel}", rel, checked, failed.get(rel) or "not in the folder yet, or another version")
        for rel in missing[:50]
    ]
    if missing:
        result.status = health.ATTENTION
        result.summary = (
            f"{len(missing)} file{'s' if len(missing) != 1 else ''} of the data packs in force {'are' if len(missing) != 1 else 'is'} "
            "not in the workers' reference folder, so runs pinned to them are refused. The calculations sync copies them within "
            "minutes; if this stays, check its log."
        )
    elif timezone.now() - checked > timedelta(seconds=every * 3 + 3600):
        result.status = health.ATTENTION
        result.summary = (
            f"The workers' reference folder was last checked {health.ago(checked)} ago: check that the calculations sync is running."
        )
    else:
        result.summary = f"The workers' reference folder held every file of the data packs in force when checked {health.ago(checked)} ago."
    return result


def settings_minutes(name: str) -> int:
    return int(health.limit(name).total_seconds() // 60)


def register() -> None:
    health.register("calculations.stalled", stalled)
    health.register("calculations.failed", failed)
    health.register("calculations.workers", workers)
    health.register("calculations.fast_lane", fast_lane)
    health.register("calculations.reference_folder", reference_folder_health)

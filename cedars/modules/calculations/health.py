"""The Calculations module's health checks (cedars/core/health.py): runs that wait or run too long, and runs that failed."""

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
    result = health.Result("calculations.stalled", "Calculations", "Runs waiting or running too long", link="/calculations/")
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
    result = health.Result("calculations.failed", "Calculations", "Runs that failed", items=items, link="/calculations/")
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


def settings_minutes(name: str) -> int:
    return int(health.limit(name).total_seconds() // 60)


def register() -> None:
    health.register("calculations.stalled", stalled)
    health.register("calculations.failed", failed)
    health.register("calculations.workers", workers)

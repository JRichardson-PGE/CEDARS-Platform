"""Queued checks: a filing upload, or a Check again, too large to check while the page waits. A check reads the whole upload, compiles it
over the year to date and writes every record, which for a large filing takes minutes and gigabytes: far past what a web request may
take. So an upload that reads more records than the queued_check_records limit, or unpacks to more than queued_check_mb (both on the
Limits page, changed on Change limits), is kept in the uploads area and recorded as a QueuedCheck, and the filing's page shows its place
in the queue. A worker then runs the module's own check on it (the function the module registered for its kind with
`register_queued_check`), as the person who asked and exactly as the request would have, and the page shows what it found. A smaller
upload is checked in the request as before. Everything after the module's check is unchanged: its rules still go to the engine
release's worker as check jobs, and the version is finished when they end.

Where queued checks run (QUEUED_CHECKS_BACKEND):

local   `manage.py run_queued_checks --loop` (a single server, the sandbox): takes the oldest queued check while fewer than the
        queued_checks_at_once limit are running (across every worker), and runs each in a process of its own
        (`run_queued_checks --check <id>`), so a large check's memory is given back when it ends and a check that runs out of memory
        fails alone. A check left running by a worker that stopped (a deploy) is put back in the queue once, then failed.
batch   AWS Batch: each check is a job on QUEUED_CHECKS_BATCH_QUEUE with QUEUED_CHECKS_BATCH_JOB_DEFINITION (this image, running
        `run_queued_checks --check <id>` with the web tasks' access), submitted while fewer than queued_checks_at_once are submitted
        or running; `calculations_sync` submits the next ones and fails a check whose job Batch ended without it finishing.
inline  Run once the request that queued it commits (development and tests)."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from cedars.core import storage
from cedars.core import uploads as core_uploads
from cedars.services import audit
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.context import acting_as

from .models import QueuedCheck

log = logging.getLogger(__name__)

AREA = "uploads"
PREFIX = "queued-checks/"
LOCK = 7_211_031  # the advisory lock that keeps queued_checks_at_once true when several workers claim at once
HEARTBEAT = "queued_checks.worker."
HEARTBEAT_SECONDS = 120  # a local worker not seen for this long has stopped; its running check goes back in the queue
Status = QueuedCheck.Status


class Refused(Exception):
    """The check refused the upload, in words for the person who sent it."""


@dataclass(frozen=True)
class Kind:
    """What a module does with a queued check of one kind: `run(check, files)` checks it as the request would have and returns what
    the filing's page says ({level: info|warning|success, text, version}); an exception of a type in `errors` (or AccessDenied, or
    Refused) is the module refusing the upload, said to the person as it is."""

    run: Callable[[QueuedCheck, list[tuple[str, bytes]]], dict]
    label: str
    errors: tuple[type[BaseException], ...] = ()


_kinds: dict[str, Kind] = {}


def register_queued_check(kind: str, run, *, label: str, errors: tuple = ()) -> None:
    """Called from a filing module's AppConfig.ready: the module's check for queued checks of `kind` ("claims.upload" ...)."""
    _kinds[kind] = Kind(run, label, tuple(errors))


def backend() -> str:
    return getattr(settings, "QUEUED_CHECKS_BACKEND", "local")


def _limit(key: str) -> int:
    from cedars.services.releases import limits

    return int(limits.value(key))


def records_limit() -> int:
    return _limit("queued_check_records")


def bytes_limit() -> int:
    return _limit("queued_check_mb") * 1024 * 1024


def at_once() -> int:
    return max(_limit("queued_checks_at_once"), 1)


# --- queuing


def pending(deadline, organisation) -> QueuedCheck | None:
    """The filing's queued check still to run or running, if any."""
    if deadline is None or organisation is None:
        return None
    return (
        QueuedCheck.objects.filter(deadline=deadline, organisation=organisation, status__in=[Status.QUEUED, Status.RUNNING])
        .order_by("created_at")
        .first()
    )


def require_none_pending(deadline, organisation) -> None:
    """Refuse an upload or a check of a filing while an upload of it waits or is being checked: it would be replaced underneath."""
    q = pending(deadline, organisation)
    if q is not None:
        state = "is being checked" if q.status == Status.RUNNING else "is waiting in the queue to be checked"
        raise AccessDenied(f"An upload of this filing {state}. Wait for it to finish, then upload again or check again.")


def is_large(records: int, nbytes: int) -> bool:
    return records > records_limit() or nbytes > bytes_limit()


def queue_if_large(
    actor,
    kind: str,
    *,
    deadline=None,
    organisation=None,
    files: list[tuple[str, bytes]] = (),
    version=None,
    extra_records: int = 0,
    assurance: str = "",
    params: dict | None = None,
    precheck: Callable[[], None] | None = None,
) -> QueuedCheck | None:
    """Queue the check when it is too large to run while the person waits (the upload's records plus `extra_records`, the records it is
    compiled over, or what its files unpack to); None when the caller checks it at once. Refused while another of the filing's
    uploads waits or runs. `precheck` raises what the module would refuse at once (who may file, the order of confirmations), so a
    file that would be refused is not queued first."""
    require_none_pending(deadline, organisation)
    size = core_uploads.measure(list(files), enough=records_limit())
    if not is_large(size.records + extra_records, size.bytes):
        return None
    if precheck is not None:
        precheck()
    return enqueue(
        actor,
        kind,
        deadline=deadline,
        organisation=organisation,
        files=files,
        version=version,
        records=size.records + extra_records,
        nbytes=size.bytes,
        assurance=assurance,
        params=params,
    )


def enqueue(
    actor,
    kind: str,
    *,
    deadline=None,
    organisation=None,
    files=(),
    version=None,
    records: int = 0,
    nbytes: int = 0,
    assurance: str = "",
    params: dict | None = None,
) -> QueuedCheck:
    """Keep the files and record the queued check; it is handed to the backend once the caller's transaction commits."""
    if kind not in _kinds:
        raise ValueError(f"no module checks queued checks of kind {kind!r}")
    qid = uuid.uuid4()
    store = storage.store(AREA)
    kept = []
    for i, (name, content) in enumerate(files):
        key = f"{PREFIX}{qid}/{i:02d}-{Path(name).name or 'upload'}"
        store.put_bytes(key, content)
        kept.append({"name": name, "key": key, "bytes": len(content)})
    q = QueuedCheck.objects.create(
        id=qid,
        kind=kind,
        deadline=deadline,
        version=version,
        organisation=organisation,
        created_by=actor,
        assurance=assurance or "",
        files=kept,
        params=params or {},
        records=records,
        bytes=nbytes,
        backend=backend(),
    )
    audit.record("queued_check.queued", obj=q, organisation=organisation, after={"kind": kind, "records": records, "bytes": nbytes})
    transaction.on_commit(dispatch)
    return q


def dispatch() -> None:
    """Hand waiting checks to the backend: run them here (inline), submit them to AWS Batch (batch); a local worker takes them itself."""
    if backend() == "inline":
        while (q := claim_next("inline")) is not None:
            execute(q.pk, "inline")
    elif backend() == "batch":
        submit_waiting()


# --- where a check stands, for the filing's page


def position(q: QueuedCheck) -> int:
    """How many queued checks are ahead of it (0 when it is next or running)."""
    if q.status != Status.QUEUED:
        return 0
    return QueuedCheck.objects.filter(status=Status.QUEUED, created_at__lt=q.created_at).count()


def _size_words(q: QueuedCheck) -> str:
    return f"{q.records:,} records" if q.records else f"{q.bytes / 1024 / 1024:,.0f} MB"


def state(q: QueuedCheck) -> dict:
    """{ended, text, status}: the check in words, for the filing's page and its status JSON."""
    label = _kinds[q.kind].label if q.kind in _kinds else "upload"
    if q.status == Status.QUEUED:
        ahead = position(q)
        where = "it is next" if ahead == 0 else f"{ahead:,} {'check is' if ahead == 1 else 'checks are'} ahead of it"
        text = f"The {label} ({_size_words(q)}) is waiting in the queue to be checked: {where}."
        if backend() == "local" and not workers_running():
            text += " No worker is taking queued checks just now; it starts when one does."
        return {"ended": False, "status": q.status, "text": text}
    if q.status == Status.RUNNING:
        since = timezone.localtime(q.started_at).strftime("%H:%M") if q.started_at else ""
        return {
            "ended": False,
            "status": q.status,
            "text": f"The {label} ({_size_words(q)}) is being checked" + (f", since {since}." if since else "."),
        }
    if q.status == Status.SUCCEEDED:
        return {"ended": True, "status": q.status, "text": (q.result or {}).get("text") or f"The {label} was checked."}
    return {"ended": True, "status": q.status, "text": f"The {label} was not checked: {q.error or 'the check failed.'}"}


def for_page(user, deadline, organisation) -> dict | None:
    """What a filing's page shows of its queued checks: the one waiting or running ({check, state, position}), else the last one the
    person who asked has not seen since it ended (marked seen now: it is shown once)."""
    if deadline is None or organisation is None:
        return None
    q = pending(deadline, organisation)
    if q is None and getattr(user, "is_authenticated", False):
        q = (
            QueuedCheck.objects.filter(
                deadline=deadline, organisation=organisation, created_by=user, status__in=QueuedCheck.ENDED, seen_at__isnull=True
            )
            .order_by("-finished_at")
            .first()
        )
        if q is not None:
            QueuedCheck.objects.filter(deadline=deadline, organisation=organisation, created_by=user, seen_at__isnull=True).exclude(
                status__in=[Status.QUEUED, Status.RUNNING]
            ).update(seen_at=timezone.now())
    if q is None:
        return None
    s = state(q)
    level = (q.result or {}).get("level") or ("error" if q.status == Status.FAILED else "info")
    return {"check": q, "state": s, "level": level if s["ended"] else "info"}


def queued_words(q: QueuedCheck) -> str:
    """What the page says just after an upload was queued."""
    return (
        f"The file is large ({_size_words(q)}), so it is checked by a worker instead of while you wait. This page shows its place in "
        "the queue and updates itself when the check finishes; you can leave it and come back."
    )


# --- the worker's side


def claim_next(worker: str) -> QueuedCheck | None:
    """Take the oldest queued check while fewer than queued_checks_at_once are running (an advisory lock keeps the count true across
    workers); None when none waits or enough run already."""
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK])
        if QueuedCheck.objects.filter(status=Status.RUNNING).count() >= at_once():
            return None
        q = (
            QueuedCheck.objects.select_for_update(skip_locked=True)
            .filter(status=Status.QUEUED, backend_job_id="")
            .order_by("created_at")
            .first()
        )
        if q is None:
            return None
        q.status, q.worker, q.started_at = Status.RUNNING, worker[:200], timezone.now()
        q.save(update_fields=["status", "worker", "started_at"])
    return q


def _files(q: QueuedCheck) -> list[tuple[str, bytes]]:
    store = storage.store(AREA)
    return [(f["name"], store.get_bytes(f["key"])) for f in q.files or []]


def execute(pk, worker: str = "") -> QueuedCheck:
    """Run one queued check to its end (in a worker's child process, a Batch job, or inline): the module's check as the person who
    asked, then its outcome recorded and the kept files deleted."""
    q = QueuedCheck.objects.select_related("created_by", "organisation").get(pk=pk)
    if q.ended:
        return q
    if q.status == Status.QUEUED:  # a Batch job starting
        QueuedCheck.objects.filter(pk=q.pk, status=Status.QUEUED).update(
            status=Status.RUNNING, worker=(worker or socket.gethostname())[:200], started_at=timezone.now()
        )
        q.refresh_from_db()
    kind = _kinds.get(q.kind)
    if kind is None:
        return finish(q, Status.FAILED, error=f"Nothing here checks queued checks of kind {q.kind}.")
    try:
        files = _files(q)
        with acting_as(q.created_by, q.organisation, q.assurance):
            result = kind.run(q, files)
    except (Refused, AccessDenied, *kind.errors) as e:
        return finish(q, Status.FAILED, error=str(e))
    except Exception as e:  # noqa: BLE001 - a crash is recorded on the check, never lost; the person is told to try again
        log.exception("queued check %s", q.pk)
        return finish(q, Status.FAILED, error=f"the check could not be finished ({type(e).__name__}: {e}). Upload the file again.")
    return finish(q, Status.SUCCEEDED, result=result)


def finish(q: QueuedCheck, status: str, *, result: dict | None = None, error: str = "") -> QueuedCheck:
    result = dict(result or {})
    if "version" in result and result["version"] is not None and not isinstance(result["version"], int):
        result["version"] = getattr(result["version"], "pk", result["version"])
    updated = (
        QueuedCheck.objects.filter(pk=q.pk)
        .exclude(status__in=QueuedCheck.ENDED)
        .update(status=status, result=result, error=error[:4000], finished_at=timezone.now())
    )
    q.refresh_from_db()
    if updated:
        try:
            storage.store(AREA).delete_prefix(f"{PREFIX}{q.pk}/")
        except storage.StorageError:  # pragma: no cover - the retention job's purge takes what is left
            log.warning("queued check %s: could not delete its files", q.pk)
        action = "queued_check.finished" if status == Status.SUCCEEDED else "queued_check.failed"
        audit.record(action, obj=q, organisation=q.organisation, actor=q.created_by, after={"error": error[:500]} if error else None)
        if backend() == "batch":  # the next waiting check takes its place (a local worker claims it itself)
            transaction.on_commit(submit_waiting)
    return q


def _alive(name: str) -> bool:
    seen = cache.get(HEARTBEAT + name)
    return seen is not None and (timezone.now() - seen).total_seconds() < HEARTBEAT_SECONDS


def heartbeat(name: str) -> None:
    cache.set(HEARTBEAT + name, timezone.now(), HEARTBEAT_SECONDS * 5)
    cache.set(HEARTBEAT + "any", timezone.now(), HEARTBEAT_SECONDS * 5)


def workers_running() -> bool:
    """Whether a local worker has polled in the last two minutes."""
    return _alive("any")


def recover(now=None) -> int:
    """Checks left running: by a local worker that has stopped (back in the queue once, then failed), or for longer than
    QUEUED_CHECKS_MAX_HOURS anywhere (failed). Returns how many were changed."""
    now = now or timezone.now()
    changed = 0
    too_long = now - timedelta(hours=settings.QUEUED_CHECKS_MAX_HOURS)
    for q in QueuedCheck.objects.filter(status=Status.RUNNING):
        if q.started_at and q.started_at < too_long:
            finish(
                q,
                Status.FAILED,
                error=f"it ran for more than {settings.QUEUED_CHECKS_MAX_HOURS} hours and was stopped. Upload the file again.",
            )
            changed += 1
        elif q.backend == "local" and q.worker and not _alive(q.worker):
            if (q.params or {}).get("requeued"):
                finish(q, Status.FAILED, error="the worker checking it stopped twice. Upload the file again.")
            else:
                QueuedCheck.objects.filter(pk=q.pk, status=Status.RUNNING).update(
                    status=Status.QUEUED, worker="", started_at=None, params={**(q.params or {}), "requeued": True}
                )
            changed += 1
    return changed


def _child(q: QueuedCheck) -> subprocess.Popen:
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": os.environ.get("DJANGO_SETTINGS_MODULE") or settings.SETTINGS_MODULE}
    manage = Path(settings.BASE_DIR) / "manage.py"
    return subprocess.Popen([sys.executable, str(manage), "run_queued_checks", "--check", str(q.pk)], env=env)  # noqa: S603 - our own


def _after_child(pk, code: int) -> None:
    q = QueuedCheck.objects.filter(pk=pk).first()
    if q is None or q.ended:
        return
    why = "it ran out of memory" if code in (-9, 137) else f"the process checking it ended with exit code {code}"
    finish(q, Status.FAILED, error=f"{why}. Upload the file again; if it fails again, ask the CPUC's support.")


def work(*, once: bool = False, idle: float = 2.0) -> int:
    """The local worker: keep up to queued_checks_at_once checks running, each in a process of its own (`--check`), oldest first.
    Returns how many it ran (once=True: stops when none waits and its checks have ended)."""
    name = f"{socket.gethostname()}:{os.getpid()}"
    log.info("queued checks worker %s", name)
    children: dict = {}
    done = 0
    while True:
        close_old_connections()
        heartbeat(name)
        for pk, proc in list(children.items()):
            code = proc.poll()
            if code is not None:
                del children[pk]
                _after_child(pk, code)
                done += 1
        try:
            recover()
            while len(children) < at_once() and (q := claim_next(name)) is not None:
                log.info("checking %s (%s, %s records)", q.pk, q.kind, q.records)
                children[q.pk] = _child(q)
        except Exception:  # noqa: BLE001 - a lost connection must not end the worker
            log.exception("queued checks worker: a pass failed")
        if once and not children:
            return done
        time.sleep(idle)


# --- AWS Batch


def _batch_client():
    import boto3

    return boto3.client("batch")


def submit_waiting(client=None) -> int:
    """Submit queued checks to AWS Batch while fewer than queued_checks_at_once are submitted or running; returns how many."""
    n = 0
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK])
        active = (
            QueuedCheck.objects.filter(status=Status.RUNNING).count()
            + QueuedCheck.objects.filter(status=Status.QUEUED).exclude(backend_job_id="").count()
        )
        waiting = list(
            QueuedCheck.objects.filter(status=Status.QUEUED, backend_job_id="").order_by("created_at")[: max(at_once() - active, 0)]
        )
        if not waiting:
            return 0
        if not (settings.QUEUED_CHECKS_BATCH_QUEUE and settings.QUEUED_CHECKS_BATCH_JOB_DEFINITION):
            for q in waiting:
                finish(q, Status.FAILED, error="no AWS Batch queue is set up for queued checks (QUEUED_CHECKS_BATCH_QUEUE).")
            return 0
        client = client or _batch_client()
        for q in waiting:
            response = client.submit_job(
                jobName=f"queued-check-{q.pk}"[:128],
                jobQueue=settings.QUEUED_CHECKS_BATCH_QUEUE,
                jobDefinition=settings.QUEUED_CHECKS_BATCH_JOB_DEFINITION,
                parameters={"check_id": str(q.pk)},
                tags={"platform_queued_check_id": str(q.pk)},
                propagateTags=True,
            )
            QueuedCheck.objects.filter(pk=q.pk).update(backend="batch", backend_job_id=response["jobId"])
            n += 1
    return n


def sync_batch(client=None) -> int:
    """Fail queued checks whose Batch job ended without the check finishing (out of memory, a stopped job), then submit the next ones.
    Returns how many were failed."""
    found = list(QueuedCheck.objects.filter(backend="batch", status__in=[Status.QUEUED, Status.RUNNING]).exclude(backend_job_id=""))
    failed = 0
    if found:
        client = client or _batch_client()
        described = {}
        for i in range(0, len(found), 100):
            for job in client.describe_jobs(jobs=[q.backend_job_id for q in found[i : i + 100]])["jobs"]:
                described[job["jobId"]] = job
        for q in found:
            job = described.get(q.backend_job_id)
            if job is None or job.get("status") not in ("SUCCEEDED", "FAILED"):
                continue
            q.refresh_from_db()
            if q.ended:
                continue
            reason = (job.get("statusReason") or "") + " " + ((job.get("container") or {}).get("reason") or "")
            why = "it ran out of memory" if "OutOfMemory" in reason or "memory" in reason.lower() else "its job ended without finishing"
            finish(q, Status.FAILED, error=f"{why} ({reason.strip() or job.get('status')}). Upload the file again.")
            failed += 1
    recover()
    submit_waiting(client)
    return failed


# --- retention and health


def expired(cutoff):
    """Queued checks that ended before the cutoff (record class "queued_check"), with any files a failed delete left."""
    for q in QueuedCheck.objects.filter(status__in=QueuedCheck.ENDED, finished_at__lt=cutoff).iterator():

        def delete(q=q):
            storage.store(AREA).delete_prefix(f"{PREFIX}{q.pk}/")
            QueuedCheck.objects.filter(pk=q.pk).delete()

        yield f"queued_check:{q.pk}", delete


def stalled():
    """Health: queued checks waiting longer than HEALTH queued_check minutes (no worker takes them?), or running longer than
    QUEUED_CHECKS_MAX_HOURS."""
    from cedars.core import health

    result = health.Result("submissions.queued_checks", "Filings", "Queued checks waiting or running too long")
    wait = timezone.now() - health.limit("queued_check")
    waiting = list(QueuedCheck.objects.filter(status=Status.QUEUED, created_at__lt=wait).order_by("created_at")[:20])
    result.items = [
        health.Item(f"queued-check:{q.pk}", f"{q.kind}, {_size_words(q)}", q.created_at, f"waiting {health.ago(q.created_at)}")
        for q in waiting
    ]
    if result.items:
        result.status = health.FAILING
        result.summary = (
            f"{len(result.items)} queued check(s) have waited more than {settings.HEALTH['queued_check']} minutes: is a worker running?"
        )
        if backend() == "local" and not workers_running():
            result.summary += " No local worker (run_queued_checks --loop) has polled in the last two minutes."
    else:
        result.summary = "No queued check has waited too long."
    return result

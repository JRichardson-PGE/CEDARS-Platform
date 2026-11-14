"""Where runs are priced: one interface, two backends (CALCULATIONS_BACKEND).

local  A worker process on this machine (`manage.py calculations_worker`; development, the sandbox, a single server). It takes queued
       runs from PostgreSQL (SELECT ... FOR UPDATE SKIP LOCKED, so several workers never take the same run), runs the configured
       ReCET worker command on the run's job spec, copies the worker's progress into the run record while it runs, and records the
       receipt when it ends. Stopping a run ends the worker process.
batch  AWS Batch (production): submitting a run submits a Batch job, on the small-runs queue for jobs of up to
       CALCULATIONS_SMALL_RUN_RECORDS measures and the large-runs queue otherwise, running the release's job definition with
       `recet worker run <spec URI>`. `manage.py calculations_sync` (scheduled every minute) copies each job's progress.json and
       receipt.json from S3 into its run record, and records jobs Batch ended without a receipt as failed. Stopping a run terminates
       the job.

Either way the worker reads its inputs from and writes its outputs to object storage (cedars.core.storage), and the run record is
changed only through service.py.

Check jobs (cedars.services.rules.jobs: a filing's rules run by the worker of the release its specification version names) go the same
two ways: the local worker takes queued check jobs of its release before runs (`recet worker check <spec URI>`; they are short), and on
AWS Batch each is a job on the small-runs queue with the release's job definition, its command overridden to `recet worker check`,
whose receipt `calculations_sync` reads. The rules service's job records change only through its own functions."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from cedars.core import health, storage
from cedars.services.rules import jobs as checks
from cedars.services.rules.models import CheckJob

from . import service
from .models import EngineRelease, Run, Worker

log = logging.getLogger(__name__)


def default_backend_name() -> str:
    return getattr(settings, "CALCULATIONS_BACKEND", "local")


def backend(name: str | None = None):
    name = name or default_backend_name()
    if name == "batch":
        return BatchBackend()
    if name == "local":
        return LocalBackend()
    raise ValueError(f"unknown calculations backend {name!r}")


def read_json(key: str) -> dict | None:
    try:
        return json.loads(storage.store("results").get_bytes(key))
    except (storage.StorageError, ValueError):
        return None


def sync_from_storage(run: Run) -> dict | None:
    """Copy the worker's progress.json into the run; return its receipt.json when the worker has written one."""
    out = run.storage_prefix + "out/"
    progress = read_json(out + "progress.json")
    if progress:
        service.apply_progress(run, progress)
    return read_json(out + "receipt.json")


# --- local


class LocalBackend:
    name = "local"

    def submit(self, run: Run) -> None:
        """Nothing to do: a worker process takes queued runs from the database."""

    def cancel(self, run: Run) -> None:
        """Nothing to do: the worker running it sees cancel_requested_at and ends the process."""


def command() -> list[str]:
    cmd = getattr(settings, "CALCULATIONS_WORKER_COMMAND", "recet worker")
    return shlex.split(cmd) if isinstance(cmd, str) else list(cmd)


def version_command() -> list[str]:
    """The command that prints the worker's release. `recet --version` prints "recet X.Y.Z", but `recet worker --version` is refused
    (the worker subcommand takes no --version, UAT 2 U01), so a command ending in the worker subcommand asks the program itself."""
    cmd = command()
    if len(cmd) > 1 and cmd[-1] == "worker":
        cmd = cmd[:-1]
    return [*cmd, "--version"]


def worker_release() -> str:
    """The ReCET release the worker command runs; "" when it cannot say."""
    try:
        out = subprocess.run(version_command(), capture_output=True, text=True, timeout=120, check=False)  # noqa: S603 - our own setting
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\b(\d+\.\d+\.\d+[\w.+-]*)", (out.stdout or "") + (out.stderr or ""))
    return m.group(1) if m else ""


def heartbeat(name: str, release: str, lanes=(Run.Lane.SMALL, Run.Lane.LARGE), priced: int = 0) -> Worker:
    """Record that a worker is polling: its release and queues, now. Workers not seen for a week are forgotten."""
    now = timezone.now()
    w, _ = Worker.objects.update_or_create(name=name, defaults={"release": release, "lanes": list(lanes), "last_seen_at": now})
    if priced:
        Worker.objects.filter(pk=w.pk).update(runs_priced=priced)
    Worker.objects.filter(last_seen_at__lt=now - timedelta(days=7)).delete()
    return w


def live_workers():
    """The workers that polled within HEALTH worker_seen minutes."""
    return Worker.objects.filter(last_seen_at__gte=timezone.now() - health.limit("worker_seen"))


def serves(release: str) -> bool:
    """Whether a run on the release is priced now: on AWS Batch always (a worker of the release's image starts per run); locally, when
    a live worker runs that release (or one that could not say which)."""
    if default_backend_name() != "local":
        return True
    return live_workers().filter(Q(release=release) | Q(release="")).exists()


def unserved(releases) -> list[EngineRelease]:
    """The releases among `releases` no live worker runs (local backend; empty on Batch)."""
    return [r for r in releases if not serves(r.release)]


def claim_next(worker: str, lanes=(Run.Lane.SMALL, Run.Lane.LARGE), release: str = "") -> Run | None:
    """Take the next queued local run, small runs first, oldest first; a worker that knows its release takes only that release's runs
    (another release's run waits for its own worker instead of being refused)."""
    mine = {"engine_release": release} if release else {}
    with transaction.atomic():
        for lane in lanes:
            run = (
                Run.objects.select_for_update(skip_locked=True)
                .filter(status=Run.Status.QUEUED, backend="local", lane=lane, **mine)
                .order_by("created_at")
                .first()
            )
            if run is not None:
                return service.mark_started(run, worker=worker)
    return None


def run_local(run: Run, poll: float = 2.0) -> Run:
    """Run one claimed run to its end with the worker command."""
    spec_uri = storage.store("results").uri(run.storage_prefix + "job.json")
    with tempfile.TemporaryDirectory(prefix="cedars_run_") as work:
        log_path = Path(work) / "worker.log"
        with open(log_path, "wb") as log_file:
            proc = subprocess.Popen(  # noqa: S603 - the command is the platform's own setting, the spec URI its own storage
                [*command(), "run", spec_uri, "--work", str(Path(work) / "job")],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env={**os.environ, "RECET_PLATFORM_RUN": str(run.id)},
            )
            stopped = False
            while proc.poll() is None:
                time.sleep(poll)
                sync_from_storage(run)
                if not stopped and Run.objects.filter(pk=run.pk, cancel_requested_at__isnull=False).exists():
                    proc.terminate()
                    stopped = True
            code = proc.wait()
        tail = log_path.read_bytes()[-4000:].decode("utf-8", "replace")
    receipt = sync_from_storage(run)
    error = "" if code == 0 else ((receipt or {}).get("error") or _error_from(tail, code))
    return service.finish(run, exit_code=code, receipt=receipt, error=error)


def _error_from(tail: str, code: int) -> str:
    if code == 2:
        refused = [line for line in tail.splitlines() if "refused" in line]
        return refused[-1] if refused else tail.strip()[-1000:]
    return tail.strip()[-2000:] or f"the worker ended with exit code {code}"


def work(
    once: bool = False,
    poll: float = 2.0,
    idle: float = 2.0,
    lanes=(Run.Lane.SMALL, Run.Lane.LARGE),
    *,
    take_checks: bool = True,
    price: bool = True,
) -> int:
    """The worker loop: take a check job of its release if one waits (they are short, and a filer may be waiting for one), else a run to
    price, repeat. Returns the number of runs priced (once=True: at most one run or check job). `price=False`: a worker kept free for
    checks; `take_checks=False`: one that only prices."""
    name = f"{socket.gethostname()}:{os.getpid()}"
    release = worker_release()
    log.info("worker %s runs ReCET %s", name, release or "(unknown release)")
    done = 0
    while True:
        heartbeat(name, release, lanes, priced=done)
        if take_checks and check_one(name, release, poll=poll):
            if once:
                return done
            continue
        run = claim_next(name, lanes, release) if price else None
        if run is None:
            if once:
                return done
            time.sleep(idle)
            continue
        log.info("pricing %s (%s measures)", run.id, run.measure_count)
        try:
            run_local(run, poll=poll)
        except Exception as e:  # noqa: BLE001 - a crash here must not leave the run "running" for ever
            log.exception("run %s", run.id)
            service.finish(run, exit_code=None, error=f"The platform's worker failed: {e}")
        done += 1
        if once:
            return done


# --- check jobs on the local worker


def check_one(name: str, release: str, poll: float = 2.0) -> bool:
    """Take the next queued check job of this worker's release and run it; False when none waits."""
    job = checks.claim_next(name, release)
    if job is None:
        return False
    log.info("checking %s (%s, %s rows)", job.pk, job.table, job.rows)
    try:
        run_check_local(job)
    except Exception as e:  # noqa: BLE001 - a crash here must not leave the job "running" for ever
        log.exception("check job %s", job.pk)
        checks.finish(job, exit_code=None, error=f"The platform's worker failed: {e}")
    return True


def run_check_local(job: CheckJob) -> CheckJob:
    """Run one claimed check job to its end with the worker command (`recet worker check`)."""
    with tempfile.TemporaryDirectory(prefix="cedars_check_") as work:
        out = subprocess.run(  # noqa: S603 - the command is the platform's own setting, the spec URI its own storage
            [*command(), "check", checks.spec_uri(job), "--work", str(Path(work) / "job")],
            capture_output=True,
            timeout=getattr(settings, "RULES_CHECK_TIMEOUT_SECONDS", 3600),
            check=False,
        )
    tail = ((out.stdout or b"") + (out.stderr or b""))[-4000:].decode("utf-8", "replace")
    receipt = checks.output(job, "receipt.json")
    error = "" if out.returncode == 0 else ((receipt or {}).get("error") or _error_from(tail, out.returncode))
    return checks.finish(job, exit_code=out.returncode, receipt=receipt, error=error)


def submit_check(job: CheckJob) -> None:
    """The rules service's dispatcher: hand a queued check job to this platform's backend."""
    if default_backend_name() == "batch":
        BatchBackend().submit_check(job)
    else:
        checks.submitted(job, "local")


def resolve_release(release: str) -> str:
    """The release that checks and prices a version naming `release`: itself, or the default release for a version naming none."""
    if release:
        return release
    r = service.default_release()
    return r.release if r is not None else ""


# --- AWS Batch


class BatchBackend:
    name = "batch"

    def __init__(self, client=None):
        if client is None:
            import boto3

            client = boto3.client("batch")
        self.client = client

    def queue(self, run: Run) -> str:
        queues = settings.CALCULATIONS_BATCH_QUEUES
        return queues[run.lane]

    def submit(self, run: Run) -> None:
        release = EngineRelease.objects.get(release=run.engine_release)
        definition = release.batch_job_definition or getattr(settings, "CALCULATIONS_BATCH_JOB_DEFINITION", "")
        if not definition:
            service.finish(run, exit_code=None, error=f"ReCET {release.release} has no AWS Batch job definition set.")
            return
        response = self.client.submit_job(  # the job definition runs `recet worker run Ref::job_spec` (infra/terraform workers.tf)
            jobName=run.job_id[:128],
            jobQueue=self.queue(run),
            jobDefinition=definition,
            parameters={"job_spec": storage.store("results").uri(run.storage_prefix + "job.json")},
            tags={"platform_run_id": str(run.id), "organisation": run.organisation.code if run.organisation else "community"},
            propagateTags=True,
        )
        Run.objects.filter(pk=run.pk).update(backend_job_id=response["jobId"])

    def submit_check(self, job: CheckJob) -> None:
        """A check job: the release's job definition on the small-runs queue, its command `recet worker check <spec URI>`."""
        release = EngineRelease.objects.filter(release=job.engine_release).first()
        definition = (release.batch_job_definition if release else "") or getattr(settings, "CALCULATIONS_BATCH_JOB_DEFINITION", "")
        if not definition:
            checks.submitted(job, "batch")
            checks.finish(job, exit_code=None, error=f"ReCET {job.engine_release} has no AWS Batch job definition set.")
            return
        response = self.client.submit_job(
            jobName=f"check-{job.pk}"[:128],
            jobQueue=settings.CALCULATIONS_BATCH_QUEUES[Run.Lane.SMALL],
            jobDefinition=definition,
            containerOverrides={"command": ["recet", "worker", "check", checks.spec_uri(job)]},
            tags={"platform_check_id": str(job.pk)},
            propagateTags=True,
        )
        checks.submitted(job, "batch", response["jobId"])

    def sync_checks(self, found) -> int:
        """Bring check jobs up to date with Batch and storage; returns how many ended."""
        found = [j for j in found if j.backend_job_id]
        described = {}
        for i in range(0, len(found), 100):
            for job in self.client.describe_jobs(jobs=[j.backend_job_id for j in found[i : i + 100]])["jobs"]:
                described[job["jobId"]] = job
        ended = 0
        for job in found:
            info = described.get(job.backend_job_id, {})
            status = info.get("status")
            if job.status == CheckJob.Status.QUEUED and status in ("STARTING", "RUNNING", "SUCCEEDED", "FAILED"):
                job = checks.mark_started(job, "aws-batch")
            receipt = checks.output(job, "receipt.json")
            if status in ("SUCCEEDED", "FAILED") or (receipt and receipt.get("finished")):
                code = (info.get("container") or {}).get("exitCode")
                if code is None and receipt:
                    code = 0 if receipt.get("status") == "succeeded" else 1
                checks.finish(job, exit_code=code, receipt=receipt, error="" if code == 0 else (info.get("statusReason") or ""))
                ended += 1
        return ended

    def cancel(self, run: Run) -> None:
        if run.backend_job_id:
            self.client.terminate_job(jobId=run.backend_job_id, reason=f"stopped in CEDARS by {run.cancelled_by or 'the platform'}")

    def sync(self, runs) -> int:
        """Bring the runs' records up to date with Batch and storage; returns how many ended."""
        runs = [r for r in runs if r.backend_job_id]
        jobs = {}
        for i in range(0, len(runs), 100):
            ids = [r.backend_job_id for r in runs[i : i + 100]]
            for job in self.client.describe_jobs(jobs=ids)["jobs"]:
                jobs[job["jobId"]] = job
        ended = 0
        for run in runs:
            job = jobs.get(run.backend_job_id, {})
            status = job.get("status")
            if run.status == Run.Status.QUEUED and status in ("STARTING", "RUNNING", "SUCCEEDED", "FAILED"):
                run = service.mark_started(run, worker="aws-batch", backend_job_id=run.backend_job_id)
            receipt = sync_from_storage(run)
            if status in ("SUCCEEDED", "FAILED") or (receipt and receipt.get("finished")):
                code = (job.get("container") or {}).get("exitCode")
                if code is None and receipt:
                    code = 0 if receipt.get("status") == "succeeded" else 1
                error = "" if code == 0 else (job.get("statusReason") or "")
                service.finish(run, exit_code=code, receipt=receipt, error=error)
                ended += 1
        return ended


def sync_batch() -> int:
    runs = list(Run.objects.filter(backend="batch", status__in=[Run.Status.QUEUED, Run.Status.RUNNING]))
    return BatchBackend().sync(runs) if runs else 0


def sync_batch_checks() -> int:
    found = list(checks.jobs().filter(backend="batch", status__in=[CheckJob.Status.QUEUED, CheckJob.Status.RUNNING]))
    return BatchBackend().sync_checks(found) if found else 0

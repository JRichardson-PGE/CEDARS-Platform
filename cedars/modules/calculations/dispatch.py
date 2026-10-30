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
changed only through service.py."""

from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.db import transaction

from cedars.core import storage

from . import service
from .models import EngineRelease, Run

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


def claim_next(worker: str, lanes=(Run.Lane.SMALL, Run.Lane.LARGE)) -> Run | None:
    """Take the next queued local run, small runs first, oldest first."""
    with transaction.atomic():
        for lane in lanes:
            run = (
                Run.objects.select_for_update(skip_locked=True)
                .filter(status=Run.Status.QUEUED, backend="local", lane=lane)
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


def work(once: bool = False, poll: float = 2.0, idle: float = 5.0, lanes=(Run.Lane.SMALL, Run.Lane.LARGE)) -> int:
    """The worker loop: take a run, price it, repeat. Returns the number of runs priced (once=True: at most one)."""
    name = f"{socket.gethostname()}:{os.getpid()}"
    done = 0
    while True:
        run = claim_next(name, lanes)
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

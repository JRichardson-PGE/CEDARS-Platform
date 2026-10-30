"""Calculation runs: who may start and see them, how a job is put together for the ReCET worker, and what a run's record says as it
moves from queued to finished. Pages and the API call these functions; every change is an audit event.

Who sees what (BR-01, Jake 2026-10-29): a run made for an organisation belongs to it, and its members with calculations.read or
calculations.submit see it, so it stays with the organisation when its owner leaves; a run with no organisation (a community account's
own) is its owner's alone. Starting a run for an organisation takes calculations.submit there; stopping one takes being its owner (still
a member) or calculations.admin / org.admin there."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from cedars.core import storage
from cedars.services import audit
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.refdata import service as refdata
from cedars.services.refdata.models import PackVersion
from cedars.services.registry import service as registry
from cedars.services.registry.models import AllocationVersion

from . import jobfiles, jobspec
from .models import EngineRelease, Run

RECET = "recet"
OUTPUT_FORMATS = {"csv.zip": "CSV in zip files", "parquet": "Parquet", "xlsx": "Excel (up to 50,000 measures)"}
FILING_CHECKS = {"": "None", "claims": "Claims rules", "budget_filing": "Budget filing rules"}
# Settings a replica (a past CET release, D13) does not take: ReCET's engine refuses them for replicas (cet_versions/job.REFUSED).
REPLICA_REFUSES = ("allocate_statewide", "filing_validation", "preflight_only")
# Seconds per 1,000,000 measures (server, 2026-10-28/29: ReCET 392 s with SCT; a replica 427 s on the laptop), before calibration.
SECONDS_PER_MILLION = {RECET: 400, "replica": 430}
BASE_SECONDS = 10


def max_records() -> int:
    return int(getattr(settings, "CALCULATIONS_MAX_RECORDS", 2_000_000))


def small_run_records() -> int:
    return int(getattr(settings, "CALCULATIONS_SMALL_RUN_RECORDS", 10_000))


# --- engine releases (configured on the admin page, never in code)


def releases():
    return EngineRelease.objects.filter(active=True)


def default_release() -> EngineRelease | None:
    return releases().filter(default=True).first() or releases().first()


def calculation_label(calc: str) -> str:
    return "ReCET" if calc in ("", RECET) else f"CET {calc.removeprefix('cet-')} replica"


def accs_for(release: EngineRelease, calculation: str) -> list[str]:
    return [str(a) for a in (release.accs or {}).get(calculation, [])]


@transaction.atomic
def add_release(actor, *, release: str, calculations: list[str], accs: dict, image="", batch_job_definition="", notes="", default=False):
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator adds engine releases.")
    if RECET not in calculations:
        raise AccessDenied('A release offers ReCET\'s own calculation ("recet").')
    bad = [c for c in calculations if c != RECET and not c.startswith("cet-")]
    if bad:
        raise AccessDenied(f"Not calculations: {', '.join(bad)}. Use recet or cet-<release>.")
    missing = [c for c in calculations if not accs.get(c)]
    if missing:
        raise AccessDenied(f"Name the ACCs each calculation has; none given for {', '.join(missing)}.")
    if default:
        EngineRelease.objects.filter(default=True).update(default=False)
    r = EngineRelease.objects.create(
        release=release.strip(),
        calculations=calculations,
        accs={c: [str(a) for a in accs[c]] for c in calculations},
        image=image,
        batch_job_definition=batch_job_definition,
        notes=notes,
        default=default or not releases().exists(),
        added_by=actor,
    )
    audit.record(
        "engine_release.added", obj=r, after=audit.snapshot(r, ["release", "calculations", "accs", "image", "default"]), actor=actor
    )
    return r


@transaction.atomic
def set_default_release(actor, r: EngineRelease) -> EngineRelease:
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator changes the default release.")
    if not r.active:
        raise AccessDenied("A retired release cannot be the default.")
    before = EngineRelease.objects.filter(default=True).first()
    EngineRelease.objects.filter(default=True).update(default=False)
    EngineRelease.objects.filter(pk=r.pk).update(default=True)
    audit.record("engine_release.default", obj=r, before={"default": str(before) if before else None}, actor=actor)
    r.refresh_from_db()
    return r


@transaction.atomic
def retire_release(actor, r: EngineRelease, reason: str = "") -> EngineRelease:
    if actor is not None and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator retires engine releases.")
    if r.default:
        raise AccessDenied("Make another release the default first.")
    EngineRelease.objects.filter(pk=r.pk).update(active=False)
    audit.record("engine_release.retired", obj=r, reason=reason, actor=actor)
    r.refresh_from_db()
    return r


# --- who may do what


def can_submit(user, organisation) -> bool:
    if organisation is None:
        return user is not None and user.is_authenticated
    return access.has_permission(user, organisation, "calculations.submit")


def readable_organisations(user):
    return [
        o
        for o in access.organisations_of(user)
        if access.has_permission(user, o, "calculations.read") or access.has_permission(user, o, "calculations.submit")
    ]


def visible_runs(user, organisations=None):
    """The runs a user sees: their own community runs and their organisations' runs (for one organisation when given)."""
    orgs = readable_organisations(user)
    if organisations is not None:
        orgs = [o for o in orgs if o in organisations]
    q = Q(organisation__isnull=True, owner=user) | Q(organisation__in=orgs)
    return Run.objects.filter(q).select_related("organisation", "owner", "allocation_version")


def can_read(user, run: Run) -> bool:
    if run.organisation is None:
        return run.owner_id == user.pk
    return access.has_permission(user, run.organisation, "calculations.read") or access.has_permission(
        user, run.organisation, "calculations.submit"
    )


def can_cancel(user, run: Run) -> bool:
    if run.organisation is None:
        return run.owner_id == user.pk
    if run.owner_id == user.pk and access.has_permission(user, run.organisation, "calculations.submit"):
        return True
    return access.has_permission(user, run.organisation, "calculations.admin") or access.is_org_admin(user, run.organisation)


# --- starting a run


@dataclass
class RunRequest:
    """What a person (New run) or a system (the API) asks for. `settings` are RunConfig settings the job spec may carry."""

    name: str
    release: EngineRelease
    calculation: str
    acc_version: str
    output_format: str = "csv.zip"
    settings: dict | None = None
    allocation_version: AllocationVersion | None = None
    cet_outputs: object = None  # a CET run's outputs CSV (a local path or an UploadedFile): a parallel run, compared measure by measure


def _check(req: RunRequest) -> dict:
    if not req.release.active:
        raise AccessDenied(f"ReCET {req.release.release} is retired; choose another release.")
    if req.calculation not in req.release.calculations:
        raise AccessDenied(f"ReCET {req.release.release} does not offer {calculation_label(req.calculation)}.")
    if req.acc_version not in accs_for(req.release, req.calculation):
        raise AccessDenied(
            f"{calculation_label(req.calculation)} has no {req.acc_version} ACC in ReCET {req.release.release}; it has "
            + ", ".join(accs_for(req.release, req.calculation))
            + "."
        )
    if req.output_format not in OUTPUT_FORMATS:
        raise AccessDenied(f"Output format {req.output_format} is not offered.")
    st = {k: v for k, v in (req.settings or {}).items() if v not in (None, "")}
    unknown = sorted(set(st) - set(jobspec.SETTINGS))
    if unknown:
        raise AccessDenied(f"Not run settings: {', '.join(unknown)}.")
    if req.calculation != RECET:
        refused = [k for k in REPLICA_REFUSES if st.get(k)]
        if refused:
            raise AccessDenied(
                f"A CET replica does not take {', '.join(jobspec.SETTINGS[k] for k in refused).lower()}: it prices the job as that CET "
                "release did. Use ReCET's own calculation for these."
            )
    if st.get("filing_validation") and st["filing_validation"] not in FILING_CHECKS:
        raise AccessDenied(f"Filing checks {st['filing_validation']} are not offered.")
    if req.allocation_version is not None:
        if not st.get("allocate_statewide"):
            raise AccessDenied("An allocation version applies only when statewide programs are allocated.")
        if req.allocation_version.status != AllocationVersion.Status.CONFIRMED:
            raise AccessDenied(f"{req.allocation_version} is a draft; only confirmed allocation versions price runs.")
    return st


def input_hash(
    inputs: dict, req_settings: dict, release: str, calculation: str, acc: str, allocation_sha: str | None, packs: dict | None = None
) -> str:
    payload = {
        "inputs": {k: v["sha256"] for k, v in sorted(inputs.items())},
        "settings": req_settings,
        "release": release,
        "calculation": calculation,
        "acc": acc,
        "allocation": allocation_sha,
    }
    if packs:  # the reference data packs pinned (their fingerprints), so the same job on other data does not share the hash
        payload["packs"] = {k: v["fingerprint"] for k, v in sorted(packs.items())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# --- the reference data a run is pinned to (BR-11.8, BR-06.21)

# The reference-folder files each pack kind holds for ReCET's own calculation, by their paths in the engine's reference folder (ReCET
# ARCHITECTURE 7.1: manifest.json keys every file by that path, and a job spec's packs.files pins those keys). A pack's manifest entries
# under another path are recorded with the run but not pinned, since the worker could not find them.
REFERENCE_PREFIXES = {PackVersion.Kind.ACC_ELECTRIC: "acc/{acc}/", PackVersion.Kind.IMPACT_PROFILES: "impact_profiles/"}


def packs_for(req: RunRequest, *, filing: bool) -> list[PackVersion]:
    """The registered packs a run uses: the electric ACC pack of its ACC year and the impact-profile library in force. A filing takes
    only published packs; an analysis run takes the newest published one, or a draft when nothing is published (BR-11.8). A replica
    reads its own release's tables, so it is pinned to none. Kinds with no registered pack are not pinned (the worker's folder is used
    as it is), and the run record says which kinds were pinned."""
    if req.calculation != RECET:
        return []
    out = []
    for kind in REFERENCE_PREFIXES:
        packs = PackVersion.objects.filter(kind=kind).exclude(status=PackVersion.Status.WITHDRAWN)
        if kind == PackVersion.Kind.ACC_ELECTRIC:
            packs = packs.filter(acc_year=int(req.acc_version)) if req.acc_version.isdigit() else packs.none()
        published = packs.filter(status=PackVersion.Status.PUBLISHED).order_by("-published_at", "-registered_at").first()
        pack = published or packs.filter(status=PackVersion.Status.DRAFT).order_by("-registered_at").first()
        if pack is None:
            continue
        ok, why = refdata.usable(pack, "filing" if filing else "analysis")
        if not ok:
            raise AccessDenied(f"{why} Publish it first, or price this as an analysis run.")
        out.append(pack)
    return out


def pack_files(packs, acc: str) -> dict:
    """The job spec's packs.files: {reference-folder path: {sha256, version}} for the files of `packs` under their kind's folder."""
    files = {}
    for p in packs:
        prefix = REFERENCE_PREFIXES[p.kind].format(acc=acc)
        for rel, entry in sorted(p.manifest.items()):
            if rel.startswith(prefix):
                files[rel] = {"sha256": entry["sha256"], "version": f"{p.label} {p.version}".strip()}
    return files


@transaction.atomic
def create_run(actor, organisation, req: RunRequest, job_zip: Path, *, submission_version=None, backend: str | None = None) -> Run:
    """Check the request and the upload, put the inputs in storage, write the job spec and queue the run. `job_zip` is a local file
    (an upload's temporary file); it is copied, never moved."""
    from . import dispatch

    # a filing's run: the submissions service has checked <module>.submit
    if submission_version is None and not can_submit(actor, organisation):
        raise AccessDenied(f"You need calculations.submit in {organisation.code} to start a run for it.")
    if not req.name.strip():
        raise AccessDenied("Give the run a name.")
    st = _check(req)
    if submission_version is not None and req.calculation != RECET:
        raise AccessDenied("A filing is priced only with ReCET's own calculation (D13: replicas are never a filing's engine).")
    if req.cet_outputs is not None and req.calculation != RECET:
        raise AccessDenied("A CET replica does not take a CET outputs comparison: compare the replica's run with ReCET's on Compare.")
    try:
        info = jobfiles.inspect(Path(job_zip))
    except jobfiles.JobFileError as e:
        raise AccessDenied(str(e)) from None
    if info.records == 0:
        raise AccessDenied("Measure.csv has no records.")
    if info.records > max_records():
        raise AccessDenied(f"The job has {info.records:,} measure records; a run takes at most {max_records():,} (BRD v0.5). Split it.")

    run = Run(
        organisation=organisation,
        owner=actor,
        name=req.name.strip()[:200],
        engine_release=req.release.release,
        calculation=req.calculation,
        acc_version=req.acc_version,
        settings=st,
        output_format=req.output_format,
        measure_count=info.records,
        lane=Run.Lane.SMALL if info.records <= small_run_records() else Run.Lane.LARGE,
        submission_version=submission_version,
        backend=backend or dispatch.default_backend_name(),
    )
    run.storage_prefix = f"runs/{run.id}/"
    uploads, results = storage.store("uploads"), storage.store("results")
    inputs = {}
    if req.cet_outputs is not None:  # a parallel run: the engine writes cet_comparison_* (ReCET's RunConfig.cet_outputs)
        with local_copy(req.cet_outputs, ".csv") as cet:
            try:
                jobfiles.check_cet_outputs(cet)
            except jobfiles.JobFileError as e:
                raise AccessDenied(str(e)) from None
            key = run.storage_prefix + "cet_outputs.csv"
            inputs["cet_outputs"] = {"key": key, "sha256": storage.sha256_of(cet), "bytes": uploads.put_file(key, cet).size}
    inputs["job_zip"] = {"key": run.storage_prefix + "job.zip", "sha256": storage.sha256_of(Path(job_zip))}
    inputs["job_zip"]["bytes"] = uploads.put_file(inputs["job_zip"]["key"], Path(job_zip)).size
    allocation_sha = None
    if st.get("allocate_statewide"):
        version = req.allocation_version or (registry.most_recent_allocation(info.first_year) if info.first_year else None)
        if version is None:
            raise AccessDenied(
                f"No confirmed statewide allocation version has factors for {info.first_year or 'the job'}'s program year; "
                "choose one, or price without allocation."
            )
        factors = registry.allocation_csv(version).encode()
        allocation_sha = hashlib.sha256(factors).hexdigest()
        key = run.storage_prefix + "sw_factors.csv"
        inputs["sw_factors"] = {"key": key, "sha256": allocation_sha, "bytes": uploads.put_bytes(key, factors).size}
        run.allocation_version = version
    run.inputs = inputs
    packs = packs_for(req, filing=submission_version is not None)
    pinned = refdata.pin(packs)
    run.input_hash = input_hash(inputs, st, req.release.release, req.calculation, req.acc_version, allocation_sha, pinned)
    run.estimate_seconds = estimate_seconds(run)
    run.spec = jobspec.build(run, uploads, results, packs=pinned, pack_files=pack_files(packs, req.acc_version))
    run.save()
    results.put_bytes(run.storage_prefix + "job.json", json.dumps(run.spec, indent=2).encode())
    audit.record(
        "calculation_run.queued",
        obj=run,
        organisation=organisation,
        after={
            "name": run.name,
            "release": run.engine_release,
            "calculation": run.calculation,
            "acc": run.acc_version,
            "records": run.measure_count,
            "input_hash": run.input_hash,
            "allocation_version": str(run.allocation_version) if run.allocation_version else None,
            "packs": {k: f"{v['label']} {v['version']}".strip() for k, v in pinned.items()},
        },
        actor=actor,
    )
    transaction.on_commit(lambda: dispatch.backend(run.backend).submit(run))
    return run


@contextmanager
def local_copy(upload, suffix: str = ""):
    """A local path for a file: a path as it is, a Django UploadedFile's temporary file, or a spooled copy of one held in memory."""
    if isinstance(upload, (str, Path)):
        yield Path(upload)
        return
    path = getattr(upload, "temporary_file_path", None)
    if path is not None:
        yield Path(path())
        return
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        for chunk in upload.chunks():
            tmp.write(chunk)
        tmp.flush()
        yield Path(tmp.name)


def create_run_from_upload(actor, organisation, req: RunRequest, upload, **kw) -> Run:
    """create_run for a Django UploadedFile."""
    with local_copy(upload, ".zip") as job:
        return create_run(actor, organisation, req, job, **kw)


# --- the run's life (the worker side: dispatch.py and the worker command)


def _event(run: Run, action: str, **after):
    audit.record(action, obj=run, organisation=run.organisation, after=after or None, actor=None)


@transaction.atomic
def mark_started(run: Run, worker: str = "", backend_job_id: str = "") -> Run:
    Run.objects.filter(pk=run.pk).update(
        status=Run.Status.RUNNING, started_at=timezone.now(), worker=worker[:200], backend_job_id=backend_job_id or run.backend_job_id
    )
    run.refresh_from_db()
    _event(run, "calculation_run.started", worker=worker)
    return run


def apply_progress(run: Run, progress: dict) -> None:
    if progress and progress != run.progress:
        Run.objects.filter(pk=run.pk).update(progress=progress)
        run.progress = progress


EXIT_STATUS = {0: Run.Status.SUCCEEDED, 1: Run.Status.FAILED, 2: Run.Status.REFUSED}


@transaction.atomic
def finish(run: Run, *, exit_code: int | None, receipt: dict | None = None, error: str = "") -> Run:
    """Record the worker's end: exit 0 succeeded, 1 the run failed (a receipt says why), 2 the engine refused the spec; a stop asked
    for while running ends as stopped whatever the exit."""
    run = Run.objects.select_for_update().get(pk=run.pk)
    if run.ended:
        return run
    if run.cancel_requested_at:
        status = Run.Status.CANCELLED
    else:
        status = EXIT_STATUS.get(exit_code, Run.Status.FAILED)
    receipt = receipt or {}
    run.status, run.finished_at, run.receipt = status, timezone.now(), receipt
    run.error = (error or receipt.get("error") or "")[:20000]
    if status == Run.Status.SUCCEEDED and run.progress.get("stage") != "done":
        run.progress = {**run.progress, "stage": "done", "fraction": 1.0, "seconds_left": 0}
    run.save()
    _event(run, f"calculation_run.{status}", exit_code=exit_code, seconds=receipt.get("seconds"), totals=receipt.get("totals") or None)
    if status == Run.Status.SUCCEEDED and run.submission_version is not None:
        _record_for_submission(run)
    if status != Run.Status.CANCELLED:
        transaction.on_commit(lambda: _notify_owner(run))
    return run


NOTICE_TITLES = {
    Run.Status.SUCCEEDED: "Your run finished: {name}",
    Run.Status.FAILED: "Your run failed: {name}",
    Run.Status.REFUSED: "Your run was refused: {name}",
}


def _notify_owner(run: Run) -> None:
    """Tell the run's owner it ended (BR-06.2): on their notifications page, and by email unless they turned that off. The key keeps a
    retried worker from telling them twice."""
    from django.urls import reverse

    from cedars.services.notifications import service as notifications

    totals = (run.receipt or {}).get("totals") or {}
    if run.status == Run.Status.SUCCEEDED:
        body = ", ".join(f"{k} {v:.2f}" for k, v in totals.items() if k.endswith("Ratio") and isinstance(v, int | float))
    else:
        body = (run.error or "").strip().splitlines()[-1][:300] if run.error else ""
    notifications.notify(
        run.owner,
        NOTICE_TITLES[run.status].format(name=run.name),
        body,
        reverse("calculations:run", args=[run.pk]),
        key=f"run:{run.pk}:{run.status}",
    )


def expired_runs(cutoff):
    """Retention purger for record class "scratch_run" (decision M): runs that ended before the cutoff, with their files, except a run
    a submission version was priced with (it is kept with that version)."""
    from cedars.services.submissions.models import SubmissionVersion

    cited = {str(r) for r in SubmissionVersion.objects.exclude(calculation={}).values_list("calculation__run", flat=True) if r}
    runs = Run.objects.filter(record_class="scratch_run", status__in=Run.ENDED, finished_at__lt=cutoff, submission_version__isnull=True)
    for run in runs.select_related("organisation"):
        if str(run.pk) in cited:
            continue

        def delete(run=run):
            audit.record(
                "calculation_run.expired",
                obj=run,
                organisation=run.organisation,
                before={"name": run.name, "finished_at": run.finished_at.isoformat(), "input_hash": run.input_hash},
                actor=None,
            )
            prefix = run.storage_prefix
            run.delete()
            transaction.on_commit(
                lambda: [storage.store(a).delete_prefix(prefix) for a in ("uploads", "results")]
            )  # once the record is gone

        yield f"calculation_run:{run.pk}", delete


def price_submission(actor, version, job_zip: Path, *, name: str, acc_version: str, allocation_version=None, settings: dict | None = None):
    """The pricer the submissions service calls (registered in apps.py): a filing version's job on the default release, ReCET's own
    calculation; with settings allocate_statewide, statewide programs are allocated with the filing's allocation version (or the most
    recent confirmed one for the program year)."""
    release = default_release()
    if release is None:
        raise AccessDenied("No engine release is the default yet; a CPUC system administrator sets one on Engine releases.")
    req = RunRequest(
        name=name,
        release=release,
        calculation=RECET,
        acc_version=acc_version,
        settings=settings or {},
        allocation_version=allocation_version if (settings or {}).get("allocate_statewide") else None,
    )
    return create_run(actor, version.submission.organisation, req, job_zip, submission_version=version).pk


GROUP_SUMS = re.compile(r"group_sums(\d+)_")


def results_keys(run: Run) -> dict:
    """Where a finished run's kept results are in the results area: the group sums (one row per PA, program, sector, segment and program
    year; the newest cube version when there are several) and the measure-level parquet."""
    out = {"group_sums": None, "measure_level": None}
    prefix = run.storage_prefix + "out/_results/"
    names = {f.key.rsplit("/", 1)[-1]: f.key for f in storage.store("results").list(prefix) if f.key.endswith(".parquet")}
    cubes = {int(m.group(1)): key for name, key in names.items() if (m := GROUP_SUMS.match(name))}
    out["group_sums"] = cubes[max(cubes)] if cubes else None
    out["measure_level"] = next((key for name, key in sorted(names.items()) if name.startswith("measure_level_")), None)
    return out


def _record_for_submission(run: Run) -> None:
    """Tell the submissions service a run on the version's rows has finished, so the version can be confirmed (its hash: the run's), with
    what it was priced with and where its results are, so the filing module reads them without this module."""
    from cedars.services.submissions import service as submissions

    packs = {rel: f.get("sha256") for rel, f in ((run.receipt.get("packs") or {}).get("files") or {}).items()}
    out = run.storage_prefix + "out/"
    details = {
        "job_sha256": run.inputs["job_zip"]["sha256"],
        "job_key": run.inputs["job_zip"]["key"],
        "release": run.engine_release,
        "acc": run.acc_version,
        "allocation_version": {"id": run.allocation_version_id, "label": run.allocation_version.label} if run.allocation_version else None,
        "allocation_sha256": (run.inputs.get("sw_factors") or {}).get("sha256"),
        "spec_key": run.storage_prefix + "job.json",
        "receipt_key": out + "receipt.json",
        "results": results_keys(run),
    }
    try:
        submissions.record_calculation(
            None, run.submission_version, run=str(run.id), input_hash=run.input_hash, pack_versions=packs, details=details
        )
    except AccessDenied as e:
        Run.objects.filter(pk=run.pk).update(error=f"Finished, but the filing was not updated: {e}")


@transaction.atomic
def cancel_run(actor, run: Run, reason: str = "") -> Run:
    from . import dispatch

    if not can_cancel(actor, run):
        raise AccessDenied("Only the run's owner or the organisation's calculation admins can stop it.")
    run = Run.objects.select_for_update().get(pk=run.pk)
    if run.ended:
        raise AccessDenied("That run has already ended.")
    now = timezone.now()
    run.cancel_requested_at, run.cancelled_by = now, actor
    if run.status == Run.Status.QUEUED:
        run.status, run.finished_at = Run.Status.CANCELLED, now
    run.save()
    audit.record("calculation_run.cancelled", obj=run, organisation=run.organisation, reason=reason, actor=actor)
    transaction.on_commit(lambda: dispatch.backend(run.backend).cancel(run))
    return run


# --- reading a run


def output_files(run: Run) -> list[dict]:
    """The run's output files: from the receipt (with their SHA-256) when there is one, else what is in storage so far."""
    prefix = run.storage_prefix + "out/"
    listed = {f.key[len(prefix) :]: f.size for f in storage.store("results").list(prefix)}
    outputs = (run.receipt or {}).get("outputs") or {}
    names = sorted(set(listed) | set(outputs) | ({"receipt.json"} if "receipt.json" in listed else set()))
    out = []
    for name in names:
        if name == "progress.json" or name.startswith("_results/") or name not in listed:  # _results: what the Results pages read
            continue
        out.append({"name": name, "bytes": listed[name], "sha256": (outputs.get(name) or {}).get("sha256", "")})
    return out


def output_key(run: Run, name: str) -> str | None:
    """The storage key of one of the run's output files, or None when it has no such file."""
    if any(f["name"] == name for f in output_files(run)):
        return run.storage_prefix + "out/" + name
    return None


def queue_position(run: Run) -> int | None:
    """1 = next in its lane; None once it has started."""
    if run.status != Run.Status.QUEUED:
        return None
    return Run.objects.filter(status=Run.Status.QUEUED, lane=run.lane, created_at__lt=run.created_at).count() + 1


def calibration() -> float:
    """Median of actual / estimated seconds over the last ten finished runs of at least 20,000 measures (1.0 before there are any),
    held between 0.25 and 4, as ReCET's queue estimate (recet/web/eta.py) does."""
    ratios = []
    for r in Run.objects.filter(status=Run.Status.SUCCEEDED, measure_count__gte=20_000, estimate_seconds__gt=0).order_by("-finished_at")[
        :10
    ]:
        if r.seconds:
            ratios.append(r.seconds / r.estimate_seconds)
    if not ratios:
        return 1.0
    ratios.sort()
    mid = len(ratios) // 2
    median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
    return min(4.0, max(0.25, median))


def estimate_seconds(run: Run) -> int:
    rate = SECONDS_PER_MILLION[RECET if run.calculation == RECET else "replica"]
    if run.settings.get("include_societal") is False:
        rate *= 0.85  # SCT is about 15% of a run (ReCET 0.1.66)
    if run.settings.get("preflight_only"):
        rate *= 0.17
    return int(BASE_SECONDS + rate * (run.measure_count or 0) / 1_000_000)


def _left(run: Run, scale: float) -> float:
    if run.status == Run.Status.RUNNING:
        if run.progress.get("seconds_left") is not None:
            return float(run.progress["seconds_left"])
        elapsed = (timezone.now() - run.started_at).total_seconds() if run.started_at else 0
        return max(0.0, (run.estimate_seconds or 0) * scale - elapsed)
    return (run.estimate_seconds or 0) * scale


def eta(run: Run) -> dict:
    """{"starts_in", "done_in"} in seconds (estimates), from the runs ahead of it in its lane over the lane's workers."""
    if run.ended:
        return {}
    scale = calibration()
    if run.status == Run.Status.RUNNING:
        return {"starts_in": 0, "done_in": int(_left(run, scale))}
    workers = max(1, int(getattr(settings, "CALCULATIONS_WORKERS", {}).get(run.lane, 1)))
    ahead = Run.objects.filter(lane=run.lane, status__in=[Run.Status.QUEUED, Run.Status.RUNNING], created_at__lt=run.created_at).order_by(
        "created_at"
    )
    slots = [0.0] * workers
    for other in ahead:
        i = slots.index(min(slots))
        slots[i] += _left(other, scale)
    starts = min(slots)
    return {"starts_in": int(starts), "done_in": int(starts + _left(run, scale))}

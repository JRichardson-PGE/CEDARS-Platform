"""Check jobs: a filing's rules run by the worker of the engine release its specification version names (`recet worker check`), the
way that release prices it, so a filing is checked and priced by one release for its whole life whatever release this website has
installed.

A caller opens an engine run for the release (`engine_run`) around the code that checks; inside it every `service.check` of a table
becomes a check job instead of running here: the table, the tables its conditions read and a check spec (the table's rules, who files,
the programs, the upload's day, the value lists in force) go to the results store, a CheckJob row is written through its own database
connection (RULES_JOBS_DATABASE, so a worker sees it at once) and handed to the dispatcher Calculations registers (its local worker takes
it from the queue; on AWS Batch it is a job of its own). Then either

- the run waits (`wait=True`: the pre-upload checker, a correction of a few records, examples, the impact test) up to the
  check_wait_seconds limit and turns the worker's findings into findings here; or
- the run does not wait (a filing's check): the job is left pending, the module records the version as being checked, and when the
  job ends the module's finisher (`register_finisher`) checks the version again inside an engine run that is handed the finished jobs,
  whose findings it takes in place of new jobs.

A job's `key` is the SHA-256 of everything its findings depend on, so a check whose inputs have not changed takes the findings of the
last one (a re-check after the job ends, a second upload of the same file). The rules run here instead, in this website's copy of the
engine, when RULES_RUN_IN_WORKER is off (tests, a server without workers), when no dispatcher is registered, or when the named release
is older than the first whose worker runs checks (0.1.95): the check's notes then say so."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cedars.core import storage

from .models import CheckJob

log = logging.getLogger(__name__)

FIRST_RELEASE = (0, 1, 95)  # the first ReCET release whose worker runs checks
AREA = "results"
PREFIX = "checks/"


class CheckNotRun(ValueError):
    """The rules were not run by the release's worker: none runs it now, it had not finished in time, or the check failed."""


@dataclass(frozen=True)
class Dispatcher:
    """How check jobs reach a worker (the Calculations module's): `resolve(release)` the release number that prices a version naming
    `release` ("" = the default release), `submit(job)` hands a queued job to the backend, `serves(release)` whether a worker runs that
    release now (a job for a release no worker runs waits in the queue)."""

    resolve: Callable[[str], str]
    submit: Callable[[CheckJob], None]
    serves: Callable[[str], bool]


_dispatchers: list[Dispatcher] = []
_finishers: dict[str, Callable[[CheckJob], None]] = {}


def register_dispatcher(*, resolve, submit, serves) -> None:
    """Called from the Calculations module's AppConfig.ready (a service never imports a module)."""
    _dispatchers[:] = [Dispatcher(resolve, submit, serves)]


def register_finisher(module: str, finish: Callable[[CheckJob], None]) -> None:
    """A filing module's `finish(job)`, called when a job it submitted ends (`caller["module"]`): it checks the version again with the
    finished jobs once every job of its check has ended."""
    _finishers[module] = finish


def db() -> str:
    return getattr(settings, "RULES_JOBS_DATABASE", "default")


def jobs():
    return CheckJob.objects.using(db())


def release_number(release: str) -> tuple[int, ...] | None:
    m = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", release or "")
    return tuple(int(x) for x in m.groups()) if m else None


def can_check(release: str) -> bool:
    """Whether release `release`'s worker runs checks (`recet worker check`, 0.1.95 and later)."""
    n = release_number(release)
    return n is not None and n >= FIRST_RELEASE


def installed() -> str:
    """The release of this website's own copy of the engine ("" without one)."""
    try:
        from recet import __version__
    except ImportError:
        return ""
    return __version__


def wait_seconds() -> int:
    from cedars.services.releases import limits

    return int(limits.value("check_wait_seconds"))


# --- engine runs


@dataclass
class EngineRun:
    """Where the rules of one check run: `release` (the number), `in_worker` (by its worker; else here), whether the caller `wait`s
    for each job, and what a job records of it (`purpose`, `caller`). `finished`: jobs that ended, by key, whose findings a re-check
    takes. After the check: `pending` (jobs not ended, when not waiting), `used` (jobs whose findings were taken), `failed` (why a job's
    findings could not be had while waiting) and `note` (said on the check when the rules ran here although the version names another
    release)."""

    release: str
    in_worker: bool
    wait: bool = True
    purpose: str = ""
    caller: dict = field(default_factory=dict)
    finished: dict[str, CheckJob] = field(default_factory=dict)
    pending: list[CheckJob] = field(default_factory=list)
    used: list[CheckJob] = field(default_factory=list)
    failed: str = ""
    note: str = ""

    def checking(self) -> dict:
        """What a version being checked records of its jobs ({jobs, release, since})."""
        return {"jobs": [str(j.id) for j in self.pending], "release": self.release, "since": timezone.now().isoformat()}


_current: ContextVar[EngineRun | None] = ContextVar("rules_engine_run", default=None)


def current() -> EngineRun | None:
    return _current.get()


def plan(release: str = "", *, wait: bool = True, purpose: str = "", caller: dict | None = None, finished=()) -> EngineRun:
    """Where a check for a version naming engine release `release` ("" = the default) runs (`EngineRun`)."""
    named = (release or "").strip()
    here = installed()
    common = {"wait": wait, "purpose": purpose, "caller": dict(caller or {}), "finished": {j.key: j for j in finished}}
    if not getattr(settings, "RULES_RUN_IN_WORKER", False) or not _dispatchers:
        return EngineRun(here, False, **common)
    resolved = _dispatchers[0].resolve(named) or named
    if not can_check(resolved):
        note = ""
        if resolved and resolved != here:
            note = (
                f"ReCET {resolved}'s worker cannot run rules kept as data (it is older than "
                f"{'.'.join(map(str, FIRST_RELEASE))}), so this website's copy of the engine, ReCET {here}, ran them."
            )
        return EngineRun(here, False, note=note, **common)
    return EngineRun(resolved, True, **common)


@contextmanager
def engine_run(release: str = "", *, wait: bool = True, purpose: str = "", caller: dict | None = None, finished=()):
    """Run the rules checked inside the block where version release `release` runs them (`plan`); yields the EngineRun."""
    run = plan(release, wait=wait, purpose=purpose, caller=caller, finished=finished)
    token = _current.set(run)
    try:
        yield run
    finally:
        _current.reset(token)


# --- one table's check


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _csv(df) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def _strings(value) -> set[str]:
    if isinstance(value, str):
        return {value.strip().lower()} if value.strip() else set()
    if isinstance(value, dict):
        return set().union(*(_strings(v) for v in value.values())) if value else set()
    if isinstance(value, list | tuple):
        return set().union(*(_strings(v) for v in value)) if value else set()
    return set()


def _lists(rules: list[dict]) -> dict[str, list[dict]]:
    """The value lists in force here that the rules name, under each name they use (a list's own name or the CET field name the engine
    knows it by), as the engine reads a list given with a check: the worker checks exactly this website's codes, whatever its release
    ships. A list with no version published here is left to the worker's own copy."""
    from cedars.services.dictionary import value_lists
    from cedars.services.dictionary.models import ValueList

    try:
        from recet.filing_rules import VALUE_LIST_FILES
    except ImportError:
        VALUE_LIST_FILES = {}
    words = _strings([[r["field"], r["definition"]] for r in rules])
    out: dict[str, list[dict]] = {}
    for name in ValueList.objects.filter(made_for="").values_list("name", flat=True):
        names = {name.lower()} | {k for k, v in VALUE_LIST_FILES.items() if v == name.lower()}
        hit = names & words
        if not hit:
            continue
        codes = value_lists.codes_in_force(name)
        if codes is None:
            continue
        rows = [{"code": c["code"], "start_year": c.get("start_year"), "end_year": c.get("end_year")} for c in codes]
        for n in sorted(hit):
            out[n] = rows
    return out


def _linked(rules: list[dict], table: str, context) -> dict[str, object]:
    """The tables of the context the rules' conditions read, other than the table checked (whose own records it reads)."""
    from recet.filing_rules import PSEUDO_TABLES, conditions

    given = {k.lower(): k for k in (context.tables or {})} if context is not None else {}
    named = set()
    for r in rules:
        for c in conditions(r["definition"]):
            tb = str(c[0]).strip()
            if tb and tb.lower() != table.lower() and tb not in PSEUDO_TABLES and tb.lower() in given:
                named.add(given[tb.lower()])
    return {n: context.tables[n] for n in sorted(named)}


@dataclass
class Part:
    """One table's check, ready to submit: its spec (without the addresses), the files and their key."""

    rules: list[dict]
    table_csv: bytes
    linked: dict[str, bytes]
    context: dict
    skip: list[str]
    key: str
    rows: int


def prepare(run: EngineRun, rs, table: str, df, *, filing_year=None, skip=(), context=None) -> Part:
    rules = [
        {"id": r.rule_id, "severity": r.severity, "table": r.table, "field": r.field, "text": r.message, "kind": r.kind,
         "source": r.source, "definition": r.definition or {}}
        for r in rs.rules.filter(table=table).order_by("rule_id")
    ]  # fmt: skip
    skip = sorted(s for s in skip if any(r["id"] == s for r in rules))
    linked = {n: _csv(t) for n, t in _linked(rules, table, context).items()}
    ctx = {"filing_year": filing_year}
    if context is not None:
        if context.upload_date is not None:
            ctx["upload_date"] = context.upload_date.isoformat()
        if context.filer:
            ctx["filer"] = dict(context.filer)
        if context.keys:
            ctx["keys"] = {k: list(v) for k, v in context.keys.items()}
        if context.programs is not None:
            ctx["programs"] = [{k: "" if v is None else str(v) for k, v in p.items()} for p in context.programs]
    lists = _lists(rules)
    if lists:
        ctx["lists"] = lists
    ctx = {k: v for k, v in ctx.items() if v is not None}
    table_csv = _csv(df)
    core = {
        "release": run.release,
        "rule_set": [rs.code, rs.effective_from.isoformat(), rs.pk],
        "rules": rules,
        "table": table,
        "csv": _sha(table_csv),
        "linked": {n: _sha(b) for n, b in linked.items()},
        "context": ctx,
        "skip": skip,
    }
    key = _sha(json.dumps(core, sort_keys=True, default=str).encode())
    return Part(rules, table_csv, linked, ctx, skip, key, len(df))


def _reusable(key: str, release: str) -> CheckJob | None:
    """A job with the same inputs that has not failed: still to run, or succeeded with its findings still stored."""
    for job in jobs().filter(key=key, engine_release=release).exclude(status__in=[CheckJob.Status.FAILED, CheckJob.Status.REFUSED]):
        if job.status != CheckJob.Status.SUCCEEDED or storage.store(AREA).exists(job.storage_prefix + "out/findings.json"):
            return job
    return None


def submit(run: EngineRun, rs, table: str, part: Part) -> CheckJob:
    """Write the part's files and spec to the results store, record the job (through the jobs connection) and hand it to the backend."""
    jid = uuid.uuid4()
    prefix = f"{PREFIX}{jid}/"
    st = storage.store(AREA)
    st.put_bytes(f"{prefix}in/{table}.csv", part.table_csv)
    linked = {}
    for n, b in part.linked.items():
        st.put_bytes(f"{prefix}in/linked/{n}.csv", b)
        linked[n] = {"uri": st.uri(f"{prefix}in/linked/{n}.csv"), "sha256": _sha(b)}
    spec = {
        "check_spec_version": 1,
        "job_id": f"check-{jid}",
        "engine": {"release": run.release},
        "rule_set": {"code": rs.code, "version": rs.effective_from.isoformat(), "rules": part.rules},
        "tables": {table: {"uri": st.uri(f"{prefix}in/{table}.csv"), "sha256": _sha(part.table_csv)}},
        "context": {**part.context, **({"tables": linked} if linked else {})},
        "outputs": {"uri": st.uri(prefix + "out/")},
        "caller": {"platform_check_id": str(jid), **run.caller},
    }
    if part.skip:
        spec["skip"] = {table: part.skip}
    st.put_bytes(prefix + "check.json", json.dumps(spec, indent=1, default=str).encode())
    job = jobs().create(
        id=jid,
        key=part.key,
        engine_release=run.release,
        rule_set_id=rs.pk,
        table=table,
        rows=part.rows,
        purpose=run.purpose,
        caller=run.caller,
        storage_prefix=prefix,
    )
    _dispatchers[0].submit(job)
    return jobs().get(pk=job.pk)


def wait_for(job: CheckJob, seconds: float, poll: float = 0.5) -> CheckJob:
    """The job once it has ended, or as it stands after `seconds`."""
    deadline = time.monotonic() + seconds
    while not job.ended and time.monotonic() < deadline:
        time.sleep(poll)
        job = jobs().get(pk=job.pk)
    return job


def outcome(job: CheckJob, table: str) -> dict:
    """The worker's findings for `table` ({rows, results: [{rule_id, severity, rows, actual}], notes}); CheckNotRun when it failed."""
    if job.status != CheckJob.Status.SUCCEEDED:
        why = job.error or job.get_status_display().lower()
        raise CheckNotRun(f"ReCET {job.engine_release}'s worker could not run the rules: {why}")
    try:
        found = json.loads(storage.store(AREA).get_bytes(job.storage_prefix + "out/findings.json"))
    except (storage.StorageError, ValueError) as e:
        raise CheckNotRun(f"ReCET {job.engine_release}'s worker's findings could not be read: {e}") from None
    return found.get("tables", {}).get(table) or {"rows": 0, "results": [], "notes": []}


def check_part(run: EngineRun, rs, table: str, df, *, filing_year=None, skip=(), context=None) -> dict | None:
    """One table's rules by the run's release's worker: its findings (`outcome`), or None while its job is pending (a run that does
    not wait). Raises CheckNotRun when the worker could not give them (the run's `failed` says why)."""
    part = prepare(run, rs, table, df, filing_year=filing_year, skip=skip, context=context)
    try:
        job = run.finished.get(part.key)
        if job is None:
            job = _reusable(part.key, run.release)
        if job is None:
            if run.wait and not _dispatchers[0].serves(run.release):
                raise CheckNotRun(f"No worker runs ReCET {run.release} just now, so the {table} records were not checked by its rules.")
            job = submit(run, rs, table, part)
        if not job.ended and not run.wait:
            run.pending.append(job)
            return None
        if not job.ended:
            n = wait_seconds()
            job = wait_for(job, n)
            if not job.ended:
                raise CheckNotRun(
                    f"ReCET {run.release}'s worker had not finished checking the {table} records after {n} seconds, so they were not "
                    "checked yet. Check again in a minute."
                )
        run.used.append(job)
        return outcome(job, table)
    except CheckNotRun as e:
        run.failed = run.failed or str(e)
        raise


# --- the worker's side (the Calculations module's dispatch calls these)


def spec_uri(job: CheckJob) -> str:
    return storage.store(AREA).uri(job.storage_prefix + "check.json")


def output(job: CheckJob, name: str) -> dict | None:
    try:
        return json.loads(storage.store(AREA).get_bytes(job.storage_prefix + "out/" + name))
    except (storage.StorageError, ValueError):
        return None


def submitted(job: CheckJob, backend: str, backend_job_id: str = "") -> None:
    jobs().filter(pk=job.pk).update(backend=backend, backend_job_id=backend_job_id)


def claim_next(worker: str, release: str = "") -> CheckJob | None:
    """Take the next queued local check job, oldest first (a worker that knows its release takes only that release's jobs)."""
    with transaction.atomic(using=db()):
        q = jobs().select_for_update(skip_locked=True).filter(status=CheckJob.Status.QUEUED, backend="local")
        if release:
            q = q.filter(engine_release=release)
        job = q.order_by("created_at").first()
        if job is None:
            return None
        job.status, job.worker, job.started_at = CheckJob.Status.RUNNING, worker, timezone.now()
        job.save(update_fields=["status", "worker", "started_at"])
    return job


def mark_started(job: CheckJob, worker: str) -> CheckJob:
    jobs().filter(pk=job.pk, status=CheckJob.Status.QUEUED).update(status=CheckJob.Status.RUNNING, worker=worker, started_at=timezone.now())
    return jobs().get(pk=job.pk)


def finish(job: CheckJob, *, exit_code: int | None, receipt: dict | None = None, error: str = "") -> CheckJob:
    """Record how a job ended (the worker's exit code and receipt), then tell the module that submitted it."""
    receipt = receipt or {}
    if exit_code == 0 and receipt.get("status", "succeeded") == "succeeded":
        status = CheckJob.Status.SUCCEEDED
    elif exit_code == 2:
        status = CheckJob.Status.REFUSED
    else:
        status = CheckJob.Status.FAILED
    error = "" if status == CheckJob.Status.SUCCEEDED else (receipt.get("error") or error or f"the worker ended with exit code {exit_code}")
    updated = (
        jobs()
        .filter(pk=job.pk)
        .exclude(status__in=CheckJob.ENDED)
        .update(status=status, finished_at=timezone.now(), receipt=receipt, error=error[:4000])
    )
    job = jobs().get(pk=job.pk)
    if updated:
        notify(job)
    return job


def notify(job: CheckJob) -> None:
    """Hand an ended job to the module that submitted it (its finisher); a finisher's failure is logged, never the worker's."""
    finisher = _finishers.get((job.caller or {}).get("module", ""))
    if finisher is None:
        return
    try:
        finisher(job)
    except Exception:  # noqa: BLE001 - the version stays "being checked" and its page offers Check again
        log.exception("finishing check job %s", job.pk)


def all_ended(ids) -> list[CheckJob] | None:
    """The jobs named, once every one has ended; None while any is still to run (or a job named is gone)."""
    found = list(jobs().filter(pk__in=list(ids)))
    if len(found) != len(set(ids)) or not all(j.ended for j in found):
        return None
    return found


def state(ids) -> dict:
    """A version being checked, in words, for its filing page ({ended, text})."""
    found = list(jobs().filter(pk__in=list(ids)))
    if not found:
        return {"ended": True, "text": "The check is no longer known."}
    release = found[0].engine_release
    done = sum(1 for j in found if j.ended)
    if done == len(found):
        return {"ended": True, "text": f"Checked by ReCET {release}'s worker."}
    if any(j.status == CheckJob.Status.RUNNING for j in found) or done:
        part = f" ({done} of {len(found)} tables done)" if len(found) > 1 else ""
        return {"ended": False, "text": f"Being checked by ReCET {release}'s worker{part}."}
    return {"ended": False, "text": f"Waiting for a worker of ReCET {release}."}


def expired(cutoff):
    """Check jobs that ended (or were queued) before the cutoff, for the retention job (record class "check_job"): their stored copies
    of the tables and findings, then the record."""
    for job in jobs().filter(created_at__lt=cutoff).iterator():

        def delete(job=job):
            storage.store(AREA).delete_prefix(job.storage_prefix)
            jobs().filter(pk=job.pk).delete()

        yield f"check_job:{job.pk}", delete

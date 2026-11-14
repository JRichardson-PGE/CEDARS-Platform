# ruff: noqa: F811 - fixtures imported from other test files
"""A filing's rules run by the worker of the engine release its specification version names (rules.jobs, `recet worker check`): a
claims filing is recorded as being checked, the local worker runs its check jobs and the version takes their findings, which are the
findings the rules give when run here; the pre-upload checker, examples and the impact test wait for theirs; a release older than the
first whose worker runs checks, a worker that is not running and a failed job are said, never passed over; AWS Batch gets a job with
the check command; ended jobs are purged."""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from cedars.core import storage
from cedars.modules.calculations import dispatch
from cedars.modules.claims import service as claims
from cedars.services.rules import jobs, precheck
from cedars.services.rules.models import CheckJob
from cedars.services.submissions import filing_parts
from cedars.services.submissions.models import Deadline, SubmissionVersion

from .conftest import sign_in
from .test_budgets import release  # noqa: F401 - the engine release fixture
from .test_calculations import StubBatch
from .test_claims import claim, claims_zip, deadline, filer, programs, row, tables, year  # noqa: F401 - fixtures
from .test_rules_checker import table_of
from .test_rules_wider import add, draft, rules  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
RECET = Path(sys.executable).parent / "recet"


@pytest.fixture
def here():
    from recet import __version__

    return __version__


@pytest.fixture
def worker_mode(settings, monkeypatch, here):
    """Rules run in a worker; the dispatcher queues each job for the local worker (as Calculations' does), which the test runs."""
    settings.RULES_RUN_IN_WORKER = True
    settings.CALCULATIONS_WORKER_COMMAND = f"{RECET} worker"
    served = {"now": True}
    d = jobs.Dispatcher(resolve=lambda r: r or here, submit=lambda job: jobs.submitted(job, "local"), serves=lambda r: served["now"])
    monkeypatch.setattr(jobs, "_dispatchers", [d])
    return served


@pytest.fixture
def at_once(worker_mode, monkeypatch, here):
    """A worker that runs each job the moment it is submitted (the real `recet worker check`, in this process)."""
    from recet import worker

    def submit(job):
        jobs.submitted(job, "local")
        code = worker.main(["check", jobs.spec_uri(job)])
        jobs.finish(job, exit_code=code, receipt=jobs.output(job, "receipt.json"))

    monkeypatch.setattr(jobs, "_dispatchers", [jobs.Dispatcher(resolve=lambda r: r or here, submit=submit, serves=lambda r: True)])


def wide_rules(rules, admin, rs):  # noqa: F811
    sem = add(rules, admin, rs, "Claim", "ParticipantID", "is not null", conds=[("Measure", "MeasImpactType", "in", "Cust-SEM", "")])
    big = add(rules, admin, rs, "Measure", "NumUnits", "is less than", "5", conds=[("Program", "PrgID", "equals", "'PGE_Com'", "")],
              severity="warning")  # fmt: skip
    rules.publish(admin, rs)
    rs.refresh_from_db()
    return sem, big


def the_file():
    return claims_zip(
        full=True,
        t=tables(
            claim("PGE-2028-001", MeasImpactType="Cust-SEM"),
            claim("PGE-2028-002", prg="PGE_Com", NumUnits="7"),
            ProgramCost=[row("ProgramCost", PrgID="PGE_Res"), row("ProgramCost", PrgID="PGE_Com")],
            Site=[row("Site", SiteID="PGE-S1")],
        ),
    )


def comparable(found):
    return sorted((f["table"], f["row"], f["record_id"], f["rule_id"], f["severity"], f["message"]) for f in found)


def test_a_claims_filing_is_checked_by_its_releases_worker(
    rules,
    draft,
    cpuc_admin,
    filer,
    year,
    pge,
    programs,
    worker_mode,
    settings,
    here,
    client,  # noqa: F811
):
    rs, version = draft
    sem, big = wide_rules(rules, cpuc_admin, rs)
    Deadline.objects.filter(pk=deadline().pk).update(specification_version=version)
    # the same file checked here, for the findings to compare
    settings.RULES_RUN_IN_WORKER = False
    v_here = claims.upload_claims(filer, deadline(), pge, the_file(), "q1.zip")
    expected = comparable(claims.findings(v_here))
    assert {sem.rule_id, big.rule_id} <= {f[3] for f in expected}
    settings.RULES_RUN_IN_WORKER = True

    v = claims.upload_claims(filer, deadline(), pge, the_file(), "q1.zip")
    assert v.status == SubmissionVersion.Status.DRAFT and v.findings["checking"]["release"] == here
    queued = list(CheckJob.objects.filter(status=CheckJob.Status.QUEUED))
    assert {j.table for j in queued} == {"Claim", "Measure", "ProgramCost"} and all(
        j.caller == {"module": "claims", "version": v.pk} for j in queued
    )
    assert sorted(str(j.id) for j in queued) == sorted(v.findings["checking"]["jobs"])
    assert {sem.rule_id, big.rule_id}.isdisjoint({f["rule_id"] for f in claims.findings(v)})  # the rules' findings are still to come
    spec = json.loads(storage.store("results").get_bytes(next(j for j in queued if j.table == "Claim").storage_prefix + "check.json"))
    assert spec["engine"]["release"] == here and spec["context"]["filer"] == {"PA": "PGE", "PAType": "IOU"}
    assert set(spec["context"]["tables"]) == {"Measure"} and spec["context"]["keys"]["Measure"] == ["ClaimID"]
    assert filing_parts.stage(v, has_file=True, priced=False).key == "checking"
    sign_in(client, filer)
    state = client.get(f"/filings/versions/{v.pk}/checking.json").json()
    assert state == {"ended": False, "text": f"Waiting for a worker of ReCET {here}."}

    # the local worker runs them, one by one; the last one's end checks the version again with their findings
    for _ in range(len(queued) - 1):
        assert dispatch.check_one("test-worker", here, poll=0.05)
    v.refresh_from_db()
    assert v.findings.get("checking")  # one job still to run
    assert dispatch.check_one("test-worker", here, poll=0.05)
    assert not dispatch.check_one("test-worker", here)
    v.refresh_from_db()
    assert "checking" not in v.findings
    assert comparable(claims.findings(v)) == expected  # the worker's findings are the rules' findings here
    assert all(j.status == CheckJob.Status.SUCCEEDED and j.receipt["kind"] == "check" for j in CheckJob.objects.all())
    assert client.get(f"/filings/versions/{v.pk}/checking.json").json()["ended"] is True

    # Check again with nothing changed takes the findings of the jobs that ran (same inputs, same key): no new job
    claims.check(filer, v)
    assert CheckJob.objects.count() == len(queued)
    v.refresh_from_db()
    assert "checking" not in v.findings and comparable(claims.findings(v)) == expected


def test_a_failed_check_job_rejects_the_version_until_it_is_checked_again(
    rules,
    draft,
    cpuc_admin,
    filer,
    year,
    pge,
    programs,
    worker_mode,
    settings,
    monkeypatch,
    here,  # noqa: F811
):
    rs, version = draft
    wide_rules(rules, cpuc_admin, rs)
    Deadline.objects.filter(pk=deadline().pk).update(specification_version=version)
    v = claims.upload_claims(filer, deadline(), pge, the_file(), "q1.zip")
    settings.CALCULATIONS_WORKER_COMMAND = f"{sys.executable} -c 'import sys; sys.exit(1)' worker"
    while dispatch.check_one("test-worker", here, poll=0.05):
        pass
    v.refresh_from_db()
    assert "checking" not in v.findings and v.status == SubmissionVersion.Status.DRAFT
    failed = [f for f in claims.findings(v) if f["rule_id"] == "CLM-RULES"]
    assert failed and f"ReCET {here}'s worker could not run the rules" in failed[0]["message"]
    assert CheckJob.objects.filter(status=CheckJob.Status.FAILED).count() == CheckJob.objects.count() > 1


def test_the_pre_upload_checker_waits_for_the_worker(rules, draft, cpuc_admin, at_once, settings, programs):  # noqa: F811
    rs, version = draft
    _sem, big = wide_rules(rules, cpuc_admin, rs)
    content = table_of(the_file(), "Measure")
    worker = precheck.check("claims", "Measure", content, rule_set=rs, filing_year=2028)
    assert CheckJob.objects.filter(purpose="pre-upload check", status=CheckJob.Status.SUCCEEDED).count() == 1
    settings.RULES_RUN_IN_WORKER = False
    here_ = precheck.check("claims", "Measure", content, rule_set=rs, filing_year=2028)
    assert comparable([f.__dict__ for f in worker.findings]) == comparable([f.__dict__ for f in here_.findings])
    assert big.rule_id in {f.rule_id for f in worker.findings}  # the program's record, read from the registry, went with the check


def test_no_worker_or_one_too_slow_is_said(rules, draft, cpuc_admin, worker_mode, settings, here):  # noqa: F811
    rs, _version = draft
    wide_rules(rules, cpuc_admin, rs)
    content = table_of(the_file(), "Measure")
    worker_mode["now"] = False
    result = precheck.check("claims", "Measure", content, rule_set=rs, filing_year=2028)
    assert any(f.rule_id == "CLM-RULES" and f"No worker runs ReCET {here} just now" in f.message for f in result.findings)
    assert not CheckJob.objects.exists()  # nothing queued that nobody would run
    worker_mode["now"] = True
    from cedars.services.releases.models import LimitChange

    LimitChange.objects.create(key="check_wait_seconds", value=0, changed_by=cpuc_admin, reason="test")
    result = precheck.check("claims", "Measure", content, rule_set=rs, filing_year=2028)
    assert any(f.rule_id == "CLM-RULES" and "had not finished checking" in f.message for f in result.findings)
    assert CheckJob.objects.filter(status=CheckJob.Status.QUEUED).count() == 1  # it runs when a worker comes, and is taken next time


def test_a_release_before_checks_runs_the_rules_here_and_says_so(rules, draft, cpuc_admin, settings, monkeypatch, here):  # noqa: F811
    rs, _version = draft
    wide_rules(rules, cpuc_admin, rs)
    settings.RULES_RUN_IN_WORKER = True
    monkeypatch.setattr(jobs, "_dispatchers", [jobs.Dispatcher(resolve=lambda r: "0.1.94", submit=lambda j: None, serves=lambda r: True)])
    result = precheck.check("claims", "Measure", table_of(the_file(), "Measure"), rule_set=rs, filing_year=2028)
    assert any("ReCET 0.1.94's worker cannot run rules kept as data" in n and f"ReCET {here}, ran them" in n for n in result.notes)
    assert not CheckJob.objects.exists()
    assert jobs.can_check("0.1.95") and jobs.can_check("0.2.0") and not jobs.can_check("0.1.94") and not jobs.can_check("")


def test_batch_runs_a_check_job_with_the_check_command(rules, draft, cpuc_admin, settings, monkeypatch, release, here):  # noqa: F811
    from cedars.modules.calculations.models import EngineRelease

    rs, _version = draft
    wide_rules(rules, cpuc_admin, rs)
    EngineRelease.objects.filter(pk=release.pk).update(release=here)
    settings.RULES_RUN_IN_WORKER = True
    settings.CALCULATIONS_BACKEND = "batch"
    settings.CALCULATIONS_BATCH_QUEUES = {"small": "cedars-small", "large": "cedars-large"}
    settings.CALCULATIONS_BATCH_JOB_DEFINITION = "recet-worker"
    stub = StubBatch()
    monkeypatch.setattr(dispatch.BatchBackend, "__init__", lambda self, client=None: setattr(self, "client", stub))
    monkeypatch.setattr(jobs, "wait_seconds", lambda: 0)
    result = precheck.check("claims", "Measure", table_of(the_file(), "Measure"), rule_set=rs, filing_year=2028)
    job = CheckJob.objects.get()
    sent = stub.submitted[0]
    assert sent["jobQueue"] == "cedars-small" and sent["jobDefinition"] == "recet-worker"
    assert sent["containerOverrides"]["command"] == ["recet", "worker", "check", jobs.spec_uri(job)]
    assert job.backend == "batch" and job.backend_job_id == "job-1"
    assert any(f.rule_id == "CLM-RULES" for f in result.findings)  # not waited for (0 seconds)
    stub.jobs["job-1"] = {"jobId": "job-1", "status": "SUCCEEDED", "container": {"exitCode": 0}}
    receipt = json.dumps({"status": "succeeded", "finished": "x"}).encode()
    storage.store("results").put_bytes(job.storage_prefix + "out/receipt.json", receipt)
    assert dispatch.sync_batch_checks() == 1
    job.refresh_from_db()
    assert job.status == CheckJob.Status.SUCCEEDED


def test_ended_check_jobs_are_purged(rules, draft, cpuc_admin, at_once):  # noqa: F811
    rs, _version = draft
    wide_rules(rules, cpuc_admin, rs)
    precheck.check("claims", "Measure", table_of(the_file(), "Measure"), rule_set=rs, filing_year=2028)
    job = CheckJob.objects.get()
    assert storage.store("results").exists(job.storage_prefix + "check.json")
    found = list(jobs.expired(timezone.now() + timedelta(days=1)))
    assert [ref for ref, _ in found] == [f"check_job:{job.pk}"]
    found[0][1]()
    assert not CheckJob.objects.exists() and not storage.store("results").exists(job.storage_prefix + "check.json")

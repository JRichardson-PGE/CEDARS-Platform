"""BR-12.2: job and system health for administrators (cedars/core/health.py, /admin/health/, manage.py check_health)."""

import json
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import timezone

from cedars.core import health
from cedars.modules.calculations.models import Run
from cedars.modules.impact_profiles import etrm
from cedars.modules.impact_profiles.models import EtrmSyncRun, ProcessingJob
from cedars.services.notifications.models import Notification

FIXTURE = Path(__file__).parent / "data" / "etrm_fixture.json"
pytestmark = pytest.mark.django_db


def by_code(results=None):
    return {r.code: r for r in (results or health.run_all())}


def old_run(owner, minutes, status=Run.Status.QUEUED, **fields):
    run = Run.objects.create(
        owner=owner,
        name="2028 PG&E filing",
        engine_release="0.1.80",
        acc_version="2024",
        input_hash="x" * 64,
        storage_prefix="runs/x/",
        status=status,
        **fields,
    )
    then = timezone.now() - timedelta(minutes=minutes)
    Run.objects.filter(pk=run.pk).update(created_at=then, started_at=then if status == Run.Status.RUNNING else None)
    return run


def test_every_module_registers_its_checks():
    assert {
        "calculations.stalled",
        "calculations.failed",
        "impact_profiles.stalled",
        "impact_profiles.failed",
        "impact_profiles.etrm",
        "documents.text",
        "notifications.email",
    } <= set(by_code())


def test_a_run_queued_too_long_or_running_too_long_fails_the_check(cpuc_admin):
    assert by_code()["calculations.stalled"].status == health.OK
    old_run(cpuc_admin, 10)  # not yet
    assert by_code()["calculations.stalled"].status == health.OK
    queued = old_run(cpuc_admin, 45)
    running = old_run(cpuc_admin, 180, Run.Status.RUNNING, backend="batch", backend_job_id="job-1")
    r = by_code()["calculations.stalled"]
    assert r.status == health.FAILING
    assert [i.key for i in r.items] == [f"run:{running.pk}", f"run:{queued.pk}"]
    assert "queued 45 minutes" in r.items[1].detail and "running 3 hours (batch, job-1)" in r.items[0].detail


def test_failed_runs_need_attention(cpuc_admin):
    old_run(cpuc_admin, 5, Run.Status.FAILED, error="worker exited 137\nkilled", finished_at=timezone.now())
    r = by_code()["calculations.failed"]
    assert r.status == health.ATTENTION and r.items[0].detail == "worker exited 137"


def test_profile_jobs_waiting_too_long_fail_the_check():
    job = ProcessingJob.objects.create(kind=ProcessingJob.Kind.EXPORT, queue="system")
    ProcessingJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(hours=2))
    r = by_code()["impact_profiles.stalled"]
    assert r.status == health.FAILING and "Write the profile library" in r.items[0].label


def test_the_etrm_check(settings, cpuc_admin, tmp_path):
    settings.ETRM_DATABASE_URL = settings.ETRM_FIXTURE = ""
    assert by_code()["impact_profiles.etrm"].status == health.ATTENTION  # nothing set up
    settings.ETRM_FIXTURE = str(FIXTURE)
    assert by_code()["impact_profiles.etrm"].status == health.FAILING  # set up, never succeeded
    run = etrm.sync(None, etrm.FileSource(FIXTURE))
    assert by_code()["impact_profiles.etrm"].status == health.OK
    EtrmSyncRun.objects.filter(pk=run.pk).update(finished_at=timezone.now() - timedelta(hours=40))
    r = by_code()["impact_profiles.etrm"]
    assert r.status == health.FAILING and "for 40 hours" in r.summary
    etrm.sync(None, etrm.FileSource(tmp_path / "missing.json"))
    r = by_code()["impact_profiles.etrm"]
    assert r.status == health.FAILING and "latest eTRM sync failed" in r.summary and "could not read" in r.items[0].detail


def test_a_check_that_breaks_is_shown_failing(monkeypatch):
    def broken():
        raise RuntimeError("no such table")

    monkeypatch.setattr(health, "_checks", [*health._checks, ("broken.check", broken)])
    r = by_code()["broken.check"]
    assert r.status == health.FAILING and "no such table" in r.summary


def test_check_health_writes_one_json_line_and_alerts_once_per_set_of_failures(cpuc_admin, make_user):
    make_user("someone@example.com")
    old_run(cpuc_admin, 45)
    out = StringIO()
    call_command("check_health", stdout=out)
    line = json.loads(out.getvalue())
    assert line["event"] == "cedars_health" and line["failing"] >= 1 and line["checks"]["calculations.stalled"] == "failing"
    told = Notification.objects.filter(title="Calculations: Runs waiting or running too long")
    assert list(told.values_list("user", flat=True)) == [cpuc_admin.pk]  # system administrators only
    assert "2028 PG&E filing" in told.get().body
    call_command("check_health", stdout=StringIO())
    assert told.count() == 1  # the same stalled run is not alerted again
    old_run(cpuc_admin, 50)
    call_command("check_health", stdout=StringIO())
    assert told.count() == 2  # a new one is
    call_command("check_health", "--no-alert", stdout=StringIO())
    assert told.count() == 2


def test_the_page_is_for_system_administrators(signed_in, cpuc_admin, pge_admin):
    old_run(cpuc_admin, 45)
    page = signed_in(cpuc_admin).get("/admin/health/")
    html = page.content.decode()
    assert page.status_code == 200
    assert "Failing" in html and "Runs waiting or running too long" in html and "2028 PG&amp;E filing" in html
    assert signed_in(pge_admin).get("/admin/health/").status_code == 404

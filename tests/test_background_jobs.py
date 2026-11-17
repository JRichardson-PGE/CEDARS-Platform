"""The background work Terraform runs (infra/terraform/modules/platform/jobs.tf): the always-on loops keep going when a pass fails,
and every scheduled or always-on command exists."""

import re
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command, get_commands

from cedars.modules.calculations import dispatch
from cedars.modules.impact_profiles import jobs

TERRAFORM = Path(__file__).resolve().parent.parent / "infra" / "terraform" / "modules" / "platform"


def test_every_command_terraform_runs_exists():
    text = "\n".join(p.read_text() for p in TERRAFORM.glob("*.tf"))
    commands = set(re.findall(r'"manage\.py",\s*"([a-z_]+)"', text))
    assert {"sync_etrm", "calculations_sync", "run_impact_profile_jobs", "documents_read_text", "purge_expired"} <= commands
    assert commands <= set(get_commands()), commands - set(get_commands())


@pytest.mark.django_db
def test_the_calculations_sync_loop_survives_a_failed_pass(monkeypatch):
    calls = []

    def sync():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Batch throttled the request")
        return 1

    monkeypatch.setattr(dispatch, "sync_batch", sync)
    out = StringIO()
    call_command("calculations_sync", "--loop", "--sleep", "0", "--limit", "3", stdout=out)
    assert len(calls) == 3
    assert "3 pass(es)" in out.getvalue()


@pytest.mark.django_db
def test_one_calculations_sync_without_loop(monkeypatch):
    monkeypatch.setattr(dispatch, "sync_batch", lambda: 2)
    out = StringIO()
    call_command("calculations_sync", stdout=out)
    assert "2 run(s) ended" in out.getvalue()


@pytest.mark.django_db
def test_the_profile_jobs_loop_survives_a_failed_pass(monkeypatch):
    calls = []

    def run_pending(limit=None, queue=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("server closed the connection unexpectedly")
        return 1

    monkeypatch.setattr(jobs, "run_pending", run_pending)
    out = StringIO()
    call_command("run_impact_profile_jobs", "--loop", "--sleep", "0", "--passes", "3", stdout=out)
    assert len(calls) == 3
    assert "2 jobs run" in out.getvalue()


@pytest.mark.django_db
def test_without_loop_a_failed_pass_is_raised(monkeypatch):
    def run_pending(limit=None, queue=None):
        raise RuntimeError("no database")

    monkeypatch.setattr(jobs, "run_pending", run_pending)
    with pytest.raises(RuntimeError):
        call_command("run_impact_profile_jobs", stdout=StringIO())

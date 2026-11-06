"""Rerun with different settings (ReCET UI parity PR 3, R5 and R6): New run filled in from a run, using its stored job file and optional
input files again unless others are uploaded; and deleting one's own run before retention would (R9)."""

# ruff: noqa: F811 - pytest fixtures imported by name

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from cedars.core import storage
from cedars.modules.calculations import dispatch, service
from cedars.modules.calculations.models import Run
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.retention import service as retention

from .conftest import sign_in
from .test_calculation_optional_inputs import GAS_PROFILES, RUN_PARAMETERS
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


@pytest.fixture
def web(analyst):
    c = Client()
    sign_in(c, analyst)
    return c


def original(analyst, pge, release, tmp_path, org="pge"):
    files = {"RunParameters.csv": RUN_PARAMETERS, "GasProfiles.csv": GAS_PROFILES}
    req = request(release, name="2028 claims", settings={"acc_purpose": "claims", "emissions": False})
    return service.create_run(analyst, pge if org == "pge" else None, req, job_zip(tmp_path, extra=files, name="orig.zip"))


def post(web, run, **fields):
    data = {
        "from": str(run.pk),
        "name": "2028 claims (rerun)",
        "run_for": "PGE",
        "engine": "0.1.78|recet",
        "acc_version": "2026",
        "market_effects": "0",
        "output_format": "csv.zip",
        "keep_run_parameters": "on",
        "keep_profiles_gas": "on",
        **fields,
    }
    return web.post("/calculations/new/", {k: v for k, v in data.items() if v is not None})


def test_new_run_is_filled_in_from_the_run(analyst, pge, release, tmp_path, web):
    run = original(analyst, pge, release, tmp_path)
    page = web.get(f"/calculations/new/?from={run.pk}").content.decode()
    assert 'value="2028 claims (rerun)"' in page and f'name="from" value="{run.pk}"' in page
    assert '<option value="claims" selected>' in page and '<option value="2024" selected>' in page
    assert "Use the run&#x27;s run parameters again" in page and "Use the run&#x27;s gas monthly profiles again" in page
    assert "Another job file (optional)" in page
    assert "Rerun with different settings" in web.get(f"/calculations/runs/{run.pk}/").content.decode()


def test_a_kept_inputs_acronym_keeps_its_capitals(analyst, pge, release, tmp_path, web, monkeypatch):
    """ "Use the run's CET outputs to compare with again", not "cET outputs" (the first letter of a word is lowered, not an acronym's)."""
    run = original(analyst, pge, release, tmp_path)
    monkeypatch.setattr(
        service, "reusable_inputs", lambda r: [("cet_outputs", "CET outputs to compare with"), ("run_parameters", "Run parameters")]
    )
    page = web.get(f"/calculations/new/?from={run.pk}").content.decode()
    assert "Use the run&#x27;s CET outputs to compare with again" in page and "Use the run&#x27;s run parameters again" in page


def test_a_rerun_takes_the_runs_inputs_again_with_new_settings(analyst, pge, release, tmp_path, web):
    run = original(analyst, pge, release, tmp_path)
    r = post(web, run)
    assert r.status_code == 302, r.content.decode()[:3000]
    new = Run.objects.exclude(pk=run.pk).get()
    assert new.acc_version == "2026" and new.name == "2028 claims (rerun)" and new.organisation == pge
    assert new.inputs["job_zip"]["sha256"] == run.inputs["job_zip"]["sha256"]
    for n in ("run_parameters", "profiles_gas"):
        assert new.inputs[n]["sha256"] == run.inputs[n]["sha256"]
    uploads = storage.store("uploads")
    assert uploads.get_bytes(new.inputs["job_zip"]["key"]) == uploads.get_bytes(run.inputs["job_zip"]["key"])
    assert new.input_hash != run.input_hash  # another ACC


def test_unticked_or_uploaded_inputs_are_not_taken_from_the_run(analyst, pge, release, tmp_path, web):
    run = original(analyst, pge, release, tmp_path)
    other = "PA,DiscountRateAnnual,DiscountRateAnnual_SCT,CO2Gas\nPGE,0.05,0.03,0.0053\n"
    r = post(
        web,
        run,
        keep_profiles_gas=None,
        run_parameters=SimpleUploadedFile("RunParameters.csv", other.encode()),
        job=SimpleUploadedFile("job.zip", job_zip(tmp_path, rows=5, name="five.zip").read_bytes()),
    )
    assert r.status_code == 302, r.content.decode()[:3000]
    new = Run.objects.exclude(pk=run.pk).get()
    assert new.measure_count == 5 and "profiles_gas" not in new.inputs
    assert new.inputs["run_parameters"]["sha256"] != run.inputs["run_parameters"]["sha256"]


def test_only_those_who_see_a_run_rerun_it(analyst, pge, release, tmp_path, make_user):
    run = original(analyst, pge, release, tmp_path)
    c = Client()
    sign_in(c, make_user("stranger@sce.example.org"))
    assert c.get(f"/calculations/new/?from={run.pk}").status_code == 404
    assert c.get("/calculations/new/?from=not-a-run").status_code == 404


def test_the_owner_deletes_their_own_ended_run(analyst, pge, release, worker, tmp_path, web, django_capture_on_commit_callbacks):
    run = original(analyst, pge, release, tmp_path, org=None)
    page = web.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert 'id="delete-run"' not in page  # queued: stop it first
    assert web.get(f"/calculations/runs/{run.pk}/delete")["Location"] == f"/calculations/runs/{run.pk}/"
    with pytest.raises(AccessDenied, match="Stop the run first"):
        service.delete_run(analyst, run)
    run = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    page = web.get(f"/calculations/runs/{run.pk}/").content.decode()
    head = page.split('class="head-row"')[1].split("</div>\n</div>")[0]  # Delete this run is up beside Results (Jake, 2026-11-04)
    assert f'<a class="button danger" id="delete-run" href="/calculations/runs/{run.pk}/delete">Delete this run</a>' in head
    assert 'id="delete-h"' not in page and 'name="sure"' not in page  # nothing deletes from the run page itself

    confirm = web.get(f"/calculations/runs/{run.pk}/delete")  # the button asks first
    html = confirm.content.decode()
    assert confirm.status_code == 200 and "Delete “2028 claims”?" in html and "Delete the run for good" in html and "Keep the run" in html
    assert Run.objects.filter(pk=run.pk).exists()
    r = web.post(f"/calculations/runs/{run.pk}/delete")  # not confirmed
    assert r.status_code == 302 and r["Location"] == f"/calculations/runs/{run.pk}/delete" and Run.objects.filter(pk=run.pk).exists()
    prefix = run.storage_prefix
    with django_capture_on_commit_callbacks(execute=True):
        r = web.post(f"/calculations/runs/{run.pk}/delete", {"sure": "1"})
    assert r.status_code == 302 and r["Location"] == "/calculations/runs/" and not Run.objects.filter(pk=run.pk).exists()
    assert not list(storage.store("results").list(prefix)) and not list(storage.store("uploads").list(prefix))
    e = AuditEvent.objects.get(action="calculation_run.deleted")
    assert e.before["name"] == "2028 claims" and e.actor == analyst


def test_organisation_runs_held_runs_and_others_runs_are_not_deleted(analyst, pge, release, worker, tmp_path, web, cpuc_admin):
    run = original(analyst, pge, release, tmp_path)
    run = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    assert 'id="delete-h"' not in web.get(f"/calculations/runs/{run.pk}/").content.decode()
    with pytest.raises(AccessDenied, match="Only your own runs"):
        service.delete_run(analyst, run)

    mine = original(analyst, pge, release, tmp_path, org=None)
    mine = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    with pytest.raises(AccessDenied, match="Only your own runs"):
        service.delete_run(cpuc_admin, mine)
    retention.place_hold(cpuc_admin, "scratch_run", "an inquiry", object_ref=f"calculation_run:{mine.pk}")
    with pytest.raises(AccessDenied, match="legal hold"):
        service.delete_run(analyst, mine)
    assert Run.objects.count() == 2

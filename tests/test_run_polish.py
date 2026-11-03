"""New run, the run page and the runs list as ReCET's own pages have them (ReCET UI parity, PR 7): a blank name takes the job file's
name and the ACC, an analysis run may bring its own cost-test set file (never a filing), New run lists what each registered set
prices, the run page explains its tiles and says when retention deletes the run, and the runs list gives each run's settings, run
time and an input-check tag."""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from cedars.core import storage
from cedars.modules.calculations import dispatch, service
from cedars.modules.calculations.models import Run
from cedars.services.accounts.service import AccessDenied
from cedars.services.retention import service as retention

from .conftest import sign_in
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures
from .test_cost_test_sets import registered, set_file

pytestmark = pytest.mark.django_db


def test_a_run_left_unnamed_is_named_after_its_job_file_and_acc(tmp_path, analyst, pge, release):  # noqa: F811
    assert service.run_name("2028 claims.zip", "2024") == "2028 claims · 2024 ACC"
    assert service.run_name("My draft v1.2", "2026") == "My draft v1.2 · 2026 ACC"
    assert service.run_name("", "2026") == "Job · 2026 ACC"
    path = job_zip(tmp_path, name="pge q1.zip")
    with path.open("rb") as f:
        run = service.create_run_from_upload(analyst, pge, request(release, name="  "), SimpleUploadedFile("pge q1.zip", f.read()))
    assert run.name == "pge q1 · 2024 ACC"


def test_an_analysis_run_may_bring_its_own_cost_test_set_file(tmp_path, analyst, pge, release, cpuc_admin):  # noqa: F811
    data = set_file(sid="EE-2026-mine", version="v1", tests=("TRC", "PAC", "JST"))
    upload = SimpleUploadedFile("mine.yaml", data)
    run = service.create_run(analyst, pge, request(release, cost_tests_file=upload), job_zip(tmp_path))
    f = run.inputs["cost_tests"]
    assert storage.store("uploads").get_bytes(f["key"]) == data and run.spec["inputs"]["cost_tests"]["sha256"] == f["sha256"]
    assert "cost_test_set" not in run.spec["settings"]
    assert run.cost_tests == {"id": "EE-2026-mine", "version": "v1", "status": "custom", "sha256": f["sha256"], "file": "mine.yaml"}
    c = Client()
    sign_in(c, analyst)
    page = c.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "EE-2026-mine v1 (custom file: mine.yaml)" in page
    assert "cost tests EE-2026-mine (custom file)" in c.get("/calculations/").content.decode()


@pytest.mark.parametrize(
    "kw, why",
    [
        ({"calculation": "cet-25.1"}, "CET replica"),
        ({"settings": {"filing_validation": "claims"}}, "analysis runs only"),
        ({"settings": {"acc_purpose": "claims"}}, "analysis runs only"),
        ({"settings": {"cost_test_set": "EE-2026"}}, "not both"),
    ],
)
def test_an_own_cost_test_set_file_is_refused_where_a_published_set_is_needed(
    tmp_path,
    analyst,  # noqa: F811
    pge,
    release,  # noqa: F811
    kw,
    why,
):
    upload = SimpleUploadedFile("mine.yaml", set_file())
    with pytest.raises(AccessDenied, match=why):
        service.create_run(analyst, pge, request(release, cost_tests_file=upload, **kw), job_zip(tmp_path))
    with pytest.raises(AccessDenied, match="The cost-test set file: .*id"):
        service.create_run(analyst, pge, request(release, cost_tests_file=SimpleUploadedFile("x.yaml", b"id: no\n")), job_zip(tmp_path))
    assert not Run.objects.exists()


def test_new_run_lists_what_each_cost_test_set_prices(signed_in, analyst, release, cpuc_admin):  # noqa: F811
    registered(cpuc_admin)
    registered(cpuc_admin, publish=False, sid="EE-2028", version="draft-1", years=(2028, None), tests=("TRC", "PAC", "RIM"))
    page = signed_in(analyst).get("/calculations/new/").content.decode()
    assert 'id="cost-test-sets"' in page and "EE-2026 2026-09-15" in page and "EE-2028 draft-1" in page
    assert '<strong>TRC</strong> <span class="small muted">(TRC test)</span>' in page and "RIM" in page
    assert "2028 on (until a later set starts)" in page
    assert 'name="cost_tests_file"' in page and "data-upload-progress" in page and "new_run.js" in page


def test_the_run_page_explains_its_tiles_and_says_when_the_run_is_deleted(
    tmp_path,
    worker,  # noqa: F811
    signed_in,
    analyst,  # noqa: F811
    pge,
    release,  # noqa: F811
    cpuc_admin,
):
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    c = signed_in(analyst)
    assert 'id="run-deletion"' not in c.get(f"/calculations/runs/{run.pk}/").content.decode()  # not ended: its period has not started
    dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    run.refresh_from_db()
    page = c.get(f"/calculations/runs/{run.pk}/").content.decode()
    day = timezone.localdate(run.finished_at + timedelta(days=30))
    assert f"Deleted on or after {day:%Y-%m-%d} (30 days after it ended)" in page
    if '<dl class="tiles">' in page:
        assert "What these numbers are" in page and "The measure records the run priced." in page
    retention.place_hold(cpuc_admin, "scratch_run", "audit", f"calculation_run:{run.pk}")
    assert "Kept while a legal hold stands" in c.get(f"/calculations/runs/{run.pk}/").content.decode()


def test_every_headline_tile_has_its_help():
    from cedars.modules.calculations import results

    tiles = results.headline_totals({k: 1.0 for k, _ in results.HEADLINE}, 3)
    assert len(tiles) == len(results.HEADLINE) + 1 and all(t["help"] for t in tiles)
    assert tiles[1]["help"].startswith("Net lifecycle system benefit")


def test_deletion_dates_follow_the_retention_rules(tmp_path, analyst, pge, release, cpuc_admin):  # noqa: F811
    from cedars.services.retention.models import RetentionPolicy

    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    assert service.deletion_dates([run]) == {}
    Run.objects.filter(pk=run.pk).update(status=Run.Status.SUCCEEDED, finished_at=timezone.now())
    run.refresh_from_db()
    assert service.deletion_dates([run])[run.pk].startswith("Deleted on or after")
    retention.set_period(cpuc_admin, RetentionPolicy.objects.get(record_class="scratch_run"), None, "keep them")
    assert service.deletion_dates([run])[run.pk] == "Kept permanently"


def test_the_runs_list_gives_settings_run_time_and_input_checks(tmp_path, worker, signed_in, analyst, pge, release):  # noqa: F811
    service.create_run(
        analyst, pge, request(release, name="check", settings={"preflight_only": True, "include_societal": False}), job_zip(tmp_path)
    )
    dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    page = signed_in(analyst).get("/calculations/").content.decode()
    assert '<span class="status info">Input check</span>' in page and "SCT off · csv.zip" in page
    assert '<th scope="col">Run time</th>' in page and "Deleted on or after" in page

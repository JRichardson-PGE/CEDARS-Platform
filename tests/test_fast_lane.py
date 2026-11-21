# ruff: noqa: F811 - the fixtures imported from test_calculations are parameters here
"""The fast lane for small check jobs and runs (dispatch.py) and the workers' shared reference folder (reference_folder.py)."""

import hashlib
import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

from cedars.modules.calculations import dispatch, reference_folder, service
from cedars.modules.calculations import health as calc_health
from cedars.modules.calculations.models import Run, Worker
from cedars.services.refdata.models import PackVersion
from cedars.services.releases import service as releases
from cedars.services.rules.models import CheckJob

from .test_calculations import FAKE_WORKER, StubBatch, analyst, job_zip, release, request  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_folder_state():
    """What a reference folder sync found is kept in the cache (in memory in tests): none before or after each test."""
    cache.delete(reference_folder.STATE_KEY)
    yield
    cache.delete(reference_folder.STATE_KEY)


@pytest.fixture
def batch(settings, monkeypatch):
    settings.CALCULATIONS_BACKEND = "batch"
    settings.CALCULATIONS_BATCH_QUEUES = {"small": "cedars-small", "large": "cedars-large"}
    stub = StubBatch()
    monkeypatch.setattr(dispatch.BatchBackend, "__init__", lambda self, client=None: setattr(self, "client", stub))
    return stub


@pytest.fixture
def fast(batch, settings):
    settings.CALCULATIONS_FAST_LANE = True
    settings.CALCULATIONS_FAST_LANE_RECORDS = 5
    return batch


def ready_worker(release="0.1.78", seen=None, name="fast:1"):
    return Worker.objects.create(name=name, release=release, lanes=["small", "large", dispatch.FAST], last_seen_at=seen or timezone.now())


def check_job(rows=3, release="0.1.78", **kw):
    return CheckJob.objects.create(
        id=uuid.uuid4(), key="k", engine_release=release, table="Measure", rows=rows, storage_prefix="checks/x/", **kw
    )


# --- where work goes


def test_work_goes_to_batch_unless_the_fast_lane_is_on_ready_and_the_work_small(fast, settings, cpuc_admin):
    assert dispatch.backend_for(3, "0.1.78") == "batch"  # no fast worker is ready
    ready_worker()
    assert dispatch.backend_for(3, "0.1.78") == "local" and dispatch.backend_for(5, "0.1.78") == "local"
    assert dispatch.backend_for(6, "0.1.78") == "batch"  # over the limit
    assert dispatch.backend_for(3, "0.1.99") == "batch"  # no fast worker runs that release
    releases.set_limit(cpuc_admin, "fast_lane_records", 10, "bigger jobs start at once")
    assert dispatch.fast_records() == 10 and dispatch.backend_for(6, "0.1.78") == "local"
    settings.CALCULATIONS_FAST_LANE = False
    assert dispatch.backend_for(3, "0.1.78") == "batch"


def test_a_fast_worker_not_seen_lately_or_still_warming_up_is_not_ready(fast):
    ready_worker(seen=timezone.now() - timedelta(minutes=5))
    Worker.objects.create(name="warming:2", release="0.1.78", lanes=["small", "large"], last_seen_at=timezone.now())
    assert not dispatch.fast_ready("0.1.78") and dispatch.backend_for(1, "0.1.78") == "batch"


def test_the_local_backend_never_uses_the_fast_lane(settings):
    settings.CALCULATIONS_FAST_LANE = True
    ready_worker()
    assert dispatch.backend_for(1, "0.1.78") == "local" and not dispatch.fast_lane_on()


def test_a_small_run_goes_to_the_fast_lane_and_a_large_one_to_batch(
    tmp_path,
    fast,
    analyst,
    pge,
    release,
    django_capture_on_commit_callbacks,  # noqa: F811
):
    ready_worker()
    with django_capture_on_commit_callbacks(execute=True):
        small = service.create_run(analyst, pge, request(release), job_zip(tmp_path, rows=3))
        large = service.create_run(analyst, pge, request(release, name="big"), job_zip(tmp_path, rows=8, name="big.zip"))
    assert small.backend == "local" and large.backend == "batch"
    assert [s["jobName"] for s in fast.submitted] == [large.job_id]
    assert dispatch.claim_next("fast:1", release="0.1.78").pk == small.pk


def test_a_check_job_goes_by_its_rows(fast, release):  # noqa: F811
    ready_worker()
    small, large = check_job(rows=4), check_job(rows=50)
    dispatch.submit_check(small)
    dispatch.submit_check(large)
    small.refresh_from_db()
    large.refresh_from_db()
    assert small.backend == "local" and not small.backend_job_id
    assert large.backend == "batch" and large.backend_job_id == "job-1"


# --- the fast worker


@pytest.fixture
def fast_worker(settings, tmp_path, monkeypatch):
    settings.CALCULATIONS_WORKER_COMMAND = [sys.executable, str(FAKE_WORKER)]
    settings.CALCULATIONS_FAST_READY_FILE = str(tmp_path / "ready")
    settings.CALCULATIONS_REFERENCE_FOLDER = str(tmp_path / "ref")
    monkeypatch.delenv("RECET_DATA", raising=False)
    monkeypatch.setenv("FAKE_RECET_WARM_LOG", str(tmp_path / "warmed"))
    return tmp_path


def reference_with(folder: Path, *accs):
    files = {}
    for acc in accs:
        f = folder / "acc" / acc / "electric_hourly.parquet"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"acc " + acc.encode())
        files[f"acc/{acc}/electric_hourly.parquet"] = {"size": f.stat().st_size, "sha256": "0" * 64, "acc_version": acc}
    (folder / "manifest.json").write_text(json.dumps({"format": 2, "files": files}))


def test_the_fast_worker_warms_up_on_each_acc_before_it_says_it_is_ready(fast_worker, tmp_path, monkeypatch):
    reference_with(tmp_path / "ref", "2024", "2026")
    dispatch.work(once=True, fast=True)
    assert (tmp_path / "warmed").read_text().split() == ["2024", "2026"]
    w = Worker.objects.get()
    assert dispatch.FAST in w.lanes and (tmp_path / "ready").exists() and dispatch.fast_ready("0.1.78")


def test_a_fast_worker_that_cannot_warm_up_takes_no_work_and_is_not_ready(fast_worker, tmp_path, monkeypatch):
    dispatch.work(once=True, fast=True)  # nothing in the reference folder yet
    assert not (tmp_path / "warmed").exists() and not dispatch.fast_ready("0.1.78")
    reference_with(tmp_path / "ref", "2024")
    monkeypatch.setenv("FAKE_RECET_WARM_EXIT", "1")
    Worker.objects.all().delete()
    dispatch.work(once=True, fast=True)
    w = Worker.objects.get()
    assert dispatch.FAST not in w.lanes and not (tmp_path / "ready").exists()


def test_the_fast_worker_prices_the_runs_sent_to_it(
    tmp_path,
    fast,
    fast_worker,
    analyst,
    pge,
    release,
    django_capture_on_commit_callbacks,  # noqa: F811
):
    reference_with(tmp_path / "ref", "2024")
    ready_worker()  # another fast worker is ready, so the run goes to the fast lane
    with django_capture_on_commit_callbacks(execute=True):
        run = service.create_run(analyst, pge, request(release), job_zip(tmp_path, rows=2))
    assert run.backend == "local"
    assert dispatch.work(once=True, fast=True, poll=0.05) == 1
    run.refresh_from_db()
    assert run.status == Run.Status.SUCCEEDED and not fast.submitted


# --- calculations_sync's fallback


def test_queued_fast_lane_work_goes_to_batch_when_no_fast_worker_is_ready(
    tmp_path,
    fast,
    analyst,
    pge,
    release,  # noqa: F811
):
    w = ready_worker()
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    job = check_job()
    dispatch.submit_check(job)
    assert run.backend == "local" and dispatch.hand_over_fast_work() == 0  # its worker is still ready
    Worker.objects.filter(pk=w.pk).update(last_seen_at=timezone.now() - timedelta(minutes=5))
    assert dispatch.hand_over_fast_work() == 2
    run.refresh_from_db()
    job.refresh_from_db()
    assert run.backend == "batch" and run.backend_job_id and run.status == Run.Status.QUEUED
    assert job.backend == "batch" and job.backend_job_id
    assert sorted(s["jobQueue"] for s in fast.submitted) == ["cedars-small", "cedars-small"]


def test_work_left_running_by_a_fast_worker_that_stopped_is_failed(tmp_path, fast, analyst, pge, release):  # noqa: F811
    ready_worker()
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    run = dispatch.claim_next("fast:gone", release="0.1.78")
    job = check_job(backend="local", status=CheckJob.Status.RUNNING, worker="fast:gone")
    assert dispatch.hand_over_fast_work() == 2
    run.refresh_from_db()
    job.refresh_from_db()
    assert run.status == Run.Status.FAILED and "stopped before it finished" in run.error
    assert job.status == CheckJob.Status.FAILED and "Check again" in job.error


def test_the_fallback_does_nothing_on_the_local_backend(tmp_path, analyst, pge, release):  # noqa: F811
    service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    assert dispatch.hand_over_fast_work() == 0


def test_calculations_sync_runs_every_ten_seconds_by_default(batch, capsys):
    call_command("calculations_sync")
    assert "0 fast-lane job(s) handed over" in capsys.readouterr().out
    from cedars.modules.calculations.management.commands.calculations_sync import Command

    parser = Command().create_parser("manage.py", "calculations_sync")
    assert parser.parse_args(["--loop"]).sleep == 10.0


# --- the shared reference folder


def pack(tmp_path, kind, label, files: dict, status=PackVersion.Status.PUBLISHED, acc_year=None, when=0):
    src = tmp_path / "packs" / f"{label}-{when}"
    manifest = {}
    for rel, data in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
        manifest[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "what": "test"}
    now = timezone.now() + timedelta(minutes=when)
    return PackVersion.objects.create(
        kind=kind,
        label=label,
        version=str(when),
        acc_year=acc_year,
        manifest=manifest,
        fingerprint=str(when),
        storage_uri=src.as_uri(),
        status=status,
        published_at=now if status == PackVersion.Status.PUBLISHED else None,
    )


ACC = PackVersion.Kind.ACC_ELECTRIC
PROFILES = PackVersion.Kind.IMPACT_PROFILES


def test_the_reference_folder_holds_the_files_a_run_would_be_pinned_to(tmp_path, settings):
    dest = tmp_path / "ref"
    settings.CALCULATIONS_REFERENCE_FOLDER = str(dest)
    pack(tmp_path, ACC, "2024 ACC", {"acc/2024/electric_hourly.parquet": b"old 2024"}, acc_year=2024, when=0)
    pack(tmp_path, ACC, "2024 ACC", {"acc/2024/electric_hourly.parquet": b"new 2024"}, acc_year=2024, when=1)
    pack(
        tmp_path,
        ACC,
        "2026 ACC",
        {"acc/2026/electric_hourly.parquet": b"draft 2026", "impact_profiles/other.parquet": b"x"},
        acc_year=2026,
        status=PackVersion.Status.DRAFT,
    )
    pack(tmp_path, PROFILES, "Profiles", {"library.parquet": b"profiles"})  # registered from its own folder
    assert set(reference_folder.behind()) == {
        "acc/2024/electric_hourly.parquet",
        "acc/2026/electric_hourly.parquet",
        "impact_profiles/library.parquet",
    }
    done = reference_folder.sync()
    assert sorted(done["copied"]) == sorted(reference_folder.wanted()) and not done["failed"]
    assert (dest / "acc/2024/electric_hourly.parquet").read_bytes() == b"new 2024"  # the newest published pack
    assert not list(dest.rglob("other.parquet"))  # another kind's file in a whole-folder pack
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["format"] == 2 and manifest["pack"]["type"] == "reference"
    entry = manifest["files"]["acc/2026/electric_hourly.parquet"]
    assert entry["draft"] is True and entry["acc_version"] == "2026" and entry["size"] == len(b"draft 2026")
    assert entry["sha256"] == hashlib.sha256(b"draft 2026").hexdigest()
    assert manifest["files"]["acc/2024/electric_hourly.parquet"]["draft"] is False
    assert reference_folder.behind() == [] and reference_folder.accs() == ["2024", "2026"]
    assert reference_folder.state()["behind"] == [] and calc_health.reference_folder_health().status == "ok"
    # nothing new: nothing copied, the manifest left as it was
    assert reference_folder.sync()["copied"] == [] and json.loads((dest / "manifest.json").read_text()) == manifest
    # the 2026 ACC published: copied again with draft off; a withdrawn pack's files taken out
    PackVersion.objects.filter(acc_year=2026).update(status=PackVersion.Status.PUBLISHED, published_at=timezone.now())
    PackVersion.objects.filter(kind=PROFILES).update(status=PackVersion.Status.WITHDRAWN)
    done = reference_folder.sync()
    assert done["removed"] == ["impact_profiles/library.parquet"] and not (dest / "impact_profiles/library.parquet").exists()
    assert json.loads((dest / "manifest.json").read_text())["files"]["acc/2026/electric_hourly.parquet"]["draft"] is False


def test_files_put_in_the_folder_by_hand_are_kept_until_a_pack_replaces_them(tmp_path, settings):
    dest = tmp_path / "ref"
    settings.CALCULATIONS_REFERENCE_FOLDER = str(dest)
    reference_with(dest, "2024", "2026")  # a folder built with `recet data build`, copied in
    pack(tmp_path, ACC, "2026 ACC", {"acc/2026/electric_hourly.parquet": b"from the pack"}, acc_year=2026)
    done = reference_folder.sync()
    assert done["copied"] == ["acc/2026/electric_hourly.parquet"] and done["removed"] == []
    assert (dest / "acc/2024/electric_hourly.parquet").read_bytes() == b"acc 2024"
    files = json.loads((dest / "manifest.json").read_text())["files"]
    assert set(files) == {"acc/2024/electric_hourly.parquet", "acc/2026/electric_hourly.parquet"}
    assert files["acc/2026/electric_hourly.parquet"]["version"] == "2026 ACC 0"


def test_a_pack_file_with_another_sha256_is_not_copied(tmp_path, settings):
    dest = tmp_path / "ref"
    settings.CALCULATIONS_REFERENCE_FOLDER = str(dest)
    pack(tmp_path, ACC, "2024 ACC", {"acc/2024/electric_hourly.parquet": b"good"}, acc_year=2024)
    (tmp_path / "packs" / "2024 ACC-0" / "acc/2024/electric_hourly.parquet").write_bytes(b"changed")
    done = reference_folder.sync()
    assert list(done["failed"]) == ["acc/2024/electric_hourly.parquet"] and "SHA-256" in done["failed"]["acc/2024/electric_hourly.parquet"]
    assert not (dest / "acc/2024/electric_hourly.parquet").exists() and not list(dest.rglob("*.part"))
    result = calc_health.reference_folder_health()
    assert result.status == "attention" and result.items[0].label == "acc/2024/electric_hourly.parquet"


# --- health


def test_the_health_page_says_whether_a_fast_worker_is_ready(fast, release):  # noqa: F811
    assert calc_health.fast_lane().status == "attention"
    ready_worker()
    result = calc_health.fast_lane()
    assert result.status == "ok" and "up to 5 records" in result.summary


def test_no_reference_folder_and_no_fast_lane_are_fine(settings):
    assert calc_health.fast_lane().status == "ok" and calc_health.reference_folder_health().status == "ok"

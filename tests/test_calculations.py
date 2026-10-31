import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from cedars.core import storage
from cedars.modules.calculations import dispatch, jobfiles, service
from cedars.modules.calculations.models import Run, Worker
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry

pytestmark = pytest.mark.django_db
DATA = Path(__file__).parent / "data"
FAKE_WORKER = Path(__file__).parent / "fake_recet_worker.py"
HEADER = "CEInputID|PrgID|ClaimYearQuarter|NumUnits"


def job_zip(tmp_path, rows=3, year=2028, extra: dict | None = None, name="job.zip", sep="|") -> Path:
    lines = [HEADER.replace("|", sep)] + [sep.join([f"M{i}", "PGE_Res_HVAC", f"{year + (i % 2)}Q1", "1"]) for i in range(rows)]
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Measure.csv", "\n".join(lines) + "\n")
        z.writestr("ProgramCost.csv", "PrgID|Year|AdminCostsOverhead\nPGE_Res_HVAC|2028|1000\n")
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return path


@pytest.fixture
def worker(settings):
    settings.CALCULATIONS_WORKER_COMMAND = [sys.executable, str(FAKE_WORKER)]


@pytest.fixture
def release(cpuc_admin):
    return service.add_release(
        cpuc_admin,
        release="0.1.78",
        calculations=["recet", "cet-25.1"],
        accs={"recet": ["2021", "2022", "2024", "2026"], "cet-25.1": ["2024", "2026"]},
        batch_job_definition="recet-worker-0-1-78",
    )


@pytest.fixture
def analyst(make_user, pge, pge_admin):
    u = make_user("analyst@pge.com", "Ana Lyst")
    access.grant(pge_admin, u, pge, "calculations.submit")
    return u


def request(release, **kw):
    kw.setdefault("name", "2028 claims")
    kw.setdefault("calculation", "recet")
    kw.setdefault("acc_version", "2024")
    return service.RunRequest(release=release, **kw)


# --- the upload


def test_a_job_zip_is_read_once_for_its_records_and_first_year(tmp_path):
    info = jobfiles.inspect(job_zip(tmp_path, rows=5, year=2029))
    assert (info.measures, info.programcost, info.records, info.first_year) == ("Measure.csv", "ProgramCost.csv", 5, 2029)
    info = jobfiles.inspect(job_zip(tmp_path, rows=2, sep=",", name="comma.zip"))
    assert info.records == 2 and info.first_year == 2028


def test_a_job_zip_holds_only_the_job(tmp_path):
    with pytest.raises(jobfiles.JobFileError, match="only Measure.csv and ProgramCost.csv; it also has notes.docx"):
        jobfiles.inspect(job_zip(tmp_path, extra={"notes.docx": "x"}))
    assert jobfiles.inspect(job_zip(tmp_path, extra={"__MACOSX/._Measure.csv": "x", ".DS_Store": "x"}, name="mac.zip")).records == 3
    bad = tmp_path / "no.zip"
    bad.write_bytes(b"CEInputID|PrgID\n")
    with pytest.raises(jobfiles.JobFileError, match="not a valid .zip"):
        jobfiles.inspect(bad)
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("ProgramCost.csv", "x")
    with pytest.raises(jobfiles.JobFileError, match="no Measure.csv"):
        jobfiles.inspect(empty)


# --- starting a run


def test_a_run_stores_its_inputs_pins_them_and_queues(tmp_path, analyst, pge, release, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        run = service.create_run(analyst, pge, request(release, settings={"acc_purpose": "claims", "emissions": False}), job_zip(tmp_path))
    assert run.status == Run.Status.QUEUED and run.measure_count == 3 and run.lane == Run.Lane.SMALL
    store, uploads = storage.store("results"), storage.store("uploads")
    zipped = uploads.get_bytes(run.inputs["job_zip"]["key"])
    assert run.inputs["job_zip"]["sha256"] == storage.sha256_of(job_zip(tmp_path)) and zipped == job_zip(tmp_path).read_bytes()
    spec = json.loads(store.get_bytes(run.storage_prefix + "job.json"))
    assert spec == run.spec
    assert spec["spec_version"] == 1 and spec["job_id"] == f"run-{run.id}"
    assert spec["engine"] == {"release": "0.1.78", "calculation": "recet"}
    assert spec["inputs"]["job_zip"] == {"uri": uploads.uri(run.inputs["job_zip"]["key"]), "sha256": run.inputs["job_zip"]["sha256"]}
    assert "/uploads/" in spec["inputs"]["job_zip"]["uri"] and "/results/" in spec["outputs"]["uri"]
    assert spec["settings"] == {"acc_version": "2024", "acc_purpose": "claims", "emissions": False, "programs_replace": True}
    assert spec["outputs"]["uri"].endswith(f"runs/{run.id}/out/") and spec["outputs"]["format"] == "csv.zip"
    assert spec["context"] == {"platform_run_id": str(run.id), "organisation": "PGE", "input_hash": run.input_hash}
    e = AuditEvent.objects.get(action="calculation_run.queued")
    assert e.organisation == pge and e.after["records"] == 3


def test_identical_jobs_share_an_input_hash_and_any_change_breaks_it(tmp_path, analyst, pge, release):
    a = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    b = service.create_run(analyst, pge, request(release, name="again"), job_zip(tmp_path))
    c = service.create_run(analyst, pge, request(release, acc_version="2026"), job_zip(tmp_path))
    d = service.create_run(analyst, pge, request(release), job_zip(tmp_path, rows=4, name="other.zip"))
    assert a.input_hash == b.input_hash and len({a.input_hash, c.input_hash, d.input_hash}) == 3


def test_large_jobs_take_the_large_queue_and_the_cap_refuses_more(tmp_path, settings, analyst, pge, release):
    settings.CALCULATIONS_SMALL_RUN_RECORDS = 2
    assert service.create_run(analyst, pge, request(release), job_zip(tmp_path)).lane == Run.Lane.LARGE
    settings.CALCULATIONS_MAX_RECORDS = 2
    with pytest.raises(AccessDenied, match="3 measure records; a run takes at most 2"):
        service.create_run(analyst, pge, request(release), job_zip(tmp_path))


def test_the_request_is_checked_against_the_release(tmp_path, analyst, pge, release):
    with pytest.raises(AccessDenied, match="has no 2021 ACC"):
        service.create_run(analyst, pge, request(release, calculation="cet-25.1", acc_version="2021"), job_zip(tmp_path))
    with pytest.raises(AccessDenied, match="does not offer CET 21.0 replica"):
        service.create_run(analyst, pge, request(release, calculation="cet-21.0"), job_zip(tmp_path))
    with pytest.raises(AccessDenied, match="does not take statewide allocation"):
        service.create_run(analyst, pge, request(release, calculation="cet-25.1", settings={"allocate_statewide": True}), job_zip(tmp_path))
    with pytest.raises(AccessDenied, match="Not run settings: out_dir"):
        service.create_run(analyst, pge, request(release, settings={"out_dir": "/tmp"}), job_zip(tmp_path))


def test_starting_a_run_for_an_organisation_needs_calculations_submit(tmp_path, make_user, pge, pge_admin, release):
    reader = make_user("reader@pge.com")
    access.grant(pge_admin, reader, pge, "calculations.read")
    with pytest.raises(AccessDenied, match="calculations.submit in PGE"):
        service.create_run(reader, pge, request(release), job_zip(tmp_path))
    own = service.create_run(reader, None, request(release, name="mine"), job_zip(tmp_path))  # a run of one's own needs nothing
    assert own.organisation is None and own.spec["context"]["organisation"] is None


def test_who_sees_a_run(tmp_path, make_user, analyst, pge, pge_admin, cpuc_admin, release):
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    mine = service.create_run(analyst, None, request(release, name="mine"), job_zip(tmp_path))
    colleague = make_user("colleague@pge.com")
    access.grant(pge_admin, colleague, pge, "calculations.read")
    sce = access.create_organisation(cpuc_admin, code="SCE", name="SCE", kind="pa", pa_type="IOU")
    other = make_user("other@sce.com")
    access.grant(cpuc_admin, other, sce, "org.admin")
    assert set(service.visible_runs(analyst)) == {run, mine}
    assert set(service.visible_runs(colleague)) == {run}
    assert service.can_read(colleague, run) and not service.can_read(colleague, mine) and not service.can_read(other, run)
    # the run stays with PG&E when its owner leaves
    access.end_membership(pge_admin, analyst.memberships.get(), "left")
    assert set(service.visible_runs(analyst)) == {mine} and service.can_read(colleague, run)


def test_statewide_allocation_pins_the_version_used(tmp_path, analyst, pge, cpuc_admin, release):
    content = (DATA / "statewide_factors_20260918.csv").read_bytes()
    with pytest.raises(AccessDenied, match="No confirmed statewide allocation version has factors for 2028"):
        service.create_run(analyst, pge, request(release, settings={"allocate_statewide": True}), job_zip(tmp_path))
    draft = registry.import_allocation_csv(cpuc_admin, content, label="2028 TUAL draft", purpose="filing")
    with pytest.raises(AccessDenied, match="is a draft"):
        service.create_run(
            analyst, pge, request(release, settings={"allocate_statewide": True}, allocation_version=draft), job_zip(tmp_path)
        )
    registry.confirm_allocation(cpuc_admin, draft)
    run = service.create_run(analyst, pge, request(release, settings={"allocate_statewide": True}), job_zip(tmp_path))
    factors = registry.allocation_csv(draft).encode()
    assert run.allocation_version == draft
    assert storage.store("uploads").get_bytes(run.inputs["sw_factors"]["key"]) == factors
    assert run.spec["allocation_version"] == run.inputs["sw_factors"]["sha256"] == storage.hashlib.sha256(factors).hexdigest()
    assert run.spec["inputs"]["sw_factors"]["sha256"] == run.spec["allocation_version"]
    assert run.spec["settings"]["sw_factors_replace"] is True  # exactly the version's table, no built-in rows
    assert run.spec["context"]["allocation_version"] == "2028 TUAL draft"


# --- the registry's programs in every run (BR-13.1)


def _registry_programs(cpuc_admin, pge):
    from datetime import date

    hvac = registry.create_program(
        cpuc_admin,
        prg_id="PGE_Res_HVAC",
        administrator=pge,
        valid_from=date(2024, 1, 1),
        name="Residential HVAC",
        status="active",
        segment="resource_acquisition",
        primary_sector="Residential",
        sectors={"Residential": 0.75, "Commercial": 0.25},
        start_year=2024,
        flags={"Resource_Flag": "True", "Deemed_Flag": "True"},
    )
    registry.change_program(cpuc_admin, hvac, effective=date(2029, 1, 1), reason="renamed", name="Home Comfort", exclude_from_ce=True)
    later = date(2030, 1, 1)
    registry.create_program(cpuc_admin, prg_id="SW_HVAC_Up", administrator=None, statewide=True, valid_from=later, name="SW HVAC")
    return hvac


def test_a_run_carries_the_registrys_programs_for_its_program_year(tmp_path, analyst, pge, cpuc_admin, release):
    import csv

    plain = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    # an empty registry is still the whole table: the engine never uses its own copy of CEDARS' Programs list (Jake, 2026-10-31)
    assert storage.store("uploads").get_bytes(plain.inputs["programs"]["key"]).decode().count("\n") == 1
    assert plain.spec["settings"]["programs_replace"] is True and plain.spec["inputs"]["programs"]
    assert service.programs_label(plain) == "Program registry, program year 2028 (0 programs)"
    _registry_programs(cpuc_admin, pge)
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path, year=2028))
    table = storage.store("uploads").get_bytes(run.inputs["programs"]["key"])
    assert run.inputs["programs"]["sha256"] == storage.hashlib.sha256(table).hexdigest()
    assert run.spec["inputs"]["programs"]["sha256"] == run.inputs["programs"]["sha256"]
    rows = {r["PrgID"]: r for r in csv.DictReader(io.StringIO(table.decode()))}
    assert list(rows) == ["PGE_Res_HVAC", "SW_HVAC_Up"]
    hvac = rows["PGE_Res_HVAC"]
    assert (hvac["PA"], hvac["ProgramName"], hvac["Exclude_From_CE"]) == ("PGE", "Residential HVAC", "False")  # 2028's version
    assert set(hvac["BPSector"].split(", ")) == {"Residential: 0.750000", "Commercial: 0.250000"}  # CEDARS' layout (jsonb keeps no order)
    assert (hvac["Resource_Flag"], hvac["Deemed_Flag"], hvac["Upstream_Flag"]) == ("True", "True", "")
    assert (rows["SW_HVAC_Up"]["PA"], rows["SW_HVAC_Up"]["ProgramName"]) == ("SW", "SW HVAC")  # starts later: its latest version
    assert service.programs_label(run) == "Program registry, program year 2028 (2 programs)"
    assert AuditEvent.objects.get(action="calculation_run.queued", object_id=str(run.pk)).after["programs"] == service.programs_label(run)
    later = service.create_run(analyst, pge, request(release), job_zip(tmp_path, year=2029, name="2029.zip"))
    table = storage.store("uploads").get_bytes(later.inputs["programs"]["key"]).decode()
    hvac = next(r for r in csv.DictReader(io.StringIO(table)) if r["PrgID"] == "PGE_Res_HVAC")
    assert (hvac["ProgramName"], hvac["Exclude_From_CE"]) == ("Home Comfort", "True")
    assert later.input_hash != run.input_hash


def test_the_engine_reads_the_registrys_programs_over_its_own(tmp_path, analyst, pge, cpuc_admin, release):
    programs = pytest.importorskip("recet.programs")
    _registry_programs(cpuc_admin, pge)
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path, year=2029))
    path = tmp_path / "programs.csv"
    path.write_bytes(storage.store("uploads").get_bytes(run.inputs["programs"]["key"]))
    table, notes = programs.load_programs(path)
    assert any("Sector" in n for n in notes) and not any("extra columns" in n for n in notes)
    programs.set_user_programs(table, replace=run.spec["settings"]["programs_replace"])
    try:
        got = programs.lookup(["PGE_Res_HVAC", "PGE_Ag_001"])  # PGE_Ag_001 is in the engine's shipped list, not the registry
    finally:
        programs.set_user_programs(None)
    assert (got.loc[0, "Prg_ProgramName"], got.loc[0, "Prg_Exclude_From_CE"], got.loc[0, "Prg_PrimarySector"]) == (
        "Home Comfort",
        "True",
        "Residential",
    )
    assert got.loc[1, "Prg_MatchedOn"] == "(not found)"


# --- the local worker


def test_the_local_worker_prices_a_run_and_records_its_receipt(tmp_path, worker, analyst, pge, release):
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    claimed = dispatch.claim_next("test")
    assert claimed == run and claimed.status == Run.Status.RUNNING and dispatch.claim_next("test") is None
    run = dispatch.run_local(claimed, poll=0.05)
    assert run.status == Run.Status.SUCCEEDED and run.error == ""
    assert run.receipt["job_id"] == f"run-{run.id}" and run.receipt["context"]["platform_run_id"] == str(run.id)
    assert run.receipt["inputs"]["job_zip"]["sha256"] == run.inputs["job_zip"]["sha256"]
    assert run.progress["stage"] == "done"
    names = [f["name"] for f in service.output_files(run)]
    assert names == ["measure_level_run.csv", "receipt.json", "run_log.txt"]
    assert all(f["sha256"] for f in service.output_files(run) if f["name"] != "receipt.json")
    assert AuditEvent.objects.filter(action="calculation_run.succeeded", object_id=str(run.pk)).exists()


def test_a_failed_run_and_a_refused_spec_say_why(tmp_path, worker, monkeypatch, analyst, pge, release):
    monkeypatch.setenv("FAKE_RECET_EXIT", "1")
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    run = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    assert run.status == Run.Status.FAILED and "no CEInputID column" in run.error
    monkeypatch.setenv("FAKE_RECET_EXIT", "2")
    service.create_run(analyst, pge, request(release, name="refused"), job_zip(tmp_path))
    run = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    assert run.status == Run.Status.REFUSED and "engine.release 9.9.9" in run.error and run.receipt == {}


def test_small_runs_are_taken_first(tmp_path, settings, analyst, pge, release):
    settings.CALCULATIONS_SMALL_RUN_RECORDS = 3
    big = service.create_run(analyst, pge, request(release, name="big"), job_zip(tmp_path, rows=10, name="big.zip"))
    small = service.create_run(analyst, pge, request(release, name="small"), job_zip(tmp_path))
    assert (big.lane, small.lane) == (Run.Lane.LARGE, Run.Lane.SMALL)
    assert dispatch.claim_next("w1") == small and dispatch.claim_next("w2") == big


def test_stopping_a_queued_run_ends_it_and_a_running_one_ends_the_worker(tmp_path, worker, monkeypatch, analyst, pge, release):
    queued = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    queued = service.cancel_run(analyst, queued, "wrong file")
    assert queued.status == Run.Status.CANCELLED and dispatch.claim_next("test") is None
    with pytest.raises(AccessDenied, match="already ended"):
        service.cancel_run(analyst, queued)
    monkeypatch.setenv("FAKE_RECET_SLEEP", "30")
    run = service.create_run(analyst, pge, request(release, name="long"), job_zip(tmp_path))
    run = dispatch.claim_next("test")
    service.cancel_run(analyst, run)
    run = dispatch.run_local(run, poll=0.05)
    assert run.status == Run.Status.CANCELLED and run.cancelled_by == analyst


def test_only_the_owner_or_the_organisations_admins_stop_a_run(tmp_path, make_user, analyst, pge, pge_admin, release):
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    colleague = make_user("colleague@pge.com")
    access.grant(pge_admin, colleague, pge, "calculations.submit")
    with pytest.raises(AccessDenied, match="owner"):
        service.cancel_run(colleague, run)
    assert service.cancel_run(pge_admin, run).status == Run.Status.CANCELLED


def test_a_finished_filing_run_prices_its_submission_version(tmp_path, worker, analyst, pge, pge_admin, cpuc_admin, release):
    from cedars.services.submissions import service as submissions

    access.grant(pge_admin, analyst, pge, "claims.submit")
    quarter = [p for p in registry.ensure_periods(2028) if p.kind == "quarter" and p.number == 1][0]
    from django.utils import timezone

    deadline = submissions.add_deadline(
        cpuc_admin, module="claims", kind="quarterly claim", period=quarter, due_at=timezone.now() + timezone.timedelta(days=10)
    )
    v = submissions.start(analyst, deadline, pge)
    submissions.record_validation(analyst, v, None, {"records": 3, "rejected": 0, "warnings": 0})
    with pytest.raises(AccessDenied, match="only with ReCET's own calculation"):
        service.create_run(analyst, pge, request(release, calculation="cet-25.1"), job_zip(tmp_path), submission_version=v)
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path), submission_version=v)
    dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    v.refresh_from_db()
    assert v.calculation["run"] == str(run.id) and v.calculation["input_hash"] == run.input_hash
    assert v.pack_versions == {"acc/2024/electric_hourly.parquet": "f" * 64}


# --- AWS Batch


class StubBatch:
    def __init__(self):
        self.submitted, self.terminated, self.jobs = [], [], {}

    def submit_job(self, **kw):
        self.submitted.append(kw)
        return {"jobId": f"job-{len(self.submitted)}"}

    def terminate_job(self, **kw):
        self.terminated.append(kw)

    def describe_jobs(self, jobs):
        return {"jobs": [self.jobs[j] for j in jobs if j in self.jobs]}


def test_the_batch_backend_submits_to_the_runs_queue_and_syncs_from_storage(tmp_path, settings, analyst, pge, release):
    settings.CALCULATIONS_BATCH_QUEUES = {"small": "cedars-small", "large": "cedars-large"}
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path), backend="batch")
    stub = StubBatch()
    dispatch.BatchBackend(stub).submit(run)
    run.refresh_from_db()
    sent = stub.submitted[0]
    assert sent["jobQueue"] == "cedars-small" and sent["jobDefinition"] == "recet-worker-0-1-78" and run.backend_job_id == "job-1"
    assert sent["parameters"] == {"job_spec": storage.store("results").uri(run.storage_prefix + "job.json")}
    assert dispatch.claim_next("local") is None  # a batch run is never taken by a local worker
    # running: progress copied in
    stub.jobs["job-1"] = {"jobId": "job-1", "status": "RUNNING"}
    storage.store("results").put_bytes(run.storage_prefix + "out/progress.json", b'{"stage": "pricing", "fraction": 0.4}')
    assert dispatch.BatchBackend(stub).sync([run]) == 0
    run.refresh_from_db()
    assert run.status == Run.Status.RUNNING and run.progress["fraction"] == 0.4
    # ended: receipt recorded
    stub.jobs["job-1"] = {"jobId": "job-1", "status": "SUCCEEDED", "container": {"exitCode": 0}}
    storage.store("results").put_bytes(
        run.storage_prefix + "out/receipt.json", json.dumps({"status": "succeeded", "finished": "x"}).encode()
    )
    assert dispatch.BatchBackend(stub).sync([run]) == 1
    run.refresh_from_db()
    assert run.status == Run.Status.SUCCEEDED and run.receipt["status"] == "succeeded"
    dispatch.BatchBackend(stub).cancel(run)
    assert stub.terminated[0]["jobId"] == "job-1"


def test_a_batch_job_that_dies_without_a_receipt_is_a_failed_run(tmp_path, settings, analyst, pge, release):
    settings.CALCULATIONS_BATCH_QUEUES = {"small": "s", "large": "l"}
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path), backend="batch")
    stub = StubBatch()
    dispatch.BatchBackend(stub).submit(run)
    run.refresh_from_db()
    stub.jobs["job-1"] = {"jobId": "job-1", "status": "FAILED", "statusReason": "Host EC2 terminated", "container": {"exitCode": None}}
    dispatch.BatchBackend(stub).sync([run])
    run.refresh_from_db()
    assert run.status == Run.Status.FAILED and run.error == "Host EC2 terminated"


# --- estimates


def test_queue_position_and_estimates(tmp_path, analyst, pge, release):
    a = service.create_run(analyst, pge, request(release, name="a"), job_zip(tmp_path))
    b = service.create_run(analyst, pge, request(release, name="b"), job_zip(tmp_path))
    assert (service.queue_position(a), service.queue_position(b)) == (1, 2)
    est_a, est_b = service.eta(a), service.eta(b)
    assert est_a["starts_in"] == 0 and est_b["starts_in"] == est_a["done_in"] > 0
    Run.objects.filter(pk=a.pk).update(estimate_seconds=100, measure_count=1_000_000)
    assert service.estimate_seconds(Run(calculation="recet", measure_count=1_000_000, settings={})) == 410
    assert service.estimate_seconds(Run(calculation="cet-25.1", measure_count=1_000_000, settings={})) == 440


def test_estimates_learn_from_finished_runs(tmp_path, analyst, pge, release):
    from datetime import timedelta

    from django.utils import timezone

    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    now = timezone.now()
    Run.objects.filter(pk=run.pk).update(
        status="succeeded", measure_count=50_000, estimate_seconds=100, started_at=now - timedelta(seconds=200), finished_at=now
    )
    assert service.calibration() == 2.0


# --- storage


def test_storage_keys_cannot_leave_their_folder():
    for bad in ("../x", "a/../../x", "/etc/passwd", ""):
        with pytest.raises(storage.StorageError):
            storage.clean_key(bad)


def test_the_s3_store(monkeypatch):
    moto = pytest.importorskip("moto")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    with moto.mock_aws():
        import boto3

        client = boto3.client("s3")
        client.create_bucket(Bucket="cedars", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        s3 = storage.S3Store("cedars", "prod", client=client)
        s3.put_bytes("runs/1/out/a.csv", b"x,y\n")
        s3.put_bytes("runs/1/out/sub/b.csv", b"1")
        assert s3.uri("runs/1/out/") == "s3://cedars/prod/runs/1/out/"
        assert s3.get_bytes("runs/1/out/a.csv") == b"x,y\n" and s3.exists("runs/1/out/a.csv") and not s3.exists("nope")
        assert [f.key for f in s3.list("runs/1/out/")] == ["runs/1/out/a.csv", "runs/1/out/sub/b.csv"]
        assert "Signature=" in s3.download_url("runs/1/out/a.csv", "a.csv")
        with pytest.raises(storage.StorageError):
            s3.get_bytes("runs/1/missing")
        s3.delete_prefix("runs/1/")
        assert list(s3.list("runs/1/")) == []


# --- the job spec against the engine's own rules (when the engine is installed)


def test_specs_pass_the_engines_validator(tmp_path, analyst, pge, release, cpuc_admin):
    jobspec = pytest.importorskip("recet.jobspec")
    _registry_programs(cpuc_admin, pge)  # the programs table is one of the spec's inputs
    run = service.create_run(
        analyst, pge, request(release, settings={"acc_purpose": "claims", "include_societal": False}), job_zip(tmp_path)
    )
    assert "programs" in run.spec["inputs"] and jobspec.validate(run.spec) == []


# --- pages


def test_new_run_page_starts_a_run(tmp_path, signed_in, analyst, release):
    c = signed_in(analyst)
    page = c.get("/calculations/new/")
    assert page.status_code == 200 and "CET 25.1 replica (ReCET 0.1.78)" in page.content.decode()
    upload = SimpleUploadedFile("job.zip", job_zip(tmp_path).read_bytes(), content_type="application/zip")
    r = c.post(
        "/calculations/new/",
        {
            "name": "Q1 claims",
            "run_for": "PGE",
            "job": upload,
            "engine": "0.1.78|recet",
            "acc_version": "2024",
            "acc_purpose": "claims",
            "filing_validation": "claims",
            "include_societal": "on",
            "output_format": "csv.zip",
        },
    )
    run = Run.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/calculations/runs/{run.pk}/"
    assert run.settings == {"acc_purpose": "claims", "filing_validation": "claims", "include_societal": True, "emissions": False}
    html = c.get(r["Location"]).content.decode()
    assert "Q1 claims" in html and "next in line for small runs" in html and "Stop the run" in html
    assert c.get(f"/calculations/runs/{run.pk}/status.json").json()["status"] == "queued"
    assert "Q1 claims" in c.get("/calculations/").content.decode()


def test_new_run_shows_the_reason_a_run_is_refused(tmp_path, signed_in, analyst, release):
    upload = SimpleUploadedFile("job.zip", job_zip(tmp_path, extra={"x.txt": "x"}).read_bytes())
    r = signed_in(analyst).post(
        "/calculations/new/",
        {"name": "x", "run_for": "PGE", "job": upload, "engine": "0.1.78|recet", "acc_version": "2024", "output_format": "csv.zip"},
    )
    assert r.status_code == 200 and "it also has x.txt" in r.content.decode() and not Run.objects.exists()


def test_a_finished_runs_page_lists_its_files_and_serves_them(tmp_path, worker, signed_in, analyst, pge, release):
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    c = signed_in(analyst)
    html = c.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "measure_level_run.csv" in html and "Finished" in html and "TRCRatio" in html
    r = c.get(f"/calculations/runs/{run.pk}/files/measure_level_run.csv")
    assert r.status_code == 200 and b"".join(r.streaming_content) == b"CEInputID,TRCRatio\nM1,1.5\n"
    assert c.get(f"/calculations/runs/{run.pk}/files/../job.json").status_code == 404
    assert c.get(f"/calculations/runs/{run.pk}/files/progress.json").status_code == 404


def test_other_organisations_cannot_see_a_run(tmp_path, signed_in, make_user, analyst, pge, release):
    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    c = signed_in(make_user("stranger@example.com"))
    assert c.get(f"/calculations/runs/{run.pk}/").status_code == 404
    assert c.get(f"/calculations/runs/{run.pk}/status.json").status_code == 404
    assert run.name not in c.get("/calculations/").content.decode()


def test_engine_releases_are_configured_by_the_cpuc(signed_in, cpuc_admin, analyst, release):
    assert signed_in(analyst).get("/calculations/engines/").status_code == 404
    c = signed_in(cpuc_admin)
    r = c.post(
        "/calculations/engines/",
        {"action": "add", "release": "0.1.80", "calculations": "recet: 2024, 2026\ncet-25.1: 2024", "default": "on"},
    )
    assert r.status_code == 302
    new = service.default_release()
    assert new.release == "0.1.80" and new.accs == {"recet": ["2024", "2026"], "cet-25.1": ["2024"]}
    r = c.post("/calculations/engines/", {"action": "retire", "release": new.pk})
    assert "Make another release the default first" in r.content.decode()
    c.post("/calculations/engines/", {"action": "default", "release": release.pk})
    c.post("/calculations/engines/", {"action": "retire", "release": new.pk})
    new.refresh_from_db()
    assert not new.active and service.default_release() == release
    assert AuditEvent.objects.filter(action__startswith="engine_release.").count() == 4


def test_a_release_needs_recet_and_its_accs(cpuc_admin):
    with pytest.raises(AccessDenied, match="ReCET's own calculation"):
        service.add_release(cpuc_admin, release="0.1.81", calculations=["cet-25.1"], accs={"cet-25.1": ["2024"]})
    with pytest.raises(AccessDenied, match="none given for cet-24.3"):
        service.add_release(cpuc_admin, release="0.1.81", calculations=["recet", "cet-24.3"], accs={"recet": ["2024"]})


def test_uploads_held_in_memory_are_spooled(analyst, pge, release, tmp_path):
    data = job_zip(tmp_path).read_bytes()
    upload = SimpleUploadedFile("job.zip", data)
    run = service.create_run_from_upload(analyst, pge, request(release), upload)
    assert storage.store("uploads").get_bytes(run.inputs["job_zip"]["key"]) == data
    assert io.BytesIO(data).getbuffer().nbytes == run.inputs["job_zip"]["bytes"]


# --- notifications and retention


def test_the_owner_is_told_when_a_run_ends(tmp_path, worker, monkeypatch, analyst, pge, release, django_capture_on_commit_callbacks):
    from cedars.services.notifications.models import Notification

    run = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    with django_capture_on_commit_callbacks(execute=True):
        dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    n = Notification.objects.get(user=analyst)
    assert n.title == "Your run finished: 2028 claims" and n.body == "TRCRatio 1.50" and n.link == f"/calculations/runs/{run.pk}/"
    with django_capture_on_commit_callbacks(execute=True):
        service._notify_owner(Run.objects.get(pk=run.pk))  # a retried worker does not tell them twice
    assert Notification.objects.filter(user=analyst).count() == 1
    monkeypatch.setenv("FAKE_RECET_EXIT", "1")
    service.create_run(analyst, pge, request(release, name="broken"), job_zip(tmp_path))
    with django_capture_on_commit_callbacks(execute=True):
        dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    assert Notification.objects.filter(user=analyst, title="Your run failed: broken").exists()


def test_scratch_runs_expire_with_their_files_unless_a_filing_cites_them(
    tmp_path, worker, analyst, pge, release, django_capture_on_commit_callbacks
):
    from datetime import timedelta

    from django.utils import timezone

    from cedars.services.retention import service as retention

    old = service.create_run(analyst, pge, request(release, name="old"), job_zip(tmp_path))
    dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    recent = service.create_run(analyst, pge, request(release, name="recent"), job_zip(tmp_path))
    dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    Run.objects.filter(pk=old.pk).update(finished_at=timezone.now() - timedelta(days=31))
    assert "scratch_run" in retention.purgers()
    assert [ref for ref, _ in service.expired_runs(timezone.now() - timedelta(days=30))] == [f"calculation_run:{old.pk}"]
    with django_capture_on_commit_callbacks(execute=True):
        retention.purge()
    assert list(Run.objects.values_list("name", flat=True)) == ["recent"]
    for area in ("uploads", "results"):
        assert not list(storage.store(area).list(old.storage_prefix)) and list(storage.store(area).list(recent.storage_prefix))
    assert AuditEvent.objects.filter(action="calculation_run.expired", object_id=str(old.pk)).exists()


def test_s3_needs_a_bucket_per_area(settings):
    settings.CEDARS_STORAGE = {"backend": "s3", "root": "", "buckets": {"results": "cedars-results"}}
    storage.reset()
    with pytest.raises(storage.StorageError, match="CEDARS_BUCKET_UPLOADS"):
        storage.store("uploads")
    with pytest.raises(storage.StorageError, match="no storage area"):
        storage.store("elsewhere")


def test_new_run_for_only_me_is_the_users_own(tmp_path, signed_in, analyst, release):
    upload = SimpleUploadedFile("job.zip", job_zip(tmp_path).read_bytes(), content_type="application/zip")
    r = signed_in(analyst).post(
        "/calculations/new/",
        {"name": "mine", "run_for": "", "job": upload, "engine": "0.1.78|recet", "acc_version": "2024", "output_format": "csv.zip"},
    )
    run = Run.objects.get()
    assert r.status_code == 302 and run.organisation is None and run.owner == analyst


def test_the_worker_records_its_release_and_takes_only_that_releases_runs(tmp_path, worker, analyst, pge, release, cpuc_admin):
    other = service.add_release(cpuc_admin, release="0.1.99", calculations=["recet"], accs={"recet": ["2024"]})
    theirs = service.create_run(analyst, pge, request(other), job_zip(tmp_path))
    mine = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    assert dispatch.worker_release() == "0.1.78"
    assert not dispatch.serves("0.1.78") and dispatch.unserved([release, other]) == [release, other]
    assert dispatch.work(once=True, poll=0.05) == 1
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.status == Run.Status.SUCCEEDED and theirs.status == Run.Status.QUEUED  # the older run waits for a 0.1.99 worker
    w = Worker.objects.get()
    assert w.release == "0.1.78" and w.lanes == ["small", "large"] and w.runs_priced == 0 and w.last_seen_at
    assert dispatch.serves("0.1.78") and not dispatch.serves("0.1.99") and dispatch.unserved([release, other]) == [other]


def test_new_run_says_when_no_worker_runs_a_release(signed_in, analyst, release, settings):
    c = signed_in(analyst)
    assert "No worker is running ReCET 0.1.78" in c.get("/calculations/new/").content.decode()
    dispatch.heartbeat("host:1", "0.1.78")
    assert "No worker is running" not in c.get("/calculations/new/").content.decode()
    settings.CALCULATIONS_BACKEND = "batch"
    Worker.objects.all().delete()
    assert "No worker is running" not in c.get("/calculations/new/").content.decode()  # Batch starts one per run

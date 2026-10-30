import pytest
from django.test import Client

from cedars.modules.calculations import dispatch
from cedars.modules.calculations.models import Run
from cedars.services.accounts import service as access
from cedars.services.accounts import tokens
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry

from .test_calculations import DATA, analyst, job_zip, release, worker  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db


def bearer(key):
    return {"HTTP_AUTHORIZATION": f"Bearer {key}"}


@pytest.fixture
def key(analyst, pge):  # noqa: F811
    return tokens.create_token(analyst, pge, name="claims system", scopes=["calculations"], days=30)[1]


def start(key, tmp_path, **fields):
    fields.setdefault("name", "2028 claims")
    fields.setdefault("acc_version", "2024")
    with open(job_zip(tmp_path), "rb") as f:
        return Client().post("/api/v1/calculations/runs", {"job": f, **fields}, **bearer(key))


def test_start_follow_and_download_a_run(key, release, worker, tmp_path, analyst, pge):  # noqa: F811
    r = start(key, tmp_path)
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["status"] == "queued" and body["organisation"] == "PGE" and body["owner"] == "analyst@pge.com"
    assert body["engine"] == {"release": "0.1.78", "calculation": "recet"} and body["acc"] == "2024" and body["measures"] == 3
    assert body["queue_position"] == 1 and body["estimate"]["done_in"] > 0 and body["files"] == []
    assert r["Location"] == body["url"]
    run = Run.objects.get()
    assert run.settings == {"include_societal": True, "emissions": True}
    e = AuditEvent.objects.get(action="calculation_run.queued")
    assert e.assurance == "token" and e.actor == analyst and e.organisation == pge

    dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    body = Client().get(f"/api/v1/calculations/runs/{run.pk}", **bearer(key)).json()
    assert body["status"] == "succeeded" and body["receipt"]["status"] == "succeeded"
    names = {f["name"]: f for f in body["files"]}
    assert "measure_level_run.csv" in names
    got = Client().get(names["measure_level_run.csv"]["url"], **bearer(key))
    assert got.status_code == 200 and b"CEInputID" in b"".join(got.streaming_content)
    assert Client().get(f"/api/v1/calculations/runs/{run.pk}/files/../job.json", **bearer(key)).status_code == 404

    listed = Client().get("/api/v1/calculations/runs?status=succeeded", **bearer(key)).json()
    assert listed["count"] == 1 and listed["runs"][0]["id"] == str(run.pk) and listed["next"] is None


def test_the_service_refuses_what_new_run_refuses(key, release, tmp_path):  # noqa: F811
    r = start(key, tmp_path, acc_version="2030")
    assert r.status_code == 400 and "no 2030 ACC" in r.json()["detail"]
    r = start(key, tmp_path, calculation="cet-25.1", allocate_statewide="true")
    assert r.status_code == 400 and "replica does not take statewide allocation" in r.json()["detail"]
    r = start(key, tmp_path, release="9.9.9")
    assert r.status_code == 400 and "No ReCET release 9.9.9" in r.json()["detail"]
    r = Client().post("/api/v1/calculations/runs", {"name": "x", "acc_version": "2024"}, **bearer(key))
    assert r.status_code == 400 and "job" in r.json()
    assert not Run.objects.exists()


def test_a_token_reaches_only_its_organisation_and_scopes(key, release, tmp_path, analyst, pge, pge_admin, make_user, cpuc_admin):  # noqa: F811
    assert start(key, tmp_path).status_code == 201
    pge_run = Run.objects.get()
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    sce_admin, other = make_user("admin@sce.com"), make_user("analyst@sce.com")
    access.grant(cpuc_admin, sce_admin, sce, "org.admin", reason="first admin")
    access.grant(sce_admin, other, sce, "calculations.submit")
    sce_key = tokens.create_token(other, sce, name="sce", scopes=["calculations"], days=30)[1]
    assert Client().get(f"/api/v1/calculations/runs/{pge_run.pk}", **bearer(sce_key)).status_code == 404
    assert Client().get("/api/v1/calculations/runs", **bearer(sce_key)).json()["count"] == 0
    assert Client().post(f"/api/v1/calculations/runs/{pge_run.pk}/stop", **bearer(sce_key)).status_code == 404

    access.grant(pge_admin, analyst, pge, "claims.read")
    claims_key = tokens.create_token(analyst, pge, name="claims only", scopes=["claims"], days=30)[1]
    assert start(claims_key, tmp_path).status_code == 403
    assert Client().get("/api/v1/calculations/runs", **bearer(claims_key)).json()["count"] == 0
    assert Client().get("/api/v1/calculations/engines", **bearer(claims_key)).status_code == 403
    assert Client().get("/api/v1/calculations/runs").status_code in (401, 403)  # no credentials


def test_personal_runs(analyst, release, tmp_path, key):  # noqa: F811
    own = tokens.create_token(analyst, None, name="my scripts", scopes=["calculations"], days=30)[1]
    r = start(own, tmp_path)
    assert r.status_code == 201 and r.json()["organisation"] is None
    r = start(key, tmp_path, personal="true")
    assert r.status_code == 201 and r.json()["organisation"] is None
    # an organisation token also sees the caller's own runs
    assert Client().get("/api/v1/calculations/runs", **bearer(key)).json()["count"] == 2


def test_stop_a_queued_run(key, release, tmp_path):  # noqa: F811
    pk = start(key, tmp_path).json()["id"]
    r = Client().post(f"/api/v1/calculations/runs/{pk}/stop", {"reason": "wrong ACC"}, **bearer(key))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert AuditEvent.objects.get(action="calculation_run.cancelled").reason == "wrong ACC"


def test_engines_and_schema(key, release):  # noqa: F811
    body = Client().get("/api/v1/calculations/engines", **bearer(key)).json()
    assert body["default"] == "0.1.78"
    calcs = {c["id"]: c for c in body["releases"][0]["calculations"]}
    assert calcs["cet-25.1"]["accs"] == ["2024", "2026"] and "allocate_statewide" in body["settings"]
    schema = Client().get("/api/v1/schema", HTTP_ACCEPT="application/vnd.oai.openapi+json").json()
    assert {"/api/v1/calculations/runs", "/api/v1/calculations/runs/{id}", "/api/v1/calculations/engines"} <= set(schema["paths"])


def test_statewide_run_names_its_allocation_version(key, release, tmp_path, cpuc_admin):  # noqa: F811
    content = (DATA / "statewide_factors_20260918.csv").read_bytes()
    version = registry.import_allocation_csv(cpuc_admin, content, label="2028 TUAL", purpose="filing")
    registry.confirm_allocation(cpuc_admin, version)
    r = start(key, tmp_path, allocate_statewide="true", allocation_version=version.pk)
    assert r.status_code == 201, r.content
    assert r.json()["allocation_version"] == str(version) and "sw_factors" in r.json()["inputs"]
    assert start(key, tmp_path, allocate_statewide="true", allocation_version=99999).json()["detail"] == "No allocation version 99999."

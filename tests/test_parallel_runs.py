import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from cedars.core import storage
from cedars.modules.calculations import dispatch, service
from cedars.modules.calculations.models import Run
from cedars.services.accounts import tokens
from cedars.services.accounts.service import AccessDenied

from .conftest import sign_in
from .test_calculations import analyst, job_zip, release, request, worker  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
CET_OUTPUTS = b"CET_ID,TotalSystemBenefit,TRCCost,TRCRatio\nM0,10,8,1.25\nM1,20,10,2\n"


def cet_file(tmp_path, content=CET_OUTPUTS, name="job_outputs.csv"):
    p = tmp_path / name
    p.write_bytes(content)
    return p


def test_a_parallel_run_carries_the_cet_outputs_into_its_spec(analyst, pge, release, tmp_path):  # noqa: F811
    plain = service.create_run(analyst, pge, request(release), job_zip(tmp_path))
    run = service.create_run(analyst, pge, request(release, cet_outputs=cet_file(tmp_path)), job_zip(tmp_path))
    assert (
        run.inputs["cet_outputs"]["bytes"] == len(CET_OUTPUTS)
        and run.inputs["cet_outputs"]["key"] == run.storage_prefix + "cet_outputs.csv"
    )
    assert run.spec["inputs"]["cet_outputs"]["sha256"] == run.inputs["cet_outputs"]["sha256"]
    assert storage.store("uploads").open(run.inputs["cet_outputs"]["key"]).read() == CET_OUTPUTS
    assert run.input_hash != plain.input_hash
    try:
        from recet import jobspec
    except ImportError:
        pass
    else:
        assert jobspec.validate(run.spec) == []  # the engine's own name for the input


def test_what_a_parallel_run_refuses(analyst, pge, release, tmp_path):  # noqa: F811
    with pytest.raises(AccessDenied, match="replica does not take a CET outputs comparison"):
        service.create_run(analyst, pge, request(release, calculation="cet-25.1", cet_outputs=cet_file(tmp_path)), job_zip(tmp_path))
    with pytest.raises(AccessDenied, match="no CET_ID column"):
        service.create_run(analyst, pge, request(release, cet_outputs=cet_file(tmp_path, b"a,b\n1,2\n")), job_zip(tmp_path))
    with pytest.raises(AccessDenied, match="is empty"):
        service.create_run(analyst, pge, request(release, cet_outputs=cet_file(tmp_path, b"")), job_zip(tmp_path))
    piped = cet_file(tmp_path, b'"CET_ID"|"TRCCost"\nM0|1\n', "piped.csv")  # CET's pipe-delimited form, quoted names
    assert "cet_outputs" in service.create_run(analyst, pge, request(release, cet_outputs=piped), job_zip(tmp_path)).inputs
    assert Run.objects.count() == 1


def test_new_run_and_the_run_page(analyst, pge, release, worker, tmp_path):  # noqa: F811
    c = Client()
    sign_in(c, analyst)
    assert "CET outputs to compare with" in c.get("/calculations/new/").content.decode()
    r = c.post(
        "/calculations/new/",
        {
            "name": "Parallel",
            "run_for": "PGE",
            "job": SimpleUploadedFile("job.zip", job_zip(tmp_path).read_bytes()),
            "cet_outputs": SimpleUploadedFile("job_outputs.csv", CET_OUTPUTS),
            "engine": "0.1.78|recet",
            "acc_version": "2024",
            "output_format": "csv.zip",
        },
    )
    run = Run.objects.get()
    assert r.status_code == 302 and "cet_outputs" in run.inputs
    run = dispatch.run_local(dispatch.claim_next("test"), poll=0.05)
    assert "Compared with CET" not in c.get(f"/calculations/runs/{run.pk}/").content.decode()  # the (fake) worker wrote no comparison

    pytest.importorskip("recet.outputs", reason="the comparison table is read with the engine's read_table")
    out = storage.store("results")
    out.put_bytes(
        run.storage_prefix + "out/cet_comparison_summary_run.csv", b"metric,POC,CET,POC/CET\nTRCCost,1200,1000,1.2\nGasBen,5,0,\n"
    )
    out.put_bytes(run.storage_prefix + "out/cet_comparison_measures_run.csv", b"CEInputID,TRCCost_poc\nM0,1\n")
    page = c.get(f"/calculations/runs/{run.pk}/").content.decode()
    assert "Compared with CET" in page and "1,200" in page and "1.2000" in page and "Not defined (CET total 0)" in page
    assert "cet_comparison_measures_run.csv" in page


def test_the_api_takes_cet_outputs(analyst, pge, release, tmp_path):  # noqa: F811
    key = tokens.create_token(analyst, pge, name="claims system", scopes=["calculations"], days=30)[1]
    with open(job_zip(tmp_path), "rb") as job:
        r = Client().post(
            "/api/v1/calculations/runs",
            {"job": job, "cet_outputs": SimpleUploadedFile("o.csv", CET_OUTPUTS), "name": "API parallel", "acc_version": "2024"},
            HTTP_AUTHORIZATION=f"Bearer {key}",
        )
    assert r.status_code == 201, r.content
    assert "cet_outputs" in r.json()["inputs"]

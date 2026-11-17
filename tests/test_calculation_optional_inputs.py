"""ReCET's optional input files (run parameters, statewide allocation factors, program metadata, 8,760 electric profiles, gas monthly
profiles; ReCET INPUT_FIELDS section 4) on the platform: in the job zip, as sheets of the job workbook, or uploaded on their own (New run,
the API). The program registry and the allocation version stay every run's; a CET replica takes none."""

import io
import json
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from cedars.core import storage
from cedars.modules.calculations import drafts, jobfiles, service
from cedars.modules.calculations.models import Run
from cedars.services.accounts.service import AccessDenied

from .conftest import sign_in
from .test_calculation_workbooks import job_workbook
from .test_calculations import analyst, job_zip, release, request  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
RUN_PARAMETERS = "PA,DiscountRateAnnual,DiscountRateAnnual_SCT,CO2Gas\nPGE,0.07,0.03,0.0053\n"
GAS_PROFILES = "Profile,Month,Impact\n" + "".join(f"Flat,{m},{1 / 12}\n" for m in range(1, 13))
SW = "Year,ProgramID,PGEElectricFactor\n2028,SW_X,1\n"
PROGRAMS = "PA,PrgID\nPGE,PGE_Res_HVAC\n"


def with_files(tmp_path, files: dict, name="opt.zip"):
    return job_zip(tmp_path, extra=files, name=name)


def test_a_zip_may_carry_recets_optional_files_by_name(tmp_path):
    info = jobfiles.inspect(
        with_files(
            tmp_path,
            {
                "RunParameters.csv": RUN_PARAMETERS,
                "job/gas_monthly_profiles.CSV": GAS_PROFILES,
                "template_sw_allocation_factors.csv": SW,
                "Programs.csv": PROGRAMS,
            },
        )
    )
    assert info.records == 3
    assert info.optional == {
        "run_parameters": "RunParameters.csv",
        "profiles_gas": "job/gas_monthly_profiles.CSV",
        "sw_factors": "template_sw_allocation_factors.csv",
        "programs": "Programs.csv",
    }
    with pytest.raises(jobfiles.JobFileError, match="ReCET's optional input files .*it also has notes.txt"):
        jobfiles.inspect(with_files(tmp_path, {"notes.txt": "x"}, name="notes.zip"))
    with pytest.raises(jobfiles.JobFileError, match="more than one Run parameters file"):
        jobfiles.inspect(with_files(tmp_path, {"RunParameters.csv": "x", "run_parameters.parquet": "y"}, name="two.zip"))


def test_the_worker_gets_the_job_zip_and_each_optional_file_as_its_own_input(tmp_path, analyst, pge, release):  # noqa: F811
    upload = with_files(tmp_path, {"RunParameters.csv": RUN_PARAMETERS, "Profiles8760.parquet": b"PAR1fake", "Programs.csv": PROGRAMS})
    run = service.create_run(analyst, pge, request(release), upload)
    uploads = storage.store("uploads")
    assert set(run.inputs) == {"job_zip", "run_parameters", "profiles_8760", "programs"}
    assert uploads.get_bytes(run.inputs["run_parameters"]["key"]).decode() == RUN_PARAMETERS
    assert run.inputs["profiles_8760"]["key"].endswith("/profiles_8760.parquet")  # ReCET reads each by its extension
    # the job zip the worker unpacks holds only Measure.csv and ProgramCost.csv (ReCET's jobzip refuses anything else)
    with zipfile.ZipFile(io.BytesIO(uploads.get_bytes(run.inputs["job_zip"]["key"]))) as z:
        assert sorted(z.namelist()) == ["Measure.csv", "ProgramCost.csv"]
        with zipfile.ZipFile(upload) as orig:
            assert z.read("Measure.csv") == orig.read("Measure.csv")
    assert run.inputs["job_zip"]["upload_sha256"] == storage.sha256_of(upload)
    # the program registry is still the program table, and the job's own is noted as not used
    assert run.inputs["programs"]["key"].endswith("/programs.csv") and run.inputs["programs"]["programs"] >= 0
    assert uploads.get_bytes(run.inputs["programs"]["key"]).decode() != PROGRAMS
    spec = json.loads(storage.store("results").get_bytes(run.storage_prefix + "job.json"))
    assert set(spec["inputs"]) == {"job_zip", "run_parameters", "profiles_8760", "programs"}
    assert spec["settings"]["programs_replace"] is True
    assert any("Program metadata" in n and "program registry" in n for n in spec["context"]["notes"])
    # a zip without optional files is stored as uploaded
    plain = service.create_run(analyst, pge, request(release), job_zip(tmp_path, name="plain.zip"))
    assert "upload_sha256" not in plain.inputs["job_zip"] and "notes" not in plain.spec["context"]


def test_supplied_statewide_factors_never_replace_the_allocation_version(tmp_path, analyst, pge, release):  # noqa: F811
    run = service.create_run(analyst, pge, request(release), with_files(tmp_path, {"SWAllocation.csv": SW}))
    assert "sw_factors" not in run.inputs
    assert any("does not allocate" in n for n in run.spec["context"]["notes"])
    run = service.create_run(analyst, pge, request(release), job_workbook(tmp_path, sheets={"SWAllocation": [["Year"], [2028]]}))
    assert any("SWAllocation sheet" in n for n in run.spec["context"]["notes"])


def test_files_uploaded_on_their_own(tmp_path, analyst, pge, release):  # noqa: F811
    supplied = {
        "run_parameters": SimpleUploadedFile("params.csv", RUN_PARAMETERS.encode()),
        "profiles_gas": SimpleUploadedFile("gas.xlsx", b"PK fake"),
    }
    run = service.create_run(analyst, pge, request(release, supplied=supplied), job_zip(tmp_path))
    assert set(run.inputs) == {"job_zip", "run_parameters", "profiles_gas", "programs"}
    assert run.inputs["profiles_gas"]["key"].endswith("/profiles_gas.xlsx")
    # a file uploaded on its own wins over the workbook's sheet, with a note
    sheets = {"RunParameters": [["PA", "DiscountRateAnnual"], ["PGE", 0.05]]}
    supplied = {"run_parameters": SimpleUploadedFile("params.csv", RUN_PARAMETERS.encode())}
    run = service.create_run(analyst, pge, request(release, supplied=supplied), job_workbook(tmp_path, sheets=sheets))
    assert "run_parameters" in run.inputs and any("not the workbook's RunParameters sheet" in n for n in run.spec["context"]["notes"])
    # but the same file in the zip and on its own is ambiguous
    supplied = {"run_parameters": SimpleUploadedFile("params.csv", RUN_PARAMETERS.encode())}
    with pytest.raises(AccessDenied, match="Run parameters given twice"):
        service.create_run(analyst, pge, request(release, supplied=supplied), with_files(tmp_path, {"RunParameters.csv": RUN_PARAMETERS}))
    with pytest.raises(AccessDenied, match="upload a .csv, .parquet or .xlsx"):
        service.create_run(
            analyst, pge, request(release, supplied={"profiles_gas": SimpleUploadedFile("gas.docx", b"x")}), job_zip(tmp_path)
        )


def test_a_replica_takes_no_optional_file(tmp_path, analyst, pge, release):  # noqa: F811
    replica = request(release, calculation="cet-25.1")
    with pytest.raises(AccessDenied, match="cannot use the run parameters file"):
        service.create_run(analyst, pge, replica, with_files(tmp_path, {"RunParameters.csv": RUN_PARAMETERS}))
    supplied = {"profiles_8760": SimpleUploadedFile("p.csv", b"end_use_name\n")}
    with pytest.raises(AccessDenied, match="cannot use the 8,760 electric profiles file"):
        service.create_run(analyst, pge, request(release, calculation="cet-25.1", supplied=supplied), job_zip(tmp_path))
    # its program table is the registry's anyway, so a job's own Programs.csv is only noted
    run = service.create_run(analyst, pge, replica, with_files(tmp_path, {"Programs.csv": PROGRAMS}, name="p.zip"))
    assert "programs" in run.inputs and run.spec["context"]["notes"]


def test_new_run_and_the_api_take_optional_files(tmp_path, analyst, pge, release):  # noqa: F811
    c = Client()
    sign_in(c, analyst)
    page = c.get("/calculations/new/").content.decode()
    assert "Optional input files" in page and "Run parameters (optional)" in page and "#4-supplied-input-files" in page
    r = c.post(
        "/calculations/new/",
        {
            "name": "With parameters",
            "run_for": "PGE",
            "job": SimpleUploadedFile("job.zip", with_files(tmp_path, {"Programs.csv": PROGRAMS}).read_bytes()),
            "run_parameters": SimpleUploadedFile("params.csv", RUN_PARAMETERS.encode()),
            "engine": f"{release.release}|recet",
            "acc_version": "2024",
            "include_societal": "on",
            "output_format": "csv.zip",
        },
    )
    assert r.status_code == 302, r.content.decode()[:3000]
    run = Run.objects.get(name="With parameters")
    assert "run_parameters" in run.inputs
    page = c.get(r["Location"]).content.decode()
    assert "Program metadata (the file supplied) not used: the program registry" in page


def test_edit_inputs_takes_a_zip_with_optional_files(tmp_path, analyst, pge):  # noqa: F811
    pytest.importorskip("recet.editor", reason="Edit inputs uses the ReCET engine's editor code")
    d = drafts.create_from_zip(analyst, pge, "With files", with_files(tmp_path, {"RunParameters.csv": RUN_PARAMETERS}))
    assert d.measure_count == 3


def test_new_run_offers_the_engines_own_templates(tmp_path, analyst, release):  # noqa: F811
    pytest.importorskip("recet.templates", reason="the templates are the engine's own")
    from recet import templates

    c = Client()
    sign_in(c, analyst)
    page = c.get("/calculations/new/").content.decode()
    assert "Templates to download" in page
    for kind in ("workbook", "measure", "run-parameters", "profiles-8760", "profiles-gas"):
        assert f"/calculations/input-templates/{kind}" in page
    r = c.get("/calculations/input-templates/measure")
    assert r.status_code == 200 and "template_Measure_header.csv" in r["Content-Disposition"]
    assert b"".join(r.streaming_content).decode().split("\r\n")[0].split("|") == list(templates.MEASURE_COLUMNS)
    r = c.get("/calculations/input-templates/run-parameters?acc=2024")
    assert r.status_code == 200 and "template_2024_acc_run_parameters.csv" in r["Content-Disposition"]
    assert b"".join(r.streaming_content).decode().startswith("PA,")
    r = c.get("/calculations/input-templates/workbook?acc=2026")
    assert r.status_code == 200 and b"".join(r.streaming_content)[:2] == b"PK"
    assert c.get("/calculations/input-templates/sw-factors").status_code == 404  # the allocation version is every run's
    assert c.get("/calculations/input-templates/workbook?acc=1999").status_code == 404

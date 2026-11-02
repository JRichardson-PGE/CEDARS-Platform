"""A calculation job uploaded as ReCET's job workbook (.xlsx: a Measure sheet and the optional ProgramCost, RunParameters, SWAllocation,
GasProfiles, Programs and Profiles8760 sheets), as standalone ReCET takes it: New run, the API and Edit inputs take it beside the job zip,
and the worker gets it as its `workbook` input."""

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from openpyxl import Workbook

from cedars.core import storage
from cedars.modules.calculations import drafts, jobfiles, service
from cedars.modules.calculations.models import Run
from cedars.services.accounts.service import AccessDenied

from .conftest import sign_in
from .test_calculations import analyst, job_zip, release, request  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
MEASURE = ["CEInputID", "PrgID", "ClaimYearQuarter", "NumUnits", "EUL_Yrs", "RUL_Yrs"]


def job_workbook(tmp_path, rows=3, year=2028, sheets: dict | None = None, name="job.xlsx", measure_sheet="Measure"):
    """A job workbook: the Measure sheet (ClaimYearQuarter as Excel keeps it, the year as a number in some rows), a ProgramCost sheet,
    an Instructions sheet the engine ignores, and any `sheets` ({name: rows})."""
    wb = Workbook()
    wb.active.title = "Instructions"
    wb.active.append(["Fill in the Measure sheet"])
    ws = wb.create_sheet(measure_sheet)
    ws.append(MEASURE)
    for i in range(rows):
        ws.append([f"M{i}", "PGE_Res_HVAC", f"{year + (i % 2)}Q1", 1, 10, 0])
    ws.append([None] * len(MEASURE))  # a blank row is not a record
    pc = wb.create_sheet("Program Cost")  # matched ignoring case, spaces and underscores, as the engine matches it
    pc.append(["PrgID", "Year", "AdminCostsOverhead"])
    pc.append(["PGE_Res_HVAC", 2028, 1000])
    for sheet, data in (sheets or {}).items():
        s = wb.create_sheet(sheet)
        for r in data:
            s.append(r)
    path = tmp_path / name
    wb.save(path)
    return path


def test_a_workbook_is_told_from_a_zip_by_its_contents_and_read_for_its_records(tmp_path):
    info = jobfiles.inspect(job_workbook(tmp_path, rows=5, year=2029))
    assert (info.kind, info.measures, info.programcost, info.records, info.first_year) == (
        jobfiles.WORKBOOK,
        "Measure",
        "Program Cost",
        5,
        2029,
    )
    assert info.sheets == ("Measure", "ProgramCost")
    renamed = tmp_path / "job.zip"  # a workbook under another name is still a workbook
    renamed.write_bytes(job_workbook(tmp_path, rows=2).read_bytes())
    assert jobfiles.inspect(renamed).kind == jobfiles.WORKBOOK and jobfiles.inspect(renamed).records == 2
    assert jobfiles.inspect(job_zip(tmp_path)).kind == jobfiles.ZIP
    # a sheet with only its header row counts as not given, as the engine reads it
    info = jobfiles.inspect(job_workbook(tmp_path, sheets={"RunParameters": [["PA", "DiscountRateAnnual"]]}, name="hdr.xlsx"))
    assert info.sheets == ("Measure", "ProgramCost")


def test_a_workbook_without_measure_records_is_refused(tmp_path):
    with pytest.raises(jobfiles.JobFileError, match="no Measure sheet"):
        jobfiles.inspect(job_workbook(tmp_path, measure_sheet="Measures"))
    with pytest.raises(jobfiles.JobFileError, match="Measure sheet has no records"):
        jobfiles.inspect(job_workbook(tmp_path, rows=0, name="empty.xlsx"))
    with pytest.raises(jobfiles.JobFileError, match="more than one Measure sheet"):
        jobfiles.inspect(job_workbook(tmp_path, sheets={"measure ": [["CEInputID"], ["X"]]}, name="two.xlsx"))


def test_a_run_from_a_workbook_hands_the_worker_the_workbook(tmp_path, analyst, pge, release):  # noqa: F811
    book = job_workbook(tmp_path)
    upload = SimpleUploadedFile("2028 job.xlsx", book.read_bytes())
    run = service.create_run_from_upload(analyst, pge, request(release), upload)
    assert run.measure_count == 3 and "job_zip" not in run.inputs
    stored = run.inputs["workbook"]
    assert stored["key"].endswith("/job.xlsx") and stored["sha256"] == storage.sha256_of(book)
    assert storage.store("uploads").get_bytes(stored["key"]) == book.read_bytes()
    spec = json.loads(storage.store("results").get_bytes(run.storage_prefix + "job.json"))
    assert set(spec["inputs"]) == {"workbook", "programs"}
    assert spec["inputs"]["workbook"]["uri"].endswith("/job.xlsx")  # the worker reads it by its suffix
    assert service.job_input(run) == stored
    # the same job as a zip and as a workbook are different uploads, so different runs
    assert run.input_hash != service.create_run(analyst, pge, request(release), job_zip(tmp_path)).input_hash


def test_a_replica_refuses_the_sheets_it_cannot_use(tmp_path, analyst, pge, release):  # noqa: F811
    params = {"RunParameters": [["PA", "DiscountRateAnnual"], ["PGE", 0.07]]}
    with pytest.raises(AccessDenied, match="cannot use the workbook's RunParameters sheet"):
        service.create_run(analyst, pge, request(release, calculation="cet-25.1"), job_workbook(tmp_path, sheets=params))
    run = service.create_run(analyst, pge, request(release), job_workbook(tmp_path, sheets=params, name="recet.xlsx"))
    assert "workbook" in run.inputs  # ReCET's own calculation uses it, as standalone ReCET does
    run = service.create_run(analyst, pge, request(release, calculation="cet-25.1"), job_workbook(tmp_path, name="plain.xlsx"))
    assert "workbook" in run.inputs


def test_new_run_takes_a_workbook(tmp_path, analyst, pge, release):  # noqa: F811
    c = Client()
    sign_in(c, analyst)
    page = c.get("/calculations/new/").content.decode()
    assert "Job file (.zip or .xlsx)" in page and "job workbook" in page
    r = c.post(
        "/calculations/new/",
        {
            "name": "From Excel",
            "run_for": "PGE",
            "job": SimpleUploadedFile("job.xlsx", job_workbook(tmp_path).read_bytes()),
            "engine": f"{release.release}|recet",
            "acc_version": "2024",
            "include_societal": "on",
            "emissions": "on",
            "output_format": "csv.zip",
        },
    )
    assert r.status_code == 302, r.content.decode()[:2000]
    run = Run.objects.get(name="From Excel")
    assert run.measure_count == 3 and "workbook" in run.inputs


def test_edit_inputs_takes_a_workbook_and_reruns_a_workbook_run(tmp_path, analyst, pge, release):  # noqa: F811
    pytest.importorskip("recet.editor", reason="Edit inputs uses the ReCET engine's editor code")
    d = drafts.create_from_zip(analyst, pge, "From Excel", job_workbook(tmp_path), label="job.xlsx")
    m, pc = drafts.frames(d)
    assert d.measure_count == 3 and len(pc) == 1 and m.at[1, "ClaimYearQuarter"] == "2029Q1"
    run = service.create_run(analyst, pge, request(release), job_workbook(tmp_path, name="again.xlsx"))
    again = drafts.create_from_run(analyst, run)
    assert again.measure_count == 3 and again.source_run == run
    with pytest.raises(AccessDenied, match="no Measure sheet"):
        drafts.create_from_zip(analyst, pge, "Bad", job_workbook(tmp_path, measure_sheet="Sheet2", name="bad.xlsx"))

import io
import json
import zipfile
from datetime import date

import pytest
from django.core.management import call_command
from django.utils import timezone
from django.utils.encoding import iri_to_uri

from cedars.modules.documents import pda_import, studies
from cedars.modules.documents.models import Comment, Document, WorkOrder
from cedars.services.audit.models import AuditEvent
from tests.test_documents import PDF, make_people, option

pytestmark = pytest.mark.django_db

# The shapes of the PDA's API responses, as captured on 2026-10-30 (CEDARS-Research notes/pda_psr_capture_findings.md).
WORK_ORDERS = [{"contractID": 7, "contName": "Group B Sectors Year 1 Evaluation", "cpucContID": "23NC0465", "name": "An evaluation firm"}]


def opt(oid, cat):
    return {"catOptID": oid, "catOptDesc": "", "category": {"catID": cat, "catDesc": "", "catAbbrev": ""}}


DELIVERABLES = [
    {
        "deliverableID": 4210,
        "name": "PY2019 Residential HVAC Impact Evaluation Draft Report",
        "isPublished": True,
        "dateUploaded": "2021-03-03T02:15:00.000Z",  # 2021-03-02 in Pacific time, as the PDA shows it
        "pubViewStart": "2021-03-02T08:00:00.000Z",
        "pubViewEnd": "2021-03-23T07:00:00.000Z",
        "summary": "Gross and net savings of residential HVAC measures.",
        "calmacID": "CPU0213",
        "fileName": "HVAC Draft Report.pdf",
        "attachments": [
            {"delivAttachmentID": 1, "AttachmentNbr": 1, "attachmentUploaded": None, "attachment": "Appendices.xlsx"},
            {"delivAttachmentID": 2, "AttachmentNbr": 2, "attachmentUploaded": None, "attachment": "Calculator.exe"},
            {"delivAttachmentID": 3, "AttachmentNbr": 3, "attachmentUploaded": None, "attachment": "Missing appendix.pdf"},
        ],
        "options": [opt(70, 6), opt(5, 1), opt(23, 3), opt(999, 9)],
        "userDocuments": {},
        "task": {},
        "workOrder": {**WORK_ORDERS[0], "company": {"companyID": 3, "name": "An evaluation firm"}},
    },
    {
        "deliverableID": 12,
        "name": "2006-2008 Evaluation Plan",
        "isPublished": True,
        "dateUploaded": "2007-12-18T16:34:55.183Z",
        "pubViewStart": "2000-01-01T00:00:00.000Z",  # the PDA's "no date"
        "pubViewEnd": "2008-01-19T00:00:00.000Z",
        "fileName": "plan.pdf</a>",  # leftover HTML after the name
        "attachments": [],
        "options": [],
        "userDocuments": {},
        "task": {
            "taskID": 31,
            "taskName": "Task 3: HVAC",
            "contract": {"contractID": 8, "contName": "Year 2 Group D", "cpucContID": "D-Year 2"},
        },
        "workOrder": {},
    },
    {
        "deliverableID": 15,
        "name": "Reversed window",
        "isPublished": True,
        "dateUploaded": "2015-06-01T20:00:00.000Z",
        "pubViewStart": "2015-06-10T07:00:00.000Z",
        "pubViewEnd": "2015-06-09T07:00:00.000Z",
        "fileName": "notes.docx",
        "options": [opt(10, 6)],
    },
    {"deliverableID": 13, "name": "No file", "isPublished": True, "dateUploaded": "2008-01-02T00:00:00.000Z", "fileName": "gone.pdf"},
    {"deliverableID": 14, "name": "Unpublished", "isPublished": False, "fileName": "x.pdf"},
]
COMMENTS = [
    {
        "commentID": 900,
        "subject": "Baseline",
        "comment": "Which baseline year?",
        "isQuestion": True,
        "section": "",
        "createdOn": "2021-03-05T18:00:00.000Z",
        "createdBy": "A Commenter",
        "response": "2019.",
        "responseBy": "ED staff",
        "responseDate": "2021-03-10T17:00:00.000Z",
        "isActive": True,
        "attachment": "markup.pdf",
    },
    {
        "commentID": 901,
        "subject": "Deleted",
        "comment": "Never mind.",
        "isQuestion": False,
        "createdOn": "2021-03-06T18:00:00.000Z",
        "createdBy": "Someone",
        "isActive": False,
    },
]


@pytest.fixture
def capture(tmp_path):
    (tmp_path / "deliverables.json").write_text(json.dumps(DELIVERABLES))
    (tmp_path / "work_orders.json").write_text(json.dumps(WORK_ORDERS))
    (tmp_path / "comments").mkdir()
    (tmp_path / "comments" / "4210.json").write_text(json.dumps(COMMENTS))
    files = tmp_path / "files"
    for pda_id, name, data in [
        (4210, "HVAC Draft Report.pdf", PDF),
        (4210, "Appendices.xlsx", b"PK fake workbook"),
        (4210, "Calculator.exe", b"MZ program"),
        (12, "plan.pdf", PDF),
        (15, "notes.docx", b"PK fake document"),
    ]:
        (files / str(pda_id)).mkdir(parents=True, exist_ok=True)
        (files / str(pda_id) / name).write_bytes(data)
    (files / "comments" / "900").mkdir(parents=True)
    (files / "comments" / "900" / "markup.pdf").write_bytes(PDF)
    return tmp_path


def run(capture, *extra):
    out = io.StringIO()
    call_command(
        "documents_import_pda",
        "--deliverables",
        str(capture / "deliverables.json"),
        "--work-orders",
        str(capture / "work_orders.json"),
        "--comments",
        str(capture / "comments"),
        "--files",
        str(capture / "files"),
        *extra,
        stdout=out,
    )
    return out.getvalue()


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    return make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)


def test_a_dry_run_reports_and_stores_nothing(people, capture):
    out = run(capture, "--dry-run")
    assert out.startswith("Dry run: nothing stored.")
    assert "3 documents imported (5 files)" in out and "1 not published" in out
    assert "main file not found: 13" in out and "4210/Missing appendix.pdf" in out
    assert "documents with no deliverable type filed as Report: 12" in out and "999 x1" in out
    assert not Document.objects.exists() and not WorkOrder.objects.exists()


def test_the_pda_is_migrated_with_its_dates_files_and_comments(people, capture, cpuc):
    out = run(capture)
    assert "3 documents imported (5 files)" in out and "2 comments and 1 responses" in out
    doc = Document.objects.get(pda_id=4210)
    assert doc.collection == "emv" and doc.owner == cpuc and doc.document_type.label == "Draft Report"
    assert sorted(o.label for o in doc.attributes.all()) == ["Impact Evaluation", "Residential"]
    assert doc.published_at == doc.created_at and timezone.localdate(doc.published_at) == date(2021, 3, 2)
    assert doc.calmac_id == "CPU0213" and doc.task is None
    wo = doc.work_order
    assert (wo.number, wo.name, wo.cpuc_contract_id, wo.firm_name) == (
        "23NC0465",
        WORK_ORDERS[0]["contName"],
        "23NC0465",
        "An evaluation firm",
    )
    (v,) = doc.versions.all()
    assert v.published and v.reason == "Migrated from the PDA"
    assert (v.comment_period.opens, v.comment_period.closes) == (date(2021, 3, 2), date(2021, 3, 23))
    files = {f.name: f.legacy_name for f in v.files.all()}
    assert files == {"HVAC Draft Report.pdf": "", "Appendices.xlsx": "", "Calculator.exe.zip": "Calculator.exe"}
    question, deleted = Comment.objects.filter(version=v).order_by("created_at")
    assert question.kind == "question" and question.author is None and question.author_name == "A Commenter"
    assert question.created_at.date() == date(2021, 3, 5) and question.file_name == "markup.pdf"
    (r,) = question.responses.all()
    assert r.author_name == "ED staff" and r.organisation == cpuc and r.created_at.date() == date(2021, 3, 10)
    assert deleted.withdrawn_at is not None
    old = Document.objects.get(pda_id=12)
    f = old.versions.get().files.get()
    assert old.document_type.label == "Report" and (f.name, f.legacy_name) == ("plan.pdf", "plan.pdf</a>")
    period = old.versions.get().comment_period
    # no start: opens on publication; the end is a UTC midnight, which the PDA showed (and the import keeps) as the Pacific day before
    assert (period.opens, period.closes) == (date(2007, 12, 18), date(2008, 1, 18))
    assert (old.work_order.number, old.work_order.cpuc_contract_id) == ("PDA-8", "D-Year 2")  # the task's contract
    assert old.task.name == "Task 3: HVAC" and old.task.work_order == old.work_order and old.task.pda_id == 31
    reversed_window = Document.objects.get(pda_id=15).versions.get().comment_period
    assert (reversed_window.opens, reversed_window.closes) == (date(2015, 6, 9), date(2015, 6, 9))
    assert AuditEvent.objects.filter(action="documents.imported").count() == 3

    again = run(capture)
    assert "0 documents imported" in again and "3 already imported" in again
    assert Document.objects.count() == 3


def test_a_refused_type_is_kept_inside_a_zip(people, capture, client):
    run(capture)
    f = Document.objects.get(pda_id=4210).versions.get().files.get(name="Calculator.exe.zip")
    r = client.get(f.get_absolute_url())
    data = b"".join(r.streaming_content)
    assert zipfile.ZipFile(io.BytesIO(data)).read("Calculator.exe") == b"MZ program"


def test_the_old_links_lead_to_the_migrated_records(people, capture, client):
    run(capture)
    doc = Document.objects.get(pda_id=4210)
    files = {f.name: f for f in doc.versions.get().files.all()}
    expect = {
        "/documents/pda/documents/4210/view": doc.get_absolute_url(),
        "/documents/pda/documents/4210/comments/list": f"{doc.get_absolute_url()}comments/",
        "/documents/pda/api/view/4210/HVAC%20Draft%20Report.pdf": files["HVAC Draft Report.pdf"].get_absolute_url() + "?view=1",
        "/documents/pda/api/downloads/4210/HVAC%20Draft%20Report.pdf": files["HVAC Draft Report.pdf"].get_absolute_url(),
        "/documents/pda/api/view/4210/Appendices.xlsx": files["Appendices.xlsx"].get_absolute_url(),  # not viewable: downloads
        "/documents/pda/api/downloads/4210/Calculator.exe": files["Calculator.exe.zip"].get_absolute_url(),
        "/documents/pda/search?q=hvac": "/documents/",
        "/documents/pda/": "/documents/",
        "/documents/psr/project-status": "/documents/studies/",
    }
    for old, new in expect.items():
        r = client.get(old)
        assert (r.status_code, r["Location"]) == (301, iri_to_uri(new)), old
    for missing in ["/documents/pda/documents/13/view", "/documents/pda/api/view/4210/other.pdf", "/documents/pda/api/edit/4210/x.pdf"]:
        assert client.get(missing).status_code == 404, missing

    study = studies.create_study(
        people["ed"],
        None,
        studies.StudyData(title="Residential HVAC impact evaluation", manager=people["dnv"], lead=option("study_lead", "CPUC")),
        number=512,
    )
    r = client.get("/documents/psr/project-status-view-edit/512")
    assert (r.status_code, r["Location"]) == (301, study.get_absolute_url())
    page = client.get(doc.get_absolute_url()).content.decode()
    assert "The PDA, document 4210" in page and "CPU0213" in page
    r = client.get("/documents/pda/api/downloads/12/plan.pdf%3C/a%3E")
    assert r.status_code == 301 and r["Location"].endswith("/plan.pdf")


def test_dates_without_a_time_are_local_midnight():
    when = pda_import._when("2007-12-18")
    assert timezone.localdate(when) == date(2007, 12, 18) and pda_import._when("") is None and pda_import._when("n/a") is None
    assert pda_import._when("2000-01-01T00:00:00.000Z") is None
    assert pda_import._day("2026-10-29T00:38:21.110Z") == date(2026, 10, 28)  # Pacific, as the PDA shows it

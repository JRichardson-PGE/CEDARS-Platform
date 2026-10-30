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

WORK_ORDERS = [{"contractID": 7, "contName": "ED_D_Res_7", "cpucContID": "17PS5017", "company": "An evaluation firm"}]
DELIVERABLES = [
    {
        "deliverableID": 4210,
        "name": "PY2019 Residential HVAC Impact Evaluation Draft Report",
        "isPublished": True,
        "dateUploaded": "2021-03-02T17:15:00.000Z",
        "pubViewStart": "2021-03-02",
        "pubViewEnd": "2021-03-23",
        "summary": "Gross and net savings of residential HVAC measures.",
        "calmacID": "CPU0213.01",
        "fileName": "HVAC Draft Report.pdf",
        "attachments": [{"fileName": "Appendices.xlsx"}, "Calculator.exe", "Missing appendix.pdf"],
        "options": [{"catOptID": 70, "catOptDesc": "Draft Report"}, {"catOptID": 5}, {"catOptID": 23}, {"catOptID": 999}],
        "workOrder": WORK_ORDERS[0],
        "task": {"taskID": 31, "taskName": "Task 3: HVAC"},
    },
    {
        "deliverableID": 12,
        "name": "2006-2008 Evaluation Plan",
        "isPublished": True,
        "dateUploaded": "2007-12-18",
        "fileName": "plan<b>.pdf",
        "options": [],
    },
    {"deliverableID": 13, "name": "No file", "isPublished": True, "dateUploaded": "2008-01-02", "fileName": "gone.pdf"},
    {"deliverableID": 14, "name": "Unpublished", "isPublished": False, "fileName": "x.pdf"},
]
COMMENTS = [
    {
        "commentID": 900,
        "subject": "Baseline",
        "comment": "Which baseline year?",
        "isQuestion": True,
        "section": "",
        "createdOn": "2021-03-05T10:00:00Z",
        "createdBy": "A Commenter",
        "response": "2019.",
        "responseBy": "ED staff",
        "responseDate": "2021-03-10T09:00:00Z",
        "isActive": True,
        "attachment": "markup.pdf",
    },
    {
        "commentID": 901,
        "subject": "Deleted",
        "comment": "Never mind.",
        "isQuestion": False,
        "createdOn": "2021-03-06",
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
        (12, "plan<b>.pdf", PDF),
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
    assert "2 documents imported (4 files)" in out and "1 not published" in out
    assert "main file not found: 13" in out and "4210/Missing appendix.pdf" in out
    assert "documents with no deliverable type filed as Report: 12" in out and "999 x1" in out
    assert not Document.objects.exists() and not WorkOrder.objects.exists()


def test_the_pda_is_migrated_with_its_dates_files_and_comments(people, capture, cpuc):
    out = run(capture)
    assert "2 documents imported (4 files)" in out and "2 comments and 1 responses" in out
    doc = Document.objects.get(pda_id=4210)
    assert doc.collection == "emv" and doc.owner == cpuc and doc.document_type.label == "Draft Report"
    assert sorted(o.label for o in doc.attributes.all()) == ["Impact Evaluation", "Residential"]
    assert doc.published_at == doc.created_at and doc.published_at.date() == date(2021, 3, 2)
    assert doc.calmac_id == "CPU0213.01"
    assert doc.work_order.number == "ED_D_Res_7" and doc.work_order.cpuc_contract_id == "17PS5017" and doc.task.name == "Task 3: HVAC"
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
    assert old.document_type.label == "Report" and old.versions.get().files.get().legacy_name == "plan<b>.pdf"
    assert AuditEvent.objects.filter(action="documents.imported").count() == 2

    again = run(capture)
    assert "0 documents imported" in again and "2 already imported" in again
    assert Document.objects.count() == 2


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
    assert "The PDA, document 4210" in page and "CPU0213.01" in page


def test_dates_without_a_time_are_local_midnight():
    when = pda_import._when("2007-12-18")
    assert timezone.localdate(when) == date(2007, 12, 18) and pda_import._when("") is None and pda_import._when("n/a") is None

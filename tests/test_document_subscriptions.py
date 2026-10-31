import io
import zipfile
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from cedars.modules.documents import comments, service, studies, subscriptions, textract
from cedars.modules.documents.models import DocumentSubscription, FileText, KeyDate
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications.models import Notification
from tests.conftest import sign_in
from tests.test_documents import details, make_people, option, upload

from .conftest import page_only

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()


def days(n):
    return TODAY + timedelta(days=n)


def pdf_with(*lines: str) -> bytes:
    """A one-page PDF whose page shows the lines as text."""
    text = " ".join(f"({line.replace('(', '').replace(')', '')}) Tj 0 -14 Td" for line in lines)
    stream = f"BT /F1 12 Tf 72 720 Td {text} ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for o in offsets:
        out.write(b"%010d 00000 n \n" % o)
    out.write(b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def office(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    p = make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)
    p["public"] = make_user("resident@example.org", "Rae Resident")
    p["other"] = make_user("planner@example.org", "Pat Planner")
    return p


def read(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return textract.read(path, name)


# --- the text of files (BR-15.2)


def test_text_is_read_from_pdf_office_and_plain_files(tmp_path):
    assert "Refrigerant charge and airflow" in read(tmp_path, "a.pdf", pdf_with("Refrigerant charge and airflow", "Second line"))
    docx = office(
        {
            "word/document.xml": '<w:document><w:body><w:p><w:r><w:t>Heat pump</w:t></w:r><w:r><w:t xml:space="preserve"> water'
            "</w:t></w:r><w:r><w:t>heaters &amp; more</w:t></w:r></w:p><w:p><w:r><w:t>Next paragraph</w:t></w:r></w:p></w:body>"
            "</w:document>",
            "word/styles.xml": "<w:t>not text</w:t>",
        }
    )
    assert read(tmp_path, "a.docx", docx) == "Heat pump waterheaters & more\nNext paragraph"
    pptx = office(
        {
            "ppt/slides/slide10.xml": "<p:sld><a:p><a:r><a:t>Tenth</a:t></a:r></a:p></p:sld>",
            "ppt/slides/slide2.xml": "<p:sld><a:p><a:r><a:t>Second</a:t></a:r></a:p></p:sld>",
        }
    )
    assert read(tmp_path, "a.pptx", pptx) == "Second\nTenth"  # slides in their order, not the zip's
    xlsx = office({"xl/sharedStrings.xml": "<sst><si><t>Measure</t></si><si><r><t>Net </t></r><r><t>kWh</t></r></si></sst>"})
    assert read(tmp_path, "a.xlsx", xlsx) == "Measure\nNet kWh"
    assert read(tmp_path, "a.csv", "Programme,Coût\n".encode("cp1252")) == "Programme,Coût\n"
    with pytest.raises(textract.Unreadable, match=".png"):
        read(tmp_path, "a.png", b"\x89PNG")
    with pytest.raises(textract.Unreadable, match="could not be read"):
        read(tmp_path, "b.docx", b"not a zip")
    with pytest.raises(textract.Unreadable, match="no text"):
        read(tmp_path, "b.pdf", pdf_with())


def test_a_stored_file_keeps_its_text_and_search_finds_it(people, pge):
    w = people["pge_writer"]
    d = details("pa", "Cap and Target Report", "2025 Annual Report")
    doc = service.create_document(
        w, pge, "pa", d, [upload("report.pdf", pdf_with("Refrigerant charge and airflow diagnostics"))], publish=True
    )
    f = doc.versions.get().files.get()
    assert f.text.error == "" and "diagnostics" in f.text.text and f.text.vector
    other = service.create_document(
        w, pge, "pa", details("pa", "Cap and Target Report", "Water plan"), [upload("scan.png", b"\x89PNG")], publish=True
    )
    assert other.versions.get().files.get().text.error.startswith("Text is not read")

    anyone = AnonymousUser()
    found = lambda q: set(service.search(anyone, service.Search(q=q)).values_list("pk", flat=True))  # noqa: E731
    assert found("diagnostic") == {doc.pk}  # stemmed: diagnostics
    assert found('"charge and airflow"') == {doc.pk}
    assert found('"airflow charge"') == set()
    assert found("refrigerant -airflow") == set()
    assert found("water") == {other.pk}  # the title still counts
    matches = service.text_matches(anyone, "airflow", [doc])
    ((file, extract),) = matches[doc.pk]
    assert file == f and f"{service.MARK_START}airflow{service.MARK_END}" in extract


def test_the_text_of_unpublished_work_is_found_only_by_those_who_see_it(people, pge, client):
    w = people["pge_writer"]
    first = [upload("v1.pdf", pdf_with("First draft"))]
    doc = service.create_document(w, pge, "pa", details("pa", "Cap and Target Report"), first, publish=True)
    service.add_version(w, pge, doc, [upload("v2.pdf", pdf_with("Confidential tariff Q&A changes"))], reason="update")
    public = service.Search(q="tariff")
    assert not service.search(AnonymousUser(), public).exists()
    assert not service.search(people["sce_user"], public).exists()
    assert service.search(w, public).get() == doc
    assert service.search(people["ed"], public).get() == doc
    assert service.text_matches(AnonymousUser(), "tariff", [doc]) == {}

    sign_in(client, w)
    page = client.get("/documents/", {"q": "tariff"}).content.decode()
    assert "Found in v2.pdf (version 2)" in page and "<mark>tariff</mark>" in page
    assert "tariff</mark> Q&amp;A changes" in page  # the file's text escaped


def test_the_backfill_reads_files_stored_before(people, pge):
    doc = service.create_document(
        people["pge_writer"], pge, "pa", details("pa", "Cap and Target Report"), [upload("old.pdf", pdf_with("Backfilled"))], publish=True
    )
    FileText.objects.all().delete()
    call_command("documents_read_text", stdout=io.StringIO())
    assert "Backfilled" in doc.versions.get().files.get().text.text
    out = io.StringIO()
    call_command("documents_read_text", stdout=out)
    assert out.getvalue().startswith("0 files read")


# --- subscriptions (BR-15.5)


@pytest.fixture
def study(people):
    return studies.create_study(
        people["ed"],
        None,
        studies.StudyData(
            title="Residential HVAC impact evaluation",
            manager=people["dnv"],
            lead=option("study_lead", "CPUC"),
            status=option("study_status", "Design"),
            budget=Decimal(500000),
        ),
    )


def test_followers_hear_once_when_a_matching_version_is_published(people, pge, study, django_capture_on_commit_callbacks):
    pub, other, ed = people["public"], people["other"], people["ed"]
    report_type = option("pa_report_type", "Cap and Target Report")
    subscriptions.subscribe(pub, collection="pa")
    subscriptions.subscribe(pub, option=report_type)  # both match: still one message
    subscriptions.subscribe(other, option=report_type, channel=subscriptions.IN_APP)
    subscriptions.subscribe(ed, collection="emv")  # does not match
    w = people["pge_writer"]
    with django_capture_on_commit_callbacks(execute=True):
        doc = service.create_document(w, pge, "pa", details("pa", "Cap and Target Report"), [upload()])  # unpublished: nobody hears
    assert not Notification.objects.exists()
    with django_capture_on_commit_callbacks(execute=True):
        service.publish_version(w, doc.versions.get())
    assert Notification.objects.filter(user=pub).count() == 1
    assert Notification.objects.filter(user=other, email=False).count() == 1
    assert not Notification.objects.filter(user=ed).exists()
    assert [m.to for m in mail.outbox] == [[pub.email]]
    n = Notification.objects.get(user=pub)
    assert n.title == f"Published: {doc.title}" and n.link == doc.get_absolute_url()

    pub.is_active = False
    pub.save()
    with django_capture_on_commit_callbacks(execute=True):
        service.add_version(w, pge, doc, [upload("v2.pdf")], reason="corrected table 3", publish=True)
    assert Notification.objects.filter(user=pub).count() == 1
    assert "corrected table 3" in Notification.objects.filter(user=other).first().body


def test_study_followers_hear_of_documents_comment_periods_and_status(people, study, django_capture_on_commit_callbacks):
    pub, ev, dnv = people["public"], people["evaluator"], people["dnv"]
    subscriptions.subscribe(pub, study=study)
    with django_capture_on_commit_callbacks(execute=True):
        doc = service.create_document(
            ev, dnv, "emv", details("emv", "Evaluation Plan", "HVAC workplan", study=study), [upload()], comment_period=(days(0), days(14))
        )
        comments.set_period(ev, doc.versions.get(), days(0), days(20), reason="longer")  # unpublished: nobody hears
    assert not Notification.objects.filter(user=pub).exists()
    access_grant(people)
    with django_capture_on_commit_callbacks(execute=True):
        service.publish_version(ev, doc.versions.get())
    (n,) = Notification.objects.filter(user=pub)
    assert f"Comments are open {days(0):%Y-%m-%d} to {days(20):%Y-%m-%d}" in n.body
    with django_capture_on_commit_callbacks(execute=True):
        comments.set_period(ev, doc.versions.get(), days(0), days(30), reason="extended at the PAs' request")
    latest = Notification.objects.filter(user=pub).first()
    assert latest.title == "Open for comment: HVAC workplan" and f"to {days(30):%Y-%m-%d}" in latest.body
    assert latest.link == f"{doc.get_absolute_url()}comments/"

    data = studies.StudyData(
        title=study.title, manager=study.manager, lead=study.lead, status=option("study_status", "Data/Analysis"), budget=study.budget
    )
    with django_capture_on_commit_callbacks(execute=True):
        studies.change_study(people["ed"], study, data, reason="fieldwork done")
    latest = Notification.objects.filter(user=pub).first()
    assert latest.title == f"{study.study_id} is now Data/Analysis" and "was Design" in latest.body
    with django_capture_on_commit_callbacks(execute=True):
        data.budget = Decimal(600000)
        studies.change_study(people["ed"], study, data, reason="budget only")
    assert Notification.objects.filter(user=pub).count() == 3


def access_grant(people):
    from cedars.services.accounts import service as access
    from cedars.services.accounts.models import User

    access.grant(User.objects.get(email="admin@dnv.com"), people["evaluator"], people["dnv"], "documents.confirm")


def test_the_subscriptions_page_sets_collections_and_values_and_keeps_studies(people, study, client):
    pub = people["public"]
    subscriptions.subscribe(pub, study=study)
    subscriptions.subscribe(pub, collection="deer")
    sign_in(client, pub)
    page = client.get("/documents/subscriptions/").content.decode()
    assert "Residential HVAC impact evaluation" in page and "My subscriptions" in page
    report_type = option("pa_report_type", "Cap and Target Report")
    r = client.post("/documents/subscriptions/", {"collections": ["pa", "nonsense"], "options": [report_type.pk, "x"], "channel": "in_app"})
    assert r.status_code == 302
    mine = {s.target_label: s.channel for s in DocumentSubscription.objects.filter(user=pub)}
    assert mine == {
        "Collection: PA documents": "in_app",
        f"{report_type.attribute_list.name}: Cap and Target Report": "in_app",
        f"Study {study.study_id}": "in_app",
    }
    r = client.post("/documents/subscriptions/follow", {"study": study.number, "action": "unfollow", "next": "https://evil.example/"})
    assert r.url == study.get_absolute_url()
    assert not DocumentSubscription.objects.filter(user=pub, study=study).exists()
    wo = studies.add_work_order(people["ed"], number="ED_D_HVAC_1")
    client.post("/documents/subscriptions/follow", {"work_order": wo.pk})
    assert "Stop following" in client.get(wo.get_absolute_url()).content.decode()
    assert DocumentSubscription.objects.get(user=pub, work_order=wo).channel == "email"


def test_cpuc_staff_see_who_subscribes(people, study, client):
    subscriptions.subscribe(people["public"], study=study)
    sign_in(client, people["public"])
    assert client.get("/documents/subscribers/").status_code == 404
    sign_in(client, people["ed"])
    page = client.get("/documents/subscribers/").content.decode()
    assert f"Study {study.study_id}" in page and "Rae Resident" in page
    csv_text = client.get("/documents/subscribers/", {"format": "csv"}).content.decode()
    assert "resident@example.org" in csv_text


# --- key dates and the home page (BR-15.3)


def test_cpuc_ed_posts_key_dates_on_the_home_page(people, pge, client):
    with pytest.raises(subscriptions.AccessDenied):
        subscriptions.add_key_date(people["pge_writer"], days(5), "Workshop")
    sign_in(client, people["pge_writer"])
    assert client.post("/documents/key-dates/new", {"on": days(5), "title": "Workshop"}).status_code == 404
    sign_in(client, people["ed"])
    client.post("/documents/key-dates/new", {"on": days(5), "title": "EM&V workshop", "link": "https://www.cpuc.ca.gov/"})
    client.post("/documents/key-dates/new", {"on": days(-3), "title": "Workplan comments closed"})
    client.post("/documents/key-dates/new", {"on": days(-60), "title": "Long ago"})
    client.get("/documents/")  # shows the messages
    page = client.get("/documents/").content.decode()
    assert "EM&amp;V workshop" in page and "Workplan comments closed" in page and "Long ago" not in page
    assert "Post a key date" in page
    kd = KeyDate.objects.get(title="EM&V workshop")
    client.post(f"/documents/key-dates/{kd.pk}/remove", {"reason": "postponed"})
    client.get("/documents/")
    kd.refresh_from_db()
    assert kd.removed_at is not None and "EM&amp;V workshop" not in client.get("/documents/").content.decode()
    assert AuditEvent.objects.filter(action="documents.key_date_removed", reason="postponed").exists()
    client.logout()
    page = client.get("/documents/").content.decode()
    assert "Workplan comments closed" in page and "Post a key date" not in page and "Remove" not in page
    assert "Key dates" not in page_only(client.get("/documents/", {"q": "x"}).content.decode())  # a search shows results only


def test_the_home_page_lists_what_is_open_for_comment(people, pge, client):
    w = people["pge_writer"]
    d = details("pa", "Cap and Target Report", "Draft portfolio plan")
    service.create_document(w, pge, "pa", d, [upload()], publish=True, comment_period=(days(-1), days(9)))
    service.create_document(
        w, pge, "pa", details("pa", "Cap and Target Report", "Unpublished plan"), [upload()], comment_period=(days(-1), days(9))
    )
    service.create_document(
        w, pge, "pa", details("pa", "Cap and Target Report", "Closed plan"), [upload()], publish=True, comment_period=(days(-9), days(-1))
    )
    page = client.get("/documents/").content.decode()
    assert "Draft portfolio plan</a>, version 1: until" in page
    assert "Unpublished plan" not in page and "Closed plan" not in page.split("Open for comment now")[1].split("</section>")[0]

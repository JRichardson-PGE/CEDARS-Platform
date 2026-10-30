import csv
import io
from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, InternalError, ProgrammingError, transaction
from django.utils import timezone

from cedars.modules.documents import service
from cedars.modules.documents.models import AttributeList, AttributeOption, Collection, Document, DocumentFile, DocumentVersion
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry

pytestmark = pytest.mark.django_db
PDF = b"%PDF-1.7\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


def option(list_code, label):
    return AttributeOption.objects.get(attribute_list__code=list_code, label=label)


def upload(name="report.pdf", data=PDF):
    return service.Upload(name, SimpleUploadedFile(name, data))


def details(collection, type_label, title="2025 Annual Report", **kw):
    types = service.type_list(collection)
    return service.Details(document_type=types.options.get(label=type_label), title=title, **kw)


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    bayren = access.create_organisation(cpuc_admin, code="BAYREN", name="Bay Area Regional Energy Network", kind="pa", pa_type="REN")
    dnv = access.create_organisation(cpuc_admin, code="DNV", name="DNV Energy Insights", kind="other")
    pge_writer = make_user("writer@pge.com", "Wren Writer")
    access.grant(pge_admin, pge_writer, pge, "documents.submit")
    access.grant(pge_admin, pge_writer, pge, "documents.confirm")
    pge_drafter = make_user("drafter@pge.com", "Dee Drafter")
    access.grant(pge_admin, pge_drafter, pge, "documents.submit")
    bay_user = make_user("analyst@bayren.org", "Bay Analyst")
    access.grant(cpuc_admin, bay_user, bayren, perms_org_admin())
    access.grant(bay_user, make_user("x@bayren.org"), bayren, "documents.read")
    bay_writer = make_user("writer@bayren.org")
    access.grant(bay_user, bay_writer, bayren, "documents.submit")
    sce_user = make_user("reader@sce.com")
    access.grant(cpuc_admin, sce_user, sce, perms_org_admin())
    access.grant(sce_user, make_user("docs@sce.com"), sce, "documents.submit")
    evaluator = make_user("evaluator@dnv.com", "Eva Luator")
    dnv_admin = make_user("admin@dnv.com")
    access.grant(cpuc_admin, dnv_admin, dnv, perms_org_admin())
    access.grant(dnv_admin, evaluator, dnv, "documents.submit")
    ed = make_user("ed@cpuc.ca.gov", "Ed Staff")
    access.grant(cpuc_admin, ed, cpuc, "documents.admin")
    access.grant(cpuc_admin, ed, cpuc, "documents.submit")
    return {
        "sce": sce,
        "bayren": bayren,
        "dnv": dnv,
        "pge_writer": pge_writer,
        "pge_drafter": pge_drafter,
        "bay_writer": bay_writer,
        "sce_user": sce_user,
        "evaluator": evaluator,
        "ed": ed,
    }


def perms_org_admin():
    from cedars.services.accounts import permissions

    return permissions.ORG_ADMIN


@pytest.fixture
def jcm(people, pge):
    """A Joint Cooperation Memo filed by PG&E naming BayREN, not yet published."""
    d = details("pa", "Joint Cooperation Memo", title="PG&E / BayREN Joint Cooperation Memo 2026", pas=[people["bayren"]], first_year=2026)
    return service.create_document(people["pge_writer"], pge, "pa", d, [upload("jcm.pdf"), upload("appendix.xlsx", b"PK\x03\x04x")])


def test_the_library_starts_with_the_pdas_categories_and_each_collections_document_types():
    lists = {al.code: al for al in AttributeList.objects.all()}
    pda = [al for al in lists.values() if al.pda_id]
    assert len(pda) == 8
    assert AttributeOption.objects.filter(attribute_list__in=pda).count() == 82
    assert AttributeOption.objects.filter(pda_id__isnull=False).count() == 82
    assert option("study_lead", "IOU").pda_id == 21
    assert {al.type_of for al in lists.values() if al.type_of} == {c.value for c in Collection}
    assert lists["pa_report_type"].options.count() == 8
    assert lists["sector"].collections == ["pa", "guidance", "deer", "program", "emv"]


def test_a_pa_files_a_joint_document_that_every_pa_named_sees_and_manages(people, pge, jcm, client):
    assert set(jcm.pas.values_list("code", flat=True)) == {"PGE", "BAYREN"}
    v = jcm.versions.get()
    assert v.number == 1 and [f.name for f in v.files.all()] == ["jcm.pdf", "appendix.xlsx"] and not v.published
    assert jcm.published_at is None
    bay = people["bay_writer"]
    assert service.can_read(bay, jcm) and service.can_manage(bay, jcm) and not service.can_publish(bay, jcm)
    assert not service.can_read(people["sce_user"], jcm)  # unpublished: only its PAs and CPUC staff
    assert service.can_read(people["ed"], jcm)
    assert not service.can_read(_anon(), jcm)
    assert client.get(f"/documents/{jcm.pk}/").status_code == 404
    service.publish_version(people["pge_writer"], v)
    jcm.refresh_from_db()
    assert jcm.published_at is not None
    assert client.get(f"/documents/{jcm.pk}/").status_code == 200
    assert service.can_read(people["sce_user"], jcm) and not service.can_manage(people["sce_user"], jcm)


def _anon():
    from django.contrib.auth.models import AnonymousUser

    return AnonymousUser()


def test_who_files_what(people, pge, cpuc):
    assert service.collections_for(people["pge_writer"], pge) == ["pa", "program", "emv"]
    assert service.collections_for(people["evaluator"], people["dnv"]) == ["emv"]
    assert service.collections_for(people["ed"], cpuc) == ["pa", "guidance", "deer", "program", "emv"]
    assert service.collections_for(people["sce_user"], people["sce"]) == []  # org admin, but no documents.submit
    with pytest.raises(AccessDenied):
        service.create_document(people["pge_writer"], pge, "deer", details("deer", "Resolution", "E-5221"), [upload()])
    with pytest.raises(AccessDenied):
        service.create_document(people["sce_user"], people["sce"], "pa", details("pa", "Cap and Target Report"), [upload()])
    deer = service.create_document(people["ed"], cpuc, "deer", details("deer", "Resolution", "Resolution E-5221"), [upload()], publish=True)
    assert deer.published_at and not deer.pas.exists()  # a CPUC document names no PA unless asked


def test_a_new_version_keeps_every_earlier_one_and_needs_a_reason(people, jcm, client):
    writer = people["pge_writer"]
    service.publish_version(writer, jcm.versions.get())
    with pytest.raises(service.DocumentError, match="why"):
        service.add_version(writer, None, jcm, [upload("jcm_v2.pdf")], reason=" ")
    v2 = service.add_version(people["bay_writer"], people["bayren"], jcm, [upload("jcm_v2.pdf")], reason="Signed copy")
    assert v2.number == 2 and v2.organisation.code == "BAYREN" and not v2.published
    # the public sees version 1 until version 2 is published; those on the document see both
    assert service.current_version(_anon(), jcm).number == 1
    assert service.current_version(writer, jcm).number == 1  # the newest PUBLISHED one is the current one
    assert [v.number for v in service.visible_versions(_anon(), jcm)] == [1]
    assert [v.number for v in service.visible_versions(writer, jcm)] == [2, 1]
    service.publish_version(writer, v2)
    assert service.current_version(_anon(), jcm).number == 2
    html = client.get(f"/documents/{jcm.pk}/").content.decode()
    assert "Version 2, published" in html and "jcm.pdf" in html and "Signed copy" in html  # version 1's file is still listed
    with pytest.raises(service.DocumentError, match="already published"):
        service.publish_version(writer, v2)


def test_the_database_refuses_to_change_a_published_version_or_its_files(people, jcm):
    v = jcm.versions.get()
    f = v.files.first()
    with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
        DocumentFile.objects.filter(pk=f.pk).update(name="other.pdf")  # a file never changes, published or not
    service.publish_version(people["pge_writer"], v)
    with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
        DocumentVersion.objects.filter(pk=v.pk).update(reason="rewritten")
    with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
        DocumentVersion.objects.filter(pk=v.pk).update(published_at=None)
    with pytest.raises((InternalError, ProgrammingError, IntegrityError)), transaction.atomic():
        DocumentFile.objects.filter(pk=f.pk).delete()


def test_files_are_checked_before_anything_is_filed(people, pge):
    writer = people["pge_writer"]
    d = details("pa", "Cap and Target Report")
    for files, message in [
        ([upload("setup.exe", b"MZ")], "not accepted"),
        ([upload("page.html", b"<script>")], "not accepted"),
        ([upload("a.pdf"), upload("A.PDF")], "same name"),
        ([upload("empty.pdf", b"")], "empty"),
        ([], "main file"),
    ]:
        with pytest.raises(service.DocumentError, match=message):
            service.create_document(writer, pge, "pa", d, files)
    assert not Document.objects.exists()
    assert service.clean_name('C:\\Users\\me\\"Q3"\r\nreport.pdf') == "_Q3_report.pdf"


def test_details_are_checked_against_the_collections_value_lists(people, pge):
    writer = people["pge_writer"]
    with pytest.raises(service.DocumentError, match="document type"):
        service.create_document(writer, pge, "pa", service.Details(option("deer_type", "Resolution"), "x"), [upload()])
    d = details("emv", "Draft Report", "Draft impact evaluation", attributes=[option("study_lead", "IOU"), option("study_lead", "CPUC")])
    with pytest.raises(service.DocumentError, match="one study lead"):
        service.create_document(writer, pge, "emv", d, [upload()])
    d = details("pa", "Cap and Target Report", attributes=[option("study_type", "Impact Evaluation")])
    with pytest.raises(service.DocumentError, match="not used"):
        service.create_document(writer, pge, "pa", d, [upload()])
    d = details("pa", "Cap and Target Report", pas=[people["dnv"]])
    with pytest.raises(service.DocumentError, match="program administrators"):
        service.create_document(writer, pge, "pa", d, [upload()])


def test_a_retired_value_stays_on_documents_but_is_not_offered(people, pge):
    ed, writer = people["ed"], people["pge_writer"]
    cycle = option("portfolio_cycle", "2021")
    doc = service.create_document(writer, pge, "emv", details("emv", "Report", "PY2021 report", attributes=[cycle]), [upload()])
    with pytest.raises(AccessDenied):
        service.retire_option(writer, cycle, reason="old")
    service.retire_option(ed, cycle, reason="The 2021 cycle is closed")
    new = details("emv", "Report", "Another", attributes=[cycle])
    with pytest.raises(service.DocumentError, match="retired"):
        service.create_document(writer, pge, "emv", new, [upload()])
    # changing the first document's other details keeps the retired value
    service.change_details(writer, doc, details("emv", "Report", "PY2021 impact report", attributes=[cycle]), reason="Fuller title")
    assert list(doc.attributes.all()) == [cycle]
    event = AuditEvent.objects.get(action="documents.details_changed")
    assert event.before["title"] == "PY2021 report" and event.after["title"] == "PY2021 impact report" and event.reason == "Fuller title"
    service.add_option(ed, cycle.attribute_list, "2024-2027", reason="BR-15.3: the list stopped at 2021")
    with pytest.raises(service.DocumentError, match="already has"):
        service.add_option(ed, cycle.attribute_list, "2024-2027")
    service.rename_option(ed, option("evaluation_firm", "Kema"), "KEMA", reason="spelling")
    assert AuditEvent.objects.filter(action__startswith="documents.option_").count() == 3


def test_search_filters_and_the_result_file(people, pge, cpuc, jcm, client):
    writer, ed = people["pge_writer"], people["ed"]
    hvac = registry.create_program(None, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1))
    impact = details(
        "emv",
        "Final Evaluation Report",
        "PY2022-2024 HVAC impact evaluation",
        summary="Gross and net savings of residential HVAC.",
        programs=[hvac],
        first_year=2022,
        last_year=2024,
        attributes=[option("study_type", "Impact Evaluation"), option("sector", "Residential"), option("study_lead", "CPUC")],
    )
    evaluation = service.create_document(ed, cpuc, "emv", impact, [upload("hvac_impact.pdf")], publish=True)
    process = details(
        "emv", "Report", "Process evaluation", attributes=[option("study_type", "Process Evaluation"), option("sector", "Residential")]
    )
    service.create_document(ed, cpuc, "emv", process, [upload("process.pdf")], publish=True)

    def titles(user, **kw):
        return [d.title for d in service.search(user, service.Search(**kw))]

    assert titles(_anon()) == ["Process evaluation", "PY2022-2024 HVAC impact evaluation"]  # the unpublished memo is not public
    assert "PG&E / BayREN Joint Cooperation Memo 2026" in titles(people["bay_writer"])
    assert titles(_anon(), q="hvac savings") == ["PY2022-2024 HVAC impact evaluation"]
    assert titles(_anon(), q="hvac_impact") == ["PY2022-2024 HVAC impact evaluation"]  # file names too
    assert titles(_anon(), year=2023) == ["PY2022-2024 HVAC impact evaluation"]
    assert titles(_anon(), year=2025) == []
    assert titles(_anon(), program="pge_res_hvac") == ["PY2022-2024 HVAC impact evaluation"]
    impact_opt, process_opt, res = (
        option("study_type", "Impact Evaluation"),
        option("study_type", "Process Evaluation"),
        option("sector", "Residential"),
    )
    assert len(titles(_anon(), options=[impact_opt, process_opt])) == 2  # any within a list
    assert titles(_anon(), options=[impact_opt, res]) == ["PY2022-2024 HVAC impact evaluation"]  # each list must match
    assert titles(writer, status="unpublished") == ["PG&E / BayREN Joint Cooperation Memo 2026"]
    assert titles(_anon(), pa=pge) == []
    html = client.get(f"/documents/?q=hvac&options={impact_opt.pk}").content.decode()
    assert "1 document." in html and evaluation.title in html
    rows = list(csv.DictReader(io.StringIO(client.get(f"/documents/documents.csv?options={res.pk}").content.decode())))
    assert [r["Title"] for r in rows] == ["Process evaluation", "PY2022-2024 HVAC impact evaluation"]
    hvac_row = rows[1]
    assert hvac_row["Programs"] == "PGE_Res_HVAC" and hvac_row["Study type"] == "Impact Evaluation" and hvac_row["Version"] == "1"
    assert hvac_row["Link"].endswith(f"/documents/{evaluation.pk}/") and hvac_row["FirstYear"] == "2022"


def test_every_download_is_an_audit_event_and_unpublished_files_stay_private(people, jcm, client, signed_in):
    v = jcm.versions.get()
    pdf, xlsx = v.files.all()
    assert client.get(pdf.get_absolute_url()).status_code == 404  # not published yet
    service.publish_version(people["pge_writer"], v)
    r = client.get(pdf.get_absolute_url())
    assert r.status_code == 200 and b"".join(r.streaming_content) == PDF and "attachment" in r["Content-Disposition"]
    r = client.get(pdf.get_absolute_url() + "?view=1")
    assert "inline" in r["Content-Disposition"] and r["Content-Type"] == "application/pdf"
    r = client.get(xlsx.get_absolute_url() + "?view=1")  # only PDFs, text and images open in the browser
    assert "attachment" in r["Content-Disposition"]
    assert client.get(f"/documents/files/{pdf.pk}/other.pdf").status_code == 404
    events = AuditEvent.objects.filter(object_type="documents.documentfile").order_by("id")
    assert [(e.action, e.actor_label) for e in events] == [
        ("documents.file_downloaded", "public"),
        ("documents.file_viewed", "public"),
        ("documents.file_downloaded", "public"),
    ]
    v2 = service.add_version(people["pge_writer"], None, jcm, [upload("jcm_signed.pdf")], reason="Signed")
    draft_file = v2.files.get()
    assert client.get(draft_file.get_absolute_url()).status_code == 404
    c = signed_in(people["bay_writer"])
    assert c.get(draft_file.get_absolute_url()).status_code == 200
    assert AuditEvent.objects.filter(object_type="documents.documentfile").order_by("id").last().actor_label == "writer@bayren.org"
    log = c.get(f"/documents/{jcm.pk}/downloads/").content.decode()
    assert log.count("public") >= 3 and "writer@bayren.org" in log


def test_the_pages_file_a_document_and_add_a_version(people, pge, signed_in):
    c = signed_in(people["pge_writer"])
    html = c.get("/documents/new/?collection=emv").content.decode()
    assert "Stage of work" in html and "Evaluation firm" in html
    assert c.get("/documents/new/?collection=deer").status_code == 404
    data = {
        "collection": "pa",
        "document_type": option("pa_report_type", "Cap and Target Report").pk,
        "title": "2025 Cap and Target Report",
        "pas": [people["sce"].pk],
        "first_year": "2025",
        "attr_sector": [option("sector", "Commercial").pk],
        "main_file": SimpleUploadedFile("cap.pdf", PDF),
        "attachments": [SimpleUploadedFile("cap.xlsx", b"PK"), SimpleUploadedFile("notes.txt", b"notes")],
        "publish": "on",
    }
    r = c.post("/documents/new/", data)
    doc = Document.objects.get(title="2025 Cap and Target Report")
    assert r.status_code == 302 and r["Location"] == f"/documents/{doc.pk}/"
    assert doc.published_at and doc.first_year == doc.last_year == 2025 and set(doc.pas.values_list("code", flat=True)) == {"PGE", "SCE"}
    assert [f.name for f in doc.versions.get().files.all()] == ["cap.pdf", "cap.xlsx", "notes.txt"]
    r = c.post(f"/documents/{doc.pk}/versions/new/", {"main_file": SimpleUploadedFile("cap2.pdf", PDF), "reason": "Corrected table 3"})
    assert r.status_code == 302 and doc.versions.count() == 2 and not doc.versions.get(number=2).published
    page = c.get(f"/documents/{doc.pk}/").content.decode()
    assert "Version 1, published" in page and "Publish version 2" in page
    r = c.post(f"/documents/{doc.pk}/versions/2/publish")
    assert doc.versions.get(number=2).published
    drafter = signed_in(people["pge_drafter"])
    assert "Publish it now" not in drafter.get(f"/documents/{doc.pk}/versions/new/").content.decode()
    r = c.post(f"/documents/{doc.pk}/change/", {"document_type": doc.document_type_id, "title": "2025 Cap and Target", "reason": ""})
    assert r.status_code == 200 and Document.objects.get(pk=doc.pk).title == "2025 Cap and Target Report"


def test_only_cpuc_ed_keeps_the_value_lists(people, signed_in, client):
    assert client.get("/documents/lists/").status_code == 200
    sector = AttributeList.objects.get(code="sector")
    c = signed_in(people["pge_writer"])
    c.post("/documents/lists/sector/add", {"label": "Public"})
    assert not sector.options.filter(label="Public").exists()
    ed = signed_in(people["ed"])
    ed.post("/documents/lists/sector/add", {"label": "Public", "reason": "PSR sector"})
    public = sector.options.get(label="Public")
    ed.post(f"/documents/lists/options/{public.pk}/retire", {"reason": "not used after all"})
    public.refresh_from_db()
    assert public.retired_on == timezone.localdate()
    assert "Retired" in ed.get("/documents/lists/").content.decode()
    ed.post(f"/documents/lists/options/{public.pk}/reinstate", {"reason": "used after all"})
    public.refresh_from_db()
    assert public.retired_on is None and public.offered_on(timezone.localdate() + timedelta(days=1))

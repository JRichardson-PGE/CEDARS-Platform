"""Old CEDARS links: CPUC ED's document administrators repoint an old address at another document or file, with a reason."""

import pytest

from cedars.modules.documents import service
from cedars.modules.documents.models import DocumentFile, LegacyLink
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from tests.test_documents import details, make_people, upload

pytestmark = pytest.mark.django_db

OLD = "/documents/standalone/41/"


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    return make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)


@pytest.fixture
def jcm(people, pge):
    d = details("pa", "Joint Cooperation Memo", title="PG&E / BayREN Joint Cooperation Memo 2026", pas=[people["bayren"]], first_year=2026)
    return service.create_document(people["pge_writer"], pge, "pa", d, [upload("jcm.pdf"), upload("appendix.xlsx", b"PK\x03\x04x")])


@pytest.fixture
def memo(people, pge):
    d = details("pa", "Joint Cooperation Memo", title="PG&E / SCE Joint Cooperation Memo 2026", pas=[people["sce"]], first_year=2026)
    return service.create_document(people["pge_writer"], pge, "pa", d, [upload("memo.pdf"), upload("tables.xlsx", b"PK\x03\x04x")])


@pytest.fixture
def link(jcm):
    return LegacyLink.objects.create(path=OLD, document=jcm)


def test_only_document_administrators_repoint_and_each_change_needs_a_reason(people, link, memo, jcm):
    with pytest.raises(AccessDenied):
        service.repoint_legacy_link(people["pge_writer"], link, document=memo, reason="wrong memo")
    with pytest.raises(service.DocumentError, match="reason"):
        service.repoint_legacy_link(people["ed"], link, document=memo, reason="  ")
    with pytest.raises(service.DocumentError, match="already leads there"):
        service.repoint_legacy_link(people["ed"], link, document=jcm, reason="no change")
    other = jcm.versions.get().files.get(name="jcm.pdf")
    with pytest.raises(service.DocumentError, match="is not a file of document"):
        service.repoint_legacy_link(people["ed"], link, document=memo, file=other, reason="wrong file")
    link.refresh_from_db()
    assert link.document == jcm and not AuditEvent.objects.filter(action="documents.legacy_link_repointed").exists()


def test_a_repoint_is_audited_with_where_it_led_before(people, link, memo, jcm):
    f = service.file_named(memo, "TABLES.XLSX")
    service.repoint_legacy_link(people["ed"], link, document=memo, file=f, reason="the import matched the wrong memo")
    link.refresh_from_db()
    assert (link.document, link.file) == (memo, f)
    e = AuditEvent.objects.get(action="documents.legacy_link_repointed")
    assert e.before == {"document": jcm.pk, "title": jcm.title, "file": ""}
    assert e.after == {"document": memo.pk, "title": memo.title, "file": "tables.xlsx"}
    assert e.reason == "the import matched the wrong memo"


def test_a_file_is_found_by_its_name_or_its_old_sites_name(memo):
    f = memo.versions.get().files.get(name="memo.pdf")
    renamed = DocumentFile.objects.create(
        version=f.version, position=5, name="memo-final.pdf", key=f.key, sha256=f.sha256, size=f.size, legacy_name="Memo (final).pdf"
    )
    assert service.file_named(memo, "MEMO.pdf") == f
    assert service.file_named(memo, "memo (FINAL).pdf") == renamed
    assert service.file_named(memo, "") is None and service.file_named(memo, "other.pdf") is None


def test_the_old_links_pages_are_for_document_administrators_only(people, link, signed_in):
    writer = signed_in(people["pge_writer"])
    assert writer.get("/documents/old-links/").status_code == 404
    assert writer.get(f"/documents/old-links/{link.pk}/").status_code == 404
    ed = signed_in(people["ed"])
    page = ed.get("/documents/old-links/?q=standalone").content.decode()
    assert OLD in page and "1 old link." in page
    assert "0 old links." in ed.get("/documents/old-links/?q=nothing-like-it").content.decode()


def test_an_old_link_is_repointed_on_its_page_and_then_leads_there(people, link, memo, signed_in):
    ed = signed_in(people["ed"])
    url = f"/documents/old-links/{link.pk}/"
    page = ed.post(url, {"document": memo.pk, "file": "nope.pdf", "reason": "wrong memo"}).content.decode()
    assert f"Document {memo.pk} has no file named nope.pdf." in page
    page = ed.post(url, {"document": 999999, "file": "", "reason": "wrong memo"}).content.decode()
    assert "There is no document 999999." in page
    page = ed.post(url, {"document": memo.pk, "file": "", "reason": ""}).content.decode()
    link.refresh_from_db()
    assert link.document_id != memo.pk
    r = ed.post(url, {"document": memo.pk, "file": "memo.pdf", "reason": "the import matched the wrong memo"})
    assert r.status_code == 302 and r["Location"] == "/documents/old-links/"
    link.refresh_from_db()
    assert link.document == memo and link.file.name == "memo.pdf"
    r = ed.get(OLD)
    assert r.status_code == 301 and r["Location"].startswith(link.file.get_absolute_url())

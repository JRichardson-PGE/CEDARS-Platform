import csv
import io

import pytest
from django.core.management import call_command
from django.utils.encoding import iri_to_uri

from cedars.modules.documents import cedars_import
from cedars.modules.documents.models import Document, DocumentFile, LegacyLink
from cedars.services.audit.models import AuditEvent
from tests.test_document_migration import capture  # noqa: F401  (the PDA capture fixture)
from tests.test_document_migration import run as run_pda
from tests.test_documents import PDF, make_people

pytestmark = pytest.mark.django_db

# The 2006-2013 EM&V site as its Wayback captures showed it (CEDARS-Research notes): document pages carry the PDA's deliverableID, and
# deliverableView.aspx sent the browser to /cpucFiles/pdaDocs/<id>/<file>; its home page linked files under /cpucFiles/pdaHomeDocs/
# <section>/ that never reached the PDA.
HOME_FILES = [
    {
        "collection": "emv",
        "key": "edw/pdaHomeDocs/2/EMV 9-9-14.zip",
        "document_type": "Quarterly Meeting Materials",
        "title": "EM&V quarterly meeting, September 2014",
        "version": "1",
        "posted": "2014-09-09",
        "file": "pdaHomeDocs/2/EMV 9-9-14.zip",
        "file_links": "http://www.energydataweb.com/cpucFiles/pdaHomeDocs/2/EMV%209-9-14.zip",
    },
    {
        "collection": "emv",
        "key": "edw/pdaHomeDocs/3/status.xlsx",
        "document_type": "Project Status Report",
        "title": "EM&V project status",
        "version": "1",
        "posted": "2013-04-01",
        "file": "pdaHomeDocs/3/status.xlsx",
        "file_links": "/cpucFiles/pdaHomeDocs/3/status.xlsx",
    },
    {
        "collection": "emv",
        "key": "pdaHomeDocs/4/info.pdf",  # an EM&V record that is not the site's
        "document_type": "Program Information",
        "version": "1",
        "posted": "2013-04-01",
        "file": "pdaHomeDocs/4/info.pdf",
    },
    {
        "collection": "deer",
        "key": "edw/pdaHomeDocs/4/info.pdf",  # the site's file outside the EM&V collection
        "document_type": "Guidance",
        "version": "1",
        "posted": "2013-04-01",
        "file": "pdaHomeDocs/4/info.pdf",
    },
]


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    return make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)


@pytest.fixture
def home_files(tmp_path):
    files = tmp_path / "edw"
    for row in HOME_FILES:
        path = files / row["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PDF if path.suffix == ".pdf" else b"PK archive")
    manifest = tmp_path / "edw.csv"
    with open(manifest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for row in HOME_FILES for k in row}))
        w.writeheader()
        w.writerows(HOME_FILES)
    out = io.StringIO()
    call_command("documents_import_cedars", "--manifest", str(manifest), "--files", str(files), stdout=out)
    return out.getvalue()


def test_the_sites_document_links_lead_to_the_pdas_migrated_records(people, capture, client):  # noqa: F811
    run_pda(capture)
    doc = Document.objects.get(pda_id=4210)
    main = doc.versions.get().files.get(position=0)
    plan = DocumentFile.objects.get(version__document__pda_id=12, position=0)
    expect = {
        "/documents/edw/cpuc/deliverableView.aspx?did=4210&uid=&tid=&cid=": main.get_absolute_url() + "?view=1",
        "/documents/edw/cpuc/DeliverableView.aspx?DID=12": plan.get_absolute_url() + "?view=1",  # IIS: any case
        "/documents/edw/cpucFiles/pdaDocs/4210/HVAC%20Draft%20Report.pdf": main.get_absolute_url() + "?view=1",
        "/documents/edw/cpuc/search.aspx?did=4210": doc.get_absolute_url(),
        "/documents/edw/cpuc/search.aspx?delivID=4210": doc.get_absolute_url(),
        "/documents/edw/cpuc/deliverableDownload.aspx?did=4210": doc.get_absolute_url(),
        "/documents/edw/cpuc/comment.aspx?did=4210": f"{doc.get_absolute_url()}comments/",
        "/documents/edw/cpuc/search.aspx": "/documents/?collection=emv",
        "/documents/edw/cpuc/": "/documents/?collection=emv",
        "/documents/edw/": "/documents/?collection=emv",
    }
    for old, new in expect.items():
        r = client.get(old)
        assert (r.status_code, r.get("Location")) == (301, iri_to_uri(new)), old
    for missing in [
        "/documents/edw/cpuc/deliverableView.aspx?did=13",  # not migrated (no file)
        "/documents/edw/cpuc/comment.aspx?did=99999",
        "/documents/edw/cpucFiles/pdaDocs/4210/other.pdf",
        "/documents/edw/cpucFiles/0/comments/comAttach_1.pdf",
        "/documents/edw/cpucFiles/pdaHomeDocs/2/EMV%209-9-14.zip",  # not imported in this test
    ]:
        assert client.get(missing).status_code == 404, missing


def test_the_home_page_files_are_migrated_from_a_manifest(people, home_files, client):
    assert "2 documents imported (2 versions, 2 files; EM&V documents 2)" in home_files
    assert "2 old addresses lead to them" in home_files
    assert home_files.count("only they, are EM&V records with keys starting 'edw/'") == 2

    meeting = Document.objects.get(cedars_key="emv:edw/pdaHomeDocs/2/EMV 9-9-14.zip")
    assert (meeting.collection, meeting.document_type.label, meeting.migrated_from) == (
        "emv",
        "Quarterly Meeting Materials",
        "The 2006-2013 EM&V site",
    )
    assert meeting.versions.get().reason == "Migrated from the 2006-2013 EM&V site"
    assert AuditEvent.objects.filter(action="documents.imported", actor_label="EM&V archive migration").count() == 2
    assert sorted(LegacyLink.objects.values_list("path", flat=True)) == [
        "/edw/cpucFiles/pdaHomeDocs/2/EMV 9-9-14.zip",
        "/edw/cpucFiles/pdaHomeDocs/3/status.xlsx",
    ]

    files = {f.name: f for f in DocumentFile.objects.all()}
    for old, new in {
        "/documents/edw/cpucFiles/pdaHomeDocs/2/EMV%209-9-14.zip": files["EMV 9-9-14.zip"].get_absolute_url(),
        "/documents/edw/cpucfiles/PDAHOMEDOCS/3/STATUS.XLSX": files["status.xlsx"].get_absolute_url(),
    }.items():
        r = client.get(old)
        assert (r.status_code, r.get("Location")) == (301, iri_to_uri(new)), old
    assert client.get("/documents/edw/cpucFiles/pdaHomeDocs/4/info.pdf").status_code == 404
    # kept apart from CEDARS' own addresses
    assert client.get("/deer-resources/cpucFiles/pdaHomeDocs/3/status.xlsx").get("Location") == "/documents/?collection=deer"

    page = client.get(meeting.get_absolute_url()).content.decode()
    assert "The 2006-2013 EM&amp;V site: its old links lead here" in page


def test_the_sites_addresses_are_kept_apart():
    assert cedars_import.legacy_path("https://www.energydataweb.com/cpucFiles/pdaHomeDocs/2/EMV%209-9-14.zip") == (
        "/edw/cpucFiles/pdaHomeDocs/2/EMV 9-9-14.zip"
    )
    assert cedars_import.legacy_path("/cpucFiles/x.pdf", archive=True) == "/edw/cpucFiles/x.pdf"
    assert cedars_import.legacy_path("/edw/cpucFiles/x.pdf", archive=True) == "/edw/cpucFiles/x.pdf"
    assert cedars_import.legacy_path("/cpucFiles/x.pdf") == "/cpucFiles/x.pdf"

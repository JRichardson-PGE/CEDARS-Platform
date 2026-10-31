import csv
import io
import zipfile
from datetime import date

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django.utils.encoding import iri_to_uri

from cedars.modules.documents import cedars_import
from cedars.modules.documents.models import Document, DocumentFile, LegacyLink
from cedars.services.audit.models import AuditEvent
from cedars.services.registry import service as registry
from tests.test_documents import PDF, make_people

pytestmark = pytest.mark.django_db

COLUMNS = [
    "collection",
    "key",
    "document_type",
    "title",
    "summary",
    "pas",
    "programs",
    "first_year",
    "last_year",
    "version",
    "posted",
    "in_effect_from",
    "in_effect_to",
    "notes",
    "published",
    "file",
    "file_name",
    "role",
    "file_links",
    "page_links",
]

# The records as CEDARS' public pages showed them on 2026-10-31: a DEER resource with three dated versions (its history page, one
# file per version), a staff guidance file, an implementation plan whose later versions have a redline and a change summary (the old
# site's broken download route kept as it is), and a PA's joint report.
DEER_HISTORY = "/deer-resources/deer-versions/2020/resource/16/history/"
ROWS = [
    {
        "collection": "deer",
        "key": "deer-versions/2020/16",
        "document_type": "DEER version",
        "title": "Errata for Resolution E-4952 DEER2020 Update",
        "first_year": "2020",
        "version": "1",
        "posted": "2019-01-18",
        "in_effect_from": "2020-01-01",
        "in_effect_to": "2019-02-12",
        "notes": "v1",
        "file": "deer/errata_2019-01-18.pdf",
        "file_links": "https://cedars.cpuc.ca.gov/deer-resources/deer-versions/2020/file/101/download/",
        "page_links": DEER_HISTORY,
    },
    {
        "collection": "deer",
        "key": "deer-versions/2020/16",
        "version": "2",
        "posted": "2019-02-13",
        "in_effect_to": "2023-02-01",
        "notes": "v2",
        "file": "deer/errata_2019-02-13.pdf",
        "file_links": "/deer-resources/deer-versions/2020/file/102/download/",
    },
    {
        "collection": "deer",
        "key": "deer-versions/2020/16",
        "version": "3",
        "posted": "2023-02-02",
        "notes": "v3",
        "file": "deer/errata_2023-02-02.pdf",
        "file_links": "/deer-resources/deer-versions/2020/file/103/download/",
    },
    {
        "collection": "guidance",
        "key": "guidance/29",
        "document_type": "Template",
        "title": "2025 Annual Report Template",
        "version": "1",
        "posted": "2026-03-24",
        "file": "guidance/2025_Annual_Report_Template_V1.2_Final.xlsx",
        "file_links": "/documents/guidance-document/download/29/",
    },
    {
        "collection": "program",
        "key": "history/1203",
        "document_type": "Implementation plan",
        "title": "Implementation Plan for PGE_Ag_001",
        "pas": "PGE",
        "programs": "PGE_Ag_001;PGE_Missing_9",
        "version": "1",
        "posted": "2020-08-31",
        "file": "program/v1/implementation_plan_v1.pdf",
        "file_links": "/documents/download/3001/mainchange_summary%7Cmain%7Credline)/",
        "page_links": "/documents/history/1203/",
    },
    {
        "collection": "program",
        "key": "history/1203",
        "version": "2",
        "posted": "2020-12-21",
        "file": "program/v2/implementation_plan_change_summary_v2.pdf",
        "role": "change_summary",
        "file_links": "/documents/download/3002/change_summarychange_summary%7Cmain%7Credline)/",
    },
    {
        "collection": "program",
        "key": "history/1203",
        "version": "2",
        "posted": "2020-12-21",
        "file": "program/v2/implementation_plan_v2.pdf",
        "role": "main",
        "file_links": "/documents/download/3002/mainchange_summary%7Cmain%7Credline)/",
    },
    {
        "collection": "program",
        "key": "history/1203",
        "version": "2",
        "posted": "2020-12-21",
        "file": "program/v2/implementation_plan_redline_v2.pdf",
        "role": "redline",
        "file_links": "/documents/download/3002/redlinechange_summary%7Cmain%7Credline)/",
    },
    {
        "collection": "pa",
        "key": "standalone/77",
        "document_type": "Joint Cooperation Memo",
        "title": "PG&E and BayREN joint cooperation memo",
        "pas": "PGE;BAYREN;NOPE",
        "first_year": "2025",
        "version": "1",
        "posted": "2025-02-03T21:00:00Z",
        "file": "pa/jcm.pdf",
        "file_name": "JCM 2025.pdf",
    },
    {
        "collection": "pa",
        "key": "standalone/78",
        "document_type": "Joint Cooperation Memo",
        "version": "1",
        "posted": "",
        "file": "gone.pdf",
    },
    {"collection": "deer", "key": "x/1", "document_type": "Not a type", "version": "1", "posted": "", "file": "deer/errata_2019-01-18.pdf"},
    {"collection": "guidance", "key": "guidance/30", "document_type": "Template", "version": "1", "file": "guidance/tool.exe"},
]


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    return make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)


@pytest.fixture
def export(tmp_path, pge):
    registry.create_program(None, prg_id="PGE_Ag_001", administrator=pge, valid_from=date(2020, 1, 1))
    files = tmp_path / "files"
    for row in ROWS:
        path = files / row["file"]
        if row["file"] == "gone.pdf":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ program" if path.suffix == ".exe" else b"PK workbook" if path.suffix == ".xlsx" else PDF)
    manifest = tmp_path / "manifest.csv"
    with open(manifest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(ROWS)
    return manifest, files


def run(export, *extra):
    manifest, files = export
    out = io.StringIO()
    call_command("documents_import_cedars", "--manifest", str(manifest), "--files", str(files), *extra, stdout=out)
    return out.getvalue()


def test_a_dry_run_checks_everything_and_stores_nothing(people, export):
    out = run(export, "--dry-run")
    assert out.startswith("Dry run: nothing stored.")
    assert "5 documents imported (8 versions, 10 files;" in out
    assert "files not found (their documents left out): gone.pdf" in out
    assert "deer-versions/2020/16" not in out and "no type 'Not a type'" in out
    assert "'NOPE' x1" in out and "'PGE_Missing_9' x1" in out
    assert not Document.objects.exists()


def test_cedars_documents_are_migrated_with_their_versions_dates_and_old_links(people, export, cpuc, pge, client):
    out = run(export)
    assert "5 documents imported (8 versions, 10 files; DEER resources 1, CPUC staff guidance 2, PA documents 1, Program files 1)" in out
    assert "10 old addresses lead to them" in out

    deer = Document.objects.get(cedars_key="deer:deer-versions/2020/16")
    assert (deer.collection, deer.document_type.label, deer.owner, deer.first_year) == ("deer", "DEER version", cpuc, 2020)
    v1, v2, v3 = deer.versions.order_by("number")
    assert [v.published for v in (v1, v2, v3)] == [True, True, True]
    assert timezone.localdate(v1.published_at) == date(2019, 1, 18) and timezone.localdate(v3.created_at) == date(2023, 2, 2)
    assert deer.published_at == v1.published_at and deer.created_at == v1.created_at
    assert v1.reason == "Migrated from CEDARS. In effect from 2020-01-01 to 2019-02-12. v1"
    assert v2.reason == "Migrated from CEDARS: version 2. In effect to 2023-02-01. v2"

    plan = Document.objects.get(cedars_key="program:history/1203")
    assert plan.owner == pge and list(plan.pas.all()) == [pge] and [p.prg_id for p in plan.programs.all()] == ["PGE_Ag_001"]
    v2 = plan.versions.get(number=2)
    assert [f.name for f in v2.files.all()] == [
        "implementation_plan_v2.pdf",
        "implementation_plan_redline_v2.pdf",
        "implementation_plan_change_summary_v2.pdf",
    ]
    jcm = Document.objects.get(cedars_key="pa:standalone/77")
    assert sorted(o.code for o in jcm.pas.all()) == ["BAYREN", "PGE"] and jcm.owner == pge
    assert jcm.versions.get().files.get().name == "JCM 2025.pdf"
    tool = Document.objects.get(cedars_key="guidance:guidance/30").versions.get().files.get()
    assert tool.name == "tool.exe.zip" and tool.legacy_name == "tool.exe"
    assert AuditEvent.objects.filter(action="documents.imported", actor_label="CEDARS migration").count() == 5

    files = {f.name: f for f in DocumentFile.objects.all()}
    expect = {
        DEER_HISTORY: deer.get_absolute_url(),
        "/deer-resources/deer-versions/2020/file/102/download/": files["errata_2019-02-13.pdf"].get_absolute_url() + "?view=1",
        "/documents/guidance-document/download/29/": files["2025_Annual_Report_Template_V1.2_Final.xlsx"].get_absolute_url(),
        "/documents/history/1203/": plan.get_absolute_url(),
        "/documents/download/3002/redlinechange_summary%7Cmain%7Credline)/": files["implementation_plan_redline_v2.pdf"].get_absolute_url()
        + "?view=1",
        "/deer-resources/": "/documents/?collection=deer",
        "/deer-resources/deemed-measure-packages/dispositions/": "/documents/?collection=deer",
        "/documents/guidance-document/": "/documents/?collection=guidance",
        "/documents/standalone/list/": "/documents/?collection=pa",
    }
    for old, new in expect.items():
        r = client.get(old)
        assert (r.status_code, r.get("Location")) == (301, iri_to_uri(new)), old
    for missing in [
        "/deer-resources/tools/energy-plus/resource/29/history/",
        "/deer-resources/tools/energy-plus/file/3195/download/",
        "/documents/download/9999/mainchange_summary%7Cmain%7Credline)/",
        "/documents/history/9999/",
        "/documents/guidance-document/download/2/",
    ]:
        assert client.get(missing).status_code == 404, missing
    page = client.get(deer.get_absolute_url()).content.decode()
    assert "Migrated from" in page and "CEDARS: its old links lead here" in page

    again = run(export)
    assert "0 documents imported" in again and "5 already imported" in again
    assert Document.objects.count() == 5 and LegacyLink.objects.count() == 10


def test_a_refused_type_is_kept_inside_a_zip(people, export, client):
    run(export)
    f = Document.objects.get(cedars_key="guidance:guidance/30").versions.get().files.get()
    data = b"".join(client.get(f.get_absolute_url()).streaming_content)
    assert zipfile.ZipFile(io.BytesIO(data)).read("tool.exe") == b"MZ program"


def test_a_manifest_without_its_columns_is_refused(people, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("collection,key\ndeer,1\n")
    with pytest.raises(CommandError, match="lacks the columns document_type, title, version, posted, file"):
        call_command("documents_import_cedars", "--manifest", str(bad), "--files", str(tmp_path), stdout=io.StringIO())


def test_old_addresses_are_kept_decoded_without_their_host():
    assert cedars_import.legacy_path("https://cedars.cpuc.ca.gov/documents/download/1/mainchange_summary%7Cmain%7Credline)/") == (
        "/documents/download/1/mainchange_summary|main|redline)/"
    )
    assert cedars_import.legacy_path("deer-resources/x/") == "/deer-resources/x/"

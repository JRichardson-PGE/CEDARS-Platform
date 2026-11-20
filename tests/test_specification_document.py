"""The specification document (cedars.services.specifications.printable): every version downloads from its page as a tagged PDF/UA-1
document made from the version on request, with its tables, fields, rules, built-in checks, value lists' codes and what changed from
the version before it. The fields-and-rules workbook carries the value lists' codes and the changes too."""

import copy
import io
import re

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

from cedars.services.accessibility import checks
from cedars.services.specifications import downloads, editor, historic, printable
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification, SpecificationVersion

from .test_document_accessibility import assert_accessible

pytestmark = pytest.mark.django_db
CODES = ("budget_filing", "claims", "monthly")


def current(code: str) -> SpecificationVersion:
    return specifications.current(Specification.objects.get(code=code))


def new_version(code: str, name: str, change=None, **kw) -> SpecificationVersion:
    doc = copy.deepcopy(specifications.stored_starting(code))
    doc["version"]["name"] = name
    if change:
        change(doc)
    return specifications.load(doc, **kw)


def text_of(body: bytes) -> str:
    """The PDF's text with its lines joined, so a sentence a table cell wraps is still found."""
    return re.sub(r"\s+", " ", " ".join(p.extract_text() for p in PdfReader(io.BytesIO(body)).pages))


def assert_tagged(body: bytes, title: str) -> None:
    result = checks.check_bytes(body, "specification.pdf")  # the platform's own check of uploaded documents
    assert result.checked and result.clear, [f.describe() for f in result.findings]
    r = PdfReader(io.BytesIO(body))
    root = r.trailer["/Root"]
    assert root["/MarkInfo"]["/Marked"] and "/StructTreeRoot" in root and root["/Lang"] == "en-US"
    assert r.metadata.title == title and root["/ViewerPreferences"].get("/DisplayDocTitle")
    xmp = root["/Metadata"].get_object().get_data().decode()
    assert "pdfuaid:part" in xmp and re.search(r"pdfuaid:part(>|=\")1", xmp), "declared PDF/UA-1"
    assert "/Outlines" in root, "bookmarks for the headings"


@pytest.mark.parametrize("code", CODES)
def test_every_current_version_is_a_tagged_pdf_ua_document_with_every_table_and_field(code):
    v = current(code)
    body = printable.pdf(v, base="https://cedars.example.org")
    assert_tagged(body, f"{v.specification.name}: {v.name}")
    text = text_of(body)
    assert f"{v.specification.name}: {v.name}" in text and "About this version" in text and "Built-in checks" in text
    for t in v.tables.prefetch_related("fields"):
        assert t.name in text
        for f in t.fields.all():
            assert f.name in text, f"{t.name}.{f.name}"
    for c in v.checks.all():
        assert c.rule_id in text
    assert f"https://cedars.example.org/specifications/{code}/{v.slug}/" in text
    assert "This is the first version of the specification" in text


def test_the_rules_are_written_out_and_the_changes_from_the_version_before(with_rules):
    v = with_rules
    rules = specifications.rules(v)
    assert rules
    text = text_of(printable.pdf(v))
    for r in rules[:25]:
        assert r.rule_id in text
    assert "What changed from Starting version" in text
    assert f"Added {rules[0].rule_id}" in text, "a rule the starting version did not take"


def test_a_removed_field_and_a_changed_example_are_listed_as_changes():
    def change(doc):
        t = next(t for t in doc["tables"] if t["name"] == "Commitments")
        t["fields"] = [f for f in t["fields"] if f["name"] != "Comments"]
        next(f for f in t["fields"] if f["name"] == "YearMonth")["example"] = "202901"

    v = new_version("monthly", "Monthly reports 2029", change)
    assert printable.prior_of(v) == current("monthly") or printable.prior_of(v).name == "Starting version"
    found = dict(printable.changes(v))
    assert "Commitments: removed Comments." in found["Fields"]
    assert any(i.startswith("Commitments: YearMonth.") and "202901" in i for i in found["Fields"])
    text = text_of(printable.pdf(v))
    assert "What changed from Starting version" in text and "Commitments: removed Comments." in text


def test_words_from_the_version_are_printed_as_they_are_never_read_as_layout():
    sneaky = 'Uses #panic("boom") and [brackets], *stars*, $math$, <label> @ref // not a comment \\ backslash'

    def change(doc):
        doc["tables"][0]["fields"][0]["definition"] = sneaky

    v = new_version("monthly", "Monthly reports 2030", change)
    assert sneaky.replace("\\", "") in text_of(printable.pdf(v)).replace("\\", "")


def test_a_value_list_made_in_a_draft_is_printed_with_its_codes(cpuc_admin):
    draft = editor.start_draft(cpuc_admin, current("monthly"), "Monthly reports 2031", "New codes")
    field = draft.tables.get(name="Measure").fields.get(name="PreDesc")
    editor.new_value_list(cpuc_admin, field, "pre_desc_kind", codes='A1, Alpha\nB2, "Beta, the second", 2028, 2030\n', reason="New codes")
    (lst,) = [x for x in printable.value_lists_of(draft) if x.name == "pre_desc_kind"]
    assert [c["code"] for c in lst.codes] == ["A1", "B2"]
    text = text_of(printable.pdf(draft))
    assert "a draft; not in use" in text
    assert "Beta, the second" in text and "2028 to 2030" in text


def test_a_long_value_list_is_pointed_to_not_printed(monkeypatch):
    pytest.importorskip("recet")  # the engine ships the lists the starting versions name
    monkeypatch.setattr(printable, "CODES_SHOWN", 3)
    v = current("claims")
    data = printable.content(v, base="https://cedars.example.org")
    long = next(x for x in data["lists"] if x["count"] > 3)
    assert not long["shown"] and long["codes"] == [] and long["page"].endswith(f"/dictionary/lists/{long['name']}/")
    assert "too many to print here" in text_of(printable.pdf(v))


def test_the_engines_codes_are_printed_with_the_years_they_are_accepted():
    pytest.importorskip("recet")
    v = current("claims")
    data = printable.content(v)
    lst = next(x for x in data["lists"] if x["shown"] and x["count"])
    code, _, years = lst["codes"][0]
    text = text_of(printable.pdf(v))
    assert code in text and years in text


def test_a_legacy_layout_downloads_as_a_document_too():
    v = next(iter(historic.versions()))
    body = printable.pdf(v)
    assert_tagged(body, f"{v.specification.name}: {v.name}")
    text = text_of(body)
    assert "Column, as CEDARS labels it" in text and "What changed" not in text and "Built-in checks" not in text


def test_the_version_page_links_the_document_and_it_downloads(client):
    v = current("claims")
    url = f"/specifications/claims/{v.slug}/specification.pdf"
    assert url in client.get(f"/specifications/claims/{v.slug}/").content.decode()
    r = client.get(url)
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
    assert r["Content-Disposition"] == f'attachment; filename="claims_{v.slug}.pdf"'
    assert r.content.startswith(b"%PDF-")


def test_a_draft_document_is_for_system_administrators_only(client, cpuc_admin):
    draft = new_version("monthly", "Monthly reports 2032", status="draft")
    assert client.get(f"/specifications/monthly/{draft.slug}/specification.pdf").status_code == 404


def test_the_fields_workbook_holds_the_value_lists_and_the_changes():
    def change(doc):
        t = next(t for t in doc["tables"] if t["name"] == "Commitments")
        t["fields"] = [f for f in t["fields"] if f["name"] != "Comments"]

    v = new_version("monthly", "Monthly reports 2033", change)
    data = downloads.fields_xlsx(v)
    assert_accessible(data)
    wb = load_workbook(io.BytesIO(data))
    assert [c.value for c in wb["Value lists"][1]] == ["Value list", "Field", "Code", "Description", "First year", "Last year"]
    changes = [[c.value for c in row] for row in wb["Changes"].iter_rows(min_row=2)]
    assert ["Starting version", "Fields", "Commitments: removed Comments."] in changes
    first = load_workbook(io.BytesIO(downloads.fields_xlsx(current("budget_filing"))))
    assert "first version" in first["Changes"]["C2"].value


@pytest.fixture
def with_rules(cpuc_admin):
    """A claims version that takes the engine's claims rule set, published in the rule library."""
    pytest.importorskip("recet")
    from cedars.services.rules import service as rules

    rs = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "claims"))
    return new_version("claims", "Claims 2029", rule_set=rs)

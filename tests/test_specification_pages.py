"""The filing specification pages, downloads and API (cedars.services.specifications views, downloads, api): published and withdrawn
versions are public, proposed ones are for anyone signed in, drafts for CPUC system administrators; every download is made from the
version, so the template, the fields workbook and the JSON say what the version says."""

import copy
import io
import json
import zipfile

import pytest
from openpyxl import load_workbook

from cedars.modules.budgets import jobfile
from cedars.modules.claims import spec as claims_spec
from cedars.services.specifications import downloads
from cedars.services.specifications import service as specifications
from cedars.services.specifications.models import Specification, SpecificationVersion

from .conftest import page_only, sign_in
from .test_claims import filer, programs, year  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
CODES = ("budget_filing", "claims", "monthly")


def current(code: str) -> SpecificationVersion:
    return specifications.current(Specification.objects.get(code=code))


def new_version(code: str, name: str, status: str = "published", change=None) -> SpecificationVersion:
    """A second version of a specification, made from its starting document with a change."""
    doc = copy.deepcopy(specifications.stored_starting(code))
    doc["version"]["name"] = name
    if change:
        change(doc)
    return specifications.load(doc, status=status)


def drop_comments(doc):
    """Monthly: Commitments loses its Comments field and its YearMonth gets an example."""
    t = next(t for t in doc["tables"] if t["name"] == "Commitments")
    t["fields"] = [f for f in t["fields"] if f["name"] != "Comments"]
    next(f for f in t["fields"] if f["name"] == "YearMonth")["example"] = "202901"


# --- the pages


def test_the_index_lists_every_specification_and_its_current_version_to_everyone(client):
    html = client.get("/specifications/").content.decode()
    for code in CODES:
        v = current(code)
        assert v.specification.name in html
        assert f"/specifications/{code}/{v.slug}/" in html


@pytest.mark.parametrize("code", CODES)
def test_a_version_page_shows_its_tables_fields_and_checks(client, code):
    v = current(code)
    r = client.get(f"/specifications/{code}/{v.slug}/")
    assert r.status_code == 200
    html = r.content.decode()
    for t in v.tables.all():
        assert f'id="table-{t.name}"' in html
        for f in t.fields.all():
            assert f'id="field-{t.name}-{f.name}"' in html
    for c in v.checks.all():
        assert f'id="check-{c.rule_id}"' in html
    assert "The current version" in html


def test_the_claims_page_says_which_tables_are_kept_in_the_vault(client):
    html = client.get(f"/specifications/claims/{current('claims').slug}/").content.decode()
    assert "kept in the PII vault" in html
    assert "PII upload template" in html


@pytest.fixture
def with_rules(cpuc_admin):
    """A claims version that takes the engine's claims rule set, published in the rule library."""
    from cedars.services.rules import service as rules

    rs = rules.publish(cpuc_admin, rules.import_from_engine(cpuc_admin, "claims"))
    doc = copy.deepcopy(specifications.stored_starting("claims"))
    doc["version"]["name"] = "Claims 2029"
    return specifications.load(doc, rule_set=rs)


def test_the_rules_about_a_field_are_shown_beside_it_in_words(client, with_rules):
    v = with_rules
    by_field, _ = specifications.rules_by_field(v)
    assert by_field, "the claims rule set has rules about named fields"
    (table, field), rules = next(iter(by_field.items()))
    html = client.get(f"/specifications/claims/{v.slug}/").content.decode()
    row = html.split(f'id="field-{table}-{field}"', 1)[1].split("</tr>", 1)[0]
    assert rules[0].rule_id in row


def test_current_follows_the_newest_published_version(client):
    assert client.get("/specifications/monthly/current/")["Location"] == f"/specifications/monthly/{current('monthly').slug}/"
    v = new_version("monthly", "Monthly reports 2029", change=drop_comments)
    assert client.get("/specifications/monthly/current/")["Location"] == f"/specifications/monthly/{v.slug}/"
    html = client.get("/specifications/monthly/starting-version/").content.decode()
    assert "A newer version is published" in html


def test_drafts_are_for_system_administrators_and_proposed_versions_for_anyone_signed_in(client, cpuc_admin, pge_admin):
    draft = new_version("monthly", "Monthly reports 2030", status="draft")
    url = f"/specifications/monthly/{draft.slug}/"
    assert client.get(url).status_code == 404
    assert client.get(f"/api/v1/specifications/monthly/{draft.slug}").status_code == 404
    assert client.get(f"/specifications/monthly/{draft.slug}/specification.json").status_code == 404
    assert "Monthly reports 2030" not in client.get("/specifications/").content.decode()
    sign_in(client, pge_admin)
    assert client.get(url).status_code == 404
    sign_in(client, cpuc_admin)
    assert client.get(url).status_code == 200
    assert "Monthly reports 2030" in page_only(client.get("/specifications/").content.decode())

    SpecificationVersion.objects.filter(pk=draft.pk).update(status="proposed")
    client.logout()
    assert client.get(url).status_code == 404
    sign_in(client, pge_admin)
    assert client.get(url).status_code == 200
    assert client.get(f"/api/v1/specifications/monthly/{draft.slug}").status_code == 404  # the API gives public versions only


def test_an_unknown_version_or_download_is_not_found(client):
    assert client.get("/specifications/claims/no-such-version/").status_code == 404
    assert client.get("/specifications/nothing/current/").status_code == 404
    assert client.get("/specifications/claims/starting-version/template.exe").status_code == 404
    assert client.get("/specifications/claims/starting-version/schema/NoSuchTable.json").status_code == 404
    assert client.get("/specifications/budget_filing/starting-version/template.pii-xlsx").status_code == 404
    assert client.get("/specifications/claims/starting-version/examples/none.xlsx").status_code == 404


# --- comparing two versions


def test_compare_names_what_changed_between_two_versions(client):
    v = new_version("monthly", "Monthly reports 2029", change=drop_comments)
    starting = SpecificationVersion.objects.get(specification__code="monthly", slug="starting-version")
    result = specifications.compare(starting, v)
    assert [(t, f.name) for t, f in result.fields_removed] == [("Commitments", "Comments")]
    assert result.fields_changed == [("Commitments", "YearMonth", [("Example", "", "202901")])]
    assert not result.tables_added and not result.tables_removed
    html = client.get(f"/specifications/compare/?a=monthly/starting-version&b=monthly/{v.slug}").content.decode()
    assert "Comments" in html and "202901" in html


def test_compare_of_a_version_with_itself_finds_nothing(client):
    v = current("claims")
    assert specifications.compare(v, v).empty
    assert client.get(f"/specifications/compare/?a=claims/{v.slug}&b=claims/{v.slug}").status_code == 200


def test_compare_without_two_readable_versions_says_so(client):
    draft = new_version("monthly", "Monthly reports 2030", status="draft")
    r = client.get(f"/specifications/compare/?a=monthly/starting-version&b=monthly/{draft.slug}")
    assert r.status_code == 200
    assert "Monthly reports 2030" not in page_only(r.content.decode())
    assert r.context["missing"]


# --- the downloads


def test_the_claims_template_from_the_version_has_the_modules_sheets_and_columns(client):
    """The version's template holds the tables and the columns the claims module reads from it."""
    v = current("claims")
    r = client.get(f"/specifications/claims/{v.slug}/template.xlsx")
    assert r["Content-Disposition"] == 'attachment; filename="claims_starting-version_template.xlsx"'
    ours = load_workbook(io.BytesIO(r.content), read_only=True)
    lay = claims_spec.of_version(v)
    assert ours.sheetnames == ["Fields", *lay.TABLES]
    for name in lay.TABLES:
        assert list(next(ours[name].iter_rows(values_only=True))) == lay.upload_columns(name)


def test_the_pii_template_holds_only_the_vault_tables(client):
    v = current("claims")
    book = load_workbook(io.BytesIO(client.get(f"/specifications/claims/{v.slug}/template.pii-xlsx").content), read_only=True)
    assert book.sheetnames == ["Fields", "PII_Contact", "PII_Site"]
    with zipfile.ZipFile(io.BytesIO(client.get(f"/specifications/claims/{v.slug}/template.pii-zip").content)) as z:
        assert z.namelist() == ["PII_Contact.csv", "PII_Site.csv"]


def test_the_budget_template_zip_has_the_required_columns(client):
    with zipfile.ZipFile(io.BytesIO(client.get("/specifications/budget_filing/starting-version/template.zip").content)) as z:
        assert sorted(z.namelist()) == sorted(f"{t}.csv" for t in jobfile.TABLES)
        for t in jobfile.TABLES:
            header = z.read(f"{t}.csv").decode().strip().split("|")
            assert set(specifications.layout_for_module("budgets").table(t).refuse_without) <= set(header)


def test_the_fields_and_rules_workbook_lists_every_field_rule_and_check(client, with_rules):
    v = with_rules
    assert specifications.rules(v)
    book = load_workbook(io.BytesIO(client.get(f"/specifications/claims/{v.slug}/fields-and-rules.xlsx").content), read_only=True)
    assert book.sheetnames == ["Tables", "Fields", "Rules", "Built-in checks", "Settings"]
    count = lambda s: sum(1 for r in book[s].iter_rows(min_row=2, values_only=True) if r[0])  # noqa: E731
    assert count("Tables") == v.tables.count()
    assert count("Fields") == sum(t.fields.count() for t in v.tables.all())
    assert count("Rules") == len(specifications.rules(v))
    assert count("Built-in checks") == v.checks.count()


@pytest.mark.parametrize("code", CODES)
def test_the_json_document_is_the_version_and_the_api_gives_the_same(client, code):
    v = current(code)
    from_page = json.loads(client.get(f"/specifications/{code}/{v.slug}/specification.json").content)
    from_api = client.get(f"/api/v1/specifications/{code}/{v.slug}").json()
    assert from_page == from_api
    assert from_api["format_name"] == "cedars-filing-specification"
    assert from_api["fingerprint"] == v.fingerprint == specifications.fingerprint(specifications.as_document(v))
    assert [t["name"] for t in from_api["tables"]] == [t.name for t in v.tables.all()]
    assert all(t["schema"].endswith(f"/api/v1/specifications/{code}/{v.slug}/schema/{t['name']}") for t in from_api["tables"])
    assert client.get(f"/api/v1/specifications/{code}/current").json()["fingerprint"] == v.fingerprint


def test_the_api_index_lists_the_public_versions(client):
    new_version("monthly", "Monthly reports 2030", status="draft")
    body = client.get("/api/v1/specifications/").json()
    assert [s["code"] for s in body["results"]] == ["budget_filing", "claims", "monthly"]
    monthly = body["results"][2]
    assert [x["name"] for x in monthly["versions"]] == ["Starting version"]
    assert monthly["versions"][0]["current"] is True
    assert monthly["current"].endswith("/api/v1/specifications/monthly/current")


def test_a_tables_json_schema_requires_its_required_fields(client):
    v = current("claims")
    t = v.tables.get(name="Measure")
    schema = client.get(f"/api/v1/specifications/claims/{v.slug}/schema/Measure").json()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert list(schema["properties"]) == [f.name for f in t.fields.all()]
    assert schema["required"] == [f.name for f in t.fields.all() if f.requirement == "required"]
    assert schema == json.loads(client.get(f"/specifications/claims/{v.slug}/schema/Measure.json").content)
    assert schema == downloads.json_schema(v, t)


def test_a_withdrawn_version_stays_readable_and_says_why(client):
    v = new_version("monthly", "Monthly reports 2029", change=drop_comments)
    from django.utils import timezone

    SpecificationVersion.objects.filter(pk=v.pk).update(status="withdrawn", withdrawn_at=timezone.now(), withdrawn_reason="Replaced.")
    html = client.get(f"/specifications/monthly/{v.slug}/").content.decode()
    assert "Withdrawn" in html and "Replaced." in html
    assert client.get("/specifications/monthly/current/")["Location"] == "/specifications/monthly/starting-version/"
    assert client.get(f"/api/v1/specifications/monthly/{v.slug}").json()["status"] == "withdrawn"


# --- the filing pages name the specification


def test_a_filing_page_links_the_specification_it_follows(client, filer, year, pge, programs):  # noqa: F811
    from django.urls import reverse

    from cedars.modules.claims import service as claims

    from .test_claims import deadline, q1

    d = deadline()
    url = reverse("claims:period", args=[d.pk])
    sign_in(client, filer)
    html = page_only(client.get(url).content.decode())
    assert 'href="/specifications/claims/starting-version/"' in html
    assert "the specification it follows, Quarterly claims: Starting version" in html
    claims.upload_claims(filer, d, pge, q1(), "q1.zip")
    html = page_only(client.get(url).content.decode())
    assert ', under <a href="/specifications/claims/starting-version/">' in html

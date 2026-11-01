"""Program field guidance (BR-13.7) and placeholder implementation plans (BR-13.6): every program field has its definition (the data
dictionary's where it has the field), how to fill it in and its valid values, on /programs/fields/, under each field of the change
form and from each label on the program page; an implementation plan's version can be filed as a placeholder, which the program page
shows until a full plan replaces it."""

import csv
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import InternalError, ProgrammingError, transaction
from django.utils import timezone
from django.utils.html import escape

from cedars.modules.documents import service as documents
from cedars.modules.documents.models import DocumentVersion
from cedars.modules.programs import forms as program_forms
from cedars.services.dictionary import service as dictionary
from cedars.services.registry import field_guide
from cedars.services.registry import service as registry

from .conftest import page_only

pytestmark = pytest.mark.django_db
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).parent / "data"
CATALOG = sorted((ROOT / "data" / "dictionary").glob("recet-*.json"))[-1]
PDF = b"%PDF-1.7\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


@pytest.fixture
def hvac(pge, pge_admin):
    return registry.create_program(
        pge_admin,
        prg_id="PGE_Res_HVAC",
        administrator=pge,
        valid_from=date(2024, 1, 1),
        name="Residential HVAC",
        status="active",
        segment="resource_acquisition",
        primary_sector="Residential",
        implementer="Third/Local Party",
        sectors={"Residential": 1.0},
        start_year=2024,
    )


@pytest.fixture
def catalog():
    return dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))


# --- the guide itself


def test_every_program_field_has_guidance_and_every_guide_entry_is_a_field():
    names = [e.name for e in field_guide.entries()]
    assert set(registry.VERSION_FIELDS) <= set(names)
    assert set(names) - set(registry.VERSION_FIELDS) == {"prg_id", "administrator"}
    assert set(program_forms.ProgramChangeForm.ATTRIBUTES) <= set(names)
    for e in field_guide.entries():
        assert e.label and e.definition and e.guidance, e.name
        assert e.values or e.values_note or e.name.startswith("exclude_"), e.name
        if e.name in program_forms.ProgramChangeForm.ATTRIBUTES:
            assert e.hint, e.name


def test_the_guide_names_only_fields_the_data_dictionary_has():
    shipped = {f["name"] for f in json.loads(CATALOG.read_text(encoding="utf-8"))["fields"]}
    named = [name for e in field_guide.entries() for name in e.dictionary]
    assert named and set(named) <= shipped


def test_the_catalog_filters_offer_the_guides_values():
    assert [c for c, _ in program_forms.STATUS_CHOICES[1:]] == [v.code for v in field_guide.entry("status").values]
    assert [c for c, _ in program_forms.SEGMENT_CHOICES[1:]] == [v.code for v in field_guide.entry("segment").values]


def test_every_value_in_cedars_programs_list_is_a_listed_value():
    """No program imported from CEDARS' list would show a value the guide does not know."""
    columns = {
        "status": "ProgramStatus",
        "segment": "ProgramSegment",
        "primary_sector": "PrimarySector",
        "implementer": "ProgramImplementer",
        "category": "ProgramCategory",
    }
    with (DATA / "cedars_programs_20261003.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for name, column in columns.items():
        found = {r[column].strip() for r in rows if r.get(column, "").strip()}
        assert found <= {v.code for v in field_guide.entry(name).values}, (name, found)


def test_definitions_come_from_the_data_dictionary_where_it_has_the_field(catalog):
    status = field_guide.definitions(field_guide.entry("status"))
    assert [d.dictionary_field for d in status] == ["Prg_ProgramStatus"] and status[0].catalog == catalog.label
    assert status[0].text.startswith("active, new, closed")
    website = field_guide.definitions(field_guide.entry("website"))  # not in the dictionary: the guide's own
    assert website[0].dictionary_field == "" and "public web page" in website[0].text
    assert len(field_guide.definitions(field_guide.entry("flags"))) == 10


def test_without_a_catalog_the_guides_own_definitions_are_shown():
    d = field_guide.definitions(field_guide.entry("status"))
    assert d[0].dictionary_field == "" and d[0].text == field_guide.entry("status").definition


def _retire(monkeypatch, name, code, on):
    entries = tuple(
        replace(e, values=tuple(replace(v, retired_from=on) if v.code == code else v for v in e.values)) if e.name == name else e
        for e in field_guide.entries()
    )
    monkeypatch.setattr(field_guide, "entries", lambda: entries)


def test_a_retired_value_is_offered_only_to_a_program_that_has_it(monkeypatch):
    _retire(monkeypatch, "implementer", "Third/Local Party", date(2026, 1, 1))
    day = date(2026, 6, 1)
    assert ("Third/Local Party", "Third/Local Party (retired)") in field_guide.choices("implementer", current="Third/Local Party", day=day)
    assert "Third/Local Party" not in [c for c, _ in field_guide.choices("implementer", day=day)]
    assert "Third/Local Party" in [c for c, _ in field_guide.choices("implementer", day=date(2025, 12, 31))]
    unknown = field_guide.choices("category", current="Something old", day=day)
    assert ("Something old", "Something old (not in the list)") in unknown


# --- the guide page


def test_the_program_fields_page_is_public_and_gives_definition_guidance_and_values(client, catalog, hvac):
    html = page_only(client.get("/programs/fields/").content.decode())
    for e in field_guide.entries():
        assert f'id="{e.anchor}"' in html and f'href="#{e.anchor}"' in html
    assert "/dictionary/fields/Prg_ProgramStatus/" in html and catalog.label in html
    assert "Closed to new commitments" in html and "Write each sector the program serves" in html
    assert "<code>ProgramImplementer</code>" in html


def test_the_page_counts_programs_and_names_values_not_in_the_list(client, monkeypatch, hvac, pge_admin, pge):
    registry.create_program(pge_admin, prg_id="PGE_X", administrator=pge, valid_from=date(2024, 1, 1), name="X", category="Something old")
    _retire(monkeypatch, "implementer", "Third/Local Party", date(2020, 1, 1))
    guide = {g["entry"].name: g for g in field_guide.guide()}
    assert ("Something old", 1) in guide["category"]["unlisted"]
    third = next(row for row in guide["implementer"]["values"] if row[0].code == "Third/Local Party")
    assert third[1:] == (False, 1)  # retired, one program
    html = page_only(client.get("/programs/fields/").content.decode())
    assert "Something old (1 program)" in html and "Retired" in html


def test_a_locked_field_says_so(client, cpuc_admin, hvac):
    registry.set_locked_fields(cpuc_admin, ["segment"], effective=timezone.localdate(), reason="test")
    assert next(g for g in field_guide.guide() if g["entry"].name == "segment")["locked"]
    assert "<strong>Locked</strong>" in client.get("/programs/fields/").content.decode()


# --- the change form and the program page


def test_the_change_form_shows_how_to_fill_in_each_field_and_offers_the_listed_values(signed_in, pge_admin, hvac):
    c = signed_in(pge_admin)
    r = c.get("/programs/PGE_Res_HVAC/change")
    form = r.context["form"]
    for name in program_forms.ProgramChangeForm.ATTRIBUTES:
        e = field_guide.entry(name)
        assert escape(e.hint) in str(form.fields[name].help_text) and f"/programs/fields/#{e.anchor}" in str(form.fields[name].help_text)
    assert [c for c, _ in form.fields["primary_sector"].choices][1:] == [v.code for v in field_guide.entry("primary_sector").values]
    html = r.content.decode()
    assert 'aria-describedby="id_primary_sector_helptext"' in html and "More about primary sector (opens in a new tab)" in html


def test_a_value_not_in_the_list_is_refused_and_a_listed_one_saved(signed_in, pge_admin, hvac):
    c = signed_in(pge_admin)
    form = c.get("/programs/PGE_Res_HVAC/change").context["form"]
    data = {k: v for k, v in form.initial.items() if v not in (None, False)}
    data.update(effective=(timezone.localdate() + timedelta(days=5)).isoformat(), reason="new contract", category="Not a category")
    r = c.post("/programs/PGE_Res_HVAC/change", data)
    assert r.status_code == 200 and "category" in r.context["form"].errors
    data["category"] = "New Construction"
    assert c.post("/programs/PGE_Res_HVAC/change", data).status_code == 302
    assert registry.version_on(hvac, timezone.localdate() + timedelta(days=5)).category == "New Construction"


def test_a_program_keeps_its_retired_value_when_another_field_changes(signed_in, monkeypatch, pge_admin, hvac):
    _retire(monkeypatch, "implementer", "Third/Local Party", date(2020, 1, 1))
    c = signed_in(pge_admin)
    form = c.get("/programs/PGE_Res_HVAC/change").context["form"]
    assert ("Third/Local Party", "Third/Local Party (retired)") in form.fields["implementer"].choices
    data = {k: v for k, v in form.initial.items() if v not in (None, False)}
    data.update(effective=(timezone.localdate() + timedelta(days=5)).isoformat(), reason="renamed", name="Residential HVAC Plus")
    assert c.post("/programs/PGE_Res_HVAC/change", data).status_code == 302
    assert registry.version_on(hvac, timezone.localdate() + timedelta(days=5)).implementer == "Third/Local Party"


def test_the_program_page_links_each_field_to_its_guidance(client, hvac):
    html = client.get("/programs/PGE_Res_HVAC/").content.decode()
    for name in ("status", "segment", "primary_sector", "implementer", "sectors", "administrator"):
        assert f'href="/programs/fields/#field-{name}"' in html, name
    assert 'href="/programs/fields/">Program fields</a>' in html


# --- placeholder implementation plans (BR-13.6)


def _plan_type():
    return documents.type_list("program").options.get(label="Implementation plan")


def _upload(name):
    return documents.Upload(name, SimpleUploadedFile(name, PDF))


def _plan(pge, hvac, *, placeholder=True, publish=True):
    return documents.create_document(
        None,
        pge,
        "program",
        documents.Details(document_type=_plan_type(), title="Residential HVAC implementation plan", programs=[hvac], first_year=2028),
        [_upload("ip.pdf")],
        publish=publish,
        placeholder=placeholder,
    )


def test_a_placeholder_plan_is_shown_on_the_program_page_until_the_full_plan_replaces_it(client, cpuc_admin, pge, hvac):
    doc = _plan(pge, hvac)
    v1 = doc.versions.get()
    assert v1.placeholder and documents.is_placeholder(None, doc)
    html = page_only(client.get("/programs/PGE_Res_HVAC/").content.decode())
    assert "Placeholder plan" in html and "is a placeholder: the full plan is still to come" in html
    assert "Placeholder plan" in client.get(doc.get_absolute_url()).content.decode()
    full = documents.add_version(cpuc_admin, None, doc, [_upload("ip_full.pdf")], reason="The full plan")
    assert not full.placeholder
    assert "Placeholder plan" in page_only(client.get("/programs/PGE_Res_HVAC/").content.decode())  # not published yet
    documents.publish_version(cpuc_admin, full)
    html = page_only(client.get("/programs/PGE_Res_HVAC/").content.decode())
    assert "Placeholder plan" not in html and "is a placeholder" not in html
    doc_html = client.get(doc.get_absolute_url()).content.decode()
    assert "Placeholder plan" not in doc_html and '<span class="status warn">Placeholder</span>' in doc_html  # version 1 still says so
    e = doc.versions.get(number=1)
    assert e.placeholder


def test_only_an_implementation_plan_is_filed_as_a_placeholder(pge, hvac):
    evaluation = documents.type_list("program").options.get(label="Evaluation")
    with pytest.raises(documents.DocumentError, match="Only an implementation plan"):
        documents.create_document(
            None, pge, "program", documents.Details(document_type=evaluation, title="E"), [_upload("e.pdf")], placeholder=True
        )
    doc = documents.create_document(None, pge, "program", documents.Details(document_type=evaluation, title="E"), [_upload("e.pdf")])
    with pytest.raises(documents.DocumentError):
        documents.add_version(None, None, doc, [_upload("e2.pdf")], reason="again", placeholder=True)


def test_a_published_versions_placeholder_flag_never_changes(pge, hvac):
    v = _plan(pge, hvac).versions.get()
    with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
        DocumentVersion.objects.filter(pk=v.pk).update(placeholder=False)
    draft = _plan(pge, hvac, publish=False).versions.get()
    DocumentVersion.objects.filter(pk=draft.pk).update(placeholder=False)  # an unpublished version still may


def test_the_forms_offer_the_placeholder_box_for_program_files_and_plans_only(signed_in, cpuc_admin, pge, hvac):
    plan = _plan(pge, hvac)
    c = signed_in(cpuc_admin)
    assert "placeholder" in c.get(f"/documents/{plan.pk}/versions/new/").context["form"].fields
    assert "placeholder" in documents_new_fields(c, "program") and "placeholder" not in documents_new_fields(c, "pa")
    guidance = documents.create_document(
        None, pge, "pa", documents.Details(document_type=documents.type_list("pa").options.first(), title="G"), [_upload("g.pdf")]
    )
    assert "placeholder" not in c.get(f"/documents/{guidance.pk}/versions/new/").context["form"].fields


def documents_new_fields(client, collection):
    r = client.get(f"/documents/new/?collection={collection}")
    return set(r.context["form"].fields) if r.status_code == 200 else set()

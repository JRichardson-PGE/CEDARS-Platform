"""Pick, don't type (UI review 3): the shared program, field and member pickers in cedars/core/pickers.py, and the pages that use them
(the records page and the dashboards, the studies and library filters, Link a record on a mandated change)."""

import json
from datetime import date
from pathlib import Path

import pytest
from django import forms

from cedars.core import pickers
from cedars.core.pickers import Choice
from cedars.services.accounts import service as access
from cedars.services.dictionary import service as dictionary
from cedars.services.registry import service as registry
from cedars.services.releases import mandates
from cedars.services.rules.models import RuleSet

pytestmark = pytest.mark.django_db
CATALOG = Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.94.json"
CHOICES = [Choice("PGE21001", "Home Energy Reports"), Choice("PGE21002", "Commercial HVAC"), Choice("SCE13001", "Home Energy Reports")]


@pytest.fixture
def programs(pge):
    hvac = registry.create_program(None, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1), name="Residential HVAC")
    lights = registry.create_program(
        None, prg_id="PGE_Com_Light", administrator=pge, valid_from=date(2024, 1, 1), name="Commercial lighting"
    )
    return hvac, lights


def test_what_was_typed_is_read_as_a_choice():
    assert pickers.find("pge21001", CHOICES).value == "PGE21001"  # any case
    assert pickers.find("PGE21002 Commercial HVAC", CHOICES).value == "PGE21002"  # a value with its label, as a browser may fill it
    assert pickers.find("commercial hvac", CHOICES).value == "PGE21002"  # a label only one choice has
    assert pickers.find("Home Energy Reports", CHOICES) is None  # two programs have that name
    assert pickers.find("", CHOICES) is None


def test_text_that_names_nothing_names_the_near_matches():
    assert [c.value for c in pickers.near_matches("PGE2100", CHOICES)] == ["PGE21001", "PGE21002"]
    assert {c.value for c in pickers.near_matches("PGE21003", CHOICES)} == {"PGE21001", "PGE21002"}
    assert pickers.not_found("PGE2103", CHOICES[:1], "program") == "No program is “PGE2103”. Did you mean PGE21001 (Home Energy Reports)?"
    assert pickers.not_found("zzz", CHOICES, "program") == "No program is “zzz”. Pick one from the list as you type."
    said = pickers.not_found("home energy", CHOICES, "program")
    assert "PGE21001 (Home Energy Reports) or SCE13001 (Home Energy Reports)" in said


def test_a_filters_program_values_read_as_prg_ids():
    assert pickers.resolve_programs(["pge21001", "PGE21002, sce13001"], CHOICES) == ["PGE21001", "PGE21002", "SCE13001"]
    assert pickers.resolve_programs(["PGE21001 PGE21002"], CHOICES) == ["PGE21001", "PGE21002"]  # separated by spaces
    assert pickers.resolve_programs(["Commercial HVAC", "NOPE", "", "pge21001"], CHOICES) == ["PGE21002", "NOPE", "PGE21001"]
    assert pickers.resolve_programs([], CHOICES) == []


def test_the_program_choices_come_from_the_registry(programs, pge, cpuc_admin):
    sce = access.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    registry.create_program(None, prg_id="SCE_Res", administrator=sce, valid_from=date(2024, 1, 1), name="SCE residential")
    registry.create_program(None, prg_id="PGE_Cost", administrator=pge, valid_from=date(2024, 1, 1), portfolio_cost=True)
    every = {c.value: c for c in pickers.program_choices()}
    assert set(every) == {"PGE_Res_HVAC", "PGE_Com_Light", "SCE_Res"}  # portfolio-cost IDs are not programs to pick
    assert every["PGE_Res_HVAC"].label == "Residential HVAC" and every["PGE_Res_HVAC"].note == "PGE"
    assert {c.value for c in pickers.program_choices(pa=pge)} == {"PGE_Res_HVAC", "PGE_Com_Light"}
    assert {c.value for c in pickers.program_choices(pa="SCE")} == {"SCE_Res"}


class _Form(forms.Form):
    program = pickers.ProgramField(label="Program", required=False)


def test_the_program_field_draws_a_list_and_names_near_matches(programs):
    html = str(_Form()["program"])
    assert 'list="id_program-list"' in html and '<datalist id="id_program-list">' in html
    assert '<option value="PGE_Res_HVAC">Residential HVAC · PGE</option>' in html
    f = _Form({"program": "pge_res_hvac"})
    assert f.is_valid() and f.cleaned_data["program"] == "PGE_Res_HVAC"
    f = _Form({"program": "residential hvac"})
    assert f.is_valid() and f.cleaned_data["program"] == "PGE_Res_HVAC"
    f = _Form({"program": "PGE_Res_HVX"})
    assert not f.is_valid() and f.errors["program"] == ["No program is “PGE_Res_HVX”. Did you mean PGE_Res_HVAC (Residential HVAC)?"]
    assert _Form({"program": ""}).is_valid()


def test_the_field_and_member_pickers(pge, pge_admin, make_user):
    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))

    class F(forms.Form):
        field = pickers.FieldField(label="Field tested")
        member = pickers.MemberField(label="Member")

        def __init__(self, *args, organisation=None, **kwargs):
            super().__init__(*args, **kwargs)
            self.fields["member"].set_organisation(organisation)

    names = {c.value for c in pickers.field_choices()}
    assert "AC_Climate_Zone" in names and "NumUnits" in names
    assert {c.value for c in pickers.member_choices(pge)} == {"admin@pge.example.org"}
    f = F({"field": "ac_climate_zone", "member": "pat admin"}, organisation=pge)
    assert f.is_valid(), f.errors
    assert f.cleaned_data == {"field": "AC_Climate_Zone", "member": "admin@pge.example.org"}
    outsider = make_user("someone@example.org", "Sam Someone")
    f = F({"field": "AC_Climate_Zon", "member": outsider.email}, organisation=pge)
    assert not f.is_valid()
    assert "Did you mean AC_Climate_Zone" in f.errors["field"][0] and f.errors["member"][0].startswith("No member is")
    assert "Pat Admin" in str(F(organisation=pge)["member"])  # each form's own organization's members
    assert "Pat Admin" not in str(F()["member"])


def test_the_records_page_picks_programs(client, programs):
    html = client.get("/data/records/?dataset=claims&program=pge_res_hvac&program=PGE_Com_Light").content.decode()
    assert "data-program-picker" in html and '<datalist id="records-program-list">' in html
    assert 'name="program" value="PGE_Res_HVAC" checked' in html and 'name="program" value="PGE_Com_Light" checked' in html
    assert "(2 programs chosen)" in html and "PrgIDs, separated by commas" not in html
    html = client.get("/data/records/?dataset=claims&program=PGE_Res_HVX").content.decode()
    assert "No program is “PGE_Res_HVX”. Did you mean PGE_Res_HVAC (Residential HVAC)?" in html
    r = client.post("/data/records/", {"dataset": "claims", "program": ["Residential HVAC", ""], "columns": "none"})
    assert r.status_code == 302 and "program=PGE_Res_HVAC" in r["Location"]


def test_the_dashboards_pick_programs(client, programs):
    from django.urls import reverse

    from cedars.modules.data import dashboard

    page = reverse("data:dashboard", args=[next(iter(dashboard.PAGES))])
    html = client.get(page, {"program": ["pge_res_hvac, Commercial lighting"]}).content.decode()
    assert '<datalist id="dash-program-list">' in html
    assert 'name="program" value="PGE_Res_HVAC" checked' in html and 'name="program" value="PGE_Com_Light" checked' in html


def test_the_studies_filter_picks_a_program(client, programs):
    html = client.get("/documents/studies/").content.decode()
    assert '<datalist id="id_program-list">' in html and "Program evaluated" in html
    html = client.get("/documents/studies/", {"program": "PGE_Res_HVX"}).content.decode()
    assert "Did you mean PGE_Res_HVAC (Residential HVAC)?" in html


def test_the_library_filter_picks_a_program(client, programs):
    html = client.get("/documents/", {"program": "pge_com_lite"}).content.decode()
    assert "Did you mean PGE_Com_Light (Commercial lighting)?" in html and '<datalist id="id_program-list">' in html


def test_link_a_record_picks_it_from_a_list(client, signed_in, cpuc_admin):
    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))
    rules = RuleSet.objects.create(code="claims", label="Claims 2026", effective_from=date(2026, 3, 12))
    RuleSet.objects.filter(pk=rules.pk).update(status="published")
    RuleSet.objects.create(code="claims", label="Claims 2027 draft", effective_from=date(2027, 1, 1))
    entry = mandates.create_draft(
        cpuc_admin,
        reference="Res. E-5350",
        authority="resolution",
        title="Specification change log",
        requirement="Keep a public register.",
        effective_on=date(2026, 1, 1),
        document_url="https://docs.cpuc.ca.gov/PublishedDocs/Published/G000/M000/K000/000000.PDF",
    )
    signed_in(cpuc_admin)
    url = f"/mandates/manage/{entry.pk}/"
    html = client.get(url).content.decode()
    assert 'data-reveal-for="rule_set"' in html and f'<option value="{rules.pk}">Claims 2026, in force from 2026-03-12</option>' in html
    assert "Claims 2027 draft" not in html  # only published records are offered
    assert '<datalist id="link_key_field-list">' in html and "No published release note yet" in html
    assert "address pasted" not in html
    r = client.post(url, {"action": "link", "kind": "rule_set", "key_rule_set": ""})
    assert r.status_code == 200 and "Choose the rule set to link." in r.content.decode()
    r = client.post(url, {"action": "link", "kind": "rule_set", "key_rule_set": str(rules.pk), "key_field": "ignored"})
    assert r.status_code == 302, [ln for ln in r.content.decode().splitlines() if "error" in ln or "alert" in ln]
    assert entry.links.get().key == str(rules.pk)
    r = client.post(url, {"action": "link", "kind": "field", "key_field": "e3climatezone"})
    assert r.status_code == 302 and entry.links.filter(kind="field").get().key == "AC_Climate_Zone"
    r = client.post(url, {"action": "link", "kind": "field", "key_field": "AC_Climate_Zon"})
    assert "Did you mean AC_Climate_Zone" in r.content.decode()

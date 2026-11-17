"""Documents and EM&V (UI review 3): the library shows its list first with key dates to the side, File a document is four short steps
that pick programs and studies from lists and fold each list of values, and Add a study folds its lists too."""

from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from cedars.modules.documents import forms, service, studies
from cedars.modules.documents.models import Document
from cedars.services.registry import service as registry
from tests.conftest import page_only
from tests.test_documents import PDF, details, make_people, option, upload

pytestmark = pytest.mark.django_db


@pytest.fixture
def people(cpuc_admin, cpuc, pge, pge_admin, make_user):
    return make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)


@pytest.fixture
def programs(pge):
    hvac = registry.create_program(None, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1), name="Residential HVAC")
    lights = registry.create_program(
        None, prg_id="PGE_Com_Light", administrator=pge, valid_from=date(2024, 1, 1), name="Commercial lighting"
    )
    return hvac, lights


@pytest.fixture
def study(people):
    d = studies.StudyData(title="Residential HVAC impact evaluation", manager=people["dnv"], lead=option("study_lead", "CPUC"))
    return studies.create_study(people["ed"], None, d)


def test_the_library_lists_documents_first_with_key_dates_beside(people, pge, client):
    service.create_document(
        people["pge_writer"], pge, "pa", details("pa", "Joint Cooperation Memo", title="A memo"), [upload()], publish=True
    )
    html = page_only(client.get("/documents/").content.decode())
    assert 'class="library-layout"' in html
    assert html.index('<table class="list">') < html.index('<aside class="library-side"') < html.index('id="dates-h"')
    assert html.index('<form method="get" class="filters"') < html.index('id="open-h"')
    searched = page_only(client.get("/documents/", {"q": "memo"}).content.decode())
    assert "library-layout" not in searched and "library-side" not in searched  # a search shows its results only


def test_file_a_document_is_four_short_steps(people, signed_in, programs):
    c = signed_in(people["pge_writer"])
    html = c.get("/documents/new/?collection=pa").content.decode()
    for step in ("step-what", "step-covers", "step-lists", "step-files"):
        assert f'id="{step}"' in html
    assert '<span class="step-n">4</span> The files' in html
    assert '<form method="get" class="filters" aria-label="Choose the collection">' not in html  # no separate Choose step
    assert 'aria-label="Other collections"' in html and "/documents/new/?collection=emv" in html
    # Programs from a list, the PAs and each list of values folded with what is ticked.
    assert "data-program-picker" in html and '<datalist id="id_programs-list">' in html and "(none named)" in html
    assert '<details class="tick-list" data-tick-list data-none="None named">' in html and "PAs it applies to" in html
    assert '<span class="tick-legend">Sector</span>' in html
    # Started from a program: it is ticked under step 2, and the link to another collection keeps it.
    html = c.get("/documents/new/?collection=pa&program=PGE_Res_HVAC").content.decode()
    assert 'name="programs" value="PGE_Res_HVAC" checked' in html and "collection=emv&amp;program=PGE_Res_HVAC" in html


def test_programs_are_read_by_name_and_a_typo_names_the_near_matches(people, signed_in, programs):
    c = signed_in(people["pge_writer"])
    data = {
        "collection": "pa",
        "document_type": option("pa_report_type", "Cap and Target Report").pk,
        "title": "2025 Cap and Target Report",
        "programs": ["PGE_Res_HVX", ""],
        "main_file": SimpleUploadedFile("cap.pdf", PDF),
    }
    r = c.post("/documents/new/", data)
    assert r.status_code == 200
    assert "No program is “PGE_Res_HVX”. Did you mean PGE_Res_HVAC (Residential HVAC)?" in r.content.decode()
    data |= {"programs": ["Commercial lighting", "pge_res_hvac"], "main_file": SimpleUploadedFile("cap.pdf", PDF)}
    r = c.post("/documents/new/", data)
    doc = Document.objects.get(title="2025 Cap and Target Report")
    assert r.status_code == 302 and set(doc.programs.values_list("prg_id", flat=True)) == {"PGE_Res_HVAC", "PGE_Com_Light"}
    # Change the details: the programs are ticked boxes; untick one to drop it.
    html = c.get(f"/documents/{doc.pk}/change/").content.decode()
    assert 'name="programs" value="PGE_Com_Light" checked' in html and 'id="step-covers"' in html and "step-n" not in html
    # Several to a box separated by commas still read, as old forms sent them.
    f = forms.DetailsForm(
        {"document_type": data["document_type"], "title": "T", "programs": "PGE_Res_HVAC, PGE_Com_Light"}, collection="pa"
    )
    assert f.is_valid(), f.errors
    assert [p.prg_id for p in f.cleaned_data["programs"]] == ["PGE_Res_HVAC", "PGE_Com_Light"]


def test_the_study_is_picked_from_the_register(people, signed_in, study):
    c = signed_in(people["evaluator"])
    html = c.get("/documents/new/?collection=emv").content.decode()
    assert '<datalist id="id_study-list">' in html and f'<option value="{study.study_id}">{study.title}</option>' in html
    types = service.type_list("emv")
    base = {"collection": "emv", "document_type": types.options.first().pk, "title": "Workplan"}
    f = forms.DetailsForm(base | {"study": "residential hvac impact evaluation"}, collection="emv")
    assert f.is_valid(), f.errors
    assert f.cleaned_data["study"] == study
    f = forms.DetailsForm(base | {"study": str(study.number)}, collection="emv")
    assert f.is_valid() and f.cleaned_data["study"] == study  # its number, as before
    f = forms.DetailsForm(base | {"study": study.study_id[:-1] + "9"}, collection="emv")
    assert not f.is_valid() and f.errors["study"][0].startswith(f"No study is “{study.study_id[:-1]}9”. Did you mean {study.study_id}")


def test_add_a_study_is_in_short_parts_with_its_lists_folded(people, pge, signed_in):
    c = signed_in(people["pge_writer"])
    html = c.get("/documents/studies/new/").content.decode()
    for part in ("The study", "Budget and work order", "What it studies", "What comes next", "Notes and contacts"):
        assert f">{part}</h2>" in html
    assert '<span class="tick-legend">Sectors</span>' in html and '<span class="tick-legend">Funding cycles</span>' in html
    r = c.post(
        "/documents/studies/new/",
        {
            "title": "PG&E market study",
            "manager": pge.pk,
            "lead": option("study_lead", "IOU").pk,
            "sectors": [option("sector", "Commercial").pk],
            "funding_cycles": [option("portfolio_cycle", "2026").pk],
        },
    )
    assert r.status_code == 302
    s = studies.Study.objects.get(title="PG&E market study")
    assert s.study_id == f"2026_Commercial_{s.number}"
    html = c.get(f"/documents/studies/{s.number}/change/").content.decode()
    assert "2 chosen" not in html and "1 chosen: Commercial" in html and ">Why it changes</h2>" in html

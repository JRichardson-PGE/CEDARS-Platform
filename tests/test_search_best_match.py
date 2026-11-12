"""Site search: what the query names exactly leads as the best match, and a signed-in person's own filings and runs are found
(core/search.py with the parts the submissions service and the Calculations, Claims and Documents modules register)."""

import json
import re
from datetime import date
from pathlib import Path

import pytest
from django.contrib.auth.models import AnonymousUser
from django.urls import reverse

from cedars.core import search
from cedars.modules.calculations.models import Run
from cedars.services.accounts import service as access
from cedars.services.dictionary import service as dictionary
from cedars.services.registry import service as registry
from cedars.services.submissions.models import Submission

from .test_budgets import release  # noqa: F401 - the engine release fixture
from .test_claims import claims_zip, confirmed, deadline, filer, linked, programs, row, tables, year  # noqa: F401

pytestmark = pytest.mark.django_db
CATALOG = Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.94.json"


def parts(user, q) -> dict[str, search.Part]:
    return {p.key: p for p in search.search(user, q)}


def first_part(html: str) -> str:
    return html.split('<section class="search-part"', 1)[1].split("</section>", 1)[0]


def make_run(owner, name, organisation=None) -> Run:
    return Run.objects.create(
        owner=owner,
        organisation=organisation,
        name=name,
        engine_release="0.1.93",
        acc_version="2026",
        input_hash="0" * 64,
        storage_prefix="x",
    )


def test_an_exact_prgid_leads_as_the_best_match(client, cpuc_admin, pge):
    start = date(2024, 1, 1)
    registry.create_program(cpuc_admin, prg_id="PGE21001", administrator=pge, valid_from=start, name="Residential Energy Fitness")
    registry.create_program(cpuc_admin, prg_id="PGE210011", administrator=pge, valid_from=date(2024, 1, 1), name="Another")
    found = search.search(AnonymousUser(), "PGE21001")
    assert found[0].key == "best" and [h.title for h in found[0].hits] == ["PGE21001: Residential Energy Fitness"]
    assert found[0].hits[0].kind == "Program"
    # the program is not listed twice; the near match stays in Programs
    assert [h.title for h in {p.key: p for p in found}["programs"].hits] == ["PGE210011: Another"]
    html = client.get("/search/", {"q": "pge21001"}).content.decode()
    best = first_part(html)
    assert 'id="part-best"' in best and reverse("programs:program", args=["PGE21001"]) in best


def test_an_exact_field_name_or_its_cet_name_leads(client):
    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))
    found = search.search(AnonymousUser(), "NumUnits")
    assert found[0].key == "best" and [h.title for h in found[0].hits] == ["NumUnits"]
    assert found[0].hits[0].kind == "Data dictionary field"
    best = parts(AnonymousUser(), "E3MeaElecEndUseShape")["best"]
    assert [h.title for h in best.hits] == ["AC_Elec_Impact_Profile"]
    assert "NumUnits" not in [h.title for h in parts(AnonymousUser(), "NumUnits").get("fields", search.Part("", "")).hits]


def test_a_page_named_exactly_leads(signed_in, pge_admin):
    signed_in(pge_admin)
    best = parts(pge_admin, "new run")["best"]
    assert [h.url for h in best.hits] == [reverse("calculations:new")]
    assert best.hits[0].kind == "Page"
    # a query that names nothing exactly has no best match
    assert "best" not in parts(pge_admin, "run new")


def test_a_glossary_term_named_exactly_leads():
    best = parts(AnonymousUser(), "TSB")["best"]
    assert any(h.url.endswith("/help/glossary/#tsb") for h in best.hits)


def test_runs_are_found_by_the_people_who_see_them(client, signed_in, pge, pge_admin, make_user):
    analyst = make_user("analyst@pge.example.org", "Ana Lyst")
    access.grant(pge_admin, analyst, pge, "calculations.read")
    mine = make_run(pge_admin, "Quokka heat pump study", pge)
    other = make_user("other@example.org", "Oth Er")
    theirs = make_run(other, "Quokka private sketch")  # a community run: its owner's own
    found = parts(analyst, "quokka")
    assert [h.url for h in found["runs"].hits] == [reverse("calculations:run", args=[mine.pk])]
    assert found["runs"].title == "Your runs"
    assert "runs" not in parts(AnonymousUser(), "quokka")
    assert [h.url for h in parts(other, "quokka")["runs"].hits] == [reverse("calculations:run", args=[theirs.pk])]
    # a run named exactly is the best match
    best = parts(analyst, "quokka heat pump study")["best"]
    assert [h.kind for h in best.hits] == ["Run"]
    html = signed_in(analyst).get("/search/", {"q": "quokka"}).content.decode()
    assert "Your runs" in html and reverse("calculations:run", args=[mine.pk]) in html


def test_own_filings_are_found_signed_in(signed_in, filer, year, pge, programs):  # noqa: F811
    sce = programs
    Submission.objects.create(deadline=deadline("Q1 claim"), organisation=pge)
    Submission.objects.create(deadline=deadline("Q1 claim"), organisation=sce)
    found = parts(filer, "2028 q1 claim")["filings"]
    assert found.title == "Your filings" and len(found.hits) == 1
    hit = found.hits[0]
    assert "PGE" in hit.title and "SCE" not in hit.title and "not started" in hit.text and "?org=PGE" in hit.url
    assert "filings" not in parts(AnonymousUser(), "2028 q1 claim")
    html = signed_in(filer).get("/search/", {"q": "2028 q1 claim"}).content.decode()
    assert "Your filings" in html


def test_a_confirmed_claim_is_found_by_its_claimid(client, filer, year, pge, programs, release):  # noqa: F811
    cost = {"ProgramCost": [row("ProgramCost", PrgID="PGE_Res")], "Site": [row("Site", SiteID="PGE-S1")]}
    confirmed(filer, pge, claims_zip(tables(linked("PGE-2028-001"), cost)))
    best = parts(AnonymousUser(), "pge-2028-001")["best"]
    assert [h.title for h in best.hits] == ["Claim PGE-2028-001"] and best.hits[0].kind == "Confirmed claim"
    assert "claim year 2028" in best.hits[0].text
    html = client.get("/search/", {"q": "PGE-2028-001"}).content.decode()
    assert re.search(r'href="/claims/links/\?kind=claim&amp;q=PGE-2028-001"', first_part(html))
    assert "claims" not in parts(AnonymousUser(), "PGE-2028-999")  # an unknown ClaimID finds nothing


def test_a_failing_part_never_hides_the_rest(monkeypatch):
    def broken(user, q):
        raise RuntimeError("boom")

    monkeypatch.setitem(search._sources, "broken", search.Source("broken", "Broken", broken, 1))
    assert "best" in parts(AnonymousUser(), "tsb")


def test_modules_register_their_parts():
    assert {"filings", "runs", "claims", "documents"} <= set(search._sources)

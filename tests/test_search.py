"""Site search (core/search.py): the header's box, its results page and who sees what."""

import json
import re
from datetime import date
from pathlib import Path

import pytest
from django.urls import reverse

from cedars.core import nav, page_help, search
from cedars.modules.documents import service as docs
from cedars.services.accounts import service as access
from cedars.services.dictionary import service as dictionary
from cedars.services.registry import service as registry

from .test_documents import details, make_people, upload

pytestmark = pytest.mark.django_db
CATALOG = Path(__file__).parent.parent / "data" / "dictionary" / "recet-0.1.94.json"
BASE = Path(__file__).parent.parent / "cedars" / "core" / "templates" / "core" / "base.html"


def links(html: str) -> list[str]:
    return re.findall(r'<a href="([^"#?]+)', html.split('<ul class="search-hits">', 1)[-1]) if "search-hits" in html else []


def test_the_header_has_the_search_box_on_every_page(client):
    html = client.get("/").content.decode()
    box = html.split('class="site-search"', 1)[1].split("</div>", 1)[0]
    assert 'role="search"' in box and f'action="{reverse("core:search")}"' in box
    assert '<label for="site-search-q"' in box and 'name="q"' in box and ">Search</button>" in box
    assert client.get("/help/").content.decode().count('id="site-search-q"') == 1


def test_every_header_page_is_searchable_with_help():
    """Each page the header or footer links to is in search.all_pages(), and each PAGES entry names a help entry that exists."""
    names = set(re.findall(r"\{% url '([a-z_]+:[a-z_]+)'", BASE.read_text(encoding="utf-8"))) | nav.all_url_names()
    # a release note's and a guide's own pages are found through their lists; switching organization and putting a bar away are forms
    names -= {
        "core:search",
        "account_login",
        "administration:switch_organisation",
        "releases:detail",
        "core:help_guide",
        "releases:put_away",
    }
    listed = {p.url_name for p in search.all_pages()}
    assert names - listed == set()
    entries = page_help.entries()
    for p in search.all_pages():
        reverse(p.url_name)
        assert p.help_key in entries, p


def test_public_pages_listed_to_everyone_open_signed_out(client):
    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))
    for p in search.all_pages():
        if "" in p.who or "anon" in p.who:
            assert client.get(reverse(p.url_name), follow=True).status_code == 200, p.url_name
            assert not client.get(reverse(p.url_name)).get("Location", "").startswith("/accounts/"), p.url_name


def test_signed_out_visitors_find_only_public_pages(client):
    html = client.get("/search/", {"q": "claims"}).content.decode()
    assert reverse("claims:index") not in links(html)
    assert reverse("core:help_guide", args=["claims"]) in links(html)  # the user guide is public


def test_pages_follow_the_header_roles(client, signed_in, pge_admin, cpuc_admin):
    signed_in(pge_admin)
    html = client.get("/search/", {"q": "claims"}).content.decode()
    assert reverse("claims:index") in links(html)
    html = client.get("/search/", {"q": "retention"}).content.decode()
    assert reverse("retention:index") not in links(html)  # system administrators only
    signed_in(cpuc_admin)
    assert reverse("retention:index") in links(client.get("/search/", {"q": "retention"}).content.decode())


def test_glossary_terms_and_pa_codes(client, cpuc_admin):
    access.create_organisation(cpuc_admin, code="BAY", name="Bay Area Regional Energy Network (BayREN)", kind="pa")
    hits = {p.key: p for p in search.search(None_user(), "tsb")}
    assert any(h.url.endswith("/help/glossary/#tsb") for h in hits["best"].hits)  # the term named exactly leads
    hits = {p.key: p for p in search.search(None_user(), "BAYREN")}
    assert any(h.url.endswith("#pa-codes") for h in hits["glossary"].hits)
    assert search.glossary_entries() and all(a and t and d for a, t, d in search.glossary_entries())


def None_user():
    from django.contrib.auth.models import AnonymousUser

    return AnonymousUser()


def test_programs_and_dictionary_fields(client, cpuc_admin, pge):
    registry.create_program(cpuc_admin, prg_id="PGE_Res_HVAC", administrator=pge, valid_from=date(2024, 1, 1), name="Residential HVAC")
    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))
    html = client.get("/search/", {"q": "pge_res"}).content.decode()
    assert reverse("programs:program", args=["PGE_Res_HVAC"]) in html and "PGE_Res_HVAC: Residential HVAC" in html
    html = client.get("/search/", {"q": "NTGRkWh"}).content.decode()
    assert reverse("dictionary:field", args=["NTGRkWh"]) in html
    html = client.get("/search/", {"q": "ElecBen"}).content.decode()
    part = {p.key: p for p in search.search(None_user(), "ElecBen")}["fields"]
    assert part.hits[0].title.lower().startswith("elecben")
    if part.total > search.PER_PART:
        assert "/dictionary/?q=ElecBen" in html


def test_unpublished_documents_stay_with_those_who_may_read_them(client, signed_in, cpuc_admin, cpuc, pge, pge_admin, make_user):
    people = make_people(cpuc_admin, cpuc, pge, pge_admin, make_user)
    d = details("pa", "Joint Cooperation Memo", title="Quokka Joint Cooperation Memo", pas=[people["bayren"]], first_year=2026)
    doc = docs.create_document(people["pge_writer"], pge, "pa", d, [upload("jcm.pdf")])
    url = reverse("documents:document", args=[doc.pk])
    assert url not in client.get("/search/", {"q": "quokka"}).content.decode()
    signed_in(people["sce_user"])
    assert url not in client.get("/search/", {"q": "quokka"}).content.decode()
    signed_in(people["pge_writer"])
    assert url in client.get("/search/", {"q": "quokka"}).content.decode()


def test_every_word_must_match_and_an_empty_search_finds_nothing(client):
    assert search.search(None_user(), "") == [] and search.search(None_user(), "  ") == []
    assert search.search(None_user(), "zzqx glossary") == []
    html = client.get("/search/", {"q": "zzqx"}).content.decode()
    assert "Nothing matches" in html
    assert client.get("/search/").status_code == 200


def test_the_faq_is_searched_too(client):
    from cedars.core import faq

    q = faq.questions()[0]
    word = max(re.findall(r"[A-Za-z]{5,}", q.q), key=len)
    part = {p.key: p for p in search.search(None_user(), word)}["help"]
    assert part.total >= 1 and any(h.kind == "Question" for h in part.hits) or part.total > search.PER_PART


def test_every_page_with_its_own_help_and_address_is_searchable(client, signed_in, cpuc_admin):
    """Navigation review D6: not only the header's pages; each listed to whoever may open it."""
    listed = {p.url_name: p for p in search.all_pages()}
    for name in ("impact_profiles:packages", "documents:work_orders", "claims:restatements", "snapshots:scan_settings"):
        assert name in listed, name
    assert "rules:differences" in listed
    assert len(listed) > 70
    assert "" in listed["documents:work_orders"].who and listed["snapshots:scan_settings"].who == ("sysadmin",)
    assert 'href="/documents/work-orders/"' in client.get("/search/", {"q": "work orders"}).content.decode()  # public
    assert f'href="{reverse("snapshots:scan_settings")}"' not in client.get("/search/", {"q": "scan settings"}).content.decode()
    html = signed_in(cpuc_admin).get("/search/", {"q": "scan settings"}).content.decode()
    assert f'href="{reverse("snapshots:scan_settings")}"' in html

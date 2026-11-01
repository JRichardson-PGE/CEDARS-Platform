"""Help on every page (Jake, 2026-10-31): a (?) beside each page's heading opens a guide to the page and a tour of it
(cedars/core/page_help.py). These check the help is there for every page and well formed; tests/a11y checks each tour finds its parts
of the page and that the dialogs work by keyboard and without scripts."""

import re
from pathlib import Path

import pytest
from django.template import Context, Template, TemplateSyntaxError

from cedars.core import help as site_help
from cedars.core import page_help as pages
from cedars.modules.data import dashboard

ROOT = Path(pages.ROOT)
TAG = re.compile(r'<div class="page-title">(<h1\b[^>]*>.*?</h1>)\{% page_help "([^"]+)"( page\.slug)? %\}</div>', re.S)


def templates_with_a_heading():
    return sorted(p for p in ROOT.rglob("templates/**/*.html") if "<h1" in p.read_text(encoding="utf-8"))


def key_of(path: Path) -> str:
    return str(path).split("/templates/", 1)[1].removesuffix(".html")


def test_the_help_loads_and_is_well_formed():
    found = pages.entries()
    assert len(found) > 100
    for key, e in found.items():
        assert e.title and e.guide, key
        assert e.tour or e.tour_from == "front", key
        assert e.see in site_help.BY_SLUG, f"{key}: see: names no guide ({e.see!r}); every entry links its user guide (UI review 2, #37)"
        assert e.area in pages.AREAS, f"{key}: add {e.area!r} to page_help.AREAS"
        if e.tour:
            assert e.tour[-1] == pages.LAST_STEP and pages.LAST_STEP not in e.tour[:-1], key


def test_every_page_heading_has_its_help():
    """Every template with a page heading puts the (?) right after it, in a .page-title, naming its own entry."""
    used = set()
    for path in templates_with_a_heading():
        src = path.read_text(encoding="utf-8")
        if key_of(path) in pages.WITHOUT_HELP:
            assert not TAG.search(src), f"{path}: a help page has no (?) of its own"
            continue
        assert src.count("<h1") == 1, f"{path}: one page heading"
        m = TAG.search(src)
        assert m, f'{path}: the heading needs {{% page_help %}} beside it, in <div class="page-title">'
        key = m.group(2)
        own = key_of(path)
        # django-allauth's heading element: every allauth page we do not override shares one guide to the security settings
        shared = {"allauth/elements/h1": "account/security"}
        assert key == shared.get(own, own), f"{path}: names {key!r}"
        assert pages.lookup(key) is not None, f"{path}: no page help for {key!r}"
        used.add(key)
        if m.group(3):  # a variant per dashboard page
            for slug in dashboard.PAGES:
                assert f"{key}/{slug}" in pages.entries(), f"no page help for the {slug} dashboard"
                used.add(f"{key}/{slug}")
    assert set(pages.entries()) - used == set(), "page help that no page shows"


def test_the_tag_needs_an_entry():
    with pytest.raises(TemplateSyntaxError, match="No page help"):
        Template('{% page_help "nope/nothing" %}').render(Context({}))


def test_the_button_and_the_dialog(client):
    page = client.get("/help/contact/").content.decode()
    assert (
        '<a class="page-help" href="/help/pages/core/help/contact/?from=%2Fhelp%2Fcontact%2F" data-page-help '
        'aria-label="Help with this page: a guide and a tour"><span aria-hidden="true">?</span></a>'
    ) in page
    assert '<dialog id="page-help"' in page and "Getting support</h2>" in page
    assert 'id="tour-steps"' in page and 'data-heading="Before you write"' in page and "core/tour.js" in page
    assert page.count('id="tour"') == 1


@pytest.mark.django_db
def test_every_page_help_page(client):
    for key, e in pages.entries().items():
        r = client.get(f"/help/pages/{key}/")
        assert r.status_code == 200, key
        body = r.content.decode()
        assert f"Help: {e.title}" in body.replace("&quot;", '"').replace("&#x27;", "'"), key
    index = client.get("/help/pages/").content.decode()
    assert all(f'href="/help/pages/{k}/"' in index for k in pages.entries())
    assert client.get("/help/pages/nope/").status_code == 404


def test_the_help_page_links_back_and_to_the_tour(client):
    body = client.get("/help/pages/rules/index/?from=/rules/%3Fq%3Dx").content.decode()
    assert 'href="/rules/?q=x"' in body and 'href="/rules/?q=x&amp;tour=1"' in body
    assert "<h3>" in body  # the tour written out
    body = client.get("/help/pages/rules/index/?from=https://example.org/").content.decode()
    assert 'href="https://example.org/' not in body and "Back to the page" not in body and ">Take the tour on the page</a>" not in body
    assert 'href="/?tour=1"' in client.get("/help/pages/data/front/").content.decode()


def test_the_help_index_lists_the_pages(client):
    assert 'href="/help/pages/"' in client.get("/help/").content.decode()

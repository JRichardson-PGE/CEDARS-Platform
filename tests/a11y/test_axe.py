"""WCAG 2.2 AA on every page, in both colour schemes, at full width and at 320 px (reflow, 1.4.10).

Run with `pytest -m a11y` (CI does). axe finds what can be found automatically; keyboard and screen-reader checks are by hand
(docs/ACCESSIBILITY.md)."""

import json

import pytest

from .pages import PAGES

pytestmark = [pytest.mark.a11y, pytest.mark.django_db(transaction=True)]

TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


def _violations(page, axe_source):
    page.add_script_tag(content=axe_source)
    result = page.evaluate("async (tags) => await axe.run(document, {runOnly: {type: 'tag', values: tags}})", TAGS)
    return [
        {"id": v["id"], "impact": v["impact"], "help": v["help"], "targets": [n["target"] for n in v["nodes"]][:5]}
        for v in result["violations"]
    ]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("name,path", PAGES, ids=[p[0] for p in PAGES])
def test_axe_clean(browser, axe_source, live_server, name, path, scheme):
    context = browser.new_context(color_scheme=scheme, bypass_csp=True)  # axe is injected inline; the page keeps its own CSP otherwise
    page = context.new_page()
    page.goto(live_server.url + path)
    found = _violations(page, axe_source)
    context.close()
    assert not found, f"{name} ({scheme}): " + json.dumps(found, indent=1)


@pytest.mark.parametrize("name,path", PAGES, ids=[p[0] for p in PAGES])
def test_reflows_at_320px(browser, live_server, name, path):
    context = browser.new_context(viewport={"width": 320, "height": 640})
    page = context.new_page()
    page.goto(live_server.url + path)
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    context.close()
    assert overflow <= 0, f"{name} scrolls sideways by {overflow} px at 320 px wide"

"""The "Before you upload" line under every file control (navigation review D3, Jake 2026-11-04): the template, the field definitions,
the rules in force and the guide section, from one tag (core/templatetags/ui.before_upload)."""

from pathlib import Path

import pytest
from django.contrib.auth.models import AnonymousUser
from django.template import Context, Template
from django.test import RequestFactory

ROOT = Path(__file__).parent.parent / "cedars"
# Uploads of any kind of file, where no template or rule applies: documents and their attachments, comments' files.
EXEMPT = {
    "modules/documents/templates/documents/version_new.html",
    "modules/documents/templates/documents/comments.html",
    "modules/documents/templates/documents/new.html",
    "modules/documents/templates/documents/response_new.html",
    "modules/documents/templates/documents/study_form.html",
    "services/submissions/templates/submissions/_comments.html",
}


def test_every_data_upload_has_the_line():
    for path in ROOT.rglob("templates/**/*.html"):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if 'enctype="multipart/form-data"' in text and rel not in EXEMPT:
            assert text.count("{% before_upload") >= 1, rel


def _render(src, user=None):
    request = RequestFactory().get("/")
    request.user = user or AnonymousUser()
    return Template(src).render(Context({"request": request}))


@pytest.mark.django_db
def test_the_parts(cpuc_admin):
    html = _render(
        '{% before_upload template="/budgets/template.xlsx" template_label="the budget filing template" fields="calculations.Measure" '
        'rule_set="budget_filing" guide="budgets" %}'
    )
    assert html.startswith('<p class="before-upload"><strong>Before you upload:</strong> ')
    assert '<a href="/budgets/template.xlsx">the budget filing template</a>' in html
    assert '<a href="/dictionary/?table=calculations.Measure">the field definitions</a>' in html
    assert "no rule set is published for it yet" in html and '<a href="/help/budgets/">the user guide</a>' in html
    assert _render("{% before_upload %}").strip() == ""


@pytest.mark.django_db
def test_the_rule_set_in_force_is_linked_for_those_signed_in(cpuc_admin):
    from cedars.services.rules.models import RuleSet

    rs = RuleSet.objects.create(
        code="budget_filing", label="Budget filing rules", status=RuleSet.Status.PUBLISHED, effective_from="2026-01-01"
    )
    html = _render('{% before_upload rule_set="budget_filing" %}', cpuc_admin)
    assert f'<a href="/rules/{rs.pk}/">the rules it is checked against, Budget filing rules</a>' in html
    assert "/rules/" not in _render('{% before_upload rule_set="budget_filing" %}')  # the rules are signed in only (Jake, 2026-11-02)


@pytest.mark.django_db
def test_the_dictionary_lists_an_upload_tables_fields(client):
    import json

    from cedars.services.dictionary import service as dictionary

    from .test_search import CATALOG

    dictionary.load_catalog(None, json.loads(CATALOG.read_text(encoding="utf-8")))
    page = client.get("/dictionary/", {"table": "calculations.ProgramCost"}).content.decode()
    assert 'href="/dictionary/fields/PrgID/"' in page or "PrgID" in page
    assert "NumUnits" not in page.split("<h2>Fields</h2>", 1)[1].split("</table>", 1)[0]  # a Measure field, not ProgramCost's
    assert '<option value="calculations.ProgramCost" selected>' in page

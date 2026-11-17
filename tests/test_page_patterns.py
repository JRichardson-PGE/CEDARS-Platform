"""Page patterns every template keeps (UI review 2, batch D; DESIGN.md): breadcrumbs in one shape, one Apply button per filter bar,
titles that name the page as well as its object."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "cedars"
TEMPLATES = sorted(ROOT.rglob("templates/**/*.html"))


def _content_start(text: str) -> str:
    """The first line inside the content block."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "{% block content %}" in line and i + 1 < len(lines):
            return lines[i + 1]
    return ""


def test_breadcrumbs_have_one_shape():
    """A page's parents are a nav named Breadcrumb holding an ordered list, never a paragraph of links with › or · between them."""
    old, unnamed = [], []
    for path in TEMPLATES:
        text = path.read_text()
        first = _content_start(text)
        if re.search(r'<p( class="small")?>(\{% if [^%]+%\})?<a href="[^"]*">[^<]*</a>( (›|·) |</p>)', first):
            old.append(str(path.relative_to(ROOT)))
        for m in re.finditer(r'<ol class="crumbs">', text):
            if '<nav aria-label="Breadcrumb">' not in text[max(0, m.start() - 40) : m.start()]:
                unnamed.append(str(path.relative_to(ROOT)))
    assert not old, old
    assert not unnamed, unnamed


@pytest.mark.parametrize("name", ["Show", "Apply filters"])
def test_filter_bars_say_apply(name):
    """A filter bar's one button is "Apply" (a search box alone: "Search")."""
    found = [str(p.relative_to(ROOT)) for p in TEMPLATES if f'<button type="submit">{name}</button>' in p.read_text()]
    found += [str(p.relative_to(ROOT)) for p in TEMPLATES if f'class="primary">{name}</button>' in p.read_text()]
    assert not found, found


def test_a_filter_bar_has_one_apply_button():
    many = []
    for path in TEMPLATES:
        for form in re.findall(r'<form method="get" class="filters[^"]*".*?</form>', path.read_text(), re.S):
            if len(re.findall(r'<button type="submit"(?: class="[^"]*")?>(?:Apply|Search)</button>', form)) > 1:
                many.append(str(path.relative_to(ROOT)))
    assert not many, many


def test_page_titles_name_the_page(client, db):
    """The second factor page is in sentence case like every other page (allauth writes "Two-Factor Authentication")."""
    from django.template.loader import get_template

    source = get_template("mfa/index.html").template.source
    assert '"Two-factor authentication"' in source and "Two-Factor" not in source

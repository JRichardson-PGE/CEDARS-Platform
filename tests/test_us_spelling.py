"""The site uses US spelling (CLAUDE.md): no British spellings in what a page shows or in the docs.

Template tags, variables and identifier attributes (id, name, for, href, class, action, value) are left out: route names, template file
names and fields keep their original `organisation` spelling.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRITISH = re.compile(
    r"\b(organis(e|es|ed|ing|ation|ations|ational)|colours?|behaviours?|favour\w*|honour\w*|cancelled|cancelling|totalled|labelled|"
    r"catalogue|centres?|licence|recognis\w+|analys(e|ed|ing)|summaris\w+|prioritis\w+|authoris\w+|customis\w+|optimis\w+|"
    r"normalis\w+|initialis\w+|serialis\w+|modelling|acknowledgements?)\b",
    re.I,
)
TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)
IDENT_ATTR = re.compile(
    r"""\s(id|name|for|href|src|class|action|value|aria-labelledby|aria-describedby|aria-controls|data-[\w-]+)"""
    r"""=("[^"]*"|'[^']*')"""
)
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def _found(text: str) -> list[str]:
    return sorted({m.group(0) for m in BRITISH.finditer(text)})


def test_templates_use_us_spelling():
    bad = {}
    for p in sorted((ROOT / "cedars").rglob("templates/**/*.html")):
        text = IDENT_ATTR.sub(" ", TAG.sub(" ", p.read_text(encoding="utf-8")))
        if words := _found(text):
            bad[str(p.relative_to(ROOT))] = words
    assert not bad, f"British spellings on pages: {bad}"


def test_words_inside_template_tags_use_us_spelling():
    """Prose passed inside a tag ({% include ... text="..." %}, a default:"..." filter) reaches the page too (UI review 2, #24). A quoted
    string with a space in it is prose; one without is a name (a route, a template, a field)."""
    bad = {}
    for p in sorted((ROOT / "cedars").rglob("templates/**/*.html")):
        quoted = [q for t in TAG.findall(p.read_text(encoding="utf-8")) for pair in re.findall(r'"([^"]*)"|\'([^\']*)\'', t) for q in pair]
        prose = [q for q in quoted if " " in q]
        if words := _found(" ".join(prose)):
            bad[str(p.relative_to(ROOT))] = words
    assert not bad, f"British spellings inside template tags: {bad}"


def test_page_help_and_python_strings_use_us_spelling():
    """The page help (every app's page_help.yaml) and the strings Python code shows: messages, labels, help text, choices. A string
    with a space in it, docstrings left out (they name parameters such as `organisation`)."""
    import io
    import tokenize

    bad = {}
    for p in sorted((ROOT / "cedars").rglob("page_help.yaml")):
        text = re.sub(r"(?m)^[\w/]+:$", "", p.read_text(encoding="utf-8"))  # an entry's key is its template's name
        if words := _found(text):
            bad[str(p.relative_to(ROOT))] = words
    for p in sorted((ROOT / "cedars").rglob("*.py")):
        if "migrations" in p.parts:
            continue
        strings = [
            t.string
            for t in tokenize.generate_tokens(io.StringIO(p.read_text(encoding="utf-8")).readline)
            if t.type == tokenize.STRING and " " in t.string and not t.string.lstrip("rbuf").startswith(('"""', "'''"))
        ]
        if words := _found(" ".join(strings)):
            bad[str(p.relative_to(ROOT))] = words
    assert not bad, f"British spellings in page help or shown strings: {bad}"


def test_docs_use_us_spelling():
    bad = {}
    for p in [ROOT / "CLAUDE.md", ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        text = CODE.sub(" ", p.read_text(encoding="utf-8"))
        text = re.sub(r"\(e\.g\. the PSR's \"Cancelled\" status\)", "", text)
        if words := _found(text):
            bad[str(p.relative_to(ROOT))] = words
    assert not bad, f"British spellings in the docs: {bad}"


def test_old_organisation_links_redirect(client):
    r = client.get("/admin/organisations/PGE/?tab=members")
    assert r.status_code == 302 and r["Location"] == "/admin/organizations/PGE/?tab=members"

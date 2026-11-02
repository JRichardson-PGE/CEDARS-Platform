"""ReCET's own documents under Help (core/engine_docs.py): the shipped copies are the pinned engine's, every page renders, every link
between them resolves, and what the pages show is in US spelling."""

import hashlib
import re
from pathlib import Path

import pytest

from cedars.core import engine_docs

from .test_us_spelling import BRITISH

ROOT = Path(__file__).resolve().parent.parent


def test_the_shipped_copies_match_the_manifest_and_the_pin():
    m = engine_docs.manifest()
    assert m["engine"]["commit"] == (ROOT / "engine" / "recet-commit").read_text(encoding="utf-8").strip()
    assert sorted(m["documents"]) == sorted(d.file for d in engine_docs.DOCS)
    for doc in engine_docs.DOCS:
        assert engine_docs.file_sha256(doc) == m["documents"][doc.file], doc.file
    assert sorted(p.name for p in engine_docs.DOCS_DIR.glob("*.md")) == sorted(m["documents"])


def test_the_shipped_copies_are_the_pinned_engines():
    """When the pinned ReCET is installed, its documents are the shipped ones (re-run scripts/export_recet_docs.py after a pin change)."""
    recet = pytest.importorskip("recet")
    assert engine_docs.manifest()["engine"]["version"] == recet.__version__
    src = Path(recet.__file__).parent / "docs"
    for doc in engine_docs.DOCS:
        assert hashlib.sha256((src / doc.file).read_bytes()).hexdigest() == engine_docs.file_sha256(doc), doc.file


def _ids(html: str) -> set[str]:
    return set(re.findall(r'\sid="([^"]+)"', html))


@pytest.mark.django_db
def test_every_document_renders_with_its_links_resolved(client):
    r = client.get("/help/recet/")
    assert r.status_code == 200
    page = r.content.decode()
    ids = {}
    for doc in engine_docs.DOCS:
        assert f'href="/help/recet/{doc.slug}/"' in page
        r = client.get(f"/help/recet/{doc.slug}/")
        assert r.status_code == 200, doc.slug
        html = r.content.decode()
        assert html.count("<h1") == 1 and f">{doc.title}</h1>" in html
        assert engine_docs.manifest()["engine"]["version"] in html
        ids[doc.slug] = _ids(html)
    for doc in engine_docs.DOCS:
        body = engine_docs.render(doc.slug).html
        assert "<h1" not in body
        for href in re.findall(r'href="([^"]*)"', body):
            if href.startswith("#"):
                assert href[1:] in ids[doc.slug], (doc.slug, href)
            elif href.startswith("/help/recet/"):
                slug, _, frag = href.removeprefix("/help/recet/").partition("/")
                assert slug in engine_docs.BY_SLUG, (doc.slug, href)
                if frag.startswith("#"):
                    assert frag[1:] in ids[slug], (doc.slug, href)
            else:
                assert href.startswith(("https://", "http://", "mailto:")), (doc.slug, href)
                assert not href.startswith(engine_docs.REPOSITORY) or engine_docs.manifest()["engine"]["commit"] in href, href


@pytest.mark.django_db
def test_an_unknown_document_is_not_found(client):
    assert client.get("/help/recet/nothing-here/").status_code == 404


def test_links_to_documents_not_shown_go_to_github_at_the_pin():
    commit = engine_docs.manifest()["engine"]["commit"]
    assert engine_docs._href("WEB_GUIDE.md#runs") == f"{engine_docs.REPOSITORY}/blob/{commit}/recet/docs/WEB_GUIDE.md#runs"
    assert engine_docs._href("../../tests/reference_calc.py") == f"{engine_docs.REPOSITORY}/blob/{commit}/tests/reference_calc.py"
    assert engine_docs._href("OUTPUT_FIELDS.md#x") == "/help/recet/output-fields/#x"
    assert engine_docs._href("#here") == "#here"
    # GitHub keeps a run of dashes in an anchor where Python-Markdown makes one
    assert engine_docs._href("CALCULATIONS.md#c6-benefit--supply-cost-split") == "/help/recet/calculations/#c6-benefit-supply-cost-split"


def test_wide_tables_scroll_in_a_named_region_and_diagrams_are_text():
    html = engine_docs.render("engine-architecture").html
    assert "<table>" in html
    assert html.count("<table>") == html.count('role="region" aria-label="Table') == html.count('<caption class="sr-only">Table')
    assert engine_docs.render("engine-architecture").diagrams == 7
    assert html.count('class="language-mermaid"') == 7
    assert html.count("<pre>") == html.count('role="region" aria-label="Code')


def test_the_pages_show_us_spelling():
    """CLAUDE.md: every page in US English. The engine's few British spellings are shown in US spelling (engine_docs.US_SPELLING), in
    text only; code is left as written."""
    for doc in engine_docs.DOCS:
        r = engine_docs.render(doc.slug)
        text = re.sub(r"<(pre|code)\b.*?</\1>", " ", r.html, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        found = sorted({m.group(0) for m in BRITISH.finditer(text)})
        assert not found, (doc.slug, found)
        names = " ".join(h["name"] for h in r.toc)
        assert not BRITISH.search(names), doc.slug

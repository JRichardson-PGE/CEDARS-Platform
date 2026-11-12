"""ReCET's own documents under Help (Jake, 2026-11-02: "The more detailed ReCET docs need to be available on CEDARS platform too").

The documents are the pinned engine's (`recet/docs/*.md`), copied unchanged into data/recet_docs/ by scripts/export_recet_docs.py with a
manifest of the engine version, commit and each file's SHA-256, so they always describe the engine CEDARS runs, and the pages work
without the engine installed. tests/test_engine_docs.py checks the copies are the pinned engine's.

They are rendered here with Python-Markdown as ReCET's own Docs pages render them (same extensions, so the same heading anchors), then:
the document's own title becomes the page's h1 (kept out of the body, which starts at h2); links to another document shown here go to its
page, links to anything else in the ReCET repository go to that file on GitHub at the pinned commit; wide tables and code blocks scroll
inside a focusable, named region (WCAG 2.1.1); Mermaid diagrams are drawn by the export (scripts are same-origin only and the
pages work without them; the export draws each one as an SVG, shown as an image whose text alternative is its accTitle / accDescr,
with its source text under it); and the few British spellings in the engine's prose
(`US_SPELLING`) are shown in US spelling (CLAUDE.md), in text only: code, anchors and links are left as written.

This module is importable without Django settings (the export script reads DOCS)."""

import base64
import hashlib
import json
import posixpath
import re
from dataclasses import dataclass
from functools import lru_cache
from html import escape, unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCS_DIR = ROOT / "data" / "recet_docs"
REPOSITORY = "https://github.com/JRichardson-PGE/ReCET"


@dataclass(frozen=True)
class Doc:
    slug: str
    file: str
    title: str
    summary: str


# In reading order. ReCET's WEB_GUIDE (its own web site's pages) and README (its index) are not shown: the platform's own Calculations
# guide and pages take their place, and links to them go to GitHub.
DOCS = [
    Doc(
        "input-formats",
        "INPUT_FORMATS.md",
        "Input formats",
        "The layout of each file a run takes: the job's Measure.csv and ProgramCost.csv or one Excel workbook, 8,760-hour electric "
        "profiles, gas monthly profiles, electric and gas ACC files, run parameters and statewide allocation factors, and what a run "
        "writes.",
    ),
    Doc(
        "input-fields",
        "INPUT_FIELDS.md",
        "Input fields",
        "Every input field of Measure.csv and ProgramCost.csv, every run setting, supplied file and reference table, with its unit and how "
        "the engine uses it.",
    ),
    Doc(
        "output-fields",
        "OUTPUT_FIELDS.md",
        "Output fields",
        "Every output column with its complete formula in terms of the input fields, what each symbol is, the inputs it depends on and a "
        "worked number; the other files of a run; two worked examples.",
    ),
    Doc(
        "calculations",
        "CALCULATIONS.md",
        "Calculations",
        "The method behind the formulas: which avoided costs and load shapes are used and why, discounting and baselines, the cost tests, "
        "the validation against CET, and the deliberate differences from CET.",
    ),
    Doc(
        "cost-tests",
        "COST_TESTS.md",
        "Cost tests as configuration",
        "How the cost tests are defined as versioned definition sets (EE-2026), what a set holds, how a run picks and records one, and "
        "what a custom set may be used for.",
    ),
    Doc(
        "desktop-and-command-line",
        "USER_GUIDE.md",
        "Desktop and command line",
        "Running ReCET on your own computer: installing it, every control of the desktop window, the command line, common jobs and "
        "troubleshooting.",
    ),
    Doc(
        "engine-architecture",
        "ARCHITECTURE.md",
        "Engine architecture",
        "How the engine is built: its requirements and how each is met, its layers and modules, a run end to end, the pricing core, its "
        "data, and its performance design.",
    ),
]
BY_SLUG = {d.slug: d for d in DOCS}
BY_FILE = {d.file: d for d in DOCS}

# The engine's British spellings, shown in US spelling. tests/test_engine_docs.py fails on any other, so a new one is added here.
US_SPELLING = {
    "behaviour": "behavior",
    "colour": "color",
    "colours": "colors",
    "labelled": "labeled",
    "recognised": "recognized",
    "totalled": "totaled",
}
_SPELLING = re.compile(r"\b(" + "|".join(US_SPELLING) + r")\b", re.I)

EXTENSIONS = ["tables", "fenced_code", "toc", "sane_lists"]  # ReCET's Docs pages' (recet/web/app.py), so the anchors are the same


def manifest() -> dict:
    return json.loads((DOCS_DIR / "manifest.json").read_text(encoding="utf-8"))


def engine() -> dict:
    """The engine version and commit the documents come from."""
    e = manifest()["engine"]
    return {**e, "short": e["commit"][:7], "url": f"{REPOSITORY}/tree/{e['commit']}/recet/docs"}


def source_url(doc: Doc) -> str:
    return f"{REPOSITORY}/blob/{manifest()['engine']['commit']}/recet/docs/{doc.file}"


@dataclass(frozen=True)
class Rendered:
    title: str  # the document's own first heading
    html: str
    toc: list  # [{"id", "name", "children": [...]}] of the h2 and h3 headings
    diagrams: int


def _us(text: str) -> str:
    def swap(m):
        word = US_SPELLING[m.group(0).lower()]
        return word.capitalize() if m.group(0)[0].isupper() else word

    return _SPELLING.sub(swap, text)


def _text_only(html: str, fn) -> str:
    """Apply fn to the text of html outside tags, <code> and <pre>."""
    out, depth = [], 0
    for part in re.split(r"(<[^>]+>)", html):
        if part.startswith("<"):
            tag = re.match(r"</?(\w+)", part)
            if tag and tag.group(1).lower() in ("code", "pre"):
                depth += -1 if part.startswith("</") else 1
            out.append(part)
        else:
            out.append(part if depth > 0 else fn(part))
    return "".join(out)


def page_url(slug: str) -> str:
    from django.urls import reverse

    return reverse("core:recet_doc", args=[slug])


def _href(target: str, here: str = "") -> str:
    """A link in document `here` (a slug) as it is shown on its page."""
    if re.match(r"^([a-z][a-z0-9+.-]*:|/)", target, re.I) or (target.startswith("#") and not here):
        return target
    path, _, frag = target.partition("#")
    frag = f"#{frag}" if frag else ""
    if path in BY_FILE or not path:
        slug = BY_FILE[path].slug if path else here
        return ("" if not path else page_url(slug)) + _anchor(slug, frag)
    commit = manifest()["engine"]["commit"]
    resolved = posixpath.normpath(posixpath.join("recet/docs", path))
    return f"{REPOSITORY}/blob/{commit}/{resolved}{frag}"


@lru_cache(maxsize=16)
def _convert(slug: str):
    """The document's body (its first heading taken off as the title) through Python-Markdown: (title, html, toc tokens)."""
    import markdown

    doc = BY_SLUG[slug]
    text = (DOCS_DIR / doc.file).read_text(encoding="utf-8")
    first = re.match(r"\A#\s+(.+?)\s*\n", text)
    title = first.group(1) if first else doc.title
    body = text[first.end() :] if first else text
    md = markdown.Markdown(extensions=EXTENSIONS, extension_configs={"toc": {"toc_depth": "2-3"}})
    html = md.convert(body)
    return title, html, md.toc_tokens


def _anchor(slug: str, frag: str) -> str:
    """A fragment as the page has it. The engine's documents are also read on GitHub, whose anchors keep a run of dashes
    ("c6-benefit--supply-cost-split" for "C6 Benefit / supply-cost split") where Python-Markdown makes one."""
    if not frag:
        return ""
    ids = set(re.findall(r'\sid="([^"]+)"', _convert(slug)[1]))
    name = frag[1:]
    if name not in ids and re.sub(r"-{2,}", "-", name) in ids:
        name = re.sub(r"-{2,}", "-", name)
    return "#" + name


def _toc(tokens) -> list:
    return [{"id": t["id"], "name": _us(unescape(t["name"])), "children": _toc(t.get("children", []))} for t in tokens]


@lru_cache(maxsize=16)
def render(slug: str) -> Rendered:
    """One document as HTML for its Help page (the files never change while the site runs)."""
    title, html, tokens = _convert(slug)

    html = re.sub(r'href="([^"]*)"', lambda m: f'href="{escape(_href(unescape(m.group(1)), slug))}"', html)

    diagrams = 0
    sources = mermaid_blocks((DOCS_DIR / BY_SLUG[slug].file).read_text(encoding="utf-8"))

    def mermaid(m):
        nonlocal diagrams
        diagrams += 1
        n, text = diagrams, f'<pre><code class="language-mermaid">{m.group(1)}</code></pre>'
        drawing = DOCS_DIR / "diagrams" / f"{BY_SLUG[slug].file.removesuffix('.md')}-{n}.svg"
        if not drawing.exists():  # not drawn (the export draws every one): its text only
            hint = f"Diagram {n}, as its source text (Mermaid): each line after accTitle and accDescr is a box or an arrow."
            return f'<p class="hint">{hint}</p>{text}'
        data = base64.b64encode(drawing.read_bytes()).decode("ascii")
        alt = escape(_us(diagram_alt(sources[n - 1]) if n <= len(sources) else f"Diagram {n}"))
        return (
            f'<figure class="diagram"><img src="data:image/svg+xml;base64,{data}" alt="{alt}"></figure>'
            f'<details class="diagram-text"><summary>Diagram {n} as text</summary><p class="hint">The diagram\'s source (Mermaid): each '
            f"line after accTitle and accDescr is a box or an arrow.</p>{text}</details>"
        )

    html = re.sub(r'<pre><code class="language-mermaid">(.*?)</code></pre>', mermaid, html, flags=re.S)

    heading, counts = ["the document's start"], {}

    def region(m):
        if m.group(1):
            heading[0] = re.sub(r"<[^>]+>", "", m.group(3)).strip()
            return m.group(1)
        kind = "Table" if m.group(0) == "<table>" else "Code"
        n = counts[kind, heading[0]] = counts.get((kind, heading[0]), 0) + 1
        under = unescape(heading[0])
        label = escape(_us(f"{kind} {n} under {under}" if n > 1 else f"{kind} under {under}"))
        box = f'<div class="hscroll" tabindex="0" role="region" aria-label="{label}">'
        return box + (f'<table><caption class="sr-only">{label}</caption>' if kind == "Table" else "<pre>")

    # wide tables and code blocks (formulas line up in columns, so they do not wrap) scroll inside a focusable, named box (1.4.10, 2.1.1)
    html = re.sub(r"(<h([2-6])[^>]*>(.*?)</h\2>)|<table>|<pre>", region, html, flags=re.S)
    html = html.replace("</table>", "</table></div>").replace("</pre>", "</pre></div>")
    html = _text_only(html, _us)
    return Rendered(_us(title), html, _toc(tokens), diagrams)


def mermaid_blocks(text: str) -> list[str]:
    """The source of each ```mermaid block of a document's Markdown, in order."""
    return re.findall(r"^```mermaid[ \t]*\n(.*?)^```[ \t]*$", text, flags=re.M | re.S)


def diagram_alt(source: str) -> str:
    """A diagram's text alternative: its accTitle and accDescr (one line, or a { } block)."""
    title = re.search(r"^\s*accTitle\s*:\s*(.+?)\s*$", source, re.M)
    descr = re.search(r"^\s*accDescr\s*:\s*(.+?)\s*$", source, re.M) or re.search(r"^\s*accDescr\s*\{(.*?)\}", source, re.M | re.S)
    parts = [title.group(1) if title else "", " ".join(descr.group(1).split()) if descr else ""]
    return ". ".join(p.rstrip(".") for p in parts if p) + "." if any(parts) else "Diagram"


def file_sha256(doc: Doc) -> str:
    return hashlib.sha256((DOCS_DIR / doc.file).read_bytes()).hexdigest()


def reference(link: str) -> dict | None:
    """A link into the engine's documents ("INPUT_FIELDS.md#13-keys-that-select-prices-profiles-and-rates", as the data dictionary
    holds it) named in words, for a person to read: {"doc": "Input fields", "section": "1.3 Keys that select prices, profiles and
    rates", "href": its page here}. None for a document not shown here; when the anchor names no heading, the section is blank and the
    link goes to the top of the document."""
    path, _, frag = (link or "").strip().partition("#")
    doc = BY_FILE.get(path)
    if doc is None:
        return None
    anchor = _anchor(doc.slug, f"#{frag}" if frag else "")[1:]
    section = ""
    if anchor:
        m = re.search(rf'<h[1-6][^>]*\sid="{re.escape(anchor)}"[^>]*>(.*?)</h[1-6]>', _convert(doc.slug)[1], re.S)
        if m:
            section = _us(unescape(re.sub(r"<[^>]+>", "", m.group(1)))).strip()
    return {"doc": doc.title, "section": section, "href": page_url(doc.slug) + (f"#{anchor}" if section else "")}


@lru_cache(maxsize=1)
def input_sections() -> dict[str, dict[str, str]]:
    """{table: {field name, lower case: its section}} from the engine's input field reference (INPUT_FIELDS.md): the subsections of
    "1. `Measure.csv`" ("Identification and program link", "Quantities", ...), without their numbers and notes in parentheses. A table
    the reference does not split (ProgramCost) is left out. A record's page groups its fields by these."""
    path = DOCS_DIR / "INPUT_FIELDS.md"
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    table = section = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^## \d+\. `(\w+)\.csv`", line):
            table, section = m.group(1), ""
        elif line.startswith("## "):
            table = section = ""
        elif table and (m := re.match(r"^### [\d.]+ (.+)$", line)):
            section = re.sub(r"\s*\(.*\)\s*$", "", m.group(1)).strip()
        elif table and section and (m := re.match(r"^\| `(\w+)` \|", line)):
            out.setdefault(table, {}).setdefault(m.group(1).lower(), section)
    return out

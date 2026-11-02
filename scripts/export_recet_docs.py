"""Copy the pinned ReCET engine's documents into the platform (data/recet_docs/), so Help shows the documents of the engine CEDARS runs.

    uv run python scripts/export_recet_docs.py data/recet_docs/      (needs the pinned ReCET engine installed: engine/README.md)

Copies each document `cedars.core.engine_docs.DOCS` lists from the installed package's `recet/docs/` unchanged, draws each ```mermaid
block as an SVG file (diagrams/<document>-<n>.svg) with the Mermaid the engine itself ships (recet/web/static/vendor/mermaid, the npm
release, integrity checked when ReCET vendored it) in Chromium through Playwright, and writes manifest.json (the engine's version, the
pinned commit, each file's SHA-256 and each diagram's source SHA-256). Drawing the diagrams here means the pages show them with no script
and no inline style (the site's CSP allows neither; an SVG shown as an image keeps its own styles). Re-run it whenever the pin moves;
tests/test_engine_docs.py checks the shipped copies and diagrams are the pinned engine's. Chromium: PLAYWRIGHT_CHROMIUM_EXECUTABLE, else
Playwright's own (`uv run playwright install chromium`)."""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import recet  # noqa: E402

from cedars.core.engine_docs import DOCS, mermaid_blocks  # noqa: E402

# How the engine's own Docs pages draw them (recet/web/static/docs.js), with a font every browser has, as an image cannot load one
MERMAID_CONFIG = {
    "startOnLoad": False,
    "securityLevel": "strict",
    "theme": "default",
    "fontFamily": "Arial, Helvetica, sans-serif",
    "htmlLabels": False,
    "flowchart": {"htmlLabels": False},
}


def draw(sources: list[str]) -> list[str]:
    """Each Mermaid source as a standalone SVG document."""
    from playwright.sync_api import sync_playwright

    script = Path(recet.__file__).parent / "web" / "static" / "vendor" / "mermaid" / "mermaid.min.js"
    with sync_playwright() as p:
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(path=str(script))
        page.evaluate("c => mermaid.initialize(c)", MERMAID_CONFIG)
        svgs = [
            page.evaluate("async ([s, id]) => (await mermaid.render(id, s)).svg", [src, f"diagram{n}"]) for n, src in enumerate(sources, 1)
        ]
        browser.close()
    return [_sized(svg) for svg in svgs]


def _sized(svg: str) -> str:
    """Give the drawing its own width and height (Mermaid writes width="100%" and a max-width style for a page), so as an image it keeps
    its size and shrinks to fit a narrow screen."""
    box = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([\d.]+) ([\d.]+)"', svg)
    w, h = (round(float(box.group(3))), round(float(box.group(4)))) if box else (800, 600)
    head = re.match(r"<svg\b[^>]*>", svg).group(0)
    new = re.sub(r'\s(width|height|style)="[^"]*"', "", head)
    new = new.replace("<svg", f'<svg width="{w}" height="{h}"', 1)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + svg.replace(head, new, 1) + "\n"


def main(out_dir: Path) -> Path:
    src = Path(recet.__file__).parent / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.md"):
        old.unlink()
    drawings = out_dir / "diagrams"
    drawings.mkdir(exist_ok=True)
    for old in drawings.glob("*.svg"):
        old.unlink()
    files, diagrams = {}, {}
    for doc in DOCS:
        data = (src / doc.file).read_bytes()
        (out_dir / doc.file).write_bytes(data)
        files[doc.file] = hashlib.sha256(data).hexdigest()
        sources = mermaid_blocks(data.decode("utf-8"))
        for n, (source, svg) in enumerate(zip(sources, draw(sources) if sources else [], strict=True), 1):
            name = f"{doc.file.removesuffix('.md')}-{n}.svg"
            (drawings / name).write_text(svg, encoding="utf-8")
            diagrams[name] = {"source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}
    manifest = {
        "engine": {"version": recet.__version__, "commit": (ROOT / "engine" / "recet-commit").read_text(encoding="utf-8").strip()},
        "documents": files,
        "diagrams": diagrams,
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    print(main(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data" / "recet_docs")))

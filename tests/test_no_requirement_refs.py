"""The requirements document is internal (CLAUDE.md, Jake 2026-11-09): nothing in this repository names it or cites its requirement IDs,
not pages, page help, the FAQ, release notes, docs, code comments or test names. Say what the code does in plain words instead.

ReCET's documents and field catalog, exported unchanged from the pinned engine (`data/recet_docs/`, `data/dictionary/`), are the engine's
and are left out here: they change in the ReCET repository.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Built from parts so this file does not match itself
REFERENCE = re.compile(r"\b" + "B" + r"RD\b|\b" + "B" + r"R-\d{1,2}\b")
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "vendor", ".pytest_cache", ".ruff_cache", "staticfiles"}
ENGINE_EXPORTS = (ROOT / "data" / "recet_docs", ROOT / "data" / "dictionary")
TEXT = {".py", ".html", ".txt", ".md", ".yaml", ".yml", ".json", ".csv", ".toml", ".tf", ".js", ".css", ".cfg", ".ini", ".sh", ""}


def _files():
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT or SKIP_DIRS & set(p.relative_to(ROOT).parts):
            continue
        if any(p.is_relative_to(d) for d in ENGINE_EXPORTS):
            continue
        yield p


def test_no_file_cites_the_requirements_document():
    bad = {}
    for p in _files():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = [n for n, line in enumerate(text.splitlines(), 1) if REFERENCE.search(line)]
        if lines:
            bad[str(p.relative_to(ROOT))] = lines[:10]
    assert not bad, f"References to the internal requirements document (file: line numbers): {bad}"


def test_the_check_finds_a_reference():
    assert REFERENCE.search("priced before confirmation (" + "B" + "R-02.5)")
    assert REFERENCE.search("the " + "B" + "RD v0.5 cap")
    assert not REFERENCE.search("BR-ASSET or BRDGE or CBRD")

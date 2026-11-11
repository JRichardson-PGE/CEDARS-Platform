"""A `{# ... #}` comment is hidden only when it opens and closes on one line: the template language prints a multi-line one on the page
(UI review 3, X1: three pickers showed their notes as text). Longer notes use `{% comment %}` blocks."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPEN = re.compile(r"\{#")


def _unclosed(text: str) -> list[int]:
    lines = []
    for n, line in enumerate(text.split("\n"), 1):
        for m in OPEN.finditer(line):
            if "#}" not in line[m.end() :]:
                lines.append(n)
    return lines


def test_no_multiline_hash_comments():
    bad = {}
    for p in sorted((ROOT / "cedars").rglob("templates/**/*")):
        if p.suffix not in {".html", ".txt", ".xml", ".md"} or not p.is_file():
            continue
        if lines := _unclosed(p.read_text(encoding="utf-8")):
            bad[str(p.relative_to(ROOT))] = lines
    assert not bad, f"{{# ... #}} comments not closed on their own line (they print on the page; use {{% comment %}}): {bad}"


def test_the_check_finds_an_unclosed_comment():
    assert _unclosed("{# one line #}\n<p>x</p>") == []
    assert _unclosed("<p>{# a\n b #}</p>") == [1]

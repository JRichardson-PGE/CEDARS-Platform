"""Answers a person can read before they send a request: the help of the page they came from, and the user guides, pages' help and
frequently asked questions that share words with what they wrote. Site search finds what has every word of a short query; a message
is a few sentences, so this ranks by how many of its telling words an entry has, a word in the entry's title counting most."""

import re
from dataclasses import dataclass

from django.urls import reverse

from cedars.core import faq, page_help
from cedars.core import help as site_help

MOST = 5  # suggestions shown
MOST_WORDS = 40  # distinct words of a message that are looked for
# Words that say nothing about the subject (a message is prose): left out before matching.
COMMON = frozenset(
    """a about after again all also am an and any are as at be because been before being but by can cannot could did do does doing
    done each else for from get got had has have having he her here hers him his how however i if in into is it its just like me more
    most my need needs no not now of off on once one only or other our out over please same see she should so some still such than
    thank thanks that the their them then there these they this those through to too try trying under until up us use used using very
    want wanted was we were what when where which while who why will with would yet you your cedars page pages help work working
    works something anything nothing thing things know find found show shows shown tell says say said new old when""".split()
)


@dataclass(frozen=True)
class Suggestion:
    kind: str  # "User guide", "Page help", "Question"
    title: str
    url: str
    text: str = ""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def telling_words(message: str) -> list[str]:
    """The message's words that say what it is about, in the order met, each once."""
    seen: dict[str, None] = {}
    for w in _words(message):
        if len(w) > 2 and w not in COMMON and not w.isdigit():
            seen.setdefault(w)
    return list(seen)[:MOST_WORDS]


def _first_sentence(text: str, limit: int = 200) -> str:
    text = re.sub(r"<[^>]+>", "", text or "").strip()
    end = text.find(". ")
    text = text[: end + 1] if 0 < end < limit else text
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _entries() -> list[tuple[str, str, Suggestion]]:
    """Everything that can be suggested: (title, the rest of its words, the suggestion)."""
    out = [
        (g.title, f"{g.summary} {g.audience}", Suggestion("User guide", g.title, reverse("core:help_guide", args=[g.slug]), g.summary))
        for g in site_help.GUIDES
    ]
    faq_url = reverse("core:help_faq")
    out += [
        (q.q, " ".join((*q.tags, *q.a)), Suggestion("Question", q.q, f"{faq_url}#{q.id}", _first_sentence(q.a[0]) if q.a else ""))
        for q in faq.questions()
    ]
    out += [
        (
            e.title,
            " ".join((*e.guide, *e.how)),
            Suggestion("Page help", e.title, reverse("core:help_page", args=[e.key]), _first_sentence(e.guide[0]) if e.guide else ""),
        )
        for e in page_help.entries().values()
        if not e.key.startswith("feedback/")  # the form's own help answers nothing
    ]
    return out


def matching(message: str, *, skip_url: str = "") -> list[Suggestion]:
    """The entries that share the most telling words with the message, best first. An entry needs two of the words (one when the
    message has only one), so a single common word does not bring up half the site."""
    terms = telling_words(message)
    if not terms:
        return []
    need = 1 if len(terms) == 1 else 2
    found = []
    for i, (title, text, suggestion) in enumerate(_entries()):
        if suggestion.url == skip_url:
            continue
        in_title, in_text = set(_words(title)), set(_words(text))
        hit = score = 0
        for t in terms:
            if any(w.startswith(t) for w in in_title):
                hit, score = hit + 1, score + 3
            elif any(w.startswith(t) for w in in_text):
                hit, score = hit + 1, score + 1
        if hit >= need:
            found.append((-hit, -score, i, suggestion))
    return [f[3] for f in sorted(found)[:MOST]]


def for_page(key: str):
    """The help of the page a request is sent from (its page-help key), with its user guide: (entry, guide), or (None, None)."""
    entry = page_help.entries().get((key or "").strip("/")) if key else None
    if entry is None or entry.key.startswith("feedback/") or entry.key in page_help.WITHOUT_HELP:
        return None, None
    return entry, site_help.BY_SLUG.get(entry.see)

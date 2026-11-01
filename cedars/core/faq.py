"""The FAQ (BR-12.3: "a searchable FAQ and worked examples"): questions by topic, kept as data in core/faq.yaml, shown and searched at
/help/faq/ without scripts (?q= words, ?topic= one topic). Answers name limits by key ({limit:upload_mb}) and the release notice period
({notice_days}), filled in as they stand when the page is shown, so an answer never states an old number."""

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml

FILE = Path(__file__).resolve().parent / "faq.yaml"
PLACEHOLDER = re.compile(r"\{(limit:[a-z_]+|notice_days)\}")
QUESTION_KEYS = {"id", "q", "a", "tags", "guide", "links", "example"}
EXAMPLE_KEYS = {"title", "intro", "steps", "table", "result"}
WORD = re.compile(r"[a-z0-9&]+(?:[-'][a-z0-9]+)*")


class FAQError(ValueError):
    pass


@dataclass(frozen=True)
class Table:
    caption: str
    head: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Example:
    title: str
    intro: str = ""
    steps: tuple[str, ...] = ()
    table: Table | None = None
    result: str = ""


@dataclass(frozen=True)
class Question:
    id: str
    topic: str
    q: str
    a: tuple[str, ...]
    tags: tuple[str, ...] = ()
    guide: str = ""
    links: tuple[tuple[str, str], ...] = ()
    example: Example | None = None

    def words(self) -> dict[str, set[str]]:
        """The question's words by where they are, for scoring."""
        ex = self.example
        parts = [ex.title, ex.intro, *ex.steps, ex.result] if ex else []
        if ex and ex.table:
            parts += [c for r in ex.table.rows for c in r]
        example_text = " ".join(parts)
        return {
            "q": _words(self.q),
            "tags": _words(" ".join(self.tags)),
            "a": _words(" ".join(self.a) + " " + example_text),
        }


@dataclass(frozen=True)
class Topic:
    key: str
    title: str
    questions: tuple[Question, ...] = field(default_factory=tuple)


def _stem(word: str) -> str:
    for suffix in ("ies", "es", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in WORD.findall(text.lower())}


def _text(value) -> str:
    return " ".join(str(value).split())


def _fill(text: str) -> str:
    from cedars.services.releases import limits, service

    def one(m):
        key = m.group(1)
        if key == "notice_days":
            return str(service.notice_days())
        return limits.current(key.split(":", 1)[1])

    return PLACEHOLDER.sub(one, text)


def _example(qid: str, raw) -> Example | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - EXAMPLE_KEYS or not raw.get("title"):
        raise FAQError(f"{qid}: an example has a title and only {sorted(EXAMPLE_KEYS)}")
    table = None
    if raw.get("table"):
        t = raw["table"]
        head = tuple(_text(h) for h in t.get("head") or [])
        rows = tuple(tuple(_text(c) for c in r) for r in t.get("rows") or [])
        if not t.get("caption") or not head or not rows or any(len(r) != len(head) for r in rows):
            raise FAQError(f"{qid}: an example's table has a caption, a head and rows as long as the head")
        table = Table(_text(t["caption"]), head, rows)
    return Example(
        _text(raw["title"]),
        _text(raw.get("intro", "")),
        tuple(_text(s) for s in raw.get("steps") or []),
        table,
        _text(raw.get("result", "")),
    )


@cache
def _raw_topics() -> tuple[Topic, ...]:
    """The FAQ as written (placeholders not filled). Raises FAQError on a malformed entry (the tests load it)."""
    from .help import BY_SLUG

    data = yaml.safe_load(FILE.read_text(encoding="utf-8")) or {}
    topics, seen = [], set()
    for t in data.get("topics") or []:
        questions = []
        for raw in t.get("questions") or []:
            qid = raw.get("id", "")
            if not re.fullmatch(r"[a-z0-9-]+", qid or "") or qid in seen:
                raise FAQError(f"{qid!r}: every question has its own id (lowercase letters, digits and hyphens)")
            seen.add(qid)
            if set(raw) - QUESTION_KEYS:
                raise FAQError(f"{qid}: unknown fields {sorted(set(raw) - QUESTION_KEYS)}")
            if not raw.get("q") or not raw.get("a"):
                raise FAQError(f"{qid}: needs a question (q) and an answer (a)")
            if raw.get("guide", "") not in BY_SLUG:
                raise FAQError(f"{qid}: guide names no user guide ({raw.get('guide')!r})")
            answer = raw["a"] if isinstance(raw["a"], list) else [raw["a"]]
            links = tuple((_text(x["text"]), x["href"]) for x in raw.get("links") or [])
            questions.append(
                Question(
                    qid,
                    t["key"],
                    _text(raw["q"]),
                    tuple(_text(p) for p in answer),
                    tuple(_text(x) for x in raw.get("tags") or []),
                    raw["guide"],
                    links,
                    _example(qid, raw.get("example")),
                )
            )
        topics.append(Topic(t["key"], _text(t["title"]), tuple(questions)))
    return tuple(topics)


def _filled(q: Question) -> Question:
    ex = q.example
    if ex is not None:
        table = ex.table and Table(_fill(ex.table.caption), ex.table.head, tuple(tuple(_fill(c) for c in r) for r in ex.table.rows))
        ex = Example(_fill(ex.title), _fill(ex.intro), tuple(_fill(s) for s in ex.steps), table, _fill(ex.result))
    return Question(q.id, q.topic, _fill(q.q), tuple(_fill(p) for p in q.a), q.tags, q.guide, q.links, ex)


def topics() -> list[Topic]:
    """Every topic and question, with limits as they stand."""
    return [Topic(t.key, t.title, tuple(_filled(q) for q in t.questions)) for t in _raw_topics()]


def questions() -> list[Question]:
    return [q for t in topics() for q in t.questions]


def for_guide(slug: str) -> list[Question]:
    return [q for q in questions() if q.guide == slug]


def search(text: str, topic: str = "") -> list[Question]:
    """The questions with every word of `text` (in the question, its tags, its answer or its example), best first: a word in the
    question counts most, then in the tags. Words match whole or as the start of a longer word ("upload" finds "uploads")."""
    terms = [t for t in _words(text) if len(t) > 1]
    found = []
    for i, q in enumerate(questions()):
        if topic and q.topic != topic:
            continue
        if not terms:
            found.append((0, i, q))
            continue
        where = q.words()
        score = 0
        for term in terms:
            hits = {k for k, ws in where.items() if any(w == term or w.startswith(term) for w in ws)}
            if not hits:
                break
            score += 3 * ("q" in hits) + 2 * ("tags" in hits) + ("a" in hits)
        else:
            found.append((-score, i, q))
    return [q for _, _, q in sorted(found)]

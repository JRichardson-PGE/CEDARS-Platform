"""Site search (Jake, 2026-11-03): the box in the header finds, in one list, the pages a visitor can open, the help (user guides, each
page's help and the FAQ), glossary terms, programs, data-dictionary fields and documents.

Nothing is indexed: each part asks the same source its own page reads, with the same rules on who sees what. Pages are listed only to
the visitors their header shows them to (`all_pages()`: the header's and footer's pages, and every other page with its own help and a
fixed address, navigation review D6); documents come from the library's search, which keeps
unpublished documents to those who may read them; programs and dictionary fields are public, as their pages are. Each part shows its
best `PER_PART` matches and links to its own page's search for the rest, so a part never runs more than one query a page does.

Words match whole or as the start of a longer word ("upload" finds "uploads"), and every word must match (the FAQ's rule, faq.search)."""

import html
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from . import faq, nav, page_help
from . import help as site_help

PER_PART = 8  # the matches each part shows; "See all" links to that part's own page for more
MAX_QUERY = 200  # characters kept of a query


@dataclass(frozen=True)
class Page:
    """A page search lists by name: its URL name, the key of its help entry (title and description) and who sees it (core/nav.py's
    `who` values: any one is enough)."""

    url_name: str
    help_key: str
    who: tuple[str, ...] = ("",)
    words: str = ""  # other words it is known by


# Each page's help entry and other words it is known by. The header's pages (core/nav.py, listed to whoever the header shows them) and the
# footer's; tests/test_search.py checks every page the header or footer links to is here.
HELP_KEYS = {
    "core:home": ("data/front", "home front page program cycle"),
    "data:dashboards": ("data/dashboard", "portfolio dashboard charts"),
    "programs:catalog": ("programs/catalog", "programs list prgid"),
    "budgets:index": ("budgets/index", "filings budget filing"),
    "budgets:shifts": ("budgets/shifts", "filings fund shift operating budget"),
    "claims:index": ("claims/index", "filings claim"),
    "monthly:index": ("monthly/index", "filings monthly report"),
    "submissions:calendar": ("submissions/calendar", "filings due dates deadlines"),
    "submissions:filing_sets": (
        "submissions/filing_sets",
        "filing sets set-up edit cancel a filing claim year budget filing monthly reports",
    ),
    "submissions:workflow_settings": (
        "submissions/workflow_settings",
        "workflow settings correction policy reopen after due date late confirmation close-out",
    ),
    "rules:check": ("rules/check", "filings validation rules check upload"),
    "submissions:reopen_requests": ("submissions/reopen_requests", "filings correction corrections reopen approve"),
    "submissions:delegations": ("submissions/delegations", "approver delegate delegates leave away deputy"),
    "budgets:changes": ("budgets/changes", "change log letters corrections"),
    "claims:changes": ("claims/changes", "change log letters corrections"),
    "monthly:changes": ("monthly/changes", "change log letters corrections"),
    "calculations:runs": ("calculations/runs", "calculations recet cet cost effectiveness run"),
    "calculations:new": ("calculations/new", "calculations recet cet cost effectiveness run"),
    "calculations:drafts": ("calculations/drafts", "calculations recet"),
    "calculations:templates": ("calculations/templates", "calculations recet"),
    "impact_profiles:library": ("impact_profiles/library", "load shapes"),
    "core:recet_docs": ("core/help/recet_docs", "calculations cet engine recet documents"),
    "calculations:engines": ("calculations/engines", "recet"),
    "data:records": ("data/records", "download records data"),
    "data:views": ("data/views", "saved views dashboard"),
    "documents:library": ("documents/library", "pda psr emv deer"),
    "documents:studies": ("documents/studies", "emv evaluation studies work orders"),
    "dictionary:index": ("dictionary/index", "fields data dictionary"),
    "refdata:index": ("refdata/index", "acc avoided costs data packs reference data"),
    "refdata:cost_tests": ("refdata/cost_tests", "cost tests trc pac builder yaml"),
    "administration:allocations": ("administration/allocations", "statewide sw"),
    "rules:index": ("rules/index", "validation"),
    "dictionary:lists": ("dictionary/lists", "codes value lists"),
    "mandates:index": ("releases/mandates", "mandated changes"),
    "administration:organisations": ("administration/organisations", "organisations"),
    "administration:people": ("administration/people", "people users accounts deactivate reactivate disable name email"),
    "administration:role_templates": ("administration/role_templates", "roles permissions"),
    "administration:stewardship": ("administration/stewardship", ""),
    "administration:settings": ("administration/settings", "access settings"),
    "administration:audit": ("administration/audit", "log history"),
    "administration:goal_sets": ("administration/goal_sets", "goals savings targets decision tsb gwh template"),
    "snapshots:holds": ("snapshots/holds", "publication"),
    "accessibility:files": ("accessibility/files", ""),
    "retention:index": ("retention/index", ""),
    "releases:manage": ("releases/manage", "release notes write"),
    "mandates:manage": ("releases/mandates_manage", "mandated changes register"),
    "feedback:inbox": ("feedback/inbox", ""),
    "administration:health": ("administration/health", ""),
    "core:filings": ("core/section_filings", "filings status budgets claims monthly reports every pa"),
    "core:calculations": ("core/section_calculations", "calculations recet cet cost effectiveness"),
    "core:published": ("core/section_published", "published data public open data downloads"),
    "core:reference": ("core/section_reference", "reference data acc avoided costs"),
    "core:administration": ("core/section_administration", "administration admin"),
    "core:queue": ("core/queue", "my queue work waiting"),
    "notifications:index": ("notifications/index", ""),
    "administration:account": ("administration/account", "my account api token profile"),
    "programs:fields": ("programs/fields", ""),
    "programs:requests": ("programs/requests", ""),
    "core:help": ("core/help/index", "support"),
    "core:help_faq": ("core/help/faq", "faq questions"),
    "core:help_contact": ("core/help/contact", "support email"),
    "core:about": ("core/about", ""),
    "releases:index": ("releases/index", "changes what's new"),
    "limits:index": ("releases/limits", ""),
    "feedback:form": ("feedback/form", ""),
}
# Pages outside the menus, and who may open them.
OTHER_PAGES = {
    "core:home": ("",),
    # each menu's own page, to those who see the menu
    "core:filings": ("member",),
    "core:calculations": ("user",),
    "core:published": ("",),
    "core:reference": ("user",),
    "core:administration": ("sysadmin", "org_admin", "steward"),
    "programs:fields": ("",),
    "programs:requests": ("user",),
    "core:queue": ("member",),
    "notifications:index": ("user",),
    "administration:account": ("user",),
    "core:help": ("",),
    "core:help_faq": ("",),
    "core:help_contact": ("",),
    "core:about": ("",),
    "releases:index": ("",),
    "limits:index": ("",),
    "feedback:form": ("",),
}


# Pages with a help entry and a fixed address whose help key is not their URL name with "/" for ":" (navigation review D6).
OTHER_KEYS = {
    "core:help_tour": "core/help/tour",
    "refdata:build_start": "refdata/builder/start",
    "documents:study_summary": "documents/summary",
    "documents:new_study": "documents/study_form",
}
# Who may open a page that no menu item says, or says too widely (its menu item's who otherwise).
WHO = {
    "administration:request_access": ("user",),
    "notifications:settings": ("user",),
    "documents:new": ("user",),
    "documents:subscriptions": ("user",),
    "documents:subscribers": ("sysadmin",),
    "documents:aging": ("user",),
    "documents:new_study": ("user",),
    "data:downloads": ("user",),
    "impact_profiles:custom_new": ("member",),
    "administration:organisation_new": ("sysadmin",),
    "budgets:filing_new": ("sysadmin",),
    "claims:year_new": ("sysadmin",),
    "monthly:year_new": ("sysadmin",),
    "refdata:build_start": ("user",),
    "programs:locked": ("user",),
}
SKIP = {"core:search"}  # this page itself


def _url_names() -> list[str]:
    from django.urls import get_resolver

    out = []

    def walk(res, ns=None):
        for p in res.url_patterns:
            if hasattr(p, "url_patterns"):
                walk(p, ":".join(x for x in (ns, p.namespace) if x) if p.namespace else ns)
            elif p.name:
                out.append(f"{ns}:{p.name}" if ns else p.name)

    walk(get_resolver())
    return out


@cache
def all_pages() -> tuple[Page, ...]:
    """Every page search lists by name: the menus' and the footer's (HELP_KEYS, with the words they are known by), then every other
    page with a help entry and an address that takes no arguments (navigation review D6), each listed to whoever may open it: the roles
    of the menu items that hold it (core/nav.py), OTHER_PAGES or WHO."""
    from django.urls import NoReverseMatch

    entries = page_help.entries()
    every = [(i,) for m in nav.MENUS for i in m.items]
    who: dict[str, set[str]] = {}
    for m in nav.MENUS:
        for i in m.items:
            who.setdefault(i.url_name, set()).update(i.who)
    for name, roles in OTHER_PAGES.items():
        who.setdefault(name, set()).update(roles)
    pages = [Page(name, HELP_KEYS[name][0], tuple(sorted(r)), HELP_KEYS[name][1]) for name, r in who.items()]
    listed = set(who)
    for name in _url_names():
        if name in listed or name in SKIP:
            continue
        key = OTHER_KEYS.get(name, name.replace(":", "/"))
        if key not in entries:
            continue
        try:
            reverse(name)
        except NoReverseMatch:  # an address with arguments: found through its parent page
            continue
        roles = WHO.get(name) or (h[0].who if (h := nav.holder(name, every)) else None)
        if roles is None:
            continue
        listed.add(name)
        pages.append(Page(name, key, tuple(sorted(roles))))
    return tuple(pages)


@dataclass(frozen=True)
class Hit:
    title: str
    url: str
    text: str = ""  # one line about it
    kind: str = ""  # a short label shown before the title, e.g. "User guide"


@dataclass
class Part:
    key: str
    title: str
    hits: list[Hit] = field(default_factory=list)
    total: int = 0  # all its matches (more than shown when it has a See all link)
    more_url: str = ""  # its own page's search
    more_text: str = ""


def clean(q: str) -> str:
    return " ".join((q or "").split())[:MAX_QUERY]


def _terms(q: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", q.lower()) if len(t) > 1 or t.isdigit()]


def _score(terms: list[str], title: str, text: str) -> int:
    """0 when some word of the query matches nothing; otherwise more for words in the title (3) than in the text (1)."""
    title_words, text_words = set(_terms(title)), set(_terms(text))
    score = 0
    for t in terms:
        in_title = any(w.startswith(t) for w in title_words)
        if not in_title and not any(w.startswith(t) for w in text_words):
            return 0
        score += 3 if in_title else 1
    return score


def _ranked(terms, items):
    """items: (title, text, hit). The matching hits, best first, in their own order between equals."""
    found = [(-s, i, hit) for i, (title, text, hit) in enumerate(items) if (s := _score(terms, title, text))]
    return [hit for _, _, hit in sorted(found, key=lambda f: (f[0], f[1]))]


def _part(key, title, hits, more_url="", more_text="") -> Part:
    return Part(key, title, hits[:PER_PART], len(hits), more_url if len(hits) > PER_PART else "", more_text)


# --- who sees a page


def _roles(user) -> set[str]:
    return nav.roles_of(user)


def pages(user, terms) -> Part:
    roles = _roles(user)
    entries = page_help.entries()
    items = []
    for p in all_pages():
        if not roles.intersection(p.who):
            continue
        e = entries[p.help_key]
        title = e.title.removeprefix("The ")
        title = title[0].upper() + title[1:]
        items.append((f"{title} {p.words}", " ".join(e.guide), Hit(title, reverse(p.url_name), _first_sentence(e.guide[0]))))
    return _part("pages", "Pages", _ranked(terms, items))


# --- help: user guides, each page's help and the FAQ


def help_part(terms, q: str) -> Part:
    items = [
        (g.title, f"{g.summary} {g.audience}", Hit(g.title, reverse("core:help_guide", args=[g.slug]), g.summary, "User guide"))
        for g in site_help.GUIDES
    ]
    items += [
        (
            e.title,
            " ".join((*e.guide, *e.how)),
            Hit(e.title, reverse("core:help_page", args=[e.key]), _first_sentence(e.guide[0]) if e.guide else "", "Page help"),
        )
        for e in page_help.entries().values()
        if e.key != "core/search"  # this page's own help would match most searches
    ]
    hits = _ranked(terms, items)
    faq_url = reverse("core:help_faq")
    questions = [Hit(x.q, f"{faq_url}#{x.id}", _first_sentence(_plain(x.a[0])) if x.a else "", "Question") for x in faq.search(q)]
    hits = _merge(hits, questions)
    return _part("help", "Help", hits, f"{faq_url}?{urlencode({'q': q})}", "Search the frequently asked questions")


def _merge(a: list, b: list) -> list:
    """Two best-first lists, alternating, so neither crowds the other out of the first few."""
    out = []
    for i in range(max(len(a), len(b))):
        out += [x[i] for x in (a, b) if i < len(x)]
    return out


# --- the glossary (core/templates/core/help/glossary.html, read as written so it stays the only copy) and the PA codes table

_ENTRY = re.compile(r'<dt id="([^"]+)">(.*?)</dt>\s*<dd>(.*?)</dd>', re.S)


def _plain(text: str) -> str:
    text = re.sub(r"\{%.*?%\}|\{\{.*?\}\}", "", text, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", text)).split())


def _first_sentence(text: str, limit: int = 200) -> str:
    """The first sentence, or the first two when the first is very short ("Yes."), cut at `limit` characters."""
    text = _plain(text)
    sentences = re.findall(r".+?[.!?](?:\s|$)|.+$", text)
    out = ""
    for sentence in sentences:
        out = (out + " " + sentence.strip()).strip()
        if len(out) >= 60:
            break
    return out if len(out) <= limit else out[: limit - 1].rsplit(" ", 1)[0] + "…"


@cache
def glossary_entries() -> tuple[tuple[str, str, str], ...]:
    """(anchor, term, definition) of every glossary entry."""
    path = Path(__file__).parent / "templates" / "core" / "help" / "glossary.html"
    return tuple((a, _plain(t), _plain(d)) for a, t, d in _ENTRY.findall(path.read_text(encoding="utf-8")))


def glossary(terms) -> Part:
    url = reverse("core:help_guide", args=["glossary"])
    items = [(term, text, Hit(term, f"{url}#{anchor}", _first_sentence(text))) for anchor, term, text in glossary_entries()]
    from cedars.services.accounts import service as accounts

    for code, kind in accounts.KNOWN_PA_TYPES.items():
        name = accounts.known_pa_name(code)
        items.append((code, f"{name} {kind}", Hit(code, f"{url}#pa-codes", f"{name} ({kind}), a program administrator code.")))
    return _part("glossary", "Glossary", _ranked(terms, items))


# --- programs, dictionary fields and documents: each asks its own page's source, with its page's rules


def programs(q: str) -> Part:
    """The program catalog's search (programs.views._filtered): the query in a PrgID, name or commercial name, the version in force."""
    from cedars.services.registry import service as registry

    needle = q.lower()
    hits = []
    for p, v in registry.catalog(None):
        if v is None or not (needle in p.prg_id.lower() or needle in v.name.lower() or needle in v.commercial_name.lower()):
            continue
        pa = "SW" if p.statewide or p.administrator is None else p.administrator.code
        status = "closed" if v.status == "closed" else "open"
        hits.append(Hit(f"{p.prg_id}: {v.name}", reverse("programs:program", args=[p.page_prg_id]), f"{pa} program, {status}"))
    # an exact PrgID first
    hits.sort(key=lambda h: not h.title.lower().startswith(needle + ":"))
    url = reverse("programs:catalog")
    return _part("programs", "Programs", hits, f"{url}?{urlencode({'q': q})}", "See every match in the program catalog")


def fields(q: str) -> Part:
    """The data dictionary's search (dictionary.views.index): the query in a field's name, definition or another name; retired fields
    left out, as the dictionary leaves them out until asked."""
    from cedars.services.dictionary import service as dictionary
    from cedars.services.dictionary.models import Field

    v = dictionary.current()
    if v is None:
        return Part("fields", "Data dictionary fields")
    qs = (
        Field.objects.filter(version=v)
        .filter(Q(name__icontains=q) | Q(definition__icontains=q) | Q(aliases__name__icontains=q))
        .exclude(state=Field.State.RETIRED, retired_from__lte=timezone.localdate())
        .distinct()
    )
    total = qs.count()
    # a name match first, then the rest, by name
    shown = list(qs.filter(name__icontains=q).order_by("name")[:PER_PART])
    if len(shown) < PER_PART:
        shown += list(qs.exclude(name__icontains=q).order_by("name")[: PER_PART - len(shown)])
    hits = [Hit(f.name, reverse("dictionary:field", args=[f.name]), _first_sentence(f.definition), f.get_kind_display()) for f in shown]
    url = reverse("dictionary:index")
    part = Part("fields", "Data dictionary fields", hits, total)
    if total > PER_PART:
        part.more_url, part.more_text = f"{url}?{urlencode({'q': q})}", "See every match in the data dictionary"
    return part


def documents(user, q: str) -> Part:
    """The document library's search (documents.service.search): published documents for everyone, unpublished ones only for those
    who may read them."""
    from cedars.modules.documents import service as docs

    qs = docs.search(user, docs.Search(q=q))
    total = qs.count()
    hits = [
        Hit(d.title, reverse("documents:document", args=[d.pk]), _first_sentence(d.summary), d.get_collection_display())
        for d in qs[:PER_PART]
    ]
    part = Part("documents", "Documents", hits, total)
    if total > PER_PART:
        part.more_url, part.more_text = f"{reverse('documents:library')}?{urlencode({'q': q})}", "See every match in the document library"
    return part


def search(user, q: str) -> list[Part]:
    """Every part with a match, in the order people look for things: pages, help, the glossary, programs, fields, documents."""
    q = clean(q)
    terms = _terms(q)
    if not terms:
        return []
    parts = [pages(user, terms), help_part(terms, q), glossary(terms), programs(q), fields(q), documents(user, q)]
    return [p for p in parts if p.hits]

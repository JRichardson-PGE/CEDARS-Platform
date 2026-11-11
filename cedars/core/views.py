from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import engine_docs, faq, queue, revisions
from . import help as site_help
from . import page_help as pages
from . import search as site_search

# What the platform will hold, in the order the phases deliver it (architecture page, "Phasing toward program year 2028").
# What CEDARS holds (About CEDARS; navigation review D8, L4: what the platform holds, not the build plan): each part, what it does and
# its page.
MODULES = [
    (
        "Portfolio dashboard and record downloads",
        "Published results by year, PA, program and sector, and record-level downloads.",
        "data:dashboards",
    ),
    ("Programs", "The program catalog and one page per program, closed programs included.", "programs:catalog"),
    ("Budgets", "Budget filings for each four-year cycle, fund shifts and the operating budget.", "budgets:index"),
    ("Claims", "Quarterly and annual claims on the common template.", "claims:index"),
    ("Monthly reports", "Monthly expenditures and commitments against the operating budget.", "monthly:index"),
    ("Calculations", "Cost-effectiveness runs on the ReCET engine: new runs, results, comparisons.", "core:calculations"),
    ("Impact profiles", "Deemed load shapes from eTRM and custom impact profiles.", "impact_profiles:library"),
    ("Documents", "PA documents, CPUC guidance, DEER resources, program files and EM&V documents and studies.", "documents:library"),
    (
        "Reference data",
        "The ACCs, eTRM packs, statewide allocation factors and cost-test sets runs and filings are priced on.",
        "core:reference",
    ),
    (
        "Data dictionary and validation rules",
        "Every field's definition, the value lists and the rules each filing is checked against.",
        "dictionary:index",
    ),
]


# A menu's own page (Jake, 2026-11-06): the header's headings link here. Each lists the menu's pages the visitor may open, in its
# groups, with a sentence on each (core/nav.py); Filings adds where every PA's budgets, claims and monthly reports stand.
SECTIONS = {
    "filings": "filings",
    "calculations": "calculations",
    "published": "published",
    "reference": "reference",
    "administration": "admin",
}


@require_GET
def section(request, page):
    from django.contrib.auth.views import redirect_to_login

    from . import nav

    key = SECTIONS[page]
    if page == "reference" and "kind" in request.GET:  # the data packs' list was here, filtered by kind, before it moved
        from django.shortcuts import redirect

        return redirect(f"{reverse('refdata:index')}?{request.GET.urlencode()}")
    menu = next((m for m in nav.header(request) if m.key == key), None)
    if menu is None:  # the visitor sees none of its pages
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        raise Http404
    ctx = {"menu": menu, "groups": menu.groups}
    if key == "filings":
        from cedars.services.submissions import status

        ctx["board"] = status.board(request.user)
    return render(request, f"core/section_{page}.html", ctx)


@login_required
@require_GET
def after_set_up(request):
    """Continue, on the recovery codes page after the first second factor is set up (UI review 3, U6): an account that belongs to no
    organization goes on to Ask for access, the third step; any other goes where its sign-in was heading, else the front page."""
    from django.shortcuts import redirect

    from cedars.services.accounts import service as access
    from cedars.services.accounts.signals import FIRST_SET_UP

    heading = (request.session.pop(FIRST_SET_UP, None) or {}).get("next", "")
    if not access.organisations_of(request.user).exists():
        return redirect("administration:request_access")
    return redirect(heading or "core:home")


def about(request):
    return render(request, "core/about.html", {"modules": MODULES})


@login_required
@require_GET
def my_queue(request):
    """The work waiting on the signed-in user across every module, read live from each module's records (core/queue.py)."""
    sections = queue.sections(request.user)
    return render(request, "core/queue.html", {"sections": sections, "total": sum(len(s.items) for s in sections)})


@require_GET
def help_index(request):
    return render(
        request,
        "core/help/index.html",
        {
            "guides": site_help.GUIDES,
            "readers": site_help.by_reader(),
            "support": site_help.support(),
            "engine_docs": engine_docs.DOCS,
        },
    )


@require_GET
def recet_docs(request):
    """ReCET's own documents, from the pinned engine (core/engine_docs.py). Public, as they are in the engine's public repository."""
    return render(request, "core/help/recet_docs.html", {"docs": engine_docs.DOCS, "engine": engine_docs.engine()})


@require_GET
def recet_doc(request, slug):
    doc = engine_docs.BY_SLUG.get(slug)
    if doc is None:
        raise Http404
    return render(
        request,
        "core/help/recet_doc.html",
        {
            "doc": doc,
            "docs": engine_docs.DOCS,
            "page": engine_docs.render(slug),
            "engine": engine_docs.engine(),
            "source": engine_docs.source_url(doc),
        },
    )


@require_GET
def help_guide(request, slug):
    guide = site_help.BY_SLUG.get(slug)
    if guide is None:
        raise Http404
    return render(
        request,
        f"core/help/{slug}.html",
        {
            "guide": guide,
            "guides": site_help.GUIDES,
            "support": site_help.support(),
            "questions": faq.for_guide(slug),
            "revision": _revision(guide),
            **({"pa_codes": _pa_codes()} if slug == "glossary" else {}),
        },
    )


def _pa_codes() -> list[tuple[str, str, str]]:
    """The glossary's program administrator codes: each code CEDARS knows (accounts.KNOWN_PA_TYPES, former codes included), its name
    and its type, IOUs first, then RENs and CCAs."""
    from cedars.services.accounts import service as accounts

    order = {"IOU": 0, "REN": 1, "CCA": 2}
    rows = []
    for code, kind in accounts.KNOWN_PA_TYPES.items():
        name = accounts.known_pa_name(code)
        if code in accounts.KNOWN_RENAMES:  # a former code: say what it became and when
            new_code, new_name, start, _ = accounts.KNOWN_RENAMES[code]
            name = f"{name} (before {start.year}; now {new_name}, {new_code})"
        rows.append((code, name, kind))
    return sorted(rows, key=lambda r: (order.get(r[2], 9), r[0]))


def _revision(guide) -> dict:
    """The release a guide was revised for, the release the site runs, and the releases in effect since that changed its part."""
    from cedars.services.releases import service as releases

    behind = next((g.behind for g in releases.guide_revisions() if g.slug == guide.slug), [])
    return {"release": guide.revised, "running": releases.running(), "behind": behind}


@require_GET
def help_faq(request):
    """The FAQ: every question by topic, or those matching ?q= words, in ?topic= one topic. Works without scripts."""
    q = " ".join(request.GET.get("q", "").split())[:200]
    topics = faq.topics()
    topic = request.GET.get("topic", "")
    if topic not in {t.key for t in topics}:
        topic = ""
    searching = bool(q or topic)
    return render(
        request,
        "core/help/faq.html",
        {
            "topics": topics,
            "q": q,
            "topic": topic,
            "searching": searching,
            "results": faq.search(q, topic) if searching else [],
            "topic_titles": {t.key: t.title for t in topics},
            "revision": {"release": revisions.revised("faq"), "running": settings.VERSION},
        },
    )


@require_GET
def search(request):
    """Site search (the header's box): pages, help, the glossary, programs, dictionary fields and documents in one list, each part
    filtered by what this visitor may see (core/search.py). Works without scripts."""
    q = site_search.clean(request.GET.get("q", ""))
    parts = site_search.search(request.user, q) if q else []
    return render(request, "core/search.html", {"q": q, "parts": parts, "total": sum(p.total for p in parts)})


@require_GET
def help_contact(request):
    return render(request, "core/help/contact.html", {"support": site_help.support()})


@require_GET
def help_tour(request):
    """The tour as one page: every step, for anyone without scripts or who would rather read it."""
    return render(request, "core/help/tour.html", {"tour": site_help.tour_steps(site_help.visitor(request))})


@require_GET
def help_pages(request):
    """Every page's help, by part of CEDARS: the same guides the (?) beside each heading opens."""
    return render(request, "core/help/pages.html", {"areas": pages.by_area()})


@require_GET
def help_page(request, key):
    """One page's help on a page of its own: what the (?) beside its heading opens without scripts, with the tour written out. ?from= is
    the page it was opened from (a local path only), for the links back to it and to its tour."""
    entry = pages.entries().get(key.strip("/"))
    if entry is None:
        raise Http404
    back = request.GET.get("from", "")
    if not back.startswith("/") or not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = ""
    steps = pages.steps_for(entry, request)
    tour_link = (back + ("&" if "?" in back else "?") + "tour=1") if back and steps else ""
    if entry.tour_from == "front":
        tour_link = "/?tour=1"
    return render(
        request,
        "core/help/page.html",
        {"entry": entry, "steps": steps, "back": back, "tour_link": tour_link, "guide": site_help.BY_SLUG.get(entry.see)},
    )


# Paths no crawler needs: each record page and download runs a query over the published records, and a crawler following every
# dashboard row's link to its records kept a small server busy (2026-11-02).
ROBOTS_DISALLOW = ("/data/records/", "/data/download", "/api/", "/accounts/", "/feedback/")


@require_GET
def robots_txt(request):
    """What crawlers may read: nothing at all where ROBOTS_DISALLOW_ALL is set (the sandbox and demos), else everything but the
    query-heavy record and download paths, the API and the sign-in pages."""
    paths = ("/",) if settings.ROBOTS_DISALLOW_ALL else ROBOTS_DISALLOW
    body = "User-agent: *\n" + "".join(f"Disallow: {p}\n" for p in paths)
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


@never_cache
def healthz(request):
    """The load balancer's check: the process answers and the database does."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - any database failure means unhealthy
        return JsonResponse({"status": "error", "database": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})

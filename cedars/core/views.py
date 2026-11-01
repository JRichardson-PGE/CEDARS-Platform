from django.conf import settings
from django.db import connection
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import faq, revisions
from . import help as site_help
from . import page_help as pages

# What the platform will hold, in the order the phases deliver it (architecture page, "Phasing toward program year 2028").
MODULES = [
    ("Programs", "The program catalog and one page per program, closed programs included.", 2),
    ("Calculations", "Cost-effectiveness runs on the ReCET engine: new runs, results, comparisons.", 2),
    ("Impact profiles", "Deemed load shapes from eTRM and custom impact profiles.", 2),
    ("Budgets", "Budget filings for each four-year cycle, and fund shifts.", 3),
    ("Claims", "Quarterly and annual claims on the common template.", 4),
    ("Monthly reports", "Monthly expenditures and commitments against the operating budget.", 5),
    ("Data and downloads", "Dashboards and filtered downloads of published data.", 6),
    ("Documents", "PA documents, CPUC guidance, DEER resources, program files and EM&V documents, replacing the PDA and PSR.", 6),
]


def about(request):
    return render(request, "core/about.html", {"modules": MODULES})


@require_GET
def help_index(request):
    return render(request, "core/help/index.html", {"guides": site_help.GUIDES, "support": site_help.support()})


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
        },
    )


def _revision(guide) -> dict:
    """The release a guide was revised for, the release the site runs, and the releases in effect since that changed its part (BR-12.5)."""
    from cedars.services.releases import service as releases

    behind = next((g.behind for g in releases.guide_revisions() if g.slug == guide.slug), [])
    return {"release": guide.revised, "running": releases.running(), "behind": behind}


@require_GET
def help_faq(request):
    """The FAQ (BR-12.3): every question by topic, or those matching ?q= words, in ?topic= one topic. Works without scripts."""
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


@never_cache
def healthz(request):
    """The load balancer's check: the process answers and the database does."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - any database failure means unhealthy
        return JsonResponse({"status": "error", "database": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})

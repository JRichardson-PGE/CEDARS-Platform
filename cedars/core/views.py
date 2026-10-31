from django.db import connection
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import help as site_help

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
        {"guide": guide, "guides": site_help.GUIDES, "support": site_help.support()},
    )


@require_GET
def help_contact(request):
    return render(request, "core/help/contact.html", {"support": site_help.support()})


@require_GET
def help_tour(request):
    """The tour as one page: every step, for anyone without scripts or who would rather read it."""
    return render(request, "core/help/tour.html", {"tour": site_help.tour_steps(site_help.visitor(request))})


@never_cache
def healthz(request):
    """The load balancer's check: the process answers and the database does."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - any database failure means unhealthy
        return JsonResponse({"status": "error", "database": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})

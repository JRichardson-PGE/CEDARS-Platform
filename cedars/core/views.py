from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache

# What the platform will hold, in the order the phases deliver it (architecture page, "Phasing toward program year 2028").
MODULES = [
    ("Programs", "The program catalog and one page per program, closed programs included.", 2),
    ("Calculations", "Cost-effectiveness runs on the ReCET engine: new runs, results, comparisons.", 2),
    ("Impact profiles", "Deemed load shapes from eTRM and custom impact profiles.", 2),
    ("Budgets", "Budget filings for each four-year cycle, and fund shifts.", 3),
    ("Claims", "Quarterly and annual claims on the common template.", 4),
    ("Monthly reports", "Monthly expenditures and commitments against the operating budget.", 5),
    ("Data and downloads", "Dashboards and filtered downloads of published data.", 6),
    ("Documents", "Filed documents and DEER resources.", 6),
]


def home(request):
    return render(request, "core/home.html", {"modules": MODULES})


@never_cache
def healthz(request):
    """The load balancer's check: the process answers and the database does."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - any database failure means unhealthy
        return JsonResponse({"status": "error", "database": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})

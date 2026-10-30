from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import service
from .models import LegalHold, PurgeRun, RetentionPolicy


@login_required
def index(request):
    """Retention periods per record class, legal holds and the last purges (decision M; system administrators)."""
    if not access.is_system_admin(request.user):
        raise PermissionDenied("Only a CPUC system administrator manages retention.")
    if request.method == "POST":
        action, reason = request.POST.get("action"), request.POST.get("reason", "")
        try:
            if action == "period":
                policy = get_object_or_404(RetentionPolicy, pk=request.POST.get("policy"))
                raw = request.POST.get("keep_days", "").strip()
                service.set_period(request.user, policy, None if raw in ("", "permanent") else int(raw), reason)
                messages.success(request, f"{policy.label}: period changed.")
            elif action == "hold":
                service.place_hold(request.user, request.POST.get("record_class", ""), reason, request.POST.get("object_ref", ""))
                messages.success(request, "Legal hold placed.")
            elif action == "release":
                service.release_hold(request.user, get_object_or_404(LegalHold, pk=request.POST.get("hold")), reason)
                messages.success(request, "Legal hold released.")
        except ValueError:
            messages.error(request, "Enter the period as a whole number of days, or leave it blank for permanent.")
        except AccessDenied as e:
            messages.error(request, str(e))
        return redirect("retention:index")
    policies = RetentionPolicy.objects.all()
    purgers = service.purgers()
    return render(
        request,
        "retention/index.html",
        {
            "policies": [(p, p.record_class in purgers) for p in policies],
            "classes": [(p.record_class, p.label) for p in policies],
            "holds": LegalHold.objects.filter(released_at__isnull=True).select_related("placed_by"),
            "runs": PurgeRun.objects.filter(dry_run=False)[:10],
        },
    )

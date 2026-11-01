"""Publication holds (BR-08.5): the snapshots the scan for personal information held back from the public release, what it found,
and the system administrators' release and scan settings. A held filing's own organization sees its holds; nothing here shows a value
the scan found (masked examples and record IDs only)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import pii, service
from .models import PIIScan, Snapshot


def _rows(scans):
    return [
        {"scan": s, "snap": s.snapshot, "label": service.label(s.snapshot.version), "superseded": service.superseded(s.snapshot)}
        for s in scans
    ]


@login_required
def holds(request):
    sysadmin = access.is_system_admin(request.user)
    scans = list(service.holds(request.user)[:200])
    rows = _rows(scans)
    return render(
        request,
        "snapshots/holds.html",
        {
            "waiting": [r for r in rows if r["scan"].held and not r["superseded"]],
            "past": [r for r in rows if not (r["scan"].held and not r["superseded"])],
            "sysadmin": sysadmin,
        },
    )


@login_required
def hold(request, pk):
    snap = get_object_or_404(Snapshot.objects.select_related("organisation", "version__submission__deadline__period"), pk=pk)
    if not service.may_see(request.user, snap):
        raise Http404
    try:
        scan = snap.pii_scan
    except PIIScan.DoesNotExist:
        raise Http404 from None
    if request.method == "POST":
        try:
            if request.POST.get("action") == "release":
                service.release_hold(request.user, snap, request.POST.get("reason", ""))
                messages.success(request, "Released: the records are in the public dashboards and downloads now.")
            elif request.POST.get("action") == "rescan":
                scan = service.rescan(request.user, snap)
                messages.success(
                    request,
                    "Scanned again: still held." if scan.held else "Scanned again: nothing found, so it is published now.",
                )
        except AccessDenied as e:
            messages.error(request, str(e))
        return redirect("snapshots:hold", pk=snap.pk)
    findings = [{**f, "label": pii.BY_CODE[f["pattern"]].label if f["pattern"] in pii.BY_CODE else f["pattern"]} for f in scan.findings]
    return render(
        request,
        "snapshots/hold.html",
        {
            "snap": snap,
            "scan": scan,
            "label": service.label(snap.version),
            "findings": findings,
            "superseded": service.superseded(snap),
            "sysadmin": access.is_system_admin(request.user),
            "module": service.module_of(snap),
        },
    )


@login_required
def scan_settings(request):
    if not access.is_system_admin(request.user):
        raise PermissionDenied("Only a CPUC system administrator changes the publication scan.")
    if request.method == "POST":
        exempt = [line for raw in request.POST.get("exempt_columns", "").splitlines() for line in raw.split(",")]
        try:
            service.set_scan_settings(
                request.user, patterns=request.POST.getlist("patterns"), exempt_columns=exempt, reason=request.POST.get("reason", "")
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Scan settings saved. They apply to the next publication, and to a held one scanned again.")
        return redirect("snapshots:scan_settings")
    cfg = service.scan_settings()
    return render(
        request,
        "snapshots/scan_settings.html",
        {"cfg": cfg, "patterns": [(p, p.code in cfg.patterns) for p in pii.PATTERNS], "exempt": "\n".join(cfg.exempt_columns)},
    )

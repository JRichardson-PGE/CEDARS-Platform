"""The accounts service's work in each user's queue (cedars.core.queue): access requests waiting on an organization's
administrators (or on the CPUC's system administrators while it has none), PII grants and access-policy changes waiting on a data
steward."""

from __future__ import annotations

from cedars.core.queue import Item

from . import permissions as perms
from . import service
from .models import AccessRequest, Grant, PolicyChange


def access_requests(user) -> list[Item]:
    if user is None or not user.is_authenticated:
        return []
    orgs = service.organisations_deciding(user)
    out = []
    pending = AccessRequest.objects.filter(status=AccessRequest.Status.PENDING, organisation__in=orgs).select_related(
        "user", "organisation"
    )
    for r in pending:
        who = r.user.name or r.user.email
        out.append(
            Item(
                f"{who} asks to join {r.organisation.code}",
                f"/admin/organizations/{r.organisation.code}/",
                r.organisation.code,
                r.created_at,
                None,
                ("Asks for: " + perms.labels(r.permissions) if r.permissions else "")
                + (f" ({r.organisation.code} has no administrator yet: the CPUC decides)" if service.cpuc_decides(r.organisation) else ""),
            )
        )
    return out


def steward_decisions(user) -> list[Item]:
    if user is None or not user.is_authenticated or not service.is_steward(user):
        return []
    out = []
    for g in Grant.objects.filter(status=Grant.Status.PENDING).select_related("membership__user", "membership__organisation"):
        who = g.user.name or g.user.email
        out.append(Item(f"PII access for {who}", "/admin/stewardship/", g.organisation.code, g.granted_at, None, g.grant_reason))
    for c in PolicyChange.objects.filter(status=PolicyChange.Status.PROPOSED).exclude(proposed_by=user):
        out.append(Item(f"Access policy change: {c.setting}", "/admin/stewardship/", "", c.proposed_at, None, c.reason))
    return out


RECENT_DAYS = 14  # a decided access request stays in Your work this long, so the person sees the answer on the front page


def your_requests(request, page):
    """Your work on the front page (core/yourwork.py): the person's own access requests, waiting or decided in the last two weeks."""
    from datetime import timedelta

    from django.db.models import Q
    from django.urls import reverse
    from django.utils import timezone

    from cedars.core.yourwork import Line, Part, day

    since = timezone.now() - timedelta(days=RECENT_DAYS)
    mine = (
        AccessRequest.objects.filter(user=request.user)
        .filter(Q(status=AccessRequest.Status.PENDING) | Q(decided_at__gte=since))
        .select_related("organisation")
        .order_by("-created_at")
    )
    account = reverse("administration:account")
    lines = []
    for r in mine:
        org = r.organisation
        if r.status == AccessRequest.Status.PENDING:
            asked = f"asked {day(r.created_at).isoformat()}"
            if service.cpuc_decides(org):
                detail = f"{asked}; {org.code} has no administrator yet, so the CPUC decides it"
            else:
                detail = f"{asked}; waiting for {org.code}'s administrators to decide"
        else:
            detail = f"decided {day(r.decided_at).isoformat()}" + (f": {r.decision_note}" if r.decision_note else "")
        lines.append(Line(f"Access to {org.code} ({org.name})", account, "See it", r.get_status_display(), detail))
    if not lines:
        return None
    return Part("access", "Your access requests", lines, more_url=account, more_label="Your account")

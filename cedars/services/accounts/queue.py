"""The accounts service's work in each user's queue (cedars.core.queue, BR-09.5): access requests waiting on an organization's
administrators, PII grants and access-policy changes waiting on a data steward."""

from __future__ import annotations

from cedars.core.queue import Item

from . import service
from .models import AccessRequest, Grant, PolicyChange


def access_requests(user) -> list[Item]:
    if user is None or not user.is_authenticated:
        return []
    orgs = [o for o in service.organisations_of(user) if service.is_org_admin(user, o)]
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
                "Asks for: " + ", ".join(r.permissions) if r.permissions else "",
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

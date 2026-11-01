"""Budgets' work in each user's queue (cedars.core.queue, BR-09.5): fund shifts waiting for a co-funder's acknowledgment, for the people
who may acknowledge them (budgets.confirm in the co-funding PA)."""

from __future__ import annotations

from django.urls import reverse

from cedars.core.queue import Item
from cedars.services.accounts import service as access

from . import shifts


def acknowledgements(user) -> list[Item]:
    if user is None or not user.is_authenticated:
        return []
    out = []
    for org in access.organisations_of(user):
        if not access.has_permission(user, org, f"{shifts.MODULE}.confirm"):
            continue
        for s in shifts.waiting_for(org):
            out.append(
                Item(
                    f"Fund shift: {s.organisation.code} {s.title} ({s.cycle})",
                    reverse("budgets:shift_acknowledge", args=[s.pk]),
                    org.code,
                    s.submitted_at,
                    None,
                    f"Acting for {org.code}, place its portion of the statewide lines among its own programs and acknowledge the shift.",
                )
            )
    return out

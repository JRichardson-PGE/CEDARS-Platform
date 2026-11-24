"""Budgets' work in each user's queue (cedars.core.queue): fund shifts waiting for a co-funder to cover its portion of a statewide
change, for the people who may (budgets.confirm in the co-funding PA)."""

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
                    f"{shifts.cover_words(org.code, shifts.portion(s, org.code))}: acting for {org.code}, choose the programs and confirm.",
                )
            )
    return out

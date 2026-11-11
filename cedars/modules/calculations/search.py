"""Calculations' part of site search (cedars.core.search): the runs the signed-in person sees (their own and their organizations', the
Runs page's rule), matched on the run's name; a run named exactly as the query is the best match."""

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from cedars.core import search

from . import service


def runs(user, q: str):
    if user is None or not user.is_authenticated:
        return None
    terms = search.words(q)
    if not terms:
        return None
    narrowed = Q()
    for t in terms:
        narrowed &= Q(name__icontains=t)
    found = [r for r in service.visible_runs(user).filter(narrowed).order_by("-created_at") if search.matches(q, r.name)]
    found.sort(key=lambda r: not search.same(q, r.name))  # the run the query names first, then newest first
    hits = []
    for r in found[: search.PER_PART]:
        whose = r.organisation.code if r.organisation else "your own"
        when = timezone.localtime(r.created_at).strftime("%Y-%m-%d")
        text = f"{r.get_status_display()}, started {when}, {whose}, {r.acc_version} ACC"
        hits.append(search.Hit(r.name, reverse("calculations:run", args=[r.pk]), text, exact=search.same(q, r.name)))
    return search.Part(
        "runs", "Your runs", hits, len(found), reverse("calculations:runs") if len(found) > len(hits) else "", "See all your runs", "Run"
    )

"""The filings' part of site search (cedars.core.search): a signed-in person's own organizations' filings (claims, budget filings,
monthly reports) that they may read, matched on the filing's name, its organization, period and due date, each leading to its page."""

from django.urls import reverse
from django.utils import timezone

from cedars.core import search
from cedars.services.accounts import service as access

from . import service
from .models import Submission, SubmissionVersion
from .queue import _filing_page, describe


def filings(user, q: str):
    if user is None or not user.is_authenticated:
        return None
    orgs = list(access.organisations_of(user))
    if not orgs:
        return None
    subs = (
        Submission.objects.filter(organisation__in=orgs, deadline__retired_at__isnull=True)
        .select_related("organisation", "deadline__period", "deadline__cycle")
        .order_by("-deadline__due_at", "organisation__code")
    )
    readable: dict[tuple[int, str], bool] = {}
    found = []
    for sub in subs:
        due = timezone.localdate(sub.deadline.due_at)
        name = describe(sub)
        if not search.matches(q, f"{name} {sub.deadline.kind} due {due:%Y-%m-%d} filing"):
            continue
        key = (sub.organisation_id, sub.deadline.module)
        if key not in readable:
            readable[key] = service.can_read_filing(user, sub)
        if readable[key]:
            found.append((sub, name, due))
    latest = {}
    for v in SubmissionVersion.objects.filter(submission__in=[s for s, _, _ in found[: search.PER_PART]]).order_by("number"):
        latest[v.submission_id] = v
    hits = []
    for sub, name, due in found[: search.PER_PART]:
        v = latest.get(sub.pk)
        where = f"version {v.number}, {v.get_status_display().lower()}" if v else "not started"
        url = _filing_page(sub, v)
        if url:
            hits.append(search.Hit(name, url, f"Due {due:%Y-%m-%d}; {where}."))
    more = reverse("core:filings") if len(found) > len(hits) else ""
    return search.Part("filings", "Your filings", hits, len(found), more, "See every PA's filings", "Filing")

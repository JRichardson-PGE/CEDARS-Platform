"""Claims' part of site search (cedars.core.search): a confirmed claim found by its ClaimID, leading to how it links up (the Claim links
page, public as confirmed claims are). Only an exact ClaimID is looked up, so a search never reads the claim records it does not name."""

from urllib.parse import urlencode

from django.urls import reverse

from cedars.core import search

from . import service
from .models import ClaimRecord


def claims(user, q: str):
    claim_id = q.strip()
    if not claim_id or " " in claim_id or len(claim_id) < 3:
        return None
    # the forms a ClaimID is written in, so the indexed column is matched exactly (ClaimIDs carry the PA code and claim year)
    spellings = {claim_id, claim_id.upper(), claim_id.lower()}
    records = (
        ClaimRecord.objects.filter(version__in=service.standing_versions(), claim_id__in=spellings)
        .select_related("version__submission__organisation", "version__submission__deadline__period")
        .order_by("claim_id")
    )
    hits = []
    for r in records[: search.PER_PART]:
        sub = r.version.submission
        program = f", program {r.prg_id}" if r.prg_id else ""
        text = f"{sub.organisation.code} claim, claim year {sub.deadline.period.year}{program}" + (", withdrawn" if r.withdrawn else "")
        url = f"{reverse('claims:links')}?{urlencode({'kind': 'claim', 'q': r.claim_id})}"
        hits.append(search.Hit(f"Claim {r.claim_id}", url, text, exact=True))
    return search.Part("claims", "Confirmed claims", hits, len(hits), label="Confirmed claim")

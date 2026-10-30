"""The aging review (Jake, 2026-10-30): for CPUC ED, where each study manager is falling behind. Drafts are kept however old they are (no
purge); this review shows how long each has waited instead. Items, each with the day it became late and its age in days:
  - an unpublished version of a study's document (age from when it was filed);
  - a study's next activity past its due date (the study not completed, cancelled or combined);
  - a question nobody has answered once its comment period has closed (owed by the document's study manager, else its owner);
  - an active study nobody has updated in `stale_days` days;
  - a PA's response to the recommendations past its due date with nothing published (owed by the PA).
Read only, from the records the module already keeps."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.utils import timezone

from .models import Comment, DocumentVersion, ResponseRequest, Stage, Study

CLOSED_STATUSES = ("Completed", "Cancelled", "Combined")
BUCKETS = [(0, 30, "Up to 30 days"), (31, 60, "31 to 60 days"), (61, 90, "61 to 90 days"), (91, None, "Over 90 days")]
KINDS = {
    "draft": "Unpublished draft",
    "activity": "Next activity overdue",
    "question": "Question unanswered",
    "stale": "Study not updated",
    "response": "PA response overdue",
}
STALE_DAYS = 90


@dataclass
class Item:
    kind: str
    owner: object  # the Organisation that owes it
    study: Study | None
    what: str
    url: str
    since: date

    def age(self, day: date) -> int:
        return (day - self.since).days

    @property
    def kind_label(self) -> str:
        return KINDS[self.kind]


def _active(qs):
    return qs.exclude(status__label__in=CLOSED_STATUSES)


def items(day: date | None = None, stale_days: int = STALE_DAYS) -> list[Item]:
    day = day or timezone.localdate()
    out = []
    drafts = DocumentVersion.objects.filter(published_at__isnull=True, document__study__isnull=False).select_related(
        "document__study__manager"
    )
    for v in drafts:
        doc = v.document
        label = v.stage_label or f"version {v.number}"
        out.append(Item("draft", doc.study.manager, doc.study, f"{doc.title}: {label}", doc.get_absolute_url(), v.created_at.date()))
    for s in _active(Study.objects.filter(next_activity_due__lt=day)).select_related("manager"):
        out.append(Item("activity", s.manager, s, s.next_activity or "Next activity", s.get_absolute_url(), s.next_activity_due))
    questions = (
        Comment.objects.filter(
            kind=Comment.Kind.QUESTION, withdrawn_at__isnull=True, responses__isnull=True, version__comment_period__closes__lt=day
        )
        .select_related("version__comment_period", "version__document__study__manager", "version__document__owner")
        .distinct()
    )
    for c in questions:
        doc = c.version.document
        owner = doc.study.manager if doc.study else doc.owner
        out.append(
            Item(
                "question",
                owner,
                doc.study,
                f"{doc.title}: {c.subject}",
                f"{doc.get_absolute_url()}comments/#comment-{c.pk}",
                c.version.comment_period.closes,
            )
        )
    cutoff = timezone.now() - timedelta(days=stale_days)
    for s in _active(Study.objects.filter(updated_at__lt=cutoff)).select_related("manager"):
        out.append(Item("stale", s.manager, s, f"Last updated {s.updated_at:%Y-%m-%d}", s.get_absolute_url(), s.updated_at.date()))
    for r in ResponseRequest.objects.filter(due__lt=day).select_related("pa", "document__study"):
        published = r.document.versions.filter(stage=Stage.PA_RESPONSE, responding_pa=r.pa, published_at__isnull=False).exists()
        if not published:
            out.append(Item("response", r.pa, r.document.study, r.document.title, r.document.get_absolute_url(), r.due))
    return sorted(out, key=lambda i: (i.since, i.owner.code))


def bucket(age: int) -> str:
    for low, high, label in BUCKETS:
        if age >= low and (high is None or age <= high):
            return label
    return BUCKETS[0][2]


def by_owner(found: list[Item], day: date | None = None) -> list[dict]:
    """One row per organisation that owes something: counts by kind and by age band, and the oldest item's age; oldest first."""
    day = day or timezone.localdate()
    rows = defaultdict(lambda: {"kinds": defaultdict(int), "buckets": defaultdict(int), "oldest": 0, "total": 0})
    for i in found:
        r = rows[i.owner]
        r["kinds"][i.kind] += 1
        r["buckets"][bucket(i.age(day))] += 1
        r["oldest"] = max(r["oldest"], i.age(day))
        r["total"] += 1
    out = [
        {
            "owner": owner,
            "kinds": [r["kinds"].get(k, 0) for k in KINDS],
            "buckets": [r["buckets"].get(label, 0) for _, _, label in BUCKETS],
            "oldest": r["oldest"],
            "total": r["total"],
        }
        for owner, r in rows.items()
    ]
    return sorted(out, key=lambda r: (-r["oldest"], r["owner"].code))

"""Measures of the loop from a request to a release, read from the records as they stand: nobody fills anything in for them.

- Requests (the Feedback service): how many were sent in the period, how long the first reply and the answer took, how many
  wait on the CPUC now, and the pages most asked about.
- Work items: the backlog as it stands, how long accepted items took to be released (by size), and the problems reported
  since the last release on the pages that release changed.
- Testing (where it is on): each recent round's outcomes, and the tests of the loaded scripts no round has answered.

A closed request is deleted when its retention period ends, so a long period counts only the requests still kept. Counts
only: no request's words and no person are shown."""

from collections import Counter
from datetime import timedelta
from statistics import median

from django.utils import timezone

from cedars.services.audit.models import AuditEvent
from cedars.services.feedback.models import Feedback, Reply
from cedars.services.testing import service as testing
from cedars.services.testing.models import Campaign, Result, TestCase

from .models import ReleasePlan, WorkItem

PERIODS = [(30, "The last 30 days"), (90, "The last 90 days"), (365, "The last year")]
DEFAULT_PERIOD = 90
PAGES_SHOWN = 10
ROUNDS_SHOWN = 5


def span(seconds: float | None) -> str:
    """A length of time in the words a page shows: minutes under an hour, hours under two days, days after."""
    if seconds is None:
        return "No data"
    if seconds < 3600:
        n = max(round(seconds / 60), 1)
        return f"{n} minute{'s' if n != 1 else ''}"
    if seconds < 2 * 86400:
        n = round(seconds / 3600)
        return f"{n} hour{'s' if n != 1 else ''}"
    n = round(seconds / 86400)
    return f"{n} days"


def _median(values) -> float | None:
    values = list(values)
    return median(values) if values else None


def _plain_page(page: str) -> str:
    return page.split("?", 1)[0] or "/"


def requests(days: int) -> dict:
    """The requests sent in the last `days` days that the site still keeps (test findings apart)."""
    since = timezone.now() - timedelta(days=days)
    sent = list(Feedback.objects.filter(created_at__gte=since).values_list("pk", "kind", "page", "created_at"))
    findings = sum(1 for _, kind, _, _ in sent if kind == Feedback.Kind.FINDING)
    people = [row for row in sent if row[1] != Feedback.Kind.FINDING]
    first_reply: dict[int, object] = {}
    first_answer: dict[int, object] = {}
    replies = Reply.objects.filter(feedback_id__in=[pk for pk, *_ in people], kind__in=[Reply.Kind.ANSWER, Reply.Kind.QUESTION]).order_by(
        "created_at", "pk"
    )
    for fb, kind, at in replies.values_list("feedback_id", "kind", "created_at"):
        first_reply.setdefault(fb, at)
        if kind == Reply.Kind.ANSWER:
            first_answer.setdefault(fb, at)
    sent_at = {pk: at for pk, _, _, at in people}
    kinds = Counter(kind for _, kind, _, _ in people)
    pages = Counter(_plain_page(page) for _, _, page, _ in people)
    waiting = Feedback.objects.filter(status__in=Feedback.TO_ANSWER).exclude(kind=Feedback.Kind.FINDING)
    oldest = waiting.order_by("created_at").values_list("created_at", flat=True).first()
    return {
        "sent": len(people),
        "findings": findings,
        "kinds": [(label, kinds[key]) for key, label in Feedback.Kind.choices if kinds.get(key)],
        "replied": len(first_reply),
        "answered": len(first_answer),
        "first_reply": span(_median((first_reply[pk] - sent_at[pk]).total_seconds() for pk in first_reply)),
        "answer": span(_median((first_answer[pk] - sent_at[pk]).total_seconds() for pk in first_answer)),
        "waiting": waiting.count(),
        "oldest_waiting": span((timezone.now() - oldest).total_seconds()) if oldest else "",
        "pages": pages.most_common(PAGES_SHOWN),
    }


def _accepted_at(items) -> dict[int, object]:
    """When each item was last accepted into the backlog, from the audit trail."""
    events = AuditEvent.objects.filter(
        action="work.accepted", object_type="work.workitem", object_id__in=[str(it.pk) for it in items]
    ).order_by("at")
    return {int(pk): at for pk, at in events.values_list("object_id", "at")}


def delivery(days: int) -> dict:
    """The backlog as it stands, and how long the items released in the last `days` days took from accepted to released."""
    since = timezone.localdate() - timedelta(days=days)
    done = list(
        WorkItem.objects.filter(status=WorkItem.Status.DONE, release__released_on__gte=since).select_related("release").order_by("pk")
    )
    accepted = _accepted_at(done)
    took: dict[str, list[float]] = {}
    for it in done:
        at = accepted.get(it.pk)
        if at is not None:
            took.setdefault(it.size, []).append(max((it.release.released_on - timezone.localdate(at)).days, 0))
    by_size = []
    for key, label in [*WorkItem.Size.choices, ("", "No size")]:
        if took.get(key):
            by_size.append({"size": label, "items": len(took[key]), "median": round(_median(took[key])), "longest": max(took[key])})
    now = Counter(WorkItem.objects.values_list("status", flat=True))
    sizes = Counter(WorkItem.objects.filter(status=WorkItem.Status.ACCEPTED).values_list("size", flat=True))
    return {
        "released": len(done),
        "releases": ReleasePlan.objects.filter(status=ReleasePlan.Status.RELEASED, released_on__gte=since).count(),
        "by_size": by_size,
        "now": [(label, now.get(key, 0)) for key, label in WorkItem.Status.choices if key != WorkItem.Status.DONE],
        "backlog_sizes": [(label, sizes[key]) for key, label in [*WorkItem.Size.choices, ("", "No size")] if sizes.get(key)],
    }


def escaped() -> dict | None:
    """What came back after the last release: the problems and accessibility barriers reported since it went out on the pages
    its work items changed. None when no release has gone out."""
    plan = ReleasePlan.objects.filter(status=ReleasePlan.Status.RELEASED).order_by("-released_on", "-pk").first()
    if plan is None:
        return None
    changed = sorted({p for it in plan.items.all() for p in it.page_list})
    reported = Feedback.objects.filter(kind__in=[Feedback.Kind.PROBLEM, Feedback.Kind.BARRIER], created_at__date__gte=plan.released_on)
    counts = Counter()
    for page in reported.values_list("page", flat=True):
        plain = _plain_page(page)
        for prefix in changed:
            if plain == prefix or plain.startswith(prefix.rstrip("/") + "/"):
                counts[prefix] += 1
                break
    return {"plan": plan, "pages": [(p, counts.get(p, 0)) for p in changed], "total": sum(counts.values())}


def rounds() -> dict | None:
    """Each recent round of testing's outcomes, and the tests of the scripts as last loaded that no round has answered. None
    where testing is off."""
    if not testing.enabled():
        return None
    recent = []
    for campaign in Campaign.objects.select_related("version")[:ROUNDS_SHOWN]:
        whole = testing.summary(campaign)["whole"]
        recent.append({"round": campaign, "release": campaign.release_plans.first(), **whole})
    version = testing.newest_version()
    never = []
    if version is not None:
        answered = set(Result.objects.filter(test__script__version=version).values_list("test_id", flat=True))
        for script in version.scripts.all():
            tests = list(TestCase.objects.filter(script=script).values_list("pk", flat=True))
            missing = sum(1 for pk in tests if pk not in answered)
            if missing:
                never.append({"script": script, "never": missing, "tests": len(tests)})
    return {"rounds": recent, "version": version, "never": never, "never_total": sum(n["never"] for n in never)}


def page(days: int) -> dict:
    return {"requests": requests(days), "delivery": delivery(days), "escaped": escaped(), "testing": rounds()}

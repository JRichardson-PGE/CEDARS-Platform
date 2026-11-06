"""Where every program administrator's filings stand, for the Filings page (Jake, 2026-11-06): for budgets, claims and monthly reports,
the filing most recently due and the next one due, each PA's status on each.

What a reader sees follows the filing modules' own rules (`service.register_reader`): CPUC staff and the filing organization's readers
see where a filing stands (not started, draft, validated, confirmed, published, late, a correction under way) and get a link to it;
everyone else sees only whether it is published, since a published filing is public and an unpublished one is not."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from cedars.services.accounts.models import Organisation

from . import service
from .models import Deadline, Submission, SubmissionVersion

MODULES = (("budgets", "Budgets"), ("claims", "Claims"), ("monthly", "Monthly reports"))
LAST, NEXT = "Last due", "Next due"


@dataclass
class Column:
    module: str
    module_label: str
    when: str  # LAST or NEXT
    deadline: Deadline
    url: str  # the module's page for the deadline ("" when it has none)


@dataclass
class Cell:
    status: str  # the words shown, also the badge's tone (ui.status_tone)
    detail: str = ""  # past due, filed late, a correction under way
    link: str = ""  # the PA's filing, for a reader who may open it


@dataclass
class Row:
    organisation: Organisation
    cells: list[Cell] = field(default_factory=list)


@dataclass
class Board:
    columns: list[Column]
    rows: list[Row]
    without: list[str]  # the modules with no deadline on the calendar
    full: bool  # the reader sees every PA's full status (CPUC staff)

    def groups(self) -> list[tuple[str, int]]:
        """(module label, number of columns) in order, for the table's grouped header."""
        out: list[tuple[str, int]] = []
        for c in self.columns:
            if out and out[-1][0] == c.module_label:
                out[-1] = (c.module_label, out[-1][1] + 1)
            else:
                out.append((c.module_label, 1))
        return out


def columns(now=None) -> tuple[list[Column], list[str]]:
    """For each filing module, its deadline most recently due and its next one due, from the calendar's active deadlines."""
    now = now or timezone.now()
    cols, without = [], []
    for code, label in MODULES:
        due = Deadline.objects.active().filter(module=code).select_related("period", "cycle")
        last = due.filter(due_at__lte=now).order_by("-due_at", "-pk").first()
        nxt = due.filter(due_at__gt=now).order_by("due_at", "pk").first()
        if last is None and nxt is None:
            without.append(label)
        for when, d in ((LAST, last), (NEXT, nxt)):
            if d is not None:
                cols.append(Column(code, label, when, d, service.deadline_page(d)))
    return cols, without


def _cell(readable: bool, org, col: Column, sub: Submission | None, versions: list[SubmissionVersion], now) -> Cell:
    published = [v for v in versions if v.status == SubmissionVersion.Status.PUBLISHED]
    if not readable:
        return Cell("Published" if published else "Not published yet")
    link = f"{col.url}?org={org.code}" if col.url else ""
    past_due = col.deadline.due_at <= now
    if sub is None:
        return Cell("Not started", "past due" if past_due else "", link)
    if not versions:
        return Cell("Started", "past due" if past_due else "", link)
    latest = versions[-1]
    locked = [v for v in versions if v.locked]
    details = []
    if published and latest.number != published[-1].number:
        status = "Published"
        details.append(f"correction under way (version {latest.number}, {latest.get_status_display().lower()})")
    else:
        status = latest.get_status_display()
    if locked and locked[0].late:
        details.append("filed late")
    elif not locked and past_due:
        details.append("past due")
    return Cell(status, "; ".join(details), link)


def board(user, now=None) -> Board:
    """Every active program administrator's status on each column's filing, by code."""
    from cedars.services.accounts import service as access

    now = now or timezone.now()
    cols, without = columns(now)
    orgs = [o for o in Organisation.objects.filter(kind=Organisation.Kind.PA, active=True).order_by("code") if o.files]
    subs = {
        (s.deadline_id, s.organisation_id): s
        for s in Submission.objects.filter(deadline__in=[c.deadline for c in cols]).select_related("deadline", "organisation")
    }
    versions: dict[int, list[SubmissionVersion]] = {}
    for v in SubmissionVersion.objects.filter(submission__in=list(subs.values())).order_by("submission_id", "number"):
        versions.setdefault(v.submission_id, []).append(v)
    readers: dict[tuple[str, int], bool] = {}  # the modules' rules read by the filing organization, not the filing
    rows = []
    for o in orgs:
        cells = []
        for c in cols:
            sub = subs.get((c.deadline.pk, o.pk))
            key = (c.module, o.pk)
            if key not in readers:
                readers[key] = service.can_read_filing(user, sub or Submission(deadline=c.deadline, organisation=o))
            cells.append(_cell(readers[key], o, c, sub, versions.get(sub.pk, []) if sub else [], now))
        rows.append(Row(o, cells))
    return Board(cols, rows, without, access.is_cpuc_staff(user))

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
NOT_ON = "Nothing to file"  # a PA not on a filing (its set-up lists who files), or not one of the module's filers


@dataclass
class Column:
    module: str
    module_label: str
    when: str  # LAST or NEXT
    deadline: Deadline
    url: str  # the module's page for the deadline ("" when it has none)

    @property
    def words(self) -> str:
        return filing_words(self)


@dataclass
class Cell:
    status: str  # the words shown, also the badge's tone (ui.status_tone)
    detail: str = ""  # past due, filed late, a correction under way
    link: str = ""  # the PA's filing, for a reader who may open it
    readable: bool = True  # False: the reader sees only whether it is published

    @property
    def action(self) -> str:
        return action(self)


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
    own: Row | None = None  # the reader's own organization's row ("Your filings"), when it files

    @property
    def own_shut(self) -> bool:
        """The reader's organization files some of these filings, but the reader may open none of them (an organization administrator
        without a filing permission): no "Your filings" for them."""
        cells = [c for c in self.own.cells if c.status != NOT_ON] if self.own else []
        return bool(cells) and not any(c.readable for c in cells)

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
        due = Deadline.objects.active().filter(module=code).select_related("period", "cycle").prefetch_related("filers")
        due = [d for d in due if not service.nothing_to_file(d)]  # the 13th-month report: nothing to file
        last = max((d for d in due if d.due_at <= now), key=lambda d: (d.due_at, d.pk), default=None)
        nxt = min((d for d in due if d.due_at > now), key=lambda d: (d.due_at, d.pk), default=None)
        if last is None and nxt is None:
            without.append(label)
        for when, d in ((LAST, last), (NEXT, nxt)):
            if d is not None:
                cols.append(Column(code, label, when, d, service.deadline_page(d)))
    return cols, without


def _cell(readable: bool, org, col: Column, sub: Submission | None, versions: list[SubmissionVersion], now) -> Cell:
    published = [v for v in versions if v.status == SubmissionVersion.Status.PUBLISHED]
    if not readable:
        return Cell("Published" if published else "Not published yet", readable=False)
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


def board(user, now=None, org=None) -> Board:
    """Every active program administrator's status on each column's filing, by code. A filing's set-up may list who files
    (submissions.set_filers): a PA not on it has nothing to file there, and a PA on none of the filings shown is left out. `org`, the
    organization the reader acts for, puts its row first as `own` too (UI review 3, U5: "Your filings" above the board)."""
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
            if sub is None and not service.on_filing(c.deadline, o):
                cells.append(Cell(NOT_ON, "not on this filing"))
                continue
            if sub is None and not service.files_for(c.deadline, o):
                cells.append(Cell(NOT_ON, "not one of its filers"))
                continue
            cells.append(_cell(readers[key], o, c, sub, versions.get(sub.pk, []) if sub else [], now))
        # a PA its set-ups leave off every filing shown is not listed (Jake, 2026-11-09)
        if any(service.on_filing(c.deadline, o) or (c.deadline.pk, o.pk) in subs for c in cols):
            rows.append(Row(o, cells))
    own = next((r for r in rows if org is not None and r.organisation.pk == org.pk), None)
    return Board(cols, rows, without, access.is_cpuc_staff(user), own)


# --- the reader's own filings (UI review 3, U5): Your filings on the Filings page, Your next filings on the front page

TO_DO = {"Not started": "Start", "Started": "Continue", "Draft": "Continue", "Validated": "Continue"}


def action(cell: Cell) -> str:
    """The one thing to do on a filing, from its status: Start, Continue, or Open one that is confirmed or published."""
    if cell.status == NOT_ON or not cell.link:
        return ""
    return TO_DO.get(cell.status, "Open")


def needs_work(cell: Cell) -> bool:
    return cell.status in TO_DO or "correction under way" in cell.detail


def filing_words(col: Column) -> str:
    """A column's filing as the board's heading names it: "Claims: Q3 claim, 2026 Q3"."""
    d = col.deadline
    what = d.title
    if not d.name:
        from cedars.core.templatetags.ui import cycle_label

        what += f", {d.period}" if d.period else f", {cycle_label(d.cycle)}" if d.cycle else ""
    return f"{col.module_label}: {what}"


def own_filings(b: Board) -> list[tuple[Column, Cell]]:
    """The reader's organization's filings on the board, those it has nothing to file on left out."""
    if b.own is None:
        return []
    # a filing the reader may not open (a claims filer's monthly reports) is not theirs to work on
    return [(c, cell) for c, cell in zip(b.columns, b.own.cells, strict=True) if cell.status != NOT_ON and cell.readable]


def next_filings(b: Board) -> list[tuple[Column, Cell]]:
    """For each filing module, the one filing to look at now: the one most recently due while it still needs work (not started,
    a draft, a correction under way), else the next one due, else the last one."""
    out = []
    for module, _ in MODULES:
        mine = [(c, cell) for c, cell in own_filings(b) if c.module == module]
        last = next(((c, cell) for c, cell in mine if c.when == LAST), None)
        nxt = next(((c, cell) for c, cell in mine if c.when == NEXT), None)
        pick = last if last is not None and needs_work(last[1]) else nxt or last
        if pick is not None:
            out.append(pick)
    return out


def your_work(request, page):
    """Your work on the front page (core/yourwork.py): the next filing of each kind for the organization the person acts for, its
    status and the one thing to do. Nothing for an account that acts for no filing organization."""
    from django.urls import reverse

    from cedars.core.yourwork import Line, Part, day

    org = getattr(request, "filing_organisation", None)
    if org is None:
        return None
    b = board(request.user, org=org)
    if b.own_shut:
        return None
    lines = []
    for col, cell in next_filings(b):
        due = day(col.deadline.due_at)
        lines.append(
            Line(
                what=filing_words(col),
                url=cell.link,
                action=action(cell),
                status=cell.status,
                detail="; ".join(x for x in (f"due {due.isoformat()}", cell.detail) if x),
                overdue="past due" in cell.detail,
            )
        )
    return Part(
        "filings",
        f"{org.code}'s next filings",
        lines,
        more_url=reverse("core:filings") + "#your-filings",
        more_label=f"Where {org.code}'s filings stand",
        empty=f"{org.code} has nothing to file on the calendar yet.",
    )


# --- the filing modules' lists (UI review 3, U5): the next one to file is marked and every row has its action


def mark_next(rows: list[dict], org) -> None:
    """For a filing module's list of deadlines (the Claims or Monthly reports page; each row a dict with `deadline`, `latest`, `can_open`
    and optionally `files` and `off_filing`), set `action` on every row (Start, Continue or Open) and `next` on the one to file now: the
    earliest by due date that the organization files and has not confirmed. Without an organization (the CPUC) nothing is set."""
    if org is None:
        return
    found = False
    now = timezone.now()
    for row in sorted(rows, key=lambda r: (r["deadline"].due_at, r["deadline"].pk)):
        d, latest = row["deadline"], row.get("latest")
        files = row.get("files", True) and not row.get("off_filing") and not (d.retired_at and latest is None)
        open_ = files and (latest is None or not latest.locked)
        row["next"] = open_ and not found
        found = found or open_
        if not row.get("can_open", True) or not files:
            row["action"] = ""
        elif latest is None:
            row["action"] = "Start" if row["next"] or d.due_at <= now else "Open"
        else:
            row["action"] = "Open" if latest.locked else "Continue"

import calendar as pycalendar
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from cedars.core import nav, storage
from cedars.core import uploads as core_uploads
from cedars.services.accessibility import service as accessibility
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import comments, corrections, service
from .models import Attachment, Comment, Deadline, ReopenRequest, SubmissionVersion, WorkflowPolicy

MODULES = [("budgets", "Budgets"), ("claims", "Claims"), ("monthly", "Monthly reports")]


class DeadlineChangeForm(forms.Form):
    """A deadline's due date, opening date and notes, with the reason for the change; for a filing on a cycle (a budget filing), also
    its name and program years (service.renamable). What is due, and a claim's or monthly report's period, never change."""

    name = forms.CharField(
        label="Filing name",
        max_length=120,
        required=False,
        help_text="What the calendar and the filing's pages call it. Blank: its program years and what is due.",
    )
    first_year = forms.IntegerField(label="First program year", min_value=2000, max_value=2100, required=False)
    last_year = forms.IntegerField(
        label="Last program year",
        min_value=2000,
        max_value=2100,
        required=False,
        help_text="Any number of years: the same as the first year for a one-year filing. Changes only before any filing starts.",
    )
    due = forms.DateField(label="Due date", widget=forms.DateInput(attrs={"type": "date"}))
    opens = forms.DateField(
        label="Opens (optional)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The first day filings can start. Blank: open now.",
    )
    notes = forms.CharField(
        label="Notes (optional)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Shown with the deadline and in reminders.",
    )
    reason = forms.CharField(
        label="Reason for the change",
        max_length=500,
        help_text="Kept in the deadline's history and sent to those who follow the filing calendar.",
    )

    def __init__(self, *args, renamable=False, years_fixed=False, **kw):
        super().__init__(*args, **kw)
        if not renamable:
            for f in ("name", "first_year", "last_year"):
                del self.fields[f]
        elif years_fixed:
            for f in ("first_year", "last_year"):
                self.fields[f].disabled = True
                self.fields[f].help_text = "Filings have started, so the program years no longer change."
        else:
            self.fields["first_year"].required = self.fields["last_year"].required = True

    def clean(self):
        d = super().clean()
        if d.get("opens") and d.get("due") and d["opens"] > d["due"]:
            self.add_error("opens", "A filing opens on or before its due date.")
        first, last = d.get("first_year"), d.get("last_year")
        if first and last and last < first:
            self.add_error("last_year", "The last program year is the same as the first or after it.")
        return d


class RetireForm(forms.Form):
    """The reason for canceling a filing or restoring it."""

    reason = forms.CharField(label="Reason", max_length=500, help_text="Kept in the filing's history.")


def _due_at(day: date, keep=None):
    """The deadline's due moment on `day`: the time of day it already had (`keep`), else the end of the day."""
    if keep is not None and timezone.localtime(keep).date() == day:
        return keep
    at = timezone.localtime(keep).time() if keep is not None else time(23, 59, 59)
    return timezone.make_aware(datetime.combine(day, at))


def _opens_at(day: date | None, keep=None):
    """When filings open on `day` (its start), or the moment it already had when the day is unchanged; None for blank."""
    if day is None:
        return None
    if keep is not None and timezone.localtime(keep).date() == day:
        return keep
    return timezone.make_aware(datetime.combine(day, time.min))


RANGES = [  # the due-date choices on the filing calendar, in the order shown; the first is the default
    ("window", "Last 3 and next 9 months"),
    ("year", "This year"),
    ("all", "All"),
]


def _add_months(day: date, months: int) -> date:
    """The same day of the month `months` later (or earlier), on the month's last day when it has no such day."""
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def calendar_filters(params, today: date) -> dict:
    """The filing calendar's filters from its query string, so any view of it is a link that can be shared: `type` (repeated, one per
    module; none = every type), `range` (window, year, all or custom) and, for a custom range, `from` and `to` (YYYY-MM-DD, either may be
    left open). Unknown values fall back to the defaults; a custom range with a date that does not parse is an error and shows the
    default range."""
    codes = [code for code, _ in MODULES]
    types = [code for code in codes if code in params.getlist("type")]
    rng = params.get("range") or "window"
    start = end = None
    error = ""
    raw_from, raw_to = (params.get("from") or "").strip(), (params.get("to") or "").strip()
    if rng == "custom":
        start, end = _parse_day(raw_from) if raw_from else None, _parse_day(raw_to) if raw_to else None
        if (raw_from and start is None) or (raw_to and end is None):
            error = "Enter dates as YYYY-MM-DD. Showing the last 3 and next 9 months instead."
            rng, start, end = "window", None, None
        elif start and end and start > end:
            start, end = end, start
        elif not raw_from and not raw_to:
            rng = "all"
    elif rng not in {code for code, _ in RANGES}:
        rng = "window"
    if rng == "window":
        start, end = _add_months(today, -3), _add_months(today, 9)
    elif rng == "year":
        start, end = date(today.year, 1, 1), date(today.year, 12, 31)
    return {
        "types": types,
        "range": rng,
        "start": start,
        "end": end,
        "error": error,
        "from": start.isoformat() if rng == "custom" and start else raw_from if rng == "custom" else "",
        "to": end.isoformat() if rng == "custom" and end else raw_to if rng == "custom" else "",
    }


VIEWS = ("year", "month", "list")
DEFAULT_VIEW = "year"  # Jake 2026-11-09: "show filing calendars in a mini-view ... showing all of the months at the same time"


def _calendar_link(
    types, rng="window", start=None, end=None, retired=False, *, view="list", month: date | None = None, year: int | None = None
) -> str:
    """The calendar's URL for these filters, leaving out the defaults (every type, retired deadlines left out). The default view
    (DEFAULT_VIEW) carries no view; the year view shows this year unless `year` names another, the month view this month unless
    `month` names another; the list view carries its due-date range (default: the last 3 and next 9 months)."""
    query = [("type", code) for code, _ in MODULES if code in types]
    if view != DEFAULT_VIEW:
        query.append(("view", view))
    if view == "list":
        if rng != "window":
            query.append(("range", rng))
        if rng == "custom":
            query += [(k, v.isoformat()) for k, v in (("from", start), ("to", end)) if v]
    elif view == "month" and month is not None:
        query.append(("month", f"{month:%Y-%m}"))
    elif view == "year" and year is not None:
        query.append(("year", str(year)))
    if retired:
        query.append(("retired", "1"))
    return reverse("submissions:calendar") + (f"?{urlencode(query)}" if query else "")


def _parse_month(value) -> date | None:
    """The first day of a month written YYYY-MM, or None."""
    try:
        y, m = (int(x) for x in (value or "").strip().split("-"))
        return date(y, m, 1) if 1900 <= y <= 2200 else None
    except ValueError:
        return None


def _parse_year(value) -> int | None:
    """A year written YYYY, or None."""
    try:
        year = int((value or "").strip())
    except ValueError:
        return None
    return year if 1900 <= year <= 2200 else None


WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]  # weeks start on Sunday, as US calendars do


def month_grid(month: date, rows: list[dict], today: date) -> list[list[dict]]:
    """The weeks of a month (Sunday first) as calendar cells: each day, whether it is in the month or today, and the rows (deadlines,
    with their filing page) due on it. Days of the months before and after fill the first and last weeks, without their deadlines."""
    due: dict[date, list] = defaultdict(list)
    for row in rows:
        due[timezone.localtime(row["d"].due_at).date()].append(row)
    weeks = pycalendar.Calendar(firstweekday=6).monthdatescalendar(month.year, month.month)
    return [
        [
            {
                "day": day,
                "weekday": WEEKDAYS[(day.weekday() + 1) % 7],
                "in_month": day.month == month.month,
                "today": day == today,
                "rows": due.get(day, []) if day.month == month.month else [],
            }
            for day in week
        ]
        for week in weeks
    ]


def year_grid(year: int, rows: list[dict], today: date) -> list[dict]:
    """The twelve months of a year as small calendars (Sunday first): each month's weeks of day cells, a day of another month left
    blank (None). A day with deadlines carries its rows and its filing types (`types`, in MODULES order, each once), which the year view
    colors and marks; the rows give each deadline's specifics in the day's tooltip."""
    due: dict[date, list] = defaultdict(list)
    for row in rows:
        due[timezone.localtime(row["d"].due_at).date()].append(row)
    order = [code for code, _ in MODULES]
    months = []
    for number in range(1, 13):
        weeks = pycalendar.Calendar(firstweekday=6).monthdatescalendar(year, number)
        cells = []
        for week in weeks:
            row = []
            for day in week:
                if day.month != number:
                    row.append(None)
                    continue
                on_day = due.get(day, [])
                present = {r["d"].module for r in on_day}
                row.append(
                    {
                        "day": day,
                        "weekday": WEEKDAYS[(day.weekday() + 1) % 7],
                        "today": day == today,
                        "rows": on_day,
                        "types": [c for c in order if c in present] + sorted(present - set(order)),
                    }
                )
            cells.append(row)
        first = date(year, number, 1)
        months.append(
            {
                "first": first,
                "label": f"{first:%B}",
                "weeks": cells,
                "count": sum(len(due.get(d, [])) for w in weeks for d in w if d.month == number),
            }
        )
    return months


def calendar(request):
    """The filing calendar: what each module expects and when. Public. Three views of the same deadlines: a year
    of twelve small months (the default, Jake 2026-11-09; `year=YYYY`, this year by default), each due date colored and marked by
    filing type with a legend and the deadlines' specifics in its tooltip; a month grid (`view=month`; Jake 2026-11-06: "more of a
    true calendar view"), one month at a time (`month=YYYY-MM`, this month by default); and a list (`view=list`, or any due-date range
    in the address) filtered by due date (last 3 and next 9 months by default). All filter by filing type; the filters are kept in
    the URL; retired deadlines are shown only to administrators who ask (`retired=1`). The calendar fills from the modules' set-ups
    (a budget filing, a claim year, a year of monthly reports: Add a filing leads to them); a deadline's own page changes it."""
    sysadmin = access.is_system_admin(request.user)
    today = timezone.localdate()
    view = request.GET.get("view")
    if view not in VIEWS:
        if any(k in request.GET for k in ("range", "from", "to")):
            view = "list"
        elif "month" in request.GET:
            view = "month"
        else:
            view = DEFAULT_VIEW
    f = calendar_filters(request.GET, today)
    types, rng, start, end = f["types"], f["range"], f["start"], f["end"]
    month = _parse_month(request.GET.get("month")) or today.replace(day=1)
    year = _parse_year(request.GET.get("year")) or today.year
    if view == "month":
        start, end = month, month.replace(day=monthrange(month.year, month.month)[1])
    elif view == "year":
        start, end = date(year, 1, 1), date(year, 12, 31)
    retired = sysadmin and request.GET.get("retired") == "1"
    query = Deadline.objects.select_related("period", "cycle")
    if not retired:
        query = query.active()
    if types:
        query = query.filter(module__in=types)
    tz = timezone.get_current_timezone()
    if start:
        query = query.filter(due_at__gte=datetime.combine(start, time.min, tzinfo=tz))
    if end:
        query = query.filter(due_at__lt=datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz))
    labels = dict(MODULES)
    deadlines = [
        {"d": d, "url": service.deadline_page(d), "module": labels.get(d.module, d.module.capitalize())}
        for d in query.order_by("due_at", "module", "period__starts", "kind")
    ]
    this_month = today.replace(day=1)

    def link(**kw):
        args = {"types": types, "rng": rng, "start": start, "end": end, "retired": retired, "view": view}
        if view == "month":
            args |= {"rng": "window", "start": None, "end": None, "month": None if month == this_month else month}
        elif view == "year":
            args |= {"rng": "window", "start": None, "end": None, "year": None if year == today.year else year}
        args |= kw
        if args.get("view") == "month" and args.get("month") == this_month:
            args["month"] = None
        if args.get("view") == "year" and args.get("year") == today.year:
            args["year"] = None
        return _calendar_link(**args)

    def toggled(code):
        return [t for t in types if t != code] if code in types else types + [code]

    type_chips = [{"label": "All types", "url": link(types=[]), "on": not types}] + [
        {"label": label, "url": link(types=toggled(code)), "on": code in types} for code, label in MODULES
    ]
    range_chips = [{"label": label, "url": _calendar_link(types, code, retired=retired), "on": rng == code} for code, label in RANGES]
    what = " and ".join(labels[t] for t in types) if types else "every filing type"
    n = f"{len(deadlines)} deadline{'' if len(deadlines) == 1 else 's'}"
    if view == "month":
        showing = f"{n} due in {month:%B %Y} for {what}"
    elif view == "year":
        showing = f"{n} due in {year} for {what}"
    else:
        if start and end:
            when = f"due {start.isoformat()} to {end.isoformat()}"
        elif start:
            when = f"due on or after {start.isoformat()}"
        elif end:
            when = f"due on or before {end.isoformat()}"
        else:
            when = "of every date"
        showing = f"{n} for {what}, {when}"
    prev_month, next_month = _add_months(month, -1), _add_months(month, 1)
    policies = {p.module: p for p in WorkflowPolicy.objects.all()}
    return render(
        request,
        "submissions/calendar.html",
        {
            "view": view,
            "deadlines": deadlines,
            "filters": f,
            "type_chips": type_chips,
            "range_chips": range_chips,
            "showing": showing + (", canceled ones included" if retired else ""),
            "filtered": bool(types) or (view == "list" and rng != "all"),
            "all_url": link(types=[]) if view in ("month", "year") else _calendar_link([], "all", retired=retired),
            "view_urls": {
                "year": _calendar_link(
                    types, retired=retired, view="year", year=None if view != "month" or month.year == today.year else month.year
                ),
                "month": _calendar_link(types, retired=retired, view="month"),
                "list": _calendar_link(types, retired=retired, view="list"),
            },
            "month": month,
            "weeks": month_grid(month, deadlines, today) if view == "month" else [],
            "weekdays": WEEKDAYS,
            "month_urls": {
                "prev": link(month=prev_month),
                "next": link(month=next_month),
                "this": link(month=this_month),
            },
            "month_label": f"{month:%B %Y}",
            "month_value": f"{month:%Y-%m}",
            "prev_label": f"{prev_month:%B %Y}",
            "next_label": f"{next_month:%B %Y}",
            "is_this_month": month == this_month,
            "year": year,
            "months": [m | {"url": link(view="month", month=m["first"], year=None)} for m in year_grid(year, deadlines, today)]
            if view == "year"
            else [],
            "year_urls": {"prev": link(year=year - 1), "next": link(year=year + 1), "this": link(year=today.year)},
            "is_this_year": year == today.year,
            "legend": [(code, label) for code, label in MODULES if not types or code in types],
            "sysadmin": sysadmin,
            "retired": retired,
            "retired_count": Deadline.objects.filter(retired_at__isnull=False).count() if sysadmin else 0,
            "retired_url": link(retired=not retired),
            "policies": [(code, label, policies.get(code)) for code, label in MODULES],
        },
    )


class WorkflowPolicyForm(forms.Form):
    """One filing module's workflow settings, with the reason for changing them."""

    reopen_after_due = forms.ChoiceField(
        label="Correcting a confirmed filing after its due date",
        choices=WorkflowPolicy.Reopen.choices,
        widget=forms.RadioSelect,
        help_text="A correction is always a new version; the version filed stays readable.",
    )
    late_confirmation = forms.BooleanField(
        label="A first version may still be confirmed after the due date (it is marked late)",
        required=False,
    )
    reason = forms.CharField(
        label="Reason for the change",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Kept in the audit trail and shown in the history below.",
    )


def _policy_words(module: str, before: dict, after: dict) -> str:
    """A change in words, for the message after saving and a release note's summary."""
    parts = []
    if "reopen_after_due" in after:
        parts.append(f"correcting a confirmed filing after its due date: {WorkflowPolicy.Reopen(after['reopen_after_due']).label.lower()}")
    if "late_confirmation" in after:
        parts.append("a late first confirmation is " + ("allowed (marked late)" if after["late_confirmation"] else "no longer allowed"))
    return f"{dict(MODULES)[module]}: " + "; ".join(parts)


def _settings_words(values: dict | None) -> str:
    out = []
    for k, v in (values or {}).items():
        if k == "reopen_after_due" and v in WorkflowPolicy.Reopen.values:
            out.append(f"corrections: {WorkflowPolicy.Reopen(v).label.lower()}")
        elif k == "late_confirmation":
            out.append("late first confirmation " + ("allowed" if v else "not allowed"))
    return "; ".join(out)


@login_required
def workflow_settings(request):
    """Each filing module's correction policy and late-confirmation rule (CPUC system administrators), with the history of changes."""
    if not access.is_system_admin(request.user):
        raise Http404
    modules = []
    for code, label in MODULES:
        p = service.policy(code)
        initial = {"reopen_after_due": p.reopen_after_due, "late_confirmation": p.late_confirmation}
        bound = request.method == "POST" and request.POST.get("module") == code
        form = WorkflowPolicyForm(request.POST if bound else None, initial=initial, prefix=code)
        if bound and form.is_valid():
            d = form.cleaned_data
            after = {k: d[k] for k in service.POLICY_SETTINGS if d[k] != initial[k]}
            if not after:
                messages.info(request, f"{label}: nothing changed.")
                return redirect("submissions:workflow_settings")
            before = {k: initial[k] for k in after}
            try:
                service.set_policy(request.user, code, reason=d["reason"], **after)
            except AccessDenied as e:
                messages.error(request, str(e))
                return redirect("submissions:workflow_settings")
            words = _policy_words(code, before, after)
            messages.success(request, f"Saved. {words}.")
            if service.policy_tightens(before, after):
                note = urlencode({"title": f"{label}: workflow change", "summary": f"{words}. {d['reason'].strip()}"})
                request.session["workflow_note"] = note
            return redirect("submissions:workflow_settings")
        history = [
            {"event": e, "before": _settings_words(e.before), "after": _settings_words(e.after)} for e in service.policy_history(code)
        ]
        modules.append({"code": code, "label": label, "policy": p, "form": form, "history": history})
    note = request.session.pop("workflow_note", "")
    return render(
        request,
        "submissions/workflow_settings.html",
        {"modules": modules, "note_url": f"{reverse('releases:manage')}?{note}" if note else ""},
    )


class DelegationForm(forms.Form):
    """Name a delegate: who decides for the approver in a module, and for which days."""

    module = forms.ChoiceField(label="Approvals for")
    approver = forms.EmailField(
        label="Approver's email",
        required=False,
        help_text="For a CPUC system administrator naming a delegate on an approver's behalf. Blank: yourself.",
    )
    delegate = forms.EmailField(label="Delegate's email", help_text="A member of the CPUC who can read this module.")
    starts = forms.DateField(label="First day", widget=forms.DateInput(attrs={"type": "date"}))
    ends = forms.DateField(
        label="Last day", widget=forms.DateInput(attrs={"type": "date"}), help_text="The delegate decides through this day."
    )
    reason = forms.CharField(
        label="Reason (optional)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="e.g. leave from 2 to 13 March. Needed when you name a delegate on someone else's behalf.",
    )

    def __init__(self, *args, modules, on_behalf, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["module"].choices = [(m, service.DELEGABLE[m]) for m in modules]
        if not on_behalf:
            del self.fields["approver"]


def _delegation_rows(qs, user, today):
    return [
        {"d": d, "state": d.state(today), "can_end": service.can_end_delegation(user, d) and d.state(today) in ("in force", "upcoming")}
        for d in qs
    ]


@login_required
def delegations(request):
    """Approvers name delegates to decide for them for a period (e.g. leave), and end them early; CPUC system administrators can do
    it on an approver's behalf. A delegate sees whom they decide for."""
    from cedars.services.accounts.models import User

    from .models import Delegation

    user = request.user
    sysadmin = access.is_system_admin(user)
    mine = service.delegable_modules(user)
    today = timezone.localdate()
    current = Delegation.objects.filter(ended_at__isnull=True, ends__gte=today).select_related("approver", "delegate", "created_by")
    for_me = current.filter(delegate=user)
    if not (sysadmin or mine or for_me.exists()):
        raise Http404
    modules = list(service.DELEGABLE) if sysadmin else mine
    form = DelegationForm(request.POST or None, modules=modules, on_behalf=sysadmin, initial={"starts": today}) if modules else None
    if request.method == "POST" and form is not None and form.is_valid():
        d = form.cleaned_data
        people = {}
        for key in ("approver", "delegate"):
            email = d.get(key) or ""
            if email:
                people[key] = User.objects.filter(email__iexact=email).first()
                if people[key] is None:
                    messages.error(request, f"There is no account for {email}.")
                    return redirect("submissions:delegations")
        try:
            made = service.delegate(
                user,
                module=d["module"],
                delegate_to=people["delegate"],
                starts=d["starts"],
                ends=d["ends"],
                approver=people.get("approver") or user,
                reason=d["reason"],
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(
                request,
                f"{made.delegate.email} decides {service.DELEGABLE[made.module]} approvals for {made.approver.email} from "
                f"{made.starts:%Y-%m-%d} to {made.ends:%Y-%m-%d}.",
            )
            return redirect("submissions:delegations")
    own = Delegation.objects.filter(approver=user).select_related("delegate", "created_by", "ended_by")[:50]
    return render(
        request,
        "submissions/delegations.html",
        {
            "form": form,
            "sysadmin": sysadmin,
            "own": _delegation_rows(own, user, today),
            "for_me": _delegation_rows(for_me, user, today),
            "every": _delegation_rows(current.order_by("starts"), user, today) if sysadmin else [],
            "modules": [service.DELEGABLE[m] for m in mine],
        },
    )


@login_required
@require_POST
def end_delegation(request, pk):
    from .models import Delegation

    d = get_object_or_404(Delegation.objects.select_related("approver", "delegate"), pk=pk)
    if not service.can_end_delegation(request.user, d):
        raise Http404
    try:
        service.end_delegation(request.user, d, request.POST.get("reason", ""))
    except AccessDenied as e:
        messages.error(request, str(e))
    else:
        messages.success(
            request,
            f"Ended: {d.delegate.email} no longer decides {service.DELEGABLE.get(d.module, d.module)} approvals for {d.approver.email}.",
        )
    return redirect("submissions:delegations")


@login_required
def deadline_new(request):
    """Add a filing (CPUC system administrators): what can be set up, each on its module's set-up page, which puts its deadlines on
    the calendar (service.register_setup_page). The calendar itself only changes what is on it."""
    if not access.is_system_admin(request.user):
        raise Http404
    nav.belongs_to(request, "submissions:calendar")
    pages = [p | {"url": reverse(p["url_name"])} for p in service.setup_pages()]
    return render(request, "submissions/deadline_new.html", {"pages": pages})


@login_required
def filing_sets(request):
    """Every filing set on the calendar (each budget filing, claim year and year of monthly reports set up), for CPUC system
    administrators: its filings, how many are canceled, what is due next, and a link to edit it on its set-up page (Jake, 2026-11-09:
    edit the filing sets directly)."""
    if not access.is_system_admin(request.user):
        raise Http404
    sets = service.filing_sets()
    kinds = []
    for s in sets:
        if s["kind"] not in kinds:
            kinds.append(s["kind"])
    groups = [(k, [s for s in sets if s["kind"] == k]) for k in kinds]
    return render(request, "submissions/filing_sets.html", {"groups": groups, "count": len(sets)})


FIELD_LABELS = {
    "name": "Name",
    "years": "Program years",
    "due_at": "Due date",
    "opens_at": "Opens",
    "notes": "Notes",
    "filers": "Who files",
}
EVENT_LABELS = {
    "deadline.created": "Added to the calendar",
    "deadline.moved": "Due date moved",
    "deadline.changed": "Changed",
    "deadline.retired": "Canceled (taken off the calendar)",
    "deadline.restored": "Restored to the calendar",
    "deadline.filers_changed": "Who files changed",
}


def _shown(field: str, value) -> str:
    if value in (None, ""):
        return "none" if field != "notes" else "(blank)"
    if isinstance(value, list):
        return ", ".join(value)
    if field in ("due_at", "opens_at"):
        try:
            return f"{timezone.localtime(datetime.fromisoformat(value)):%Y-%m-%d}"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def deadline_history(d: Deadline) -> list[dict]:
    """Every audit event about the deadline, oldest first, in words: what changed from what to what, why, when and who."""
    from cedars.services import audit

    out = []
    for e in audit.changes(d):
        before, after = e.before or {}, e.after or {}
        if e.action == "deadline.created":
            changes = [f"named {after['name']}"] if after.get("name") else []
            if after.get("years"):
                changes.append(f"program years {after['years']}")
            changes.append(f"due {_shown('due_at', after.get('due_at'))}")
            if after.get("opens_at"):
                changes.append(f"opens {_shown('opens_at', after['opens_at'])}")
        else:
            changes = [
                f"{FIELD_LABELS[k]}: {_shown(k, before.get(k))} to {_shown(k, after.get(k))}"
                for k in FIELD_LABELS
                if k in before or k in after
            ]
        out.append({"e": e, "what": EVENT_LABELS.get(e.action, e.action), "changes": changes})
    return out


def deadline(request, pk):
    """One deadline: when it opens and is due, its notes and its history (public, as the calendar is). A CPUC system administrator
    changes its dates and notes here with a reason, and retires or restores it (submissions.service.change_deadline and friends)."""
    d = get_object_or_404(Deadline.objects.select_related("period", "cycle"), pk=pk)
    sysadmin = access.is_system_admin(request.user)
    local = {k: timezone.localtime(getattr(d, k)).date() if getattr(d, k) else None for k in ("due_at", "opens_at")}
    initial = {"due": local["due_at"], "opens": local["opens_at"], "notes": d.notes, "name": d.name}
    if d.cycle_id:
        initial |= {"first_year": d.cycle.first_year, "last_year": d.cycle.last_year}
    started = d.submissions.count()
    renamable = service.renamable(d)
    action = request.POST.get("action", "") if request.method == "POST" else ""
    change_form = DeadlineChangeForm(
        request.POST if action == "change" else None,
        initial=initial,
        auto_id="change_%s",
        renamable=renamable,
        years_fixed=bool(started),
    )
    if renamable:
        change_form.fields["name"].help_text = f"What the calendar and the filing's pages call it. Blank: {d.kind}."
    retire_form = RetireForm(request.POST if action in ("retire", "restore") else None, auto_id="retire_%s")
    if action and not sysadmin:
        raise Http404
    if action == "change" and change_form.is_valid():
        c = change_form.cleaned_data
        try:
            service.change_deadline(
                request.user,
                d,
                due_at=_due_at(c["due"], d.due_at),
                opens_at=_opens_at(c["opens"], d.opens_at),
                notes=c["notes"],
                reason=c["reason"],
                name=c["name"] if renamable else None,
                years=(c["first_year"], c["last_year"]) if renamable and not started else None,
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Deadline changed. Those who follow the filing calendar are told.")
            return redirect("submissions:deadline", pk=d.pk)
    elif action in ("retire", "restore") and retire_form.is_valid():
        do = service.retire_deadline if action == "retire" else service.restore_deadline
        try:
            do(request.user, d, retire_form.cleaned_data["reason"])
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Filing canceled." if action == "retire" else "Filing restored to the calendar.")
            return redirect("submissions:deadline", pk=d.pk)
    page = service.deadline_page(d)
    return render(
        request,
        "submissions/deadline.html",
        {
            "d": d,
            "page": page,
            "sysadmin": sysadmin,
            "change_form": change_form,
            "retire_form": retire_form,
            "started": started,
            "renamable": renamable,
            "can_retire": not started,
            "setup_url": service.setup_link(d),
            "history": deadline_history(d),
        },
    )


@login_required
def reopen_requests(request):
    """Requests to correct filings after their due date, for approvers and their delegates. Each filing is named once, plainly
    (Submission.plain_name), with what the approver needs to decide: a link to the filing, its confirmed version (its records and
    totals), the earlier corrections and the comments."""
    related = ("submission__deadline__period", "submission__deadline__cycle", "submission__organisation")
    pending = []
    for r in ReopenRequest.objects.filter(status=ReopenRequest.Status.PENDING).select_related(*related, "requested_by"):
        approver = service.approver_for(request.user, r.submission.module)
        if approver is None:
            continue
        stands = service.current(r.submission)
        earlier = list(r.submission.versions.filter(number__gt=1).exclude(origin=SubmissionVersion.Origin.IMPORTED).order_by("number"))
        pending.append(
            {
                "r": r,
                "approver": approver,
                "page": service.filing_page(r.submission, stands),
                "stands": stands,
                "stands_page": comments.version_page(stands) if stands else "",
                "earlier": earlier,
                "comments": Comment.objects.filter(version__submission=r.submission).count(),
            }
        )
    decided = [
        (r, service.filing_page(r.submission, service.current(r.submission)))
        for r in ReopenRequest.objects.exclude(status=ReopenRequest.Status.PENDING).select_related(*related, "decided_by")[:50]
    ]
    return render(request, "submissions/reopen_requests.html", {"pending": pending, "decided": decided})


@login_required
@require_POST
def decide(request, pk):
    req = get_object_or_404(ReopenRequest, pk=pk)
    approve = request.POST.get("decision") == "approve"
    try:
        service.decide_reopen(request.user, req, approve, request.POST.get("note", ""))
    except AccessDenied as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{'Approved' if approve else 'Declined'}: {req.submission.plain_name}.")
    return redirect("submissions:reopen_requests")


@login_required
@require_POST
def comment(request, pk):
    """Post a comment, with any files, on a filing version, then return to the version's page."""
    v = get_object_or_404(SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation"), pk=pk)
    if not comments.can_discuss(request.user, v.submission):
        raise Http404
    uploads = [(f.name, f) for f in request.FILES.getlist("files")]
    try:
        c = comments.post(request.user, v, request.POST.get("text", ""), uploads)
    except (AccessDenied, comments.CommentRefused) as e:
        messages.error(request, f"Your comment was not posted: {e}")
        anchor = "#comments"
    else:
        messages.success(request, "Comment posted.")
        text = accessibility.upload_warning("filing_comment", [a.pk for a in c.attachments.all()])
        if text:
            messages.warning(request, text)
        anchor = f"#comment-{c.pk}"
    page = comments.version_page(v)
    return redirect(page + anchor) if page else redirect("core:queue")


@login_required
def attachment(request, pk):
    """A file attached to a comment, for the filing organization and CPUC staff; each download is an audit event on the version."""
    try:
        a = comments.attachment_for(request.user, pk)
    except (Attachment.DoesNotExist, AccessDenied) as e:
        raise Http404 from e
    comments.record_download(request.user, a)
    store = storage.store("uploads")
    url = store.download_url(a.key, a.name, content_type=a.content_type)
    if url:
        return HttpResponseRedirect(url)
    response = FileResponse(store.open(a.key), as_attachment=True, filename=a.name, content_type=a.content_type)
    response["X-Content-Type-Options"] = "nosniff"
    return response


# --- correcting a filing's records in place


def _correctable(request, pk) -> tuple[SubmissionVersion, str]:
    """The version, if the user may submit its filing (else 404), and why its records cannot be corrected now ("" = they can)."""
    v = get_object_or_404(SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation"), pk=pk)
    sub = v.submission
    if not access.has_permission(request.user, sub.organisation, f"{sub.deadline.module}.submit"):
        raise Http404
    nav.belongs_to(request, f"{sub.deadline.module}:index")  # the filing's own menu item, not the filing calendar's
    e = corrections.editor_for(sub.deadline.module)
    if e is None:
        raise Http404
    if not v.locked and not v.imported:
        e.prepare(v)
        v.refresh_from_db()
    return v, corrections.refusal(request.user, v)


def _base_context(v: SubmissionVersion, why: str) -> dict:
    e = corrections.editor_for(v.submission.deadline.module)
    return {
        "v": v,
        "why": why,
        "editor": e,
        "back": e.version_page(v) if e else "",
        "tables": e.tables(v) if e and not why else [],
        "state": corrections.state(v),
        "max_rows": corrections.max_rows(),
        "list_rows": corrections.list_rows(),
        "max_mb": core_uploads.max_mb(),
    }


def _key_from(request, t) -> list[str]:
    key = request.GET.getlist("k") or request.POST.getlist("k")
    if len(key) != len(t.key) or not all(k.strip() for k in key):
        raise Http404
    return key


def _key_query(t, key) -> str:
    from urllib.parse import urlencode

    return urlencode([("k", k) for k in key])


def _outcome(request, v: SubmissionVersion, scope) -> None:
    s = v.findings or {}
    n = sum(1 for c in scope.changes if c.kind != "carry")
    what = f"Saved {n:,} record{'s' if n != 1 else ''}"
    again = (s.get("checked_again") or {}).get("records")
    checked = f"checked {again:,} record{'s' if again != 1 else ''} again" if again is not None else "checked every record again"
    if v.status == SubmissionVersion.Status.VALIDATED:
        rest = "none is rejected, so the version is validated. Price it again before confirming."
        rest += f" {s['warnings']:,} have warnings." if s.get("warnings") else ""
        messages.success(request, f"{what} and {checked}: {rest}")
    else:
        messages.warning(request, f"{what} and {checked}: {s.get('rejected', 0):,} records are still rejected.")


@login_required
def records(request, pk):
    """Find a version's records to correct, add one, or upload a partial correction."""
    v, why = _correctable(request, pk)
    ctx = _base_context(v, why)
    if not why:
        table, q = request.GET.get("table", ""), request.GET.get("q", "").strip()
        names = {t.name for t in ctx["tables"]}
        table = table if table in names else ""
        ctx.update({"table": table, "q": q, "searched": "q" in request.GET or "table" in request.GET})
        if ctx["searched"]:
            found, n = corrections.find(v, table, q)
            defs = {t.name: t for t in ctx["tables"]}
            ctx["found"] = [(f, _key_query(defs[f.table], f.key)) for f in found]
            ctx["found_count"] = n
        ctx["summary"] = corrections.summary(v)
        ctx["s"] = v.findings or {}
    return render(request, "submissions/records.html", ctx)


def _field_rows(t, values: dict, findings: list[dict], *, key_locked: bool) -> list[dict]:
    by_field: dict[str, list[dict]] = {}
    for f in findings:
        for name in str(f.get("field", "")).split(", "):
            by_field.setdefault(name, []).append(f)
    return [
        {
            "f": fd,
            "value": values.get(fd.name, ""),
            "findings": by_field.get(fd.name, []),
            "locked": key_locked and fd.name in t.key,
        }
        for fd in t.fields
    ]


@login_required
def record(request, pk, table):
    """One record on a form: its findings beside its fields; saving re-checks it and the records its checks touch."""
    v, why = _correctable(request, pk)
    if why:
        return render(request, "submissions/records.html", _base_context(v, why))
    t = corrections.tables_of(v).get(table)
    if t is None:
        raise Http404
    key = _key_from(request, t)
    found = corrections.record(v, table, key)
    if found is None:
        raise Http404
    values = dict(found.row)
    if request.method == "POST":
        posted = {fd.name: request.POST.get(f"f-{fd.name}", "") for fd in t.fields}
        if found.earlier:  # an earlier record's key stays
            posted.update({c: found.row.get(c, "") for c in t.key})
        have = set(corrections.editor(v).rows(v).get(table, corrections.Rows(table, [], [])).columns) | set(found.row)
        posted = {c: x for c, x in posted.items() if c in have or x.strip()}  # a blank column the file never had stays out of it
        try:
            v, scope = corrections.correct(
                request.user, v, table, key, posted, reason=request.POST.get("reason", "").strip(), expected=request.POST.get("state", "")
            )
        except (AccessDenied, corrections.CorrectionRefused) as e:
            messages.error(request, f"Not saved: {e}")
            values.update(posted)
        else:
            _outcome(request, v, scope)
            new = next((c.after for c in scope.changes if c.kind == "edit"), None) or found.row
            url = reverse("submissions:record", args=[v.pk, table]) + "?" + _key_query(t, [new.get(c, "") for c in t.key])
            return redirect(url)
    findings = corrections.row_findings(v, table, found.line)
    ctx = _base_context(v, "")
    ctx.update(
        {
            "t": t,
            "found": found,
            "key_query": _key_query(t, key),
            "rows": _field_rows(t, values, findings, key_locked=bool(found.earlier)),
            "findings": findings,
            "rejected": any(f.get("severity") == "reject" for f in findings),
            "mode": "edit",
        }
    )
    return render(request, "submissions/record.html", ctx)


@login_required
def record_new(request, pk, table):
    v, why = _correctable(request, pk)
    if why:
        return render(request, "submissions/records.html", _base_context(v, why))
    t = corrections.tables_of(v).get(table)
    if t is None:
        raise Http404
    values: dict = {}
    if request.method == "POST":
        values = {fd.name: request.POST.get(f"f-{fd.name}", "") for fd in t.fields}
        have = set(corrections.editor(v).rows(v).get(table, corrections.Rows(table, [], [])).columns) | set(t.key)
        posted = {c: x for c, x in values.items() if c in have or x.strip()}
        try:
            v, scope = corrections.add(
                request.user, v, table, posted, reason=request.POST.get("reason", "").strip(), expected=request.POST.get("state", "")
            )
        except (AccessDenied, corrections.CorrectionRefused) as e:
            messages.error(request, f"Not added: {e}")
        else:
            _outcome(request, v, scope)
            new = scope.changes[-1].after
            return redirect(reverse("submissions:record", args=[v.pk, table]) + "?" + _key_query(t, [new.get(c, "") for c in t.key]))
    ctx = _base_context(v, "")
    ctx.update({"t": t, "rows": _field_rows(t, values, [], key_locked=False), "findings": [], "mode": "add"})
    return render(request, "submissions/record.html", ctx)


@login_required
def record_delete(request, pk, table):
    v, why = _correctable(request, pk)
    if why:
        return render(request, "submissions/records.html", _base_context(v, why))
    t = corrections.tables_of(v).get(table)
    if t is None:
        raise Http404
    key = _key_from(request, t)
    found = corrections.record(v, table, key)
    if found is None:
        raise Http404
    if request.method == "POST":
        try:
            v, scope = corrections.delete(
                request.user, v, table, key, reason=request.POST.get("reason", "").strip(), expected=request.POST.get("state", "")
            )
        except (AccessDenied, corrections.CorrectionRefused) as e:
            messages.error(request, f"Not deleted: {e}")
        else:
            _outcome(request, v, scope)
            return redirect("submissions:records", v.pk)
    ctx = _base_context(v, "")
    ctx.update({"t": t, "found": found, "key_query": _key_query(t, key)})
    return render(request, "submissions/record_delete.html", ctx)


@login_required
@require_POST
def correction_upload(request, pk):
    """Take a partial correction's file and show what applying it would change."""
    v, why = _correctable(request, pk)
    if why:
        messages.error(request, why)
        return redirect("submissions:records", v.pk)
    f = request.FILES.get("file")
    if f is None:
        messages.error(request, "Choose the correction's .zip, .xlsx or .csv file.")
        return redirect("submissions:records", v.pk)
    if problem := core_uploads.too_large(f.name, f.size):
        messages.error(request, problem)
        return redirect("submissions:records", v.pk)
    try:
        p = corrections.preview(request.user, v, f.read(), f.name)
    except (AccessDenied, corrections.CorrectionRefused) as e:
        messages.error(request, f"The correction was not read: {e}")
        return redirect(reverse("submissions:records", args=[v.pk]) + "#partial-h")
    return redirect("submissions:correction", v.pk, p.token)


def _preview_of(request, v: SubmissionVersion, token: str):
    got = corrections.pending(v, token)
    if got is None:
        raise Http404
    data, name = got
    return corrections.preview(request.user, v, data, name)


@login_required
def correction(request, pk, token):
    """A partial correction's preview: what applying it would change, record by record; nothing is changed until it is applied."""
    v, why = _correctable(request, pk)
    if why:
        return render(request, "submissions/records.html", _base_context(v, why))
    try:
        p = _preview_of(request, v, token)
    except corrections.CorrectionRefused as e:
        messages.error(request, f"The correction no longer applies: {e}")
        return redirect("submissions:records", v.pk)
    ctx = _base_context(v, "")
    limit = corrections.list_rows()
    shown = [c for c in p.changes if c.kind != "carry"][:limit]
    ctx.update(
        {
            "p": p,
            "shown": [{"c": c, "fields": [(n, (c.before or {}).get(n, ""), (c.after or {}).get(n, "")) for n in c.fields]} for c in shown],
            "carried": sum(1 for c in p.changes if c.kind == "carry"),
            "counts": [(table, kinds) for table, kinds in p.counts.items()],
            "more": p.total - len(shown),
        }
    )
    return render(request, "submissions/correction.html", ctx)


@login_required
def correction_csv(request, pk, token):
    v, why = _correctable(request, pk)
    if why:
        raise Http404
    try:
        p = _preview_of(request, v, token)
    except corrections.CorrectionRefused as e:
        raise Http404 from e
    response = HttpResponse(corrections.changes_csv(p, corrections.tables_of(v)), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="correction_v{v.number}_{token[:12]}.csv"'
    return response


@login_required
@require_POST
def correction_apply(request, pk, token):
    v, why = _correctable(request, pk)
    if why:
        messages.error(request, why)
        return redirect("submissions:records", v.pk)
    try:
        v, scope = corrections.apply_preview(
            request.user, v, token, request.POST.get("state", ""), reason=request.POST.get("reason", "").strip()
        )
    except (AccessDenied, corrections.CorrectionRefused) as e:
        messages.error(request, f"Not applied: {e}")
        return redirect("submissions:records", v.pk)
    _outcome(request, v, scope)
    e = corrections.editor_for(v.submission.deadline.module)
    return redirect(e.version_page(v) or reverse("submissions:records", args=[v.pk]))

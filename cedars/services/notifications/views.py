import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from cedars.services.accounts.models import Organisation
from cedars.services.registry import service as registry
from cedars.services.registry.models import Program

from . import service
from .models import PROGRAM_EVENTS, DeadlineReminder, Notification, Preference, Subscription

OFF = "off"
BLANK_REMINDER_ROWS = 2
PROGRAM_EVENT_LABELS = {
    "submission.published:budgets": "A budget filing that includes it is published or corrected",
    "fund_shift.effective": "A fund shift that moves its funds takes effect",
    "submission.published:monthly": "A monthly report that reports it is published or corrected",
    "submission.published:claims": "Claims for it are published or corrected",
    "program.changed": "Its details change (a new version of its record)",
    "documents.version_published": "A document about it is published",
}
PA_GROUPS = [
    ("IOU", "Investor-owned utilities"),
    ("REN", "Regional energy networks"),
    ("CCA", "Community choice aggregators"),
    ("", "Other"),
]


@login_required
def index(request):
    """The user's notifications, newest first; opening the page does not mark them read (a button does)."""
    if request.method == "POST":
        n = service.mark_read(request.user, request.POST.getlist("id") or None)
        messages.success(request, f"{n} marked as read." if n != 1 else "1 marked as read.")
        return redirect("notifications:index")
    page = Paginator(Notification.objects.filter(user=request.user), 50).get_page(request.GET.get("page"))
    return render(request, "notifications/index.html", {"page": page, "unread": service.unread(request.user)})


def _program_name(p: Program) -> str:
    v = registry.current_version(p) or p.versions.order_by("-valid_from").first()
    return v.name if v else ""


def _reminder_modules() -> list[tuple[str, str]]:
    from cedars.services.submissions.models import Deadline

    known = dict(service.FILING_MODULES)
    for m in Deadline.objects.order_by().values_list("module", flat=True).distinct():
        known.setdefault(m, m.replace("_", " ").capitalize())
    return [("", "Every filing")] + sorted(known.items(), key=lambda x: x[1])


def _save_follows(request, pref: Preference) -> list[str]:
    """The PAs and programs parts of the form; returns the problems to tell the user (what was not saved)."""
    problems = []
    scope = request.POST.get("pa_scope", pref.pa_scope)
    if scope not in Preference.PAScope.values:
        scope = pref.pa_scope
    ids = [int(x) for x in request.POST.getlist("pa") if x.isdigit()]
    pas = Organisation.objects.filter(pk__in=ids, kind=Organisation.Kind.PA)
    if scope == Preference.PAScope.CHOSEN and not pas:
        problems.append("You chose to follow only the PAs you choose but ticked none, so you follow no PA until you tick one.")
    keep = set(request.POST.getlist("program"))
    programs = {p.pk: p for p in pref.programs.all() if p.prg_id in keep}
    asked = [x for x in re.split(r"[\s,;]+", request.POST.get("add_programs", "")) if x]
    unknown = []
    for prg in asked:
        p = Program.objects.filter(prg_id__iexact=prg).first()
        if p is None:
            unknown.append(prg)
        else:
            programs[p.pk] = p
    if unknown:
        problems.append(f"Not followed, as no program has the PrgID {', '.join(unknown)}. Check the PrgID on the Programs catalog.")
    events = [k for k in request.POST.getlist("program_event") if k in PROGRAM_EVENTS]
    channel = request.POST.get("program_channel", pref.program_channel)
    if channel not in Subscription.Channel.values:
        channel = pref.program_channel
    service.set_follows(
        request.user, pa_scope=scope, pas=pas, programs=list(programs.values()), program_events=events, program_channel=channel
    )
    return problems


def _save_reminders(request) -> list[str]:
    rows, problems = [], []
    count = request.POST.get("reminder_rows", "0")
    for i in range(int(count) if count.isdigit() else 0):
        days = request.POST.get(f"r{i}_days", "").strip()
        if not days:
            continue
        if not days.isdigit() or int(days) > DeadlineReminder.MAX_DAYS:
            problems.append(f"Reminder {i + 1}: give a whole number of days from 0 to {DeadlineReminder.MAX_DAYS} ({days!r} is not).")
            continue
        channel = request.POST.get(f"r{i}_channel", Subscription.Channel.EMAIL)
        rows.append((int(days), request.POST.get(f"r{i}_module", ""), channel))
    if problems:
        return problems + ["Your deadline reminders were left as they were."]
    try:
        service.set_reminders(request.user, rows)
    except ValueError as e:
        return [str(e), "Your deadline reminders were left as they were."]
    return []


@login_required
def settings_page(request):
    """What the user hears about, and how (BR-09.2): the PAs and programs they follow, each event off, on the page, or on the page and
    by email, their deadline reminders, and messages about their own work."""
    user = request.user
    pref, _ = Preference.objects.get_or_create(user=user)
    if request.method == "POST":
        for e in service.EVENTS:
            choice = request.POST.get(e.key, OFF)
            if choice == OFF:
                service.unsubscribe(user, e.code, module=e.module)
            elif choice in Subscription.Channel.values:
                service.subscribe(user, e.code, choice, module=e.module)
        problems = []
        if request.POST.get("follows"):
            problems += _save_follows(request, pref)
        if request.POST.get("reminders"):
            problems += _save_reminders(request)
        pref.refresh_from_db()
        pref.email_direct = request.POST.get("email_direct") == "on"
        pref.save(update_fields=["email_direct"])
        for p in problems:
            messages.error(request, p)
        messages.success(request, "Notification settings saved." if not problems else "The rest of your notification settings are saved.")
        return redirect("notifications:settings")
    current = {(s.event, s.module): s.channel for s in Subscription.objects.filter(user=user, organisation__isnull=True)}
    groups = {}
    for e in service.EVENTS:
        groups.setdefault(e.group, []).append((e, current.get((e.code, e.module), OFF)))
    chosen = set(pref.pas.values_list("pk", flat=True))
    pas = {}
    for o in Organisation.objects.filter(kind=Organisation.Kind.PA, active=True).order_by("code"):
        pas.setdefault(o.pa_type, []).append((o, o.pk in chosen))
    reminders = list(DeadlineReminder.objects.filter(user=user).order_by("-days_before", "module"))
    rows = [(r.days_before, r.module, r.channel) for r in reminders] + [("", "", Subscription.Channel.EMAIL)] * BLANK_REMINDER_ROWS
    choices = [(OFF, "Off")] + list(Subscription.Channel.choices)
    return render(
        request,
        "notifications/settings.html",
        {
            "groups": groups.items(),
            "choices": choices,
            "channels": Subscription.Channel.choices,
            "pref": pref,
            "scopes": Preference.PAScope.choices,
            "pa_groups": [(label, pas[t]) for t, label in PA_GROUPS if pas.get(t)],
            "programs": [(p, _program_name(p)) for p in pref.programs.order_by("prg_id")],
            "program_events": [(k, PROGRAM_EVENT_LABELS[k], k in (pref.program_events or [])) for k in PROGRAM_EVENTS],
            "reminders": list(enumerate(rows)),
            "reminder_rows": len(rows),
            "reminder_modules": _reminder_modules(),
            "has_reminders": bool(reminders),
        },
    )


def _next(request, default: str) -> str:
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return default


@login_required
@require_POST
def follow_program(request):
    """Follow or stop following one program (the button on a program's page)."""
    prg_id = request.POST.get("prg_id", "")
    if not prg_id:
        raise Http404
    p = get_object_or_404(Program, prg_id=prg_id)
    if request.POST.get("action") == "unfollow":
        service.follow_program(request.user, p, follow=False)
        messages.success(request, f"You no longer follow {p.prg_id}.")
    else:
        service.follow_program(request.user, p)
        messages.success(
            request,
            f"You follow {p.prg_id}. You hear about it for the filing, program and document events you subscribe to on your "
            "notification settings.",
        )
    return redirect(_next(request, f"/programs/{p.prg_id}/"))

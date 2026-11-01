"""Notifications: subscriptions fed from the audit trail, reminders, and direct messages from modules (a run finished, access
changed). Every message lands on the user's notifications page; email is per subscription (or the user's preference for direct
messages). Delivery happens after the transaction that wrote the event commits, so a rolled-back change notifies nobody.

What a user follows (BR-09.2): the PAs (every PA, the ones they choose, or none) and any programs by PrgID. A subscribed event that
concerns a PA reaches the subscriber only for a PA they follow; events about no PA (reference data, rules, the calendar) reach every
subscriber, and approvers' work (a correction or program change waiting on them) is never narrowed. A program's followers also hear,
whatever their PA choice and subscriptions, of the PROGRAM_EVENTS they kept ticked that concern it: a filing or a correction that
includes it, a fund shift that moves its funds, a change to its record, a document about it.

What an event concerns comes from the record it is about. This service reads its own services' records; a module tells it about its
own through hooks, so the service never imports a module:
  - `register_filing(module, describe)`: describe(version, event) -> Concern for a filing version's events (its programs, a summary of
    what it holds or, for a correction, what changed, and the page to open). Budgets, Monthly reports and Claims register one; a
    corrected version's publication (a refile, BR-09.3) then tells every subscriber of its PA or of a program it changed what changed.
  - `register_concerns(object_type, describe)`: describe(event) -> Concern for a module's own records (a fund shift, a document).

Deadline reminders: each user keeps their own (`DeadlineReminder`): how many days before a due date, for one module or every module."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from cedars.services.accounts import service as access

from .models import PROGRAM_EVENTS, DeadlineReminder, Notification, Preference, Subscription

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventType:
    code: str
    label: str
    scope: str  # "public": anyone; "organisation": members of the organization it concerns, and CPUC staff; "cpuc": CPUC staff only
    group: str
    module: str = ""  # an event offered once per module (a filing published): the module this row is for
    narrowed: bool = True  # narrowed by the PAs and programs the user follows; False for an approver's work

    @property
    def key(self) -> str:
        """The settings page's name for the row."""
        return f"{self.code}:{self.module}" if self.module else self.code


FILINGS = "Filings: budgets, fund shifts, monthly reports and claims"
EVENTS = [
    EventType("submission.published", "A budget filing is published, or a published one is corrected", "public", FILINGS, "budgets"),
    EventType("fund_shift.effective", "A fund shift takes effect", "public", FILINGS),
    EventType("submission.published", "A monthly report is published, or a published one is corrected", "public", FILINGS, "monthly"),
    EventType("submission.published", "Claims are published, or published claims are corrected", "public", FILINGS, "claims"),
    EventType("submission.unconfirmed", "A filing is un-confirmed before its due date", "organisation", FILINGS),
    EventType("submission.reopened", "A filing is reopened for a correction", "organisation", FILINGS),
    EventType("reopen.requested", "A correction is requested after a due date (for approvers)", "cpuc", FILINGS, narrowed=False),
    EventType("reopen.declined", "A correction request is declined", "organisation", FILINGS),
    EventType("program.changed", "A program's details change (a new version of its record)", "public", "Programs"),
    EventType("allocation_version.confirmed", "Statewide allocation factors are confirmed", "public", "Programs"),
    EventType(
        "program_change.requested",
        "A change to a locked program attribute is requested (for approvers)",
        "cpuc",
        "Programs",
        narrowed=False,
    ),
    EventType("program_change.approved", "A program change request is approved", "organisation", "Programs"),
    EventType("program_change.declined", "A program change request is declined", "organisation", "Programs"),
    EventType("program_policy.changed", "The locked program attributes change", "public", "Programs"),
    EventType("documents.version_published", "A document filed by or about a PA, or about a program, is published", "public", "Documents"),
    EventType("deadline.created", "A deadline is added to the filing calendar", "public", "Filing calendar"),
    EventType("deadline.moved", "A deadline moves", "public", "Filing calendar"),
    EventType("pack.published", "Reference data is published (ACC, impact profiles, DEER, value lists ...)", "public", "Reference data"),
    EventType("pack.withdrawn", "Reference data is withdrawn", "public", "Reference data"),
    EventType("rule_set.published", "A validation rule set is published", "public", "Rules and dictionary"),
    EventType("dictionary.published", "A new version of the data dictionary is published", "public", "Rules and dictionary"),
    EventType("value_list.published", "A new version of a value list is published", "public", "Rules and dictionary"),
]
BY_KEY = {e.key: e for e in EVENTS}
CODES = {e.code for e in EVENTS}
FILING_MODULES = {"budgets": "Budgets", "monthly": "Monthly reports", "claims": "Claims"}


def event_type(code: str, module: str = "") -> EventType | None:
    return BY_KEY.get(f"{code}:{module}") or BY_KEY.get(code)


# --- what an event concerns: its organizations, module, programs and page


@dataclass
class Concern:
    organisations: set = field(default_factory=set)  # organization ids
    module: str = ""
    link: str = ""
    programs: set = field(default_factory=set)  # PrgIDs
    title: str = ""  # replaces "<event label>: <record>"
    body: str = ""  # replaces "Reason: <reason>"
    key: str = ""  # sends the message once per user (e.g. the key a module's own subscriptions use for the same event)


_filings: dict[str, Callable] = {}
_concern_hooks: dict[str, Callable] = {}


def register_filing(module: str, describe: Callable) -> None:
    """describe(version, event) -> Concern | None, for the events of a module's filing versions (see the module docstring)."""
    _filings[module] = describe


def register_concerns(object_type: str, describe: Callable) -> None:
    """describe(event) -> Concern | None, for audit events about a module's own records (object_type: app_label.model)."""
    _concern_hooks[object_type] = describe


def _program_concern(event) -> Concern:
    from cedars.services.registry.models import Program

    p = Program.objects.get(pk=event.object_id)
    before, after = event.before or {}, event.after or {}
    changed = [k for k in after if k not in ("valid_from", "valid_to") and before.get(k) != after.get(k)]
    lines = []
    if changed:
        lines.append("Changed: " + ", ".join(k.replace("_", " ") for k in changed) + ".")
    if after.get("valid_from"):
        lines.append(f"In effect from {after['valid_from']}.")
    if event.reason:
        lines.append(f"Reason: {event.reason}")
    return Concern(
        {p.administrator_id} if p.administrator_id else set(),
        "programs",
        f"/programs/{p.prg_id}/",
        {p.prg_id},
        title=f"{p.prg_id} has changed",
        body="\n".join(lines),
    )


def _concerns(event) -> Concern:
    """What an audit event concerns, from the record it is about."""
    t, oid = event.object_type, event.object_id
    try:
        if t == "submissions.submissionversion":
            from cedars.services.submissions.models import SubmissionVersion

            v = SubmissionVersion.objects.select_related("submission__deadline", "submission__organisation").get(pk=oid)
            c = Concern({v.submission.organisation_id}, v.submission.deadline.module)
            if event.action == "submission.published" and v.number > 1:
                c.title = f"Corrected: {event.object_repr}"
            describe = _filings.get(c.module)
            more = describe(v, event) if describe else None
            if more is not None:
                c.programs = set(more.programs)
                c.link = more.link or c.link
                c.title = more.title or c.title
                c.body = more.body
                c.key = more.key
            return c
        if t == "submissions.reopenrequest":
            from cedars.services.submissions.models import ReopenRequest

            r = ReopenRequest.objects.select_related("submission__deadline").get(pk=oid)
            return Concern({r.submission.organisation_id}, r.submission.deadline.module, "/filings/reopen-requests/")
        if t == "submissions.deadline":
            from cedars.services.submissions.models import Deadline

            return Concern(set(), Deadline.objects.get(pk=oid).module, "/filings/calendar/")
        if t == "registry.program":
            return _program_concern(event)
        if t == "registry.programchangerequest":
            from cedars.services.registry.models import ProgramChangeRequest

            r = ProgramChangeRequest.objects.select_related("program").get(pk=oid)
            return Concern({r.organisation_id}, "programs", f"/programs/requests/{oid}/", {r.program.prg_id})
        if t == "registry.programchangepolicy":
            return Concern(set(), "programs", "/programs/locked/")
        if t == "refdata.packversion":
            return Concern(link=f"/reference-data/{oid}/")
        if t == "rules.ruleset":
            return Concern(link=f"/rules/{oid}/")
        if t == "dictionary.catalogversion":
            return Concern(link="/dictionary/")
        if t in _concern_hooks:
            c = _concern_hooks[t](event)
            if c is not None:
                return c
    except Exception:  # the record may be gone (a discarded draft); the event still says what happened
        log.debug("notification: %s %s not found", t, oid, exc_info=True)
    return Concern({event.organisation_id} if event.organisation_id else set())


def may_see(user, scope: str, organisations) -> bool:
    """organisations: the ids of the organizations the event concerns (an int or None is taken as one id or none)."""
    if scope == "public":
        return True
    if organisations is None or isinstance(organisations, int):
        organisations = {organisations} if organisations is not None else set()
    mine = set(access.organisations_of(user).values_list("pk", flat=True))
    cpuc = access.cpuc()
    staff = cpuc is not None and cpuc.pk in mine
    if scope == "cpuc":
        return staff
    return staff or bool(set(organisations) & mine)


# --- what a user follows


@dataclass
class Follows:
    scope: str = Preference.PAScope.ALL
    pas: set = field(default_factory=set)  # organization ids
    programs: set = field(default_factory=set)  # PrgIDs
    events: set = field(default_factory=lambda: set(PROGRAM_EVENTS))  # what the programs' followers hear about
    channel: str = Subscription.Channel.EMAIL

    def wants(self, c: Concern) -> bool:
        """A subscribed event concerning c passes the user's PA choice (see Preference)."""
        if not c.organisations:
            return not c.programs or self.scope == Preference.PAScope.ALL
        if self.scope == Preference.PAScope.ALL:
            return True
        if self.scope == Preference.PAScope.CHOSEN:
            return bool(c.organisations & self.pas)
        return False

    def program_hears(self, kind: "EventType", c: Concern) -> bool:
        """The event reaches the user as the follower of one of its programs."""
        return kind.key in self.events and bool(c.programs & self.programs)


def _follows(pref: Preference) -> Follows:
    return Follows(
        pref.pa_scope,
        {o.pk for o in pref.pas.all()},
        {p.prg_id for p in pref.programs.all()},
        set(pref.program_events or []),
        pref.program_channel,
    )


def follows(user) -> Follows:
    pref = Preference.objects.filter(user=user).prefetch_related("pas", "programs").first()
    return _follows(pref) if pref is not None else Follows()


@transaction.atomic
def set_follows(user, *, pa_scope=None, pas=None, programs=None, program_events=None, program_channel=None) -> Preference:
    """Change what the user follows: the PA choice, the PAs chosen (organizations), the programs (Program records), what the programs'
    followers hear about (keys of PROGRAM_EVENTS) and how. None leaves a part as it is."""
    pref, _ = Preference.objects.get_or_create(user=user)
    if pa_scope is not None:
        if pa_scope not in Preference.PAScope.values:
            raise ValueError("Choose which PAs to follow.")
        pref.pa_scope = pa_scope
    if program_events is not None:
        unknown = set(program_events) - set(PROGRAM_EVENTS)
        if unknown:
            raise ValueError(f"{', '.join(sorted(unknown))} is not something a program's followers hear about.")
        pref.program_events = [k for k in PROGRAM_EVENTS if k in program_events]
    if program_channel is not None:
        if program_channel not in Subscription.Channel.values:
            raise ValueError("Choose how the messages come.")
        pref.program_channel = program_channel
    pref.save()
    if pas is not None:
        pref.pas.set(pas)
    if programs is not None:
        pref.programs.set(programs)
    return pref


def follow_program(user, program, *, follow: bool = True) -> Preference:
    pref, _ = Preference.objects.get_or_create(user=user)
    (pref.programs.add if follow else pref.programs.remove)(program)
    return pref


def follows_program(user, program) -> bool:
    if user is None or not user.is_authenticated:
        return False
    return Preference.objects.filter(user=user, programs=program).exists()


# --- delivery


def on_audit_event(sender, instance, created, **kwargs):
    if created and instance.action in CODES:
        transaction.on_commit(lambda: deliver(instance))


def deliver(event) -> list[Notification]:
    """Fan one audit event out to its subscribers and to the followers of the programs it concerns. One message per user, by email if
    any of the ways it reaches them chose email. Never raises: a failed delivery is logged, not the change that caused it."""
    try:
        c = _concerns(event)
        chosen: dict = {}  # user -> (event type, email?)

        def add(user, kind, email):
            was = chosen.get(user)
            chosen[user] = (was[0] if was else kind, (was[1] if was else False) or email)

        subs = list(Subscription.objects.filter(event=event.action).select_related("user"))
        prefs = Preference.objects.prefetch_related("pas", "programs").select_related("user")
        followed = {p.user_id: _follows(p) for p in prefs.filter(user_id__in={s.user_id for s in subs})}
        for s in subs:
            kind = event_type(event.action, s.module or c.module)
            if kind is None or (kind.module and kind.module != c.module):
                continue
            if (s.module and c.module and s.module != c.module) or (s.organisation_id and s.organisation_id not in c.organisations):
                continue
            if not s.user.is_active or not may_see(s.user, kind.scope, c.organisations):
                continue
            if kind.narrowed and not followed.get(s.user_id, Follows()).wants(c):
                continue
            add(s.user, kind, s.channel == Subscription.Channel.EMAIL)
        kind = event_type(event.action, c.module)
        if c.programs and kind is not None and kind.key in PROGRAM_EVENTS and (not kind.module or kind.module == c.module):
            for pref in prefs.filter(programs__prg_id__in=c.programs, user__is_active=True).distinct():
                f = _follows(pref)
                if f.program_hears(kind, c) and may_see(pref.user, kind.scope, c.organisations):
                    add(pref.user, kind, f.channel == Subscription.Channel.EMAIL)
        out = []
        for user, (kind, email) in chosen.items():
            title = c.title or (f"{kind.label}: {event.object_repr}" if event.object_repr else kind.label)
            body = c.body or (event.reason and f"Reason: {event.reason}") or ""
            out.append(_create(user, title, body, c.link, email=email, event=event, key=c.key))
        return [n for n in out if n]
    except Exception:
        log.exception("notification delivery failed for audit event %s", event.pk)
        return []


def _create(user, title, body="", link="", *, email=False, event=None, key="") -> Notification | None:
    try:
        with transaction.atomic():
            n = Notification.objects.create(user=user, title=title[:300], body=body, link=link, email=email, event=event, key=key)
    except IntegrityError:  # this key was sent already
        return None
    if email:
        _send(n)
    return n


def _send(n: Notification) -> None:
    link = f"{settings.CEDARS_BASE_URL.rstrip('/')}{n.link}" if n.link else ""
    text = "\n\n".join(
        x for x in [n.body, link, "Change what you are sent: " + settings.CEDARS_BASE_URL.rstrip("/") + "/notifications/settings/"] if x
    )
    try:
        prefix = "[SANDBOX] " if settings.ENVIRONMENT == "sandbox" else ""
        send_mail(f"{prefix}CEDARS: {n.title}"[:200], text, None, [n.user.email])
    except Exception as e:  # the message stays on the page; the error is shown to admins and the user
        n.email_error = str(e)[:1000]
        log.warning("notification email to user %s failed: %s", n.user_id, e)
    else:
        n.emailed_at = timezone.now()
    n.save(update_fields=["emailed_at", "email_error"])


def notify(user, title: str, body: str = "", link: str = "", *, key: str = "") -> Notification | None:
    """A message to one user from a module (e.g. Calculations when a run finishes or fails). Emailed unless the user turned that off."""
    pref = Preference.objects.filter(user=user).first()
    return _create(user, title, body, link, email=pref.email_direct if pref else True, key=key)


def send(user, title: str, body: str = "", link: str = "", *, email: bool = True, key: str = "") -> Notification | None:
    """A message to one user from a module's own subscriptions (e.g. Documents' subscriptions to a study or an attribute, BR-15.5), by
    the channel that subscription chose. With a key it is sent once per user."""
    if not user.is_active:
        return None
    return _create(user, title, body, link, email=email, key=key)


def _due_text(days: int) -> str:
    return "Due today" if days == 0 else "Due tomorrow" if days == 1 else f"Due in {days} days"


def send_deadline_reminders(today=None) -> int:
    """Remind each user of the deadlines due within the days they chose (their DeadlineReminder rows). A deadline is due in n days;
    the user's reminder with the fewest days that are still n or more is the one sent, once per deadline, reminder and due date (a moved
    deadline is reminded of again). So a reminder set up late, or a day the job did not run, still sends it, and a user with reminders
    14 and 2 days before hears twice. Safe to run more than once a day."""
    from cedars.services.submissions.models import Deadline

    today = today or timezone.localdate()
    reminders: dict = {}
    for r in DeadlineReminder.objects.filter(user__is_active=True).select_related("user"):
        reminders.setdefault(r.user, []).append(r)
    if not reminders:
        return 0
    horizon = max(r.days_before for rs in reminders.values() for r in rs)
    deadlines = Deadline.objects.filter(due_at__date__gte=today, due_at__date__lte=today + timedelta(days=horizon)).select_related(
        "period", "cycle"
    )
    sent = 0
    for d in deadlines:
        due = timezone.localtime(d.due_at).date()
        days = (due - today).days
        for user, rs in reminders.items():
            fits = [r for r in rs if (not r.module or r.module == d.module) and r.days_before >= days]
            if not fits:
                continue
            r = min(fits, key=lambda x: (x.days_before, x.module == ""))
            what = FILING_MODULES.get(d.module, d.module)
            n = _create(
                user,
                f"{_due_text(days)}: {what} {d.kind} {d.period or d.cycle}, {due:%Y-%m-%d}",
                d.notes,
                "/filings/calendar/",
                email=r.channel == Subscription.Channel.EMAIL,
                key=f"deadline:{d.pk}:{r.days_before}:{due:%Y%m%d}",
            )
            sent += n is not None
    return sent


@transaction.atomic
def set_reminders(user, rows) -> list[DeadlineReminder]:
    """Replace the user's deadline reminders with rows of (days before, module or "", channel); repeated rows are kept once."""
    out = {}
    for days, module, channel in rows:
        days = int(days)
        if not 0 <= days <= DeadlineReminder.MAX_DAYS:
            raise ValueError(f"A reminder is 0 to {DeadlineReminder.MAX_DAYS} days before the due date.")
        if channel not in Subscription.Channel.values:
            raise ValueError("Choose how the reminder comes.")
        out[(days, module or "")] = channel
    DeadlineReminder.objects.filter(user=user).delete()
    return [DeadlineReminder.objects.create(user=user, days_before=d, module=m, channel=c) for (d, m), c in sorted(out.items())]


def expired(cutoff):
    """Notifications sent before the cutoff, for the retention job (record class "notification")."""
    for n in Notification.objects.filter(created_at__lt=cutoff).only("pk").iterator():
        yield f"notification:{n.pk}", n.delete


# --- reading and settings


def unread(user) -> int:
    return Notification.objects.filter(user=user, read_at__isnull=True).count()


def mark_read(user, ids=None) -> int:
    qs = Notification.objects.filter(user=user, read_at__isnull=True)
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    return qs.update(read_at=timezone.now())


def subscribe(user, event: str, channel: str = Subscription.Channel.EMAIL, *, module: str = "", organisation=None) -> Subscription:
    if event not in CODES:
        raise ValueError(f"{event} is not an event you can subscribe to")
    s, _ = Subscription.objects.update_or_create(
        user=user, event=event, module=module, organisation=organisation, defaults={"channel": channel}
    )
    return s


def unsubscribe(user, event: str, *, module: str = "", organisation=None) -> None:
    Subscription.objects.filter(user=user, event=event, module=module, organisation=organisation).delete()

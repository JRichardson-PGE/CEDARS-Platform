"""Template filters behind the shared components (docs/DESIGN.md): a due date's urgency in words, a status's badge tone, the one way a
date, a date and time, a person and a filing cycle are written. Loaded on every template (a builtin in settings.TEMPLATES).

Words, never color alone: a badge always carries its status text, and a due cell says "Passed 7 months ago" or "Due in 12 days" under the
date, so the urgency is read by everyone (WCAG 1.4.1)."""

from django import template
from django.template.defaultfilters import date as django_date
from django.utils import timezone
from django.utils.html import escape, format_html
from django.utils.timesince import timesince, timeuntil

register = template.Library()

# Submission version statuses (services.submissions) and the other words pages put in a badge, by tone.
_TONES = {
    "ok": {"published", "confirmed", "approved", "accepted", "active", "effective", "complete", "completed", "done", "paid"},
    "warn": {"draft", "pending", "waiting", "open", "overdue", "expiring", "late"},
    "bad": {"rejected", "declined", "failed", "error", "revoked", "withdrawn", "purged"},
    "info": {"validated", "queued", "running", "submitted", "imported", "new", "under review"},
}


@register.filter
def status_tone(status) -> str:
    """The badge tone for a status word or code: ok, warn, bad, info, or empty for a neutral badge ("Not started")."""
    key = str(status or "").strip().lower()
    for tone, words in _TONES.items():
        if key in words:
            return tone
    return ""


@register.filter
def due_words(due_at, now=None) -> str:
    """ "Due in 12 days", "Due today", or "Passed 7 months ago" for a deadline's due_at (one unit, the largest)."""
    if due_at is None:
        return ""
    now = now or timezone.now()
    if due_at.date() == now.date():
        return "Due today"
    if due_at < now:
        return f"Passed {timesince(due_at, now, depth=1)} ago"
    return f"Due in {timeuntil(due_at, now, depth=1)}"


@register.filter(expects_localtime=True)
def date_words(value) -> str:
    """A date as every record shows it: ISO, "2026-10-31" (UI review 2, 2026-10-31). Blank for none."""
    return django_date(value, "Y-m-d") if value else ""


@register.filter(expects_localtime=True)
def datetime_words(value) -> str:
    """A date and time, "2026-10-31 14:56 PDT", only where the minute matters: runs, uploads, jobs, logs and notifications. Everything
    else (a confirmation, a publication, a version) shows date_words."""
    return django_date(value, "Y-m-d H:i T") if value else ""


@register.filter
def cycle_label(cycle) -> str:
    """A filing cycle's years for people: "2024-2027", or "2021" for a one-year cycle (never "2021-2021"). Takes a FilingCycle or
    its "first-last" string; blank for none."""
    if not cycle:
        return ""
    first, last = (cycle.first_year, cycle.last_year) if hasattr(cycle, "first_year") else (str(cycle).split("-") + [None])[:2]
    return str(first) if last in (None, "", first) or str(first) == str(last) else f"{first}-{last}"


@register.filter
def person_name(user) -> str:
    """A person's name, or their email address when no name is recorded. Blank for none (callers add their own default)."""
    if not user:
        return ""
    return getattr(user, "name", "") or getattr(user, "email", "") or str(user)


@register.filter
def person(user):
    """A person as the pages show them: the name, with the email address after it in small muted text, or the email alone when no
    name is recorded (UI review 2: names before emails). Blank for none."""
    if not user:
        return ""
    name, email = getattr(user, "name", ""), getattr(user, "email", "")
    if name and email:
        return format_html('{} <span class="small muted">{}</span>', name, email)
    return escape(name or email or str(user))

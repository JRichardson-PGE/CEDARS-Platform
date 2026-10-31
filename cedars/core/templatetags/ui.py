"""Template filters behind the shared components (docs/DESIGN.md): a due date's urgency in words, and a status's badge tone.

Words, never color alone: a badge always carries its status text, and a due cell says "Passed 7 months ago" or "Due in 12 days" under the
date, so the urgency is read by everyone (WCAG 1.4.1)."""

from django import template
from django.utils import timezone
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

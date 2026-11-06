"""The calendar part of a module's set-up page (a budget filing's, a claim year's, a year of monthly reports'): the set-up's due dates,
changed there as on the filing calendar (Jake, 2026-11-06), and Remove this set-up while nobody has filed for it. The module's view
calls `calendar_forms` and its template includes "submissions/_setup_calendar.html"; the changes go through this service's
move_due_dates and remove_setup, so the calendar, the audit trail and the deadlines' subscribers stay in step."""

from __future__ import annotations

from datetime import datetime

from django import forms
from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone

from cedars.services.accounts.service import AccessDenied

from . import service


def _label(d) -> str:
    return f"{d.title}, {d.period}" if d.period_id else d.title


def _due_at(day, keep):
    """The due moment on `day`: the time of day the deadline already had."""
    if timezone.localtime(keep).date() == day:
        return keep
    return timezone.make_aware(datetime.combine(day, timezone.localtime(keep).time()))


class DueDatesForm(forms.Form):
    """One date per deadline still to come (a past one is listed beside it and moves on the filing calendar), and the reason."""

    reason = forms.CharField(label="Reason for the change", max_length=500, help_text="Kept in each deadline's history.")

    def __init__(self, deadlines, *args, **kw):
        super().__init__(*args, prefix="due", **kw)
        now = timezone.now()
        self.deadlines = [d for d in deadlines if d.retired_at is None]
        self.coming = [d for d in self.deadlines if d.due_at > now]
        self.past = [d for d in self.deadlines if d.due_at <= now]
        reason = self.fields.pop("reason")
        for d in self.coming:
            self.fields[f"on_{d.pk}"] = forms.DateField(
                label=_label(d),
                initial=timezone.localtime(d.due_at).date(),
                widget=forms.DateInput(attrs={"type": "date"}),
                help_text=f"Opens {timezone.localtime(d.opens_at):%Y-%m-%d}" if d.opens_at else "",
            )
        self.fields["reason"] = reason

    def date_fields(self):
        return [self[f"on_{d.pk}"] for d in self.coming]

    def moves(self):
        return [(d, _due_at(self.cleaned_data[f"on_{d.pk}"], d.due_at)) for d in self.coming]


class RemoveForm(forms.Form):
    reason = forms.CharField(label="Reason for removing it", max_length=500, help_text="Kept in the audit trail.")

    def __init__(self, *args, **kw):
        super().__init__(*args, prefix="remove", **kw)


def calendar_forms(request, deadlines, *, removable: bool, remove, removed_url: str, what: str):
    """The due-dates and remove forms of a set-up page, and the answer to a POST of either: (context, redirect or None).
    `remove(reason)` removes the set-up (the module's remove_setup); `what` names it in the messages ("2029 claims")."""
    action = request.POST.get("action") if request.method == "POST" else None
    due_form = DueDatesForm(list(deadlines), request.POST if action == "due" else None)
    remove_form = RemoveForm(request.POST if action == "remove" else None)
    if action == "due" and due_form.is_valid():
        try:
            moved = service.move_due_dates(request.user, due_form.moves(), due_form.cleaned_data["reason"])
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            n = len(moved)
            messages.success(request, f"Moved {n} due date{'s' if n != 1 else ''} on the filing calendar." if n else "No due date changed.")
            return {}, redirect(request.path)
    if action == "remove" and remove_form.is_valid():
        try:
            remove(remove_form.cleaned_data["reason"])
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"{what} removed from the filing calendar.")
            return {}, redirect(removed_url)
    return {"due_form": due_form, "remove_form": remove_form, "removable": removable, "setup_what": what}, None


def is_calendar_post(request) -> bool:
    """Whether a POST to a set-up page is for its calendar forms, not for the set-up's own form."""
    return request.method == "POST" and request.POST.get("action") in ("due", "remove")

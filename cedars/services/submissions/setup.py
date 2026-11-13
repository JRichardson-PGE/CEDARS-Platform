"""The calendar part of a module's set-up page (a budget filing's, a claim year's, a year of monthly reports'): the set-up's due dates,
changed there as on the filing calendar (Jake, 2026-11-06), the program administrators who file for it (Jake, 2026-11-09: not every
PA files every filing; `set_filers`), its filings one by one (Jake, 2026-11-09: cancel a single filing of any filing set, and
restore it; `retire_deadline` / `restore_deadline`), and Remove this set-up while nobody has filed for it. The module's view
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


def filers_field(initial=None) -> forms.MultipleChoiceField:
    """The "Who files" checkboxes, one per active program administrator, every one ticked unless `initial` (their codes) says
    otherwise. A module's form for setting up a filing adds it as `filers`; `chosen_pas` turns its value into organizations."""
    pas = service.filing_pas()
    return forms.MultipleChoiceField(
        label="Program administrators who file",
        choices=[(o.code, f"{o.code} ({o.name})") for o in pas],
        initial=list(initial) if initial is not None else [o.code for o in pas],
        required=bool(pas),
        widget=forms.CheckboxSelectMultiple,
        help_text="Not every PA files every filing. Only those ticked are expected to file, and only they are listed on the Filings "
        "page's status; this can change on the set-up page later.",
        error_messages={"required": "Choose at least one program administrator to file."},
    )


class _VersionChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.name + (" (current)" if obj.pk == self.current else "")


def specification_field(module: str) -> forms.ModelChoiceField | None:
    """The filing specification version a filing set is set up with: the published versions of the module's specification in effect,
    the current one chosen unless the administrator chooses another (specifications.moving.choices). None for a module without one;
    a module's form for setting up a filing adds it as `specification_version`."""
    from cedars.services.specifications import moving
    from cedars.services.specifications.models import SpecificationVersion

    versions = moving.choices(module)
    if not versions:
        return None
    field = _VersionChoice(
        label="Filing specification version",
        queryset=SpecificationVersion.objects.filter(pk__in=[v.pk for v in versions]).order_by("-published_at", "-pk"),
        initial=versions[0].pk,
        empty_label=None,
        required=False,  # not sent (a script, an older form): the current version
        help_text="The tables, fields, rules and checks every filing in the set follows, for every upload and later correction, and "
        "the engine release and cost-test set it names. The current version is chosen; a filing set can move to a later one.",
    )
    field.current = versions[0].pk
    return field


def chosen_pas(codes) -> list | None:
    """The organizations a filers_field's value names; None when it names none (a site with no PAs yet: every PA files)."""
    by_code = {o.code: o for o in service.filing_pas()}
    return [by_code[c] for c in codes or () if c in by_code] or None


class FilersForm(forms.Form):
    """Who files for a set-up's deadlines, and the reason for a change."""

    reason = forms.CharField(label="Reason for the change", max_length=500, help_text="Kept in each deadline's history.")

    def __init__(self, deadlines, *args, **kw):
        super().__init__(*args, prefix="filers", **kw)
        self.deadlines = [d for d in deadlines if d.retired_at is None]
        listed = {o.code for d in self.deadlines for o in d.filers.all()}
        reason = self.fields.pop("reason")
        self.fields["pas"] = filers_field(sorted(listed) if listed else None)
        self.fields["pas"].help_text = "Only those ticked are expected to file, and only they are listed on the Filings page's status."
        self.fields["reason"] = reason
        started = service.started_by(d.pk for d in self.deadlines)
        self.started = [o for o in service.filing_pas() if o.pk in started]


def filers(deadlines):
    """A set-up's deadlines that take a filing from the PAs (not one that only marks a date)."""
    return [d for d in deadlines if not service.nothing_to_file(d)]


class CancelForm(forms.Form):
    """Cancel one filing of the set: one no organization has started (what was filed is kept)."""

    filing = forms.TypedChoiceField(label="Filing to cancel", coerce=int)
    reason = forms.CharField(label="Reason for canceling it", max_length=500, help_text="Kept in the filing's history.")

    def __init__(self, deadlines, started, *args, **kw):
        super().__init__(*args, prefix="cancel", **kw)
        self.deadlines = [d for d in deadlines if d.retired_at is None and d.pk not in started]
        self.fields["filing"].choices = [("", "Choose a filing")] + [(d.pk, _label(d)) for d in self.deadlines]


class RestoreForm(forms.Form):
    """Put a canceled filing of the set back on the calendar."""

    filing = forms.TypedChoiceField(label="Filing to restore", coerce=int)
    reason = forms.CharField(label="Reason for restoring it", max_length=500, help_text="Kept in the filing's history.")

    def __init__(self, deadlines, *args, **kw):
        super().__init__(*args, prefix="restore", **kw)
        self.deadlines = [d for d in deadlines if d.retired_at is not None]
        self.fields["filing"].choices = [("", "Choose a filing")] + [(d.pk, _label(d)) for d in self.deadlines]


def filing_rows(deadlines, started_counts) -> list[dict]:
    """The set's filings for its "Filings in this set" table: each with its label, whether it is canceled and how many organizations
    have started it."""
    return [{"d": d, "label": _label(d), "started": started_counts.get(d.pk, 0), "canceled": d.retired_at is not None} for d in deadlines]


class RemoveForm(forms.Form):
    reason = forms.CharField(label="Reason for removing it", max_length=500, help_text="Kept in the audit trail.")

    def __init__(self, *args, **kw):
        super().__init__(*args, prefix="remove", **kw)


def calendar_forms(request, deadlines, *, removable: bool, remove, removed_url: str, what: str, filer_deadlines=None):
    """The due-dates, who-files and remove forms of a set-up page, and the answer to a POST of any: (context, redirect or None).
    `remove(reason)` removes the set-up (the module's remove_setup); `what` names it in the messages ("2029 claims");
    `filer_deadlines` are the deadlines whose filers the page sets (default: every one that takes a filing; claims leave out the
    statewide confirmations, which the statewide programs' lead PAs file)."""
    action = request.POST.get("action") if request.method == "POST" else None
    deadlines = list(deadlines)
    filer_deadlines = filers(deadlines) if filer_deadlines is None else list(filer_deadlines)
    due_form = DueDatesForm(deadlines, request.POST if action == "due" else None)
    filers_form = FilersForm(filer_deadlines, request.POST if action == "filers" else None)
    remove_form = RemoveForm(request.POST if action == "remove" else None)
    counts = service.started_counts(d.pk for d in deadlines)
    cancel_form = CancelForm(deadlines, set(counts), request.POST if action == "cancel" else None)
    restore_form = RestoreForm(deadlines, request.POST if action == "restore" else None)
    for form, do, done in (
        (cancel_form, service.retire_deadline, "canceled; the rest of {what} stays as it is"),
        (restore_form, service.restore_deadline, "restored to the filing calendar"),
    ):
        if action == form.prefix and form.is_valid():
            d = next(d for d in form.deadlines if d.pk == form.cleaned_data["filing"])
            try:
                do(request.user, d, form.cleaned_data["reason"])
            except AccessDenied as e:
                messages.error(request, str(e))
            else:
                messages.success(request, f"{_label(d)} {done.format(what=what)}.")
                return {}, redirect(request.path)
    if action == "filers" and filers_form.is_valid():
        try:
            changed = service.set_filers(
                request.user, filers_form.deadlines, chosen_pas(filers_form.cleaned_data["pas"]) or [], filers_form.cleaned_data["reason"]
            )
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"Saved who files for {what}." if changed else "Nobody was added or removed.")
            return {}, redirect(request.path)
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
    from django.urls import reverse

    from cedars.services.specifications import moving

    ctx = {
        "spec_pinned": moving.current_of(deadlines),
        "spec_later": moving.later(deadlines),
        "spec_move_url": reverse("specifications:move_filing_set", args=[deadlines[0].pk]) if deadlines else "",
        "due_form": due_form,
        "filers_form": filers_form,
        "remove_form": remove_form,
        "cancel_form": cancel_form,
        "restore_form": restore_form,
        "filing_rows": filing_rows(deadlines, counts),
        "removable": removable,
        "setup_what": what,
    }
    return ctx, None


def is_calendar_post(request) -> bool:
    """Whether a POST to a set-up page is for its calendar forms, not for the set-up's own form."""
    return request.method == "POST" and request.POST.get("action") in ("due", "filers", "cancel", "restore", "remove")

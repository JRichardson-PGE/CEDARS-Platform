from datetime import date

from django import forms
from django.utils import timezone

from cedars.core import uploads
from cedars.services.registry.models import AllocationVersion
from cedars.services.submissions import setup as setup_calendar


def _allocations():
    return AllocationVersion.objects.filter(status=AllocationVersion.Status.CONFIRMED).order_by("-confirmed_at")


MONTHS = [date(2000, m, 1).strftime("%B") for m in range(1, 13)]


def month_field(m: int) -> str:
    return f"due_{m}"


class YearForm(forms.Form):
    """One program year's monthly reports with their thirteen due dates: each month's report, then the 13th-month report, due with
    the year's annual claim."""

    year = forms.IntegerField(label="Program year", min_value=2020, max_value=2100)
    acc_version = forms.ChoiceField(label="ACC the estimates are priced on")
    allocation_version = forms.ModelChoiceField(
        label="Statewide allocation version",
        queryset=AllocationVersion.objects.none(),
        required=False,
        empty_label="The most recent confirmed version for the year",
    )
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, accs=(), **kw):
        super().__init__(*args, **kw)
        self.fields["acc_version"].choices = [(a, f"{a} ACC") for a in accs]
        self.fields["allocation_version"].queryset = _allocations()
        self.fields["year"].initial = timezone.localdate().year + 1
        self.fields["filers"] = setup_calendar.filers_field()  # who files (Jake, 2026-11-09: not every PA files every filing)
        for m, name in enumerate(MONTHS, start=1):
            self.fields[month_field(m)] = forms.DateField(label=f"{name} report: due", widget=forms.DateInput(attrs={"type": "date"}))
        self.fields["closing_due"] = forms.DateField(
            label="13th-month report: due",
            widget=forms.DateInput(attrs={"type": "date"}),
            help_text="The year's final report, generated from the PA's confirmed claims: due with the year's annual claim.",
        )

    def setup_fields(self):
        return [self[f] for f in ("year", "acc_version", "allocation_version", "notes")]

    def due_fields(self):
        return [self[month_field(m)] for m in range(1, 13)] + [self["closing_due"]]

    def clean(self):
        d = super().clean()
        year = d.get("year")
        if not year:
            return d
        for m, name in enumerate(MONTHS, start=1):
            day = d.get(month_field(m))
            ends = date(year + (m == 12), m % 12 + 1, 1)
            if day and day < ends:
                self.add_error(month_field(m), f"{name}'s report is due after {name} {year} ends: on or after {ends:%Y-%m-%d}.")
        if d.get("closing_due") and d["closing_due"] < date(year + 1, 1, 1):
            self.add_error("closing_due", f"The 13th-month report is due after {year} ends: on or after {year + 1}-01-01.")
        return d

    def due(self) -> dict[int, date]:
        return {m: self.cleaned_data[month_field(m)] for m in range(1, 13)}


class SetupForm(forms.Form):
    acc_version = forms.ChoiceField(label="ACC the estimates are priced on")
    allocation_version = forms.ModelChoiceField(
        label="Statewide allocation version",
        queryset=AllocationVersion.objects.none(),
        required=False,
        empty_label="The most recent confirmed version for the year",
    )
    reason = forms.CharField(label="Reason for the change", widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, accs=(), **kw):
        super().__init__(*args, **kw)
        self.fields["acc_version"].choices = [(a, f"{a} ACC") for a in accs]
        self.fields["allocation_version"].queryset = _allocations()


class UploadForm(forms.Form):
    file = uploads.MultipleFileField(
        label="Report files",
        help_text="A .zip of Commitments.csv, Measure.csv and ProgramCost.csv (comma- or pipe-delimited), or an Excel workbook with "
        "those three sheets. To send a report in several files, choose them all at once: .zip, .xlsx or one table's .csv each, a "
        "table split into parts named after it (Measure_1.csv, Measure_2.csv) with the same columns. They are joined into one report.",
        widget=uploads.MultipleFileInput(attrs={"accept": ".zip,.xlsx,.csv,.txt"}),
    )

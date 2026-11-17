from django import forms
from django.utils import timezone

from cedars.core import uploads
from cedars.services.refdata.models import PackVersion
from cedars.services.registry.models import AllocationVersion, FilingCycle
from cedars.services.submissions import setup as setup_calendar

from .models import ADVICE_LETTER, FILING_KINDS


def _cost_test_sets():
    return PackVersion.objects.filter(kind=PackVersion.Kind.COST_TESTS, status=PackVersion.Status.PUBLISHED).order_by(
        "label", "-published_at"
    )


def _allocations():
    return AllocationVersion.objects.filter(status=AllocationVersion.Status.CONFIRMED).order_by("-confirmed_at")


class FilingForm(forms.Form):
    kind = forms.ChoiceField(label="Type of filing", choices=FILING_KINDS)
    name = forms.CharField(
        label="Filing name",
        max_length=120,
        required=False,
        help_text="What the calendar and the filing's pages call it, e.g. 2028-2035 Business plan application, or 2028-2031 "
        "True-up advice letter (TUAL). Needed for an advice letter, to say which one; for an application, blank is its program years "
        "and its type. It can be changed later on the filing calendar.",
    )
    first_year = forms.IntegerField(label="First program year", min_value=2000, max_value=2100)
    last_year = forms.IntegerField(
        label="Last program year",
        min_value=2000,
        max_value=2100,
        help_text="Any number of years: e.g. 2028 to 2031, 2028 to 2035, or the same year as the first for a one-year filing.",
    )
    opens = forms.DateField(label="Opens on", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    due = forms.DateField(label="Due date", widget=forms.DateInput(attrs={"type": "date"}))
    acc_version = forms.ChoiceField(label="ACC it is priced on")
    allocation_version = forms.ModelChoiceField(
        label="Statewide allocation version",
        queryset=AllocationVersion.objects.none(),
        required=False,
        empty_label="The most recent confirmed version for each program year",
    )
    cost_test_set = forms.ModelChoiceField(
        label="Cost-test set",
        queryset=PackVersion.objects.none(),
        required=False,
        empty_label="The published set in force for the program year",
        help_text="The cost tests it is priced on (Reference data, Cost-test sets); only published sets",
    )
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, accs=(), **kw):
        super().__init__(*args, **kw)
        self.fields["acc_version"].choices = [(a, f"{a} ACC") for a in accs]
        self.fields["allocation_version"].queryset = _allocations()
        self.fields["cost_test_set"].queryset = _cost_test_sets()
        notes = self.fields.pop("notes")
        version = setup_calendar.specification_field("budgets")
        if version is not None:
            self.fields["specification_version"] = version
        self.fields["filers"] = setup_calendar.filers_field()  # who files it (Jake, 2026-11-09: not every PA files every filing)
        self.fields["notes"] = notes
        self.fields["first_year"].initial = timezone.localdate().year + 1
        self.fields["last_year"].initial = timezone.localdate().year + 4

    def clean(self):
        d = super().clean()
        first, last = d.get("first_year"), d.get("last_year")
        if first and last and last < first:
            self.add_error("last_year", "The last program year is the same as the first or after it.")
        if d.get("kind") == ADVICE_LETTER and not (d.get("name") or "").strip():
            self.add_error("name", "Name the advice letter, e.g. 2028-2031 True-up advice letter (TUAL).")
        return d


class SetupForm(forms.Form):
    acc_version = forms.ChoiceField(label="ACC it is priced on")
    allocation_version = forms.ModelChoiceField(
        label="Statewide allocation version",
        queryset=AllocationVersion.objects.none(),
        required=False,
        empty_label="The most recent confirmed version for each program year",
    )
    cost_test_set = forms.ModelChoiceField(
        label="Cost-test set",
        queryset=PackVersion.objects.none(),
        required=False,
        empty_label="The published set in force for the program year",
        help_text="The cost tests it is priced on (Reference data, Cost-test sets); only published sets",
    )
    notes = forms.CharField(
        label="Notes",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Administrators' notes on this set-up, kept with it; a change is recorded with the reason below",
    )
    reason = forms.CharField(label="Reason for the change", widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, accs=(), **kw):
        super().__init__(*args, **kw)
        self.fields["acc_version"].choices = [(a, f"{a} ACC") for a in accs]
        self.fields["allocation_version"].queryset = _allocations()
        self.fields["cost_test_set"].queryset = _cost_test_sets()


class UploadForm(forms.Form):
    file = uploads.MultipleFileField(
        label="Filing files",
        help_text="A .zip of Measure.csv and ProgramCost.csv (comma- or pipe-delimited), or an Excel workbook with Measure and "
        "ProgramCost sheets. To send a filing in several files, choose them all at once: .zip, .xlsx or one table's .csv each, a table "
        "split into parts named after it (Measure_1.csv, Measure_2.csv) with the same columns. They are joined into one filing.",
        widget=uploads.MultipleFileInput(attrs={"accept": ".zip,.xlsx,.csv,.txt"}),
    )


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", widget=forms.Textarea(attrs={"rows": 2}))


class ShiftForm(forms.Form):
    cycle = forms.ModelChoiceField(label="Filing cycle", queryset=FilingCycle.objects.none(), empty_label=None)
    title = forms.CharField(label="Title", max_length=200, help_text="A short name, e.g. Move 2029 marketing to residential HVAC")
    reason = forms.CharField(label="Reason", widget=forms.Textarea(attrs={"rows": 3}), help_text="Why the money moves")

    def __init__(self, *args, cycles=None, **kw):
        super().__init__(*args, **kw)
        self.fields["cycle"].queryset = cycles if cycles is not None else FilingCycle.objects.none()

from django import forms
from django.utils import timezone

from cedars.core import uploads
from cedars.services.refdata.models import PackVersion
from cedars.services.registry.models import AllocationVersion, FilingCycle

from .models import FILING_KINDS


def _cost_test_sets():
    return PackVersion.objects.filter(kind=PackVersion.Kind.COST_TESTS, status=PackVersion.Status.PUBLISHED).order_by(
        "label", "-published_at"
    )


def _allocations():
    return AllocationVersion.objects.filter(status=AllocationVersion.Status.CONFIRMED).order_by("-confirmed_at")


class FilingForm(forms.Form):
    first_year = forms.IntegerField(
        label="First year of the cycle", min_value=2020, max_value=2100, help_text="A four-year cycle, e.g. 2028 for 2028-2031"
    )
    kind = forms.ChoiceField(label="Filing", choices=FILING_KINDS)
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
        self.fields["first_year"].initial = timezone.localdate().year + 1


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

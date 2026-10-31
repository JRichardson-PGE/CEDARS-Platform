from django import forms
from django.utils import timezone

from cedars.core import uploads
from cedars.services.registry.models import AllocationVersion


def _allocations():
    return AllocationVersion.objects.filter(status=AllocationVersion.Status.CONFIRMED).order_by("-confirmed_at")


class YearForm(forms.Form):
    year = forms.IntegerField(label="Program year", min_value=2020, max_value=2100)
    due_day = forms.IntegerField(
        label="Due on day", min_value=1, max_value=31, help_text="Each month's report is due on this day of the following month"
    )
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

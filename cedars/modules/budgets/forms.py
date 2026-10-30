from django import forms
from django.utils import timezone

from cedars.services.registry.models import AllocationVersion

from .models import FILING_KINDS


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
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, accs=(), **kw):
        super().__init__(*args, **kw)
        self.fields["acc_version"].choices = [(a, f"{a} ACC") for a in accs]
        self.fields["allocation_version"].queryset = _allocations()
        self.fields["first_year"].initial = timezone.localdate().year + 1


class SetupForm(forms.Form):
    acc_version = forms.ChoiceField(label="ACC it is priced on")
    allocation_version = forms.ModelChoiceField(
        label="Statewide allocation version",
        queryset=AllocationVersion.objects.none(),
        required=False,
        empty_label="The most recent confirmed version for each program year",
    )
    reason = forms.CharField(label="Reason for the change", widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, accs=(), **kw):
        super().__init__(*args, **kw)
        self.fields["acc_version"].choices = [(a, f"{a} ACC") for a in accs]
        self.fields["allocation_version"].queryset = _allocations()


class UploadForm(forms.Form):
    file = forms.FileField(
        label="Filing file",
        help_text="A .zip of Measure.csv and ProgramCost.csv (comma- or pipe-delimited), or an Excel workbook with Measure and "
        "ProgramCost sheets.",
        widget=forms.ClearableFileInput(attrs={"accept": ".zip,.xlsx"}),
    )


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", widget=forms.Textarea(attrs={"rows": 2}))

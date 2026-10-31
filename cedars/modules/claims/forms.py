from django import forms
from django.utils import timezone

from cedars.core import uploads
from cedars.services.refdata.models import PackVersion
from cedars.services.registry.models import AllocationVersion

from .service import PERIODS


def _cost_test_sets():
    return PackVersion.objects.filter(kind=PackVersion.Kind.COST_TESTS, status=PackVersion.Status.PUBLISHED).order_by(
        "label", "-published_at"
    )


def _allocations():
    return AllocationVersion.objects.filter(status=AllocationVersion.Status.CONFIRMED).order_by("-confirmed_at")


def due_field(kind: str) -> str:
    return "due_" + kind.split()[0].lower()


class YearForm(forms.Form):
    year = forms.IntegerField(label="Claim year", min_value=2020, max_value=2100)
    acc_version = forms.ChoiceField(label="ACC its claims are priced on", help_text="Claims use the ACC of the year's final budget filing")
    allocation_version = forms.ModelChoiceField(
        label="Statewide allocation version",
        queryset=AllocationVersion.objects.none(),
        required=False,
        empty_label="The most recent confirmed version with factors for the year",
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
        self.fields["year"].initial = timezone.localdate().year + 1
        for kind, _ in PERIODS:
            self.fields[due_field(kind)] = forms.DateField(label=f"{kind}: due", widget=forms.DateInput(attrs={"type": "date"}))

    def due_fields(self):
        return [self[due_field(kind)] for kind, _ in PERIODS]


class SetupForm(forms.Form):
    acc_version = forms.ChoiceField(label="ACC its claims are priced on")
    allocation_version = forms.ModelChoiceField(
        label="Statewide allocation version",
        queryset=AllocationVersion.objects.none(),
        required=False,
        empty_label="The most recent confirmed version with factors for the year",
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
        label="Claims files",
        help_text="A .zip of the claims tables as CSV files named after them (Claim.csv, Measure.csv, ProgramCost.csv, Site.csv, "
        "ContactClaim.csv, Participant.csv, InfrastructureCost.csv; comma- or pipe-delimited), or an Excel workbook with a sheet per "
        "table. Include the records that are new or changed since the last confirmed quarter. To send them in several files, choose "
        "them all at once: .zip, .xlsx or one table's .csv each, a table split into parts named after it (Measure_1.csv, "
        "Measure_2.csv) with the same columns. They are joined into one upload.",
        widget=uploads.MultipleFileInput(attrs={"accept": ".zip,.xlsx,.csv,.txt"}),
    )


class PIIForm(forms.Form):
    file = forms.FileField(
        label="PII file",
        help_text="A .zip of PII_Contact.csv and PII_Site.csv (comma- or pipe-delimited), or an Excel workbook with a sheet for each. "
        "It may name only the ContactIDs and SiteIDs of these claims.",
        widget=forms.ClearableFileInput(attrs={"accept": ".zip,.xlsx"}),
    )

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


class RestateForm(forms.Form):
    """A restatement of confirmed claims (BR-06.17): who it is for, the release and ACC, and optionally an ACC per claim."""

    organisation = forms.ChoiceField(
        label="Run for", required=False, help_text="The runs and the restatement belong to this organization, as any run does"
    )
    release = forms.ChoiceField(label="ReCET release", help_text="The default release is the current engine")
    acc_version = forms.ChoiceField(label="ACC", help_text="Every record is priced on this ACC unless the file below names another for it")
    overrides = forms.FileField(
        label="ACC by claim (optional)",
        required=False,
        help_text="A CSV with a ClaimID column and an ACC column (comma- or pipe-delimited): each claim it names is priced on its own ACC "
        "(BR-06.5). Every ClaimID must be one of these claims.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.txt"}),
    )
    name = forms.CharField(label="Name", required=False, max_length=200, help_text="Blank: the claims, the release and the ACC")
    reason = forms.CharField(
        label="Why restate (optional)", required=False, widget=forms.Textarea(attrs={"rows": 2}), help_text="Kept with the restatement"
    )

    def __init__(self, *args, organisations=(), options=(), filed_acc="", **kw):
        super().__init__(*args, **kw)
        self.fields["organisation"].choices = [(o.code, f"{o.name} ({o.code})") for o in organisations] + [("", "My own analysis")]
        self.fields["release"].choices = [
            (o["release"], f"ReCET {o['release']}" + (" (default, the current engine)" if o["default"] else "")) for o in options
        ]
        accs = sorted({a for o in options for a in o["accs"]})
        self.fields["acc_version"].choices = [(a, f"{a} ACC" + (" (as filed)" if a == filed_acc else "")) for a in accs]
        if not self.is_bound:
            default = next((o for o in options if o["default"]), options[0] if options else None)
            self.initial.setdefault("release", default["release"] if default else "")
            self.initial.setdefault("acc_version", filed_acc)
            self.initial.setdefault("organisation", organisations[0].code if organisations else "")

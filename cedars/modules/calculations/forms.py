from django import forms

from cedars.services.refdata.models import PackVersion
from cedars.services.registry.models import AllocationVersion

from . import output_templates, service
from .models import OutputTemplate

ACC_PURPOSES = [
    ("", "Any filing"),
    ("claims", "Claims"),
]


class NewRunForm(forms.Form):
    """New run (BR-06.1, 06.4, 06.5, 06.7): the job zip and the settings a person picks. The API takes the same fields."""

    name = forms.CharField(label="Run name", max_length=200, help_text="e.g. 2028 claims Q1, PG&E")
    run_for = forms.ChoiceField(label="Run for", help_text="An organization's runs are seen by its members and stay with it")
    job = forms.FileField(
        label="Job file (.zip)", help_text="Measure.csv and, optionally, ProgramCost.csv, zipped together, as CET takes a job"
    )
    engine = forms.ChoiceField(label="Calculation engine", help_text="ReCET's own, or an exact replica of a past CET release (D13)")
    acc_version = forms.ChoiceField(label="ACC")
    acc_purpose = forms.ChoiceField(
        label="Priced for",
        required=False,
        choices=ACC_PURPOSES,
        help_text="Records on an ACC that does not apply to their program year are flagged",
    )
    allocate_statewide = forms.BooleanField(label="Allocate statewide programs to the IOUs", required=False)
    allocation_version = forms.ModelChoiceField(
        label="Allocation version",
        required=False,
        queryset=AllocationVersion.objects.filter(status=AllocationVersion.Status.CONFIRMED),
        empty_label="The most recent for the job's program year",
    )
    cost_test_set = forms.ModelChoiceField(
        label="Cost-test set",
        required=False,
        queryset=PackVersion.objects.filter(kind=PackVersion.Kind.COST_TESTS).exclude(status=PackVersion.Status.WITHDRAWN),
        empty_label="The set in force for the job's first program year",
        help_text="The cost tests it is priced on (ReCET's own calculation). A draft set is for analysis runs only",
    )
    filing_validation = forms.ChoiceField(label="CEDARS filing checks", required=False, choices=list(service.FILING_CHECKS.items()))
    filing_year = forms.IntegerField(label="Filing year (optional)", required=False, min_value=2000, max_value=2100)
    include_societal = forms.BooleanField(label="Societal cost test (SCB and SCH)", required=False, initial=True)
    emissions = forms.BooleanField(label="Emissions", required=False, initial=True)
    output_format = forms.ChoiceField(label="Output files", choices=list(service.OUTPUT_FORMATS.items()), initial="csv.zip")
    cet_outputs = forms.FileField(
        label="CET outputs to compare with (.csv, optional)",
        required=False,
        help_text="A parallel run: CET's outputs CSV for this job, compared measure by measure on CET_ID (ReCET's own calculation only)",
    )
    output_template = forms.ModelChoiceField(
        label="Output template",
        required=False,
        queryset=OutputTemplate.objects.none(),
        empty_label="All columns",
        help_text="The measure-level file's columns, in order; the Results tables still use every column",
    )

    def __init__(self, *args, releases=(), organisations=(), user=None, draft=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.draft = draft
        if draft is not None:
            del self.fields["job"]  # the draft's tables are the job
        if user is not None:
            self.fields["output_template"].queryset = output_templates.visible(user)
            self.fields["output_template"].initial = output_templates.default_for(user)
        self.organisations = {o.code: o for o in organisations}
        self.fields["run_for"].choices = [(o.code, o.code) for o in organisations] + [("", "Only me")]
        engines, accs = [], set()
        for r in releases:
            for calc in r.calculations:
                engines.append((f"{r.release}|{calc}", f"{service.calculation_label(calc)} (ReCET {r.release})"))
                accs.update(service.accs_for(r, calc))
        self.fields["engine"].choices = engines
        self.fields["acc_version"].choices = [(a, f"{a} ACC") for a in sorted(accs, reverse=True)]
        self.releases = {r.release: r for r in releases}

    def clean(self):
        data = super().clean()
        engine = data.get("engine", "")
        release, _, calc = engine.partition("|")
        data["release"] = self.releases.get(release)
        data["calculation"] = calc
        data["organisation"] = self.organisations.get(data.get("run_for") or "")
        return data

    def run_request(self) -> service.RunRequest:
        d = self.cleaned_data
        st = {
            "acc_purpose": d.get("acc_purpose") or None,
            "allocate_statewide": d.get("allocate_statewide") or None,
            "filing_validation": d.get("filing_validation") or None,
            "filing_year": d.get("filing_year"),
            "include_societal": bool(d.get("include_societal")),
            "emissions": bool(d.get("emissions")),
            "preflight_only": bool(self.data.get("preflight_only")) or None,  # the "Check the inputs only" button
            **output_templates.settings_for(d.get("output_template")),
        }
        return service.RunRequest(
            name=d["name"],
            release=d["release"],
            calculation=d["calculation"],
            acc_version=d["acc_version"],
            output_format=d["output_format"],
            settings=st,
            allocation_version=d.get("allocation_version"),
            cet_outputs=d.get("cet_outputs"),
            cost_test_set=d.get("cost_test_set"),
        )


class OutputTemplateForm(forms.Form):
    """An output template: its name and its columns in file order (text, one per line), plus columns ticked from the catalog, which are
    added at the end. Works without scripts."""

    name = forms.CharField(label="Name", max_length=output_templates.MAX_NAME)
    columns = forms.CharField(
        label="Columns, in file order",
        required=False,
        widget=forms.Textarea(attrs={"rows": 14, "spellcheck": "false"}),
        help_text="One per line (or separated by commas). Reorder by moving lines; CET names are taken as the field's name.",
    )
    add = forms.MultipleChoiceField(label="Add columns", required=False, widget=forms.CheckboxSelectMultiple)
    shared = forms.BooleanField(label="Offer it to everyone", required=False)
    make_default = forms.BooleanField(label="Start my new runs with this template", required=False)

    def __init__(self, *args, catalog=None, can_share=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.catalog = catalog or output_templates.Catalog()
        self.fields["add"].choices = [(f.name, f.name) for _g, fields in self.catalog.groups for f in fields]
        if not can_share:
            del self.fields["shared"]

    def picked(self) -> list[str]:
        """The columns as saved: the text's, then the ticked ones not already in it."""
        d = self.cleaned_data
        return output_templates.parse(d.get("columns", "")) + list(d.get("add") or [])


class EngineReleaseForm(forms.Form):
    release = forms.CharField(label="ReCET release", max_length=40, help_text="e.g. 0.1.78")
    calculations = forms.CharField(
        label="Calculations and their ACCs",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="One per line: recet: 2021, 2022, 2024, 2026 and, for each CET replica, cet-25.1: 2024, 2026",
    )
    image = forms.CharField(label="Worker image (optional)", max_length=300, required=False)
    batch_job_definition = forms.CharField(label="AWS Batch job definition (optional)", max_length=200, required=False)
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    default = forms.BooleanField(label="Make it the default for new runs", required=False)

    def clean_calculations(self):
        out = {}
        for line in self.cleaned_data["calculations"].splitlines():
            if not line.strip():
                continue
            calc, sep, accs = line.partition(":")
            years = [a.strip() for a in accs.split(",") if a.strip()]
            if not sep or not years:
                raise forms.ValidationError(f"Write each line as calculation: ACC, ACC (got {line.strip()!r}).")
            out[calc.strip()] = years
        if not out:
            raise forms.ValidationError("Name at least ReCET's own calculation and its ACCs.")
        return out

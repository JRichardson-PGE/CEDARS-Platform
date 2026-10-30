from django import forms

from cedars.services.registry.models import AllocationVersion

from . import service

ACC_PURPOSES = [
    ("", "Any filing"),
    ("claims", "Claims"),
]


class NewRunForm(forms.Form):
    """New run (BR-06.1, 06.4, 06.5, 06.7): the job zip and the settings a person picks. The API takes the same fields."""

    name = forms.CharField(label="Run name", max_length=200, help_text="e.g. 2028 claims Q1, PG&E")
    run_for = forms.ChoiceField(label="Run for", help_text="An organisation's runs are seen by its members and stay with it")
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
    filing_validation = forms.ChoiceField(label="CEDARS filing checks", required=False, choices=list(service.FILING_CHECKS.items()))
    filing_year = forms.IntegerField(label="Filing year (optional)", required=False, min_value=2000, max_value=2100)
    include_societal = forms.BooleanField(label="Societal cost test (SCB and SCH)", required=False, initial=True)
    emissions = forms.BooleanField(label="Emissions", required=False, initial=True)
    output_format = forms.ChoiceField(label="Output files", choices=list(service.OUTPUT_FORMATS.items()), initial="csv.zip")

    def __init__(self, *args, releases=(), organisations=(), **kwargs):
        super().__init__(*args, **kwargs)
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
        }
        return service.RunRequest(
            name=d["name"],
            release=d["release"],
            calculation=d["calculation"],
            acc_version=d["acc_version"],
            output_format=d["output_format"],
            settings=st,
            allocation_version=d.get("allocation_version"),
        )


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

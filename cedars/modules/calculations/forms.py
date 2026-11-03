from django import forms

from cedars.core import uploads
from cedars.services.refdata.models import PackVersion
from cedars.services.registry.models import AllocationVersion

from . import output_templates, service
from .models import OutputTemplate

ACC_PURPOSES = [
    ("", "Any filing"),
    ("claims", "Claims"),
]
MARKET_EFFECTS = [("0", "0%"), ("0.05", "5% (benefits and costs)")]


def parse_years(text: str) -> list[int]:
    """'2028, 2029' or '2028 2029' as [2028, 2029]; raises ValidationError on anything else."""
    out = []
    for part in (text or "").replace(",", " ").split():
        if not part.isdigit() or not 2000 <= int(part) <= 2100:
            raise forms.ValidationError(f"Write the years as four-digit numbers, e.g. 2028, 2029 (got {part!r}).")
        out.append(int(part))
    return sorted(set(out))


# ReCET's optional input files New run takes as uploads of their own (jobfiles.OPTIONAL). Program metadata and statewide factors are not
# offered: the program registry and the allocation version are every run's (a job zip's or workbook's own are noted as not used).
SUPPLIED_FIELDS = ("run_parameters", "profiles_8760", "profiles_gas")


class NewRunForm(forms.Form):
    """New run (BR-06.1, 06.4, 06.5, 06.7): the job zip and the settings a person picks. The API takes the same fields."""

    name = forms.CharField(
        label="Run name",
        max_length=200,
        required=False,
        help_text="e.g. 2028 claims Q1, PG&E. Left blank, the run is named after the job file and its ACC",
    )
    run_for = forms.ChoiceField(label="Run for", help_text="An organization's runs are seen by its members and stay with it")
    job = forms.FileField(
        label="Job file (.zip or .xlsx)",
        help_text=uploads.limit_text(
            "Measure.csv and, optionally, ProgramCost.csv, zipped together, as CET takes a job, or ReCET's job workbook", False
        ),
        validators=[uploads.check_size],
    )
    engine = forms.ChoiceField(label="Calculation engine", help_text="ReCET's own, or an exact replica of a past CET release (D13)")
    acc_version = forms.ChoiceField(label="ACC")
    compare_with = forms.ChoiceField(
        label="Compare with another ACC (optional)",
        required=False,
        help_text="Prices the job on this ACC too and writes the per-measure comparison; Results has a tab for each ACC",
    )
    base_year = forms.IntegerField(
        label="First Year (optional)",
        required=False,
        min_value=2000,
        max_value=2100,
        help_text="The year benefits and costs are discounted to; blank is the earliest claim year in the job",
    )
    market_effects = forms.TypedChoiceField(
        label="Market effects",
        choices=MARKET_EFFECTS,
        coerce=float,
        required=False,
        empty_value=None,
        initial="0",
        help_text="CET's market effects, on benefits and costs",
    )
    acc_purpose = forms.ChoiceField(
        label="Priced for",
        required=False,
        choices=ACC_PURPOSES,
        help_text="Records on an ACC that does not apply to their program year are flagged, not stopped",
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
    cost_tests_file = forms.FileField(
        label="Your own cost-test set (.yaml, optional)",
        required=False,
        help_text="A cost-test set file of your own, in ReCET's format, for an analysis run only (not with filing checks, Priced for or a "
        "registered set above). The run records the file's SHA-256",
        validators=[uploads.check_size],
    )
    filing_validation = forms.ChoiceField(label="CEDARS filing checks", required=False, choices=list(service.FILING_CHECKS.items()))
    filing_year = forms.IntegerField(label="Filing year (optional)", required=False, min_value=2000, max_value=2100)
    include_societal = forms.BooleanField(label="Societal cost test (SCB and SCH)", required=False, initial=True)
    components = forms.BooleanField(
        label="Price each ACC component separately",
        required=False,
        help_text="Adds each component's value per measure and a components summary (ReCET's own calculation)",
    )
    emissions = forms.BooleanField(label="Emissions", required=False, initial=True)
    apply_exclude_from_ce = forms.BooleanField(
        label="Leave Exclude_From_CE programs out of the portfolio summary",
        required=False,
        help_text="They are still priced and in the measure-level file",
    )
    portfolio_8760 = forms.BooleanField(
        label="Combined 8,760 electric load profile",
        required=False,
        help_text="The portfolio's hourly net kWh by PA and climate zone, with its peak hours (ReCET's own calculation)",
    )
    profile_years = forms.CharField(
        label="Years of the combined profile (optional)",
        required=False,
        max_length=60,
        help_text="e.g. 2028, 2029; blank is the First Year",
    )
    output_format = forms.ChoiceField(label="Output files", choices=list(service.OUTPUT_FORMATS.items()), initial="csv.zip")
    cet_outputs = forms.FileField(
        label="CET outputs to compare with (.csv, optional)",
        required=False,
        help_text="A parallel run: CET's outputs CSV for this job, compared measure by measure on CET_ID (ReCET's own calculation only)",
    )
    run_parameters = forms.FileField(
        label="Run parameters (optional)",
        required=False,
        help_text="Discount rates, base year and gas CO2 for the PAs it lists (.csv, .parquet or .xlsx; ReCET's own calculation)",
        validators=[uploads.check_size],
    )
    profiles_8760 = forms.FileField(
        label="8,760 electric profiles (optional)",
        required=False,
        help_text="Hourly profiles the measures name, in ReCET's long format (.csv, .parquet or .xlsx; ReCET's own calculation)",
        validators=[uploads.check_size],
    )
    profiles_gas = forms.FileField(
        label="Gas monthly profiles (optional)",
        required=False,
        help_text="Monthly gas profiles the measures name (.csv, .parquet or .xlsx; ReCET's own calculation)",
        validators=[uploads.check_size],
    )
    output_template = forms.ModelChoiceField(
        label="Output template",
        required=False,
        queryset=OutputTemplate.objects.none(),
        empty_label="All columns",
        help_text="The measure-level file's columns, in order; the Results tables still use every column",
    )

    def __init__(self, *args, releases=(), organisations=(), user=None, draft=None, rerun=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.draft = draft
        self.rerun = rerun
        self.kept = []  # a rerun's stored optional inputs offered again: [(name, label, keep field name)]
        if draft is not None:
            del self.fields["job"]  # the draft's tables are the job
        if rerun is not None:  # "Rerun with different settings": the run's own inputs unless others are uploaded
            self.fields["job"].required = False
            self.fields["job"].label = "Another job file (optional)"
            self.fields["job"].help_text = "Leave it empty to run the same job file again"
            for name, label in service.reusable_inputs(rerun):
                field = f"keep_{name}"
                # lower the first letter of a word ("Run parameters"), never of an acronym ("CET outputs" stayed "cET outputs")
                lead = label if label[1:2].isupper() else label[:1].lower() + label[1:]
                self.fields[field] = forms.BooleanField(label=f"Use the run's {lead} again", required=False, initial=True)
                self.kept.append((name, label, field))
        if user is not None:
            self.fields["output_template"].queryset = output_templates.visible(user)
            self.fields["output_template"].initial = output_templates.default_for(user)
        self.organisations = {o.code: o for o in organisations}
        self.fields["run_for"].choices = [(o.code, o.code) for o in organisations] + [("", "Only me")]
        self.fields["run_for"].required = False  # "Only me" is the blank value
        engines, accs = [], set()
        for r in releases:
            for calc in r.calculations:
                engines.append((f"{r.release}|{calc}", f"{service.calculation_label(calc)} (ReCET {r.release})"))
                accs.update(service.accs_for(r, calc))
        self.fields["engine"].choices = engines
        self.fields["acc_version"].choices = [(a, f"{a} ACC") for a in sorted(accs, reverse=True)]
        recet_accs = sorted({a for r in releases for a in service.accs_for(r, service.RECET)}, reverse=True)
        self.fields["compare_with"].choices = [("", "No comparison")] + [(a, f"{a} ACC") for a in recet_accs]
        self.fields["acc_purpose"].choices = service.acc_purposes()
        self.releases = {r.release: r for r in releases}

    def clean(self):
        data = super().clean()
        engine = data.get("engine", "")
        release, _, calc = engine.partition("|")
        data["release"] = self.releases.get(release)
        data["calculation"] = calc
        data["organisation"] = self.organisations.get(data.get("run_for") or "")
        if data.get("compare_with") and data.get("compare_with") == data.get("acc_version"):
            self.add_error("compare_with", "Choose an ACC other than the run's own, or no comparison.")
        try:
            data["profile_years"] = parse_years(data.get("profile_years", ""))
        except forms.ValidationError as e:
            self.add_error("profile_years", e)
        if data.get("profile_years") and not data.get("portfolio_8760"):
            self.add_error("profile_years", "Tick Combined 8,760 electric load profile to choose its years.")
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
            "compare_with": d.get("compare_with") or None,
            "base_year": d.get("base_year"),
            "market_effects": d.get("market_effects") or None,  # 0% is the engine's default: not sent
            "components": d.get("components") or None,
            "apply_exclude_from_ce": d.get("apply_exclude_from_ce") or None,
            "portfolio_8760": d.get("portfolio_8760") or None,
            "profile_years": d.get("profile_years") or None,
            "preflight_only": bool(self.data.get("preflight_only")) or None,  # the "Check the inputs only" button
            **output_templates.settings_for(d.get("output_template")),
        }
        return service.RunRequest(
            name=d.get("name") or "",
            release=d["release"],
            calculation=d["calculation"],
            acc_version=d["acc_version"],
            output_format=d["output_format"],
            settings=st,
            allocation_version=d.get("allocation_version"),
            cet_outputs=d.get("cet_outputs"),
            cost_test_set=d.get("cost_test_set"),
            cost_tests_file=d.get("cost_tests_file"),
            supplied={n: d.get(n) for n in SUPPLIED_FIELDS if d.get(n)},
        )

    def reused(self) -> list[str]:
        """The rerun's stored inputs to use again: its job file unless another is uploaded, and each optional input kept (ticked) and
        not replaced by an upload."""
        if self.rerun is None:
            return []
        d = self.cleaned_data
        out = [] if d.get("job") else ["job_zip", "workbook"]
        return out + [n for n, _, field in self.kept if d.get(field) and not d.get(n)]


def rerun_initial(run) -> dict:
    """New run's fields as `run` had them, for "Rerun with different settings" (the run's name marked as a rerun)."""
    st = run.settings or {}
    initial = {
        "name": f"{run.name} (rerun)"[:200],
        "run_for": run.organisation.code if run.organisation else "",
        "engine": f"{run.engine_release}|{run.calculation}",
        "acc_version": run.acc_version,
        "output_format": run.output_format,
        "include_societal": st.get("include_societal", True),
        "emissions": st.get("emissions", True),
        "allocation_version": run.allocation_version_id,
    }
    for k in ("acc_purpose", "allocate_statewide", "filing_validation", "filing_year"):
        if st.get(k) is not None:
            initial[k] = st[k]
    return initial


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


CALCULATIONS_HELP = (
    "One per line, the calculation and then its ACCs: recet: 2021, 2022, 2024, 2026 for ReCET's own and, for each CET replica, "
    "cet-25.1: 2024. The names the table shows (ReCET, CET 25.1 replica) work too."
)


def parse_calculations(text: str) -> dict[str, list[str]]:
    """{"recet": ["2024", "2026"], "cet-25.1": ["2024"]} from the Calculations box: one "calculation: ACC, ACC" per line, the calculation
    by its code or the name the releases table shows it by."""
    out: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        name, sep, accs = line.partition(":")
        years = [a.strip() for a in accs.replace(";", ",").split(",") if a.strip()]
        if not sep or not years:
            raise forms.ValidationError(f"Write each line as calculation: ACC, ACC, e.g. recet: 2024, 2026 (got {line.strip()!r}).")
        code = service.calculation_code(name)
        if not code:
            raise forms.ValidationError(
                f"{name.strip()!r} is not a calculation. Use recet (ReCET's own) or cet-<release> for a CET replica, e.g. cet-25.1."
            )
        bad = [y for y in years if not (y.isdigit() and len(y) == 4)]
        if bad:
            raise forms.ValidationError(f"Name each ACC by its year, e.g. 2024 (got {', '.join(bad)} for {code}).")
        if code in out:
            raise forms.ValidationError(f"{code} is on two lines; put its ACCs on one.")
        out[code] = years
    if not out:
        raise forms.ValidationError("Name at least ReCET's own calculation and its ACCs, e.g. recet: 2024, 2026.")
    if service.RECET not in out:
        raise forms.ValidationError('Add ReCET\'s own calculation: a line "recet: " followed by its ACCs, e.g. recet: 2024, 2026.')
    return out


class EngineReleaseChangeForm(forms.Form):
    """What a release offers and how it is run; its number never changes (its runs name it)."""

    calculations = forms.CharField(
        label="Calculations and their ACCs", widget=forms.Textarea(attrs={"rows": 9}), help_text=CALCULATIONS_HELP
    )
    image = forms.CharField(label="Worker image (optional)", max_length=300, required=False)
    batch_job_definition = forms.CharField(label="AWS Batch job definition (optional)", max_length=200, required=False)
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    reason = forms.CharField(
        label="Why it is changed",
        max_length=300,
        help_text="Kept in the audit trail, e.g. the data pack that added an ACC.",
    )

    def clean_calculations(self):
        return parse_calculations(self.cleaned_data["calculations"])


class EngineReleaseForm(EngineReleaseChangeForm):
    release = forms.CharField(label="ReCET release", max_length=40, help_text="The version number only, e.g. 0.1.90")
    default = forms.BooleanField(label="Make it the default for new runs", required=False)
    retire_previous = forms.BooleanField(label="Retire the release it replaces as the default", required=False)
    reason = forms.CharField(
        label="Why it is added",
        max_length=300,
        help_text="Kept in the audit trail, e.g. the release's change or the decision that calls for it.",
    )
    field_order = ["release", "calculations", "default", "retire_previous", "image", "batch_job_definition", "notes", "reason"]

    def __init__(self, *args, current_default=None, **kwargs):
        # a new release usually offers what the default does: start from its calculations and ACCs
        kwargs.setdefault("initial", {"calculations": service.calculations_text(current_default)})
        super().__init__(*args, **kwargs)
        if current_default is None:
            del self.fields["retire_previous"]
        else:
            self.fields["retire_previous"].label = f"Retire {current_default}, the current default (its runs keep naming it)"
            self.fields["retire_previous"].help_text = "Only when this release is made the default."

    def clean_release(self):
        number = service.release_number(self.cleaned_data["release"])
        if not number:
            raise forms.ValidationError(f"Name the release by its version number, e.g. 0.1.90 (got {self.cleaned_data['release']!r}).")
        return number

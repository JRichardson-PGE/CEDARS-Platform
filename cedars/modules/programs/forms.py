from datetime import timedelta

from django import forms
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from cedars.services.registry import field_guide
from cedars.services.registry.importers import parse_sectors

STATE_CHOICES = [("", "Open and closed"), ("open", "Open"), ("closed", "Closed")]
SEGMENT_CHOICES = [
    ("", "Any segment"),
    ("resource_acquisition", "Resource acquisition"),
    ("market_support", "Market support"),
    ("equity", "Equity"),
    ("codes_and_standards", "Codes and standards"),
    ("evaluation_measurement_verification", "EM&V"),
]
STATUS_CHOICES = [
    ("", "Not given"),
    ("new", "New"),
    ("active", "Active"),
    ("closed_to_new_commitments", "Closed to new commitments"),
    ("closure_pending", "Closure pending"),
    ("closed", "Closed"),
]


class CatalogFilter(forms.Form):
    q = forms.CharField(label="Name or PrgID", required=False, widget=forms.TextInput(attrs={"type": "search"}))
    state = forms.ChoiceField(label="Open or closed", required=False, choices=STATE_CHOICES)
    segment = forms.ChoiceField(label="Segment", required=False, choices=SEGMENT_CHOICES)
    # a choice of the registry's years, "Today" first (UI review 2, #32); a year outside them that a link names is still offered
    year = forms.TypedChoiceField(label="Program year", required=False, coerce=int, empty_value=None)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        asked = str(self.data.get("year", "")).strip() if self.data else ""
        chosen = int(asked) if asked.isdigit() and 1990 <= int(asked) <= 2100 else None
        self.fields["year"].choices = [("", "Today")] + [(y, str(y)) for y in registry_years(chosen)]


def registry_years(chosen: int | None = None) -> list[int]:
    """The program years the registry covers, newest first: from its first program's start through the last filing cycle on record
    (or four years ahead), plus `chosen`."""
    from cedars.services.registry.models import FilingCycle, ProgramVersion

    today = timezone.localdate().year
    starts = [y for y in ProgramVersion.objects.values_list("start_year", flat=True).distinct() if y]
    starts += [d.year for d in ProgramVersion.objects.values_list("valid_from", flat=True).distinct() if d]
    first = min(starts) if starts else today - 10
    last_cycle = FilingCycle.objects.order_by("-last_year").values_list("last_year", flat=True).first()
    last = max(today + 4, last_cycle or 0)
    years = set(range(max(first, last - 40), last + 1)) | ({chosen} if chosen else set())
    return sorted(years, reverse=True)


class ProgramChangeForm(forms.Form):
    """The attributes a program's administrator changes, pre-filled from the latest version; only the ones that differ are saved."""

    effective = forms.DateField(label="Effective from", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(
        label="Reason", max_length=1000, widget=forms.Textarea(attrs={"rows": 2}), help_text="e.g. the resolution or advice letter"
    )
    name = forms.CharField(label="Program name", max_length=300)
    commercial_name = forms.CharField(label="Commercial name", max_length=300, required=False)
    # the listed fields offer the program fields guide's values (registry/program_fields.yaml), set in __init__
    status = forms.ChoiceField(label="Status", required=False)
    segment = forms.ChoiceField(label="Segment", required=False)
    primary_sector = forms.ChoiceField(label="Primary sector", required=False)
    sectors = forms.CharField(label="Budget share by sector", required=False)
    implementer = forms.ChoiceField(label="Implementer", required=False)
    category = forms.ChoiceField(label="Category", required=False)
    implementation_contractor = forms.CharField(label="Implementation contractor", max_length=300, required=False)
    start_year = forms.IntegerField(label="Start year", min_value=1990, max_value=2100, required=False)
    end_year = forms.IntegerField(label="End year", min_value=1990, max_value=2100, required=False)
    statewide_program = forms.CharField(label="Statewide program it delivers", max_length=200, required=False)
    parent_program = forms.CharField(label="Parent program", max_length=100, required=False)
    website = forms.URLField(label="Website", required=False, assume_scheme="https")
    exclude_from_budget = forms.BooleanField(label="Exclude from budget", required=False)
    exclude_from_ce = forms.BooleanField(label="Exclude from cost-effectiveness", required=False)
    exclude_from_statewide = forms.BooleanField(label="Exclude from statewide", required=False)
    # how the program is delivered: one box per True/False flag, and the two flags with values of their own (the `flags` attribute)
    flag_resource = forms.BooleanField(label="Resource", required=False)
    flag_nonresource = forms.BooleanField(label="Non-resource", required=False)
    flag_deemed = forms.BooleanField(label="Deemed", required=False)
    flag_custom = forms.BooleanField(label="Custom", required=False)
    flag_upstream = forms.BooleanField(label="Upstream", required=False)
    flag_midstream = forms.BooleanField(label="Midstream", required=False)
    flag_downstream = forms.BooleanField(label="Downstream", required=False)
    flag_audit = forms.BooleanField(label="Audit", required=False)
    flag_direct_install = forms.ChoiceField(
        label="Direct install", required=False, choices=[("", "Not given"), ("Yes", "Yes"), ("Partial", "Partial"), ("No", "No")]
    )
    flag_financing = forms.ChoiceField(
        label="Financing",
        required=False,
        choices=[("", "Not given"), ("None", "None"), ("OBF", "OBF (on-bill financing)"), ("Other Financing", "Other financing")],
    )

    # {form field: the key CEDARS' Programs list gives the flag in `flags`}
    FLAG_KEYS = {
        "flag_resource": "Resource_Flag",
        "flag_nonresource": "NonResource_Flag",
        "flag_deemed": "Deemed_Flag",
        "flag_custom": "Custom_Flag",
        "flag_upstream": "Upstream_Flag",
        "flag_midstream": "Midstream_Flag",
        "flag_downstream": "Downstream_Flag",
        "flag_audit": "Audit_Flag",
        "flag_direct_install": "DirectInstall",
        "flag_financing": "Financing",
    }
    TRUE = {"true", "yes", "1", "y", "t"}

    ATTRIBUTES = [
        "name",
        "commercial_name",
        "status",
        "segment",
        "primary_sector",
        "sectors",
        "implementer",
        "category",
        "implementation_contractor",
        "start_year",
        "end_year",
        "statewide_program",
        "parent_program",
        "website",
        "exclude_from_budget",
        "exclude_from_ce",
        "exclude_from_statewide",
    ]

    def __init__(self, *args, latest=None, **kwargs):
        self.latest = latest
        initial = dict(kwargs.pop("initial", None) or {})
        if latest is not None:
            initial.update({k: getattr(latest, k) for k in self.ATTRIBUTES})
            initial["sectors"] = ", ".join(f"{k}: {v:g}" for k, v in (latest.sectors or {}).items())
            initial["effective"] = max(timezone.localdate(), latest.valid_from + timedelta(days=1))
            flags = latest.flags or {}
            for name, key in self.FLAG_KEYS.items():
                value = str(flags.get(key, "")).strip()
                initial[name] = value.lower() in self.TRUE if name in self.BOOLEAN_FLAGS else value
        super().__init__(*args, initial=initial, **kwargs)
        for name in ("flag_direct_install", "flag_financing"):  # a value CEDARS' list has that the choices lack stays offered
            current = initial.get(name, "")
            if current and current not in dict(self.fields[name].choices):
                self.fields[name].choices = [*self.fields[name].choices, (current, f"{current} (not in the list)")]
        guide_url = reverse("programs:fields")
        for name in self.ATTRIBUTES:
            e = field_guide.entry(name)
            if e is None:
                continue
            if e.values:
                current = getattr(latest, name, "") if latest is not None else ""
                self.fields[name].choices = field_guide.choices(name, current=current or "")
            self.fields[name].help_text = format_html(
                '{} <a href="{}#{}" target="_blank" rel="noopener">More about {} (opens in a new tab)</a>',
                e.hint,
                guide_url,
                e.anchor,
                e.label.lower(),
            )

    def clean_sectors(self):
        text = self.cleaned_data.get("sectors", "")
        try:
            shares = parse_sectors(text)
        except ValueError as e:
            raise forms.ValidationError("Write each sector as Name: share, separated by commas.") from e
        if shares and abs(sum(shares.values()) - 1) > 0.001:
            raise forms.ValidationError(f"The shares add up to {sum(shares.values()):g}, not 1.")
        return shares

    def clean(self):
        data = super().clean()
        start, end = data.get("start_year"), data.get("end_year")
        if start and end and end < start:
            self.add_error("end_year", "The end year is before the start year.")
        return data

    @property
    def BOOLEAN_FLAGS(self) -> set[str]:
        return {n for n in self.FLAG_KEYS if n not in ("flag_direct_install", "flag_financing")}

    def attribute_fields(self):
        """The form's fields except the delivery flags (shown together in their own group)."""
        return [self[n] for n in self.fields if n not in self.FLAG_KEYS]

    def flag_fields(self):
        return [self[n] for n in self.FLAG_KEYS]

    def flags(self) -> dict:
        """The `flags` attribute from the boxes: the latest version's flags with each box's value written over it; a True/False flag the
        version never had and that is not ticked stays out, as does a blank direct install or financing."""
        old = dict(self.latest.flags or {}) if self.latest is not None else {}
        out = dict(old)
        for name, key in self.FLAG_KEYS.items():
            value = self.cleaned_data.get(name)
            if name in self.BOOLEAN_FLAGS:
                if value:
                    out[key] = "True"
                elif key in old:
                    out[key] = "False"
            elif value:
                out[key] = value
            else:
                out.pop(key, None)
        return out

    def values(self) -> dict:
        """Every attribute as entered, for a new program's first version."""
        out = {}
        for k in self.ATTRIBUTES:
            v = self.cleaned_data.get(k)
            if k in ("start_year", "end_year"):
                v = v or None
            elif isinstance(v, str):
                v = v.strip()
            elif v is None and k == "sectors":
                v = {}
            out[k] = v
        out["flags"] = self.flags()
        return out

    def changes(self) -> dict:
        """{attribute: new value} for every attribute that differs from the latest version."""
        out = {}
        flags = self.flags()
        if flags != ((self.latest.flags or {}) if self.latest is not None else {}):
            out["flags"] = flags
        for k in self.ATTRIBUTES:
            new = self.cleaned_data.get(k)
            old = getattr(self.latest, k) if self.latest is not None else None
            if k in ("start_year", "end_year"):
                new = new or None
            elif isinstance(old, str):
                new = (new or "").strip()
            if new != old:
                out[k] = new
        return out


class NewProgramForm(ProgramChangeForm):
    """A new program: its PA, its PrgID (kept for good) and its first version's attributes, from the date it starts."""

    administrator = forms.ChoiceField(label="Program administrator")
    prg_id = forms.CharField(
        label="PrgID",
        max_length=60,
        help_text="Kept for good and never reused. Starts with the PA's code (a statewide program's with SW_): letters, digits, "
        "underscores and hyphens.",
    )
    field_order = ["administrator", "prg_id", "effective", "reason"]

    STATEWIDE = "SW"

    def __init__(self, *args, administrators=(), statewide=False, **kwargs):
        super().__init__(*args, latest=None, **kwargs)
        self.administrators = {o.code: o for o in administrators}
        choices = [(o.code, f"{o.code}: {o.name}") for o in administrators]
        if statewide:
            choices.append((self.STATEWIDE, "SW: a statewide program, funded by several PAs"))
        self.fields["administrator"].choices = [("", "Choose the PA"), *choices] if len(choices) > 1 else choices
        self.fields["effective"].label = "Starts on"
        self.fields["effective"].help_text = "The day its first version takes effect: usually 1 January of its first program year."
        self.fields["reason"].help_text = "e.g. the decision, resolution or advice letter that approved it"

    def clean(self):
        from cedars.services.registry import service as registry

        data = super().clean()
        code = data.get("administrator")
        statewide = code == self.STATEWIDE
        org = None if statewide else self.administrators.get(code)
        self.statewide, self.organisation = statewide, org
        prg_id = (data.get("prg_id") or "").strip()
        if code and prg_id:
            problem = registry.prg_id_problem(prg_id, org, statewide=statewide)
            if problem:
                self.add_error("prg_id", problem)
        data["prg_id"] = prg_id
        self.follows = None
        if org is not None and prg_id:  # PGE_SW_FS: its attributes come from the statewide program, so none are needed here
            from cedars.services.registry.models import Program

            sw = registry.statewide_part_of(Program(prg_id=prg_id, administrator=org))
            if sw is not None and sw.versions.exists():
                self.follows = sw
                self.errors.pop("name", None)
        return data


class CorrectionForm(ProgramChangeForm):
    """A correction of one version's values: pre-filled from it; only the attributes that differ are corrected, its dates stay."""

    def __init__(self, *args, version=None, **kwargs):
        super().__init__(*args, latest=version, **kwargs)
        del self.fields["effective"]
        self.fields["reason"].label = "Reason for the correction"
        self.fields["reason"].help_text = "What was wrong and where the right value comes from"
        self.order_fields(["reason"])


class DecisionForm(forms.Form):
    note = forms.CharField(
        label="Note", required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2}), help_text="Needed when you decline"
    )


class WithdrawForm(forms.Form):
    reason = forms.CharField(label="Why withdraw it", required=False, max_length=1000)


class LockedFieldsForm(forms.Form):
    """A new policy: which attributes a program's administrators change only through a request."""

    fields_locked = forms.MultipleChoiceField(label="Locked attributes", required=False, widget=forms.CheckboxSelectMultiple)
    effective = forms.DateField(label="Effective from", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="Reason", max_length=1000, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fields_locked"].choices = list(choices)


class LeadForm(forms.Form):
    """A statewide program's lead PA: any PA's administrators while it has none, then only the lead's."""

    lead = forms.ModelChoiceField(
        label="Lead PA",
        queryset=None,
        required=False,
        empty_label="No lead PA",
        help_text="The PA that files the program's claims (in the statewide claims, before the PAs' own) and its budget",
    )
    reason = forms.CharField(label="Reason for the change", widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kw):
        from cedars.services.accounts.models import Organisation

        super().__init__(*args, **kw)
        self.fields["lead"].queryset = Organisation.objects.filter(kind=Organisation.Kind.PA, active=True).order_by("code")

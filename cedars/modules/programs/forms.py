from datetime import timedelta

from django import forms
from django.utils import timezone

from cedars.services.registry.importers import parse_sectors
from cedars.services.registry.models import Program

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


def pa_choices():
    codes = set(Program.objects.filter(administrator__isnull=False).values_list("administrator__code", flat=True))
    if Program.objects.filter(statewide=True).exists():
        codes.add("SW")
    return [("", "Every PA")] + [(c, "Statewide (SW)" if c == "SW" else c) for c in sorted(codes)]


class CatalogFilter(forms.Form):
    q = forms.CharField(label="Name or PrgID", required=False, widget=forms.TextInput(attrs={"type": "search"}))
    pa = forms.ChoiceField(label="PA", required=False)
    state = forms.ChoiceField(label="Open or closed", required=False, choices=STATE_CHOICES)
    segment = forms.ChoiceField(label="Segment", required=False, choices=SEGMENT_CHOICES)
    year = forms.IntegerField(
        label="Program year", required=False, min_value=2000, max_value=2100, help_text="Blank: the attributes in force today"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pa"].choices = pa_choices()


class ProgramChangeForm(forms.Form):
    """The attributes a program's administrator changes, pre-filled from the latest version; only the ones that differ are saved."""

    effective = forms.DateField(label="Effective from", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(
        label="Reason", max_length=1000, widget=forms.Textarea(attrs={"rows": 2}), help_text="e.g. the resolution or advice letter"
    )
    name = forms.CharField(label="Program name", max_length=300)
    commercial_name = forms.CharField(label="Commercial name", max_length=300, required=False)
    status = forms.ChoiceField(label="Status", choices=STATUS_CHOICES, required=False)
    segment = forms.ChoiceField(label="Segment", choices=[("", "Not given")] + SEGMENT_CHOICES[1:], required=False)
    primary_sector = forms.CharField(label="Primary sector", max_length=60, required=False)
    sectors = forms.CharField(
        label="Budget share by sector", required=False, help_text="e.g. Commercial: 0.9, Industrial: 0.1 (shares add up to 1)"
    )
    implementer = forms.CharField(label="Implementer", max_length=200, required=False)
    category = forms.CharField(label="Category", max_length=200, required=False)
    implementation_contractor = forms.CharField(label="Implementation contractor", max_length=300, required=False)
    start_year = forms.IntegerField(label="Start year", min_value=1990, max_value=2100, required=False)
    end_year = forms.IntegerField(label="End year", min_value=1990, max_value=2100, required=False)
    statewide_program = forms.CharField(label="Statewide program it delivers", max_length=200, required=False)
    parent_program = forms.CharField(label="Parent program", max_length=100, required=False)
    website = forms.URLField(label="Website", required=False, assume_scheme="https")
    exclude_from_budget = forms.BooleanField(label="Exclude from budget", required=False)
    exclude_from_ce = forms.BooleanField(label="Exclude from cost-effectiveness", required=False)
    exclude_from_statewide = forms.BooleanField(label="Exclude from statewide", required=False)

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
        initial = {}
        if latest is not None:
            initial = {k: getattr(latest, k) for k in self.ATTRIBUTES}
            initial["sectors"] = ", ".join(f"{k}: {v:g}" for k, v in (latest.sectors or {}).items())
            initial["effective"] = max(timezone.localdate(), latest.valid_from + timedelta(days=1))
        super().__init__(*args, initial=initial, **kwargs)

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

    def changes(self) -> dict:
        """{attribute: new value} for every attribute that differs from the latest version."""
        out = {}
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

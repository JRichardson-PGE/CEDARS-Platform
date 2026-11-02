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
    # the listed fields offer the program fields guide's values (BR-13.7: registry/program_fields.yaml), set in __init__
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


class DecisionForm(forms.Form):
    note = forms.CharField(
        label="Note", required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2}), help_text="Needed when you decline"
    )


class WithdrawForm(forms.Form):
    reason = forms.CharField(label="Why withdraw it", required=False, max_length=1000)


class LockedFieldsForm(forms.Form):
    """A new policy: which attributes a program's administrators change only through a request (BR-13.7)."""

    fields_locked = forms.MultipleChoiceField(label="Locked attributes", required=False, widget=forms.CheckboxSelectMultiple)
    effective = forms.DateField(label="Effective from", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="Reason", max_length=1000, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fields_locked"].choices = list(choices)

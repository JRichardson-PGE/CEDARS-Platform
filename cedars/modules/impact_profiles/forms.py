from django import forms
from django.conf import settings

from . import policy
from .models import ImpactProfile


def _territories():
    return [("", "Every territory")] + [(t, t) for t in policy.territories()]


class LibraryFilterForm(forms.Form):
    q = forms.CharField(label="Profile ID or sector contains", required=False, widget=forms.TextInput(attrs={"type": "search"}))
    family = forms.ChoiceField(label="Kind", required=False, choices=[("", "Every kind")] + list(ImpactProfile.Family.choices))
    territory = forms.ChoiceField(label="IOU territory", required=False)
    retired = forms.BooleanField(label="Include retired profiles", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["territory"].choices = [("", "Any territory")] + [(t, t) for t in policy.territories()]


class PackageFilterForm(forms.Form):
    q = forms.CharField(label="Name or Measure Version ID contains", required=False, widget=forms.TextInput(attrs={"type": "search"}))
    mine = forms.BooleanField(label="Only packages my organization leads", required=False)


class SyncFilterForm(forms.Form):
    q = forms.CharField(label="Excluded package contains", required=False, widget=forms.TextInput(attrs={"type": "search"}))


class UploadForm(forms.Form):
    file = forms.FileField(
        label="8,760 load-shape file (a ZIP holding one CSV, or the CSV)",
        help_text=f"Up to {settings.LOAD_SHAPE_MAX_UPLOAD_MB} MB. Columns as the template: one or more series, each with hours 1 to 8760.",
    )
    accept_partial = forms.BooleanField(
        label="Keep the series that pass if others fail",
        required=False,
        help_text="Off (the default): one failure rejects the whole file and nothing is saved.",
    )


class MappingForm(forms.Form):
    alias_key = forms.CharField(label="Profile ID the permutation names", max_length=300, help_text="e.g. OfL*New*rDXGF*Msr*Std")
    target_sector = forms.CharField(label="Its target sector", max_length=60)
    target = forms.ModelChoiceField(label="Profile it uses", queryset=ImpactProfile.objects.none())
    reason = forms.CharField(label="Reason", max_length=500, help_text="e.g. New construction reuses the existing-vintage modeling")

    def __init__(self, *args, package=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target"].queryset = ImpactProfile.objects.filter(package=package, retired_at__isnull=True)


class CustomProfileForm(forms.Form):
    profile_key = forms.CharField(label="Profile ID", max_length=300, help_text="The value measures will give as their impact profile")
    target_sector = forms.CharField(label="Target sector", max_length=60)
    territory = forms.ChoiceField(label="IOU territory", required=False)
    description = forms.CharField(label="Description", max_length=500, required=False)
    represents_year = forms.IntegerField(
        label="Calendar year the 8,760s are drawn on", required=False, min_value=1990, max_value=2100, help_text="Blank: 2009"
    )
    file = forms.FileField(
        label="Profile file (CSV, or a ZIP holding one)",
        help_text="Columns Climate Zone, Hour of Year and Impact; zone System for a statewide profile.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["territory"].choices = _territories()


class ReplaceForm(forms.Form):
    file = forms.FileField(label="New profile file (CSV, or a ZIP holding one)")
    represents_year = forms.IntegerField(label="Calendar year the 8,760s are drawn on", required=False, min_value=1990, max_value=2100)
    reason = forms.CharField(label="Reason", max_length=500, help_text="e.g. NMEC true-up for program year 2028")


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=500)


class SettingsForm(forms.Form):
    territories = forms.CharField(
        label="IOU territories",
        help_text="Codes separated by commas, for example PGE, SCE, SCG, SDGE. A profile filed with no territory is written to the "
        "library once for each, and a custom profile's territory must be one of them.",
    )
    load_shape_first_start = forms.DateField(
        label="Load shapes from measure versions starting on",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The eTRM sync takes draft measure versions starting on or after this date; earlier ones keep their DEER impact "
        "profile IDs. A change applies from the next sync.",
    )
    reason = forms.CharField(label="Reason for the change", max_length=500, widget=forms.Textarea(attrs={"rows": 2}))

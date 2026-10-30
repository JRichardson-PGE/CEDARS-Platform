from django import forms

from cedars.services.accounts import permissions as perms
from cedars.services.accounts.models import Organisation


class GrantForm(forms.Form):
    email = forms.EmailField(
        label="Their email address", help_text="They need an account first: anyone can create one from the sign-up page."
    )
    permission = forms.ChoiceField(label="Permission")
    expires = forms.DateField(label="Expires (optional)", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="Reason", required=False, max_length=500)

    def __init__(self, *args, codes=None, **kwargs):
        super().__init__(*args, **kwargs)
        codes = codes if codes is not None else perms.all_permissions()
        self.fields["permission"].choices = [(c, perms.label(c)) for c in codes]


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=500)


class RequestAccessForm(forms.Form):
    organisation = forms.ModelChoiceField(label="Organisation", queryset=Organisation.objects.filter(active=True).exclude(kind="cpuc"))
    permissions = forms.MultipleChoiceField(
        label="Permissions you need",
        widget=forms.CheckboxSelectMultiple,
        choices=[(c, perms.label(c)) for c in perms.module_permissions() + [perms.ORG_ADMIN, perms.PII]],
    )
    message = forms.CharField(
        label="Message to the organisation's administrators", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )


class OrganisationForm(forms.ModelForm):
    class Meta:
        model = Organisation
        fields = ["code", "name", "kind", "pa_type", "federated", "federation_provider"]


class RecertificationForm(forms.Form):
    due = forms.DateField(label="Due date", widget=forms.DateInput(attrs={"type": "date"}))


class PolicyForm(forms.Form):
    recertification_auto_suspend = forms.BooleanField(label="Suspend grants not certified by the due date", required=False)
    recertification_interval_days = forms.IntegerField(label="Days between recertifications", min_value=30, max_value=1095)
    reason = forms.CharField(label="Reason for the change", max_length=500)


class PolicyChangeForm(forms.Form):
    value = forms.TypedChoiceField(
        label="PII only for federated organisations and sessions",
        choices=[("true", "Required"), ("false", "Not required")],
        coerce=lambda v: v == "true",
    )
    reason = forms.CharField(label="Reason", max_length=500)


class AuditFilterForm(forms.Form):
    action = forms.CharField(label="Action starts with", required=False)
    actor = forms.CharField(label="Actor email contains", required=False)
    object_type = forms.CharField(label="Record type", required=False)


class AllocationUploadForm(forms.Form):
    file = forms.FileField(label="Factor file (CSV, CEDARS' export layout)")
    label = forms.CharField(label="Name", max_length=120, help_text="e.g. 2028 TUAL, or PY2028 claims revision")
    purpose = forms.ChoiceField(label="For", choices=[("filing", "A budget filing"), ("claims", "Claims")])
    cycle = forms.IntegerField(label="Filing cycle's first year (optional)", required=False, min_value=2000, max_value=2100)
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 2}))


class ApiTokenForm(forms.Form):
    name = forms.CharField(label="Name", max_length=100, help_text="What will use it, e.g. our claims system")
    organisation = forms.ChoiceField(label="Acting for")
    scopes = forms.MultipleChoiceField(
        label="Modules it may reach", widget=forms.CheckboxSelectMultiple, choices=list(perms.MODULES.items())
    )
    days = forms.TypedChoiceField(
        label="Lasts", coerce=int, choices=[(30, "30 days"), (90, "90 days"), (180, "180 days"), (365, "1 year")], initial=90
    )

    def __init__(self, *args, organisations=(), max_days=365, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["organisation"].choices = [(o.code, str(o)) for o in organisations] + [("", "No organisation: my own calculations")]
        self.fields["days"].choices = [c for c in self.fields["days"].choices if c[0] <= max_days]

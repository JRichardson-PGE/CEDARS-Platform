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
    organisation = forms.ModelChoiceField(label="Organization", queryset=Organisation.objects.filter(active=True).exclude(kind="cpuc"))
    # Read and submit for each module: what a new member normally needs (UAT F23). Confirm, approve, administrator and PII rights are
    # given by the organization's administrators once the person is a member (the service still accepts any permission).
    permissions = forms.MultipleChoiceField(
        label="Permissions you need",
        widget=forms.CheckboxSelectMultiple,
        choices=[(c, perms.label(c)) for c in perms.module_permissions() if c.rsplit(".", 1)[1] in ("read", "submit")],
        help_text=(
            "Confirming, approving, administering the organization and PII access are added by its administrators once you are a member."
        ),
    )
    message = forms.CharField(
        label="Message to the organization's administrators", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )


class OrganisationForm(forms.ModelForm):
    class Meta:
        model = Organisation
        fields = ["code", "name", "kind", "pa_type", "federated", "federation_provider"]
        help_texts = {"pa_type": "Required for a program administrator: IOU, REN or CCA. Leave blank for the CPUC or another organization."}

    def clean(self):
        d = super().clean()
        if d.get("kind") == Organisation.Kind.PA and not d.get("pa_type"):
            self.add_error("pa_type", "Choose the program administrator's type: IOU, REN or CCA.")
        if d.get("kind") and d.get("kind") != Organisation.Kind.PA and d.get("pa_type"):
            self.add_error("pa_type", "Only a program administrator has a PA type. Leave it blank.")
        return d


class OrganisationDetailsForm(forms.Form):
    """A CPUC system administrator's change to an organization's name or PA type, with a reason (audited)."""

    name = forms.CharField(label="Name", max_length=200)
    pa_type = forms.ChoiceField(label="PA type", choices=Organisation.PAType.choices, required=False)
    reason = forms.CharField(label="Reason for the change", max_length=500)

    def __init__(self, *args, org: Organisation, **kwargs):
        super().__init__(*args, **kwargs)
        if org.kind == Organisation.Kind.PA:
            self.fields["pa_type"].choices = [("", "Choose a type"), *Organisation.PAType.choices]
            self.fields["pa_type"].required = True
        else:
            del self.fields["pa_type"]


DATE = forms.DateInput(attrs={"type": "date"})


class RenameForm(forms.Form):
    """A CPUC system administrator gives an organization a new code from a date (audited, with a reason)."""

    code = forms.CharField(label="New code", max_length=20, help_text="Letters, digits, hyphens or underscores, e.g. AVA")
    name = forms.CharField(label="New name (optional)", max_length=200, required=False, help_text="Leave blank to keep the name")
    effective = forms.DateField(
        label="First day under the new code", widget=DATE, help_text="Records for earlier dates keep the old code as filed"
    )
    reason = forms.CharField(label="Reason for the rename", max_length=500)


class FormerCodeForm(forms.Form):
    """A code the organization used before its current one, and when."""

    code = forms.CharField(label="Former code", max_length=20)
    since = forms.DateField(label="First day it was used (optional)", widget=DATE, required=False, help_text="Leave blank: from the start")
    until = forms.DateField(label="First day under the next code", widget=DATE)
    reason = forms.CharField(label="Reason", max_length=500)


class MergeForm(forms.Form):
    """Another organization that is the same party, recorded under an old code, merged into this one."""

    source = forms.ModelChoiceField(label="Organization recorded under the old code", queryset=Organisation.objects.none())
    effective = forms.DateField(
        label="First day under this organization's code (optional)",
        widget=DATE,
        required=False,
        help_text="Leave blank: the day this code came into use, else today",
    )
    reason = forms.CharField(label="Reason for the merge", max_length=500)

    def __init__(self, *args, org: Organisation, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["source"]
        field.queryset = Organisation.objects.filter(kind=org.kind, successor__isnull=True).exclude(pk=org.pk).exclude(kind="cpuc")
        field.label_from_instance = lambda o: f"{o.code} ({o.name})"


class RecertificationForm(forms.Form):
    due = forms.DateField(label="Due date", widget=forms.DateInput(attrs={"type": "date"}))


class PolicyForm(forms.Form):
    recertification_auto_suspend = forms.BooleanField(label="Suspend grants not certified by the due date", required=False)
    recertification_interval_days = forms.IntegerField(label="Days between recertifications", min_value=30, max_value=1095)
    reason = forms.CharField(label="Reason for the change", max_length=500)


class PolicyChangeForm(forms.Form):
    value = forms.TypedChoiceField(
        label="PII only for federated organizations and sessions",
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


class GoalSetUploadForm(forms.Form):
    file = forms.FileField(label="Goal file (CSV, one row per IOU and period)")
    rules = forms.FileField(label="Counting rule file (CSV, optional)", required=False)
    decision = forms.CharField(label="Decision", max_length=40, help_text="e.g. D.25-08-034")
    adopted = forms.DateField(label="Adopted on", help_text="YYYY-MM-DD", widget=forms.DateInput(attrs={"type": "date"}))
    url = forms.URLField(label="Link to the decision (optional)", required=False, max_length=400, assume_scheme="https")
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
        self.fields["organisation"].choices = [(o.code, str(o)) for o in organisations] + [("", "No organization: my own calculations")]
        self.fields["days"].choices = [c for c in self.fields["days"].choices if c[0] <= max_days]

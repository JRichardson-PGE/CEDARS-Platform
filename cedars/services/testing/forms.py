from django import forms
from django.conf import settings

from .models import Result, ScriptVersion


class CampaignForm(forms.Form):
    name = forms.CharField(label="Name", max_length=120, help_text="What testers will see, e.g. Release 0.3 acceptance test")
    version = forms.ModelChoiceField(
        label="Scripts", queryset=ScriptVersion.objects.all(), help_text="The loaded version of the test scripts to use"
    )
    starts_on = forms.DateField(label="First day", widget=forms.DateInput(attrs={"type": "date"}))
    ends_on = forms.DateField(
        label="Last day (optional)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Shown to testers as the due date",
    )
    note = forms.CharField(
        label="What is being tested (optional)", required=False, widget=forms.Textarea(attrs={"rows": 3}), max_length=2000
    )


class AssignForm(forms.Form):
    script = forms.ModelChoiceField(label="Script", queryset=None)
    email = forms.EmailField(label="Tester's email address", help_text="The address of the tester's own CEDARS account")

    def __init__(self, campaign, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["script"].queryset = campaign.version.scripts.all()


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=500)


class OptionalReasonForm(forms.Form):
    reason = forms.CharField(label="Note (optional)", max_length=500, required=False)


class ResultForm(forms.Form):
    outcome = forms.ChoiceField(label="Result", choices=Result.Outcome.choices, widget=forms.RadioSelect)
    comment = forms.CharField(
        label="What happened",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Needed for Failed or Blocked: what you saw, and what you expected. Leave out personal information.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["comment"].widget.attrs["maxlength"] = str(settings.CEDARS_COMMENT_MAX_CHARS)

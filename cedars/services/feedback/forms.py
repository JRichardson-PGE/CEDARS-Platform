from django import forms
from django.conf import settings


class FeedbackForm(forms.Form):
    page = forms.CharField(widget=forms.HiddenInput, required=False)
    message = forms.CharField(
        label="Your feedback",
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text="What worked, what did not, or what you expected to find. Please leave out personal information such as phone numbers.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["message"].max_length = settings.CEDARS_FEEDBACK_MAX_CHARS
        self.fields["message"].widget.attrs["maxlength"] = str(settings.CEDARS_FEEDBACK_MAX_CHARS)


class CloseForm(forms.Form):
    note = forms.CharField(label="What was done (optional)", required=False, widget=forms.Textarea(attrs={"rows": 2}), max_length=2000)


class ReopenForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=500)

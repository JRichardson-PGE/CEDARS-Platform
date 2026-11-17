from django import forms
from django.conf import settings

KINDS = [
    ("help", "I have a question"),
    ("problem", "Something is not working"),
    ("barrier", "I cannot use something (an accessibility barrier)"),
    ("security", "I have a security concern"),
    ("idea", "I have an idea"),
    ("comment", "Something else"),
]


class FeedbackForm(forms.Form):
    page = forms.CharField(widget=forms.HiddenInput, required=False)
    kind = forms.ChoiceField(
        label="What is this?",
        choices=KINDS,
        widget=forms.RadioSelect,
        initial="help",
        required=False,
        help_text="An accessibility barrier or a security concern is treated as urgent. A security concern is read only by the CPUC's "
        "system administrators.",
    )
    message = forms.CharField(
        label="Your message",
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text="What you were doing, what happened and what you expected. Please leave out personal information such as customers' "
        "names, addresses or account numbers.",
    )

    private = forms.BooleanField(label="Keep this from my organization's administrators", required=False)

    def __init__(self, *args, organisation=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organisation is None:  # signed out, or acting for no organization: nobody to keep it from
            del self.fields["private"]
        else:
            self.fields["private"].label = f"Keep this from {organisation.code}'s administrators"
            self.fields["private"].help_text = (
                f"{organisation.code}'s administrators can read their members' questions, problems and accessibility barriers, "
                "so they can help. They never read ideas, comments or security concerns."
            )
        self.fields["message"].max_length = settings.CEDARS_FEEDBACK_MAX_CHARS
        self.fields["message"].widget.attrs["maxlength"] = str(settings.CEDARS_FEEDBACK_MAX_CHARS)


class CloseForm(forms.Form):
    note = forms.CharField(label="What was done (optional)", required=False, widget=forms.Textarea(attrs={"rows": 2}), max_length=2000)


class ReopenForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=500)


class ReplyForm(forms.Form):
    body = forms.CharField(label="Your reply", widget=forms.Textarea(attrs={"rows": 4}))

    def __init__(self, *args, label: str = "", help_text: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["body"].widget.attrs["maxlength"] = str(settings.CEDARS_FEEDBACK_MAX_CHARS)
        if label:
            self.fields["body"].label = label
        self.fields["body"].help_text = help_text

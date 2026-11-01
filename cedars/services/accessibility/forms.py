from django import forms


class ReviewForm(forms.Form):
    note = forms.CharField(
        label="What was done",
        widget=forms.Textarea(attrs={"rows": 2}),
        max_length=2000,
        help_text="For example: an accessible copy is filed as version 2, or why the file stands as it is.",
    )


class ReopenForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=500)

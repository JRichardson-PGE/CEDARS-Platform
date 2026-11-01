from django import forms

YEAR = {"min_value": 1990, "max_value": 2100, "required": False}


class CodeForm(forms.Form):
    code = forms.CharField(label="Code", max_length=200)
    description = forms.CharField(label="Description", required=False, max_length=500)
    start_year = forms.IntegerField(label="First claim year it is accepted", help_text="Blank: every year up to its end year.", **YEAR)
    end_year = forms.IntegerField(label="Last claim year it is accepted", help_text="Blank: no end. Give one to retire the code.", **YEAR)
    reason = forms.CharField(label="Reason", required=False, max_length=500)


class ChangeCodeForm(forms.Form):
    description = forms.CharField(label="Description", required=False, max_length=500)
    start_year = forms.IntegerField(label="First claim year it is accepted", **YEAR)
    end_year = forms.IntegerField(
        label="Last claim year it is accepted",
        help_text="Give a year to retire the code (it stays in the list, accepted up to that year); clear it to reinstate the code.",
        **YEAR,
    )
    reason = forms.CharField(label="Reason", max_length=500, help_text="Kept in the audit trail.")

    def __init__(self, *args, carried: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if carried:  # a code from an earlier version keeps its start year (the page says so)
            del self.fields["start_year"]


class UploadForm(forms.Form):
    file = forms.FileField(
        label="Codes file (CSV)",
        help_text="A header row with code and any of description, start_year, end_year. Each code is added, or changed if the list has "
        "it; codes the file leaves out stay as they are.",
    )
    reason = forms.CharField(label="Reason", max_length=500)


class PublishForm(forms.Form):
    effective_from = forms.DateField(label="Takes effect on", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="Why the list changes", max_length=1000, help_text="Kept with the version and shown on the list's page.")

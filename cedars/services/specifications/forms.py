"""The forms of the specification editor (editor_views.py). Every change asks why; the reason is kept in the audit trail."""

from django import forms

from cedars.core import pickers

from . import editor
from .models import SpecField

REASON_HELP = "Kept with the change in the audit trail, for example the decision or the request behind it."


def _reason(label: str = "Why", required: bool = True):
    return forms.CharField(label=label, required=required, help_text=REASON_HELP, max_length=500)


def names_list(text: str) -> list[str]:
    return [n.strip() for n in (text or "").replace(";", ",").split(",") if n.strip()]


class StartDraftForm(forms.Form):
    name = forms.CharField(
        label="Name of the new version",
        max_length=120,
        help_text="The CPUC's name for it, shown to filers everywhere, such as the claim year it is for. It can change until it is "
        "published.",
    )
    reason = _reason("Why a new version is made")


class SettingsForm(forms.Form):
    name = forms.CharField(label="Name", max_length=120, help_text="Unique in the specification; fixed once the version is published.")
    summary = forms.CharField(
        label="Summary",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="What the version is and why it is made, shown at the top of its page.",
    )
    engine_release = forms.ChoiceField(label="Engine release that prices its filings", required=False)
    cost_test_set = forms.ModelChoiceField(
        label="Cost-test set",
        queryset=None,
        required=False,
        empty_label="None named: the filing set's, else the one in force for the program year",
    )
    reason = _reason()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [("", "The release marked default for new runs")]
        for r in editor.engine_releases():
            if r.get("active", True):
                choices.append((r["release"], f"ReCET {r['release']}" + (" (the default now)" if r.get("default") else "")))
        current = self.initial.get("engine_release") or ""
        if current and current not in [c[0] for c in choices]:
            choices.append((current, f"ReCET {current} (not an active release)"))
        self.fields["engine_release"].choices = choices
        self.fields["cost_test_set"].queryset = editor.cost_test_sets()
        self.fields["cost_test_set"].label_from_instance = lambda p: f"{p.label} {p.version}"


class FieldForm(forms.Form):
    """A field's attributes. The type comes from the fixed list: each brings its own check and its own words on the pages."""

    requirement = forms.ChoiceField(
        label="Requirement",
        choices=[("required", "Required"), ("optional", "Optional")],
        widget=forms.RadioSelect,
        help_text="A field required only in some cases is optional, with a rule that says when.",
    )
    type = forms.ChoiceField(label="Type", choices=SpecField.Type.choices)
    max_length = forms.IntegerField(label="Most characters", required=False, min_value=1, help_text="Text only; blank for any length.")
    decimals = forms.IntegerField(
        label="Most decimal places", required=False, min_value=0, max_value=15, help_text="Numbers only; blank for any."
    )
    value_list = forms.ChoiceField(
        label="Value list", required=False, help_text="The list whose codes are accepted, if the field is coded."
    )
    default = forms.CharField(label="What a blank means", required=False, max_length=200)
    example = forms.CharField(
        label="Example value", required=False, max_length=200, help_text="A valid value, shown on the page and in the template."
    )
    unit = forms.CharField(label="Unit", required=False, max_length=80)
    definition = forms.CharField(label="Definition", widget=forms.Textarea(attrs={"rows": 3}), required=False)
    guidance = forms.CharField(label="Guidance for filers", widget=forms.Textarea(attrs={"rows": 2}), required=False)
    other_names = forms.CharField(
        label="Other names accepted on upload",
        required=False,
        max_length=500,
        help_text="Column names from CEDARS or CET also read as this field, separated by commas.",
    )
    position = forms.TypedChoiceField(label="Place in the template", coerce=int, required=False)
    reason = _reason()

    def __init__(self, *args, table=None, field=None, locked: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.locked = locked
        lists = editor.value_list_names()
        current = (field.value_list if field else "") or self.initial.get("value_list", "")
        if current and current not in lists:
            lists.append(current)
        self.fields["value_list"].choices = [("", "None")] + [(n, n) for n in lists]
        if field is not None and field.requirement == "system":
            self.fields["requirement"].choices = [("system", "Filled in by CEDARS")]
        others = [f for f in table.fields.all() if field is None or f.pk != field.pk] if table is not None else []
        self.fields["position"].choices = [(0, "First")] + [(i + 1, f"After {f.name}") for i, f in enumerate(others)]
        if field is None:
            self.fields["position"].initial = len(others)

    def attrs(self) -> dict:
        d = dict(self.cleaned_data)
        d["other_names"] = names_list(d.get("other_names", ""))
        d.pop("reason", None)
        position = d.pop("position", None)
        return {"position": position if position not in ("", None) else None, **d}


class AddFieldForm(FieldForm):
    """A new field: one the data dictionary has (its definition, type and length come with it unless given here), or a new name."""

    dictionary_field = pickers.FieldField(
        label="A data dictionary field",
        kinds=["input"],
        required=False,
        help_text="Type part of the field's name, then pick it from the list.",
    )
    new_name = forms.CharField(
        label="Or a new field's name",
        required=False,
        max_length=80,
        help_text="Letters, digits and underscores, starting with a letter. Give its definition below.",
    )
    field_order = ["dictionary_field", "new_name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["type"].required = False
        self.fields["requirement"].initial = "optional"

    def clean(self):
        d = super().clean()
        picked, new = d.get("dictionary_field"), (d.get("new_name") or "").strip()
        if bool(picked) == bool(new):
            raise forms.ValidationError("Pick a data dictionary field or give a new field's name (one of the two).")
        if new and not d.get("type"):
            self.add_error("type", "Choose the new field's type.")
        return d

    def name(self) -> str:
        return self.cleaned_data.get("dictionary_field") or self.cleaned_data.get("new_name", "").strip()

    def attrs(self) -> dict:
        d = super().attrs()
        d.pop("dictionary_field", None)
        d.pop("new_name", None)
        if not d.get("type"):
            d.pop("type", None)
        return d


class RenameForm(forms.Form):
    new_name = forms.CharField(
        label="New name",
        max_length=80,
        help_text="Letters, digits and underscores, starting with a letter. The old name is still accepted on upload.",
    )
    reason = _reason()


class NewListForm(forms.Form):
    name = forms.CharField(
        label="Name of the new list",
        max_length=60,
        help_text="Lower-case letters, digits and underscores, starting with a letter, such as delivery_channel.",
    )
    codes = forms.CharField(
        label="Codes",
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text="One to a line: the code, then its description, first year and last year, separated by commas (each after the code "
        "may be left out). Put a description that has a comma in double quotes. Add more, or load them from a CSV, on the list's page.",
    )
    reason = _reason()


class ExampleFileForm(forms.Form):
    file = forms.FileField(
        label="The example's workbook or .zip",
        help_text="As it downloads: a sheet (or delimited file) per table, the field names in the first row. Its records replace the "
        "example's.",
    )
    reason = _reason()


class TableForm(forms.Form):
    other_names = forms.CharField(
        label="Other file or sheet names accepted", required=False, max_length=500, help_text="Separated by commas; letter case is ignored."
    )
    guidance = forms.CharField(
        label="Guidance for filers", required=False, widget=forms.Textarea(attrs={"rows": 3}), help_text="Shown above the table's fields."
    )
    reason = _reason()


class ReasonForm(forms.Form):
    reason = _reason()


class PublishForm(forms.Form):
    effective_on = forms.DateField(
        label="Takes effect on",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The first day filing sets are set up with it. Filings already set up keep their version.",
    )
    short_notice_reason = forms.CharField(
        label="Why it cannot wait for the usual notice",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Only when it takes effect sooner than the usual notice; the reason is shown with it.",
    )
    changes = forms.CharField(
        label="What changes, for the release note",
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Written from the differences with the version it was copied from; change the wording as you need.",
    )
    action = forms.CharField(
        label="What filers need to do",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Blank: download the new template.",
    )

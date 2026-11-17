from django import forms

from .models import AcceptanceTest, ReleasePlan, WorkItem


class ItemForm(forms.ModelForm):
    class Meta:
        model = WorkItem
        fields = ["title", "kind", "who", "pages", "need", "why", "severity", "size", "workaround"]
        labels = {
            "title": "Title",
            "kind": "What it is",
            "who": "As a ... (the kind of user)",
            "pages": "On ... (the pages)",
            "need": "I need ...",
            "why": "So that ...",
            "severity": "How bad it is",
            "size": "Size",
            "workaround": "Workaround",
        }
        help_texts = {
            "title": "In plain words. It is what people outside the CPUC read if the item is made public, so name no person, "
            "organization or filing.",
            "who": "For example: a PA filer, a PA administrator, a CPUC reviewer, anyone signed out.",
            "pages": "One page address a line, for example /claims/.",
            "severity": "For a problem. Leave blank for an improvement.",
            "size": "Agreed with the developers. Leave blank until it is known.",
            "workaround": "What a person can do meanwhile. Shown with the item once it is public.",
        }
        widgets = {
            "kind": forms.RadioSelect,
            "pages": forms.Textarea(attrs={"rows": 3}),
            "need": forms.Textarea(attrs={"rows": 3}),
            "why": forms.Textarea(attrs={"rows": 2}),
            "workaround": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = WorkItem.Kind.choices  # no blank choice among the radio buttons
        for name in ("severity", "size"):
            self.fields[name].choices = [("", "Not set"), *self.fields[name].choices[1:]]


class TestForm(forms.ModelForm):
    class Meta:
        model = AcceptanceTest
        fields = ["title", "steps", "expected", "library_id"]
        labels = {"title": "What it checks", "steps": "Steps", "expected": "Expected result", "library_id": "Test ID (optional)"}
        help_texts = {
            "library_id": "The test's ID in the test scripts, when it is one of them, for example PAF-023. Where testing is on, "
            "that test is run in the release's round of testing and its results are shown with the release."
        }
        widgets = {"steps": forms.Textarea(attrs={"rows": 4}), "expected": forms.Textarea(attrs={"rows": 2})}


class PlanForm(forms.ModelForm):
    class Meta:
        model = ReleasePlan
        fields = ["name", "expected_on", "note"]
        labels = {"name": "Release", "note": "What it is for"}
        widgets = {"expected_on": forms.DateInput(attrs={"type": "date"}), "note": forms.Textarea(attrs={"rows": 2})}

    def validate_unique(self):  # the service says so in its own words
        pass


class IssueForm(forms.Form):
    issue_url = forms.URLField(
        label="The issue's address",
        max_length=300,
        required=False,
        assume_scheme="https",
        help_text="Leave it blank to take the issue off.",
    )


class PostIssueForm(forms.Form):
    read = forms.BooleanField(
        label="I have read it. It names no person, organization or filing.",
        error_messages={"required": "Read the issue and tick the box before posting it."},
    )


class GoForm(forms.Form):
    released_on = forms.DateField(label="Released on", widget=forms.DateInput(attrs={"type": "date"}))


class RoundForm(forms.Form):
    name = forms.CharField(label="Name of the round", max_length=120, help_text="What testers will see.")
    starts_on = forms.DateField(label="First day", widget=forms.DateInput(attrs={"type": "date"}))
    ends_on = forms.DateField(
        label="Last day (optional)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Shown to testers as the due date.",
    )


class PlanItemForm(forms.Form):
    item = forms.ModelChoiceField(label="Work item", queryset=WorkItem.objects.none(), empty_label="Choose a work item")

    def __init__(self, *args, ready=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = WorkItem.objects.filter(pk__in=[it.pk for it in ready]).order_by("position")
        self.fields["item"].label_from_instance = lambda it: f"{it.position}. {it.number}: {it.title}"


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", widget=forms.Textarea(attrs={"rows": 2}), max_length=2000)

    def __init__(self, *args, label: str = "", help_text: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        if label:
            self.fields["reason"].label = label
        self.fields["reason"].help_text = help_text


class JoinForm(forms.Form):
    item = forms.ModelChoiceField(label="Work item", queryset=WorkItem.objects.none(), empty_label="Choose a work item")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = WorkItem.objects.exclude(status__in=[WorkItem.Status.DECLINED, WorkItem.Status.DONE]).order_by("-pk")

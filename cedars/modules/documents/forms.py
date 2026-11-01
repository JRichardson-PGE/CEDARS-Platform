import re

from django import forms
from django.utils import timezone

from cedars.services.accounts.models import Organisation
from cedars.services.registry.models import Program

from . import service
from .models import AttributeOption, Collection, Comment, Stage, Study, Task, WorkOrder


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Any number of files from one input (Django's documented pattern)."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, list | tuple):
            return [single(d, initial) for d in data if d]
        return [single(data, initial)] if data else []


def _pas():
    return Organisation.objects.filter(kind=Organisation.Kind.PA, active=True).order_by("code")


def _option_label(o: AttributeOption) -> str:
    return f"{o.label} (retired)" if o.retired_on and o.retired_on <= timezone.localdate() else o.label


ACCEPT = ",".join(sorted(service.FILE_TYPES))
YEAR = {"min_value": 2000, "max_value": 2100, "required": False}


class SearchForm(forms.Form):
    q = forms.CharField(
        label="Search",
        required=False,
        help_text='Words in the title, summary, file names or the text of the files ("a phrase" in quotes, or, -leave out)',
    )
    collection = forms.ChoiceField(label="Collection", required=False)
    document_type = forms.ModelChoiceField(label="Document type", queryset=AttributeOption.objects.none(), required=False)
    pa = forms.ModelChoiceField(label="PA", queryset=Organisation.objects.none(), required=False, to_field_name="code")
    program = forms.CharField(label="Program (PrgID)", required=False)
    year = forms.IntegerField(label="Program year", **YEAR)
    published_from = forms.DateField(label="Published from", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    published_to = forms.DateField(label="Published to", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    status = forms.ChoiceField(
        label="Publication",
        required=False,
        choices=[("", "Published and unpublished"), ("published", "Published"), ("unpublished", "Unpublished")],
    )
    study = forms.CharField(label="Study ID", required=False)
    work_order = forms.CharField(label="Work order", required=False)
    options = forms.ModelMultipleChoiceField(queryset=AttributeOption.objects.none(), required=False)

    def __init__(self, *args, show_status=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["collection"].choices = [("", "Every collection"), *Collection.choices]
        types = AttributeOption.objects.filter(attribute_list__type_of__gt="").select_related("attribute_list")
        self.fields["document_type"].queryset = types.order_by("attribute_list__position", "position")
        self.fields["document_type"].label_from_instance = lambda o: f"{Collection(o.attribute_list.type_of).label}: {o.label}"
        self.fields["document_type"].empty_label = "Any type"
        self.fields["pa"].queryset = _pas()
        self.fields["pa"].empty_label = "Any PA"
        self.fields["pa"].label_from_instance = lambda o: o.code
        self.fields["options"].queryset = AttributeOption.objects.filter(attribute_list__type_of="", attribute_list__collections__len__gt=0)
        if not show_status:
            del self.fields["status"]

    def search(self) -> service.Search:
        d = self.cleaned_data if self.is_valid() else {}
        return service.Search(
            q=(d.get("q") or "").strip(),
            collection=d.get("collection") or "",
            document_type=d.get("document_type"),
            pa=d.get("pa"),
            program=d.get("program") or "",
            year=d.get("year"),
            options=list(d.get("options") or []),
            published_from=d.get("published_from"),
            published_to=d.get("published_to"),
            status=d.get("status") or "",
            study=_study(d.get("study")),
            work_order=WorkOrder.objects.filter(number__iexact=(d.get("work_order") or "").strip()).first()
            if d.get("work_order")
            else None,
        )


def _study(text):
    """A study by its study ID or its number; a study ID that matches nothing finds nothing (not every document)."""
    text = (text or "").strip()
    if not text:
        return None
    found = Study.objects.filter(study_id__iexact=text).first() or (
        Study.objects.filter(number=int(text)).first() if text.isdigit() else None
    )
    return found or Study(pk=0)


def study_named(text) -> Study | None:
    """The study a study ID or number names, or None."""
    found = _study(text)
    return found if found is not None and found.pk else None


class DetailsForm(forms.Form):
    """A document's details for one collection: its type, the common metadata and the collection's attribute lists, one field per list
    (attr_<code>). Retired options are offered only where the document already carries them."""

    document_type = forms.ModelChoiceField(label="Document type", queryset=AttributeOption.objects.none())
    title = forms.CharField(label="Title", max_length=200)
    summary = forms.CharField(label="Summary", required=False, max_length=3500, widget=forms.Textarea(attrs={"rows": 4}))
    pas = forms.ModelMultipleChoiceField(
        label="PAs it applies to",
        queryset=Organisation.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Every PA named can see and manage the document (a joint document). A PA's own document always names the PA.",
    )
    programs = forms.CharField(label="Programs (PrgIDs)", required=False, help_text="Separate several with commas or spaces")
    first_year = forms.IntegerField(label="First program year", **YEAR)
    last_year = forms.IntegerField(label="Last program year", **YEAR, help_text="The same as the first for a single year")
    study = forms.CharField(label="Study ID", required=False, help_text="The evaluation study it belongs to, e.g. 2025_Residential_733")
    work_order = forms.ModelChoiceField(label="Work order", queryset=WorkOrder.objects.none(), required=False, empty_label="None")
    task = forms.ModelChoiceField(label="Task", queryset=Task.objects.none(), required=False, empty_label="None")

    def __init__(self, *args, collection: str, document=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.collection = collection
        if collection not in service.STUDY_COLLECTIONS:
            del self.fields["study"]
        if collection == Collection.EMV:
            self.fields["work_order"].queryset = WorkOrder.objects.all()
            self.fields["task"].queryset = Task.objects.select_related("work_order")
        else:
            del self.fields["work_order"], self.fields["task"]
        keep = set()
        if document is not None:
            keep = set(document.attributes.values_list("pk", flat=True)) | {document.document_type_id}
        today = timezone.localdate()
        types = service.type_list(collection)
        type_options = [o for o in types.options.all() if o.offered_on(today) or o.pk in keep] if types else []
        self.fields["document_type"].queryset = AttributeOption.objects.filter(pk__in=[o.pk for o in type_options])
        self.fields["document_type"].label_from_instance = _option_label
        self.fields["pas"].queryset = _pas()
        self.fields["pas"].label_from_instance = lambda o: f"{o.code} ({o.name})"
        self.lists = service.attribute_lists(collection)
        for al in self.lists:
            qs = AttributeOption.objects.filter(pk__in=[o.pk for o in al.options.all() if o.offered_on(today) or o.pk in keep])
            if al.multiple:
                f = forms.ModelMultipleChoiceField(label=al.name, queryset=qs, required=False, widget=forms.CheckboxSelectMultiple)
            else:
                f = forms.ModelChoiceField(label=al.name, queryset=qs, required=False, empty_label="Not given")
            f.label_from_instance = _option_label
            self.fields[f"attr_{al.code}"] = f
        if document is not None and not self.is_bound:
            self.initial.update(
                document_type=document.document_type_id,
                title=document.title,
                summary=document.summary,
                pas=list(document.pas.all()),
                programs=", ".join(p.prg_id for p in document.programs.all()),
                first_year=document.first_year,
                last_year=document.last_year,
                study=document.study.study_id if document.study else "",
                work_order=document.work_order_id,
                task=document.task_id,
            )
            chosen = list(document.attributes.all())
            for al in self.lists:
                mine = [o.pk for o in chosen if o.attribute_list_id == al.pk]
                self.initial[f"attr_{al.code}"] = mine if al.multiple else (mine[0] if mine else None)

    def attribute_fields(self):
        return [self[f"attr_{al.code}"] for al in self.lists]

    def clean_programs(self):
        found, missing = {}, []
        for prg_id in (p for p in re.split(r"[\s,;]+", self.cleaned_data.get("programs") or "") if p):
            program = Program.objects.filter(prg_id__iexact=prg_id).first()
            if program is None:
                missing.append(prg_id)
            else:
                found[program.pk] = program
        if missing:
            raise forms.ValidationError(f"Not in the program registry: {', '.join(missing)}.")
        return list(found.values())

    def clean_study(self):
        text = (self.cleaned_data.get("study") or "").strip()
        if not text:
            return None
        found = _study(text)
        if not found.pk:
            raise forms.ValidationError(f"No study {text} in the register.")
        return found

    def details(self) -> service.Details:
        d = self.cleaned_data
        attributes = []
        for al in self.lists:
            value = d.get(f"attr_{al.code}")
            attributes += list(value) if al.multiple else ([value] if value else [])
        first, last = d.get("first_year"), d.get("last_year")
        if first and not last:
            last = first
        return service.Details(
            document_type=d["document_type"],
            title=d["title"],
            summary=d.get("summary") or "",
            pas=list(d.get("pas") or []),
            programs=d.get("programs") or [],
            first_year=first,
            last_year=last,
            attributes=attributes,
            study=d.get("study"),
            work_order=d.get("work_order"),
            task=d.get("task"),
        )


class FilesMixin(forms.Form):
    main_file = forms.FileField(label="Main file", widget=forms.ClearableFileInput(attrs={"accept": ACCEPT}))
    attachments = MultipleFileField(label="Attachments", required=False, help_text="Optional; choose several at once")

    def uploads(self) -> list[service.Upload]:
        files = [self.cleaned_data["main_file"], *self.cleaned_data.get("attachments", [])]
        return [service.Upload(f.name, f) for f in files]


DATE = {"required": False, "widget": forms.DateInput(attrs={"type": "date"})}


class PeriodMixin(forms.Form):
    """A version's comment period (BR-15.4): registered users comment from the day it opens to the day it closes, both included."""

    comments_open = forms.DateField(label="Comments open", help_text="Leave both blank for no public comment", **DATE)
    comments_close = forms.DateField(label="Comments close", **DATE)

    def clean(self):
        d = super().clean()
        opens, closes = d.get("comments_open"), d.get("comments_close")
        if bool(opens) != bool(closes):
            raise forms.ValidationError("Give both the date comments open and the date they close, or neither.")
        if opens and closes and opens > closes:
            self.add_error("comments_close", "Comments close on or after the day they open.")
        return d

    def period(self):
        d = self.cleaned_data
        return (d["comments_open"], d["comments_close"]) if d.get("comments_open") else None


def _stage_field(stages, required=True):
    return forms.ChoiceField(
        label="Stage",
        choices=[(s, Stage(s).label) for s in stages],
        required=required,
        widget=forms.RadioSelect,
        help_text="A draft stage may have several drafts; a final is revised with a reason. A version never goes back a stage.",
    )


def _placeholder_field(replacing: bool = False):
    """BR-13.6: an implementation plan filed before the full plan is ready is flagged, on its program's page too."""
    return forms.BooleanField(
        label="This is a placeholder plan",
        required=False,
        help_text="Only for an implementation plan: tick it when the full plan is still to come"
        + ("; leave it unticked for the version that brings the full plan" if replacing else ", as a later version of this document"),
    )


class NewDocumentForm(FilesMixin, PeriodMixin, DetailsForm):
    publish = forms.BooleanField(label="Publish it now", required=False, help_text="Published documents are public and never change")

    field_order = ["document_type", "title", "summary"]

    def __init__(self, *args, can_publish=False, **kwargs):
        from .deliverables import WORK_STAGES

        super().__init__(*args, **kwargs)
        self.fields["main_file"].widget.attrs["accept"] = ACCEPT
        self.fields["attachments"].widget.attrs["accept"] = ACCEPT
        if self.collection == Collection.EMV:
            self.fields["deliverable"] = forms.BooleanField(
                label="Track it as a study deliverable",
                required=False,
                help_text="Its drafts, finals and the PAs' responses then follow on this one record. Name its study above.",
            )
            self.fields["stage"] = _stage_field(WORK_STAGES, required=False)
        if self.collection == Collection.PROGRAM:
            self.fields["placeholder"] = _placeholder_field()
        if not can_publish:
            del self.fields["publish"]

    def clean(self):
        d = super().clean()
        if d.get("placeholder") and not service.takes_placeholder(self.collection, d.get("document_type")):
            self.add_error("placeholder", "Only an implementation plan is filed as a placeholder.")
        if d.get("deliverable"):
            if not d.get("study"):
                self.add_error("study", "A deliverable belongs to a study: give its study ID.")
            if not d.get("stage"):
                self.add_error("stage", "Choose the stage of this first version.")
        return d

    def workflow(self) -> dict:
        d = self.cleaned_data
        deliverable = bool(d.get("deliverable"))
        return {
            "deliverable": deliverable,
            "stage": (d.get("stage") or "") if deliverable else "",
            "comment_period": self.period(),
            "placeholder": bool(d.get("placeholder")),
        }


class ChangeDetailsForm(DetailsForm):
    reason = forms.CharField(label="Reason for the change", widget=forms.Textarea(attrs={"rows": 2}))


class VersionForm(FilesMixin, PeriodMixin):
    reason = forms.CharField(label="Why this version replaces the previous one", widget=forms.Textarea(attrs={"rows": 2}))
    publish = forms.BooleanField(label="Publish it now", required=False, help_text="Published versions are public and never change")

    field_order = ["stage", "main_file", "attachments", "reason", "comments_open", "comments_close", "publish"]

    def __init__(self, *args, can_publish=False, stages=(), placeholder=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["attachments"].widget.attrs["accept"] = ACCEPT
        if stages:
            self.fields["stage"] = _stage_field(stages)
        if placeholder:
            self.fields["placeholder"] = _placeholder_field(replacing=True)
        if stages or placeholder:
            self.order_fields([*self.field_order[:-1], "placeholder", "publish"])
        if not can_publish:
            del self.fields["publish"]


class ResponseForm(FilesMixin):
    """A PA's response to a deliverable's recommendations."""

    pa = forms.ModelChoiceField(label="Responding for", queryset=Organisation.objects.none(), empty_label=None)
    reason = forms.CharField(
        label="Why it replaces your earlier response",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Only for a revised response",
    )
    publish = forms.BooleanField(label="Publish it now", required=False, help_text="Published responses are public and never change")

    field_order = ["pa", "main_file", "attachments", "reason", "publish"]

    def __init__(self, *args, pas=(), can_publish=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pa"].queryset = Organisation.objects.filter(pk__in=[o.pk for o in pas]).order_by("code")
        self.fields["pa"].label_from_instance = lambda o: f"{o.code} ({o.name})"
        self.fields["attachments"].widget.attrs["accept"] = ACCEPT
        if not can_publish:
            del self.fields["publish"]


class RequestResponsesForm(forms.Form):
    pas = forms.ModelMultipleChoiceField(label="PAs to respond", queryset=Organisation.objects.none(), widget=forms.CheckboxSelectMultiple)
    due = forms.DateField(label="Responses due", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="Note", required=False, help_text="Optional; kept in the deliverable's history")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pas"].queryset = _pas()
        self.fields["pas"].label_from_instance = lambda o: o.code


class CommentForm(forms.Form):
    """The PDA's Add a Comment or Question form, with the section or page BR-15.4 asks for."""

    version = forms.ChoiceField(label="On", required=True)
    kind = forms.ChoiceField(label="Comment or question", choices=Comment.Kind.choices, widget=forms.RadioSelect)
    subject = forms.CharField(label="Subject", max_length=100)
    section = forms.CharField(label="Section or page", max_length=100, required=False, help_text="Optional, e.g. 4.2 or page 31")
    text = forms.CharField(
        label="Comment",
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text="Up to 2,000 characters; one point per comment helps a response",
    )
    attachment = forms.FileField(label="Supporting file", required=False, help_text="Optional; PDF, Word, Excel and the like")

    def __init__(self, *args, versions=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["version"].choices = [(str(v.number), _version_name(v)) for v in versions]
        if len(versions) == 1:
            self.fields["version"].widget = forms.HiddenInput()
            self.initial["version"] = str(versions[0].number)
        self.fields["attachment"].widget.attrs["accept"] = ACCEPT


def _version_name(v) -> str:
    return f"Version {v.number}" + (f": {v.stage_label}" if v.stage else "")


class RespondForm(forms.Form):
    text = forms.CharField(label="Response", max_length=2000, widget=forms.Textarea(attrs={"rows": 3}))
    organisation = forms.ModelChoiceField(label="Responding for", queryset=Organisation.objects.none(), empty_label=None)

    def __init__(self, *args, organisations=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["organisation"].queryset = Organisation.objects.filter(pk__in=[o.pk for o in organisations])
        self.fields["organisation"].label_from_instance = lambda o: o.code


class PeriodForm(forms.Form):
    opens = forms.DateField(label="Comments open", widget=forms.DateInput(attrs={"type": "date"}))
    closes = forms.DateField(label="Comments close", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="Reason", required=False, help_text="Needed to change dates already set")


class OptionForm(forms.Form):
    label = forms.CharField(label="Value", max_length=200)
    reason = forms.CharField(label="Reason", required=False)


class RetireForm(forms.Form):
    reason = forms.CharField(label="Reason")


# --- the study register


def _list_field(code, label, *, multiple=False, required=False, keep=()):
    from . import studies

    qs = studies.options(code, keep)
    if multiple:
        f = forms.ModelMultipleChoiceField(label=label, queryset=qs, required=required, widget=forms.CheckboxSelectMultiple)
    else:
        f = forms.ModelChoiceField(label=label, queryset=qs, required=required, empty_label=None if required else "Not given")
    f.label_from_instance = _option_label
    return f


def _cpuc_staff():
    from cedars.services.accounts.models import User

    return User.objects.filter(memberships__organisation__kind="cpuc", memberships__ended_at__isnull=True, is_active=True).distinct()


class StudyForm(forms.Form):
    """A study's fields (BR-15.6), with the PSR's limits on its text. A PA adding its own study is its manager; only CPUC ED chooses
    another manager."""

    title = forms.CharField(label="Study name", max_length=255)
    manager = forms.ModelChoiceField(label="Study manager", queryset=Organisation.objects.none())
    budget = forms.DecimalField(label="Budget ($)", min_value=0, max_digits=14, decimal_places=2, required=False)
    work_order = forms.ModelChoiceField(label="Work order", queryset=WorkOrder.objects.none(), required=False, empty_label="None")
    description = forms.CharField(label="Description", max_length=1000, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    objective = forms.CharField(label="Objective", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    research_questions = forms.CharField(
        label="Research questions", max_length=1500, required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    methods = forms.CharField(label="Potential EM&V methods", max_length=250, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    next_activity = forms.CharField(label="Next activity", max_length=255, required=False)
    next_activity_due = forms.DateField(label="Next activity due", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    completion_date = forms.DateField(label="Completion date", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    calmac_id = forms.CharField(label="CALMAC ID", max_length=40, required=False, help_text="Leave blank until the final report has one")
    notes = forms.CharField(label="Notes", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    ed_program_manager = forms.ModelChoiceField(label="ED program manager", queryset=Organisation.objects.none(), required=False)
    contacts = forms.CharField(
        label="Contacts",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="ED consultants, PA contacts, the PCG and its coordinators. Not public: CPUC staff and the study manager see them.",
    )

    LIST_FIELDS = [
        ("lead", "study_lead", "Study lead", False, True),
        ("status", "study_status", "Status", False, False),
        ("study_type", "study_type", "Study type", False, False),
        ("group", "study_group", "Group or sub-sector", False, False),
        ("sectors", "sector", "Sectors", True, False),
        ("funding_cycles", "portfolio_cycle", "Funding cycles", True, False),
    ]

    def __init__(self, *args, study=None, organisation=None, admin=False, **kwargs):
        super().__init__(*args, **kwargs)
        keep = []
        if study is not None:
            keep = [study.lead, study.status, study.study_type, study.group, *study.sectors.all(), *study.funding_cycles.all()]
        for name, code, label, multiple, required in self.LIST_FIELDS:
            self.fields[name] = _list_field(code, label, multiple=multiple, required=required, keep=keep)
        managers = Organisation.objects.filter(active=True).order_by("code")
        if not admin:
            managers = managers.filter(pk=(study.manager_id if study else getattr(organisation, "pk", None)))
        self.fields["manager"].queryset = managers
        self.fields["manager"].label_from_instance = lambda o: f"{o.code} ({o.name})"
        self.fields["work_order"].queryset = WorkOrder.objects.all()
        self.fields["ed_program_manager"].queryset = _cpuc_staff()
        self.fields["ed_program_manager"].label_from_instance = lambda u: u.name or u.email
        self.order_fields(
            ["title", "manager", "lead", "status", "study_type", "group", "sectors", "funding_cycles", "budget", "work_order"]
        )
        if study is not None and not self.is_bound:
            for name in [n for n in self.fields if n != "reason"]:
                value = getattr(study, name)
                self.initial[name] = list(value.all()) if name in ("sectors", "funding_cycles") else value
        elif organisation is not None and not admin:
            self.initial["manager"] = organisation

    def data_object(self):
        from . import studies

        d = self.cleaned_data
        values = {}
        for k in DATA_FIELDS:
            v = d.get(k)
            if k in ("sectors", "funding_cycles"):
                v = list(v or [])
            elif v is None and k in TEXT:
                v = ""
            values[k] = v
        return studies.StudyData(**values)


TEXT = {"description", "objective", "research_questions", "methods", "next_activity", "calmac_id", "notes", "contacts", "title"}
DATA_FIELDS = [
    "title",
    "manager",
    "lead",
    "study_type",
    "sectors",
    "group",
    "funding_cycles",
    "budget",
    "status",
    "work_order",
    "description",
    "objective",
    "research_questions",
    "methods",
    "next_activity",
    "next_activity_due",
    "completion_date",
    "calmac_id",
    "notes",
    "ed_program_manager",
    "contacts",
]


class ChangeStudyForm(StudyForm):
    reason = forms.CharField(label="Reason for the change", widget=forms.Textarea(attrs={"rows": 2}))


NOT_A_DELIVERABLE = ""


class StudyDocumentForm(FilesMixin):
    """The study's first document, uploaded on the Add a study page (optional): an EM&V document filed under the new study. With no
    main file the study is added on its own; the other fields are then ignored. Its other details (summary, programs, years, the
    collection's lists) are changed later from the document's page."""

    prefix = "doc"

    document_type = forms.ModelChoiceField(label="Document type", queryset=AttributeOption.objects.none(), required=False)
    title = forms.CharField(label="Document title", max_length=200, required=False, help_text="Left blank: the file's name")
    stage = forms.ChoiceField(
        label="Deliverable stage",
        required=False,
        widget=forms.RadioSelect,
        help_text="A deliverable's later drafts, finals and the PAs' responses follow on this one record.",
    )
    publish = forms.BooleanField(label="Publish it now", required=False, help_text="Published documents are public and never change")

    field_order = ["main_file", "attachments", "document_type", "title", "stage", "publish"]

    def __init__(self, *args, can_publish=False, **kwargs):
        from .deliverables import WORK_STAGES

        super().__init__(*args, **kwargs)
        self.fields["main_file"].required = False
        self.fields["main_file"].help_text = "Optional. Leave it blank to add the study without a document."
        self.fields["attachments"].widget.attrs["accept"] = ACCEPT
        today = timezone.localdate()
        types = service.type_list(Collection.EMV)
        offered = [o.pk for o in types.options.all() if o.offered_on(today)] if types else []
        self.fields["document_type"].queryset = AttributeOption.objects.filter(pk__in=offered)
        self.fields["document_type"].label_from_instance = _option_label
        self.fields["stage"].choices = [(NOT_A_DELIVERABLE, "Not a deliverable")] + [(s, Stage(s).label) for s in WORK_STAGES]
        self.fields["stage"].initial = NOT_A_DELIVERABLE
        if not can_publish:
            del self.fields["publish"]
        self.order_fields(self.field_order)

    def has_file(self) -> bool:
        return bool(self.is_bound and self.cleaned_data.get("main_file"))

    def clean(self):
        d = super().clean()
        if d.get("main_file"):
            if not d.get("document_type"):
                self.add_error("document_type", "Choose the document's type.")
        elif d.get("attachments"):
            self.add_error("main_file", "Choose the main file as well as the attachments.")
        return d

    def details(self, study: Study) -> service.Details:
        d = self.cleaned_data
        title = (d.get("title") or "").strip() or service.clean_name(d["main_file"].name).rsplit(".", 1)[0]
        return service.Details(document_type=d["document_type"], title=title, study=study)

    def workflow(self) -> dict:
        stage = self.cleaned_data.get("stage") or ""
        return {"deliverable": bool(stage), "stage": stage, "publish": bool(self.cleaned_data.get("publish"))}


class CoverageForm(forms.Form):
    program = forms.CharField(label="Program (PrgID)")
    first_year = forms.IntegerField(label="First program year", min_value=2000, max_value=2100)
    last_year = forms.IntegerField(label="Last program year", min_value=2000, max_value=2100)
    whole = forms.BooleanField(label="The whole program", required=False, initial=True, help_text="Untick when it covers a part")
    note = forms.CharField(label="Which part, or a note", max_length=255, required=False)

    def clean_program(self):
        p = Program.objects.filter(prg_id__iexact=self.cleaned_data["program"].strip()).first()
        if p is None:
            raise forms.ValidationError("Not in the program registry.")
        return p


class RegisterForm(forms.Form):
    q = forms.CharField(label="Search", required=False, help_text="Words in the name or description, a study ID, work order or CALMAC ID")
    cycle = forms.ModelChoiceField(label="Funding cycle", queryset=AttributeOption.objects.none(), required=False)
    status = forms.ModelChoiceField(label="Status", queryset=AttributeOption.objects.none(), required=False)
    sector = forms.ModelChoiceField(label="Sector", queryset=AttributeOption.objects.none(), required=False)
    lead = forms.ModelChoiceField(label="Study lead", queryset=AttributeOption.objects.none(), required=False)
    study_type = forms.ModelChoiceField(label="Study type", queryset=AttributeOption.objects.none(), required=False)
    manager = forms.ModelChoiceField(label="Study manager", queryset=Organisation.objects.none(), required=False, to_field_name="code")
    program = forms.CharField(label="Program (PrgID)", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, code in (
            ("cycle", "portfolio_cycle"),
            ("status", "study_status"),
            ("sector", "sector"),
            ("lead", "study_lead"),
            ("study_type", "study_type"),
        ):
            self.fields[name].queryset = AttributeOption.objects.filter(attribute_list__code=code)
            self.fields[name].empty_label = "Any"
        self.fields["manager"].queryset = Organisation.objects.filter(managed_studies__isnull=False).distinct().order_by("code")
        self.fields["manager"].label_from_instance = lambda o: o.code
        self.fields["manager"].empty_label = "Any"

    def filter(self):
        from . import studies

        d = self.cleaned_data if self.is_valid() else {}

        def one(name):
            return [d[name]] if d.get(name) else []

        return studies.RegisterFilter(
            q=(d.get("q") or "").strip(),
            cycles=one("cycle"),
            statuses=one("status"),
            sectors=one("sector"),
            leads=one("lead"),
            types=one("study_type"),
            managers=one("manager"),
            program=d.get("program") or "",
        )


class WorkOrderForm(forms.Form):
    number = forms.CharField(label="Work order number", max_length=50)
    name = forms.CharField(label="Name", max_length=255, required=False)
    firm = forms.ModelChoiceField(label="Contractor organization", queryset=Organisation.objects.none(), required=False)
    firm_name = forms.CharField(label="Contractor as the contract names it", max_length=200, required=False)
    cpuc_contract_id = forms.CharField(label="CPUC contract id", max_length=50, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["firm"].queryset = Organisation.objects.filter(active=True).exclude(kind="cpuc").order_by("code")
        self.fields["firm"].label_from_instance = lambda o: f"{o.code} ({o.name})"


class KeyDateForm(forms.Form):
    on = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    title = forms.CharField(label="What happens", max_length=200)
    detail = forms.CharField(label="Detail (optional)", required=False, max_length=1000, widget=forms.Textarea(attrs={"rows": 2}))
    link = forms.URLField(label="Link (optional)", required=False, max_length=400, assume_scheme="https")

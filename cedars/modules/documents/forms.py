import re

from django import forms
from django.utils import timezone

from cedars.services.accounts.models import Organisation
from cedars.services.registry.models import Program

from . import service
from .models import AttributeOption, Collection


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
    q = forms.CharField(label="Search", required=False, help_text="Words in the title, summary or file names")
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
        self.fields["options"].queryset = AttributeOption.objects.filter(attribute_list__type_of="")
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
        )


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

    def __init__(self, *args, collection: str, document=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.collection = collection
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
        )


class FilesMixin(forms.Form):
    main_file = forms.FileField(label="Main file", widget=forms.ClearableFileInput(attrs={"accept": ACCEPT}))
    attachments = MultipleFileField(label="Attachments", required=False, help_text="Optional; choose several at once")

    def uploads(self) -> list[service.Upload]:
        files = [self.cleaned_data["main_file"], *self.cleaned_data.get("attachments", [])]
        return [service.Upload(f.name, f) for f in files]


class NewDocumentForm(FilesMixin, DetailsForm):
    publish = forms.BooleanField(label="Publish it now", required=False, help_text="Published documents are public and never change")

    field_order = ["document_type", "title", "summary"]

    def __init__(self, *args, can_publish=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["main_file"].widget.attrs["accept"] = ACCEPT
        self.fields["attachments"].widget.attrs["accept"] = ACCEPT
        if not can_publish:
            del self.fields["publish"]


class ChangeDetailsForm(DetailsForm):
    reason = forms.CharField(label="Reason for the change", widget=forms.Textarea(attrs={"rows": 2}))


class VersionForm(FilesMixin):
    reason = forms.CharField(label="Why this version replaces the previous one", widget=forms.Textarea(attrs={"rows": 2}))
    publish = forms.BooleanField(label="Publish it now", required=False, help_text="Published versions are public and never change")

    def __init__(self, *args, can_publish=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["attachments"].widget.attrs["accept"] = ACCEPT
        if not can_publish:
            del self.fields["publish"]


class OptionForm(forms.Form):
    label = forms.CharField(label="Value", max_length=200)
    reason = forms.CharField(label="Reason", required=False)


class RetireForm(forms.Form):
    reason = forms.CharField(label="Reason")

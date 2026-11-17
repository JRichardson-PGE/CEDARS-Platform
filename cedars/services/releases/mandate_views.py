"""The register of CPUC-mandated data and calculation changes. Public: /mandates/ (every published entry, filterable,
also as CSV) and /mandates/<id>/ (an entry, its links and its history). CPUC system administrators: /mandates/manage/ (drafts and a new
entry) and /mandates/manage/<id>/ (change, link, publish, discard a draft; change a published entry with a reason)."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from cedars.core import pickers
from cedars.services.accounts.service import AccessDenied

from . import mandates
from .mandates import LINK_KINDS, MandateError
from .models import Mandate, MandateLink

DATE = {"widget": forms.DateInput(attrs={"type": "date"})}
PUBLIC_ONLY = "The register is public: enter only what the CPUC's public document says."


def _filters(request, entries):
    q, status, area = request.GET.get("q", "").strip(), request.GET.get("status", ""), request.GET.get("area", "")
    if q:
        entries = entries.filter(Q(reference__icontains=q) | Q(title__icontains=q) | Q(requirement__icontains=q))
    if status in Mandate.Status.values:
        entries = entries.filter(status=status)
    if area in Mandate.Area.values:
        entries = entries.filter(area=area)
    return entries, {"q": q, "status": status, "area": area}


@require_GET
def index(request):
    entries, filters = _filters(request, mandates.published())
    if request.GET.get("format") == "csv":
        r = HttpResponse(mandates.register_csv(entries), content_type="text/csv; charset=utf-8")
        r["Content-Disposition"] = 'attachment; filename="cedars_cpuc_mandated_changes.csv"'
        return r
    entries = list(entries)
    return render(
        request,
        "releases/mandates.html",
        {
            "entries": entries,
            "statuses": Mandate.Status.choices,
            "areas": Mandate.Area.choices,
            "can_edit": mandates.can_edit(request.user),
            **filters,
        },
    )


@require_GET
def detail(request, pk):
    m = get_object_or_404(mandates.published(), pk=pk)
    return render(
        request,
        "releases/mandate.html",
        {
            "m": m,
            "links": [(ln, mandates.link_target(ln)) for ln in m.links.all()],
            "history": mandates.history(m),
            "supersedes": m.supersedes.filter(published_at__isnull=False),
            "can_edit": mandates.can_edit(request.user),
        },
    )


# --- keeping the register


class MandateForm(forms.ModelForm):
    document_url = forms.URLField(
        label="Link to the document",
        max_length=400,
        required=False,
        assume_scheme="https",
        help_text=str(Mandate._meta.get_field("document_url").help_text),
    )
    reason = forms.CharField(
        label="Why it changes",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Needed once the entry is on the register: kept in the audit trail and shown in the entry's history.",
    )

    class Meta:
        model = Mandate
        fields = mandates.FIELDS
        widgets = {
            "issued_on": DATE["widget"],
            "effective_on": DATE["widget"],
            "implemented_on": DATE["widget"],
            "requirement": forms.Textarea(attrs={"rows": 4}),
            "status_note": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {"requirement": f"What it requires of CEDARS' data or calculations. {PUBLIC_ONLY}"}

    def __init__(self, *args, published: bool = False, **kw):
        super().__init__(*args, **kw)
        others = Mandate.objects.filter(published_at__isnull=False)
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        self.fields["superseded_by"].queryset = others
        self.fields["superseded_by"].required = False
        self.fields["superseded_by"].help_text = "With the Superseded status: the published entry that replaces it, if any"
        if not published:
            del self.fields["reason"]

    def _post_clean(self):  # the service checks the entry (mandates._clean); the model form only reads the values
        pass


class LinkForm(forms.Form):
    """Link a record: what carries the change, then that record picked from a list of the ones that exist and are public (a field by
    its name, from the dictionary's picker), not its number typed or its address pasted (UI review 3). Each kind has its own list,
    key_<kind>; core/pickers.js shows only the chosen kind's, and without scripts each says which kind it is for. A key given directly
    (key=, as the form took it before) is still read."""

    kind = forms.ChoiceField(
        label="Carried by", choices=[(k, v.label) for k, v in LINK_KINDS.items()], widget=forms.Select(attrs={"data-reveal": ""})
    )
    reason = forms.CharField(label="Why (for an entry on the register)", required=False, max_length=500)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for code, k in LINK_KINDS.items():
            help_text = f"When it is carried by {k.label[0].lower()}{k.label[1:]}."
            if k.choices is None:
                field = pickers.FieldField(label=k.noun, required=False, help_text=f"{help_text} Type part of its name, then pick it.")
            else:
                choices = k.choices()
                field = forms.ChoiceField(
                    label=k.noun,
                    required=False,
                    choices=[("", f"Choose a {k.noun.lower()}" if choices else f"No published {k.noun.lower()} yet"), *choices],
                    help_text=help_text,
                )
            self.fields[f"key_{code}"] = field
        self.order_fields(["kind", *(f"key_{c}" for c in LINK_KINDS), "reason"])

    @property
    def kind_fields(self):
        """(kind, bound field) for each kind's list, for the template's show-for-the-chosen-kind wrappers."""
        return [(code, self[f"key_{code}"]) for code in LINK_KINDS]

    def clean(self):
        d = super().clean()
        kind = d.get("kind")
        for code in LINK_KINDS:  # only the chosen kind's list counts: what the others hold (a script-less form shows them all) is ignored
            if code != kind:
                self._errors.pop(f"key_{code}", None)
        if kind in LINK_KINDS:
            key = d.get(f"key_{kind}") or (self.data.get("key") or "").strip()
            if not key and f"key_{kind}" not in self.errors:
                self.add_error(f"key_{kind}", f"Choose the {LINK_KINDS[kind].noun.lower()} to link.")
            d["key"] = key
        return d


def _require(request):
    if not mandates.can_edit(request.user):
        raise PermissionDenied("Only a CPUC system administrator keeps the register of CPUC-mandated changes.")


def _values(form) -> dict:
    return {k: form.cleaned_data[k] for k in mandates.FIELDS}


@login_required
def manage(request):
    """Drafts and a new entry."""
    _require(request)
    form = MandateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            m = mandates.create_draft(request.user, **_values(form))
        except (MandateError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, "Draft saved. Link where CEDARS carries it, then publish it on the register.")
            return redirect("mandates:edit", m.pk)
    return render(request, "releases/mandates_manage.html", {"form": form, "drafts": mandates.drafts()})


@login_required
def edit(request, pk):
    """One entry: its fields, its links; publish or discard a draft. A published entry's changes need a reason."""
    _require(request)
    m = get_object_or_404(Mandate.objects.prefetch_related("links"), pk=pk)
    published = not m.is_draft
    form = MandateForm(instance=m, published=published)
    link_form = LinkForm(auto_id="link_%s")
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "save":
                form = MandateForm(request.POST, instance=Mandate.objects.get(pk=m.pk), published=published)
                if form.is_valid():
                    mandates.update(request.user, m, reason=form.cleaned_data.get("reason", ""), **_values(form))
                    messages.success(request, "Saved." + (" The change is in the entry's history." if published else ""))
                    return redirect("mandates:edit", m.pk)
            elif action == "link":
                link_form = LinkForm(request.POST, auto_id="link_%s")
                if link_form.is_valid():
                    c = link_form.cleaned_data
                    link = mandates.add_link(request.user, m, c["kind"], c["key"], reason=c["reason"])
                    messages.success(request, f"Linked: {link.label}.")
                    return redirect("mandates:edit", m.pk)
            elif action == "unlink":
                link = get_object_or_404(MandateLink, pk=request.POST.get("link"), mandate=m)
                mandates.remove_link(request.user, link, reason=request.POST.get("reason", ""))
                messages.success(request, "Link removed.")
                return redirect("mandates:edit", m.pk)
            elif action == "publish":
                mandates.publish(request.user, m)
                messages.success(request, "Published on the register.")
                return redirect("mandates:detail", m.pk)
            elif action == "discard":
                mandates.discard_draft(request.user, m)
                messages.success(request, "Draft discarded.")
                return redirect("mandates:manage")
            else:
                raise Http404
        except (MandateError, AccessDenied) as e:
            if action == "save":
                form.add_error(None, str(e))
            elif action == "link":
                link_form.add_error(None, str(e))
            else:
                messages.error(request, str(e))
                return redirect("mandates:edit", m.pk)
    return render(
        request,
        "releases/mandate_edit.html",
        {
            "m": m,
            "form": form,
            "link_form": link_form,
            "links": [(ln, mandates.link_target(ln)) for ln in m.links.all()],
            "published": published,
        },
    )

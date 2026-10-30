import csv
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cedars.core import storage
from cedars.services.accounts.service import AccessDenied

from . import forms, service
from .models import AttributeList, AttributeOption, Collection, Document, DocumentFile

PER_PAGE = 25
CSV_COLUMNS = [
    "DocumentID",
    "Link",
    "Collection",
    "DocumentType",
    "Title",
    "Summary",
    "PublishedDate",
    "Version",
    "VersionDate",
    "Owner",
    "PAs",
    "Programs",
    "FirstYear",
    "LastYear",
    "StudyID",
    "WorkOrder",
    "Task",
]


def _can_see_unpublished_anywhere(user) -> bool:
    return user.is_authenticated and (service.is_admin(user) or service.readable(user).filter(published_at__isnull=True).exists())


def _search(request):
    form = forms.SearchForm(request.GET or None, show_status=_can_see_unpublished_anywhere(request.user))
    return form, service.search(request.user, form.search())


def _attribute_lists():
    """The lists documents carry (not the document types, nor the study register's own lists)."""
    return list(AttributeList.objects.filter(type_of="", collections__len__gt=0).prefetch_related("options"))


def library(request):
    """The library: every published document, and the unpublished ones the user may see, searchable and filterable by any of its
    details, with the result list as a file (BR-15.2). Public: no account needed."""
    form, results = _search(request)
    page = Paginator(results, PER_PAGE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    chosen = set(form.cleaned_data.get("options") or []) if form.is_bound and form.is_valid() else set()
    rows = []
    for doc in page.object_list:
        version = service.listed_version(doc, unpublished=doc.published_at is None)
        rows.append((doc, version))
    return render(
        request,
        "documents/library.html",
        {
            "form": form,
            "page": page,
            "rows": rows,
            "total": page.paginator.count,
            "query": query.urlencode(),
            "lists": [(al, [(o, o in chosen) for o in al.options.all()]) for al in _attribute_lists()],
            "advanced": bool(chosen)
            or any(request.GET.get(k) for k in ("published_from", "published_to", "program", "year", "study", "work_order")),
            "collections": service.collections_for(request.user, getattr(request, "organisation", None))
            if request.user.is_authenticated
            else [],
            "collection_labels": dict(Collection.choices),
        },
    )


def library_csv(request):
    """The result list as a file, from the same search (decision H): one row per document at its listed version, and one column per
    attribute list."""
    _, results = _search(request)
    lists = _attribute_lists()
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="documents.csv"'
    w = csv.writer(response, lineterminator="\n")
    w.writerow(CSV_COLUMNS + [al.name for al in lists])
    base = request.build_absolute_uri("/documents/")
    for doc in results:
        v = service.listed_version(doc, unpublished=doc.published_at is None)
        options = list(doc.attributes.all())
        w.writerow(
            [
                doc.pk,
                f"{base}{doc.pk}/",
                doc.get_collection_display(),
                doc.document_type.label,
                doc.title,
                doc.summary,
                doc.published_at.date().isoformat() if doc.published_at else "",
                v.number if v else "",
                (v.published_at or v.created_at).date().isoformat() if v else "",
                doc.owner.code,
                "; ".join(o.code for o in doc.pas.all()),
                "; ".join(p.prg_id for p in doc.programs.all()),
                doc.first_year or "",
                doc.last_year or "",
            ]
            + [doc.study.study_id if doc.study else "", doc.work_order.number if doc.work_order else "", doc.task.name if doc.task else ""]
            + ["; ".join(o.label for o in options if o.attribute_list_id == al.pk) for al in lists]
        )
    return response


def _document(request, pk) -> Document:
    doc = get_object_or_404(Document.objects.select_related("document_type", "owner", "created_by", "study", "work_order", "task"), pk=pk)
    if not service.can_read(request.user, doc):
        raise Http404
    return doc


def document(request, pk):
    """A document's permanent link: its details, the current version's files and every version before it."""
    doc = _document(request, pk)
    versions = service.visible_versions(request.user, doc)
    current = service.current_version(request.user, doc)
    options = list(doc.attributes.select_related("attribute_list").order_by("attribute_list__position", "position"))
    attributes = {}
    for o in options:
        attributes.setdefault(o.attribute_list.name, []).append(o.label)
    can_manage = service.can_manage(request.user, doc)
    return render(
        request,
        "documents/document.html",
        {
            "doc": doc,
            "current": current,
            "versions": versions,
            "attributes": attributes,
            "pas": [o for o in doc.pas.all()],
            "programs": list(doc.programs.all()),
            "can_manage": can_manage,
            "can_publish": service.can_publish(request.user, doc),
            "sees_unpublished": service.sees_unpublished(request.user, doc) if request.user.is_authenticated else False,
            "inline": {f.pk for v in versions for f in v.files.all() if service.opens_inline(f)},
            "show_downloads": can_manage,
        },
    )


@login_required
def downloads(request, pk):
    """Who downloaded or viewed the document's files, and when (BR-15.9): the audit trail's events, for those who manage it."""
    doc = _document(request, pk)
    if not service.can_manage(request.user, doc):
        raise Http404
    events = Paginator(service.downloads(doc).select_related("organisation"), 50).get_page(request.GET.get("page"))
    return render(request, "documents/downloads.html", {"doc": doc, "page": events})


def download(request, pk, name):
    """A file at its permanent address. ?view=1 opens a PDF, text or image in the browser; everything else downloads. Each one is an
    audit event."""
    f = get_object_or_404(DocumentFile.objects.select_related("version", "version__document"), pk=pk)
    doc = f.version.document
    if f.name != name or not service.can_read(request.user, doc):
        raise Http404
    if not f.version.published and not service.sees_unpublished(request.user, doc):
        raise Http404
    store = storage.store("uploads")
    if not store.exists(f.key):
        raise Http404
    inline = request.GET.get("view") == "1" and service.opens_inline(f)
    service.record_download(request.user, f, viewed=inline)
    url = store.download_url(f.key, f.name, inline=inline, content_type=f.content_type)
    if url:
        return HttpResponseRedirect(url)
    response = FileResponse(store.open(f.key), as_attachment=not inline, filename=f.name, content_type=f.content_type)
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _organisation(request):
    return getattr(request, "organisation", None)


@login_required
def new(request):
    """File a new document in one of the collections the acting organisation files in."""
    allowed = service.collections_for(request.user, _organisation(request))
    if not allowed:
        raise Http404
    collection = request.GET.get("collection") or request.POST.get("collection") or allowed[0]
    if collection not in allowed:
        raise Http404
    org = _organisation(request)
    can_publish = service.is_admin(request.user) or service.access.has_permission(request.user, org, service.CONFIRM)
    form = forms.NewDocumentForm(request.POST or None, request.FILES or None, collection=collection, can_publish=can_publish)
    if request.method == "POST" and form.is_valid():
        try:
            doc = service.create_document(
                request.user, org, collection, form.details(), form.uploads(), publish=form.cleaned_data.get("publish", False)
            )
        except (service.DocumentError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, "The document is filed." + ("" if doc.published_at else " It is not published yet."))
            return redirect(doc)
    return render(
        request,
        "documents/new.html",
        {"form": form, "collection": collection, "collections": [(c, Collection(c).label) for c in allowed]},
    )


@login_required
def change(request, pk):
    doc = _document(request, pk)
    if not service.can_manage(request.user, doc):
        raise Http404
    form = forms.ChangeDetailsForm(request.POST or None, collection=doc.collection, document=doc)
    if request.method == "POST" and form.is_valid():
        try:
            service.change_details(request.user, doc, form.details(), reason=form.cleaned_data["reason"])
        except (service.DocumentError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, "The details are changed.")
            return redirect(doc)
    return render(request, "documents/change.html", {"form": form, "doc": doc})


@login_required
def new_version(request, pk):
    doc = _document(request, pk)
    if not service.can_manage(request.user, doc):
        raise Http404
    form = forms.VersionForm(request.POST or None, request.FILES or None, can_publish=service.can_publish(request.user, doc))
    if request.method == "POST" and form.is_valid():
        try:
            v = service.add_version(
                request.user,
                _organisation(request),
                doc,
                form.uploads(),
                reason=form.cleaned_data["reason"],
                publish=form.cleaned_data.get("publish", False),
            )
        except (service.DocumentError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"Version {v.number} is added." + ("" if v.published else " It is not published yet."))
            return redirect(doc)
    latest = doc.versions.order_by("-number").first()
    return render(request, "documents/version_new.html", {"form": form, "doc": doc, "number": (latest.number if latest else 0) + 1})


@login_required
@require_POST
def publish(request, pk, number):
    doc = _document(request, pk)
    version = get_object_or_404(doc.versions, number=number)
    try:
        service.publish_version(request.user, version)
    except AccessDenied as e:
        raise Http404 from e
    except service.DocumentError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Version {number} is published.")
    return redirect(doc)


# --- value lists (BR-15.3: controlled lists kept by CPUC ED)


def value_lists(request):
    """Every list and its values, retired ones marked; CPUC ED's document administrators add, correct, retire and reinstate values."""
    lists = AttributeList.objects.prefetch_related("options")
    admin = request.user.is_authenticated and service.is_admin(request.user)
    return render(
        request,
        "documents/lists.html",
        {
            "lists": [(al, _used_by(al), al.options.all()) for al in lists],
            "admin": admin,
        },
    )


def _used_by(al: AttributeList) -> str:
    """Where a list is used: the collections whose documents carry it, a collection's document types, or the study register."""
    if al.collections:
        return ", ".join(Collection(c).label for c in al.collections)
    return Collection(al.type_of).label if al.type_of else "Evaluation studies"


def _back(al: AttributeList):
    return HttpResponseRedirect(f"/documents/lists/#list-{quote(al.code)}")


@login_required
@require_POST
def add_option(request, code):
    al = get_object_or_404(AttributeList, code=code)
    form = forms.OptionForm(request.POST)
    try:
        if not form.is_valid():
            raise service.DocumentError("Give the new value.")
        o = service.add_option(request.user, al, form.cleaned_data["label"], reason=form.cleaned_data.get("reason", ""))
    except (service.DocumentError, AccessDenied) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{al.name}: {o.label} is added.")
    return _back(al)


@login_required
@require_POST
def change_option(request, pk, action):
    o = get_object_or_404(AttributeOption.objects.select_related("attribute_list"), pk=pk)
    reason = request.POST.get("reason", "")
    try:
        if action == "rename":
            service.rename_option(request.user, o, request.POST.get("label", ""), reason=reason)
        elif action == "retire":
            service.retire_option(request.user, o, reason=reason)
        elif action == "reinstate":
            service.reinstate_option(request.user, o, reason=reason)
        else:
            raise Http404
    except (service.DocumentError, AccessDenied) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{o.attribute_list.name}: {o.label} is {'renamed' if action == 'rename' else action + 'd'}.")
    return _back(o.attribute_list)

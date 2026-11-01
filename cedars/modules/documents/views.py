import csv
import re
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponsePermanentRedirect, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from cedars.core import storage
from cedars.services.accessibility import service as accessibility
from cedars.services.accounts.service import AccessDenied

from . import comments, deliverables, forms, service, subscriptions
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
    "Stage",
]


def _can_see_unpublished_anywhere(user) -> bool:
    return user.is_authenticated and (service.is_admin(user) or service.readable(user).filter(published_at__isnull=True).exists())


def _search(request):
    form = forms.SearchForm(request.GET or None, show_status=_can_see_unpublished_anywhere(request.user))
    return form, service.search(request.user, form.search())


def _attribute_lists():
    """The lists documents carry (not the document types, nor the study register's own lists)."""
    return list(AttributeList.objects.filter(type_of="", collections__len__gt=0).prefetch_related("options"))


def _in_order(options) -> list:
    """A list's options in its own order, except a list of years or cycles ("2013-2015", "2021"), which reads oldest first whatever
    order the PDA held them in (UI review 2, #31)."""
    options = list(options)
    if options and all(o.label[:4].isdigit() for o in options):
        return sorted(options, key=lambda o: o.label)
    return options


def library(request):
    """The library: every published document, and the unpublished ones the user may see, searchable and filterable by any of its
    details, with the result list as a file (BR-15.2). Public: no account needed."""
    form, results = _search(request)
    page = Paginator(results, PER_PAGE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    chosen = set(form.cleaned_data.get("options") or []) if form.is_bound and form.is_valid() else set()
    q = form.cleaned_data.get("q", "") if form.is_bound and form.is_valid() else ""
    found = service.text_matches(request.user, q, page.object_list)
    rows = []
    for doc in page.object_list:
        version = service.listed_version(doc, unpublished=doc.published_at is None)
        extracts = [(f, _highlight(extract)) for f, extract in found.get(doc.pk, [])]
        rows.append((doc, version, deliverables.stage_label(doc, version) if doc.deliverable else "", extracts))
    home = not request.GET
    upcoming, recent = subscriptions.key_dates() if home else ([], [])
    return render(
        request,
        "documents/library.html",
        {
            "form": form,
            "page": page,
            "rows": rows,
            "total": page.paginator.count,
            "query": query.urlencode(),
            "lists": [(al, [(o, o in chosen) for o in _in_order(al.options.all())]) for al in _attribute_lists()],
            "advanced": bool(chosen)
            or any(request.GET.get(k) for k in ("published_from", "published_to", "program", "year", "study", "work_order")),
            "collections": service.collections_for(request.user, getattr(request, "organisation", None))
            if request.user.is_authenticated
            else [],
            "collection_labels": dict(Collection.choices),
            "home": home,
            "upcoming": upcoming,
            "recent": recent,
            "open_for_comment": subscriptions.open_for_comment() if home else [],
            "is_ed": deliverables.is_ed(request.user),
            "key_date_form": forms.KeyDateForm() if home and deliverables.is_ed(request.user) else None,
        },
    )


def _highlight(extract: str):
    """A text extract as HTML: the text escaped, each matched word in a mark element."""
    parts = []
    for i, piece in enumerate(extract.split(service.MARK_START)):
        if i == 0:
            parts.append(escape(piece))
            continue
        word, _, rest = piece.partition(service.MARK_END)
        parts.append(format_html("<mark>{}</mark>{}", word, rest))
    return mark_safe("".join(str(p) for p in parts))


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
            + [v.stage_label if v else ""]
            + ["; ".join(o.label for o in options if o.attribute_list_id == al.pk) for al in lists]
        )
    return response


def _document(request, pk) -> Document:
    doc = get_object_or_404(Document.objects.select_related("document_type", "owner", "created_by", "study", "work_order", "task"), pk=pk)
    if not service.can_read(request.user, doc):
        raise Http404
    return doc


def warn_inaccessible(request, version) -> None:
    """Tell the uploader what the accessibility check found in the version's files (BR-10.3), if anything."""
    if version is None:
        return
    text = accessibility.upload_warning("document", version.files.values_list("pk", flat=True))
    if text:
        messages.warning(request, text)


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
    shown = comments.shown(doc)
    counts = {}
    for c in shown:
        counts[c.version_id] = counts.get(c.version_id, 0) + 1
    for v in versions:
        v.comment_count = counts.get(v.pk, 0)
    requests = []
    if doc.deliverable:
        requests = [(r, deliverables.status(r), deliverables.responses(r)) for r in doc.response_requests.select_related("pa")]
    is_ed = deliverables.is_ed(request.user) if request.user.is_authenticated else False
    files = list(current.files.all()) if current else []
    checked = accessibility.for_refs("document", [f.pk for f in files])
    return render(
        request,
        "documents/document.html",
        {
            "doc": doc,
            "current": current,
            "current_files": [(f, checked.get(str(f.pk))) for f in files],
            "versions": deliverables.history(versions) if doc.deliverable else versions,
            "stage": deliverables.stage_label(doc, current) if doc.deliverable else "",
            "open_versions": comments.open_versions(versions),
            "requests": requests,
            "request_form": forms.RequestResponsesForm() if is_ed and doc.deliverable and deliverables.has_final_results(doc) else None,
            "responding_pas": deliverables.responding_pas(request.user, doc) if doc.deliverable else [],
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
    if not service.sees_version(request.user, f.version):
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
    """File a new document in one of the collections the acting organization files in."""
    allowed = service.collections_for(request.user, _organisation(request))
    if not allowed:
        raise Http404
    collection = request.GET.get("collection") or request.POST.get("collection") or allowed[0]
    if collection not in allowed:
        raise Http404
    org = _organisation(request)
    can_publish = service.is_admin(request.user) or service.access.has_permission(request.user, org, service.CONFIRM)
    # Upload a document on a study's page comes here with the study named (?study=<study ID>), filled in and shown above the form.
    study = forms.study_named(request.GET.get("study")) if collection in service.STUDY_COLLECTIONS else None
    form = forms.NewDocumentForm(
        request.POST or None,
        request.FILES or None,
        collection=collection,
        can_publish=can_publish,
        initial={"study": study.study_id} if study else None,
    )
    if request.method == "POST" and form.is_valid():
        try:
            doc = service.create_document(
                request.user,
                org,
                collection,
                form.details(),
                form.uploads(),
                publish=form.cleaned_data.get("publish", False),
                **form.workflow(),
            )
        except (service.DocumentError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, "The document is filed." + ("" if doc.published_at else " It is not published yet."))
            warn_inaccessible(request, doc.versions.order_by("-number").first())
            return redirect(doc)
    return render(
        request,
        "documents/new.html",
        {"form": form, "collection": collection, "collections": [(c, Collection(c).label) for c in allowed], "study": study},
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
    stages = deliverables.next_stages(request.user, doc)
    form = forms.VersionForm(
        request.POST or None,
        request.FILES or None,
        can_publish=service.can_publish(request.user, doc),
        stages=stages,
        placeholder=service.takes_placeholder(doc.collection, doc.document_type),
    )
    if request.method == "POST" and form.is_valid():
        try:
            v = service.add_version(
                request.user,
                _organisation(request),
                doc,
                form.uploads(),
                reason=form.cleaned_data["reason"],
                publish=form.cleaned_data.get("publish", False),
                stage=form.cleaned_data.get("stage", ""),
                comment_period=form.period(),
                placeholder=form.cleaned_data.get("placeholder", False),
            )
        except (service.DocumentError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"Version {v.number} is added." + ("" if v.published else " It is not published yet."))
            warn_inaccessible(request, v)
            return redirect(doc)
    latest = doc.versions.order_by("-number").first()
    return render(
        request,
        "documents/version_new.html",
        {"form": form, "doc": doc, "number": (latest.number if latest else 0) + 1, "current": deliverables.current(doc)},
    )


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


# --- the old sites' links (BR-15.10): the PDA's and PSR's addresses, mirrored under /documents/pda/ and /documents/psr/, so the old
# domains redirect with a fixed prefix (docs/LEGACY_LINKS.md)


def pda_document(request, pda_id, page=""):
    """#!/documents/<id>/view and #!/documents/<id>/comments/list on the PDA."""
    doc = get_object_or_404(Document, pda_id=pda_id)
    if not service.can_read(request.user, doc):
        raise Http404
    url = doc.get_absolute_url() + ("comments/" if page.startswith("comments") else "")
    return HttpResponsePermanentRedirect(url)


def pda_file(request, pda_id, name, how):
    """/api/view/<id>/<file> (opens in the browser) and /api/downloads/<id>/<file> on the PDA: the file under its PDA name, which is
    its name here unless it was cleaned or wrapped in a zip."""
    if how not in ("view", "downloads"):
        raise Http404
    files = DocumentFile.objects.filter(version__document__pda_id=pda_id).select_related("version__document")
    f = files.filter(legacy_name=name).first() or files.filter(name=name, legacy_name="").first()
    if f is None or not service.can_read(request.user, f.version.document):
        raise Http404
    view = how == "view" and service.opens_inline(f)
    return HttpResponsePermanentRedirect(f.get_absolute_url() + ("?view=1" if view else ""))


def pda_other(request, rest=""):
    """Any other PDA address (search, EM&V Docs, the home page): the library."""
    return HttpResponsePermanentRedirect("/documents/")


def psr_project(request, number):
    """#!/project-status-view-edit/<id> on the PSR: the study imported with that project number."""
    from .models import Study

    study = get_object_or_404(Study, number=number)
    return HttpResponsePermanentRedirect(study.get_absolute_url())


def psr_other(request, rest=""):
    """Any other PSR address (the project status list, budget status): the study register."""
    return HttpResponsePermanentRedirect("/documents/studies/")


def _legacy_link(request, paths: set[str], *, any_case: bool = False):
    """The redirect to the migrated document or file an old address names (models.LegacyLink), or None when none does."""
    from .models import LegacyLink

    match = Q(path__in=paths)
    for p in paths if any_case else ():
        match |= Q(path__iexact=p)
    link = LegacyLink.objects.filter(match).select_related("document", "file").first()
    if link is None:
        return None
    if not service.can_read(request.user, link.document):
        raise Http404
    if link.file is None:
        return HttpResponsePermanentRedirect(link.document.get_absolute_url())
    return HttpResponsePermanentRedirect(link.file.get_absolute_url() + ("?view=1" if service.opens_inline(link.file) else ""))


# The old CEDARS site's document addresses, by prefix: the collection its other pages (lists, sections) lead to
CEDARS_PREFIXES = {
    "/deer-resources/": "deer",
    "/documents/guidance-document/": "guidance",
    "/documents/standalone/": "pa",
    "/documents/history/": "program",
    "/documents/download/": "program",
}
CEDARS_RECORD = re.compile(r"^/documents/(history|download|guidance-document/download)/|/(file|resource)/\d+/")


def cedars_legacy(request, rest=""):
    """An address of CEDARS' Reports & Documents, DEER Resources or program documents: the migrated document or file it names
    (models.LegacyLink), else, for a list or a section, the library's collection; a record that was not migrated is not found."""
    path = request.path
    found = _legacy_link(request, {path, path.rstrip("/"), path.rstrip("/") + "/"})
    if found is not None:
        return found
    if CEDARS_RECORD.search(path):
        raise Http404
    collection = next(c for prefix, c in CEDARS_PREFIXES.items() if path.startswith(prefix) or path == prefix.rstrip("/"))
    return HttpResponsePermanentRedirect(f"/documents/?collection={collection}")


# The 2006-2013 EM&V site (www.energydataweb.com/cpuc, the PDA's predecessor, on IIS: addresses in any case). Its document pages carry
# the deliverableID the PDA kept, so they lead to the PDA's migrated documents.
EDW_PAGE = re.compile(r"^/cpuc/(deliverableview|deliverabledownload|search|comment)\.aspx$")
EDW_PDA_FILE = re.compile(r"^/cpucfiles/pdadocs/(\d+)/(.+)$", re.IGNORECASE)


def edw_archive(request, rest=""):
    """An address of the 2006-2013 EM&V site (the edge sends its /cpuc and /cpucFiles addresses here, under /documents/edw/):
    deliverableView.aspx?did=<id> and /cpucFiles/pdaDocs/<id>/<file> (where it sent them) the PDA document's main file; search.aspx,
    deliverableDownload.aspx and comment.aspx with a did its page (comments for comment.aspx); a home-page file migrated from the
    site (cedars_import, kept under /edw) that file; any other page the EM&V collection. A document or file not migrated is not found."""
    from .cedars_import import ARCHIVE_PREFIX

    old = "/" + rest
    page = EDW_PAGE.match(old.lower())
    if page:
        params = {k.lower(): v for k, v in request.GET.items()}
        did = (params.get("did") or params.get("delivid") or "").strip()
        if not did.isdigit():
            return HttpResponsePermanentRedirect(f"/documents/?collection={Collection.EMV}")
        doc = get_object_or_404(Document, pda_id=int(did))
        if not service.can_read(request.user, doc):
            raise Http404
        if page.group(1) == "deliverableview":
            main = DocumentFile.objects.filter(version__document=doc, position=0).order_by("-version__number").first()
            if main is None:
                raise Http404
            return HttpResponsePermanentRedirect(main.get_absolute_url() + ("?view=1" if service.opens_inline(main) else ""))
        return HttpResponsePermanentRedirect(doc.get_absolute_url() + ("comments/" if page.group(1) == "comment" else ""))
    pda = EDW_PDA_FILE.match(old)
    if pda:
        return pda_file(request, int(pda.group(1)), pda.group(2), "view")
    found = _legacy_link(request, {ARCHIVE_PREFIX + old}, any_case=True)
    if found is not None:
        return found
    if old.lower().startswith("/cpucfiles/"):
        raise Http404
    return HttpResponsePermanentRedirect(f"/documents/?collection={Collection.EMV}")

"""Pages of public comment (BR-15.4) and of a deliverable's PA responses (BR-15.11). Reading comments and the comment-and-response report
needs no account, as on the PDA; commenting needs one."""

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cedars.core import storage
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import comments, deliverables, forms, service
from .models import Comment, Document, Study, WorkOrder
from .views import _document, _organisation


def _comment_page(request, doc: Document, form=None):
    versions = service.visible_versions(request.user, doc)
    open_versions = comments.open_versions(versions)
    shown = list(comments.shown(doc))
    by_version = {}
    for c in shown:
        by_version.setdefault(c.version_id, []).append(c)
    responders = comments.responder_organisations(request.user, doc)
    sections = [
        (v, by_version.get(v.pk, []), comments.is_open(v))
        for v in deliverables.history(versions)
        if getattr(v, "comment_period", None) is not None or v.pk in by_version
    ]
    if form is None and request.user.is_authenticated and open_versions:
        form = forms.CommentForm(versions=open_versions)
    return render(
        request,
        "documents/comments.html",
        {
            "doc": doc,
            "sections": sections,
            "form": form,
            "open_versions": open_versions,
            "responders": responders,
            "respond_form": forms.RespondForm(organisations=responders) if responders else None,
            "can_manage": service.can_manage(request.user, doc),
            "withdrawable": {c.pk for c in shown if comments.can_withdraw(request.user, c)},
            "total": len(shown),
        },
    )


def document_comments(request, pk):
    """Every version's comment period and the comments and questions posted in it, with their responses, oldest first."""
    return _comment_page(request, _document(request, pk))


@login_required
@require_POST
def post_comment(request, pk):
    doc = _document(request, pk)
    versions = comments.open_versions(service.visible_versions(request.user, doc))
    form = forms.CommentForm(request.POST, request.FILES, versions=versions)
    if not versions:
        messages.error(request, "Comments on this document are closed.")
        return redirect("documents:comments", pk=doc.pk)
    if form.is_valid():
        d = form.cleaned_data
        version = next(v for v in versions if str(v.number) == d["version"])
        f = d.get("attachment")
        try:
            comments.post(
                request.user,
                _organisation(request),
                version,
                comments.NewComment(
                    kind=d["kind"],
                    subject=d["subject"],
                    section=d.get("section") or "",
                    text=d["text"],
                    attachment=service.Upload(f.name, f) if f else None,
                ),
            )
        except (service.DocumentError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, "Your comment is posted. It is public, with your name and organization.")
            return redirect("documents:comments", pk=doc.pk)
    return _comment_page(request, doc, form)


def _comment(request, pk) -> Comment:
    c = get_object_or_404(Comment.objects.select_related("version__document"), pk=pk, withdrawn_at__isnull=True)
    if not service.can_read(request.user, c.version.document):
        raise Http404
    return c


@login_required
@require_POST
def withdraw_comment(request, pk):
    c = _comment(request, pk)
    try:
        comments.withdraw(request.user, c)
    except service.DocumentError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Your comment is withdrawn.")
    return redirect("documents:comments", pk=c.version.document_id)


@login_required
@require_POST
def respond(request, pk):
    c = _comment(request, pk)
    doc = c.version.document
    form = forms.RespondForm(request.POST, organisations=comments.responder_organisations(request.user, doc))
    if not form.is_valid():
        messages.error(request, "Write the response (up to 2,000 characters).")
        return redirect("documents:comments", pk=doc.pk)
    try:
        comments.respond(request.user, form.cleaned_data["organisation"], c, form.cleaned_data["text"])
    except AccessDenied as e:
        raise Http404 from e
    except service.DocumentError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Your response to “{c.subject}” is posted.")
    return HttpResponseRedirect(f"/documents/{doc.pk}/comments/#comment-{c.pk}")


def comment_file(request, pk, name):
    c = _comment(request, pk)
    if not c.file_name or c.file_name != name:
        raise Http404
    store = storage.store("uploads")
    if not store.exists(c.file_key):
        raise Http404
    url = store.download_url(c.file_key, c.file_name, content_type=c.file_type)
    if url:
        return HttpResponseRedirect(url)
    response = FileResponse(store.open(c.file_key), as_attachment=True, filename=c.file_name, content_type=c.file_type)
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def set_period(request, pk, number):
    doc = _document(request, pk)
    version = get_object_or_404(doc.versions, number=number)
    form = forms.PeriodForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the dates comments open and close.")
        return redirect("documents:comments", pk=doc.pk)
    d = form.cleaned_data
    try:
        comments.set_period(request.user, version, d["opens"], d["closes"], reason=d.get("reason") or "")
    except AccessDenied as e:
        raise Http404 from e
    except service.DocumentError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Comments on version {number} are open {d['opens']:%Y-%m-%d} to {d['closes']:%Y-%m-%d}.")
    return redirect("documents:comments", pk=doc.pk)


def _report(request, qs, name):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    w = csv.writer(response, lineterminator="\n")
    w.writerow(comments.REPORT_COLUMNS)
    for row in comments.report(qs, request.build_absolute_uri("/").rstrip("/")):
        w.writerow(row)
    return response


def _readable_comments(request):
    return Comment.objects.filter(
        withdrawn_at__isnull=True, version__published_at__isnull=False, version__document__in=service.readable(request.user)
    )


def document_report(request, pk):
    """The comment-and-response report of one document (the PDA's Comment and Resolution Report)."""
    doc = _document(request, pk)
    return _report(request, _readable_comments(request).filter(version__document=doc), f"comments_document_{doc.pk}.csv")


def study_report(request, number):
    s = get_object_or_404(Study, number=number)
    return _report(request, _readable_comments(request).filter(version__document__study=s), f"comments_study_{s.study_id}.csv")


def work_order_report(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    return _report(request, _readable_comments(request).filter(version__document__work_order=wo), f"comments_work_order_{wo.number}.csv")


# --- the PAs' responses to the recommendations (BR-15.11)


@login_required
@require_POST
def request_responses(request, pk):
    doc = _document(request, pk)
    form = forms.RequestResponsesForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose the PAs and the date their responses are due.")
        return redirect(doc)
    d = form.cleaned_data
    try:
        deliverables.request_responses(request.user, doc, list(d["pas"]), d["due"], reason=d.get("reason") or "")
    except AccessDenied as e:
        raise Http404 from e
    except service.DocumentError as e:
        messages.error(request, str(e))
    else:
        codes = ", ".join(o.code for o in d["pas"])
        messages.success(request, f"{codes} {'is' if len(d['pas']) == 1 else 'are'} asked to respond by {d['due']:%Y-%m-%d}.")
    return HttpResponseRedirect(f"{doc.get_absolute_url()}#responses-h")


@login_required
def new_response(request, pk):
    doc = _document(request, pk)
    pas = deliverables.responding_pas(request.user, doc)
    if not pas:
        raise Http404
    can_publish = any(access.has_permission(request.user, pa, service.CONFIRM) for pa in pas)
    form = forms.ResponseForm(request.POST or None, request.FILES or None, pas=pas, can_publish=can_publish)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            v = deliverables.file_response(
                request.user, d["pa"], doc, form.uploads(), reason=d.get("reason") or "", publish=d.get("publish", False)
            )
        except (service.DocumentError, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(
                request, f"{d['pa'].code}'s response is filed as version {v.number}." + ("" if v.published else " It is not published yet.")
            )
            return redirect(doc)
    requests = [(r, deliverables.status(r)) for r in doc.response_requests.filter(pa__in=pas).select_related("pa")]
    return render(request, "documents/response_new.html", {"doc": doc, "form": form, "requests": requests})

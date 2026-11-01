"""Pages for saved, shared and published views (BR-07.4; logic in saved.py): the Saved views list, one view's page with its sharing and
publication, the save form every dashboard page and the records page carry, and the records page's default columns."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied

from . import saved
from .models import SavedView

PAGE_LIST = 8  # views of a page listed on the page itself; the rest are on Saved views


def page_context(request, page: str, query: str | None) -> dict:
    """The saved-views part of a dashboard or records page: the views of this page the person may open, and (signed in, with a query
    the page could show) the save form, which can replace one of their own views of the page."""
    views = list(saved.for_page(request.user, page)[: PAGE_LIST + 1])
    signed_in = request.user.is_authenticated
    return {
        "saved_views": {
            "page": page,
            "query": query,
            "views": [(v, saved.url(v)) for v in views[:PAGE_LIST]],
            "more": len(views) > PAGE_LIST,
            "own": list(SavedView.objects.filter(owner=request.user, page=page, published_at__isnull=True)) if signed_in else [],
            "can_save": signed_in and query is not None,
        }
    }


@require_GET
def view_list(request):
    user = request.user
    signed_in = user.is_authenticated
    published = saved.SavedView.objects.filter(published_at__isnull=False)
    context = {
        "pages": saved.pages(),
        "published": [(v, saved.url(v)) for v in published],
        "own": [(v, saved.url(v)) for v in SavedView.objects.filter(owner=user).prefetch_related("shared_with")] if signed_in else [],
        "shared": [(v, saved.url(v)) for v in saved.shared_with_me(user)] if signed_in else [],
        "waiting": [(v, saved.url(v)) for v in saved.waiting_for_publication()] if signed_in and access.is_system_admin(user) else [],
        "default_columns": saved.default_columns(user),
    }
    return render(request, "data/views.html", context)


@login_required
@require_POST
def view_save(request):
    """Save a page's current choices as a new view, or over one of the person's own views of that page (`replace`)."""
    page, query = request.POST.get("page", ""), request.POST.get("query", "")
    back = saved.path(page) + (f"?{query}" if query else "") if page in saved.pages() else "/data/views/"
    try:
        if request.POST.get("replace"):
            view = get_object_or_404(SavedView, pk=request.POST["replace"], owner=request.user)
            if view.page != page:
                raise saved.ViewRefused("A view can only be replaced by choices from its own page.")
            saved.replace(request.user, view, query=query)
            messages.success(request, f"Saved the page's choices as {view.name}.")
        else:
            view = saved.save(
                request.user, name=request.POST.get("name", ""), page=page, query=query, description=request.POST.get("description", "")
            )
            messages.success(request, f"Saved as {view.name}. Share it from here, or open it again from Saved views.")
    except (saved.ViewRefused, AccessDenied) as e:
        messages.error(request, f"The view was not saved: {e}")
        return redirect(back)
    return redirect("data:view", view.pk)


ACTIONS = {
    "rename": lambda r, v: saved.rename(r.user, v, name=r.POST.get("name", ""), description=r.POST.get("description", "")),
    "share": lambda r, v: saved.share_with(r.user, v, r.POST.get("email", "")),
    "unshare": lambda r, v: saved.unshare(r.user, v, get_object_or_404(v.shared_with, pk=r.POST.get("person") or 0)),
    "organisation": lambda r, v: saved.set_organisation(r.user, v, _organisation(r)),
    "copy": lambda r, v: saved.copy(r.user, v),
    "request": lambda r, v: saved.request_publication(r.user, v, r.POST.get("note", "")),
    "publish": lambda r, v: saved.publish(r.user, v, r.POST.get("reason", "")),
    "decline": lambda r, v: saved.decline_publication(r.user, v, r.POST.get("reason", "")),
    "unpublish": lambda r, v: saved.unpublish(r.user, v, r.POST.get("reason", "")),
}
DONE = {
    "rename": "Saved the name and description.",
    "share": "Shared. They are told on their notifications page.",
    "unshare": "No longer shared with them.",
    "organisation": "Saved who can open it.",
    "request": "Asked for it to be published. A CPUC system administrator decides, and you are told.",
    "publish": "Published: everyone can open it now, signed in or not.",
    "decline": "Declined. The owner is told why.",
    "unpublish": "Unpublished. Only the people it is shared with can open it now.",
}


def _organisation(request):
    code = request.POST.get("organisation", "")
    if not code:
        return None
    org = next((o for o in request.user_organisations if str(o.pk) == code), None)
    if org is None:
        raise AccessDenied("You can share a view only with an organization you are a member of.")
    return org


@require_http_methods(["GET", "POST"])
def view_page(request, pk):
    view = get_object_or_404(SavedView.objects.select_related("owner", "organisation", "published_by"), pk=pk)
    if not saved.can_see(request.user, view):
        raise Http404  # a view not shared with you is not found, not forbidden
    if request.method == "POST":
        if not request.user.is_authenticated:
            from django.urls import reverse
            from django.utils.http import urlencode

            return redirect(reverse("account_login") + "?" + urlencode({"next": request.path}))
        action = request.POST.get("action", "")
        try:
            if action == "delete":
                name = view.name
                saved.delete(request.user, view)
                messages.success(request, f"Deleted {name}.")
                return redirect("data:views")
            if action not in ACTIONS:
                raise saved.ViewRefused("That is not something this page does.")
            result = ACTIONS[action](request, view)
        except (saved.ViewRefused, AccessDenied) as e:
            messages.error(request, str(e))
            return redirect("data:view", view.pk)
        if action == "copy":
            messages.success(request, f"Saved a copy to your views as {result.name}.")
            return redirect("data:view", result.pk)
        messages.success(request, DONE[action])
        return redirect("data:view", view.pk)
    user = request.user
    owner = user.is_authenticated and view.owner_id == user.pk
    admin = user.is_authenticated and access.is_system_admin(user)
    insider = owner or admin or (user.is_authenticated and saved.shared_with_me(user).filter(pk=view.pk).exists())
    context = {
        "view": view,
        "open": saved.url(view),
        "page_label": saved.pages().get(view.page, view.page),
        "choices": saved.describe(view),
        "owner": owner,
        "admin": admin,
        "insider": insider,  # sees who saved it and who it is shared with; a published view's public readers do not
        "people": list(view.shared_with.order_by("email")) if insider else [],
        "organisations": list(request.user_organisations) if owner else [],
        "history": list(saved.history(view)) if owner or admin else [],
    }
    return render(request, "data/view.html", context)


@login_required
@require_POST
def records_default_columns(request):
    """Make the records page's current columns the person's default, or (`clear`) go back to the key columns."""
    from urllib.parse import parse_qs

    from . import query as dq
    from . import records

    qs = request.POST.get("query", "")
    if request.POST.get("clear"):
        saved.clear_default_columns(request.user)
        messages.success(request, "Your records page opens on the key columns again.")
        return redirect(f"/data/records/?{qs}")
    try:
        rq = records.from_params(parse_qs(qs))
        cat = records.catalog(dq.files_of(rq.query))
        columns, _ = records.chosen(cat, rq.params)
        saved.set_default_columns(request.user, columns)
    except (dq.QueryError, saved.ViewRefused) as e:
        messages.error(request, f"Your default columns were not saved: {e}")
        return redirect(f"/data/records/?{qs}")
    messages.success(
        request, f"These {len(columns)} columns are now your default: the records page starts from them when a link names none."
    )
    return redirect(f"/data/records/?{qs}")

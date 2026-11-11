"""The error pages, in the site's layout (UI review 3, U4): the header, one plain sentence, a search box, and links to the front page,
Help and My queue.

A page that does not exist and a page the visitor's account cannot open get the same page and the same sentence (Jake, 2026-11-11),
so nobody has to tell the two apart, and a refusal says nothing about whether the page is there. The status code stays what it was
(404 or 403). The 500 page renders with no context at all: whatever failed (the database, a context processor) must not take the
error page down with it. A form refused for its security token (CSRF) says why in words and how to send it again."""

import re

from django.http import HttpResponse, HttpResponseServerError, JsonResponse
from django.shortcuts import render
from django.template import loader

# Words worth searching for in an address: its last part that is not a number or a code (/admin/retention/ -> "retention").
_WORD = re.compile(r"^[a-z][a-z-_]*$")


def _search_words(path: str) -> str:
    for part in reversed([p for p in path.split("/") if p]):
        if _WORD.match(part):
            return part.replace("-", " ").replace("_", " ")
    return ""


def _is_api(request) -> bool:
    return request.path.startswith("/api/")


def _unavailable(request, status: int) -> HttpResponse:
    if _is_api(request):  # an API client reads JSON, as the API's own refusals are
        detail = "Not found." if status == 404 else "You do not have permission to perform this action."
        return JsonResponse({"detail": detail}, status=status)
    context = {"words": _search_words(request.path), "path": request.get_full_path()}
    return render(request, "core/error.html", context, status=status)


def not_found(request, exception=None):
    return _unavailable(request, 404)


def permission_denied(request, exception=None):
    return _unavailable(request, 403)


def csrf_failure(request, reason=""):
    """CSRF_FAILURE_VIEW: a form sent without its security token, or with an old one (the page was open across a sign-out)."""
    return render(request, "core/form_expired.html", {"path": request.get_full_path()}, status=403)


def server_error(request):
    """Rendered with no context and no request: the database or a context processor may be what failed."""
    try:
        body = loader.get_template("500.html").render()
    except Exception:  # the last resort must not fail
        body = "<!doctype html><title>Something went wrong · CEDARS</title><h1>Something went wrong</h1><p>Please try again later.</p>"
    return HttpResponseServerError(body)

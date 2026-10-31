"""The (?) button beside each page's heading, and the dialog it opens (cedars/core/page_help.py). A builtin library (settings TEMPLATES),
so every template can write `{% page_help "budgets/filing" %}` after its heading without loading it."""

from urllib.parse import urlencode

from django import template
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import format_html

from cedars.core import page_help as pages

register = template.Library()


def help_url(key: str, request=None) -> str:
    url = reverse("core:help_page", args=[key])
    if request is not None:
        url += "?" + urlencode({"from": request.get_full_path()})
    return url


@register.simple_tag(takes_context=True)
def page_help(context, key, variant=""):
    """The button. Without scripts it is a link to the page's help on a page of its own; core/tour.js makes it open the dialog that
    base.html renders at the end of the page ({% page_help_dialog %})."""
    entry = pages.lookup(key, str(variant or ""))
    if entry is None:
        raise template.TemplateSyntaxError(f"No page help for {key!r}: add it to the app's page_help.yaml")
    request = context.get("request")
    if request is not None:
        request.page_help = entry
    return format_html(
        '<a class="page-help" href="{}" data-page-help aria-label="Help with this page: a guide and a tour">'
        '<span aria-hidden="true">?</span></a>',
        help_url(entry.key, request),
    )


@register.simple_tag(takes_context=True)
def page_help_dialog(context):
    request = context.get("request")
    entry = getattr(request, "page_help", None)
    if entry is None:
        return ""
    from cedars.core import help as site_help

    return render_to_string(
        "core/_page_help.html",
        {
            "entry": entry,
            "tour": pages.steps_for(entry, request),
            "guide": site_help.BY_SLUG.get(entry.see),
            "help_url": help_url(entry.key, request),
        },
        request=request,
    )

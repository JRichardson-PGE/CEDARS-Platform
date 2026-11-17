import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()
URL = re.compile(r"https?://[^\s<>\"']+")
TRAILING = ".,;:)!?"


@register.filter
def step_text(text: str, site: str = "") -> str:
    """A step's words with each address a link that opens in a new tab, so the test stays open beside the site. An address on
    the site the scripts were written with points at this site instead."""
    text = text or ""
    out, at = [], 0
    for m in URL.finditer(text):
        url = m.group(0)
        tail = ""
        while url and url[-1] in TRAILING:
            url, tail = url[:-1], url[-1] + tail
        href, shown = url, url
        if site and (url == site or url.startswith(site + "/")):
            href = url[len(site) :] or "/"
            shown = href
        out.append(escape(text[at : m.start()]))
        out.append(
            f'<a href="{escape(href)}" target="_blank" rel="noopener">{escape(shown)}<span class="sr-only"> (opens in a new tab)</span></a>'
        )
        out.append(escape(tail))
        at = m.end()
    out.append(escape(text[at:]))
    return mark_safe("".join(out))  # noqa: S308 (every part is escaped above)


@register.filter
def outcome_class(outcome: str) -> str:
    return {"passed": "ok", "failed": "bad", "blocked": "warn", "na": ""}.get(outcome or "", "")

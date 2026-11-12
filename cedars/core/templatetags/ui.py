"""Template filters behind the shared components (docs/DESIGN.md): a due date's urgency in words, a status's badge tone, the one way a
date, a date and time, a person and a filing cycle are written. Loaded on every template (a builtin in settings.TEMPLATES).

Words, never color alone: a badge always carries its status text, and a due cell says "Passed 7 months ago" or "Due in 12 days" under the
date, so the urgency is read by everyone (WCAG 1.4.1)."""

from django import template
from django.template.defaultfilters import date as django_date
from django.utils import timezone
from django.utils.html import escape, format_html
from django.utils.timesince import timesince, timeuntil

register = template.Library()

# Submission version statuses (services.submissions) and the other words pages put in a badge, by tone.
_TONES = {
    "ok": {
        "succeeded",
        "finished",
        "published",
        "confirmed",
        "approved",
        "accepted",
        "active",
        "effective",
        "complete",
        "completed",
        "done",
        "paid",
    },
    "warn": {"draft", "pending", "waiting", "open", "overdue", "expiring", "late"},
    "bad": {"rejected", "declined", "failed", "refused", "error", "revoked", "withdrawn", "purged"},
    "info": {"validated", "queued", "running", "submitted", "imported", "new", "under review"},
}


@register.filter
def status_tone(status) -> str:
    """The badge tone for a status word or code: ok, warn, bad, info, or empty for a neutral badge ("Not started")."""
    key = str(status or "").strip().lower()
    for tone, words in _TONES.items():
        if key in words:
            return tone
    return ""


@register.filter
def due_words(due_at, now=None) -> str:
    """ "Due in 12 days", "Due today", or "Passed 7 months ago" for a deadline's due_at (one unit, the largest)."""
    if due_at is None:
        return ""
    now = now or timezone.now()
    if due_at.date() == now.date():
        return "Due today"
    if due_at < now:
        return f"Passed {timesince(due_at, now, depth=1)} ago"
    return f"Due in {timeuntil(due_at, now, depth=1)}"


@register.filter(expects_localtime=True)
def date_words(value) -> str:
    """A date as every record shows it: ISO, "2026-10-31" (UI review 2, 2026-10-31). Blank for none."""
    return django_date(value, "Y-m-d") if value else ""


@register.filter(expects_localtime=True)
def datetime_words(value) -> str:
    """A date and time, "2026-10-31 14:56 PDT", only where the minute matters: runs, uploads, jobs, logs and notifications. Everything
    else (a confirmation, a publication, a version) shows date_words."""
    return django_date(value, "Y-m-d H:i T") if value else ""


@register.filter
def cycle_label(cycle) -> str:
    """A filing cycle's years for people: "2024-2027", or "2021" for a one-year cycle (never "2021-2021"). Takes a FilingCycle or
    its "first-last" string; blank for none."""
    if not cycle:
        return ""
    first, last = (cycle.first_year, cycle.last_year) if hasattr(cycle, "first_year") else (str(cycle).split("-") + [None])[:2]
    return str(first) if last in (None, "", first) or str(first) == str(last) else f"{first}-{last}"


@register.filter
def person_name(user) -> str:
    """A person's name, or their email address when no name is recorded. Blank for none (callers add their own default)."""
    if not user:
        return ""
    return getattr(user, "name", "") or getattr(user, "email", "") or str(user)


@register.filter
def person(user):
    """A person as the pages show them: the name, with the email address after it in small muted text, or the email alone when no
    name is recorded (UI review 2: names before emails). Blank for none."""
    if not user:
        return ""
    name, email = getattr(user, "name", ""), getattr(user, "email", "")
    if name and email:
        return format_html('{} <span class="small muted">{}</span>', name, email)
    return escape(name or email or str(user))


@register.filter
def action_words(action, verb_only=False) -> str:
    """An audit action in words ("Program change requested"; with an argument, "Requested"): services.audit.words."""
    from cedars.services import audit

    return audit.words(str(action or ""), verb_only=bool(verb_only))


@register.filter
def module_names(keys) -> str:
    """An API token's modules in words ("Claims, Impact profiles"), from permissions.MODULES."""
    from cedars.services.accounts import permissions

    return ", ".join(permissions.MODULES.get(k, str(k)) for k in keys or [])


@register.filter
def audit_rows(event) -> list[tuple[str, str, str]]:
    """An audit event's before and after as rows (field, before, after), one per field either side names, for a small table (UI review
    2, #7: they printed as Python dict text on one line). Field names read as words, in US spelling ("organization"); a nested value
    prints as JSON; a field absent on one side is blank there."""
    import json

    def as_dict(state):
        if state in (None, "", {}):
            return {}
        return state if isinstance(state, dict) else {"value": state}

    def shown(value):
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True, default=str)
        return str(value)

    before, after = as_dict(event.before), as_dict(event.after)
    keys = list(before) + [k for k in after if k not in before]
    return [(str(k).replace("_", " ").replace("organisation", "organization"), shown(before.get(k)), shown(after.get(k))) for k in keys]


@register.inclusion_tag("core/_before_upload.html", takes_context=True)
def before_upload(
    context, template="", template_label="", fields="", fields_label="", rule_set="", rules_note="", guide="", guide_label=""
):
    """The "Before you upload" line under a file control (navigation review D3): the template, the field definitions, the rules the
    file will be checked against and the guide section, each part only when the form has one.

    template: the template's URL; fields: "module.Table" (the data dictionary's fields of that upload table) or a URL; rule_set: the
    code of the rule set in force (rules.in_force: its page when one is published, else "none published yet"), or rules_note: the words
    to say instead (e.g. the engine checks the file); guide: a Help guide's slug, with an #anchor if any, or a URL."""
    from django.urls import NoReverseMatch, reverse

    parts = []
    if template:
        parts.append({"url": template, "label": template_label or "the template"})
    if fields:
        url = fields
        if "." in fields and "/" not in fields:
            url = f"{reverse('dictionary:index')}?table={fields}"
        parts.append({"url": url, "label": fields_label or "the field definitions"})
    if rule_set:
        from cedars.services.rules import service as rules

        rs = rules.in_force(rule_set)
        user = getattr(context.get("request"), "user", None)
        if rs is not None and user is not None and user.is_authenticated:
            parts.append({"url": reverse("rules:detail", args=[rs.pk]), "label": f"the rules it is checked against, {rs.label}"})
        elif rs is None:
            parts.append({"text": "no rule set is published for it yet"})
    if rules_note:
        parts.append({"text": rules_note})
    if guide:
        url = guide
        if "/" not in guide:
            slug, _, anchor = guide.partition("#")
            try:
                url = reverse("core:help_guide", args=[slug]) + (f"#{anchor}" if anchor else "")
            except NoReverseMatch:
                url = ""
        if url:
            parts.append({"url": url, "label": guide_label or "the user guide"})
    return {"parts": parts}


@register.inclusion_tag("core/_section_nav.html", takes_context=True)
def section_nav(context, menu: str):
    """A section's sub-navigation (navigation review D4): the items of one header menu the visitor sees, as the sub-navigation under the
    header on that section's pages. Nothing when the visitor sees fewer than two of them (a signed-out visitor on a public page)."""
    from cedars.core import nav

    shown = next((m for m in context.get("menus") or () if m.key == menu), None)
    links = [x for _, items in shown.groups for x in items] if shown and not shown.link else []
    request = context.get("request")
    here = nav.view_name(request) if request else ""
    best = nav.holder(here, links) if here else None
    out = []
    for item, url in links:
        current = ""
        if best and best[1] == url:
            current = "page" if request.path == url else "true"
        out.append({"label": item.label, "url": url, "current": current})
    return {"label": shown.label if shown else "", "links": out if len(out) > 1 else []}


@register.inclusion_tag("ui/pager.html")
def pager(page, query="", label="Pages", where="", previous="Previous", next="Next", anchor=""):
    """A long list's pager (docs/DESIGN.md, "Tables"): drawn above the table (where="above") and again below it (where="below"), so a
    person never scrolls the whole page to turn it. First and Last appear once they are more than one page away. Nothing when there is
    one page. previous / next name the two directions where "Newer" and "Older" say it better."""
    from cedars.core import tables

    if page is None or page.paginator.num_pages <= 1:
        return {"show": False}
    links = tables.pager_links(page, query or "", anchor)
    name = label if not where else f"{label}, {'above' if where == 'above' else 'below'} the list"
    return {"show": True, "name": name, "previous_word": previous, "next_word": next, "where": where, **links}


@register.simple_tag(takes_context=True)
def page_link(context, url_name: str, text: str):
    """A link to a page for a reader it opens for; for anyone else the page's name and who can open it (UI review 3, U4), so a Help
    guide never leads to "Page not available". Who may open what: cedars/core/page_access.py."""
    from django.urls import reverse

    from cedars.core import page_access

    request = context.get("request")
    if request is None or page_access.can_open(request, url_name):
        return format_html('<a href="{}">{}</a>', reverse(url_name), text)
    who = page_access.who_opens(url_name)
    return format_html('<span class="page-name">{}</span> <span class="who-opens">(for {})</span>', text, who)

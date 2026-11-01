"""`{% filing_comments v %}`: the comments section of a filing version's page (BR-09.5), the same on Budgets, Claims and Monthly reports.
Shown only to the people who may take part (the filing organization and CPUC staff); everyone else sees nothing of it."""

from django import template

from cedars.core import files as filetypes

from .. import comments as service

register = template.Library()


@register.inclusion_tag("submissions/_comments.html", takes_context=True)
def filing_comments(context, v):
    request = context.get("request")
    user = getattr(request, "user", None)
    org = service.side(user, v.submission)
    if org is None:
        return {"show": False}
    return {
        "show": True,
        "v": v,
        "side": org,
        "comments": list(service.thread(v.submission)),
        "csrf_token": context.get("csrf_token"),
        "max_chars": service.max_chars(),
        "max_files": service.max_files(),
        "max_mb": service.max_mb(),
        "accept": ",".join(sorted(filetypes.FILE_TYPES)),
        "accepted": filetypes.accepted(),
    }

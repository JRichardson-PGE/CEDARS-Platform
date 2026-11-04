"""`{% publication_hold v %}`: on a filing version's page, the hold the publication scan put on it (navigation review D7, M10), shown to
those who may see the hold (the filing organization and the CPUC system administrators), with a link to what the scan found. Nothing
for anyone else, or when the version is not held."""

from django import template

from .. import service

register = template.Library()


@register.inclusion_tag("snapshots/_hold_notice.html", takes_context=True)
def publication_hold(context, v):
    request = context.get("request")
    user = getattr(request, "user", None)
    scan = service.hold_of(v)
    if scan is None or user is None or not user.is_authenticated or not service.may_see(user, scan.snapshot):
        return {"scan": None}
    return {"scan": scan, "summary": service.summary(scan).splitlines()}

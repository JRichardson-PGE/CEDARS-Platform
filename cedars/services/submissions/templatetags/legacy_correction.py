"""`{% legacy_correction v %}`: on an imported legacy CEDARS filing, the request to correct it, for the filing organization's filers, when
its module corrects legacy filings and a correction version covers its year (submissions.legacy_correction_spec). Everyone else, and
every other version, sees nothing of it."""

from django import template

from cedars.services.accounts import service as access

from .. import service
from ..models import ReopenRequest

register = template.Library()


@register.inclusion_tag("submissions/_legacy_correction.html", takes_context=True)
def legacy_correction(context, v):
    if v is None or not v.imported:
        return {"show": False}
    sub = v.submission
    spec = service.legacy_correction_spec(sub)
    if spec is None:
        return {"show": False}
    user = getattr(context.get("request"), "user", None)
    if not getattr(user, "is_authenticated", False) or not access.has_permission(user, sub.organisation, f"{sub.module}.submit"):
        return {"show": False}
    return {
        "show": True,
        "v": v,
        "spec": spec,
        "pending": sub.reopen_requests.filter(status=ReopenRequest.Status.PENDING).first(),
        "csrf_token": context.get("csrf_token"),
    }

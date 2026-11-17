from django import template

from .. import service
from ..models import ReleasePlan, WorkItem

register = template.Library()

CLASSES = {
    WorkItem.Status.PROPOSED: "info",
    WorkItem.Status.ACCEPTED: "ok",
    WorkItem.Status.PLANNED: "ok",
    WorkItem.Status.DECLINED: "warn",
    WorkItem.Status.DONE: "ok",
}
PLAN_CLASSES = {
    ReleasePlan.Status.DRAFT: "info",
    ReleasePlan.Status.PROPOSED: "warn",
    ReleasePlan.Status.APPROVED: "ok",
    ReleasePlan.Status.RELEASED: "ok",
}


@register.filter
def work_class(status: str) -> str:
    """The status badge's class for a work item's status (the words carry the meaning; the color only repeats it)."""
    return CLASSES.get(status, "info")


@register.filter
def plan_class(status: str) -> str:
    return PLAN_CLASSES.get(status, "info")


@register.inclusion_tag("work/_request_work.html", takes_context=True)
def request_work(context, fb):
    """On a request's page for staff: the work item it is joined to, or the way to join it to one."""
    user = context["request"].user
    return {"fb": fb, "item": service.item_of(fb), "can_propose": service.can_propose(user), "can_see": service.can_see(user)}


@register.inclusion_tag("work/_request_progress.html")
def request_progress(fb):
    """On a request's page for its sender: where the work it is joined to stands, in plain words. An internal item's title
    is not shown."""
    item = service.item_of(fb)
    return {"item": item, "fb": fb, "progress": service.progress(item) if item else None}


@register.inclusion_tag("work/_known_for.html")
def known_for(page):
    """On the form people report a problem on: the public problems that name the page they came from."""
    return {"items": service.known_for(page)}

from django import template

register = template.Library()

CLASSES = {"new": "info", "looking": "info", "waiting": "warn", "answered": "ok", "closed": "ok"}


@register.filter
def request_class(status: str) -> str:
    """The status badge's class for a request's status (the words carry the meaning; the color only repeats it)."""
    return CLASSES.get(status, "info")

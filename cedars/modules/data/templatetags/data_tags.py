from django import template

register = template.Library()


@register.filter
def metric_value(value, metric):
    """A metric's value as the pages show it: its decimals, thousands separated, a dash when blank (a ratio over no cost)."""
    if value is None:
        return "–"
    return f"{value:,.{metric.decimals}f}"


@register.filter
def signed(value, metric):
    if value is None:
        return "–"
    return f"{value:+,.{metric.decimals}f}"


@register.filter
def percent(value):
    return "–" if value is None else f"{value:,.1f}%"


@register.filter
def get_item(mapping, key):
    return mapping.get(key)

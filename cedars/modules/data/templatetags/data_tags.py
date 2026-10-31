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


@register.filter
def number(value, goal_metric):
    """A goal-page value: whole dollars for TSB, two decimals for savings."""
    if value is None:
        return "–"
    return f"{value:,.0f}" if goal_metric.unit == "$" else f"{value:,.2f}"


@register.filter
def goal_percent(row, series):
    return row.percent(series)


@register.filter
def series_label(comparison, series):
    return comparison.series_label(series)


@register.filter
def series_name(comparison, series):
    from ..dashboard import SERIES

    return SERIES[series][0]


@register.filter
def has_links(rows):
    return any(r.links for r in rows)


@register.filter
def money(value):
    return "–" if value is None else f"{value:,.0f}"


@register.filter
def series_word(dataset):
    """ "claims" -> "claimed", "forecast" -> "forecast"."""
    return "claimed" if dataset == "claims" else dataset

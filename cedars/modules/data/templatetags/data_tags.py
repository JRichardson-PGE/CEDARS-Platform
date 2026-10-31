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
def code_label(value):
    """A registry code as words ("codes_and_standards" -> "Codes and standards"); other values unchanged."""
    from cedars.modules.data.dashboard import code_label as words

    return words(value)


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


@register.filter
def is_number(value):
    return isinstance(value, int | float) and not isinstance(value, bool)


@register.filter
def cell(value):
    """A record's value on the records page: blank for none, whole numbers without decimals, others to two decimals from a thousand
    up and six significant places below (the files keep every digit)."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}" if abs(value) >= 1000 else f"{value:.6g}"
    return f"{int(value):,}"

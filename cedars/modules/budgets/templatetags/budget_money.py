from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def money(x, places=0) -> str:
    """Dollars with thousands separators and the sign before the $ ("-$1,200")."""
    if x is None or x == "":
        return ""
    d = Decimal(x)
    text = f"${abs(d):,.{int(places)}f}"
    return "-" + text if d < 0 else text

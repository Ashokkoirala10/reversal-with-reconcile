from django import template

register = template.Library()


@register.filter
def attr(obj, name):
    """Dynamic attribute lookup for a template loop where the attribute
    name itself is a variable — e.g. rendering one checkbox per
    core.permissions.FEATURES entry against a UserAccess instance without
    hardcoding all 8 field names in the template."""
    return getattr(obj, name, False)


@register.filter
def npr(value):
    """Format a Rupee amount compactly using the Nepali/Indian numbering
    system (Lakh = 1,00,000 / Crore = 1,00,00,000) so large, ever-growing
    totals don't blow out the width of a dashboard stat card.

    Full precision is always still available via the element's `title`
    tooltip (set separately in the template with |floatformat:2) — this
    filter is only for the big bold number itself.
    """
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return value

    sign = "-" if v < 0 else ""
    v = abs(v)

    if v >= 1_00_00_000:
        return f"{sign}{v / 1_00_00_000:.2f} Cr"
    if v >= 1_00_000:
        return f"{sign}{v / 1_00_000:.2f} L"
    if v >= 1_000:
        return f"{sign}{v:,.0f}"
    return f"{sign}{v:,.2f}"

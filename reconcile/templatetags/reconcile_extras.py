from django import template

register = template.Library()


@register.filter
def dict_get(form, bank_key):
    """Look up a dynamically-named form field, e.g. form|dict_get:'global'
    renders the `statement_global` field's widget."""
    return form[f"statement_{bank_key}"]

from django import forms

from .banks import BANKS


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A FileField that accepts more than one file at once (e.g. the 10
    Aug *and* 11 Aug statement for one bank, since a transaction near
    midnight often only posts on the bank's next day's EOD run)."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"multiple": True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_file_clean(d, initial) for d in data]
        result = single_file_clean(data, initial)
        return [result] if result else []


class ReconcileUploadForm(forms.Form):
    transaction_file = forms.FileField(
        label="Transaction Data (TransactionReport .xlsx)",
        help_text=(
            "The day's TransactionReport export from the switch. Refund/reversal narrations "
            "are worked out directly from this file — no separate reversal file is needed."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for bank in BANKS:
            self.fields[f"statement_{bank.key}"] = MultipleFileField(
                label=bank.display_name,
                required=False,
                help_text=(
                    "Statement not yet available on the portal"
                    if not bank.available_by_default
                    else "You can select more than one file (e.g. 10 Aug + 11 Aug) if a transaction's other leg posted the next day."
                ),
            )

    def clean(self):
        cleaned = super().clean()
        statement_fields = [f for f in self.fields if f.startswith("statement_")]
        if not any(cleaned.get(f) for f in statement_fields):
            raise forms.ValidationError("Upload at least one bank statement to reconcile against.")
        return cleaned

    def statement_files(self) -> dict[str, list]:
        """Returns {bank_key: [UploadedFile, ...]} for every bank with at
        least one file actually provided in this submission."""
        result = {}
        for bank in BANKS:
            files = self.cleaned_data.get(f"statement_{bank.key}") or []
            if files:
                result[bank.key] = files
        return result

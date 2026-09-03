from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from .models import BankAccount, MailSignature, ProcessingLog, VerificationBankContact


class UploadForm(forms.Form):
    ibft_file = forms.FileField(
        label="IBFT transaction file",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx"}),
    )

    def clean_ibft_file(self):
        f = self.cleaned_data["ibft_file"]
        if not f.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("Please upload an .xlsx file.")
        return f


class DbFetchForm(forms.Form):
    """"Fetch from DB" alternative to uploading the exported .xlsx by hand
    (see core/switch_db.py) — pulls every transaction from the switch
    database across [from_date, to_date], in the same shape
    core.services.process_ibft_file() expects from an uploaded
    ibft-transaction file. If to_date is today, the fetch is capped at
    "right now" instead of running through midnight — see
    core.switch_db.fetch_ibft_export()."""

    from_date = forms.DateField(
        label="From date",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    to_date = forms.DateField(
        label="To date",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def clean(self):
        cleaned = super().clean()
        from_date = cleaned.get("from_date")
        to_date = cleaned.get("to_date")
        if from_date and to_date and to_date < from_date:
            self.add_error("to_date", "'To date' can't be before 'From date'.")
        return cleaned


class VerificationFormatUploadForm(forms.Form):
    dispute_file = forms.FileField(
        label="Dispute transaction file",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx"}),
    )

    def clean_dispute_file(self):
        f = self.cleaned_data["dispute_file"]
        if not f.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("Please upload an .xlsx file.")
        return f


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

    def value_from_datadict(self, data, files, name):
        # Django 5.0+ handles this automatically when allow_multiple_selected
        # is set, but this project targets Django 4.2+, where
        # ClearableFileInput.value_from_datadict() still only ever returns a
        # single file (files.get(name)) no matter how many were selected in
        # the browser. Explicitly pulling every file for this field name via
        # getlist() is what actually lets more than one file through (e.g.
        # up to 4 Prabhu Bank statement exports selected at once).
        if hasattr(files, "getlist"):
            uploads = files.getlist(name)
            if uploads:
                return uploads
        return files.get(name)


class MultipleFileField(forms.FileField):
    """A FileField whose widget accepts multiple files at once, returning a
    list of UploadedFile objects in cleaned_data instead of just one."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"multiple": True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_file_clean(d, initial) for d in data]
        return [single_file_clean(data, initial)] if data else []


class BankStatementUploadForm(forms.Form):
    target_log = forms.ModelChoiceField(
        queryset=ProcessingLog.objects.none(),
        label="Reversal file to check this statement against",
        empty_label="Select a generated reversal file\u2026",
    )
    global_statement_files = MultipleFileField(
        label="Global IME Bank statement(s) (.csv or .xlsx)",
        required=False,
        widget=MultipleFileInput(attrs={"accept": ".csv,.xlsx", "multiple": True}),
        help_text="Up to 3 files (e.g. separate daily exports).",
    )
    prabhu_statement_files = MultipleFileField(
        label="Prabhu Bank statement(s) (.csv or .xlsx)",
        required=False,
        widget=MultipleFileInput(attrs={"accept": ".csv,.xlsx", "multiple": True}),
        help_text="Up to 4 files (e.g. separate daily exports).",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = ProcessingLog.objects.filter(
            status=ProcessingLog.STATUS_SUCCESS, generated_file__isnull=False
        ).order_by("-created_at")
        if user is not None and not user.is_staff:
            qs = qs.filter(uploaded_by=user.username)
        self.fields["target_log"].queryset = qs

    def clean(self):
        cleaned = super().clean()
        global_files = cleaned.get("global_statement_files") or []
        prabhu_files = cleaned.get("prabhu_statement_files") or []

        if not global_files and not prabhu_files:
            self.add_error(
                "global_statement_files",
                "Please upload at least one bank statement file (Global and/or Prabhu).",
            )
            return cleaned

        if len(global_files) > 3:
            self.add_error(
                "global_statement_files",
                "Please upload at most 3 Global IME Bank statement files.",
            )
            return cleaned

        if len(prabhu_files) > 4:
            self.add_error("prabhu_statement_files", "Please upload at most 4 Prabhu Bank statement files.")
            return cleaned

        for f in global_files + prabhu_files:
            name = f.name.lower()
            if not (name.endswith(".csv") or name.endswith(".xlsx")):
                self.add_error(
                    "global_statement_files" if f in global_files else "prabhu_statement_files",
                    f"'{f.name}' is not a .csv or .xlsx file.",
                )
                return cleaned

        cleaned["global_statement_files"] = global_files
        cleaned["prabhu_statement_files"] = prabhu_files
        return cleaned


class BankAccountForm(forms.ModelForm):
    """Lets an Admin (is_staff) user add a new Debtor-Bank -> Debit-Account
    mapping from inside the app itself (Extra page), instead of only being
    possible from Django admin (which remains available, unchanged, for
    superusers — see BankAccountAdmin in core/admin.py). Editing/deleting
    an existing row is still superuser-only, via Django admin."""

    class Meta:
        model = BankAccount
        fields = ["bank_name", "keyword", "debit_account_number", "is_own_bank", "is_active"]
        labels = {
            "bank_name": "Bank name",
            "keyword": "Keyword (matched against Debtor Bank)",
            "debit_account_number": "Debit account number",
            "is_own_bank": "Is own bank (counts toward On-Us)",
            "is_active": "Active",
        }
        widgets = {
            "bank_name": forms.TextInput(attrs={"placeholder": "e.g. NIC Asia Bank"}),
            "keyword": forms.TextInput(attrs={"placeholder": "e.g. NICASIA"}),
            "debit_account_number": forms.TextInput(attrs={"placeholder": "e.g. 0001234567890"}),
        }

    def clean_keyword(self):
        keyword = (self.cleaned_data.get("keyword") or "").strip().upper()
        if not keyword:
            raise forms.ValidationError("Keyword is required.")
        return keyword

    def clean_bank_name(self):
        name = (self.cleaned_data.get("bank_name") or "").strip()
        if not name:
            raise forms.ValidationError("Bank name is required.")
        return name

    def clean_debit_account_number(self):
        account = (self.cleaned_data.get("debit_account_number") or "").strip()
        if not account:
            raise forms.ValidationError("Debit account number is required.")
        return account


class VerificationBankContactForm(forms.ModelForm):
    """Lets an Admin (is_staff) user add a Creditor-Bank -> email-contact
    mapping from the "Extra" page's "Verification format" tab, used to
    email that bank's dispute rows with one click. Editing/deleting an
    existing row is superuser-only, via Django admin (see
    VerificationBankContactAdmin in core/admin.py).

    to_emails/cc_emails each accept more than one address (comma or
    semicolon separated — see clean_to_emails()/clean_cc_emails() below);
    the template turns their "email-raw-input" widget into a chip-style
    multi-email box (type one, press Enter/comma), so this stays a plain
    CharField behind the scenes. to_emails is declared required=False here
    (instead of inheriting the model's non-blank constraint) purely so the
    browser doesn't try to HTML5-validate the hidden raw input the chip
    widget replaces — clean_to_emails() still enforces it server-side."""

    to_emails = forms.CharField(
        label="To email(s)",
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": "e.g. ops@adbl.com.np, card@adbl.com.np", "class": "email-raw-input"}
        ),
    )

    class Meta:
        model = VerificationBankContact
        fields = ["bank_name", "keyword", "to_emails", "cc_emails", "is_active"]
        labels = {
            "bank_name": "Bank name",
            "keyword": "Keyword (matched against Creditor Bank)",
            "cc_emails": "Cc email(s)",
            "is_active": "Active",
        }
        widgets = {
            "bank_name": forms.TextInput(attrs={"placeholder": "e.g. Agriculture Development Bank Ltd (ADBL)"}),
            "keyword": forms.TextInput(attrs={"placeholder": "e.g. ADBL"}),
            "cc_emails": forms.TextInput(attrs={"placeholder": "optional", "class": "email-raw-input"}),
        }

    def clean_keyword(self):
        keyword = (self.cleaned_data.get("keyword") or "").strip().upper()
        if not keyword:
            raise forms.ValidationError("Keyword is required.")
        return keyword

    def clean_bank_name(self):
        name = (self.cleaned_data.get("bank_name") or "").strip()
        if not name:
            raise forms.ValidationError("Bank name is required.")
        return name

    def clean_to_emails(self):
        raw = (self.cleaned_data.get("to_emails") or "").strip()
        if not raw:
            raise forms.ValidationError("At least one 'To' email is required.")
        addresses = [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]
        validator = forms.EmailField()
        for addr in addresses:
            validator.clean(addr)
        return ", ".join(addresses)

    def clean_cc_emails(self):
        raw = (self.cleaned_data.get("cc_emails") or "").strip()
        if not raw:
            return ""
        addresses = [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]
        validator = forms.EmailField()
        for addr in addresses:
            validator.clean(addr)
        return ", ".join(addresses)


class MailSignatureForm(forms.ModelForm):
    """Lets any logged-in user add/edit their own outgoing-email
    signature(s) (core.models.MailSignature), from the "Extra" page's
    "Mail signature" tab — `user` is set server-side to the logged-in
    user, not exposed as a form field, so each person only ever manages
    their own."""

    class Meta:
        model = MailSignature
        fields = ["name", "title", "mobile", "company", "address", "toll_free", "website", "is_active"]
        labels = {
            "name": "Name (optional)",
            "title": "Title / department",
            "mobile": "Mobile (optional)",
            "company": "Company (optional — overrides the default)",
            "address": "Address (optional — overrides the default)",
            "toll_free": "Toll free (optional — overrides the default)",
            "website": "Website (optional — overrides the default)",
            "is_active": "Active",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Ashok Koirala — leave blank for a generic mailbox"}),
            "title": forms.TextInput(attrs={"placeholder": "e.g. Tech Operation Department"}),
            "mobile": forms.TextInput(attrs={"placeholder": "e.g. +977-9849626348"}),
            "company": forms.TextInput(attrs={"placeholder": "leave blank to use the configured default"}),
            "address": forms.TextInput(attrs={"placeholder": "leave blank to use the configured default"}),
            "toll_free": forms.TextInput(attrs={"placeholder": "leave blank to use the configured default"}),
            "website": forms.TextInput(attrs={"placeholder": "leave blank to use the configured default"}),
        }

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise forms.ValidationError("Title / department is required.")
        return title


class CreateUserForm(forms.Form):
    """"Make user" tab on the "Extra" page — lets an Admin (is_staff) user
    create a new login without going through the Django admin's
    superuser-only user form. Deliberately just the fields that matter day
    to day (username/email/password/active/staff/admin); the group and
    per-permission editors the Django admin exposes are left out on
    purpose since nothing in this app needs that level of control."""

    username = forms.CharField(
        label="Username",
        max_length=150,
        widget=forms.TextInput(attrs={"placeholder": "e.g. jdoe", "autocomplete": "off"}),
    )
    email = forms.EmailField(
        label="Email",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. jdoe@example.com"}),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    is_active = forms.BooleanField(label="Active", required=False, initial=True)
    is_staff = forms.BooleanField(label="Staff", required=False, initial=False)
    is_superuser = forms.BooleanField(label="Admin", required=False, initial=False)

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if not username:
            raise forms.ValidationError("Username is required.")
        if get_user_model().objects.filter(username=username).exists():
            raise forms.ValidationError("A user with that username already exists.")
        return username

    def clean_password(self):
        password = self.cleaned_data.get("password") or ""
        try:
            validate_password(password)
        except DjangoValidationError as exc:
            raise forms.ValidationError(exc.messages)
        return password

    def save(self):
        return get_user_model().objects.create_user(
            username=self.cleaned_data["username"],
            email=self.cleaned_data.get("email") or "",
            password=self.cleaned_data["password"],
            is_active=self.cleaned_data.get("is_active", True),
            is_staff=self.cleaned_data.get("is_staff", False),
            is_superuser=self.cleaned_data.get("is_superuser", False),
        )


class UpdateUserForm(forms.Form):
    """Edit form behind the "Users" list's Edit button, next to Delete, on
    the "Extra" page's Make user tab. Same field set as CreateUserForm,
    except the password is optional here — leave it blank to keep the
    user's existing one."""

    username = forms.CharField(
        label="Username",
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    email = forms.EmailField(
        label="Email",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. jdoe@example.com"}),
    )
    password = forms.CharField(
        label="New password",
        required=False,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "new-password", "placeholder": "Leave blank to keep current password"}
        ),
    )
    is_active = forms.BooleanField(label="Active", required=False)
    is_staff = forms.BooleanField(label="Staff", required=False)
    is_superuser = forms.BooleanField(label="Admin", required=False)

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if not username:
            raise forms.ValidationError("Username is required.")
        qs = get_user_model().objects.filter(username=username)
        if self.instance is not None:
            qs = qs.exclude(id=self.instance.id)
        if qs.exists():
            raise forms.ValidationError("A user with that username already exists.")
        return username

    def clean_password(self):
        password = self.cleaned_data.get("password") or ""
        if password:
            try:
                validate_password(password, user=self.instance)
            except DjangoValidationError as exc:
                raise forms.ValidationError(exc.messages)
        return password

    def save(self):
        user = self.instance
        user.username = self.cleaned_data["username"]
        user.email = self.cleaned_data.get("email") or ""
        user.is_active = self.cleaned_data.get("is_active", False)
        user.is_staff = self.cleaned_data.get("is_staff", False)
        user.is_superuser = self.cleaned_data.get("is_superuser", False)
        if self.cleaned_data.get("password"):
            user.set_password(self.cleaned_data["password"])
        user.save()
        return user

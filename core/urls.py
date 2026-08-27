from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("login/", views.BrandedLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="core:login"), name="logout"),
    path("", views.upload_view, name="upload"),
    path("bank-statement/", views.bank_statement_upload_view, name="bank_statement_upload"),
    path("bank-statement/add-bank/", views.add_bank_account_view, name="add_bank_account"),
    path(
        "bank-statement/verification-format/add-contact/",
        views.add_verification_bank_contact_view,
        name="add_verification_bank_contact",
    ),
    path(
        "bank-statement/verification-format/update-contact/<int:contact_id>/",
        views.update_verification_bank_contact_view,
        name="update_verification_bank_contact",
    ),
    path(
        "bank-statement/verification-format/add-signature/",
        views.add_mail_signature_view,
        name="add_mail_signature",
    ),
    path(
        "bank-statement/verification-format/update-signature/<int:signature_id>/",
        views.update_mail_signature_view,
        name="update_mail_signature",
    ),
    path(
        "bank-statement/verification-format/send-mail/",
        views.verification_send_mail_view,
        name="verification_send_mail",
    ),
    path("bank-statement/create-user/", views.create_user_view, name="create_user"),
    path("bank-statement/delete-user/<int:user_id>/", views.delete_user_view, name="delete_user"),
    path("bank-statement/update-user/<int:user_id>/", views.update_user_view, name="update_user"),
    path("bank-statement/verification-format/", views.verification_format_view, name="verification_format"),
    path("result/<int:log_id>/", views.result_view, name="result"),
    path("toggle-passed/<int:log_id>/", views.toggle_passed_view, name="toggle_passed"),
    path("download/<int:log_id>/<str:kind>/", views.download_file_view, name="download_file"),
    path("audit-log/", views.audit_log_view, name="audit_log"),
    path("audit-log/export/", views.export_audit_log_view, name="export_audit_log"),
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("dashboard/export/failed/", views.export_failed_onoffus_view, name="export_failed_onoffus"),
    path("dashboard/export/summary/", views.export_dashboard_summary_view, name="export_dashboard_summary"),
    path("dashboard/export/days/", views.export_day_breakdown_view, name="export_day_breakdown"),
    path("dashboard/export/member-report/", views.export_member_report_view, name="export_member_report"),
    path("dashboard/export/aggregator-report/", views.export_aggregator_report_view, name="export_aggregator_report"),
]

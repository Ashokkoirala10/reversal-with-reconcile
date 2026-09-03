from django.urls import path

from . import dashboard, views

app_name = "reconcile"

urlpatterns = [
    path("", views.reconcile_view, name="reconcile"),
    path("result/<int:run_id>/", views.result_view, name="result"),
    path("toggle-passed/<int:run_id>/", views.toggle_passed_view, name="toggle_passed"),
    path("issue/<int:run_id>/", views.update_issue_view, name="update_issue"),
    path("download/<int:run_id>/<str:kind>/", views.download_file_view, name="download_file"),
    path("audit-log/", views.audit_log_view, name="audit_log"),
    path("day/<str:date_str>/", views.day_detail_view, name="day_detail"),
    path("dashboard/", dashboard.dashboard_view, name="dashboard"),
    path("dashboard/export/buckets/", dashboard.export_bucket_report_view, name="export_buckets"),
    path("dashboard/export/failed/", dashboard.export_failed_onoffus_view, name="export_failed_onoffus"),
    path("dashboard/export/days/", dashboard.export_day_breakdown_view, name="export_day_breakdown"),
    path("dashboard/export/summary/", dashboard.export_summary_view, name="export_summary"),
]

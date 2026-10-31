from django.urls import path

from . import views

app_name = "monthly"
urlpatterns = [
    path("", views.index, name="index"),
    path("years/new/", views.year_new, name="year_new"),
    path("years/<int:year>/", views.year, name="year"),
    path("years/<int:year>/year.csv", views.year_csv, name="year_csv"),
    path("years/<int:year>/setup/", views.setup, name="setup"),
    path("years/<int:year>/13th-month/", views.closing, name="closing"),
    path("years/<int:year>/13th-month.csv", views.closing_csv, name="closing_csv"),
    path("reports/<int:pk>/", views.report, name="report"),
    path("reports/<int:pk>/upload", views.upload, name="upload"),
    path("reports/<int:pk>/price", views.price, name="price"),
    path("reports/<int:pk>/confirm", views.confirm, name="confirm"),
    path("reports/<int:pk>/unconfirm", views.unconfirm, name="unconfirm"),
    path("reports/<int:pk>/reopen", views.reopen, name="reopen"),
    path("versions/<int:pk>/", views.version, name="version"),
    path("versions/<int:pk>/report.csv", views.version_csv, name="version_csv"),
    path("versions/<int:pk>/findings.csv", views.findings_csv, name="findings_csv"),
    path("versions/<int:pk>/files/<str:which>", views.published_file, name="file"),
    path("changes/", views.changes, name="changes"),
    path("changes/<int:pk>/", views.change_letter, name="change_letter"),
]

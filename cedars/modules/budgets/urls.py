from django.urls import path

from . import views

app_name = "budgets"
urlpatterns = [
    path("", views.index, name="index"),
    path("filings/new/", views.filing_new, name="filing_new"),
    path("filings/<int:pk>/", views.filing, name="filing"),
    path("filings/<int:pk>/setup/", views.setup, name="setup"),
    path("filings/<int:pk>/upload", views.upload, name="upload"),
    path("filings/<int:pk>/price", views.price, name="price"),
    path("filings/<int:pk>/confirm", views.confirm, name="confirm"),
    path("filings/<int:pk>/unconfirm", views.unconfirm, name="unconfirm"),
    path("filings/<int:pk>/reopen", views.reopen, name="reopen"),
    path("versions/<int:pk>/", views.version, name="version"),
    path("versions/<int:pk>/results.csv", views.version_csv, name="version_csv"),
    path("versions/<int:pk>/findings.csv", views.findings_csv, name="findings_csv"),
    path("versions/<int:pk>/appendix.xlsx", views.appendix, name="appendix"),
    path("versions/<int:pk>/files/<str:which>", views.published_file, name="file"),
    path("compare/", views.compare, name="compare"),
]

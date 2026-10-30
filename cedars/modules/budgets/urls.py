from django.urls import path

from . import shift_views, views

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
    path("shifts/", shift_views.shift_list, name="shifts"),
    path("shifts/new/", shift_views.shift_new, name="shift_new"),
    path("shifts/<int:pk>/", shift_views.shift, name="shift"),
    path("shifts/<int:pk>/lines/", shift_views.shift_lines, name="shift_lines"),
    path("shifts/<int:pk>/submit", shift_views.shift_submit, name="shift_submit"),
    path("shifts/<int:pk>/withdraw", shift_views.shift_withdraw, name="shift_withdraw"),
    path("shifts/<int:pk>/acknowledge/", shift_views.shift_acknowledge, name="shift_acknowledge"),
    path("operating/<int:first_year>/", shift_views.operating, name="operating"),
    path("operating/<int:first_year>/operating.csv", shift_views.operating_csv, name="operating_csv"),
]

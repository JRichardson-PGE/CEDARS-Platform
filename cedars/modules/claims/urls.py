from django.urls import path

from . import views

app_name = "claims"
urlpatterns = [
    path("", views.index, name="index"),
    path("template.xlsx", views.template, name="template"),
    path("years/new/", views.year_new, name="year_new"),
    path("years/<int:year>/setup/", views.setup, name="setup"),
    path("periods/<int:pk>/", views.period, name="period"),
    path("periods/<int:pk>/upload", views.upload, name="upload"),
    path("periods/<int:pk>/check", views.check, name="check"),
    path("periods/<int:pk>/price", views.price, name="price"),
    path("periods/<int:pk>/confirm", views.confirm, name="confirm"),
    path("periods/<int:pk>/unconfirm", views.unconfirm, name="unconfirm"),
    path("periods/<int:pk>/reopen", views.reopen, name="reopen"),
    path("versions/<int:pk>/", views.version, name="version"),
    path("versions/<int:pk>/results.csv", views.version_csv, name="version_csv"),
    path("versions/<int:pk>/findings.csv", views.findings_csv, name="findings_csv"),
    path("versions/<int:pk>/files/<str:which>", views.published_file, name="file"),
]

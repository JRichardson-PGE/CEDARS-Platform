from django.urls import path

from . import views

app_name = "impact_profiles"
urlpatterns = [
    path("", views.library, name="library"),
    path("<int:pk>/", views.profile, name="profile"),
    path("<int:pk>/v<int:number>.csv", views.profile_version_csv, name="profile_version_csv"),
    path("<int:pk>/replace", views.profile_replace, name="profile_replace"),
    path("<int:pk>/retire", views.profile_retire, name="profile_retire"),
    path("custom/new/", views.custom_new, name="custom_new"),
    path("custom/template.csv", views.custom_template, name="custom_template"),
    path("packages/", views.packages, name="packages"),
    path("packages/<str:mvid>/", views.package, name="package"),
    path("packages/<str:mvid>/mappings", views.mapping_new, name="mapping_new"),
    path("mappings/<int:pk>/end", views.mapping_end, name="mapping_end"),
    path("load-shapes/template.csv", views.load_shape_template, name="load_shape_template"),
    path("load-shapes/<int:pk>/", views.load_shape, name="load_shape"),
    path("uploads/", views.uploads, name="uploads"),
    path("uploads/<int:pk>/", views.upload, name="upload"),
    path("uploads/<int:pk>/generate", views.upload_generate, name="upload_generate"),
    path("uploads/<int:pk>/findings.csv", views.upload_findings_csv, name="upload_findings_csv"),
    path("etrm/", views.sync_history, name="sync_history"),
    path("library/", views.exports, name="exports"),
    path("library/<int:number>/<str:name>", views.export_file, name="export_file"),
]

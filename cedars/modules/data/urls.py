from django.urls import path

from . import saved_pages, views

app_name = "data"

urlpatterns = [
    path("", views.dashboard_index, name="dashboards"),
    path("download.<str:fmt>", views.download, name="download"),
    path("metrics.csv", views.metrics_csv, name="metrics_csv"),
    path("records/", views.records_page, name="records"),
    path("records/download.<str:fmt>", views.records_download, name="records_download"),
    path("records/default-columns", saved_pages.records_default_columns, name="records_default_columns"),
    path("views/", saved_pages.view_list, name="views"),
    path("views/save", saved_pages.view_save, name="view_save"),
    path("views/<int:pk>/", saved_pages.view_page, name="view"),
    path("downloads/", views.download_list, name="downloads"),
    path("downloads/request", views.download_request, name="download_request"),
    path("downloads/<int:pk>/", views.download_job, name="download_job"),
    path("downloads/<int:pk>/file", views.download_file, name="download_file"),
    path("<slug:slug>/", views.dashboard_page, name="dashboard"),
]

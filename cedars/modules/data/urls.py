from django.urls import path

from . import views

app_name = "data"

urlpatterns = [
    path("", views.dashboard_index, name="dashboards"),
    path("download.<str:fmt>", views.download, name="download"),
    path("metrics.csv", views.metrics_csv, name="metrics_csv"),
    path("<slug:slug>/", views.dashboard_page, name="dashboard"),
]

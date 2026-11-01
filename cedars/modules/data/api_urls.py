"""/api/v1/data: the open data API over the published snapshots (api.py)."""

from django.urls import path

from . import api

app_name = "data-api"

urlpatterns = [
    path("", api.index, name="index"),
    path("metrics", api.metric_list, name="metrics"),
    path("dimensions", api.dimension_list, name="dimensions"),
    path("query", api.aggregate, name="query"),
    path("columns", api.column_list, name="columns"),
    path("records", api.record_list, name="records"),
]

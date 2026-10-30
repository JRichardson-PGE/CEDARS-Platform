"""/api/v1/calculations: the calculation job API (api.py)."""

from django.urls import path

from . import api

app_name = "calculations-api"

urlpatterns = [
    path("engines", api.engines, name="engines"),
    path("runs", api.runs, name="runs"),
    path("runs/<uuid:pk>", api.run, name="run"),
    path("runs/<uuid:pk>/stop", api.stop, name="stop"),
    path("runs/<uuid:pk>/files/<path:name>", api.run_file, name="file"),
]

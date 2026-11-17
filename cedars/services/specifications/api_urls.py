"""/api/v1/specifications: the published filing specifications as data (api.py)."""

from django.urls import path

from . import api

app_name = "specifications-api"

urlpatterns = [
    path("", api.index, name="index"),
    path("<slug:code>/<slug:slug>", api.version, name="version"),
    path("<slug:code>/<slug:slug>/schema/<str:table>", api.schema, name="schema"),
]

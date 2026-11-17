"""/api/v1/rules: the pre-upload checker and the published rule sets (api.py)."""

from django.urls import path

from . import api

app_name = "rules-api"

urlpatterns = [
    path("check", api.check, name="check"),
    path("sets", api.sets, name="sets"),
    path("sets/<int:pk>", api.rule_set, name="set"),
]

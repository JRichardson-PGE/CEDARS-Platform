"""CEDARS' DEER Resources addresses (/deer-resources/...), which sit outside /documents/: each leads to the migrated resource or file
(views.cedars_legacy)."""

from django.urls import path

from . import views

app_name = "deer_resources"
urlpatterns = [
    path("", views.cedars_legacy, name="home"),
    path("<path:rest>", views.cedars_legacy, name="other"),
]

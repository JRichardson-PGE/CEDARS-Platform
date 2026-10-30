"""/api/v1/claims: the claims submissions API, read-only (api.py)."""

from django.urls import path

from . import api

app_name = "claims-api"

urlpatterns = [
    path("years", api.years, name="years"),
    path("periods/<int:pk>", api.period, name="period"),
    path("versions/<int:pk>", api.version, name="version"),
    path("versions/<int:pk>/findings", api.findings, name="findings"),
    path("versions/<int:pk>/results", api.results, name="results"),
    path("versions/<int:pk>/files/<int:which>", api.file, name="file"),
    path("changes", api.changes, name="changes"),
    path("changes/<int:pk>", api.change, name="change"),
    path("claims/<str:claim_id>/chain", api.chain, name="chain"),
]

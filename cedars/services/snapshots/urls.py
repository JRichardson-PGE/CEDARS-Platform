from django.urls import path

from . import views

app_name = "snapshots"
urlpatterns = [
    path("holds/", views.holds, name="holds"),
    path("holds/<uuid:pk>/", views.hold, name="hold"),
    path("scan-settings/", views.scan_settings, name="scan_settings"),
]

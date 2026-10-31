from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "core"
urlpatterns = [
    # The site opens on the portfolio dashboard's first page (Jake, 2026-10-31); the module overview is the About page.
    path("", RedirectView.as_view(pattern_name="data:dashboards"), name="home"),
    path("about/", views.about, name="about"),
    path("healthz", views.healthz, name="healthz"),
]

from django.urls import path

from . import views

app_name = "notifications"
urlpatterns = [
    path("", views.index, name="index"),
    path("settings/", views.settings_page, name="settings"),
]

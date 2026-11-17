from django.urls import path

from . import views

app_name = "accessibility"
urlpatterns = [
    path("", views.files, name="files"),
    path("settings/", views.check_settings, name="settings"),
    path("<int:pk>/", views.file, name="file"),
    path("<int:pk>/review", views.review, name="review"),
    path("<int:pk>/reopen", views.reopen, name="reopen"),
]

from django.urls import path

from . import views

app_name = "retention"
urlpatterns = [path("", views.index, name="index")]

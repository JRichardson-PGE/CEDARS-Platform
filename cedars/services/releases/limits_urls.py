from django.urls import path

from . import views

app_name = "limits"
urlpatterns = [path("", views.limits_page, name="index")]

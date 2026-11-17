from django.urls import path

from . import mandate_views as views

app_name = "mandates"
urlpatterns = [
    path("", views.index, name="index"),
    path("manage/", views.manage, name="manage"),
    path("manage/<int:pk>/", views.edit, name="edit"),
    path("<int:pk>/", views.detail, name="detail"),
]

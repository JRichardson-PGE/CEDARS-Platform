from django.urls import path

from . import views

app_name = "programs"
urlpatterns = [
    path("", views.catalog, name="catalog"),
    path("programs.csv", views.catalog_csv, name="catalog_csv"),
    path("requests/", views.requests, name="requests"),
    path("requests/<int:pk>/", views.change_request, name="request"),
    path("locked/", views.locked, name="locked"),
    path("<str:prg_id>/", views.program, name="program"),
    path("<str:prg_id>/change", views.change, name="change"),
]

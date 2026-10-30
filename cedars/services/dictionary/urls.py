from django.urls import path

from . import views

app_name = "dictionary"
urlpatterns = [
    path("", views.index, name="index"),
    path("catalog.csv", views.catalog_csv, name="catalog_csv"),
    path("crosswalk.csv", views.crosswalk_csv, name="crosswalk_csv"),
    path("templates/<str:module>/<str:table>.<str:fmt>", views.template, name="template"),
    path("fields/<str:name>/", views.detail, name="field"),
]

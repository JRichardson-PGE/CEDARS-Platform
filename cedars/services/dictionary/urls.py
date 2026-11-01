from django.urls import path

from . import views

app_name = "dictionary"
urlpatterns = [
    path("", views.index, name="index"),
    path("catalog.csv", views.catalog_csv, name="catalog_csv"),
    path("crosswalk.csv", views.crosswalk_csv, name="crosswalk_csv"),
    path("templates/<str:module>/<str:table>.<str:fmt>", views.template, name="template"),
    path("fields/<str:name>/", views.detail, name="field"),
    path("fields/<str:name>/retire/", views.retire, name="retire"),
    path("fields/<str:name>/reinstate", views.reinstate, name="reinstate"),
    path("changes/", views.changes, name="changes"),
    path("changes.csv", views.changes_csv, name="changes_csv"),
    path("changes/<int:pk>/", views.version_changes, name="version_changes"),
    path("lists/", views.lists, name="lists"),
    path("lists/<str:name>/", views.value_list, name="list"),
    path("lists/<str:name>/new-version", views.start_version, name="start_version"),
    path("lists/<str:name>/draft/", views.draft, name="draft"),
    path("lists/<str:name>/draft/add", views.add_code, name="add_code"),
    path("lists/<str:name>/draft/upload", views.upload_codes, name="upload_codes"),
    path("lists/<str:name>/draft/publish", views.publish_list, name="publish_list"),
    path("lists/<str:name>/draft/discard", views.discard_list_draft, name="discard_list_draft"),
    path("lists/<str:name>/draft/codes/<int:pk>/", views.code, name="code"),
    path("lists/<str:name>/draft/codes/<int:pk>/drop", views.drop_code, name="drop_code"),
]

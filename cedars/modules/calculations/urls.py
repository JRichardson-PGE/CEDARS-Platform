from django.urls import path

from . import draft_views, views

app_name = "calculations"
urlpatterns = [
    path("", views.runs, name="runs"),
    path("new/", views.new_run, name="new"),
    path("runs/<uuid:pk>/", views.run, name="run"),
    path("runs/<uuid:pk>/status.json", views.run_status, name="run_status"),
    path("drafts/", draft_views.draft_list, name="drafts"),
    path("drafts/<uuid:pk>/", draft_views.draft, name="draft"),
    path("drafts/<uuid:pk>/job.zip", draft_views.draft_download, name="draft_download"),
    path("drafts/<uuid:pk>/<str:table>/<int:number>/", draft_views.draft_row, name="draft_row"),
    path("runs/<uuid:pk>/edit-inputs", draft_views.draft_from_run, name="draft_from_run"),
    path("runs/<uuid:pk>/stop", views.stop, name="stop"),
    path("runs/<uuid:pk>/files/<path:name>", views.run_file, name="run_file"),
    path("engines/", views.engines, name="engines"),
]

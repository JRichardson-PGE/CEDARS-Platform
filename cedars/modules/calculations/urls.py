from django.urls import path

from . import draft_views, template_views, views

app_name = "calculations"
urlpatterns = [
    path("", views.runs, name="runs"),
    path("new/", views.new_run, name="new"),
    path("input-templates/<slug:kind>", views.input_template, name="input_template"),
    path("templates/", template_views.templates, name="templates"),
    path("templates/new/", template_views.template, name="template_new"),
    path("templates/<int:pk>/", template_views.template, name="template"),
    path("runs/<uuid:pk>/", views.run, name="run"),
    path("runs/<uuid:pk>/status.json", views.run_status, name="run_status"),
    path("drafts/", draft_views.draft_list, name="drafts"),
    path("drafts/<uuid:pk>/", draft_views.draft, name="draft"),
    path("drafts/<uuid:pk>/job.zip", draft_views.draft_download, name="draft_download"),
    path("drafts/<uuid:pk>/etrm/", draft_views.draft_etrm, name="draft_etrm"),
    path("drafts/<uuid:pk>/<str:table>/<int:number>/", draft_views.draft_row, name="draft_row"),
    path("runs/<uuid:pk>/edit-inputs", draft_views.draft_from_run, name="draft_from_run"),
    path("runs/<uuid:pk>/stop", views.stop, name="stop"),
    path("runs/<uuid:pk>/files/<path:name>", views.run_file, name="run_file"),
    path("runs/<uuid:pk>/files.zip", views.run_zip, name="run_zip"),
    path("runs/<uuid:pk>/log.txt", views.run_log, name="run_log"),
    path("runs/<uuid:pk>/inputs/<slug:name>", views.run_input, name="run_input"),
    path("runs/<uuid:pk>/inputs.zip", views.run_inputs_zip, name="run_inputs_zip"),
    path("runs/<uuid:pk>/results/", views.results, name="results"),
    path("runs/<uuid:pk>/results.csv", views.results_csv, name="results_csv"),
    path("runs/<uuid:pk>/compare/", views.compare, name="compare"),
    path("runs/<uuid:pk>/compare.csv", views.compare_csv, name="compare_csv"),
    path("engines/", views.engines, name="engines"),
]

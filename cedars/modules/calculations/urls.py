from django.urls import path

from . import views

app_name = "calculations"
urlpatterns = [
    path("", views.runs, name="runs"),
    path("new/", views.new_run, name="new"),
    path("runs/<uuid:pk>/", views.run, name="run"),
    path("runs/<uuid:pk>/status.json", views.run_status, name="run_status"),
    path("runs/<uuid:pk>/stop", views.stop, name="stop"),
    path("runs/<uuid:pk>/files/<path:name>", views.run_file, name="run_file"),
    path("runs/<uuid:pk>/results/", views.results, name="results"),
    path("runs/<uuid:pk>/results.csv", views.results_csv, name="results_csv"),
    path("runs/<uuid:pk>/compare/", views.compare, name="compare"),
    path("runs/<uuid:pk>/compare.csv", views.compare_csv, name="compare_csv"),
    path("engines/", views.engines, name="engines"),
]

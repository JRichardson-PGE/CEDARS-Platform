from django.urls import path

from . import views, views_studies

app_name = "documents"
urlpatterns = [
    path("", views.library, name="library"),
    path("documents.csv", views.library_csv, name="library_csv"),
    path("new/", views.new, name="new"),
    path("lists/", views.value_lists, name="lists"),
    path("lists/<slug:code>/add", views.add_option, name="add_option"),
    path("lists/options/<int:pk>/<str:action>", views.change_option, name="change_option"),
    path("files/<int:pk>/<str:name>", views.download, name="download"),
    path("studies/", views_studies.register, name="studies"),
    path("studies/studies.csv", views_studies.register_csv, name="studies_csv"),
    path("studies/new/", views_studies.new_study, name="new_study"),
    path("studies/budgets/", views_studies.summary, name="study_summary"),
    path("studies/<int:number>/", views_studies.study, name="study"),
    path("studies/<int:number>/change/", views_studies.change_study, name="change_study"),
    path("studies/<int:number>/report.csv", views_studies.study_report, name="study_report"),
    path("studies/<int:number>/programs", views_studies.add_coverage, name="add_coverage"),
    path("studies/<int:number>/programs/<int:pk>/remove", views_studies.remove_coverage, name="remove_coverage"),
    path("work-orders/", views_studies.work_orders, name="work_orders"),
    path("work-orders/new", views_studies.add_work_order, name="add_work_order"),
    path("work-orders/<int:pk>/", views_studies.work_order, name="work_order"),
    path("work-orders/<int:pk>/<str:action>", views_studies.work_order_change, name="work_order_change"),
    path("<int:pk>/", views.document, name="document"),
    path("<int:pk>/change/", views.change, name="change"),
    path("<int:pk>/versions/new/", views.new_version, name="new_version"),
    path("<int:pk>/versions/<int:number>/publish", views.publish, name="publish"),
    path("<int:pk>/downloads/", views.downloads, name="downloads"),
]

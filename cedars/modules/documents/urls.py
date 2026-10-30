from django.urls import path

from . import views

app_name = "documents"
urlpatterns = [
    path("", views.library, name="library"),
    path("documents.csv", views.library_csv, name="library_csv"),
    path("new/", views.new, name="new"),
    path("lists/", views.value_lists, name="lists"),
    path("lists/<slug:code>/add", views.add_option, name="add_option"),
    path("lists/options/<int:pk>/<str:action>", views.change_option, name="change_option"),
    path("files/<int:pk>/<str:name>", views.download, name="download"),
    path("<int:pk>/", views.document, name="document"),
    path("<int:pk>/change/", views.change, name="change"),
    path("<int:pk>/versions/new/", views.new_version, name="new_version"),
    path("<int:pk>/versions/<int:number>/publish", views.publish, name="publish"),
    path("<int:pk>/downloads/", views.downloads, name="downloads"),
]

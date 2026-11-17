from django.urls import path

from . import views

app_name = "work"
urlpatterns = [
    path("admin/work/", views.backlog, name="backlog"),
    path("admin/work/new/", views.new, name="new"),
    path("admin/work/from-request/<int:pk>/", views.from_request, name="from_request"),
    path("admin/work/links/<int:pk>/remove", views.unjoin, name="unjoin"),
    path("admin/work/measures/", views.measures_page, name="measures"),
    path("admin/work/releases/", views.releases, name="releases"),
    path("admin/work/releases/<int:pk>/", views.release, name="release"),
    path("admin/work/releases/<int:pk>/act", views.release_act, name="release_act"),
    path("admin/work/<int:pk>/", views.item, name="item"),
    path("admin/work/<int:pk>/tests", views.tests, name="tests"),
    path("admin/work/<int:pk>/edit/", views.edit, name="edit"),
    path("admin/work/<int:pk>/act", views.act, name="act"),
    path("hooks/issue-tracker/", views.tracker_event, name="tracker_event"),
]

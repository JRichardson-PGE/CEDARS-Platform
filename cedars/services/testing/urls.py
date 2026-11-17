from django.urls import path

from . import views

app_name = "testing"
urlpatterns = [
    path("testing/", views.mine, name="mine"),
    path("testing/open/<str:token>/", views.open_link, name="open"),
    path("testing/runs/<int:pk>/", views.run, name="run"),
    path("testing/runs/<int:pk>/new-link", views.new_link, name="new_link"),
    path("testing/runs/<int:pk>/<str:test_id>/", views.test, name="test"),
    path("testing/runs/<int:pk>/<str:test_id>/draft", views.draft, name="draft"),
    path("admin/testing/", views.manage, name="manage"),
    path("admin/testing/campaigns/<int:pk>/", views.campaign, name="campaign"),
    path("admin/testing/campaigns/<int:pk>/results/", views.results, name="results"),
    path("admin/testing/campaigns/<int:pk>/results.xlsx", views.results_download, name="results_download"),
    path("admin/testing/campaigns/<int:pk>/assign", views.assign, name="assign"),
    path("admin/testing/campaigns/<int:pk>/close", views.close, name="close"),
    path("admin/testing/campaigns/<int:pk>/reopen", views.reopen, name="reopen"),
    path("admin/testing/assignments/<int:pk>/withdraw", views.withdraw, name="withdraw"),
]

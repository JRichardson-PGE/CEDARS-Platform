from django.urls import path

from . import views

app_name = "rules"
urlpatterns = [
    path("", views.index, name="index"),
    path("import", views.import_rules, name="import"),
    path("differences/", views.differences, name="differences"),
    path("check/", views.check, name="check"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/publish", views.publish, name="publish"),
    path("<int:pk>/<str:rule_id>/", views.rule, name="rule"),
]

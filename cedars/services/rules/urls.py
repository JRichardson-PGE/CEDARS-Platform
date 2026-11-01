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
    path("<int:pk>/new-version", views.new_version, name="new_version"),
    path("<int:pk>/discard", views.discard, name="discard"),
    path("<int:pk>/add/", views.add_rule, name="add_rule"),
    path("<int:pk>/<str:rule_id>/", views.rule, name="rule"),
    path("<int:pk>/<str:rule_id>/edit/", views.edit_rule, name="edit_rule"),
    path("<int:pk>/<str:rule_id>/remove", views.remove_rule, name="remove_rule"),
]

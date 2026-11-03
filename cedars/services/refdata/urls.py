from django.urls import path

from . import builder_views, views

app_name = "refdata"
urlpatterns = [
    path("", views.index, name="index"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/definition.yaml", views.cost_test_file, name="cost_test_file"),
    path("cost-tests/", views.cost_test_sets, name="cost_tests"),
    path("cost-tests/build/", builder_views.build_start, name="build_start"),
    path("cost-tests/build/<int:pk>/definition.yaml", builder_views.build_file, name="build_file"),
    path("cost-tests/build/<int:pk>/action/", builder_views.build_action, name="build_action"),
    path("cost-tests/build/<int:pk>/inputs/new/", builder_views.build_input, name="build_input_new"),
    path("cost-tests/build/<int:pk>/inputs/<str:name>/", builder_views.build_input, name="build_input"),
    path("cost-tests/build/<int:pk>/<slug:step>/", builder_views.build_step, name="build_step"),
]

from django.urls import path

from . import views

app_name = "refdata"
urlpatterns = [
    path("", views.index, name="index"),
    path("<int:pk>/", views.detail, name="detail"),
    path("cost-tests/", views.cost_test_sets, name="cost_tests"),
]

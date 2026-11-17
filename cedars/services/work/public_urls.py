from django.urls import path

from . import views

# What anyone may read of the work items. Its own namespace, so the header marks no Administration page for these.
app_name = "known"
urlpatterns = [
    path("known-problems/", views.known, name="problems"),
    path("upcoming-changes/", views.upcoming, name="upcoming"),
    path("work-items/<int:pk>/", views.public_item, name="item"),
    path("work-items/<int:pk>/follow", views.follow, name="follow"),
]

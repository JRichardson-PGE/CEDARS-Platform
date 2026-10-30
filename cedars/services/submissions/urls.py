from django.urls import path

from . import views

app_name = "submissions"
urlpatterns = [
    path("calendar/", views.calendar, name="calendar"),
    path("reopen-requests/", views.reopen_requests, name="reopen_requests"),
    path("reopen-requests/<int:pk>", views.decide, name="decide"),
]

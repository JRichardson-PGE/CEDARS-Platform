from django.urls import path

from . import views

app_name = "submissions"
urlpatterns = [
    path("calendar/", views.calendar, name="calendar"),
    path("reopen-requests/", views.reopen_requests, name="reopen_requests"),
    path("reopen-requests/<int:pk>", views.decide, name="decide"),
    path("versions/<int:pk>/comments", views.comment, name="comment"),
    path("attachments/<int:pk>", views.attachment, name="attachment"),
    path("versions/<int:pk>/records/", views.records, name="records"),
    path("versions/<int:pk>/records/correction", views.correction_upload, name="correction_upload"),
    path("versions/<int:pk>/records/correction/<str:token>/", views.correction, name="correction"),
    path("versions/<int:pk>/records/correction/<str:token>/changes.csv", views.correction_csv, name="correction_csv"),
    path("versions/<int:pk>/records/correction/<str:token>/apply", views.correction_apply, name="correction_apply"),
    path("versions/<int:pk>/records/<str:table>/", views.record, name="record"),
    path("versions/<int:pk>/records/<str:table>/new", views.record_new, name="record_new"),
    path("versions/<int:pk>/records/<str:table>/delete", views.record_delete, name="record_delete"),
]

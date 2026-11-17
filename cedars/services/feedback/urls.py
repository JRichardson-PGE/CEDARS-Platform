from django.urls import path

from . import views

app_name = "feedback"
urlpatterns = [
    path("feedback/", views.form, name="form"),
    path("feedback/mine/", views.mine, name="mine"),
    path("feedback/organization/", views.organization, name="organization"),
    path("feedback/<int:pk>/", views.request_page, name="request"),
    path("feedback/<int:pk>/reply", views.reply, name="reply"),
    path("feedback/<int:pk>/settle", views.settle, name="settle"),
    path("admin/feedback/", views.inbox, name="inbox"),
    path("admin/feedback/<int:pk>/", views.detail, name="detail"),
    path("admin/feedback/<int:pk>/start", views.start, name="start"),
    path("admin/feedback/<int:pk>/answer", views.answer, name="answer"),
    path("admin/feedback/<int:pk>/ask", views.ask, name="ask"),
    path("admin/feedback/<int:pk>/note", views.note, name="note"),
    path("admin/feedback/<int:pk>/close", views.close, name="close"),
    path("admin/feedback/<int:pk>/reopen", views.reopen, name="reopen"),
]

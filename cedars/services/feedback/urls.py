from django.urls import path

from . import views

app_name = "feedback"
urlpatterns = [
    path("feedback/", views.form, name="form"),
    path("admin/feedback/", views.inbox, name="inbox"),
    path("admin/feedback/<int:pk>/", views.detail, name="detail"),
    path("admin/feedback/<int:pk>/close", views.close, name="close"),
    path("admin/feedback/<int:pk>/reopen", views.reopen, name="reopen"),
]

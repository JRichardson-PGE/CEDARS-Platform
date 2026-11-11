from django.urls import path

from . import views

app_name = "releases"
urlpatterns = [
    path("", views.index, name="index"),
    path("feed.xml", views.ReleaseFeed(), name="feed"),
    path("subscribe", views.subscribe, name="subscribe"),
    path("put-away", views.put_away, name="put_away"),
    path("manage/", views.manage, name="manage"),
    path("manage/<int:pk>/", views.edit, name="edit"),
    path("notices/", views.notices, name="notices"),
    path("notices/<int:pk>/withdraw", views.withdraw_notice, name="withdraw_notice"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/withdraw", views.withdraw, name="withdraw"),
]

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("cedars.core.urls")),
    path("accounts/", include("allauth.urls")),
    path("", include("cedars.modules.administration.urls")),
    path("django-admin/", admin.site.urls),
]

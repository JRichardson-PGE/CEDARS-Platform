from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("cedars.core.urls")),
    path("accounts/", include("allauth.urls")),
    path("admin/retention/", include("cedars.services.retention.urls")),
    path("", include("cedars.modules.administration.urls")),
    path("dictionary/", include("cedars.services.dictionary.urls")),
    path("rules/", include("cedars.services.rules.urls")),
    path("filings/", include("cedars.services.submissions.urls")),
    path("reference-data/", include("cedars.services.refdata.urls")),
    path("notifications/", include("cedars.services.notifications.urls")),
    path("django-admin/", admin.site.urls),
]

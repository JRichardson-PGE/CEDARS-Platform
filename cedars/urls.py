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
    path("programs/", include("cedars.modules.programs.urls")),
    path("impact-profiles/", include("cedars.modules.impact_profiles.urls")),
    path("api/v1/", include("cedars.core.api_urls")),
    path("calculations/", include("cedars.modules.calculations.urls")),
    path("django-admin/", admin.site.urls),
]

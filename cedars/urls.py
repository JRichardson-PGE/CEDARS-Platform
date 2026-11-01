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
    path("releases/", include("cedars.services.releases.urls")),
    path("limits/", include("cedars.services.releases.limits_urls")),
    path("notifications/", include("cedars.services.notifications.urls")),
    path("programs/", include("cedars.modules.programs.urls")),
    path("budgets/", include("cedars.modules.budgets.urls")),
    path("claims/", include("cedars.modules.claims.urls")),
    path("impact-profiles/", include("cedars.modules.impact_profiles.urls")),
    path("monthly/", include("cedars.modules.monthly.urls")),
    path("documents/", include("cedars.modules.documents.urls")),
    path("data/", include("cedars.modules.data.urls")),
    path("deer-resources/", include("cedars.modules.documents.legacy_urls")),
    path("api/v1/calculations/", include("cedars.modules.calculations.api_urls")),
    path("api/v1/claims/", include("cedars.modules.claims.api_urls")),
    path("api/v1/", include("cedars.core.api_urls")),
    path("calculations/", include("cedars.modules.calculations.urls")),
    path("django-admin/", admin.site.urls),
]

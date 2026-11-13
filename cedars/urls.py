from django.urls import include, path

urlpatterns = [
    path("", include("cedars.core.urls")),
    path("accounts/", include("allauth.urls")),
    path("admin/retention/", include("cedars.services.retention.urls")),
    path("admin/document-accessibility/", include("cedars.services.accessibility.urls")),
    path("", include("cedars.modules.administration.urls")),
    path("", include("cedars.services.feedback.urls")),
    path("dictionary/", include("cedars.services.dictionary.urls")),
    path("rules/", include("cedars.services.rules.urls")),
    path("specifications/", include("cedars.services.specifications.urls")),
    path("filings/", include("cedars.services.submissions.urls")),
    path("reference-data/", include("cedars.services.refdata.urls")),
    path("releases/", include("cedars.services.releases.urls")),
    path("limits/", include("cedars.services.releases.limits_urls")),
    path("mandates/", include("cedars.services.releases.mandates_urls")),
    path("notifications/", include("cedars.services.notifications.urls")),
    path("programs/", include("cedars.modules.programs.urls")),
    path("budgets/", include("cedars.modules.budgets.urls")),
    path("claims/", include("cedars.modules.claims.urls")),
    path("impact-profiles/", include("cedars.modules.impact_profiles.urls")),
    path("monthly/", include("cedars.modules.monthly.urls")),
    path("documents/", include("cedars.modules.documents.urls")),
    path("data/", include("cedars.modules.data.urls")),
    path("publication/", include("cedars.services.snapshots.urls")),
    path("deer-resources/", include("cedars.modules.documents.legacy_urls")),
    path("api/v1/calculations/", include("cedars.modules.calculations.api_urls")),
    path("api/v1/claims/", include("cedars.modules.claims.api_urls")),
    path("api/v1/rules/", include("cedars.services.rules.api_urls")),
    path("api/v1/specifications/", include("cedars.services.specifications.api_urls")),
    path("api/v1/data/", include("cedars.modules.data.api_urls")),
    path("api/v1/", include("cedars.core.api_urls")),
    path("calculations/", include("cedars.modules.calculations.urls")),
]

# The error pages, in the site's layout (cedars/core/errors.py): one page for "not found" and "your account cannot open it".
handler404 = "cedars.core.errors.not_found"
handler403 = "cedars.core.errors.permission_denied"
handler500 = "cedars.core.errors.server_error"

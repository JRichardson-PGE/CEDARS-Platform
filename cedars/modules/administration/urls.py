from django.urls import path, re_path
from django.views.generic import RedirectView

from . import views

app_name = "administration"
urlpatterns = [
    path("account/", views.account, name="account"),
    path("account/organization", views.switch_organisation, name="switch_organisation"),
    path("account/request-access/", views.request_access, name="request_access"),
    path("account/requests/<int:pk>/withdraw", views.withdraw_request, name="withdraw_request"),
    path("account/tokens", views.token_create, name="token_create"),
    path("account/tokens/<int:pk>/revoke", views.token_revoke, name="token_revoke"),
    path("admin/organizations/", views.organisations, name="organisations"),
    path("admin/organizations/new/", views.organisation_new, name="organisation_new"),
    path("admin/organizations/<str:code>/", views.organisation, name="organisation"),
    path("admin/organizations/<str:code>/details", views.organisation_details, name="organisation_details"),
    path("admin/organizations/<str:code>/rename", views.organisation_rename, name="organisation_rename"),
    path("admin/organizations/<str:code>/former-code", views.organisation_former_code, name="organisation_former_code"),
    path("admin/organizations/<str:code>/merge", views.organisation_merge, name="organisation_merge"),
    path("admin/organizations/<str:code>/grant", views.grant, name="grant"),
    path("admin/organizations/<str:code>/grant-template", views.grant_template, name="grant_template"),
    path("admin/organizations/<str:code>/recertification", views.open_recertification, name="open_recertification"),
    # The pages moved to US spelling (2026-10-31); old links still land
    re_path(r"^admin/organisations/(?P<rest>.*)$", RedirectView.as_view(url="/admin/organizations/%(rest)s", query_string=True)),
    path("admin/grants/<int:pk>/revoke", views.revoke, name="revoke"),
    path("admin/grants/<int:pk>/reinstate", views.reinstate, name="reinstate"),
    path("admin/memberships/<int:pk>/end", views.end_membership, name="end_membership"),
    path("admin/requests/<int:pk>/decide", views.decide_request, name="decide_request"),
    path("admin/recertification/<int:pk>", views.recertify, name="recertify"),
    path("admin/stewardship/", views.stewardship, name="stewardship"),
    path("admin/stewardship/grants/<int:pk>", views.pii_decision, name="pii_decision"),
    path("admin/stewardship/changes/propose", views.propose_change, name="propose_change"),
    path("admin/stewardship/changes/<int:pk>", views.decide_change, name="decide_change"),
    path("admin/settings/", views.access_settings, name="settings"),
    path("admin/role-templates/", views.role_templates, name="role_templates"),
    path("admin/role-templates/<int:pk>/", views.role_template, name="role_template"),
    path("admin/audit/", views.audit_log, name="audit"),
    path("admin/health/", views.health_page, name="health"),
    # everyone signed in reads the factors, and the funding PAs manage them (Jake, 2026-11-02), so they left /admin/
    path("allocations/", views.allocations, name="allocations"),
    path("allocations/<int:pk>/", views.allocation, name="allocation"),
    path("allocations/<int:pk>/factors.csv", views.allocation_csv, name="allocation_csv"),
    re_path(r"^admin/allocations/(?P<rest>.*)$", RedirectView.as_view(url="/allocations/%(rest)s", query_string=True)),
    path("admin/goals/", views.goal_sets, name="goal_sets"),
    path("admin/goals/<int:pk>/", views.goal_set, name="goal_set"),
    path("admin/goals/<int:pk>/goals.csv", views.goal_set_csv, name="goal_set_csv"),
]

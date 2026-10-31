from django.urls import path

from . import views

app_name = "administration"
urlpatterns = [
    path("account/", views.account, name="account"),
    path("account/organisation", views.switch_organisation, name="switch_organisation"),
    path("account/request-access/", views.request_access, name="request_access"),
    path("account/requests/<int:pk>/withdraw", views.withdraw_request, name="withdraw_request"),
    path("account/tokens", views.token_create, name="token_create"),
    path("account/tokens/<int:pk>/revoke", views.token_revoke, name="token_revoke"),
    path("admin/organisations/", views.organisations, name="organisations"),
    path("admin/organisations/new/", views.organisation_new, name="organisation_new"),
    path("admin/organisations/<str:code>/", views.organisation, name="organisation"),
    path("admin/organisations/<str:code>/grant", views.grant, name="grant"),
    path("admin/organisations/<str:code>/recertification", views.open_recertification, name="open_recertification"),
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
    path("admin/audit/", views.audit_log, name="audit"),
    path("admin/allocations/", views.allocations, name="allocations"),
    path("admin/allocations/<int:pk>/", views.allocation, name="allocation"),
    path("admin/allocations/<int:pk>/factors.csv", views.allocation_csv, name="allocation_csv"),
    path("admin/goals/", views.goal_sets, name="goal_sets"),
    path("admin/goals/<int:pk>/", views.goal_set, name="goal_set"),
    path("admin/goals/<int:pk>/goals.csv", views.goal_set_csv, name="goal_set_csv"),
]

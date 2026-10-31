import pytest

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.accounts.models import AccessRequest, Grant, PolicyChange

pytestmark = pytest.mark.django_db


# --- sign-in and MFA (decision F)


def test_password_only_sessions_are_sent_to_set_up_a_second_factor(signed_in, make_user):
    c = signed_in(make_user("new@example.com"), "password")
    r = c.get("/account/")
    assert r.status_code == 302 and r["Location"].startswith("/accounts/2fa/totp/activate")
    assert c.get("/about/").status_code == 200  # public pages stay open
    assert c.get("/accounts/2fa/").status_code == 200


def test_mfa_and_federated_sessions_get_in(signed_in, make_user):
    u = make_user("u@example.com")
    r = signed_in(u, "mfa").get("/account/")
    assert r.status_code == 200 and "password and a second factor" in r.content.decode()
    r = signed_in(u, "socialaccount").get("/account/")
    assert "your organisation's own sign-in" in r.content.decode()


def test_signed_out_users_go_to_sign_in(client):
    r = client.get("/account/")
    assert r.status_code == 302 and r["Location"].startswith("/accounts/login/")


def test_sign_in_page_renders_in_the_platform_layout(client):
    html = client.get("/accounts/login/").content.decode()
    assert '<a class="skip" href="#main">' in html and 'autocomplete="email"' in html


# --- the account page and access requests


def test_community_account_requests_access(signed_in, make_user, pge, pge_admin):
    u = make_user("community@example.com")
    c = signed_in(u)
    assert "community account" in c.get("/account/").content.decode()
    r = c.post("/account/request-access/", {"organisation": pge.pk, "permissions": ["claims.read"], "message": "hi"})
    assert r.status_code == 302
    req = AccessRequest.objects.get(user=u)
    assert req.permissions == ["claims.read"]
    admin = signed_in(pge_admin)
    page = admin.get(f"/admin/organisations/{pge.code}/").content.decode()
    assert "community@example.com" in page
    admin.post(f"/admin/requests/{req.pk}/decide", {"decision": "approve", "permissions": ["claims.read"]})
    assert service.effective_permissions(u, pge) == {"claims.read"}


def test_switching_organisation(signed_in, make_user, cpuc_admin, pge, pge_admin):
    sce = service.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa")
    u = make_user("both@example.com")
    service.grant(pge_admin, u, pge, "claims.read")
    sce_admin = make_user("admin@sce.com")
    service.grant(cpuc_admin, sce_admin, sce, perms.ORG_ADMIN)
    service.grant(sce_admin, u, sce, "claims.read")
    c = signed_in(u)
    assert 'id="org-switch"' in c.get("/account/").content.decode()
    c.post("/account/organisation", {"organisation": "SCE", "next": "/account/"})
    assert "Acting for</dt><dd>SCE" in c.get("/account/").content.decode()
    c.post("/account/organisation", {"organisation": "CPUC", "next": "/account/"})  # not a member: refused
    assert "Acting for</dt><dd>SCE" in c.get("/account/").content.decode()


# --- organisation admins


def test_admin_grants_and_revokes_through_the_page(signed_in, make_user, pge, pge_admin):
    u = make_user("analyst@pge.com")
    c = signed_in(pge_admin)
    c.post(f"/admin/organisations/{pge.code}/grant", {"email": u.email, "permission": "claims.submit", "reason": "new analyst"})
    g = Grant.objects.get(membership__user=u, permission="claims.submit")
    assert g.status == "active" and g.grant_reason == "new analyst"
    r = c.post(f"/admin/grants/{g.pk}/revoke", {"reason": "moved teams"}, follow=True)
    assert "Revoked Claims: submit" in r.content.decode()
    g.refresh_from_db()
    assert g.status == "revoked"


def test_grant_for_an_unknown_email_explains(signed_in, pge, pge_admin):
    r = signed_in(pge_admin).post(
        f"/admin/organisations/{pge.code}/grant", {"email": "nobody@pge.com", "permission": "claims.read"}, follow=True
    )
    assert "There is no account for nobody@pge.com" in r.content.decode()


def test_other_organisations_are_hidden(signed_in, make_user, cpuc_admin, pge, pge_admin):
    sce = service.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa")
    assert signed_in(pge_admin).get(f"/admin/organisations/{sce.code}/").status_code == 404
    assert signed_in(make_user("nobody@example.com")).get("/admin/organisations/").status_code == 404


def test_cpuc_assigns_a_first_admin_through_the_page(signed_in, make_user, cpuc_admin, pge):
    u = make_user("first@pge.com")
    c = signed_in(cpuc_admin)
    page = c.get(f"/admin/organisations/{pge.code}/").content.decode()
    assert "Assign the first administrator" in page
    c.post(f"/admin/organisations/{pge.code}/grant", {"email": u.email, "permission": perms.ORG_ADMIN})
    assert service.is_org_admin(u, pge)
    page = c.get(f"/admin/organisations/{pge.code}/").content.decode()
    assert "Assign the first administrator" not in page and "Grant a permission" not in page


def test_recertification_through_the_page(signed_in, make_user, pge, pge_admin):
    u = make_user("a@pge.com")
    g = service.grant(pge_admin, u, pge, "claims.read")
    c = signed_in(pge_admin)
    c.post(f"/admin/organisations/{pge.code}/recertification", {"due": "2099-01-31"})
    item = g.recertification_items.get()
    c.post(f"/admin/recertification/{item.pk}", {"decision": "certify"})
    g.refresh_from_db()
    assert g.certified_by == pge_admin


# --- stewards and CPUC administrators


def test_steward_approves_pii_through_the_page(signed_in, make_user, cpuc_admin, pge, pge_admin):
    u = make_user("pii@pge.com")
    g = service.grant(pge_admin, u, pge, perms.PII)
    c = signed_in(cpuc_admin)
    assert "pii@pge.com" in c.get("/admin/stewardship/").content.decode()
    c.post(f"/admin/stewardship/grants/{g.pk}", {"decision": "approve"})
    g.refresh_from_db()
    assert g.status == "active"
    assert signed_in(pge_admin).get("/admin/stewardship/").status_code == 404


def test_policy_change_needs_two_stewards_through_the_page(signed_in, make_user, cpuc_admin, cpuc):
    second = make_user("s2@cpuc.ca.gov")
    service.grant(cpuc_admin, second, cpuc, perms.STEWARD)
    signed_in(cpuc_admin).post("/admin/stewardship/changes/propose", {"value": "true", "reason": "security review"})
    change = PolicyChange.objects.get()
    page = signed_in(cpuc_admin).get("/admin/stewardship/").content.decode()
    assert "A second steward must confirm your proposal" in page
    signed_in(second).post(f"/admin/stewardship/changes/{change.pk}", {"decision": "confirm"})
    change.refresh_from_db()
    assert change.status == "confirmed"


def test_settings_and_audit_pages(signed_in, cpuc_admin, pge, pge_admin):
    c = signed_in(cpuc_admin)
    c.post("/admin/settings/", {"recertification_interval_days": 180, "reason": "twice a year"})
    page = c.get("/admin/audit/?action=access_policy").content.decode()
    assert "access_policy.changed" in page and "twice a year" in page
    assert signed_in(pge_admin).get("/admin/settings/").status_code == 404
    pge_view = signed_in(pge_admin).get("/admin/audit/").content.decode()
    assert "grant.created" in pge_view and "access_policy.changed" not in pge_view


def test_add_organisation_page(signed_in, cpuc_admin):
    c = signed_in(cpuc_admin)
    r = c.post("/admin/organisations/new/", {"code": "bayren", "name": "Bay Area Regional Energy Network", "kind": "pa", "pa_type": "REN"})
    assert r.status_code == 302
    assert service.cpuc() is not None
    from cedars.services.accounts.models import Organisation

    assert Organisation.objects.get(code="BAYREN").pa_type == "REN"

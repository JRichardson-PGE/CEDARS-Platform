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


def test_a_password_session_gets_the_whole_header_when_mfa_is_not_required(signed_in, make_user, settings):
    # A laptop demo turns the requirement off (CEDARS_REQUIRE_MFA=0 in dev): the header must follow the middleware and offer the
    # modules, not "Finish setting up your second factor" with nothing to set up.
    c = signed_in(make_user("demo@example.com"), "password")
    html = c.get("/about/").content.decode()
    assert "Finish setting up your second factor" in html and 'id="main-nav"' not in html
    settings.CEDARS_REQUIRE_MFA = False
    r = c.get("/account/")
    assert r.status_code == 200
    html = r.content.decode()
    assert 'id="main-nav"' in html and "Finish setting up your second factor" not in html and "Sign out" in html


def test_mfa_and_federated_sessions_get_in(signed_in, make_user):
    u = make_user("u@example.com")
    r = signed_in(u, "mfa").get("/account/")
    assert r.status_code == 200 and "password and a second factor" in r.content.decode()
    r = signed_in(u, "socialaccount").get("/account/")
    assert "your organization's own sign-in" in r.content.decode()


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
    page = admin.get(f"/admin/organizations/{pge.code}/").content.decode()
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
    c.post("/account/organization", {"organisation": "SCE", "next": "/account/"})
    assert "Acting for</dt><dd>SCE" in c.get("/account/").content.decode()
    c.post("/account/organization", {"organisation": "CPUC", "next": "/account/"})  # not a member: refused
    assert "Acting for</dt><dd>SCE" in c.get("/account/").content.decode()


# --- organization admins


def test_admin_grants_and_revokes_through_the_page(signed_in, make_user, pge, pge_admin):
    u = make_user("analyst@pge.com")
    c = signed_in(pge_admin)
    c.post(f"/admin/organizations/{pge.code}/grant", {"email": u.email, "permission": "claims.submit", "reason": "new analyst"})
    g = Grant.objects.get(membership__user=u, permission="claims.submit")
    assert g.status == "active" and g.grant_reason == "new analyst"
    r = c.post(f"/admin/grants/{g.pk}/revoke", {"reason": "moved teams"}, follow=True)
    assert "Revoked Claims: submit" in r.content.decode()
    g.refresh_from_db()
    assert g.status == "revoked"


def test_grant_for_an_unknown_email_explains(signed_in, pge, pge_admin):
    r = signed_in(pge_admin).post(
        f"/admin/organizations/{pge.code}/grant", {"email": "nobody@pge.com", "permission": "claims.read"}, follow=True
    )
    assert "There is no account for nobody@pge.com" in r.content.decode()


def test_other_organisations_are_hidden(signed_in, make_user, cpuc_admin, pge, pge_admin):
    sce = service.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa")
    assert signed_in(pge_admin).get(f"/admin/organizations/{sce.code}/").status_code == 404
    assert signed_in(make_user("nobody@example.com")).get("/admin/organizations/").status_code == 404


def test_cpuc_assigns_a_first_admin_through_the_page(signed_in, make_user, cpuc_admin, pge):
    u = make_user("first@pge.com")
    c = signed_in(cpuc_admin)
    page = c.get(f"/admin/organizations/{pge.code}/").content.decode()
    assert "Assign the first administrator" in page
    c.post(f"/admin/organizations/{pge.code}/grant", {"email": u.email, "permission": perms.ORG_ADMIN})
    assert service.is_org_admin(u, pge)
    page = c.get(f"/admin/organizations/{pge.code}/").content.decode()
    assert "Assign the first administrator" not in page and "Grant a permission" not in page


def test_recertification_through_the_page(signed_in, make_user, pge, pge_admin):
    u = make_user("a@pge.com")
    g = service.grant(pge_admin, u, pge, "claims.read")
    c = signed_in(pge_admin)
    c.post(f"/admin/organizations/{pge.code}/recertification", {"due": "2099-01-31"})
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
    r = c.post("/admin/organizations/new/", {"code": "bayren", "name": "Bay Area Regional Energy Network", "kind": "pa", "pa_type": "REN"})
    assert r.status_code == 302
    assert service.cpuc() is not None
    from cedars.services.accounts.models import Organisation

    assert Organisation.objects.get(code="BAYREN").pa_type == "REN"


# --- the second-factor set-up flow and the sign-in pages' wording (UAT F11, F12)


def _totp_code(secret):
    from allauth.mfa.totp.internal.auth import format_hotp_value, hotp_value, yield_hotp_counters_from_time

    return format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))


@pytest.mark.django_db(transaction=True)  # allauth records the activation on commit, inside the view: a real transaction shows it
def test_a_new_account_sets_up_its_second_factor_with_one_password(client, make_user):
    from allauth.account.models import EmailAddress

    u = make_user("fresh@example.com")
    EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    r = client.post("/accounts/login/", {"login": u.email, "password": "correct horse battery staple"})
    assert r.status_code == 302
    last = client.session["account_authentication_methods"][-1]
    assert last["method"] == "password" and last["reauthenticated"] is True  # the fresh sign-in counts as recent for set-up
    r = client.get("/accounts/2fa/totp/activate/")
    assert r.status_code == 200, r["Location"] if r.status_code == 302 else ""  # not sent to type the password again
    html = r.content.decode()
    assert (
        'id="authenticator_secret"' in html and "readonly" in html and "disabled" not in html.split('id="authenticator_secret"')[0][-200:]
    )
    assert 'data-copy="authenticator_secret"' in html
    assert "Finish setting up your second factor" in html and 'id="main-nav"' not in html  # no module menu yet
    r = client.post("/accounts/2fa/totp/activate/", {"code": _totp_code(client.session["mfa.totp.secret"])})
    assert r.status_code == 302 and "reauthenticate" not in r["Location"]
    assert {m["method"] for m in client.session["account_authentication_methods"]} >= {"password", "mfa"}
    r = client.get("/account/")
    assert r.status_code == 200 and 'id="main-nav"' in r.content.decode()  # the activation was the second factor


def test_sign_in_pages_and_emails_speak_for_cedars(client, mailoutbox):
    html = client.get("/accounts/login/").content.decode()
    assert "Sign in to CEDARS" in html and "Email address" in html and "Keep me signed in" in html
    assert "Remember Me" not in html and "please sign up" not in html
    r = client.post(
        "/accounts/signup/",
        {"email": "newcomer@example.org", "password1": "a long and sturdy passphrase 7", "password2": "a long and sturdy passphrase 7"},
    )
    assert r.status_code == 302, r.content.decode()[:500]
    (m,) = mailoutbox
    assert m.subject == "[CEDARS] Confirm your email address"
    assert "CEDARS account" in m.body and "Hello from" not in m.body and "127.0.0.1" not in m.body


def test_access_requests_offer_what_a_new_member_needs():
    from cedars.modules.administration import forms

    offered = [c for c, _ in forms.RequestAccessForm().fields["permissions"].choices]
    assert offered and all(c.endswith((".read", ".submit")) for c in offered)
    assert perms.ORG_ADMIN not in offered and perms.PII not in offered and "claims.approve" not in offered

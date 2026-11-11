"""CPUC system administrators manage access and workflow settings on the platform's own pages (Jake, 2026-11-11): each filing type's
correction policy, approvers' delegates, deactivating and reactivating accounts, people's names and email addresses, and an
organization's directory sign-in and retirement. Every change is audited."""

import re
import time
from datetime import timedelta

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.accounts import tokens
from cedars.services.accounts.models import Organisation, PolicyChange, User
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.submissions import service as submissions
from cedars.services.submissions.models import Delegation

from .conftest import page_only, sign_in

pytestmark = pytest.mark.django_db


def _today():
    return timezone.localdate()


# --- workflow settings


def test_workflow_settings_are_for_system_administrators(signed_in, cpuc_admin, pge_admin):
    assert signed_in(pge_admin).get("/filings/workflow-settings/").status_code == 404
    html = signed_in(cpuc_admin).get("/filings/workflow-settings/").content.decode()
    assert "<h1>Workflow settings</h1>" in html
    for module in ("budgets", "claims", "monthly"):
        assert f'id="wf-{module}-h"' in html
    assert "Allowed with an approver&#x27;s approval" in html


def test_changing_a_policy_needs_a_reason_and_is_audited(signed_in, cpuc_admin):
    c = signed_in(cpuc_admin)
    r = c.post("/filings/workflow-settings/", {"module": "claims", "claims-reopen_after_due": "open", "claims-late_confirmation": "on"})
    assert r.status_code == 200 and "This field is required" in r.content.decode()  # no reason: the form stays open with the error
    assert submissions.policy("claims").reopen_after_due == "approval"
    r = c.post(
        "/filings/workflow-settings/",
        {"module": "claims", "claims-reopen_after_due": "open", "claims-late_confirmation": "on", "claims-reason": "close-out moved"},
        follow=True,
    )
    assert submissions.policy("claims").reopen_after_due == "open"
    e = AuditEvent.objects.get(action="workflow_policy.changed")
    assert (e.before, e.after, e.reason, e.actor) == (
        {"reopen_after_due": "approval"},
        {"reopen_after_due": "open"},
        "close-out moved",
        cpuc_admin,
    )
    html = r.content.decode()
    assert "Write a release note" not in page_only(html)  # loosening the rule needs no announcement
    assert "corrections: allowed with an approver" in html and "close-out moved" in html  # the history row
    assert "Allowed with a reason, no approval" in c.get("/filings/calendar/").content.decode()


def test_a_tightening_change_offers_a_release_note(signed_in, cpuc_admin):
    c = signed_in(cpuc_admin)
    r = c.post(
        "/filings/workflow-settings/",
        {"module": "budgets", "budgets-reopen_after_due": "closed", "budgets-reason": "no edits after close-out"},
        follow=True,
    )
    p = submissions.policy("budgets")
    assert (p.reopen_after_due, p.late_confirmation) == ("closed", False)
    html = page_only(r.content.decode())
    link = re.search(r'href="(/releases/manage/\?[^"]+)">Write a release note', html).group(1).replace("&amp;", "&")
    form = c.get(link).content.decode()
    assert "Budgets: workflow change" in form and "no edits after close-out" in form
    assert "Write a release note" not in page_only(c.get("/filings/workflow-settings/").content.decode())  # offered once


def test_the_service_refuses_unknown_settings_and_other_people(cpuc_admin, pge_admin):
    with pytest.raises(AccessDenied, match="system administrator"):
        submissions.set_policy(pge_admin, "claims", reopen_after_due="open")
    with pytest.raises(AccessDenied, match="not a workflow setting"):
        submissions.set_policy(cpuc_admin, "claims", due_days=3)
    with pytest.raises(AccessDenied, match="Choose whether"):
        submissions.set_policy(cpuc_admin, "claims", reopen_after_due="sometimes")
    assert submissions.policy_tightens({"reopen_after_due": "open"}, {"reopen_after_due": "approval"})
    assert submissions.policy_tightens({"late_confirmation": True}, {"late_confirmation": False})
    assert not submissions.policy_tightens({"reopen_after_due": "closed"}, {"reopen_after_due": "approval"})
    assert submissions.policy("claims").reopen_after_due == "approval"


# --- approver delegates


@pytest.fixture
def approver(make_user, cpuc_admin, cpuc):
    u = make_user("approver@cpuc.example.org", "Avery Prover")
    access.grant(cpuc_admin, u, cpuc, "claims.approve")
    return u


@pytest.fixture
def deputy(make_user, cpuc_admin, cpuc):
    u = make_user("deputy@cpuc.example.org", "Dee Puty")
    access.grant(cpuc_admin, u, cpuc, "claims.read")
    return u


def test_an_approver_names_and_ends_a_delegate(signed_in, approver, deputy):
    c = signed_in(approver)
    html = c.get("/filings/delegates/").content.decode()
    assert "<h1>Approver delegates</h1>" in html and "You approve: Claims." in html
    r = c.post(
        "/filings/delegates/",
        {"module": "claims", "delegate": deputy.email, "starts": _today(), "ends": _today() + timedelta(days=5), "reason": "leave"},
        follow=True,
    )
    d = Delegation.objects.get()
    assert (d.approver, d.delegate, d.created_by, d.reason) == (approver, deputy, approver, "leave")
    assert submissions.approver_for(deputy, "claims") == approver
    assert "In force" in r.content.decode()
    # the delegate sees whom they decide for
    assert "You decide for" in signed_in(deputy).get("/filings/delegates/").content.decode()
    c = signed_in(approver)
    c.post(f"/filings/delegates/{d.pk}/end", {"reason": "back early"})
    d.refresh_from_db()
    assert d.ended_at and d.ended_by == approver and d.end_reason == "back early"
    assert submissions.approver_for(deputy, "claims") is None
    assert AuditEvent.objects.filter(action="delegation.ended", object_id=str(d.pk)).exists()


def test_a_delegate_must_be_cpuc_staff_who_can_read_the_module(approver, make_user, pge, cpuc_admin):
    outsider = make_user("someone@pge.example.org")
    access.grant(cpuc_admin, make_user("pgeadmin2@pge.example.org"), pge, perms.ORG_ADMIN)
    with pytest.raises(AccessDenied, match="member of the CPUC who can read Claims"):
        submissions.delegate(approver, module="claims", delegate_to=outsider, starts=_today(), ends=_today())
    with pytest.raises(AccessDenied, match="their own delegate"):
        submissions.delegate(approver, module="claims", delegate_to=approver, starts=_today(), ends=_today())
    with pytest.raises(AccessDenied, match="not an approver for Budgets"):
        submissions.delegate(approver, module="budgets", delegate_to=outsider, starts=_today(), ends=_today())


def test_a_system_administrator_names_a_delegate_on_an_approvers_behalf(signed_in, cpuc_admin, approver, deputy, make_user):
    with pytest.raises(AccessDenied, match="reason"):
        submissions.delegate(cpuc_admin, module="claims", delegate_to=deputy, starts=_today(), ends=_today(), approver=approver)
    c = signed_in(cpuc_admin)
    c.post(
        "/filings/delegates/",
        {
            "module": "claims",
            "approver": approver.email,
            "delegate": deputy.email,
            "starts": _today(),
            "ends": _today() + timedelta(days=2),
            "reason": "approver on leave",
        },
    )
    d = Delegation.objects.get()
    assert (d.approver, d.created_by) == (approver, cpuc_admin)
    assert "Every delegation in force or coming" in c.get("/filings/delegates/").content.decode()
    # someone with no approvals and no delegation sees no page
    assert signed_in(make_user("nobody@example.org")).get("/filings/delegates/").status_code == 404


def test_overlapping_and_past_delegations_are_refused(approver, deputy):
    submissions.delegate(approver, module="claims", delegate_to=deputy, starts=_today(), ends=_today() + timedelta(days=3))
    with pytest.raises(AccessDenied, match="already"):
        submissions.delegate(
            approver, module="claims", delegate_to=deputy, starts=_today() + timedelta(days=2), ends=_today() + timedelta(days=9)
        )
    with pytest.raises(AccessDenied, match="already be over"):
        submissions.delegate(
            approver, module="claims", delegate_to=deputy, starts=_today() - timedelta(days=9), ends=_today() - timedelta(days=1)
        )


# --- deactivating accounts


def test_deactivating_signs_out_revokes_tokens_and_keeps_everything(signed_in, cpuc_admin, pge, pge_admin, make_user):
    analyst = make_user("analyst@pge.example.org", "Ann Alyst")
    access.grant(pge_admin, analyst, pge, "claims.read")
    token, key = tokens.create_token(analyst, pge, name="nightly", scopes=["claims"], days=30)
    theirs = sign_in(Client(), analyst)
    assert theirs.get("/account/").status_code == 200
    c = signed_in(cpuc_admin)
    html = c.get(f"/admin/people/{analyst.pk}/").content.decode()
    assert "Ann Alyst" in html and "Deactivate the account" in html
    c.post(f"/admin/people/{analyst.pk}/deactivate", {"reason": "left PG&E"})
    analyst.refresh_from_db()
    token.refresh_from_db()
    assert not analyst.is_active and analyst.deactivated_by == cpuc_admin and analyst.deactivation_reason == "left PG&E"
    assert token.revoked_at is not None and tokens.authenticate(key) is None
    assert theirs.get("/account/").status_code == 302  # signed out
    assert analyst.memberships.filter(ended_at__isnull=True).exists()  # kept for reactivation
    e = AuditEvent.objects.get(action="user.deactivated")
    assert e.reason == "left PG&E" and e.after["tokens_revoked"] == [token.prefix] and e.after["sessions_ended"] == 1
    assert "Deactivated" in c.get("/admin/people/?status=deactivated").content.decode()
    c.post(f"/admin/people/{analyst.pk}/reactivate", {"reason": "came back"})
    analyst.refresh_from_db()
    assert analyst.is_active and access.effective_permissions(analyst, pge) == {"claims.read"}
    assert AuditEvent.objects.filter(action="user.reactivated", reason="came back").exists()


def test_the_last_system_administrator_and_yourself_are_refused(signed_in, cpuc_admin, cpuc, make_user):
    with pytest.raises(AccessDenied, match="last CPUC system administrator"):
        access.deactivate_user(None, cpuc_admin, "test")
    second = make_user("second@cpuc.example.org")
    access.grant(cpuc_admin, second, cpuc, perms.SYSTEM_ADMIN)
    with pytest.raises(AccessDenied, match="last data steward"):
        access.deactivate_user(second, cpuc_admin, "test")
    c = signed_in(cpuc_admin)
    c.post(f"/admin/people/{cpuc_admin.pk}/deactivate", {"reason": "oops"})
    cpuc_admin.refresh_from_db()
    assert cpuc_admin.is_active


def test_people_pages_are_for_system_administrators(signed_in, pge_admin):
    c = signed_in(pge_admin)
    assert c.get("/admin/people/").status_code == 404
    assert c.get(f"/admin/people/{pge_admin.pk}/").status_code == 404
    c.post(f"/admin/people/{pge_admin.pk}/deactivate", {"reason": "x"})
    pge_admin.refresh_from_db()
    assert pge_admin.is_active


def test_a_community_account_closes_itself_but_a_member_cannot(signed_in, make_user, pge_admin):
    member_page = signed_in(pge_admin).get("/account/").content.decode()
    assert "Close your account" not in page_only(member_page)
    with pytest.raises(AccessDenied, match="belong to an organization"):
        access.deactivate_user(pge_admin, pge_admin, "bye")
    u = make_user("community@example.org")
    c = signed_in(u)
    assert "Close your account" in c.get("/account/").content.decode()
    c.post("/account/close", {"reason": "no longer needed"})  # without the box ticked
    u.refresh_from_db()
    assert u.is_active
    c.post("/account/close", {"reason": "no longer needed", "confirm": "yes"})
    u.refresh_from_db()
    assert not u.is_active and AuditEvent.objects.get(action="user.deactivated").after["closed_by_themselves"] is True


# --- names and email addresses


def test_people_set_their_own_name_and_an_administrator_corrects_it(signed_in, cpuc_admin, make_user):
    u = make_user("named@example.org")
    c = signed_in(u)
    c.post("/account/name", {"name": "  Nora   Named "})
    u.refresh_from_db()
    assert u.name == "Nora Named" and AuditEvent.objects.get(action="user.name_changed").actor == u
    with pytest.raises(AccessDenied, match="reason"):
        access.set_name(cpuc_admin, u, "Norah Named")
    c = signed_in(cpuc_admin)
    c.post(f"/admin/people/{u.pk}/name", {"name": "Norah Named", "reason": "spelling"})
    u.refresh_from_db()
    assert u.name == "Norah Named"
    assert AuditEvent.objects.filter(action="user.name_changed", reason="spelling", actor=cpuc_admin).exists()


@pytest.mark.parametrize("recorded", [True, False], ids=["address-recorded", "made-by-operators"])
def test_an_email_change_is_verified_audited_told_and_signs_out_elsewhere(client, make_user, recorded):
    from allauth.account.models import EmailAddress

    new = f"new-{'a' if recorded else 'b'}@example.org"  # one address each: allauth limits confirmation emails per address
    u = make_user("old@example.org")
    if recorded:
        EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    elsewhere = sign_in(Client(), u)
    sign_in(client, u)
    session = client.session
    # a password confirmed just now: allauth asks for it again before an email change (ACCOUNT_REAUTHENTICATION_REQUIRED)
    session["account_authentication_methods"] = [
        {"method": "password", "at": time.time()},
        {"method": "mfa", "at": time.time()},
        {"method": "password", "at": time.time(), "reauthenticated": True},
    ]
    session.save()
    assert 'href="/accounts/email/"' in client.get("/account/").content.decode()
    r = client.post("/accounts/email/", {"action_add": "", "email": new})
    assert r.status_code == 302, r.content.decode()[:3000]
    assert r["Location"] == "/accounts/email/", r["Location"]
    u.refresh_from_db()
    assert u.email == "old@example.org"  # not until the new address is verified
    link = re.search(r"/accounts/confirm-email/[^/\s]+/", mail.outbox[-1].body).group(0)
    client.post(link)
    u.refresh_from_db()
    assert u.email == new
    e = AuditEvent.objects.get(action="user.email_changed")
    assert (e.before, e.after) == ({"email": "old@example.org"}, {"email": new})
    told = [m for m in mail.outbox if m.to == ["old@example.org"]]
    assert told and new in told[-1].body
    assert "notice of a change to your CEDARS account" in told[-1].body and "security precautions" not in told[-1].body
    assert elsewhere.get("/account/").status_code == 302  # the other session ended
    assert client.get("/account/").status_code == 200  # this one stays


# --- an organization's directory sign-in and retirement


def test_switching_directory_sign_in_without_pii_takes_effect_at_once(signed_in, cpuc_admin, pge):
    c = signed_in(cpuc_admin)
    html = c.get(f"/admin/organizations/{pge.code}/").content.decode()
    assert "Change how its members sign in" in html and "Save sign-in" in html
    c.post(f"/admin/organizations/{pge.code}/sign-in", {"federated": "on", "provider": "pge-entra", "reason": "directory ready"})
    pge.refresh_from_db()
    assert pge.federated and pge.federation_provider == "pge-entra"
    assert AuditEvent.objects.filter(action="organisation.federation_changed", reason="directory ready").exists()
    with pytest.raises(AccessDenied, match="Name the sign-in provider"):
        access.change_federation(cpuc_admin, pge, federated=True, provider="", reason="x")


def test_with_pii_grants_a_data_steward_confirms_the_switch(signed_in, cpuc_admin, cpuc, pge, pge_admin, make_user):
    access.grant(pge_admin, make_user("pii@pge.example.org"), pge, perms.PII)  # waits for a steward: still counts
    result = access.change_federation(cpuc_admin, pge, federated=True, provider="pge-entra", reason="directory ready")
    assert isinstance(result, PolicyChange)
    pge.refresh_from_db()
    assert not pge.federated
    with pytest.raises(AccessDenied, match="second data steward"):
        access.confirm_policy_change(cpuc_admin, result)  # the proposer is a steward too, but not a second one
    steward = make_user("steward@cpuc.example.org")
    access.grant(cpuc_admin, steward, cpuc, perms.STEWARD)
    html = signed_in(steward).get("/admin/stewardship/").content.decode()
    assert "Directory sign-in changes waiting" in html and "pge-entra" in html
    signed_in(steward).post(f"/admin/stewardship/changes/{result.pk}", {"decision": "confirm"})
    pge.refresh_from_db()
    assert pge.federated and pge.federation_provider == "pge-entra"
    e = AuditEvent.objects.get(action="organisation.federation_changed")
    assert e.actor == cpuc_admin and e.after["confirmed_by"] == steward.email


def test_retiring_an_organization_stops_filing_and_keeps_its_data(signed_in, cpuc_admin, pge, pge_admin):
    c = signed_in(cpuc_admin)
    c.post(f"/admin/organizations/{pge.code}/retire", {"action": "retire", "reason": "left the program"})
    pge.refresh_from_db()
    assert not pge.active and pge.successor is None
    assert pge not in access.organisations_of(pge_admin)
    assert pge not in submissions.filing_pas()  # a set-up no longer lists it among the filers
    html = c.get(f"/admin/organizations/{pge.code}/").content.decode()
    assert "Retired" in html and "Bring PGE back" in html
    assert "Retired" in c.get("/admin/organizations/").content.decode()
    c.post(f"/admin/organizations/{pge.code}/retire", {"action": "reinstate", "reason": "rejoined"})
    pge.refresh_from_db()
    assert pge.active and pge in access.organisations_of(pge_admin)
    assert [e.action for e in AuditEvent.objects.filter(action__startswith="organisation.re").order_by("pk")] == [
        "organisation.retired",
        "organisation.reinstated",
    ]


def test_the_cpuc_and_merged_organizations_are_not_retired(cpuc_admin, cpuc, pge):
    with pytest.raises(AccessDenied, match="CPUC's own organization"):
        access.retire_organisation(cpuc_admin, cpuc, "x")
    other = access.create_organisation(cpuc_admin, code="PGE2", name="PG&E again", kind="pa", pa_type="IOU")
    access.merge_organisation(cpuc_admin, other, into=pge, reason="same party")
    with pytest.raises(AccessDenied, match="merged"):
        access.retire_organisation(cpuc_admin, Organisation.objects.get(code="PGE2"), "x")
    assert User.objects.filter(is_active=True).exists()

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.accounts.models import AccessPolicy, AccessRequest, Grant
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent

pytestmark = pytest.mark.django_db


def test_permission_catalogue():
    assert "claims.submit" in perms.all_permissions()
    assert perms.label("claims.submit") == "Claims: submit"
    assert perms.label(perms.PII) == "Claims PII (vault)"
    assert not perms.is_valid("claims.fly")


# --- D6: the CPUC assigns only an organization's first admin


def test_cpuc_grants_the_first_admin_and_then_cannot(cpuc_admin, pge, make_user):
    first = make_user("first@pge.com")
    service.grant(cpuc_admin, first, pge, perms.ORG_ADMIN)
    assert service.is_org_admin(first, pge)
    second = make_user("second@pge.com")
    with pytest.raises(AccessDenied, match="already has an administrator"):
        service.grant(cpuc_admin, second, pge, perms.ORG_ADMIN)
    service.grant(first, second, pge, perms.ORG_ADMIN)
    assert service.is_org_admin(second, pge)


def test_only_the_cpuc_assigns_a_first_admin(pge, make_user):
    outsider, someone = make_user("x@example.com"), make_user("y@pge.com")
    with pytest.raises(AccessDenied, match="the CPUC assigns its first one"):
        service.grant(outsider, someone, pge, perms.ORG_ADMIN)


def test_the_first_admin_path_reopens_when_every_admin_has_left(cpuc_admin, pge, pge_admin, make_user):
    m = pge_admin.memberships.get(organisation=pge)
    other = make_user("other@pge.com")
    service.grant(pge_admin, other, pge, perms.ORG_ADMIN)
    service.end_membership(other, m, "left PG&E")
    service.end_membership(None, other.memberships.get(organisation=pge), "left PG&E too")
    assert not service.org_admins(pge).exists()
    new = make_user("new@pge.com")
    service.grant(cpuc_admin, new, pge, perms.ORG_ADMIN, reason="all admins left")
    assert service.is_org_admin(new, pge)


def test_admins_grant_module_permissions_but_not_to_themselves(pge, pge_admin, make_user):
    analyst = make_user("analyst@pge.com")
    service.grant(pge_admin, analyst, pge, "claims.submit")
    assert service.has_permission(analyst, pge, "claims.submit")
    assert not service.has_permission(analyst, pge, "claims.confirm")
    with pytest.raises(AccessDenied, match="yourself"):
        service.grant(pge_admin, pge_admin, pge, "claims.submit")
    with pytest.raises(AccessDenied, match="already has"):
        service.grant(pge_admin, analyst, pge, "claims.submit")


def test_a_grant_is_scoped_to_its_organisation(cpuc_admin, pge, pge_admin, make_user):
    sce = service.create_organisation(cpuc_admin, code="SCE", name="Southern California Edison", kind="pa", pa_type="IOU")
    analyst = make_user("analyst@pge.com")
    service.grant(pge_admin, analyst, pge, "claims.submit")
    assert not service.has_permission(analyst, sce, "claims.submit")
    with pytest.raises(AccessDenied, match="Only SCE's administrators"):
        service.grant(pge_admin, make_user("z@sce.com"), sce, "claims.submit")


def test_cpuc_only_permissions(cpuc_admin, cpuc, pge, pge_admin, make_user):
    u = make_user("u@pge.com")
    with pytest.raises(AccessDenied, match="only in the CPUC organization"):
        service.grant(cpuc_admin, u, pge, perms.SYSTEM_ADMIN)
    staff = make_user("staff@cpuc.ca.gov")
    service.grant(cpuc_admin, staff, cpuc, perms.STEWARD)
    assert service.is_steward(staff)
    with pytest.raises(AccessDenied, match="system administrator"):
        service.grant(staff, make_user("s2@cpuc.ca.gov"), cpuc, perms.STEWARD)


# --- grants: expiry, revocation, history


def test_expired_grants_stop_working(pge, pge_admin, make_user):
    u = make_user("temp@pge.com")
    g = service.grant(pge_admin, u, pge, "calculations.submit", expires_at=timezone.now() + timedelta(days=1))
    assert service.has_permission(u, pge, "calculations.submit")
    Grant.objects.filter(pk=g.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert not service.has_permission(u, pge, "calculations.submit")


def test_revoke_needs_a_reason_and_keeps_the_record(pge, pge_admin, make_user):
    u = make_user("u@pge.com")
    g = service.grant(pge_admin, u, pge, "claims.read")
    with pytest.raises(AccessDenied, match="reason"):
        service.revoke(pge_admin, g, " ")
    service.revoke(pge_admin, g, "changed teams")
    g.refresh_from_db()
    assert (g.status, g.revoked_by, g.revoke_reason) == ("revoked", pge_admin, "changed teams")
    assert not service.has_permission(u, pge, "claims.read")
    actions = list(AuditEvent.objects.filter(object_type="accounts.grant", object_id=str(g.pk)).values_list("action", flat=True))
    assert sorted(actions) == ["grant.created", "grant.revoked"]
    revoked = AuditEvent.objects.get(action="grant.revoked", object_id=str(g.pk))
    assert revoked.before["status"] == "active" and revoked.after["status"] == "revoked" and revoked.actor == pge_admin


def test_a_deactivated_account_has_no_permissions(pge, pge_admin, make_user):
    u = make_user("u@pge.com")
    service.grant(pge_admin, u, pge, "claims.read")
    u.is_active = False
    u.save()
    assert not service.has_permission(u, pge, "claims.read")


def test_ending_a_membership_demotes_to_community(pge, pge_admin, make_user):
    u = make_user("u@pge.com")
    service.grant(pge_admin, u, pge, "claims.read")
    service.grant(pge_admin, u, pge, "budgets.read")
    m = u.memberships.get(organisation=pge)
    service.end_membership(pge_admin, m, "left the company")
    assert list(service.organisations_of(u)) == []
    assert set(m.grants.values_list("status", flat=True)) == {"revoked"}
    service.grant(pge_admin, u, pge, "claims.read", reason="came back")
    assert u.memberships.filter(organisation=pge).count() == 2  # a new membership; the old one keeps its history


# --- PII (decisions F and G)


def test_pii_grants_wait_for_a_steward(cpuc_admin, pge, pge_admin, make_user):
    u = make_user("pii@pge.com")
    g = service.grant(pge_admin, u, pge, perms.PII)
    assert g.status == "pending" and not service.has_permission(u, pge, perms.PII)
    with pytest.raises(AccessDenied, match="data steward"):
        service.approve_pii(pge_admin, g)
    service.approve_pii(cpuc_admin, g)
    assert service.has_permission(u, pge, perms.PII)


def test_a_steward_cannot_approve_their_own_pii(cpuc_admin, cpuc, pge, pge_admin):
    service.grant(pge_admin, cpuc_admin, pge, "claims.read")  # the CPUC admin is also a PG&E member here
    g = service.grant(pge_admin, cpuc_admin, pge, perms.PII)
    with pytest.raises(AccessDenied, match="your own"):
        service.approve_pii(cpuc_admin, g)


def test_federated_sessions_for_pii(cpuc_admin, cpuc, pge, pge_admin, make_user):
    u = make_user("pii@pge.com")
    service.approve_pii(cpuc_admin, service.grant(pge_admin, u, pge, perms.PII))
    assert service.has_permission(u, pge, perms.PII, assurance="mfa")
    second = make_user("steward2@cpuc.ca.gov")
    service.grant(cpuc_admin, second, cpuc, perms.STEWARD)
    change = service.propose_policy_change(cpuc_admin, "pii_requires_federated", True, "CPUC security review")
    assert not AccessPolicy.current().pii_requires_federated
    with pytest.raises(AccessDenied, match="second data steward"):
        service.confirm_policy_change(cpuc_admin, change)
    service.confirm_policy_change(second, change)
    assert AccessPolicy.current().pii_requires_federated
    assert not service.has_permission(u, pge, perms.PII, assurance="mfa")  # PG&E does not federate
    pge.federated = True
    pge.save()
    assert not service.has_permission(u, pge, perms.PII, assurance="mfa")
    assert service.has_permission(u, pge, perms.PII, assurance="federated")
    with pytest.raises(AccessDenied, match="one data steward to propose"):
        service.set_policy(cpuc_admin, "pii_requires_federated", False)


def test_no_pii_grant_for_a_non_federated_organisation_when_required(cpuc_admin, pge, pge_admin, make_user):
    AccessPolicy.objects.update_or_create(pk=1, defaults={"pii_requires_federated": True})
    with pytest.raises(AccessDenied, match="does not federate"):
        service.grant(pge_admin, make_user("x@pge.com"), pge, perms.PII)


def test_the_last_steward_cannot_be_revoked(cpuc_admin, cpuc, make_user):
    g = service.active_stewards().get()
    with pytest.raises(AccessDenied, match="last data steward"):
        service.revoke(cpuc_admin, g, "testing")
    second = make_user("steward2@cpuc.ca.gov")
    service.grant(cpuc_admin, second, cpuc, perms.STEWARD)
    service.revoke(cpuc_admin, g, "handing over")
    assert list(service.active_stewards().values_list("membership__user__email", flat=True)) == ["steward2@cpuc.ca.gov"]
    with pytest.raises(AccessDenied, match="last data steward"):
        service.end_membership(cpuc_admin, second.memberships.get(), "leaving")


def test_steward_grants_have_no_expiry(cpuc_admin, cpuc, make_user):
    with pytest.raises(AccessDenied, match="no expiry"):
        service.grant(cpuc_admin, make_user("s@cpuc.ca.gov"), cpuc, perms.STEWARD, expires_at=timezone.now() + timedelta(days=9))


# --- access requests


def test_access_requests_go_to_the_organisations_admins(cpuc_admin, pge, pge_admin, make_user):
    u = make_user("community@example.com")
    req = service.request_access(u, pge, ["claims.read", "claims.submit"], "I file PG&E's claims")
    with pytest.raises(AccessDenied, match="already have a request"):
        service.request_access(u, pge, ["claims.read"])
    with pytest.raises(AccessDenied, match="administrators"):
        service.decide_request(cpuc_admin, req, True)
    service.decide_request(pge_admin, req, True, "welcome", permission_codes=["claims.read"])
    req.refresh_from_db()
    assert req.status == AccessRequest.Status.APPROVED
    assert service.effective_permissions(u, pge) == {"claims.read"}


def test_cpuc_only_permissions_cannot_be_requested(cpuc, make_user):
    with pytest.raises(AccessDenied, match="cannot be requested"):
        service.request_access(make_user("x@example.com"), cpuc, [perms.SYSTEM_ADMIN])


# --- recertification


def test_recertification_suspends_what_nobody_certified(pge, pge_admin, make_user):
    a, b = make_user("a@pge.com"), make_user("b@pge.com")
    ga = service.grant(pge_admin, a, pge, "claims.read")
    gb = service.grant(pge_admin, b, pge, "claims.read")
    campaign = service.open_recertification(pge_admin, pge, timezone.now() + timedelta(days=30))
    assert campaign.items.count() == 3  # the admin's own grant too
    service.certify(pge_admin, campaign.items.get(grant=ga))
    service.certify(pge_admin, campaign.items.get(grant__permission=perms.ORG_ADMIN))
    assert service.close_due_recertifications() == []  # not due yet
    closed = service.close_due_recertifications(now=timezone.now() + timedelta(days=31))
    assert closed == [campaign]
    ga.refresh_from_db()
    gb.refresh_from_db()
    assert (ga.status, gb.status) == ("active", "suspended")
    assert not service.has_permission(b, pge, "claims.read")
    service.reinstate(pge_admin, gb, "certified late")
    assert service.has_permission(b, pge, "claims.read")


def test_recertification_never_suspends_the_last_steward(cpuc_admin, cpuc):
    service.open_recertification(cpuc_admin, cpuc, timezone.now() - timedelta(seconds=1))
    service.close_due_recertifications()
    assert service.is_steward(cpuc_admin)
    assert not service.is_system_admin(cpuc_admin)  # suspended: nobody certified it
    assert AuditEvent.objects.filter(action="grant.suspension_skipped").count() == 1


def test_auto_suspend_can_be_turned_off(cpuc_admin, pge, pge_admin, make_user):
    service.set_policy(cpuc_admin, "recertification_auto_suspend", False, "trial year")
    g = service.grant(pge_admin, make_user("a@pge.com"), pge, "claims.read")
    service.open_recertification(pge_admin, pge, timezone.now() - timedelta(seconds=1))
    service.close_due_recertifications()
    g.refresh_from_db()
    assert g.status == "active"


# --- bootstrap


def test_bootstrap_command(db):
    call_command("bootstrap_cpuc", "first@cpuc.ca.gov", "--steward")
    from cedars.services.accounts.models import User

    u = User.objects.get(email="first@cpuc.ca.gov")
    assert service.is_system_admin(u) and service.is_steward(u) and service.is_org_admin(u, service.cpuc())
    assert AuditEvent.objects.filter(action="grant.created", actor__isnull=True).count() == 3
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("bootstrap_cpuc", "second@cpuc.ca.gov")

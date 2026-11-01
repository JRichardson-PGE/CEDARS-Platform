"""Role templates (BR-01.3): named bundles of module permissions a CPUC system administrator defines and an organization's
administrators grant at once, each grant recording the template, every step audited."""

import pytest

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.accounts.models import Grant, RoleTemplate
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def consultant(cpuc_admin):
    return service.create_role_template(
        cpuc_admin, name="Test consultant", kinds=["pa"], permissions=["calculations.submit", "calculations.read", "budgets.read"]
    )


def test_the_starter_templates_are_there():
    names = set(RoleTemplate.objects.values_list("name", flat=True))
    assert {"CPUC reviewer", "Evaluator", "Implementer", "Consultant"} <= names
    for t in RoleTemplate.objects.all():
        assert t.permissions and set(t.permissions) <= set(perms.module_permissions())


def test_a_template_bundles_module_permissions_only(cpuc_admin):
    for bad in (perms.ORG_ADMIN, perms.PII, perms.SYSTEM_ADMIN, "budgets.fly"):
        with pytest.raises(AccessDenied, match="module permissions only"):
            service.create_role_template(cpuc_admin, name=f"Bad {bad}", permissions=["budgets.read", bad])
    with pytest.raises(AccessDenied, match="at least one"):
        service.create_role_template(cpuc_admin, name="Empty", permissions=[])


def test_only_a_system_administrator_defines_templates(pge_admin, consultant):
    with pytest.raises(AccessDenied, match="system administrator"):
        service.create_role_template(pge_admin, name="Mine", permissions=["budgets.read"])
    with pytest.raises(AccessDenied, match="system administrator"):
        service.change_role_template(pge_admin, consultant, reason="no", active=False)


def test_permissions_are_kept_in_catalog_order_and_names_are_unique(cpuc_admin, consultant):
    assert consultant.permissions == ["calculations.read", "calculations.submit", "budgets.read"]
    with pytest.raises(AccessDenied, match="already a role template"):
        service.create_role_template(cpuc_admin, name="test CONSULTANT", permissions=["budgets.read"])


def test_applying_grants_each_permission_with_the_template(make_user, pge, pge_admin, consultant):
    alex = make_user("alex@consultants.example.org", "Alex")
    service.grant(pge_admin, alex, pge, "budgets.read", reason="earlier")
    result = service.apply_role_template(pge_admin, alex, pge, consultant, reason="2028 filing")
    assert [g.permission for g in result.granted] == ["calculations.read", "calculations.submit"]
    assert result.already == ["budgets.read"]
    assert all(g.role_template == consultant and g.granted_by == pge_admin for g in result.granted)
    assert service.effective_permissions(alex, pge) == {"budgets.read", "calculations.read", "calculations.submit"}
    event = AuditEvent.objects.get(action="role_template.applied")
    assert event.after["template"] == "Test consultant" and event.after["granted"] == ["calculations.read", "calculations.submit"]
    assert AuditEvent.objects.filter(action="grant.created", object_id__in=[str(g.pk) for g in result.granted]).count() == 2
    with pytest.raises(AccessDenied, match="already has every permission"):
        service.apply_role_template(pge_admin, alex, pge, consultant)


def test_nothing_is_granted_unless_the_actor_may_grant_it_all(make_user, cpuc_admin, pge, pge_admin, consultant):
    alex = make_user("alex@consultants.example.org")
    with pytest.raises(AccessDenied, match="administrators can grant"):
        service.apply_role_template(cpuc_admin, alex, pge, consultant)  # the CPUC does not grant a PA's module permissions
    with pytest.raises(AccessDenied, match="yourself"):
        service.apply_role_template(pge_admin, pge_admin, pge, consultant)
    assert not Grant.objects.filter(membership__user=alex).exists()


def test_a_template_is_offered_for_its_kinds_and_while_active(make_user, cpuc_admin, cpuc, pge, pge_admin, consultant):
    assert consultant in service.role_templates_for(pge)
    assert consultant not in service.role_templates_for(cpuc)
    reviewer = RoleTemplate.objects.get(name="CPUC reviewer")
    assert reviewer in service.role_templates_for(cpuc) and reviewer not in service.role_templates_for(pge)
    with pytest.raises(AccessDenied, match="not offered for program administrator"):
        service.apply_role_template(pge_admin, make_user("x@example.org"), pge, reviewer)
    service.change_role_template(cpuc_admin, consultant, reason="replaced", active=False)
    assert consultant not in service.role_templates_for(pge)
    with pytest.raises(AccessDenied, match="retired"):
        service.apply_role_template(pge_admin, make_user("y@example.org"), pge, consultant)
    assert AuditEvent.objects.filter(action="role_template.retired", object_id=str(consultant.pk)).exists()


def test_changing_a_template_leaves_its_grants(make_user, cpuc_admin, pge, pge_admin, consultant):
    alex = make_user("alex@consultants.example.org")
    service.apply_role_template(pge_admin, alex, pge, consultant)
    service.change_role_template(cpuc_admin, consultant, reason="narrower", permissions=["budgets.read"])
    consultant.refresh_from_db()
    assert consultant.permissions == ["budgets.read"]
    assert {"calculations.read", "calculations.submit"} <= service.effective_permissions(alex, pge)
    event = AuditEvent.objects.get(action="role_template.changed")
    assert event.before["permissions"] != event.after["permissions"] and event.reason == "narrower"
    with pytest.raises(AccessDenied, match="reason"):
        service.change_role_template(cpuc_admin, consultant, reason=" ", name="Other")


def test_the_pages(client, signed_in, make_user, cpuc_admin, pge, pge_admin, consultant):
    signed_in(cpuc_admin)
    page = client.get("/admin/role-templates/").content.decode()
    assert "Test consultant" in page and "CPUC reviewer" in page
    r = client.post(
        "/admin/role-templates/",
        {"name": "Auditor", "permissions": ["claims.read", "data.read"], "kinds": ["other"], "description": "", "reason": "new role"},
    )
    assert r.status_code == 302 and RoleTemplate.objects.get(name="Auditor").kinds == ["other"]
    t = RoleTemplate.objects.get(name="Auditor")
    assert client.get(f"/admin/role-templates/{t.pk}/").status_code == 200
    client.post(f"/admin/role-templates/{t.pk}/", {"action": "retire", "reason": "not needed"})
    t.refresh_from_db()
    assert not t.active

    signed_in(pge_admin)
    assert client.get("/admin/role-templates/").status_code == 404
    page = client.get(f"/admin/organizations/{pge.code}/").content.decode()
    assert "Grant a role template" in page and "Test consultant" in page and "CPUC reviewer" not in page.split("Grant a role template")[1]
    alex = make_user("alex@consultants.example.org", "Alex")
    r = client.post(
        f"/admin/organizations/{pge.code}/grant-template", {"email": alex.email, "template": str(consultant.pk), "reason": "2028"}
    )
    assert r.status_code == 302
    assert service.effective_permissions(alex, pge) == set(consultant.permissions)
    page = client.get(f"/admin/organizations/{pge.code}/").content.decode()
    assert "with the role template Test consultant" in page

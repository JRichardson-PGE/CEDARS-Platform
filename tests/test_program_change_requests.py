from datetime import timedelta

import pytest
from django.utils import timezone

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.notifications import service as notifications
from cedars.services.registry import service as registry
from cedars.services.registry.models import ProgramChangeRequest
from cedars.services.submissions import service as submissions

from .test_programs import programs  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db


@pytest.fixture
def approver(make_user, cpuc, cpuc_admin):
    u = make_user("approver@cpuc.ca.gov")
    access.grant(cpuc_admin, u, cpuc, "programs.approve")
    return u


def soon(days=30):
    return timezone.localdate() + timedelta(days=days)


def test_the_first_policy_locks_the_attributes_that_decide_filings_and_tests():
    assert registry.locked_fields() == [
        "segment",
        "statewide_program",
        "parent_program",
        "exclude_from_budget",
        "exclude_from_ce",
        "exclude_from_statewide",
    ]


def test_a_pa_admin_changes_unlocked_attributes_directly_and_requests_locked_ones(pge_admin, approver, cpuc_admin, programs):  # noqa: F811
    hvac = programs["hvac"]
    registry.change_program(pge_admin, hvac, effective=soon(10), reason="new implementer", implementer="Third-Party Solicited")
    with pytest.raises(AccessDenied, match="Segment is locked"):
        registry.change_program(pge_admin, hvac, effective=soon(), reason="r", segment="market_support")
    assert registry.needs_approval(cpuc_admin, {"segment": "x"}) == [] and registry.needs_approval(approver, {"segment": "x"}) == []

    req = registry.request_change(
        pge_admin, hvac, effective=soon(), reason="D.26-09-001 moves it", segment="market_support", name="Residential HVAC 2"
    )
    assert req.status == "pending" and req.organisation == hvac.administrator
    assert req.before == {"segment": "resource_acquisition", "name": "Residential HVAC"}
    assert hvac.versions.count() == 2  # nothing changes until it is decided
    with pytest.raises(AccessDenied, match="already waiting"):
        registry.request_change(pge_admin, hvac, effective=soon(), reason="again", segment="equity")
    with pytest.raises(AccessDenied, match="Nothing in this change needs approval"):
        registry.request_change(pge_admin, programs["old"], effective=soon(), reason="r", name="X")
    e = AuditEvent.objects.get(action="program_change.requested")
    assert e.after["changes"] == {"segment": "market_support", "name": "Residential HVAC 2"} and e.organisation == hvac.administrator

    with pytest.raises(AccessDenied, match="Only a CPUC programs approver"):
        registry.decide_change(pge_admin, req, True)
    with pytest.raises(AccessDenied, match="Say why"):
        registry.decide_change(approver, req, False)
    registry.decide_change(approver, req, True, "per the decision")
    req.refresh_from_db()
    new = registry.version_on(hvac, soon())
    assert req.status == "approved" and req.version == new and req.decided_by == approver
    assert (new.segment, new.name, new.implementer, new.created_by) == (
        "market_support",
        "Residential HVAC 2",
        "Third-Party Solicited",
        pge_admin,
    )
    changed = AuditEvent.objects.filter(action="program.changed", object_id=str(hvac.pk)).order_by("pk").last()
    assert changed.actor == approver and changed.reason == "D.26-09-001 moves it"
    with pytest.raises(AccessDenied, match="has been decided"):
        registry.decide_change(approver, req, False, "no")


def test_a_delegate_decides_and_requests_can_be_declined_or_withdrawn(pge_admin, approver, make_user, programs):  # noqa: F811
    hvac = programs["hvac"]
    delegate = make_user("delegate@cpuc.ca.gov")
    submissions.delegate(approver, module="programs", delegate_to=delegate, starts=timezone.localdate(), ends=soon(7))
    req = registry.request_change(pge_admin, hvac, effective=soon(), reason="r", exclude_from_ce=True)
    registry.decide_change(delegate, req, False, "needs a resolution first")
    req.refresh_from_db()
    assert req.status == "declined" and req.decided_as_delegate_of == approver and hvac.versions.count() == 1

    req = registry.request_change(pge_admin, hvac, effective=soon(), reason="r", exclude_from_ce=True)
    with pytest.raises(AccessDenied, match="requester or the program's administrators"):
        registry.withdraw_change(make_user("x@example.com"), req)
    registry.withdraw_change(pge_admin, req, "filed by mistake")
    assert ProgramChangeRequest.objects.get(pk=req.pk).status == "withdrawn"
    assert AuditEvent.objects.get(action="program_change.withdrawn").reason == "filed by mistake"


def test_an_approval_that_no_longer_fits_the_versions_is_refused(pge_admin, approver, cpuc_admin, programs):  # noqa: F811
    hvac = programs["hvac"]
    req = registry.request_change(pge_admin, hvac, effective=soon(30), reason="r", segment="equity")
    registry.change_program(cpuc_admin, hvac, effective=soon(60), reason="later change", name="Renamed")
    with pytest.raises(AccessDenied, match="must start after"):
        registry.decide_change(approver, req, True)
    assert ProgramChangeRequest.objects.get(pk=req.pk).status == "pending"  # still waiting: the approver declines it instead


def test_the_locked_attributes_are_a_dated_policy(cpuc_admin, pge_admin, programs):  # noqa: F811
    with pytest.raises(AccessDenied, match="CPUC system administrator"):
        registry.set_locked_fields(pge_admin, [], effective=timezone.localdate(), reason="r")
    with pytest.raises(AccessDenied, match="in the past"):
        registry.set_locked_fields(cpuc_admin, [], effective=timezone.localdate() - timedelta(days=1), reason="r")
    registry.set_locked_fields(cpuc_admin, ["status", "segment"], effective=soon(5), reason="status needs approval too")
    assert "status" not in registry.locked_fields() and registry.locked_fields(soon(5)) == ["status", "segment"]
    registry.set_locked_fields(cpuc_admin, [], effective=timezone.localdate(), reason="unlock for the migration")
    registry.change_program(pge_admin, programs["hvac"], effective=soon(), reason="r", segment="equity")  # nothing locked today
    with pytest.raises(AccessDenied, match="already starts"):
        registry.set_locked_fields(cpuc_admin, ["segment"], effective=timezone.localdate(), reason="r")
    e = AuditEvent.objects.filter(action="program_policy.changed").order_by("pk").last()
    assert e.after == {"effective_from": str(timezone.localdate()), "locked_fields": []}


def test_approvers_subscribed_to_requests_are_notified(pge_admin, approver, programs, django_capture_on_commit_callbacks):  # noqa: F811
    notifications.subscribe(approver, "program_change.requested", "site")
    with django_capture_on_commit_callbacks(execute=True):
        req = registry.request_change(pge_admin, programs["hvac"], effective=soon(), reason="r", segment="equity")
    n = approver.notifications.get()
    assert "locked program attribute" in n.title and n.link == f"/programs/requests/{req.pk}/"


def test_the_pages(signed_in, pge_admin, approver, cpuc_admin, make_user, programs):  # noqa: F811
    c = signed_in(pge_admin)
    page = c.get("/programs/PGE_Res_HVAC/change")
    assert "Locked: Segment, Statewide program it delivers" in page.content.decode()
    data = {k: v for k, v in page.context["form"].initial.items() if v not in (None, False)}
    data.update(effective=soon().isoformat(), reason="D.26-09-001", segment="market_support", sectors="Residential: 1")
    r = c.post("/programs/PGE_Res_HVAC/change", data)
    req = ProgramChangeRequest.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/programs/requests/{req.pk}/"
    assert "is waiting for a decision" in c.get("/programs/PGE_Res_HVAC/").content.decode()
    page = c.get(r["Location"]).content.decode()
    assert "Resource acquisition" in page and "Market support" in page and "Withdraw the request" in page and "Approve" not in page

    a = signed_in(approver)
    assert "PGE_Res_HVAC" in a.get("/programs/requests/").content.decode()
    assert "Approve" in a.get(f"/programs/requests/{req.pk}/").content.decode()
    assert "Say why" in a.post(f"/programs/requests/{req.pk}/", {"action": "decline", "note": ""}).content.decode()
    r = a.post(f"/programs/requests/{req.pk}/", {"action": "approve", "note": ""})
    assert r.status_code == 302 and registry.version_on(programs["hvac"], soon()).segment == "market_support"
    assert "No requests here" in a.get("/programs/requests/").content.decode()
    assert "Approved" in a.get("/programs/requests/?status=approved").content.decode()

    outsider = signed_in(make_user("someone@example.com"))
    assert outsider.get(f"/programs/requests/{req.pk}/").status_code == 404
    assert "No requests here" in outsider.get("/programs/requests/?status=all").content.decode()
    assert "Set a new policy" not in outsider.get("/programs/locked/").content.decode()
    assert outsider.post("/programs/locked/", {}).status_code == 404

    admin = signed_in(cpuc_admin)
    assert "Set a new policy" in admin.get("/programs/locked/").content.decode()
    r = admin.post("/programs/locked/", {"fields_locked": ["status"], "effective": timezone.localdate().isoformat(), "reason": "r"})
    assert r.status_code == 302 and registry.locked_fields() == ["status"]
    assert "Locked: Status" in signed_in(pge_admin).get("/programs/PGE_Res_HVAC/change").content.decode()

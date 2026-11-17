"""Work items and the backlog: who may do what, the story, requests joined as evidence, the one ordered backlog, and the CPUC's
approval of declines and public items."""

from datetime import date
from html import unescape

import pytest

from cedars.core import queue as my_queue
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent
from cedars.services.feedback import service as requests
from cedars.services.notifications.models import Notification
from cedars.services.work import service
from cedars.services.work.models import RequestLink, WorkItem
from tests.conftest import page_only

pytestmark = pytest.mark.django_db


def _text(html: str) -> str:
    return unescape(page_only(html))


def _staff(make_user, cpuc_admin, cpuc, email, name, *permissions):
    user = make_user(email, name)
    for p in permissions:
        access.grant(cpuc_admin, user, cpuc, p, reason="work items")
    return user


@pytest.fixture
def manager(make_user, cpuc_admin, cpuc):
    return _staff(make_user, cpuc_admin, cpuc, "manager@cpuc.example.org", "Mona Manager", perms.WORK_MANAGE)


@pytest.fixture
def approver(make_user, cpuc_admin, cpuc):
    return _staff(make_user, cpuc_admin, cpuc, "approver@cpuc.example.org", "Avery Approver", perms.WORK_APPROVE)


@pytest.fixture
def desk(make_user, cpuc_admin, cpuc):
    return _staff(make_user, cpuc_admin, cpuc, "desk@cpuc.example.org", "Dee Desk", perms.SUPPORT)


def _item(actor, title="The year filter cannot be reached by keyboard", **fields):
    return service.propose(actor, title=title, kind=fields.pop("kind", "problem"), **fields)


def test_the_work_permissions_are_the_cpucs_alone(make_user, cpuc_admin, pge, pge_admin):
    assert perms.WORK_APPROVE in perms.CPUC_ONLY and perms.WORK_MANAGE not in perms.CPUC_ONLY
    for p in (perms.WORK_MANAGE, perms.WORK_APPROVE):
        assert perms.label(p) and perms.describe(p)
        for actor in (cpuc_admin, pge_admin):
            with pytest.raises(AccessDenied):
                access.grant(actor, make_user(f"{p}.{actor.pk}@example.org", "X"), pge, p, reason="no")
    assert not service.can_see(pge_admin)
    assert service.can_manage(cpuc_admin) and service.can_approve(cpuc_admin)  # a system administrator holds both


def test_who_may_do_what(manager, approver, desk, pge_admin):
    assert service.can_manage(manager) and not service.can_approve(manager) and service.can_propose(manager)
    assert service.can_approve(approver) and not service.can_manage(approver) and not service.can_propose(approver)
    assert service.can_propose(desk) and not service.can_manage(desk) and not service.can_approve(desk)
    it = _item(desk)
    assert it.status == "proposed" and it.number == f"W-{it.pk}" and not it.public
    with pytest.raises(AccessDenied):
        _item(pge_admin)
    with pytest.raises(AccessDenied):
        _item(approver)
    for who in (desk, approver, pge_admin):
        with pytest.raises(AccessDenied):
            service.accept(who, it)
        with pytest.raises(AccessDenied):
            service.edit(who, it, title="Other")
    with pytest.raises(AccessDenied):
        service.decline(desk, it, "No")
    with pytest.raises(AccessDenied):
        service.make_public(desk, it)
    with pytest.raises(AccessDenied):
        service.approve(manager, it, "public")
    with pytest.raises(service.WorkRefused, match="title"):
        service.propose(manager, title=" ", kind="problem")
    with pytest.raises(service.WorkRefused, match="problem or an improvement"):
        service.propose(manager, title="T", kind="bug")


def test_the_story_is_written_and_each_change_audited(manager):
    it = _item(manager)
    service.edit(
        manager, it, who="A PA filer", pages="/budgets/\n/claims/", need="To pick the year by keyboard", size="S", severity="blocks"
    )
    it.refresh_from_db()
    assert it.page_list == ["/budgets/", "/claims/"] and it.get_size_display() == "Small" and it.get_severity_display() == "Blocks a filing"
    e = AuditEvent.objects.filter(action="work.changed").latest("pk")
    assert e.after["size"] == "S" and e.before["size"] == "" and "title" not in e.after
    before = AuditEvent.objects.count()
    service.edit(manager, it, size="S")  # nothing changed, nothing recorded
    assert AuditEvent.objects.count() == before
    with pytest.raises(service.WorkRefused, match="size"):
        service.edit(manager, it, size="XL")


def test_requests_are_joined_as_evidence(manager, desk, cpuc_admin, pge, pge_admin):
    first = requests.submit(page="/budgets/?year=2028", message="Cannot reach the filter", user=pge_admin, organisation=pge, kind="barrier")
    second = requests.submit(page="/budgets/", message="Same here", user=pge_admin, organisation=pge, kind="problem")
    secret = requests.submit(page="/account/", message="A hole", user=pge_admin, organisation=pge, kind="security")
    it = service.propose(desk, request=first, title="The year filter", kind="problem")
    assert service.item_of(first) == it and service.item_of(second) is None
    service.join(desk, it, second)
    with pytest.raises(service.WorkRefused, match="already joined"):
        service.join(manager, _item(manager, "Another"), second)
    ev = service.evidence(it)
    assert (ev["requests"], ev["organisations"], ev["urgent"]) == (2, 1, 1)
    # a security concern is joined by a system administrator only, and the item is then never public
    with pytest.raises(AccessDenied, match="security"):
        service.join(desk, it, secret)
    assert not service.is_sensitive(it)
    service.make_public(cpuc_admin, it)
    service.join(cpuc_admin, it, secret)
    it.refresh_from_db()
    assert service.is_sensitive(it) and not it.public
    with pytest.raises(service.WorkRefused, match="security concern"):
        service.make_public(cpuc_admin, it)
    # taken off, a request can be joined elsewhere; a deleted request leaves its evidence
    service.unjoin(desk, RequestLink.objects.get(feedback=second))
    other = _item(manager, "Another")
    service.join(manager, other, second)
    second.delete()
    link = other.links.get()
    assert link.feedback is None and link.kind == "problem" and link.page == "/budgets/" and link.organisation == pge
    assert AuditEvent.objects.filter(action="work.request_joined").count() == 4
    assert AuditEvent.objects.filter(action="work.request_unjoined").count() == 1


def test_the_backlog_is_one_list_in_one_order(manager, desk):
    a, b, c, d = (_item(manager, t) for t in "ABCD")
    for it in (a, b, c):
        service.accept(manager, it)
    order = lambda: [it.title for it in service.backlog()]  # noqa: E731
    assert order() == ["A", "B", "C"] and [it.position for it in service.backlog()] == [1, 2, 3]
    with pytest.raises(service.WorkRefused, match="proposed"):
        service.accept(manager, a)
    service.move(manager, c, "up")
    assert order() == ["A", "C", "B"]
    service.move(manager, a, "down")
    assert order() == ["C", "A", "B"]
    service.move(manager, b, 1)
    assert order() == ["B", "C", "A"]
    service.move(manager, b, 99)  # past the end is the end
    assert order() == ["C", "A", "B"]
    e = AuditEvent.objects.filter(action="work.moved").latest("pk")
    assert e.before == {"position": 1} and e.after == {"position": 3} and e.actor == manager
    moves = AuditEvent.objects.filter(action="work.moved").count()
    service.move(manager, c, "up")  # already first: nothing moves, nothing recorded
    assert order() == ["C", "A", "B"] and AuditEvent.objects.filter(action="work.moved").count() == moves
    with pytest.raises(service.WorkRefused, match="in the backlog"):
        service.move(manager, d, 1)
    with pytest.raises(service.WorkRefused, match="whole number"):
        service.move(manager, c, "first")
    with pytest.raises(AccessDenied):
        service.move(desk, c, "down")
    # a declined item leaves the backlog and the rest close up
    service.decline(_approver_of(manager), a, "Out of scope")
    assert order() == ["C", "B"] and [it.position for it in service.backlog()] == [1, 2]


def _approver_of(manager):
    """A person holding both permissions, made from the backlog manager."""
    cpuc = access.cpuc()
    admin = access.holders(cpuc, perms.SYSTEM_ADMIN)[0]
    access.grant(admin, manager, cpuc, perms.WORK_APPROVE, reason="both")
    return manager


def test_declining_takes_the_cpucs_approval(manager, approver, django_capture_on_commit_callbacks):
    it = _item(manager)
    service.accept(manager, it)
    with pytest.raises(service.WorkRefused, match="reason"):
        service.decline(manager, it, " ")
    with django_capture_on_commit_callbacks(execute=True):
        it = service.decline(manager, it, "Out of scope")
    assert it.status == "accepted" and it.awaits_approval and it.decline_proposed == "Out of scope"
    assert Notification.objects.filter(user=approver, title__startswith="Approve declining").count() == 1
    assert [i.what for i in service.to_approve(approver)] == [f"{it.number}: {it.title}"] and service.to_approve(manager) == []
    # not approved: it stays where it was and the manager is told why
    with pytest.raises(service.WorkRefused, match="reason"):
        service.turn_down(approver, it, "decline", "")
    with django_capture_on_commit_callbacks(execute=True):
        it = service.turn_down(approver, it, "decline", "Three organizations asked for it")
    assert it.status == "accepted" and not it.awaits_approval and it.proposed_at is None
    assert Notification.objects.filter(user=manager, title__startswith="Not approved: declining").count() == 1
    with pytest.raises(service.WorkRefused, match="Nothing of that kind"):
        service.approve(approver, it, "decline")
    # proposed again and approved
    it = service.decline(manager, it, "Replaced by another item")
    it = service.approve(approver, it, "decline")
    assert (
        it.status == "declined" and it.declined_reason == "Replaced by another item" and it.position is None and it.decided_by == approver
    )
    e = AuditEvent.objects.filter(action="work.declined").latest("pk")
    assert e.actor == approver and e.after["proposed_by"] == manager.pk and e.reason == "Replaced by another item"
    with pytest.raises(service.WorkRefused, match="already declined"):
        service.decline(approver, it, "Again")
    # an approver's own decline needs no second person; a declined item can be reconsidered
    other = _item(manager, "Other")
    assert service.decline(approver, other, "Not needed").status == "declined"
    with pytest.raises(AccessDenied):
        service.reconsider(manager, other, "Please")
    assert service.reconsider(approver, other, "New evidence").status == "proposed"


def test_making_an_item_public_takes_the_cpucs_approval(manager, approver):
    it = _item(manager)
    it = service.make_public(manager, it)
    assert not it.public and it.public_proposed_by == manager and it.awaits_approval
    with pytest.raises(service.WorkRefused, match="already waiting"):
        service.make_public(manager, it)
    it = service.approve(approver, it, "public")
    assert it.public and not it.awaits_approval
    assert AuditEvent.objects.filter(action="work.made_public").latest("pk").after == {"public": True, "proposed_by": manager.pk}
    with pytest.raises(service.WorkRefused, match="already public"):
        service.make_public(approver, it)
    with pytest.raises(AccessDenied):
        service.make_internal(manager, it, "Oops")
    assert not service.make_internal(approver, it, "Names a PA").public
    assert service.make_public(approver, it).public  # an approver's own takes effect at once
    declined = service.decline(approver, it, "No")
    assert not declined.public  # a declined item is not public
    with pytest.raises(service.WorkRefused, match="declined"):
        service.make_public(approver, declined)


def test_my_queue_lists_proposed_items_and_approvals(manager, approver, desk):
    it = _item(desk)
    assert [i.url for i in service.to_triage(manager)] == [f"/admin/work/{it.pk}/"]
    assert service.to_triage(desk) == [] and service.to_triage(approver) == []
    service.make_public(manager, it)
    assert [i.note for i in service.to_approve(approver)] == ["Making it public was proposed."]
    kinds = {s.kind.key for s in my_queue.sections(approver) if s.items}
    assert "work_approval" in kinds and "work_proposed" not in kinds


def test_the_pages(client, signed_in, manager, approver, desk, pge, pge_admin):
    fb = requests.submit(page="/budgets/?year=2028", message="Cannot reach the filter", user=pge_admin, organisation=pge, kind="barrier")
    assert client.get("/admin/work/").status_code == 302  # to sign in
    signed_in(pge_admin)
    for url in ("/admin/work/", "/admin/work/new/", f"/admin/work/from-request/{fb.pk}/"):
        assert client.get(url).status_code == 404

    # the support desk proposes one from a request
    signed_in(desk)
    assert 'href="/admin/work/"' in client.get("/admin/").content.decode()
    page = _text(client.get(f"/admin/feedback/{fb.pk}/").content.decode())
    assert f"/admin/work/from-request/{fb.pk}/" in page
    page = _text(client.get(f"/admin/work/from-request/{fb.pk}/").content.decode())
    assert "Cannot reach the filter" in page and "/budgets/</textarea>" in page  # the page, without its query
    assert "Join it to a work item" not in page  # there is none yet
    r = client.post(
        f"/admin/work/from-request/{fb.pk}/",
        {"do": "new", "title": "The year filter cannot be reached by keyboard", "kind": "problem", "pages": "/budgets/"},
    )
    it = WorkItem.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/admin/work/{it.pk}/" and service.item_of(fb) == it
    page = _text(client.get(f"/admin/feedback/{fb.pk}/").content.decode())
    assert f"{it.number}: The year filter" in page and "Make or join" not in page
    assert "is joined to" in _text(client.get(f"/admin/work/from-request/{fb.pk}/").content.decode())
    page = _text(client.get(f"/admin/work/{it.pk}/").content.decode())
    assert "1 request from 1 organization, 1 of them urgent" in page and "PGE" in page and "Take off" in page
    assert "accept-h" not in page and "decline-h" not in page and "Change the story" not in page
    assert client.get(f"/admin/work/{it.pk}/edit/").status_code == 404
    client.post(f"/admin/work/{it.pk}/act", {"do": "accept"})
    it.refresh_from_db()
    assert it.status == "proposed"

    # the backlog manager writes it up, accepts and orders
    signed_in(manager)
    assert "The year filter" in _text(client.get("/admin/work/?show=proposed").content.decode())
    r = client.post(
        f"/admin/work/{it.pk}/edit/",
        {"title": it.title, "kind": "problem", "who": "A PA filer", "pages": "/budgets/", "need": "To choose the year", "size": "S"},
    )
    assert r.status_code == 302
    client.post(f"/admin/work/{it.pk}/act", {"do": "accept"})
    r = client.post("/admin/work/new/", {"title": "Show the cap on Budgets", "kind": "improvement"})
    other = WorkItem.objects.exclude(pk=it.pk).get()
    assert r["Location"] == f"/admin/work/{other.pk}/"
    client.post(f"/admin/work/{other.pk}/act", {"do": "accept"})
    page = _text(client.get("/admin/work/").content.decode())
    assert page.index("The year filter") < page.index("Show the cap") and f'aria-label="Move {other.number} up"' in page
    assert f'aria-label="Move {it.number} up"' not in page and f'aria-label="Move {other.number} down"' not in page
    r = client.post(f"/admin/work/{other.pk}/act", {"do": "move", "to": "up", "back": "backlog"})
    assert r["Location"] == "/admin/work/"
    page = _text(client.get("/admin/work/").content.decode())
    assert page.index("Show the cap") < page.index("The year filter")
    client.post(f"/admin/work/{it.pk}/act", {"do": "move", "to": "1"})
    assert [w.pk for w in service.backlog()] == [it.pk, other.pk]
    # a decline from the manager is a proposal
    page = _text(client.get(f"/admin/work/{other.pk}/").content.decode())
    assert "Propose declining it" in page and "Propose making it public" in page
    r = client.post(f"/admin/work/{other.pk}/act", {"do": "decline", "reason": ""})
    assert r.status_code == 200 and "This field is required" in _text(r.content.decode())
    client.post(f"/admin/work/{other.pk}/act", {"do": "decline", "reason": "Out of scope"})
    client.post(f"/admin/work/{it.pk}/act", {"do": "public"})
    page = _text(client.get(f"/admin/work/{other.pk}/").content.decode())
    assert "Waiting for the CPUC's approval" in page and "Approve: decline it" not in page
    assert "Waiting for approval (2)" in _text(client.get("/admin/work/").content.decode())

    # the approver decides
    signed_in(approver)
    page = _text(client.get("/admin/work/?show=approval").content.decode())
    assert "Show the cap" in page and "The year filter" in page
    assert client.get("/admin/work/new/").status_code == 404
    page = _text(client.get(f"/admin/work/{other.pk}/").content.decode())
    assert "Approve: decline it" in page and "Out of scope" in page and "Mona Manager" in page
    client.post(f"/admin/work/{other.pk}/act", {"do": "approve", "what": "decline"})
    client.post(
        f"/admin/work/{it.pk}/act", {"do": "turn_down", "what": "public", "reason": "The title names the page only; fine, but wait"}
    )
    other.refresh_from_db()
    it.refresh_from_db()
    assert other.status == "declined" and not it.public and not it.awaits_approval
    page = _text(client.get(f"/admin/work/{other.pk}/").content.decode())
    assert "Not planned" in page and "reconsider-h" in page
    assert "Show the cap" in _text(client.get("/admin/work/?show=declined").content.decode())
    assert client.post(f"/admin/work/{it.pk}/act", {"do": "nonsense"}).status_code == 404


def test_the_developers_organization_manages_the_backlog_and_the_cpuc_approves(
    client, signed_in, make_user, cpuc_admin, cpuc, pge, pge_admin
):
    """The backlog is managed day to day by the developer's employee: a member of an organization the CPUC marks, who is not CPUC
    staff and so cannot approve what is done in the CPUC's name."""
    builder = access.create_organisation(cpuc_admin, code="BUILDER", name="The developer", kind="other")
    owner = make_user("owner@builder.example.org", "Dev Owner")
    lead = make_user("lead@builder.example.org", "Bea Backlog")
    access.grant(cpuc_admin, owner, builder, perms.ORG_ADMIN, reason="first administrator")
    with pytest.raises(AccessDenied, match="does not manage the backlog"):
        access.grant(owner, lead, builder, perms.WORK_MANAGE, reason="backlog")
    with pytest.raises(AccessDenied, match="neither a program administrator nor the CPUC"):
        access.change_organisation(cpuc_admin, pge, reason="no", manages_backlog=True)
    with pytest.raises(AccessDenied):
        access.change_organisation(owner, builder, reason="mine", manages_backlog=True)
    access.change_organisation(cpuc_admin, builder, reason="The developer manages the backlog", manages_backlog=True)
    builder.refresh_from_db()
    access.grant(owner, lead, builder, perms.WORK_MANAGE, reason="backlog")
    with pytest.raises(AccessDenied):
        access.grant(owner, lead, builder, perms.WORK_APPROVE, reason="no")

    assert service.can_manage(lead) and not service.can_approve(lead) and not requests.can_answer(lead)
    assert not service.can_manage(owner) and not service.can_see(owner)
    fb = requests.submit(page="/claims/", message="The words of a request", user=pge_admin, organisation=pge, kind="problem")
    it = service.propose(cpuc_admin, request=fb, title="Claims upload stalls", kind="problem")
    service.accept(lead, it)
    it = service.decline(lead, it, "Cannot reproduce")
    assert it.status == "accepted" and it.decline_proposed_by == lead  # a proposal only
    assert [i.url for i in service.to_approve(cpuc_admin)] == [f"/admin/work/{it.pk}/"]

    signed_in(lead)
    assert 'href="/admin/work/"' in client.get("/admin/").content.decode()
    page = _text(client.get(f"/admin/work/{it.pk}/").content.decode())
    assert "1 request from 1 organization" in page and "PGE" in page and "/claims/" in page
    assert f"/admin/feedback/{fb.pk}/" not in page and "The words of a request" not in page  # the evidence, never the request's words
    assert client.get(f"/admin/feedback/{fb.pk}/").status_code == 404
    assert "Approve: decline it" not in page
    assert client.post(f"/admin/work/{it.pk}/act", {"do": "approve", "what": "decline"}).status_code == 302
    it.refresh_from_db()
    assert it.status == "accepted"
    e = AuditEvent.objects.filter(action="work.decline_proposed").latest("pk")
    assert e.actor == lead
    # unmarked again, the permission no longer counts
    access.change_organisation(cpuc_admin, builder, reason="Contract ended", manages_backlog=False)
    assert not service.can_manage(lead) and client.get("/admin/work/").status_code == 404


# ---- acceptance tests, readiness and release plans


def _ready_item(manager, title="Ready", **more):
    it = service.propose(manager, title=title, kind="problem", who="A PA filer", pages="/budgets/", need="To file", size="S", **more)
    service.add_test(manager, it, title="It works", steps="Open Budgets\nChoose 2028", expected="The 2028 budget shows")
    service.accept(manager, it)
    it.refresh_from_db()
    return it


def test_an_item_is_ready_when_it_has_a_story_pages_a_size_and_a_test(manager, desk):
    it = _item(manager)
    assert service.lacking(it) == ["a story (who needs it and what they need)", "the pages it touches", "a size", "an acceptance test"]
    service.edit(manager, it, who="A PA filer", need="To choose the year", pages="/budgets/", size="M")
    it.refresh_from_db()
    assert service.lacking(it) == ["an acceptance test"]
    with pytest.raises(AccessDenied):
        service.add_test(desk, it, title="T", steps="S", expected="E")
    with pytest.raises(service.WorkRefused, match="needs a title"):
        service.add_test(manager, it, title="T", steps=" ", expected="E")
    test = service.add_test(manager, it, title="By keyboard", steps="Tab to Year\nPress Down", expected="2028", library_id="PAF-023")
    assert test.step_list == ["Tab to Year", "Press Down"] and service.lacking(it) == []
    assert AuditEvent.objects.filter(action="work.test_added").latest("pk").after["library_id"] == "PAF-023"
    service.remove_test(manager, test)
    assert service.lacking(it) == ["an acceptance test"] and AuditEvent.objects.filter(action="work.test_removed").count() == 1


def test_a_release_plan_is_filled_from_the_backlog_and_approved_by_the_cpuc(manager, approver, desk, django_capture_on_commit_callbacks):
    a, b = _ready_item(manager, "A"), _ready_item(manager, "B")
    unready = _item(manager, "Not ready")
    service.accept(manager, unready)
    with pytest.raises(AccessDenied):
        service.create_plan(approver, name="1.5")
    with pytest.raises(service.WorkRefused, match="name"):
        service.create_plan(manager, name=" ")
    plan = service.create_plan(manager, name="1.5", note="Fixes")
    with pytest.raises(service.WorkRefused, match="already a release plan"):
        service.create_plan(manager, name="1.5")
    with pytest.raises(service.WorkRefused, match="at least one"):
        service.propose_plan(manager, plan)
    with pytest.raises(service.WorkRefused, match="not ready.*an acceptance test"):
        service.plan_item(manager, unready, plan)
    with pytest.raises(AccessDenied):
        service.plan_item(desk, a, plan)
    a = service.plan_item(manager, a, plan)
    assert a.status == "planned" and a.release == plan and a.position is None
    assert [it.title for it in service.backlog()] == ["B", "Not ready"] and [it.position for it in service.backlog()] == [1, 2]
    with pytest.raises(service.WorkRefused, match="in the backlog"):
        service.plan_item(manager, a, plan)
    # a planned item is not declined, and keeps a test
    with pytest.raises(service.WorkRefused, match="Take it out of the release"):
        service.decline(approver, a, "No")
    with pytest.raises(service.WorkRefused, match="at least one acceptance test"):
        service.remove_test(manager, a.tests.get())

    with django_capture_on_commit_callbacks(execute=True):
        plan = service.propose_plan(manager, plan)
    assert plan.status == "proposed" and plan.proposed_by == manager
    assert Notification.objects.filter(user=approver, title="Approve the plan for release 1.5").count() == 1
    assert [i.what for i in service.to_approve(approver)] == ["The plan for release 1.5"]
    with pytest.raises(AccessDenied):
        service.approve_plan(manager, plan)
    with pytest.raises(service.WorkRefused, match="reason"):
        service.send_plan_back(approver, plan, "")
    with django_capture_on_commit_callbacks(execute=True):
        plan = service.send_plan_back(approver, plan, "B first")
    assert plan.status == "draft" and plan.sent_back_reason == "B first"
    assert Notification.objects.filter(user=manager, title="Not approved: the plan for release 1.5").count() == 1
    service.plan_item(manager, b, plan)
    plan = service.approve_plan(approver, service.propose_plan(manager, plan))
    assert plan.status == "approved" and plan.approved_by == approver and plan.sent_back_reason == ""
    e = AuditEvent.objects.filter(action="work.plan_approved").latest("pk")
    assert e.after["items"] == sorted([a.pk, b.pk]) and e.after["proposed_by"] == manager.pk and e.actor == approver
    with pytest.raises(service.WorkRefused, match="not waiting"):
        service.approve_plan(approver, plan)

    # after approval a manager's change to its work items needs approval again; the date is the approver's to change
    with pytest.raises(service.WorkRefused, match="approver's to change"):
        service.change_plan(manager, plan, name="1.5", expected_on=date(2028, 12, 3), note="Fixes")
    assert service.change_plan(manager, plan, name="1.5", note="Fixes and more").status == "approved"
    assert service.change_plan(approver, plan, name="1.5", expected_on=date(2028, 12, 3), note="Fixes and more").expected_on == date(
        2028, 12, 3
    )
    b = service.unplan_item(manager, b)
    plan.refresh_from_db()
    assert plan.status == "proposed" and plan.approved_by is None
    assert b.status == "accepted" and b.position == 1 and [it.title for it in service.backlog()] == ["B", "Not ready"]
    service.approve_plan(approver, plan)


def test_a_person_with_both_permissions_approves_in_the_same_step(cpuc_admin):
    it = _ready_item(cpuc_admin)
    plan = service.create_plan(cpuc_admin, name="2.0")
    service.plan_item(cpuc_admin, it, plan)
    plan = service.propose_plan(cpuc_admin, plan)
    assert plan.status == "approved" and plan.approved_by == cpuc_admin
    service.unplan_item(cpuc_admin, it)  # their own change does not ask for approval again
    plan.refresh_from_db()
    assert plan.status == "approved"


def test_the_release_pages(client, signed_in, manager, approver, desk, pge_admin):
    a = _ready_item(manager, "The year filter")
    unready = _item(manager, "Not ready yet")
    service.accept(manager, unready)
    signed_in(pge_admin)
    assert client.get("/admin/work/releases/").status_code == 404

    signed_in(manager)
    assert "/admin/work/releases/" in _text(client.get("/admin/work/").content.decode())
    r = client.post("/admin/work/releases/", {"name": "1.5", "expected_on": "2028-12-03", "note": "Fixes"})
    plan = service.ReleasePlan.objects.get()
    assert r["Location"] == f"/admin/work/releases/{plan.pk}/"
    r = client.post("/admin/work/releases/", {"name": "1.5"})
    assert r.status_code == 200 and "already a release plan named 1.5" in _text(r.content.decode())
    page = _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    assert f"1. {a.number}: The year filter" in page  # offered, in the backlog's order
    assert "Not ready yet</a> still needs a story" in page and "Propose the plan" in page and "Approve the plan" not in page
    # an item that is not ready is not among the choices
    r = client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "add", "item": unready.pk})
    assert r.status_code == 200 and "Select a valid choice" in _text(r.content.decode())
    client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "add", "item": a.pk})
    a.refresh_from_db()
    assert a.status == "planned"
    page = _text(client.get(f"/admin/work/{a.pk}/").content.decode())
    assert "Planned for a release" in page and "Release 1.5" in page and "decline-h" not in page
    assert "Planned for release 1.5" in _text(client.get("/admin/work/?show=planned").content.decode())
    # acceptance tests on the item's page
    page = _text(client.get(f"/admin/work/{unready.pk}/").content.decode())
    assert "Not yet. It still needs a story" in page and "Add an acceptance test" in page
    r = client.post(f"/admin/work/{unready.pk}/tests", {"title": "T", "steps": "", "expected": "E"})
    assert r.status_code == 200 and "This field is required" in _text(r.content.decode())
    client.post(f"/admin/work/{unready.pk}/tests", {"title": "It shows", "steps": "Open it", "expected": "It is there"})
    test = unready.tests.get()
    assert "It is there" in _text(client.get(f"/admin/work/{unready.pk}/").content.decode())
    client.post(f"/admin/work/{unready.pk}/tests", {"remove": test.pk})
    assert not unready.tests.exists()
    client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "propose"})
    plan.refresh_from_db()
    assert plan.status == "proposed"
    assert client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "approve"}).status_code == 302
    plan.refresh_from_db()
    assert plan.status == "proposed"  # not the manager's to approve

    signed_in(desk)  # the support desk reads the plans and changes nothing
    page = _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    assert "The year filter" in page and "Take out" not in page and "details-h" not in page
    client.post(f"/admin/work/{unready.pk}/tests", {"title": "T", "steps": "S", "expected": "E"})
    assert not unready.tests.exists()

    signed_in(approver)
    page = _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    assert "Approve the plan" in page and "Mona Manager" in page and "Plan a work item" not in page
    r = client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "send_back", "reason": ""})
    assert r.status_code == 200 and "This field is required" in _text(r.content.decode())
    client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "send_back", "reason": "Add the claims fix"})
    assert "Add the claims fix" in _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    signed_in(manager)
    client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "propose"})
    signed_in(approver)
    client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "approve"})
    plan.refresh_from_db()
    assert plan.status == "approved"
    client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "details", "name": "1.5.1", "expected_on": "2028-12-10", "note": "Fixes"})
    plan.refresh_from_db()
    assert plan.name == "1.5.1" and plan.expected_on == date(2028, 12, 10)
    assert "Approved" in _text(client.get("/admin/work/releases/").content.decode())
    assert client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "nonsense"}).status_code == 404
    signed_in(manager)
    client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "remove", "item": a.pk})
    plan.refresh_from_db()
    a.refresh_from_db()
    assert plan.status == "proposed" and a.status == "accepted" and a.position == 1


# ---- what people outside see


def test_known_problems_and_upcoming_changes_list_only_what_is_public(client, manager, approver):
    assert "No problems are listed" in _text(client.get("/known-problems/").content.decode())
    assert "Nothing is planned for a release" in _text(client.get("/upcoming-changes/").content.decode())
    cosmetic = _ready_item(manager, "A label is cut off", severity="cosmetic")
    blocks = _ready_item(manager, "The year filter cannot be reached", severity="blocks", workaround="Add ?year=2028 to the address.")
    hidden = _ready_item(manager, "An internal matter")
    idea = _item(manager, "Show the cap on Budgets", kind="improvement")
    for it in (cosmetic, blocks, idea):
        service.make_public(approver, it)
    assert [it.title for it in service.known_problems()] == [blocks.title, cosmetic.title]  # the most severe first
    page = _text(client.get("/known-problems/").content.decode())  # anyone, signed in or not
    assert "The year filter cannot be reached" in page and "Add ?year=2028" in page and "Blocks a filing" in page
    assert "To be done, not yet scheduled" in page and "An internal matter" not in page and "Show the cap" not in page
    assert client.get(f"/work-items/{hidden.pk}/").status_code == 404
    page = _text(client.get(f"/work-items/{blocks.pk}/").content.decode())
    assert "The CPUC means to do it and has not chosen a release" in page and "Sign in" in page and "Open the work item" not in page
    assert "Being considered" in _text(client.get(f"/work-items/{idea.pk}/").content.decode())

    # a release shows once the CPUC has approved its plan, and then only its public items
    plan = service.create_plan(manager, name="1.5", expected_on=date(2028, 12, 3), note="Fixes found in testing.")
    service.plan_item(manager, blocks, plan)
    service.plan_item(manager, hidden, plan)
    plan = service.propose_plan(manager, plan)
    assert plan.status == "proposed"
    page = _text(client.get("/upcoming-changes/").content.decode())
    assert "Release 1.5, expected" not in page and "Show the cap on Budgets</a>" in page and "(being considered)" in page
    assert service.progress(blocks)["key"] == "listed"  # a proposed plan promises nothing
    service.approve_plan(approver, plan)
    page = _text(client.get("/upcoming-changes/").content.decode())
    assert "Release 1.5, expected 2028-12-03" in page and "The year filter cannot be reached</a>" in page and "(a fix)" in page
    assert "An internal matter" not in page and "A label is cut off" not in page
    assert "Planned for release 1.5, expected 2028-12-03" in _text(client.get("/known-problems/").content.decode())
    # a declined item leaves public view
    service.decline(approver, cosmetic, "It is as designed.")
    assert "A label is cut off" not in _text(client.get("/known-problems/").content.decode())
    assert client.get(f"/work-items/{cosmetic.pk}/").status_code == 404


def test_a_signed_in_person_says_a_public_item_affects_them(client, signed_in, manager, approver, pge, pge_admin):
    it = _ready_item(manager, "The year filter cannot be reached")
    with pytest.raises(service.WorkRefused):
        service.follow(pge_admin, it, pge)  # internal
    service.make_public(approver, it)
    r = client.post(f"/work-items/{it.pk}/follow")
    assert r.status_code == 302 and "login" in r["Location"]
    signed_in(pge_admin)
    page = _text(client.get(f"/work-items/{it.pk}/").content.decode())
    assert "This affects me too" in page and "said this affects them" not in page
    client.post(f"/work-items/{it.pk}/follow")
    client.post(f"/work-items/{it.pk}/follow")  # once
    assert it.follows.count() == 1 and service.follows(pge_admin, it)
    assert AuditEvent.objects.filter(action="work.followed").count() == 1
    page = _text(client.get(f"/work-items/{it.pk}/").content.decode())
    assert "1 person has said this affects them" in page and "Stop following it" in page
    ev = service.evidence(it)
    assert ev["followers"] == 1 and ev["organisations"] == 1 and ev["requests"] == 0
    signed_in(manager)
    page = _text(client.get(f"/admin/work/{it.pk}/").content.decode())
    assert "1 person has said on its public page that it affects them too" in page and f'href="/work-items/{it.pk}/"' in page
    assert "Open the work item" in _text(client.get(f"/work-items/{it.pk}/").content.decode())
    signed_in(pge_admin)
    client.post(f"/work-items/{it.pk}/follow", {"do": "unfollow"})
    assert not service.follows(pge_admin, it) and AuditEvent.objects.filter(action="work.unfollowed").count() == 1


def test_senders_and_followers_are_told_and_a_request_shows_where_its_work_stands(
    client, signed_in, make_user, manager, approver, desk, pge, pge_admin, django_capture_on_commit_callbacks
):
    follower = make_user("follower@example.org", "Fay Follower")
    fb = requests.submit(page="/budgets/?year=2028", message="Cannot reach the filter", user=pge_admin, organisation=pge, kind="barrier")
    signed_in(pge_admin)
    assert "What is being done" not in _text(client.get(f"/feedback/{fb.pk}/").content.decode())
    it = service.propose(desk, request=fb, title="The year filter cannot be reached", kind="problem", workaround="Add ?year=2028.")
    page = _text(client.get(f"/feedback/{fb.pk}/").content.decode())
    assert "What is being done" in page and "Being considered" in page
    assert "The year filter cannot be reached" not in page and "Add ?year=2028." not in page  # an internal item's words stay internal
    service.edit(manager, it, who="A filer", pages="/budgets/", need="Reach it", why="To file", size="S")
    service.add_test(manager, it, title="By keyboard", steps="Tab to Year", expected="2028")
    service.accept(manager, it)
    it = service.make_public(approver, it)
    service.follow(follower, it)
    service.follow(pge_admin, it, pge)  # the sender follows too, and is told once
    page = _text(client.get(f"/feedback/{fb.pk}/").content.decode())
    assert (
        "To be done, not yet scheduled" in page and f'href="/work-items/{it.pk}/"' in page and "Meanwhile:</strong> Add ?year=2028." in page
    )
    # the form for reporting a problem on that page offers it first
    page = _text(client.get("/feedback/?from=/budgets/%3Fyear%3D2027").content.decode())
    assert "Known problem on that page" in page and "The year filter cannot be reached" in page
    assert "on that page" not in _text(client.get("/feedback/?from=/claims/").content.decode())

    plan = service.create_plan(manager, name="1.5", expected_on=date(2028, 12, 3))
    service.plan_item(manager, it, plan)
    with django_capture_on_commit_callbacks(execute=True):
        service.approve_plan(approver, service.propose_plan(manager, plan))
    told = Notification.objects.filter(title__contains="lanned for release 1.5, expected 2028-12-03")
    assert {n.user for n in told} == {pge_admin, follower} and told.count() == 2
    assert told.get(user=pge_admin).link == f"/feedback/{fb.pk}/" and told.get(user=follower).link == f"/work-items/{it.pk}/"
    assert told.get(user=pge_admin).title.startswith("Your request of ") and "The year filter" in told.get(user=follower).title
    assert "Planned for release 1.5, expected 2028-12-03" in _text(client.get(f"/feedback/{fb.pk}/").content.decode())

    service.unplan_item(manager, it)
    with django_capture_on_commit_callbacks(execute=True):
        service.decline(approver, it, "The page is being replaced.")
    told = Notification.objects.filter(title__contains="decided not to do this")
    assert {n.user for n in told} == {pge_admin, follower}
    assert told.get(user=follower).link == "/known-problems/" and "The page is being replaced." in told.get(user=follower).body
    page = _text(client.get(f"/feedback/{fb.pk}/").content.decode())
    assert "decided not to do it" in page and "Reason:</strong> The page is being replaced." in page


# ---- the issue for the developers, and the decision on a built release


def test_a_ready_item_starts_an_issue_filled_with_its_story_and_tests(client, signed_in, settings, manager, desk, pge, pge_admin):
    settings.CEDARS_ISSUE_REPOSITORY = ""
    it = _ready_item(manager, "The year filter cannot be reached", why="The budget can be reviewed", workaround="Add ?year=2028.")
    assert service.issue_link(it) == "" and "No issue tracker" in service.issue_blocked(it)
    settings.CEDARS_ISSUE_REPOSITORY = "example-org/cedars"
    fb = requests.submit(page="/budgets/", message="Words that stay in CEDARS", user=pge_admin, organisation=pge, kind="problem")
    service.join(manager, it, fb)
    title, body = service.issue_text(it)
    assert title == "The year filter cannot be reached" and f"Work item {it.number}: a problem, size small" in body
    assert "As A PA filer, I need: To file" in body and "- `/budgets/`" in body and "1. It works" in body
    assert "   - Open Budgets" in body and "   - Expected: The 2028 budget shows" in body and "Add ?year=2028." in body
    assert "Words that stay in CEDARS" not in body and "PGE" not in body  # never its requests
    link = service.issue_link(it)
    assert link.startswith("https://github.com/example-org/cedars/issues/new?title=The+year+filter") and "body=Work+item" in link
    unready = _item(manager, "Not ready")
    assert service.issue_link(unready) == "" and "still needs" in service.issue_blocked(unready)
    secret = requests.submit(page="/account/", message="A hole", user=pge_admin, organisation=pge, kind="security")
    hole = _ready_item(manager, "Close it")
    with pytest.raises(AccessDenied):
        service.join(manager, hole, secret)

    with pytest.raises(AccessDenied):
        service.record_issue(desk, it, "https://github.com/example-org/cedars/issues/7")
    with pytest.raises(service.WorkRefused, match="https"):
        service.record_issue(manager, it, "github.com/x")
    signed_in(manager)
    page = _text(client.get(f"/admin/work/{it.pk}/").content.decode())
    assert "Start the issue" in page and "https://github.com/example-org/cedars/issues/new?" in page
    assert "An issue cannot be started" in _text(client.get(f"/admin/work/{unready.pk}/").content.decode())
    client.post(f"/admin/work/{it.pk}/act", {"do": "issue", "issue_url": "https://github.com/example-org/cedars/issues/7"})
    it.refresh_from_db()
    assert it.issue_url == "https://github.com/example-org/cedars/issues/7"
    e = AuditEvent.objects.filter(action="work.issue_recorded").latest("pk")
    assert e.after == {"issue": it.issue_url} and e.actor == manager
    assert 'id="issue-link"' in client.get(f"/admin/work/{it.pk}/").content.decode()
    signed_in(desk)
    assert "The issue for the developers" not in _text(client.get(f"/admin/work/{it.pk}/").content.decode())


def test_the_cpuc_decides_whether_a_built_release_goes(
    client, signed_in, make_user, manager, approver, pge, pge_admin, django_capture_on_commit_callbacks
):
    follower = make_user("follower@example.org", "Fay Follower")
    fb = requests.submit(page="/budgets/", message="Cannot reach the filter", user=pge_admin, organisation=pge, kind="problem")
    a, b = _ready_item(manager, "The year filter cannot be reached"), _ready_item(manager, "B")
    service.join(manager, a, fb)
    a = service.make_public(approver, a)
    service.follow(follower, a)
    plan = service.create_plan(manager, name="1.5", expected_on=date(2028, 12, 3))
    for it in (a, b):
        service.plan_item(manager, it, plan)
    with pytest.raises(service.WorkRefused, match="approved release plan"):
        service.mark_built(manager, a)  # the plan is not approved yet
    plan = service.approve_plan(approver, service.propose_plan(manager, plan))
    with pytest.raises(AccessDenied):
        service.mark_built(approver, a)
    with pytest.raises(AccessDenied):
        service.release_goes(manager, plan)
    service.mark_built(manager, a)
    with pytest.raises(service.WorkRefused, match=f"Not every work item is built: {b.number}"):
        service.release_goes(approver, plan)
    assert service.to_decide() == [] and service.to_approve(approver) == []
    with django_capture_on_commit_callbacks(execute=True):
        service.mark_built(manager, b)
    assert Notification.objects.filter(user=approver, title="Release 1.5 is built: decide whether it goes").count() == 1
    assert [i.what for i in service.to_approve(approver)] == ["Release 1.5: go or no-go"]

    # no-go holds it with a reason; built again, it is the CPUC's to decide afresh
    with pytest.raises(service.WorkRefused, match="reason"):
        service.release_held(approver, plan, " ")
    with django_capture_on_commit_callbacks(execute=True):
        plan = service.release_held(approver, plan, "The year filter still fails with a screen reader.")
    assert plan.status == "approved" and plan.held_by == approver and service.to_approve(approver) == []
    assert Notification.objects.filter(user=manager, title="Release 1.5 does not go yet").count() == 1
    signed_in(manager)
    page = _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    assert "Does not go yet" in page and "still fails with a screen reader" in page and "Go: the release is out" not in page
    client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "not_built", "item": a.pk})
    assert "Still to build: " + a.number in _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "built", "item": a.pk})
    plan.refresh_from_db()
    assert plan.held_reason == "" and len(service.to_decide()) == 1

    signed_in(approver)
    page = _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    assert "Go: the release is out" in page and "No-go: it does not go yet" in page
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "go", "released_on": "2028-12-05"})
    assert r.status_code == 302
    plan.refresh_from_db()
    a.refresh_from_db()
    assert plan.status == "released" and plan.released_on == date(2028, 12, 5) and plan.released_by == approver
    assert a.status == "done" and service.to_decide() == []
    e = AuditEvent.objects.filter(action="work.released").latest("pk")
    assert e.after["items"] == [a.pk, b.pk] and e.after["released_on"] == "2028-12-05"
    told = Notification.objects.filter(title__contains="eleased in release 1.5")
    assert {n.user for n in told} == {pge_admin, follower}
    assert told.get(user=pge_admin).link == f"/feedback/{fb.pk}/" and told.get(user=follower).link == f"/work-items/{a.pk}/"
    assert Notification.objects.filter(user=manager, title="Release 1.5 goes").count() == 1

    # a released plan and its done items no longer change; they leave the public lists but keep their page
    for refused in (
        lambda: service.plan_item(manager, _ready_item(manager, "C"), plan),
        lambda: service.change_plan(approver, plan, name="1.5.1"),
        lambda: service.decline(approver, a, "No"),
        lambda: service.unplan_item(manager, a),
        lambda: service.release_goes(approver, plan),
        lambda: service.follow(pge_admin, a, pge),
    ):
        with pytest.raises(service.WorkRefused):
            refused()
    assert service.known_problems() == [] and service.upcoming()["releases"] == []
    assert "Released in release 1.5 on 2028-12-05" in _text(client.get(f"/work-items/{a.pk}/").content.decode())
    page = _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    assert "Released" in page and "Plan a work item for it" not in page and "Change the name" not in page
    assert "It went out in" in _text(client.get(f"/admin/work/{a.pk}/").content.decode())
    assert "Done in release 1.5" in _text(client.get("/admin/work/?show=done").content.decode())
    signed_in(pge_admin)
    page = _text(client.get(f"/feedback/{fb.pk}/").content.decode())
    assert "is done" in page and "Released in release 1.5 on 2028-12-05" in page


# ---- acceptance tests joined to the test scripts, and a release's round of testing


def _approved_release(manager, approver, name="2.0"):
    """An approved plan of two items: one whose two tests name PUB-001 and (in small letters) PAF-001, one whose test names an ID
    the scripts do not hold. Both carry the plain test `_ready_item` writes, which names none."""
    first, second = _ready_item(manager, "Front page"), _ready_item(manager, "Queue")
    service.add_test(manager, first, title="The front page opens", steps="Open the site", expected="It shows", library_id="PUB-001")
    service.add_test(manager, first, title="The queue opens", steps="Open My queue", expected="It shows", library_id="paf-001")
    service.add_test(manager, second, title="Gone", steps="Open it", expected="It shows", library_id="XYZ-999")
    plan = service.create_plan(manager, name=name)
    for it in (first, second):
        service.plan_item(manager, it, plan)
    service.propose_plan(manager, plan)
    return service.approve_plan(approver, plan), first, second


def test_an_acceptance_test_is_joined_to_the_test_scripts_by_its_id(client, signed_in, manager, approver, settings):
    from cedars.services.testing import service as testing
    from tests.test_testing import SCRIPTS

    plan, first, second = _approved_release(manager, approver)
    assert service.script_tests(list(first.tests.all())) == {}  # no scripts are loaded yet
    assert "No test scripts are loaded" in service.round_blocked(plan)
    testing.load_scripts(None, SCRIPTS, "2026-11-12")
    joined = service.script_tests(list(first.tests.all()) + list(second.tests.all()))
    assert sorted(case.test_id for case in joined.values()) == ["PAF-001", "PUB-001"]
    t = service.testing_of(plan)
    assert (t["total"], t["in_scripts"], t["by_hand"], t["round"]) == (5, 2, 3, None)
    assert [s.code for s in t["scripts"]] == ["PUB", "PAF"]
    signed_in(manager)
    page = _text(client.get(f"/admin/work/{first.pk}/").content.decode())
    assert "In the test scripts: PUB-001, The front page opens (script PUB)" in page and "In the test scripts: PAF-001" in page
    page = _text(client.get(f"/admin/work/{second.pk}/").content.decode())
    assert "XYZ-999 is not a test of the test scripts as last loaded" in page
    page = _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    assert "No round of testing has been opened for this release. 2 of its 5 acceptance tests are in the test scripts" in page
    assert "XYZ-999 is not in the test scripts" in page and "Open a round of testing" not in page  # a manager is not a test lead
    settings.CEDARS_TESTING = False
    assert service.testing_of(plan) is None and service.script_tests(list(first.tests.all())) == {}
    for url in (f"/admin/work/{first.pk}/", f"/admin/work/releases/{plan.pk}/"):
        page = _text(client.get(url).content.decode())
        assert "Acceptance testing" not in page and "In the test scripts" not in page


def test_a_test_lead_opens_a_round_for_an_approved_release_and_the_plan_shows_its_results(
    client, signed_in, manager, approver, cpuc_admin, make_user, django_capture_on_commit_callbacks
):
    from cedars.services.testing import service as testing
    from tests.test_testing import SCRIPTS

    version = testing.load_scripts(None, SCRIPTS, "2026-11-12")
    draft = service.create_plan(manager, name="2.1")
    assert "once the CPUC has approved" in service.round_blocked(draft)
    plain = service.create_plan(manager, name="2.2")
    service.plan_item(manager, _ready_item(manager, "Plain"), plain)
    plain = service.approve_plan(approver, service.propose_plan(manager, plain))
    assert "None of its acceptance tests names a test" in service.round_blocked(plain)
    plan, first, second = _approved_release(manager, approver)
    assert service.round_blocked(plan) == ""
    for who in (manager, approver):
        with pytest.raises(AccessDenied):
            service.open_round(who, plan, name="R", starts_on=date(2026, 11, 16))
    signed_in(cpuc_admin)
    page = _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    assert "Open a round of testing for this release" in page and "Its note names the scripts to assign: PUB, PAF." in page
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(
            f"/admin/work/releases/{plan.pk}/act", {"do": "round", "name": "Release 2.0 acceptance test", "starts_on": "2026-11-16"}
        )
    plan.refresh_from_db()
    round_ = plan.campaign
    assert r.status_code == 302 and r.url == f"/admin/testing/campaigns/{round_.pk}/"
    assert round_.version == version and round_.name == "Release 2.0 acceptance test"
    assert "Scripts to assign: PUB, PAF." in round_.note and "PAF-001, PUB-001" in round_.note
    event = AuditEvent.objects.get(action="work.round_opened")
    assert event.after == {"round": round_.name, "scripts": "2026-11-12", "tests": ["PAF-001", "PUB-001"]}
    assert Notification.objects.filter(user=manager, title="Acceptance testing of release 2.0 has started").count() == 1
    assert "is still open" in service.round_blocked(plan)
    r = client.post(f"/admin/work/releases/{plan.pk}/act", {"do": "round", "name": "Again", "starts_on": "2026-11-17"}, follow=True)
    assert "is still open. Close it before opening another." in _text(r.content.decode())

    # two testers answer PUB-001 (one passes, one fails); nobody answers PAF-001
    pub = version.scripts.get(code="PUB")
    one, two = make_user("one@example.org", "Tester One"), make_user("two@example.org", "Tester Two")
    case = pub.tests.get(test_id="PUB-001")
    testing.record(testing.assign(cpuc_admin, round_, pub, one), case, outcome="passed")
    t = service.testing_of(plan)
    assert (t["passed"], t["failing"], t["no_result"], t["by_hand"]) == (1, 0, 1, 3)
    testing.record(testing.assign(cpuc_admin, round_, pub, two), case, outcome="failed", comment="A blank page.")
    t = service.testing_of(plan)
    assert (t["passed"], t["failing"], t["no_result"]) == (0, 1, 1)
    row = next(row for row in t["rows"] if row["case"] == case)
    assert (row["state"], row["failing"], row["answers"]) == ("failing", 1, 2)
    for it in (first, second):
        service.mark_built(manager, it)
    page = _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    assert "Of the 2 acceptance tests in the test scripts: 0 passed, 1 failed or blocked, 1 with no result yet." in page
    assert "3 are not in the test scripts and are checked by hand." in page and "1 of 2 results" in page
    assert "Acceptance testing: 1 acceptance test failed or is blocked" in page and "The decision is the CPUC's either way." in page
    assert f"/admin/testing/campaigns/{round_.pk}/results/" in page
    signed_in(manager)  # reads the results on the plan, without the links to the test lead's pages
    page = client.get(f"/admin/work/releases/{plan.pk}/").content.decode()
    assert "1 failed or blocked" in _text(page) and "/admin/testing/" not in page_only(page)

    # a failed test does not stop the CPUC's go; a closed round lets another be opened while the plan is approved
    testing.close_campaign(cpuc_admin, round_)
    plan.refresh_from_db()
    assert service.round_blocked(plan) == ""
    again = service.open_round(cpuc_admin, plan, name="Release 2.0 retest", starts_on=date(2026, 11, 20))
    plan.refresh_from_db()
    assert plan.campaign == again and service.testing_of(plan)["no_result"] == 2
    testing.close_campaign(cpuc_admin, again)
    plan.refresh_from_db()
    with pytest.raises(service.WorkRefused, match="already a round of testing named"):
        service.open_round(cpuc_admin, plan, name="Release 2.0 retest", starts_on=date(2026, 11, 21))
    service.release_goes(approver, plan)
    plan.refresh_from_db()
    assert plan.status == "released" and "once the CPUC has approved" in service.round_blocked(plan)


# ---- signed events from the developers' tracker


def _event(client, event, payload, secret="a-secret-for-tests", repository="example-org/cedars"):
    import hashlib
    import hmac
    import json

    body = json.dumps({"repository": {"full_name": repository}, **payload}).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/hooks/issue-tracker/",
        body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": event},
    )


def _pull(number, words, *, state="open", merged=False, at="2028-10-01T10:00:00Z", title="A change"):
    return {
        "action": "edited",
        "pull_request": {
            "number": number,
            "html_url": f"https://github.com/example-org/cedars/pull/{number}",
            "title": title,
            "body": words,
            "state": state,
            "merged": merged,
            "updated_at": at,
        },
    }


def _issue(number, action, *, at, reason="completed"):
    return {"action": action, "issue": {"number": number, "state_reason": reason if action == "closed" else None, "updated_at": at}}


def test_only_a_signed_event_for_the_sites_repository_is_taken(client, manager, settings):
    it = _ready_item(manager)
    words = _pull(7, f"Fixes {it.number}.")
    assert _event(client, "pull_request", words, secret="another").status_code == 403
    assert client.post("/hooks/issue-tracker/", b"{}", content_type="application/json").status_code == 403
    assert client.get("/hooks/issue-tracker/").status_code == 405
    r = _event(client, "pull_request", words, repository="someone-else/cedars")
    assert r.status_code == 202 and not it.pulls.exists()
    bad = client.post(
        "/hooks/issue-tracker/",
        b"[1]",
        content_type="application/json",
        headers={"X-Hub-Signature-256": "sha256=" + __import__("hmac").new(b"a-secret-for-tests", b"[1]", "sha256").hexdigest()},
    )
    assert bad.status_code == 400
    assert _event(client, "ping", {}).json() == {"done": "Ready."}
    assert _event(client, "star", {}).json() == {"done": "Nothing to do."}
    settings.CEDARS_ISSUE_WEBHOOK_SECRET = ""
    assert _event(client, "pull_request", words).status_code == 404  # no secret: the address is not found
    assert not it.pulls.exists()


def test_a_pull_request_that_names_a_work_item_is_shown_on_it(client, signed_in, manager):
    it, other = _ready_item(manager, "One"), _ready_item(manager, "Two")
    service.record_issue(manager, other, "https://github.com/example-org/cedars/issues/34")
    r = _event(client, "pull_request", _pull(7, f"Does w-{it.pk} and closes #34. Not W-{it.pk}9, XW-{other.pk} or page#34."))
    assert r.json() == {"done": f"Pull request #7 open: {it.number}, {other.number}."}
    assert _event(client, "pull_request", _pull(8, "Tidying.")).json() == {"done": "It names no work item."}
    pull = it.pulls.get()
    assert (pull.number, pull.state, pull.url) == (7, "open", "https://github.com/example-org/cedars/pull/7")
    event = AuditEvent.objects.filter(action="work.pull_request", object_id=str(it.pk)).get()
    assert event.actor is None and event.actor_label == "the developers' tracker" and event.after == {"number": 7, "state": "open"}
    signed_in(manager)
    page = _text(client.get(f"/admin/work/{it.pk}/").content.decode())
    assert "In progress: a pull request is open." in page and "Pull request #7</a>: open" in page
    # merged; then the same delivery again and an older one change nothing
    merged = _pull(7, f"Does W-{it.pk}.", state="closed", merged=True, at="2028-10-02T10:00:00Z")
    assert _event(client, "pull_request", merged).json() == {"done": f"Pull request #7 merged: {it.number}."}
    _event(client, "pull_request", merged)
    assert _event(client, "pull_request", _pull(7, f"W-{it.pk}")).json() == {"done": "It names no work item."}  # older
    assert it.pulls.get().state == "merged" and other.pulls.get().state == "open"  # no longer named: kept as last seen
    assert AuditEvent.objects.filter(action="work.pull_request", object_id=str(it.pk)).count() == 2
    page = _text(client.get(f"/admin/work/{it.pk}/").content.decode())
    assert "Merged. " in page and "Pull request #7</a>: merged" in page
    closed = _pull(9, "x", title=f"Try {it.number}", state="closed", at="2028-10-03T10:00:00Z")
    assert _event(client, "pull_request", closed).json() == {"done": f"Pull request #9 closed: {it.number}."}
    assert service.development(it)["words"] == "Merged"


def test_a_closed_issue_marks_an_item_of_an_approved_plan_built(client, signed_in, manager, approver, django_capture_on_commit_callbacks):
    first, second = _ready_item(manager, "One"), _ready_item(manager, "Two")
    service.record_issue(manager, first, "https://github.com/example-org/cedars/issues/34")
    service.record_issue(manager, second, "https://github.com/example-org/cedars/issues/35/")
    plan = service.create_plan(manager, name="3.0")
    for it in (first, second):
        service.plan_item(manager, it, plan)
    service.propose_plan(manager, plan)
    # closed before the plan is approved: the state is kept, and the item is built when the plan is approved
    r = _event(client, "issues", _issue(34, "closed", at="2028-10-01T10:00:00Z"))
    first.refresh_from_db()
    assert r.json() == {"done": f"Issue closed: {first.number}."} and first.issue_state == "closed" and first.built_at is None
    assert _event(client, "issues", _issue(99, "closed", at="2028-10-01T10:00:00Z")).json() == {"done": "No work item has that issue."}
    service.approve_plan(approver, plan)
    first.refresh_from_db()
    assert first.built_at and first.built_by is None
    event = AuditEvent.objects.filter(action="work.built").latest("pk")
    assert event.actor_label == "the developers' tracker" and event.after["by"] == "its issue in the developers' tracker"
    # closed as not planned changes nothing; the last issue closing as done tells the approvers
    _event(client, "issues", _issue(35, "closed", at="2028-10-02T10:00:00Z", reason="not_planned"))
    second.refresh_from_db()
    assert second.issue_state == "" and second.built_at is None
    with django_capture_on_commit_callbacks(execute=True):
        _event(client, "issues", _issue(35, "closed", at="2028-10-03T10:00:00Z"))
    second.refresh_from_db()
    assert second.built_at and Notification.objects.filter(user=approver, title="Release 3.0 is built: decide whether it goes").exists()
    signed_in(manager)
    page = _text(client.get(f"/admin/work/releases/{plan.pk}/").content.decode())
    assert page.count("(its issue closed)") == 2
    page = _text(client.get(f"/admin/work/{second.pk}/").content.decode())
    assert "(closed in the developers' tracker)" in page and "when its issue closed in the developers' tracker" in page
    # reopened: the tracker's mark is taken back, an older event is not, and a manager's own mark stays
    _event(client, "issues", _issue(35, "reopened", at="2028-10-02T12:00:00Z"))  # older than the close
    second.refresh_from_db()
    assert second.built_at and second.issue_state == "closed"
    _event(client, "issues", _issue(35, "reopened", at="2028-10-04T10:00:00Z"))
    second.refresh_from_db()
    assert second.built_at is None and second.issue_state == "open"
    service.mark_built(manager, second)
    _event(client, "issues", _issue(35, "closed", at="2028-10-05T10:00:00Z"))
    _event(client, "issues", _issue(35, "reopened", at="2028-10-06T10:00:00Z"))
    second.refresh_from_db()
    assert second.built_at and second.built_by == manager and second.issue_state == "open"
    # another issue's address forgets what the tracker said about the first
    service.record_issue(manager, second, "https://github.com/example-org/cedars/issues/40")
    second.refresh_from_db()
    assert second.issue_state == "" and second.issue_changed_at is None


# ---- the site posting the issue itself


def test_the_site_posts_a_read_issue_and_keeps_its_address(client, signed_in, manager, desk, monkeypatch, settings):
    from cedars.services.work import tracker_client

    it = _ready_item(manager)
    posted = []

    def fake(title, body):
        posted.append((title, body))
        return "https://github.com/example-org/cedars/issues/51", 51

    monkeypatch.setattr(tracker_client, "post_issue", fake)
    signed_in(manager)
    page = _text(client.get(f"/admin/work/{it.pk}/").content.decode())
    assert "Or have the site post the issue" in page and "## Acceptance tests" in page
    r = client.post(f"/admin/work/{it.pk}/act", {"do": "post_issue"})  # the box is not ticked
    assert "Read the issue and tick the box before posting it." in _text(r.content.decode()) and not posted
    with pytest.raises(AccessDenied):
        service.post_issue(desk, it)
    r = client.post(f"/admin/work/{it.pk}/act", {"do": "post_issue", "read": "on"}, follow=True)
    assert "The issue is posted to the developers' tracker" in _text(r.content.decode())
    it.refresh_from_db()
    assert (it.issue_url, it.issue_state) == ("https://github.com/example-org/cedars/issues/51", "open")
    assert posted == [service.issue_text(it)] and "Or have the site post the issue" not in page_only(r.content.decode())
    event = AuditEvent.objects.get(action="work.issue_posted")
    assert event.actor == manager and event.after == {"issue": it.issue_url, "number": 51}
    with pytest.raises(service.WorkRefused, match="already has an issue"):
        service.post_issue(manager, it)

    # a call the tracker refuses keeps nothing; an item that is not ready, or declined, is never sent
    def refused(title, body):
        raise tracker_client.TrackerError("The developers' tracker refused the call (status 403).")

    monkeypatch.setattr(tracker_client, "post_issue", refused)
    other = _ready_item(manager, "Other")
    r = client.post(f"/admin/work/{other.pk}/act", {"do": "post_issue", "read": "on"}, follow=True)
    assert "refused the call (status 403). Nothing was kept." in _text(r.content.decode())
    other.refresh_from_db()
    assert other.issue_url == "" and AuditEvent.objects.filter(action="work.issue_posted").count() == 1
    monkeypatch.setattr(tracker_client, "post_issue", fake)
    with pytest.raises(service.WorkRefused, match="still needs"):
        service.post_issue(manager, _item(manager, "Bare"))
    settings.CEDARS_ISSUE_APP_PRIVATE_KEY = ""
    assert not service.can_post_issue()
    assert "Or have the site post the issue" not in _text(client.get(f"/admin/work/{other.pk}/").content.decode())


def test_the_call_to_the_tracker_signs_in_as_the_app_and_posts_one_issue(settings, monkeypatch):
    import io
    import json
    import urllib.error

    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from cedars.services.work import tracker_client

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    text = vars(serialization.Encoding)["PEM"]  # the key as text, as the App's settings page gives it
    pem = key.private_bytes(text, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    settings.CEDARS_ISSUE_APP_ID, settings.CEDARS_ISSUE_APP_INSTALLATION_ID = "77", "88"
    settings.CEDARS_ISSUE_APP_PRIVATE_KEY = pem.replace("\n", "\\n")  # as an environment variable holds it
    assert tracker_client.can_post()
    claims = jwt.decode(tracker_client.app_token(now=1_000_000), key.public_key(), algorithms=["RS256"], options={"verify_exp": False})
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "77"}
    calls = []
    answers = [{"token": "an-installation-token"}, {"html_url": "https://github.com/example-org/cedars/issues/9", "number": 9}]

    def urlopen(request, timeout):
        calls.append((request.full_url, request.get_method(), request.get_header("Authorization"), json.loads(request.data), timeout))
        return io.BytesIO(json.dumps(answers[len(calls) - 1]).encode())

    monkeypatch.setattr(tracker_client.urllib.request, "urlopen", urlopen)
    assert tracker_client.post_issue("A title", "A body") == ("https://github.com/example-org/cedars/issues/9", 9)
    (first, second) = calls
    assert first[0] == "https://api.github.com/app/installations/88/access_tokens" and first[1] == "POST" and first[4] == 10
    assert first[3] == {"repositories": ["cedars"], "permissions": {"issues": "write"}}
    jwt.decode(first[2].removeprefix("Bearer "), key.public_key(), algorithms=["RS256"])  # signed by the App's key, not expired
    assert second[:4] == (
        "https://api.github.com/repos/example-org/cedars/issues",
        "POST",
        "Bearer an-installation-token",
        {"title": "A title", "body": "A body"},
    )

    # what can go wrong says so in words, and never shows the key or a token
    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(tracker_client.urllib.request, "urlopen", refuse)
    with pytest.raises(tracker_client.TrackerError, match=r"refused the call \(status 403\)"):
        tracker_client.post_issue("T", "B")
    monkeypatch.setattr(tracker_client.urllib.request, "urlopen", lambda request, timeout: io.BytesIO(b'{"number": 9}'))
    with pytest.raises(tracker_client.TrackerError, match="gave the site no access"):
        tracker_client.post_issue("T", "B")
    settings.CEDARS_ISSUE_APP_PRIVATE_KEY = "not a key"
    with pytest.raises(tracker_client.TrackerError, match="cannot be read") as e:
        tracker_client.post_issue("T", "B")
    assert "not a key" not in str(e.value)
    for name, value in (("CEDARS_ISSUE_REPOSITORY", "example-org/cedars/../x"), ("CEDARS_ISSUE_APP_ID", "seven")):
        setattr(settings, name, value)
        assert not tracker_client.can_post()


# ---- measures


def test_lengths_of_time_are_written_in_words():
    from cedars.services.work import measures

    assert [measures.span(s) for s in (None, 20, 90, 3600, 30 * 3600, 3 * 86400)] == [
        "No data",
        "1 minute",
        "2 minutes",
        "1 hour",
        "30 hours",
        "3 days",
    ]


def test_the_measures_are_counted_from_the_records(client, signed_in, manager, approver, desk, cpuc_admin, pge_admin, make_user):
    from datetime import timedelta

    from django.utils import timezone

    from cedars.services.feedback.models import Feedback, Reply
    from cedars.services.testing import service as testing
    from cedars.services.work import measures
    from tests.test_testing import SCRIPTS

    now = timezone.now()

    def sent(kind, page, ago, **more):
        fb = requests.submit(user=pge_admin, page=page, message="Words nobody should see here", kind=kind, limited=False, **more)
        Feedback.objects.filter(pk=fb.pk).update(created_at=now - ago)
        return fb

    def replied(fb, kind, ago):
        reply = Reply.objects.create(feedback=fb, kind=kind, author=cpuc_admin, body="An answer")
        Reply.objects.filter(pk=reply.pk).update(created_at=now - ago)

    one = sent("help", "/budgets/?year=2028", timedelta(days=10))
    replied(one, "question", timedelta(days=10) - timedelta(hours=2))  # first reply after 2 hours
    replied(one, "answer", timedelta(days=6))  # answered after 4 days
    two = sent("problem", "/budgets/", timedelta(days=5))
    replied(two, "note", timedelta(days=5) - timedelta(minutes=5))  # a staff note is not a reply to the sender
    replied(two, "answer", timedelta(days=5) - timedelta(hours=4))  # reply and answer after 4 hours
    Feedback.objects.filter(pk__in=[one.pk, two.pk]).update(status="answered")
    sent("idea", "/claims/", timedelta(days=3))  # waits on the CPUC
    sent("help", "/claims/", timedelta(days=200))  # outside 90 days, inside the year
    sent("finding", "/queue/", timedelta(days=1))
    r = measures.requests(90)
    assert (r["sent"], r["findings"], r["replied"], r["answered"]) == (3, 1, 2, 2)
    assert r["kinds"] == [("A question", 1), ("A problem", 1), ("An idea", 1)]
    assert (r["first_reply"], r["answer"]) == ("3 hours", "2 days")  # medians of 2 h and 4 h; of 4 days and 4 h (50 hours)
    assert r["waiting"] == 2 and r["oldest_waiting"] == "200 days"
    assert r["pages"] == [("/budgets/", 2), ("/claims/", 1)]
    assert measures.requests(365)["sent"] == 4 and measures.requests(30)["sent"] == 3

    # delivery: two small items and one medium released; accepted 20, 10 and 40 days before the release's date
    assert measures.escaped() is None and measures.delivery(90)["by_size"] == []
    from unittest import mock

    made = {}
    for title, ago in (("Small", 20), ("Other", 10), ("Medium", 40)):
        with mock.patch("django.utils.timezone.now", return_value=now - timedelta(days=ago)):  # accepted that long ago
            made[title] = _ready_item(manager, title)
    small, other, medium = made["Small"], made["Other"], made["Medium"]
    service.edit(manager, medium, size="M", pages="/claims/")
    plan = service.create_plan(manager, name="5.0")
    for it in (small, other, medium):
        service.plan_item(manager, it, plan)
    service.approve_plan(approver, service.propose_plan(manager, plan))
    for it in (small, other, medium):
        service.mark_built(manager, it)
    released = timezone.localdate() - timedelta(days=4)
    service.release_goes(approver, plan, released)
    waiting = _ready_item(manager, "Waiting")
    d = measures.delivery(90)
    assert (d["released"], d["releases"]) == (3, 1)
    assert d["by_size"] == [
        {"size": "Small", "items": 2, "median": 11, "longest": 16},
        {"size": "Medium", "items": 1, "median": 36, "longest": 36},
    ]
    assert dict(d["now"])["In the backlog"] == 1 and d["backlog_sizes"] == [("Small", 1)] and waiting.status == "accepted"
    # after the release: the idea on /claims/ is not a problem, the problem on /budgets/ came before it; one comes after
    e = measures.escaped()
    assert e["plan"].pk == plan.pk and e["pages"] == [("/budgets/", 0), ("/claims/", 0)] and e["total"] == 0
    sent("problem", "/budgets/2028/programs/?page=2", timedelta(days=1))
    sent("barrier", "/budgetsummary/", timedelta(days=1))  # another page that only starts with the same letters
    e = measures.escaped()
    assert e["pages"] == [("/budgets/", 1), ("/claims/", 0)] and e["total"] == 1

    # testing: one round with a pass and a failure, and the tests no round has answered
    version = testing.load_scripts(None, SCRIPTS, "2026-11-12")
    round_ = testing.create_campaign(cpuc_admin, name="Round one", version=version, starts_on=timezone.localdate())
    pub = version.scripts.get(code="PUB")
    run = testing.assign(cpuc_admin, round_, pub, make_user("tess@example.org", "Tess Ter"))
    testing.record(run, pub.tests.get(test_id="PUB-001"), outcome="passed")
    t = measures.rounds()
    row = t["rounds"][0]
    assert (row["total"], row["passed"], row["failed"], row["left"], row["release"]) == (2, 1, 0, 1, None)
    assert t["never_total"] == 2 and [(n["script"].code, n["never"], n["tests"]) for n in t["never"]] == [("PUB", 1, 2), ("PAF", 1, 1)]

    # the page: staff only, counts only
    signed_in(pge_admin)
    assert client.get("/admin/work/measures/").status_code == 404
    for who in (manager, approver, desk):
        signed_in(who)
        assert client.get("/admin/work/measures/").status_code == 200
    html = client.get("/admin/work/measures/?days=nonsense").content.decode()
    page = _text(html)
    assert "Sent in the last 90 days" in page and "Words nobody should see here" not in page and "pge" not in page_only(html).lower()
    assert "Release 5.0</a> went out" in page and "1 problem or accessibility barrier was reported" in page
    assert "2 tests of the scripts as last loaded (version 2026-11-12) have no result in any round." in page
    assert "/admin/testing/" not in page_only(html)  # the desk is not a test lead
    assert "Sent in the last year" in _text(client.get("/admin/work/measures/?days=365").content.decode())
    assert 'id="measures-link"' in client.get("/admin/work/").content.decode()

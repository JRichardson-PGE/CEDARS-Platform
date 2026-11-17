"""Work items and the backlog. A work item is one problem to fix or one improvement to make that the CPUC has decided to
consider; requests (the Feedback service) are joined to the item that would settle them, and are its evidence.

An item starts Proposed: the support desk proposes one from a request, or a backlog manager writes one. A backlog manager
(work.manage, held in the CPUC organization or in the one the CPUC marks as managing the backlog: the developer's) writes its
story (who, on which pages, what they need, why), its size, severity and workaround, accepts it into the backlog and orders the
backlog, which is one list in one order. Declining an item and making one public are the CPUC's to approve (work.approve),
because the people who asked are told in the CPUC's name and a public item is read by anyone: a manager proposes, an approver
confirms or turns the proposal down, and a person holding both permissions does each in one step. System administrators hold
both. An item joined to a security concern is never made public. Every step is an audit event, and every reorder records who
moved what from where to where.

What people outside see: the public items (Known problems, Upcoming changes, each item's own page), in plain words; a signed-in
person may say a public item affects them too (a Follow). The senders of the requests joined to an item, and its followers, are
told when the CPUC approves a release plan that holds it, when it is declined and when its release goes out.

A ready item's page links to a new issue in the developers' tracker, filled with its story and acceptance tests (never its
requests), and keeps the issue's address. A backlog manager marks each item of an approved plan built; when all are, the CPUC
decides whether the release goes. Go makes the plan Released and its items Done; no-go holds it with a reason.

Where testing is on (the Testing service), an acceptance test that names a test of the loaded test scripts is joined to it: a
test lead opens a round of testing for an approved release from its page, and the page shows how each acceptance test was
answered in that round, beside the CPUC's decision. The decision stays the CPUC's: a failed test does not stop it."""

import zlib
from urllib.parse import urlencode

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from cedars.core import queue as my_queue
from cedars.services import audit
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.feedback import service as requests
from cedars.services.feedback.models import Feedback
from cedars.services.notifications import service as notifications
from cedars.services.testing import service as testing

from . import tracker_client
from .models import AcceptanceTest, Follow, ReleasePlan, RequestLink, TrackerPull, WorkItem

STORY = ("title", "kind", "who", "pages", "need", "why", "size", "severity", "workaround")
QUEUE_MOST = 50
KIND_OF = {Feedback.Kind.IDEA: WorkItem.Kind.IMPROVEMENT}  # a request's kind to the work item's; anything else is a problem


class WorkRefused(Exception):
    """The step cannot be taken as the item stands; the message says why, in words for the page."""


# ---- who


def _held(user) -> set[str]:
    if user is None or not user.is_authenticated:
        return set()
    held = access.effective_permissions(user, access.cpuc())
    if perms.SYSTEM_ADMIN in held:
        held |= {perms.WORK_MANAGE, perms.WORK_APPROVE}
    elif perms.WORK_MANAGE not in held and manages_elsewhere(user):
        held |= {perms.WORK_MANAGE}
    return held


def manages_elsewhere(user) -> bool:
    """Holds the backlog permission in an organization the CPUC marked as managing the backlog (the developer's)."""
    return any(
        access.has_permission(user, org, perms.WORK_MANAGE)
        for org in access.backlog_organisations().exclude(kind=access.Organisation.Kind.CPUC)
    )


def can_manage(user) -> bool:
    return perms.WORK_MANAGE in _held(user)


def can_approve(user) -> bool:
    return perms.WORK_APPROVE in _held(user)


def can_propose(user) -> bool:
    """Who may propose a work item and join requests to one: the backlog managers and the support desk."""
    return can_manage(user) or requests.can_answer(user)


def can_see(user) -> bool:
    return can_propose(user) or can_approve(user)


def _require(ok: bool, what: str) -> None:
    if not ok:
        raise AccessDenied(what)


def _holders(permission: str) -> list:
    org = access.cpuc()
    if org is None:
        return []
    people = {u.pk: u for u in access.holders(org, perms.SYSTEM_ADMIN)}
    people.update({u.pk: u for u in access.holders(org, permission)})
    if permission == perms.WORK_MANAGE:
        for other in access.backlog_organisations().exclude(pk=org.pk):
            people.update({u.pk: u for u in access.holders(other, permission)})
    return list(people.values())


def _tell(users, title: str, body: str, item: WorkItem, key: str, *, but=None) -> None:
    for user in users:
        if but is not None and user.pk == but.pk:
            continue
        notifications.notify(user, title, body, f"/admin/work/{item.pk}/", key=f"work:{item.pk}:{key}")


# ---- writing an item


def _clean(fields: dict) -> dict:
    out = {}
    for name in STORY:
        if name in fields:
            out[name] = (fields[name] or "").strip()
    if "title" in out and not out["title"]:
        raise WorkRefused("Give the work item a title in plain words.")
    if "kind" in out and out["kind"] not in WorkItem.Kind.values:
        raise WorkRefused("Say whether it is a problem or an improvement.")
    if out.get("size") and out["size"] not in WorkItem.Size.values:
        raise WorkRefused("Choose a size from the list.")
    if out.get("severity") and out["severity"] not in WorkItem.Severity.values:
        raise WorkRefused("Choose a severity from the list.")
    return out


def snapshot(item: WorkItem) -> dict:
    return {name: getattr(item, name) for name in (*STORY, "status", "public", "position")}


@transaction.atomic
def propose(actor, *, request: Feedback | None = None, **fields) -> WorkItem:
    """A new Proposed work item; from a request, the request is joined to it."""
    _require(can_propose(actor), "Only the CPUC's backlog managers and support desk can propose a work item.")
    if request is not None:
        _linkable(actor, request)
    values = _clean({"title": "", "kind": "", **fields})
    item = WorkItem.objects.create(created_by=actor, **values)
    audit.record("work.proposed", obj=item, after=snapshot(item), actor=actor)
    if request is not None:
        _join(actor, item, request)
    transaction.on_commit(
        lambda: _tell(_holders(perms.WORK_MANAGE), f"Work item proposed: {item.title}", item.need, item, "proposed", but=actor)
    )
    return item


@transaction.atomic
def edit(actor, item: WorkItem, **fields) -> WorkItem:
    _require(can_manage(actor), "Only a backlog manager can change a work item.")
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    values = _clean(fields)
    before = {k: getattr(item, k) for k in values}
    changed = {k: v for k, v in values.items() if before[k] != v}
    if not changed:
        return item
    for k, v in changed.items():
        setattr(item, k, v)
    item.save()
    audit.record("work.changed", obj=item, before={k: before[k] for k in changed}, after=changed, actor=actor)
    return item


# ---- requests joined to an item


def _linkable(actor, fb: Feedback) -> None:
    _require(can_propose(actor), "Only the CPUC's backlog managers and support desk can join a request to a work item.")
    if fb.kind == Feedback.Kind.SECURITY and not access.is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator can work on a security concern.")
    if RequestLink.objects.filter(feedback=fb).exists():
        raise WorkRefused("That request is already joined to a work item. Take it off that one first.")


def _join(actor, item: WorkItem, fb: Feedback) -> RequestLink:
    link = RequestLink.objects.create(
        work_item=item, feedback=fb, kind=fb.kind, page=fb.page, organisation=fb.organisation, sent_at=fb.created_at, linked_by=actor
    )
    if fb.kind == Feedback.Kind.SECURITY and (item.public or item.public_proposed_by_id):  # never public, however it got there
        item.public, item.public_proposed_by = False, None
        item.save(update_fields=["public", "public_proposed_by", "updated_at"])
    audit.record("work.request_joined", obj=item, after={"request": fb.pk, "kind": fb.kind, "page": fb.page}, actor=actor)
    return link


@transaction.atomic
def join(actor, item: WorkItem, fb: Feedback) -> RequestLink:
    """Join a request to an existing work item: how duplicates are handled (twenty requests, one item)."""
    _linkable(actor, fb)
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    return _join(actor, item, fb)


@transaction.atomic
def unjoin(actor, link: RequestLink) -> None:
    _require(can_propose(actor), "Only the CPUC's backlog managers and support desk can take a request off a work item.")
    audit.record("work.request_unjoined", obj=link.work_item, before={"request": link.feedback_id, "kind": link.kind}, actor=actor)
    link.delete()


def item_of(fb: Feedback) -> WorkItem | None:
    link = RequestLink.objects.filter(feedback=fb).select_related("work_item").first()
    return link.work_item if link else None


def is_sensitive(item: WorkItem) -> bool:
    """Joined to a security concern: never public, and its evidence names no page to those who may not read the concern."""
    return item.links.filter(kind=Feedback.Kind.SECURITY).exists()


def evidence(item: WorkItem) -> dict:
    """What the order of the backlog rests on: how many requests, from how many organizations, and of which kinds."""
    links = list(item.links.select_related("organisation", "feedback"))
    follows = list(item.follows.all())
    return {
        "links": links,
        "requests": len(links),
        "followers": len(follows),
        "organisations": len({x.organisation_id for x in (*links, *follows) if x.organisation_id}),
        "urgent": sum(1 for link in links if link.kind in Feedback.URGENT),
    }


# ---- the backlog: one list in one order


def backlog():
    return WorkItem.objects.filter(status=WorkItem.Status.ACCEPTED).order_by("position", "pk")


def _renumber(ids: list[int]) -> None:
    for n, pk in enumerate(ids, start=1):
        WorkItem.objects.filter(pk=pk).exclude(position=n).update(position=n)


def _locked_backlog() -> list[int]:
    return list(backlog().select_for_update().values_list("pk", flat=True))


@transaction.atomic
def accept(actor, item: WorkItem) -> WorkItem:
    """Into the backlog, at the bottom; the manager then moves it to its place."""
    _require(can_manage(actor), "Only a backlog manager can accept a work item into the backlog.")
    ids = _locked_backlog()
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if item.status != WorkItem.Status.PROPOSED:
        raise WorkRefused("Only a proposed work item can be accepted.")
    item.status, item.position = WorkItem.Status.ACCEPTED, len(ids) + 1
    item.decline_proposed, item.decline_proposed_by = "", None
    item.decided_by, item.decided_at = actor, timezone.now()
    if not item.public_proposed_by_id:
        item.proposed_at = None
    item.save()
    audit.record(
        "work.accepted", obj=item, before={"status": "proposed"}, after={"status": "accepted", "position": item.position}, actor=actor
    )
    return item


@transaction.atomic
def move(actor, item: WorkItem, to: int | str) -> WorkItem:
    """Move an item in the backlog: "up", "down", or to a position (1 is first)."""
    _require(can_manage(actor), "Only a backlog manager can order the backlog.")
    ids = _locked_backlog()
    if item.pk not in ids:
        raise WorkRefused("Only a work item in the backlog can be moved.")
    was = ids.index(item.pk) + 1
    if to == "up":
        to = was - 1
    elif to == "down":
        to = was + 1
    try:
        to = int(to)
    except (TypeError, ValueError):
        raise WorkRefused("Give the position as a whole number.") from None
    to = min(max(to, 1), len(ids))
    if to != was:
        ids.remove(item.pk)
        ids.insert(to - 1, item.pk)
        _renumber(ids)
        audit.record("work.moved", obj=item, before={"position": was}, after={"position": to}, actor=actor)
    item.refresh_from_db()
    return item


# ---- declining, and making public: a manager proposes, the CPUC approves


def _proposal_made(item: WorkItem) -> None:
    if item.proposed_at is None:
        item.proposed_at = timezone.now()


def _proposal_settled(item: WorkItem) -> None:
    if not item.awaits_approval:
        item.proposed_at = None


def _not_planned(item: WorkItem, what: str) -> None:
    if item.status == WorkItem.Status.DONE:
        raise WorkRefused(f"It is done: it went out in release {item.release.name}.")
    if item.status == WorkItem.Status.PLANNED:
        raise WorkRefused(f"It is planned for release {item.release.name}. Take it out of the release before {what}.")


def _decline(actor, item: WorkItem, reason: str, proposed_by=None) -> None:
    was = item.status
    item.status, item.position, item.declined_reason = WorkItem.Status.DECLINED, None, reason
    item.decline_proposed, item.decline_proposed_by = "", None
    item.public, item.public_proposed_by = False, None
    item.decided_by, item.decided_at = actor, timezone.now()
    _proposal_settled(item)
    item.save()
    _renumber(list(backlog().values_list("pk", flat=True)))
    audit.record(
        "work.declined",
        obj=item,
        before={"status": was},
        after={"status": "declined", "proposed_by": proposed_by.pk if proposed_by else None},
        reason=reason,
        actor=actor,
    )
    transaction.on_commit(lambda: _tell_people(item, "The CPUC decided not to do this", f"The reason given: {reason}", "declined"))


@transaction.atomic
def decline(actor, item: WorkItem, reason: str) -> WorkItem:
    """Decline an item with a reason. An approver's decline takes effect; a manager's is a proposal for the CPUC to confirm."""
    _require(can_manage(actor) or can_approve(actor), "Only a backlog manager or the CPUC's approver can decline a work item.")
    reason = (reason or "").strip()
    if not reason:
        raise WorkRefused("Give the reason: the people who asked are told it.")
    _locked_backlog()
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if item.status == WorkItem.Status.DECLINED:
        raise WorkRefused("That work item is already declined.")
    _not_planned(item, "declining it")
    if can_approve(actor):
        _decline(actor, item, reason)
        return item
    item.decline_proposed, item.decline_proposed_by = reason, actor
    _proposal_made(item)
    item.save()
    audit.record("work.decline_proposed", obj=item, reason=reason, actor=actor)
    transaction.on_commit(
        lambda: _tell(
            _holders(perms.WORK_APPROVE), f"Approve declining: {item.title}", reason, item, f"decline:{item.updated_at:%Y%m%d%H%M%S}"
        )
    )
    return item


@transaction.atomic
def make_public(actor, item: WorkItem) -> WorkItem:
    """Show the item's title, workaround and status to anyone. An approver's takes effect; a manager's is a proposal."""
    _require(can_manage(actor) or can_approve(actor), "Only a backlog manager or the CPUC's approver can make a work item public.")
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if item.public:
        raise WorkRefused("That work item is already public.")
    if item.status == WorkItem.Status.DECLINED:
        raise WorkRefused("A declined work item is not made public.")
    if is_sensitive(item):
        raise WorkRefused("A work item joined to a security concern is never made public.")
    if can_approve(actor):
        proposer = item.public_proposed_by
        item.public, item.public_proposed_by = True, None
        _proposal_settled(item)
        item.save()
        audit.record(
            "work.made_public",
            obj=item,
            before={"public": False},
            after={"public": True, "proposed_by": proposer.pk if proposer else None},
            actor=actor,
        )
        return item
    if item.public_proposed_by_id:
        raise WorkRefused("Making it public is already waiting for the CPUC's approval.")
    item.public_proposed_by = actor
    _proposal_made(item)
    item.save()
    audit.record("work.public_proposed", obj=item, actor=actor)
    transaction.on_commit(lambda: _tell(_holders(perms.WORK_APPROVE), f"Approve making public: {item.title}", item.title, item, "public"))
    return item


@transaction.atomic
def make_internal(actor, item: WorkItem, reason: str) -> WorkItem:
    _require(can_approve(actor), "Only the CPUC's approver can take a work item out of public view.")
    reason = (reason or "").strip()
    if not reason:
        raise WorkRefused("Give a reason for taking it out of public view.")
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if not item.public:
        raise WorkRefused("That work item is not public.")
    item.public = False
    item.save()
    audit.record("work.made_internal", obj=item, before={"public": True}, after={"public": False}, reason=reason, actor=actor)
    return item


@transaction.atomic
def approve(actor, item: WorkItem, what: str) -> WorkItem:
    """The CPUC confirms a manager's proposal: what is "decline" or "public"."""
    _require(can_approve(actor), "Only the CPUC's approver can approve it.")
    _locked_backlog()
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if what == "decline" and item.decline_proposed_by_id:
        _not_planned(item, "declining it")
        _decline(actor, item, item.decline_proposed, proposed_by=item.decline_proposed_by)
    elif what == "public" and item.public_proposed_by_id:
        if is_sensitive(item):
            raise WorkRefused("A work item joined to a security concern is never made public.")
        proposer = item.public_proposed_by
        item.public, item.public_proposed_by = True, None
        _proposal_settled(item)
        item.save()
        audit.record(
            "work.made_public", obj=item, before={"public": False}, after={"public": True, "proposed_by": proposer.pk}, actor=actor
        )
    else:
        raise WorkRefused("Nothing of that kind is waiting for approval on this work item.")
    return item


@transaction.atomic
def turn_down(actor, item: WorkItem, what: str, reason: str) -> WorkItem:
    """The CPUC does not approve a manager's proposal; the item stays as it was and the manager is told why."""
    _require(can_approve(actor), "Only the CPUC's approver can turn it down.")
    reason = (reason or "").strip()
    if not reason:
        raise WorkRefused("Give the reason, for the backlog manager who proposed it.")
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if what == "decline" and item.decline_proposed_by_id:
        proposer = item.decline_proposed_by
        item.decline_proposed, item.decline_proposed_by = "", None
    elif what == "public" and item.public_proposed_by_id:
        proposer = item.public_proposed_by
        item.public_proposed_by = None
    else:
        raise WorkRefused("Nothing of that kind is waiting for approval on this work item.")
    _proposal_settled(item)
    item.save()
    audit.record("work.proposal_turned_down", obj=item, after={"what": what, "proposed_by": proposer.pk}, reason=reason, actor=actor)
    words = "declining" if what == "decline" else "making public"
    transaction.on_commit(
        lambda: _tell([proposer], f"Not approved: {words} {item.title}", reason, item, f"turned-down:{what}:{timezone.now():%Y%m%d%H%M%S}")
    )
    return item


@transaction.atomic
def reconsider(actor, item: WorkItem, reason: str) -> WorkItem:
    """A declined item back to Proposed, with a reason."""
    _require(can_approve(actor), "Only the CPUC's approver can reconsider a declined work item.")
    reason = (reason or "").strip()
    if not reason:
        raise WorkRefused("Give a reason for reconsidering it.")
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if item.status != WorkItem.Status.DECLINED:
        raise WorkRefused("Only a declined work item can be reconsidered.")
    item.status, item.declined_reason, item.decided_by, item.decided_at = WorkItem.Status.PROPOSED, "", None, None
    item.save()
    audit.record("work.reconsidered", obj=item, before={"status": "declined"}, after={"status": "proposed"}, reason=reason, actor=actor)
    return item


# ---- acceptance tests, and whether an item is ready for a release


@transaction.atomic
def add_test(actor, item: WorkItem, *, title: str, steps: str, expected: str, library_id: str = "") -> AcceptanceTest:
    """An acceptance test: what "done" means for the item."""
    _require(can_manage(actor), "Only a backlog manager can write a work item's acceptance tests.")
    title, steps, expected = (title or "").strip(), (steps or "").strip(), (expected or "").strip()
    if not (title and steps and expected):
        raise WorkRefused("An acceptance test needs a title, its steps and the expected result.")
    test = AcceptanceTest.objects.create(
        work_item=item, title=title, steps=steps, expected=expected, library_id=(library_id or "").strip(), created_by=actor
    )
    audit.record("work.test_added", obj=item, after={"test": test.pk, "title": title, "library_id": test.library_id}, actor=actor)
    return test


@transaction.atomic
def remove_test(actor, test: AcceptanceTest) -> None:
    _require(can_manage(actor), "Only a backlog manager can remove a work item's acceptance tests.")
    item = WorkItem.objects.select_for_update().get(pk=test.work_item_id)
    if item.status == WorkItem.Status.PLANNED and item.tests.count() == 1:
        raise WorkRefused("A work item planned for a release keeps at least one acceptance test. Add another first.")
    audit.record("work.test_removed", obj=item, before={"test": test.pk, "title": test.title}, actor=actor)
    test.delete()


def lacking(item: WorkItem) -> list[str]:
    """What the item still needs before it can be planned for a release; empty when it is ready."""
    out = []
    if not (item.who and item.need):
        out.append("a story (who needs it and what they need)")
    if not item.page_list:
        out.append("the pages it touches")
    if not item.size:
        out.append("a size")
    if not item.tests.exists():
        out.append("an acceptance test")
    return out


# ---- release plans: filled from the backlog by a manager, approved by the CPUC


def _plan_snapshot(plan: ReleasePlan) -> dict:
    return {"name": plan.name, "expected_on": str(plan.expected_on) if plan.expected_on else None, "note": plan.note, "status": plan.status}


def _plan_details(name: str, expected_on, note: str, but: ReleasePlan | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise WorkRefused("Give the release a name or number.")
    if ReleasePlan.objects.filter(name__iexact=name).exclude(pk=but.pk if but else None).exists():
        raise WorkRefused(f"There is already a release plan named {name}.")
    return {"name": name, "expected_on": expected_on or None, "note": (note or "").strip()}


@transaction.atomic
def create_plan(actor, *, name: str, expected_on=None, note: str = "") -> ReleasePlan:
    _require(can_manage(actor), "Only a backlog manager can start a release plan.")
    plan = ReleasePlan.objects.create(created_by=actor, **_plan_details(name, expected_on, note))
    audit.record("work.plan_created", obj=plan, after=_plan_snapshot(plan), actor=actor)
    return plan


@transaction.atomic
def change_plan(actor, plan: ReleasePlan, *, name: str, expected_on=None, note: str = "") -> ReleasePlan:
    """Its name, expected date and note. The date of an approved plan is the CPUC's to change."""
    _require(can_manage(actor) or can_approve(actor), "Only a backlog manager or the CPUC's approver can change a release plan.")
    plan = ReleasePlan.objects.select_for_update().get(pk=plan.pk)
    _not_released(plan)
    before = _plan_snapshot(plan)
    values = _plan_details(name, expected_on, note, but=plan)
    if plan.status == ReleasePlan.Status.APPROVED and values["expected_on"] != plan.expected_on and not can_approve(actor):
        raise WorkRefused("The plan is approved, so its expected date is the CPUC's approver's to change.")
    for k, v in values.items():
        setattr(plan, k, v)
    plan.save()
    after = _plan_snapshot(plan)
    if after != before:
        audit.record("work.plan_changed", obj=plan, before=before, after=after, actor=actor)
    return plan


def _not_released(plan: ReleasePlan) -> None:
    if plan.status == ReleasePlan.Status.RELEASED:
        raise WorkRefused(f"Release {plan.name} is released, so its plan no longer changes.")


def _contents_changed(actor, plan: ReleasePlan) -> None:
    """A manager's change to an approved plan's work items sends it back for approval; an approver's own does not."""
    if plan.status != ReleasePlan.Status.APPROVED or can_approve(actor):
        return
    plan.status, plan.proposed_by, plan.proposed_at = ReleasePlan.Status.PROPOSED, actor, timezone.now()
    plan.approved_by, plan.approved_at = None, None
    plan.save()
    audit.record("work.plan_proposed", obj=plan, after={"status": "proposed", "why": "its work items changed"}, actor=actor)
    transaction.on_commit(lambda: _tell_plan(plan, f"Approve release {plan.name} again: its work items changed"))


def _tell_plan(plan: ReleasePlan, title: str, users=None, body: str = "") -> None:
    key = f"work:plan:{plan.pk}:{zlib.crc32(title.encode()):x}:{timezone.now():%Y%m%d%H%M%S}"  # one of each kind a second
    for user in users if users is not None else _holders(perms.WORK_APPROVE):
        notifications.notify(user, title, body or plan.note, f"/admin/work/releases/{plan.pk}/", key=key)


@transaction.atomic
def plan_item(actor, item: WorkItem, plan: ReleasePlan) -> WorkItem:
    """Take an item from the backlog into a release. It must be ready: a story, its pages, a size and an acceptance test."""
    _require(can_manage(actor), "Only a backlog manager can plan a work item for a release.")
    _locked_backlog()
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    plan = ReleasePlan.objects.select_for_update().get(pk=plan.pk)
    _not_released(plan)
    if item.status != WorkItem.Status.ACCEPTED:
        raise WorkRefused("Only a work item in the backlog can be planned for a release.")
    if item.decline_proposed_by_id:
        raise WorkRefused("Declining it is waiting for the CPUC's approval, so it cannot be planned.")
    need = lacking(item)
    if need:
        raise WorkRefused(f"{item.number} is not ready for a release. It still needs " + ", ".join(need) + ".")
    was = item.position
    item.status, item.release, item.position = WorkItem.Status.PLANNED, plan, None
    item.save()
    _renumber(list(backlog().values_list("pk", flat=True)))
    audit.record(
        "work.planned",
        obj=item,
        before={"status": "accepted", "position": was},
        after={"status": "planned", "release": plan.name},
        actor=actor,
    )
    _contents_changed(actor, plan)
    return item


@transaction.atomic
def unplan_item(actor, item: WorkItem) -> WorkItem:
    """Out of its release and back to the top of the backlog."""
    _require(can_manage(actor), "Only a backlog manager can take a work item out of a release.")
    ids = _locked_backlog()
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if item.status != WorkItem.Status.PLANNED:
        raise WorkRefused("That work item is not planned for a release.")
    plan = ReleasePlan.objects.select_for_update().get(pk=item.release_id)
    item.status, item.release, item.built_at, item.built_by = WorkItem.Status.ACCEPTED, None, None, None
    item.save()
    _renumber([item.pk, *ids])
    audit.record(
        "work.unplanned",
        obj=item,
        before={"status": "planned", "release": plan.name},
        after={"status": "accepted", "position": 1},
        actor=actor,
    )
    _contents_changed(actor, plan)
    item.refresh_from_db()
    return item


@transaction.atomic
def propose_plan(actor, plan: ReleasePlan) -> ReleasePlan:
    """The manager proposes the filled plan for the CPUC's approval; a person who also approves approves it in the same step."""
    _require(can_manage(actor), "Only a backlog manager can propose a release plan.")
    plan = ReleasePlan.objects.select_for_update().get(pk=plan.pk)
    if plan.status != ReleasePlan.Status.DRAFT:
        raise WorkRefused("That release plan is already proposed or approved.")
    if not plan.items.exists():
        raise WorkRefused("Plan at least one work item for the release before proposing it.")
    plan.status, plan.proposed_by, plan.proposed_at, plan.sent_back_reason = ReleasePlan.Status.PROPOSED, actor, timezone.now(), ""
    plan.save()
    audit.record("work.plan_proposed", obj=plan, after={"status": "proposed", "items": plan.items.count()}, actor=actor)
    if can_approve(actor):
        return _approve_plan(actor, plan)
    transaction.on_commit(lambda: _tell_plan(plan, f"Approve the plan for release {plan.name}"))
    return plan


def _approve_plan(actor, plan: ReleasePlan) -> ReleasePlan:
    plan.status, plan.approved_by, plan.approved_at = ReleasePlan.Status.APPROVED, actor, timezone.now()
    plan.save()
    audit.record(
        "work.plan_approved",
        obj=plan,
        after={"status": "approved", "items": sorted(plan.items.values_list("pk", flat=True)), "proposed_by": plan.proposed_by_id},
        actor=actor,
    )
    when = f", expected {plan.expected_on:%Y-%m-%d}" if plan.expected_on else ""

    def tell():
        for item in plan.items.all():
            _tell_people(
                item, f"Planned for release {plan.name}{when}", "The date may change. Release notes announce it.", f"planned:{plan.pk}"
            )

    transaction.on_commit(tell)
    for item in plan.items.select_for_update().filter(issue_state="closed", built_at__isnull=True):
        _set_built(item, True)  # its issue closed before the plan was approved
    return plan


@transaction.atomic
def approve_plan(actor, plan: ReleasePlan) -> ReleasePlan:
    _require(can_approve(actor), "Only the CPUC's approver can approve a release plan.")
    plan = ReleasePlan.objects.select_for_update().get(pk=plan.pk)
    if plan.status != ReleasePlan.Status.PROPOSED:
        raise WorkRefused("That release plan is not waiting for approval.")
    return _approve_plan(actor, plan)


@transaction.atomic
def send_plan_back(actor, plan: ReleasePlan, reason: str) -> ReleasePlan:
    """The CPUC does not approve the plan as proposed: it is being filled again, and the manager is told why."""
    _require(can_approve(actor), "Only the CPUC's approver can send a release plan back.")
    reason = (reason or "").strip()
    if not reason:
        raise WorkRefused("Give the reason, for the backlog manager who proposed it.")
    plan = ReleasePlan.objects.select_for_update().get(pk=plan.pk)
    if plan.status != ReleasePlan.Status.PROPOSED:
        raise WorkRefused("That release plan is not waiting for approval.")
    proposer = plan.proposed_by
    plan.status, plan.sent_back_reason = ReleasePlan.Status.DRAFT, reason
    plan.save()
    audit.record("work.plan_sent_back", obj=plan, before={"status": "proposed"}, after={"status": "draft"}, reason=reason, actor=actor)
    if proposer is not None:
        transaction.on_commit(lambda: _tell_plan(plan, f"Not approved: the plan for release {plan.name}", [proposer], reason))
    return plan


def plannable() -> list[WorkItem]:
    """The backlog in order, each item with what it still lacks before it can be planned."""
    items = list(backlog().prefetch_related("tests"))
    for it in items:
        it.lacking = lacking(it)
    return items


# ---- the issue for the developers


def issue_text(item: WorkItem) -> tuple[str, str]:
    """The title and body of the item's issue: its story, pages and acceptance tests. Never its requests, which may name people."""
    lines = [f"Work item {item.number}: {item.get_kind_display().lower()}"]
    if item.size:
        lines[0] += f", size {item.get_size_display().lower()}"
    if item.severity:
        lines[0] += f", {item.get_severity_display().lower()}"
    lines += ["", "## Story", f"As {item.who}, I need: {item.need}"]
    if item.why:
        lines.append(f"So that: {item.why}")
    lines += ["", "## Pages", *[f"- `{p}`" for p in item.page_list], "", "## Acceptance tests"]
    for n, test in enumerate(item.tests.all(), 1):
        lines.append(f"{n}. {test.title}" + (f" ({test.library_id})" if test.library_id else ""))
        lines += [f"   - {step}" for step in test.step_list]
        lines.append(f"   - Expected: {test.expected}")
    if item.workaround:
        lines += ["", "## Workaround meanwhile", item.workaround]
    return item.title, "\n".join(lines)


def issue_blocked(item: WorkItem) -> str:
    """Why no issue can be started for the item now; empty when one can."""
    if not getattr(settings, "CEDARS_ISSUE_REPOSITORY", ""):
        return "No issue tracker is set for this site."
    if is_sensitive(item):
        return "It is joined to a security concern, so its issue is written by hand where only the developers read it."
    need = lacking(item)
    if need:
        return "It still needs " + ", ".join(need) + "."
    return ""


def issue_link(item: WorkItem) -> str:
    """The address that starts the item's issue in the developers' tracker, filled in; empty when it cannot be started."""
    if issue_blocked(item):
        return ""
    title, body = issue_text(item)
    return f"https://github.com/{settings.CEDARS_ISSUE_REPOSITORY}/issues/new?" + urlencode({"title": title, "body": body})


@transaction.atomic
def record_issue(actor, item: WorkItem, url: str) -> WorkItem:
    """Keep the address of the item's issue (blank takes it off)."""
    _require(can_manage(actor), "Only a backlog manager can record a work item's issue.")
    url = (url or "").strip()
    if url and not url.startswith("https://"):
        raise WorkRefused("Give the issue's full address, starting with https://.")
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if url != item.issue_url:
        was, item.issue_url = item.issue_url, url
        item.issue_state, item.issue_changed_at = "", None  # what the tracker said was about the other issue
        item.save()
        audit.record("work.issue_recorded", obj=item, before={"issue": was}, after={"issue": url}, actor=actor)
    return item


def can_post_issue() -> bool:
    return tracker_client.can_post()


@transaction.atomic
def post_issue(actor, item: WorkItem) -> WorkItem:
    """A backlog manager, having read the drafted issue, has the site post it to the developers' tracker. The issue's
    address is kept on the item. Refused for an item that already has an issue: one item, one issue."""
    _require(can_manage(actor), "Only a backlog manager can post a work item's issue.")
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if item.status in (WorkItem.Status.DECLINED, WorkItem.Status.DONE):
        raise WorkRefused("An issue is posted for a work item that is still to be done.")
    if item.issue_url:
        raise WorkRefused("It already has an issue. Take the issue's address off first to post another.")
    why = issue_blocked(item)
    if why:
        raise WorkRefused(why)
    title, body = issue_text(item)
    try:
        url, number = tracker_client.post_issue(title, body)
    except tracker_client.TrackerError as e:
        raise WorkRefused(f"{e} Nothing was kept. Try again, or start the issue by the link.") from e
    item.issue_url, item.issue_state, item.issue_changed_at = url, "open", None
    item.save()
    audit.record("work.issue_posted", obj=item, after={"issue": url, "number": number}, actor=actor)
    return item


# ---- built, then the decision on the release


@transaction.atomic
def mark_built(actor, item: WorkItem, built: bool = True) -> WorkItem:
    """A backlog manager says an item of an approved release plan is built (or is not after all). When the last one is
    built, the CPUC's approvers are told the release is ready for their decision."""
    _require(can_manage(actor), "Only a backlog manager can mark a work item built.")
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if item.status != WorkItem.Status.PLANNED or item.release.status != ReleasePlan.Status.APPROVED:
        raise WorkRefused("Only a work item of an approved release plan is marked built.")
    return _set_built(item, built, actor)


TRACKER = "the developers' tracker"  # who the audit trail names for what a signed event did


def _set_built(item: WorkItem, built: bool, actor=None) -> WorkItem:
    """Mark a locked item of an approved plan built or not: by a backlog manager, or (no actor) by its issue closing."""
    if built == bool(item.built_at):
        return item
    item.built_at, item.built_by = (timezone.now(), actor) if built else (None, None)
    item.save()
    audit.record(
        "work.built" if built else "work.not_built",
        obj=item,
        after={"release": item.release.name, "by": "a backlog manager" if actor else "its issue in the developers' tracker"},
        actor=actor,
        actor_label="" if actor else TRACKER,
    )
    plan = item.release
    if built and not plan.items.filter(built_at__isnull=True).exists():
        if plan.held_at:  # built again after a no-go: the decision is the CPUC's to take afresh
            plan.held_reason, plan.held_by, plan.held_at = "", None, None
            plan.save()
        transaction.on_commit(lambda: _tell_plan(plan, f"Release {plan.name} is built: decide whether it goes"))
    return item


# ---- signed events from the developers' tracker


def issue_closed(item: WorkItem, closed: bool, when) -> bool:
    """The tracker says the item's issue closed (as done) or reopened. A closed issue marks an item of an approved plan built;
    a reopened one takes back a mark the tracker made, never a backlog manager's. False when the event is older than what is
    already kept."""
    item = WorkItem.objects.select_for_update().get(pk=item.pk)
    if item.issue_changed_at and when < item.issue_changed_at:
        return False
    was, item.issue_state, item.issue_changed_at = item.issue_state, "closed" if closed else "open", when
    item.save()
    if was != item.issue_state:
        audit.record(
            "work.issue_closed" if closed else "work.issue_reopened",
            obj=item,
            before={"issue": was},
            after={"issue": item.issue_state},
            actor_label=TRACKER,
        )
    if item.status == WorkItem.Status.PLANNED and item.release.status == ReleasePlan.Status.APPROVED:
        if closed:
            _set_built(item, True)
        elif item.built_at and item.built_by_id is None:
            _set_built(item, False)
    return True


def pull_seen(item: WorkItem, *, number: int, url: str, state: str, when) -> bool:
    """The tracker says a pull request that names the item was opened, merged or closed. False when the event is older than
    what is already kept."""
    pull = TrackerPull.objects.select_for_update().filter(work_item=item, url=url).first()
    if pull and when < pull.changed_at:
        return False
    was = pull.state if pull else ""
    if pull is None:
        pull = TrackerPull(work_item=item, number=number, url=url)
    pull.state, pull.changed_at = state, when
    pull.save()
    if was != state:
        audit.record("work.pull_request", obj=item, before={"state": was}, after={"number": number, "state": state}, actor_label=TRACKER)
    return True


def development(item: WorkItem) -> dict:
    """Where the code work stands, from the tracker's events: the issue's state and the pull requests that name the item."""
    pulls = list(item.pulls.all())
    states = {p.state for p in pulls}
    if "open" in states:
        words = "In progress: a pull request is open"
    elif "merged" in states:
        words = "Merged"
    else:
        words = ""
    return {"pulls": pulls, "words": words, "issue_state": item.issue_state, "by_tracker": bool(item.built_at and not item.built_by_id)}


def unbuilt(plan: ReleasePlan) -> list[WorkItem]:
    return list(plan.items.filter(built_at__isnull=True).order_by("pk"))


@transaction.atomic
def release_goes(actor, plan: ReleasePlan, released_on=None) -> ReleasePlan:
    """The CPUC's go: the release is out, its work items are done, and the people waiting on them are told."""
    _require(can_approve(actor), "Only the CPUC's approver decides whether a release goes.")
    plan = ReleasePlan.objects.select_for_update().get(pk=plan.pk)
    if plan.status != ReleasePlan.Status.APPROVED:
        raise WorkRefused("Only an approved release plan is decided.")
    waiting = unbuilt(plan)
    if waiting:
        raise WorkRefused(
            "Not every work item is built: " + ", ".join(it.number for it in waiting) + ". Take them out of the release, or wait."
        )
    items = list(plan.items.select_for_update().order_by("pk"))
    if not items:
        raise WorkRefused("The release holds no work item.")
    plan.status, plan.released_on, plan.released_by = ReleasePlan.Status.RELEASED, released_on or timezone.localdate(), actor
    plan.held_reason, plan.held_by, plan.held_at = "", None, None
    plan.save()
    for item in items:
        item.status, item.decided_by, item.decided_at = WorkItem.Status.DONE, actor, timezone.now()
        item.save()
    audit.record(
        "work.released",
        obj=plan,
        before={"status": "approved"},
        after={"status": "released", "released_on": str(plan.released_on), "items": [it.pk for it in items]},
        actor=actor,
    )

    def tell():
        for item in items:
            _tell_people(
                item,
                f"Released in release {plan.name}",
                "If it is still a problem for you, use the Feedback link at the foot of the page it is on.",
                f"released:{plan.pk}",
            )
        _tell_plan(plan, f"Release {plan.name} goes", _holders(perms.WORK_MANAGE), f"Released {plan.released_on:%Y-%m-%d}.")

    transaction.on_commit(tell)
    return plan


@transaction.atomic
def release_held(actor, plan: ReleasePlan, reason: str) -> ReleasePlan:
    """The CPUC's no-go: the release does not go yet. The plan stays approved, and the backlog managers are told why."""
    _require(can_approve(actor), "Only the CPUC's approver decides whether a release goes.")
    reason = (reason or "").strip()
    if not reason:
        raise WorkRefused("Give the reason, for the backlog managers.")
    plan = ReleasePlan.objects.select_for_update().get(pk=plan.pk)
    if plan.status != ReleasePlan.Status.APPROVED:
        raise WorkRefused("Only an approved release plan is decided.")
    plan.held_reason, plan.held_by, plan.held_at = reason, actor, timezone.now()
    plan.save()
    audit.record("work.release_held", obj=plan, reason=reason, actor=actor)
    transaction.on_commit(lambda: _tell_plan(plan, f"Release {plan.name} does not go yet", _holders(perms.WORK_MANAGE), reason))
    return plan


# ---- acceptance testing: the release's tests in the test scripts, and its round of testing


def testing_on() -> bool:
    return testing.enabled()


def can_test(user) -> bool:
    """A test lead: whoever sets up rounds of testing on this site."""
    return testing.can_manage(user)


def script_tests(tests) -> dict[int, object]:
    """For acceptance tests, the test of the newest loaded scripts each names: {acceptance test's pk: the script's test}. Empty
    where testing is off or no scripts are loaded."""
    if not testing.enabled():
        return {}
    found = testing.find_tests(testing.newest_version(), [t.library_id for t in tests])
    return {t.pk: found[t.library_id.strip().upper()] for t in tests if t.library_id.strip().upper() in found}


def testing_of(plan: ReleasePlan) -> dict | None:
    """A release's acceptance testing: every acceptance test of its work items, the script test each names (in the round's
    version of the scripts, or the newest loaded when no round is open yet), and how the round's testers answered it. None
    where testing is off."""
    if not testing.enabled():
        return None
    round_ = plan.campaign
    version = round_.version if round_ else testing.newest_version()
    items = list(plan.items.order_by("pk").prefetch_related("tests"))
    tests = [t for it in items for t in it.tests.all()]
    found = testing.find_tests(version, [t.library_id for t in tests])
    counts = testing.outcomes_by_test(round_) if round_ else {}
    rows, scripts = [], {}
    tally = {"in_scripts": 0, "by_hand": 0, "passed": 0, "failing": 0, "no_result": 0}
    for it in items:
        for t in it.tests.all():
            key = t.library_id.strip().upper()
            case = found.get(key)
            got = counts.get(key, {}) if case else {}
            failing = got.get("failed", 0) + got.get("blocked", 0)
            if not case:
                state = "by_hand"
            elif failing:
                state = "failing"
            elif got.get("passed", 0):
                state = "passed"
            else:
                state = "no_result"  # nobody answered it, or only "does not apply"
            tally[state] += 1
            if case:
                tally["in_scripts"] += 1
                scripts[case.script_id] = case.script
            rows.append(
                {
                    "item": it,
                    "test": t,
                    "case": case,
                    "state": state,
                    "failing": failing,
                    "passes": got.get("passed", 0),
                    "answers": sum(got.values()),
                }
            )
    return {
        "round": round_,
        "version": version,
        "rows": rows,
        "scripts": sorted(scripts.values(), key=lambda s: (s.position, s.pk)),
        "total": len(rows),
        **tally,
    }


def round_blocked(plan: ReleasePlan) -> str:
    """Why a round of testing cannot be opened for the release now; empty when one can."""
    if not testing.enabled():
        return "Testing is not switched on for this site."
    if plan.status != ReleasePlan.Status.APPROVED:
        return "A round of testing is opened once the CPUC has approved the plan and before the release goes out."
    if plan.campaign_id and plan.campaign.is_open:
        return f"Its round of testing, {plan.campaign.name}, is still open. Close it before opening another."
    if testing.newest_version() is None:
        return "No test scripts are loaded on this site yet."
    tests = AcceptanceTest.objects.filter(work_item__release=plan)
    if not testing.find_tests(testing.newest_version(), [t.library_id for t in tests]):
        return "None of its acceptance tests names a test of the loaded test scripts, so there is nothing to assign."
    return ""


@transaction.atomic
def open_round(actor, plan: ReleasePlan, *, name: str, starts_on, ends_on=None):
    """A test lead opens a round of testing for an approved release, on the newest loaded test scripts. Its note names the
    scripts to assign and the tests that are the release's acceptance tests. The backlog managers are told."""
    if not testing.can_manage(actor):
        raise AccessDenied("Only a test lead (a CPUC system administrator, where testing is on) opens a round of testing.")
    plan = ReleasePlan.objects.select_for_update().get(pk=plan.pk)
    why = round_blocked(plan)
    if why:
        raise WorkRefused(why)
    version = testing.newest_version()
    tests = list(AcceptanceTest.objects.filter(work_item__release=plan).order_by("work_item_id", "pk"))
    found = testing.find_tests(version, [t.library_id for t in tests])
    scripts = sorted({c.script for c in found.values()}, key=lambda s: (s.position, s.pk))
    note = (
        f"Acceptance testing of release {plan.name}.\n"
        f"Scripts to assign: {', '.join(s.code for s in scripts)}.\n"
        f"The release's acceptance tests in them: {', '.join(sorted(c.test_id for c in found.values()))}."
    )
    try:
        round_ = testing.create_campaign(actor, name=name, version=version, starts_on=starts_on, ends_on=ends_on, note=note)
    except testing.TestingRefused as e:
        raise WorkRefused(str(e)) from e
    was = plan.campaign.name if plan.campaign_id else ""
    plan.campaign = round_
    plan.save()
    audit.record(
        "work.round_opened",
        obj=plan,
        before={"round": was},
        after={"round": round_.name, "scripts": version.label, "tests": sorted(c.test_id for c in found.values())},
        actor=actor,
    )
    transaction.on_commit(
        lambda: _tell_plan(plan, f"Acceptance testing of release {plan.name} has started", _holders(perms.WORK_MANAGE), round_.name)
    )
    return round_


def to_decide():
    """The approved plans whose every work item is built and that the CPUC has not held since: ready for go or no-go."""
    plans = ReleasePlan.objects.filter(status=ReleasePlan.Status.APPROVED, held_at__isnull=True).order_by("expected_on", "pk")
    return [p for p in plans if p.items.exists() and not p.items.filter(built_at__isnull=True).exists()]


# ---- what people outside see


def public_items():
    """The work items anyone may read: public, and not declined (declining an item takes it out of public view)."""
    return WorkItem.objects.filter(public=True).exclude(status=WorkItem.Status.DECLINED).select_related("release")


def scheduled(item: WorkItem) -> ReleasePlan | None:
    """The release an item is to be in, once the CPUC has approved that release's plan; a plan still being filled or waiting
    for approval promises nothing."""
    if item.status == WorkItem.Status.PLANNED and item.release.status == ReleasePlan.Status.APPROVED:
        return item.release
    return None


def progress(item: WorkItem) -> dict:
    """Where an item stands, in words for people outside the CPUC."""
    plan = scheduled(item)
    if plan is not None:
        return {"key": "scheduled", "words": f"Planned for release {plan.name}", "plan": plan}
    if item.status == WorkItem.Status.DONE:
        return {"key": "done", "words": f"Released in release {item.release.name}", "plan": item.release}
    if item.status == WorkItem.Status.DECLINED:
        return {"key": "declined", "words": "Not planned", "reason": item.declined_reason}
    if item.status == WorkItem.Status.PROPOSED:
        return {"key": "considering", "words": "Being considered"}
    return {"key": "listed", "words": "To be done, not yet scheduled"}


def known_problems() -> list[WorkItem]:
    """The public problems, the most severe first."""
    order = {value: n for n, value in enumerate(WorkItem.Severity.values)}
    items = list(public_items().filter(kind=WorkItem.Kind.PROBLEM).exclude(status=WorkItem.Status.DONE))
    for it in items:
        it.progress = progress(it)
    return sorted(items, key=lambda it: (order.get(it.severity, len(order)), it.pk))


def upcoming() -> dict:
    """The public items by the approved release each is planned for (the soonest first), then those not yet scheduled."""
    releases, unscheduled = {}, []
    for it in public_items().exclude(status=WorkItem.Status.DONE).order_by("pk"):
        plan = scheduled(it)
        if plan is not None:
            releases.setdefault(plan.pk, (plan, []))[1].append(it)
        elif it.kind == WorkItem.Kind.IMPROVEMENT:
            it.progress = progress(it)
            unscheduled.append(it)
    far = timezone.localdate().replace(year=9999)
    return {"releases": sorted(releases.values(), key=lambda r: (r[0].expected_on or far, r[0].pk)), "unscheduled": unscheduled}


def known_for(page: str) -> list[WorkItem]:
    """The public problems that name this page (its address without the query), for the form people report a problem on."""
    path = (page or "").split("?")[0].split("#")[0]
    if not path:
        return []
    return [it for it in known_problems() if path in it.page_list]


def follows(user, item: WorkItem) -> bool:
    return bool(user is not None and user.is_authenticated and Follow.objects.filter(work_item=item, user=user).exists())


@transaction.atomic
def follow(user, item: WorkItem, organisation=None) -> Follow:
    """A signed-in person says a public item affects them too: they are told when it is planned or declined."""
    _require(user is not None and user.is_authenticated, "Sign in to follow a work item.")
    if not item.public or item.status in (WorkItem.Status.DECLINED, WorkItem.Status.DONE):
        raise WorkRefused("That work item is not one to follow.")
    row, made = Follow.objects.get_or_create(work_item=item, user=user, defaults={"organisation": organisation})
    if made:
        audit.record("work.followed", obj=item, after={"organisation": organisation.code if organisation else None}, actor=user)
    return row


@transaction.atomic
def unfollow(user, item: WorkItem) -> None:
    if Follow.objects.filter(work_item=item, user=user).delete()[0]:
        audit.record("work.unfollowed", obj=item, actor=user)


def _tell_people(item: WorkItem, title: str, body: str, key: str) -> None:
    """Tell the people outside who are waiting on an item: the senders of the requests joined to it (each by their own request,
    which names no work item) and its followers (by the item's title, which they have read)."""
    told = set()
    for link in item.links.select_related("feedback__user"):
        fb = link.feedback
        if fb is None or fb.user is None or fb.user_id in told:
            continue
        told.add(fb.user_id)
        notifications.notify(
            fb.user,
            f"Your request of {fb.created_at:%Y-%m-%d}: {title[0].lower()}{title[1:]}",
            body,
            f"/feedback/{fb.pk}/",
            key=f"work:{item.pk}:{key}",
        )
    link = f"/work-items/{item.pk}/" if item.public and item.status != WorkItem.Status.DECLINED else "/known-problems/"
    for row in item.follows.select_related("user"):
        if row.user_id not in told:
            told.add(row.user_id)
            notifications.notify(row.user, f"{title}: {item.title}", body, link, key=f"work:{item.pk}:{key}")


# ---- My queue


def awaiting_approval():
    return WorkItem.objects.filter(models.Q(decline_proposed_by__isnull=False) | models.Q(public_proposed_by__isnull=False)).order_by(
        "proposed_at", "pk"
    )


def to_approve(user) -> list:
    if not can_approve(user):
        return []
    return (
        [
            my_queue.Item(
                what=f"{item.number}: {item.title}",
                url=f"/admin/work/{item.pk}/",
                since=item.proposed_at,
                note=" and ".join(
                    w for w, on in (("Declining it", item.decline_proposed_by_id), ("Making it public", item.public_proposed_by_id)) if on
                )
                + " was proposed.",
            )
            for item in awaiting_approval()[:QUEUE_MOST]
        ]
        + [
            my_queue.Item(
                what=f"The plan for release {plan.name}",
                url=f"/admin/work/releases/{plan.pk}/",
                since=plan.proposed_at,
                note=f"{plan.items.count()} work items, proposed for the CPUC's approval.",
            )
            for plan in ReleasePlan.objects.filter(status=ReleasePlan.Status.PROPOSED).order_by("proposed_at")[:QUEUE_MOST]
        ]
        + [
            my_queue.Item(
                what=f"Release {plan.name}: go or no-go",
                url=f"/admin/work/releases/{plan.pk}/",
                since=plan.approved_at,
                note="Every work item is built. Decide whether the release goes.",
            )
            for plan in to_decide()[:QUEUE_MOST]
        ]
    )


def to_triage(user) -> list:
    if not can_manage(user):
        return []
    waiting = WorkItem.objects.filter(status=WorkItem.Status.PROPOSED, decline_proposed_by__isnull=True).order_by("created_at", "pk")
    return [
        my_queue.Item(
            what=f"{item.number}: {item.title}", url=f"/admin/work/{item.pk}/", since=item.created_at, note=item.get_kind_display()
        )
        for item in waiting[:QUEUE_MOST]
    ]

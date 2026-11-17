import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.feedback import service as requests
from cedars.services.feedback.models import Feedback

from . import forms, measures, service, tracker
from .models import AcceptanceTest, ReleasePlan, RequestLink, WorkItem

SHOW = [
    ("backlog", "The backlog"),
    ("planned", "Planned for a release"),
    ("proposed", "Proposed"),
    ("approval", "Waiting for approval"),
    ("declined", "Not planned"),
    ("done", "Done"),
    ("all", "All"),
]


def _require_staff(request):
    """The backlog managers, the CPUC's approvers and the support desk; anyone else is told nothing is here."""
    if not service.can_see(request.user):
        raise Http404


def _may(request) -> dict:
    return {
        "can_manage": service.can_manage(request.user),
        "can_approve": service.can_approve(request.user),
        "can_propose": service.can_propose(request.user),
    }


@login_required
def backlog(request):
    _require_staff(request)
    show = request.GET.get("show", "backlog")
    if show not in dict(SHOW):
        show = "backlog"
    if show == "backlog":
        qs = service.backlog()
    elif show == "proposed":
        qs = WorkItem.objects.filter(status=WorkItem.Status.PROPOSED).order_by("created_at", "pk")
    elif show == "planned":
        qs = (
            WorkItem.objects.filter(status=WorkItem.Status.PLANNED)
            .select_related("release")
            .order_by("release__expected_on", "release_id", "pk")
        )
    elif show == "approval":
        qs = service.awaiting_approval()
    elif show == "declined":
        qs = WorkItem.objects.filter(status=WorkItem.Status.DECLINED).order_by("-decided_at", "-pk")
    elif show == "done":
        qs = WorkItem.objects.filter(status=WorkItem.Status.DONE).select_related("release").order_by("-decided_at", "-pk")
    else:
        qs = WorkItem.objects.all()
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    last = service.backlog().count()
    for it in page:
        ev = service.evidence(it)
        it.requests, it.organisations, it.urgent = ev["requests"], ev["organisations"], ev["urgent"]
        it.is_last = it.position == last
    counts = {
        "backlog": last,
        "planned": WorkItem.objects.filter(status=WorkItem.Status.PLANNED).count(),
        "proposed": WorkItem.objects.filter(status=WorkItem.Status.PROPOSED).count(),
        "approval": service.awaiting_approval().count(),
        "declined": WorkItem.objects.filter(status=WorkItem.Status.DECLINED).count(),
        "done": WorkItem.objects.filter(status=WorkItem.Status.DONE).count(),
        "all": WorkItem.objects.count(),
    }
    return render(
        request,
        "work/backlog.html",
        {"page": page, "show": show, "shows": [(k, f"{w} ({counts[k]})") for k, w in SHOW], "work_query": f"show={show}", **_may(request)},
    )


def _request_for(request, pk) -> Feedback:
    """A request the signed-in person may join to a work item: one they could read on the staff's page."""
    fb = get_object_or_404(Feedback.objects.select_related("organisation"), pk=pk)
    if fb.kind == Feedback.Kind.SECURITY and not access.is_system_admin(request.user):
        raise Http404
    return fb


def _start(fb: Feedback) -> dict:
    """What a new work item starts with, from a request: the kind and the page. Its words are written by staff, not copied."""
    return {"kind": service.KIND_OF.get(fb.kind, WorkItem.Kind.PROBLEM), "pages": fb.page.split("?")[0]}


@login_required
def new(request):
    _require_staff(request)
    if not service.can_propose(request.user):
        raise Http404
    form = forms.ItemForm(request.POST or None, initial={"kind": WorkItem.Kind.PROBLEM})
    if request.method == "POST" and form.is_valid():
        try:
            it = service.propose(request.user, **form.cleaned_data)
        except (service.WorkRefused, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"{it.number} is proposed.")
            return redirect("work:item", it.pk)
    return render(request, "work/form.html", {"form": form, "item": None, "fb": None})


@login_required
def from_request(request, pk):
    """From a request: join it to a work item there already is, or propose a new one from it."""
    _require_staff(request)
    if not service.can_propose(request.user):
        raise Http404
    fb = _request_for(request, pk)
    joined = service.item_of(fb)
    join_form = forms.JoinForm(request.POST if request.POST.get("do") == "join" else None, auto_id="join_%s")
    form = forms.ItemForm(request.POST if request.POST.get("do") == "new" else None, initial=_start(fb))
    if request.method == "POST" and joined is None:
        try:
            if join_form.is_bound and join_form.is_valid():
                it = join_form.cleaned_data["item"]
                service.join(request.user, it, fb)
                messages.success(request, f"The request is joined to {it.number}.")
                return redirect("work:item", it.pk)
            if form.is_bound and form.is_valid():
                it = service.propose(request.user, request=fb, **form.cleaned_data)
                messages.success(request, f"{it.number} is proposed, with the request joined to it.")
                return redirect("work:item", it.pk)
        except (service.WorkRefused, AccessDenied) as e:
            (join_form if join_form.is_bound else form).add_error(None, str(e))
    return render(
        request,
        "work/from_request.html",
        {"fb": fb, "joined": joined, "join_form": join_form, "form": form, "any_items": join_form.fields["item"].queryset.exists()},
    )


@login_required
@require_POST
def unjoin(request, pk):
    _require_staff(request)
    link = get_object_or_404(RequestLink.objects.select_related("work_item"), pk=pk)
    try:
        service.unjoin(request.user, link)
    except (service.WorkRefused, AccessDenied) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "The request was taken off the work item.")
    return redirect("work:item", link.work_item_id)


def _item_page(request, it, **filled):
    ev = service.evidence(it)
    sysadmin = access.is_system_admin(request.user)
    reads = requests.can_answer(request.user)
    for link in ev["links"]:
        link.hidden = link.kind == Feedback.Kind.SECURITY and not sysadmin  # a security concern names no page to anyone else
        link.open = reads and not link.hidden and link.feedback_id is not None
        link.kind_words = dict(Feedback.Kind.choices).get(link.kind, link.kind)
    f = {
        "decline_form": forms.ReasonForm(
            auto_id="decline_%s", label="Reason for declining", help_text="The people whose requests are joined to it are told this reason."
        ),
        "turn_down_form": forms.ReasonForm(auto_id="turn_down_%s", label="Reason", help_text="For the backlog manager who proposed it."),
        "turn_down_public_form": forms.ReasonForm(
            auto_id="turn_down_public_%s", label="Reason", help_text="For the backlog manager who proposed it."
        ),
        "internal_form": forms.ReasonForm(auto_id="internal_%s", label="Reason for taking it out of public view"),
        "reconsider_form": forms.ReasonForm(auto_id="reconsider_%s", label="Reason for reconsidering it"),
        "test_form": forms.TestForm(auto_id="test_%s"),
        "issue_form": forms.IssueForm(auto_id="issue_%s", initial={"issue_url": it.issue_url}),
        "post_form": forms.PostIssueForm(auto_id="post_%s"),
    }
    f.update(filled)
    tests = list(it.tests.all())
    in_scripts = service.script_tests(tests)
    for t in tests:
        t.case = in_scripts.get(t.pk)
    return render(
        request,
        "work/item.html",
        {
            "item": it,
            "evidence": ev,
            "sensitive": service.is_sensitive(it),
            "backlog_size": service.backlog().count(),
            "tests": tests,
            "testing_on": service.testing_on(),
            "lacking": service.lacking(it),
            "issue_link": service.issue_link(it),
            "issue_blocked": service.issue_blocked(it),
            "can_post_issue": service.can_post_issue() and not it.issue_url and not service.issue_blocked(it),
            "issue_draft": service.issue_text(it),
            "development": service.development(it),
            **f,
            **_may(request),
        },
    )


def _get(pk) -> WorkItem:
    return get_object_or_404(
        WorkItem.objects.select_related("created_by", "decided_by", "decline_proposed_by", "public_proposed_by", "release"), pk=pk
    )


@login_required
def item(request, pk):
    _require_staff(request)
    return _item_page(request, _get(pk))


@login_required
def edit(request, pk):
    _require_staff(request)
    if not service.can_manage(request.user):
        raise Http404
    it = _get(pk)
    form = forms.ItemForm(request.POST or None, instance=it)
    if request.method == "POST" and form.is_valid():
        try:
            service.edit(request.user, it, **form.cleaned_data)
        except (service.WorkRefused, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"{it.number} was changed.")
            return redirect("work:item", it.pk)
    return render(request, "work/form.html", {"form": form, "item": it, "fb": None})


REASONED = {  # the steps that take a reason: the form's name on the page, and what to say when done
    "decline": ("decline", "Reason for declining"),
    "turn_down": ("turn_down", "Reason"),
    "internal": ("internal", "Reason for taking it out of public view"),
    "reconsider": ("reconsider", "Reason for reconsidering it"),
}


@login_required
@require_POST
def act(request, pk):
    """Every step on a work item: accept, move, decline, make public or internal, approve or turn down a proposal, reconsider."""
    _require_staff(request)
    it = _get(pk)
    do = request.POST.get("do", "")
    back = request.POST.get("back") == "backlog"
    reason_form = None
    try:
        if do in REASONED:
            name, label = REASONED[do]
            if do == "turn_down" and request.POST.get("what") == "public":
                name = "turn_down_public"
            reason_form = forms.ReasonForm(request.POST, auto_id=f"{name}_%s", label=label)
            if not reason_form.is_valid():
                return _item_page(request, it, **{f"{name}_form": reason_form})
            reason = reason_form.cleaned_data["reason"]
            if do == "decline":
                it = service.decline(request.user, it, reason)
                done = "Declined." if it.status == WorkItem.Status.DECLINED else "Declining it now waits for the CPUC's approval."
            elif do == "turn_down":
                service.turn_down(request.user, it, request.POST.get("what", ""), reason)
                done = "Not approved. The backlog manager who proposed it has been told."
            elif do == "internal":
                service.make_internal(request.user, it, reason)
                done = "It is no longer public."
            else:
                service.reconsider(request.user, it, reason)
                done = "It is proposed again."
        elif do == "accept":
            it = service.accept(request.user, it)
            done = f"Accepted into the backlog at position {it.position}. Move it to its place."
        elif do == "move":
            it = service.move(request.user, it, request.POST.get("to", ""))
            done = f"{it.number} is now at position {it.position}."
        elif do == "public":
            it = service.make_public(request.user, it)
            done = "It is public." if it.public else "Making it public now waits for the CPUC's approval."
        elif do == "approve":
            what = request.POST.get("what", "")
            service.approve(request.user, it, what)
            done = "Approved: it is declined." if what == "decline" else "Approved: it is public."
        elif do == "post_issue":
            post_form = forms.PostIssueForm(request.POST, auto_id="post_%s")
            if not post_form.is_valid():
                return _item_page(request, it, post_form=post_form)
            it = service.post_issue(request.user, it)
            done = "The issue is posted to the developers' tracker, and its address is kept here."
        elif do == "issue":
            issue_form = forms.IssueForm(request.POST, auto_id="issue_%s")
            if not issue_form.is_valid():
                return _item_page(request, it, issue_form=issue_form)
            it = service.record_issue(request.user, it, issue_form.cleaned_data["issue_url"])
            done = "The issue's address is kept." if it.issue_url else "The issue was taken off."
        else:
            raise Http404
    except (service.WorkRefused, AccessDenied) as e:
        if reason_form is not None:
            reason_form.add_error("reason", str(e))
            return _item_page(request, it, **{f"{name}_form": reason_form})
        messages.error(request, str(e))
    else:
        messages.success(request, done)
    if back:
        return redirect("work:backlog")
    return redirect("work:item", pk)


# ---- acceptance tests


@login_required
@require_POST
def tests(request, pk):
    """Add an acceptance test to a work item, or remove one."""
    _require_staff(request)
    it = _get(pk)
    try:
        if "remove" in request.POST:
            service.remove_test(request.user, get_object_or_404(AcceptanceTest, pk=request.POST["remove"], work_item=it))
            messages.success(request, "The acceptance test was removed.")
        else:
            form = forms.TestForm(request.POST, auto_id="test_%s")
            if not form.is_valid():
                return _item_page(request, it, test_form=form)
            service.add_test(request.user, it, **form.cleaned_data)
            messages.success(request, "The acceptance test was added.")
    except (service.WorkRefused, AccessDenied) as e:
        messages.error(request, str(e))
    return redirect("work:item", pk)


# ---- release plans


@login_required
def releases(request):
    _require_staff(request)
    form = forms.PlanForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            plan = service.create_plan(request.user, **form.cleaned_data)
        except (service.WorkRefused, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"The plan for release {plan.name} is started. Plan work items for it from the backlog.")
            return redirect("work:release", plan.pk)
    plans = list(ReleasePlan.objects.all())
    for plan in plans:
        plan.count = plan.items.count()
    return render(request, "work/releases.html", {"plans": plans, "form": form, **_may(request)})


def _ready() -> tuple[list, list]:
    """The backlog's items that can be planned now, and those that still lack something."""
    items = service.plannable()
    return [it for it in items if not it.lacking and not it.decline_proposed_by_id], [it for it in items if it.lacking]


def _release_page(request, plan, **filled):
    ready, not_ready = _ready()
    f = {
        "add_form": forms.PlanItemForm(ready=ready, auto_id="add_%s"),
        "details_form": forms.PlanForm(instance=plan, auto_id="details_%s"),
        "back_form": forms.ReasonForm(auto_id="back_%s", label="Reason", help_text="For the backlog manager who proposed it."),
        "go_form": forms.GoForm(auto_id="go_%s", initial={"released_on": timezone.localdate()}),
        "hold_form": forms.ReasonForm(auto_id="hold_%s", label="Reason it does not go yet", help_text="The backlog managers are told."),
        "round_form": forms.RoundForm(
            auto_id="round_%s", initial={"name": f"Release {plan.name} acceptance test", "starts_on": timezone.localdate()}
        ),
    }
    f.update(filled)
    return render(
        request,
        "work/release.html",
        {
            "plan": plan,
            "items": list(plan.items.order_by("pk")),
            "unbuilt": service.unbuilt(plan),
            "testing": service.testing_of(plan),
            "can_test": service.can_test(request.user),
            "round_blocked": service.round_blocked(plan),
            "ready": ready,
            "not_ready": not_ready[:10],
            **f,
            **_may(request),
        },
    )


def _plan(pk) -> ReleasePlan:
    return get_object_or_404(
        ReleasePlan.objects.select_related("created_by", "proposed_by", "approved_by", "held_by", "released_by", "campaign__version"),
        pk=pk,
    )


@login_required
def release(request, pk):
    _require_staff(request)
    return _release_page(request, _plan(pk))


@login_required
@require_POST
def release_act(request, pk):
    """Every step on a release plan: add or take out a work item, change its details, propose, approve, send back, mark an
    item built, open a round of testing for its acceptance tests, and the CPUC's decision on the built release (go or no-go)."""
    _require_staff(request)
    plan = _plan(pk)
    do = request.POST.get("do", "")
    form, name = None, ""
    try:
        if do == "add":
            form, name = forms.PlanItemForm(request.POST, ready=_ready()[0], auto_id="add_%s"), "add_form"
            if not form.is_valid():
                return _release_page(request, plan, add_form=form)
            it = service.plan_item(request.user, form.cleaned_data["item"], plan)
            done = f"{it.number} is planned for release {plan.name}."
        elif do == "remove":
            it = service.unplan_item(request.user, get_object_or_404(WorkItem, pk=request.POST.get("item"), release=plan))
            done = f"{it.number} is back at the top of the backlog."
        elif do == "details":
            form, name = forms.PlanForm(request.POST, instance=plan, auto_id="details_%s"), "details_form"
            if not form.is_valid():
                return _release_page(request, _plan(pk), details_form=form)
            service.change_plan(request.user, _plan(pk), **form.cleaned_data)
            done = "The release plan was changed."
        elif do == "propose":
            plan = service.propose_plan(request.user, plan)
            done = "The plan is approved." if plan.status == ReleasePlan.Status.APPROVED else "The plan now waits for the CPUC's approval."
        elif do == "approve":
            service.approve_plan(request.user, plan)
            done = "The plan is approved."
        elif do == "send_back":
            form, name = forms.ReasonForm(request.POST, auto_id="back_%s", label="Reason"), "back_form"
            if not form.is_valid():
                return _release_page(request, plan, back_form=form)
            service.send_plan_back(request.user, plan, form.cleaned_data["reason"])
            done = "Not approved. The backlog manager who proposed it has been told."
        elif do in ("built", "not_built"):
            it = service.mark_built(request.user, get_object_or_404(WorkItem, pk=request.POST.get("item"), release=plan), do == "built")
            done = f"{it.number} is marked built." if do == "built" else f"{it.number} is no longer marked built."
        elif do == "round":
            form, name = forms.RoundForm(request.POST, auto_id="round_%s"), "round_form"
            if not form.is_valid():
                return _release_page(request, plan, round_form=form)
            round_ = service.open_round(request.user, plan, **form.cleaned_data)
            messages.success(request, f"The round of testing {round_.name} is open. Assign its scripts to testers here.")
            return redirect("testing:campaign", round_.pk)
        elif do == "go":
            form, name = forms.GoForm(request.POST, auto_id="go_%s"), "go_form"
            if not form.is_valid():
                return _release_page(request, plan, go_form=form)
            service.release_goes(request.user, plan, form.cleaned_data["released_on"])
            done = f"Release {plan.name} is released. Its work items are done, and the people waiting on them have been told."
        elif do == "no_go":
            form, name = forms.ReasonForm(request.POST, auto_id="hold_%s", label="Reason it does not go yet"), "hold_form"
            if not form.is_valid():
                return _release_page(request, plan, hold_form=form)
            service.release_held(request.user, plan, form.cleaned_data["reason"])
            done = "The release does not go yet. The backlog managers have been told."
        else:
            raise Http404
    except (service.WorkRefused, AccessDenied) as e:
        if form is not None:
            form.add_error(None, str(e))
            return _release_page(request, _plan(pk), **{name: form})
        messages.error(request, str(e))
    else:
        messages.success(request, done)
    return redirect("work:release", pk)


# ---- measures


@login_required
def measures_page(request):
    """How the loop from a request to a release is running, for everyone who works on it: counts read from the records."""
    _require_staff(request)
    try:
        days = int(request.GET.get("days", measures.DEFAULT_PERIOD))
    except ValueError:
        days = measures.DEFAULT_PERIOD
    if days not in dict(measures.PERIODS):
        days = measures.DEFAULT_PERIOD
    return render(
        request,
        "work/measures.html",
        {
            "days": days,
            "periods": measures.PERIODS,
            "period_words": dict(measures.PERIODS)[days],
            "can_test": service.can_test(request.user),
            **measures.page(days),
        },
    )


# ---- signed events from the developers' tracker


@csrf_exempt  # not a form of this site: the tracker's signature over the body is what lets an event in
@require_POST
def tracker_event(request):
    """Take one signed event from the developers' tracker (work/tracker.py). Not found where no secret is set; refused
    without a good signature; an event for another repository is acknowledged and dropped."""
    if not tracker.enabled():
        raise Http404
    if not tracker.signed(request.body, request.headers.get("X-Hub-Signature-256", "")):
        return JsonResponse({"error": "The signature is missing or wrong."}, status=403)
    try:
        payload = json.loads(request.body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return JsonResponse({"error": "The body is not a JSON object."}, status=400)
    if not tracker.ours(payload):
        return JsonResponse({"done": "Not this site's repository: nothing changed."}, status=202)
    return JsonResponse({"done": tracker.take(request.headers.get("X-GitHub-Event", ""), payload)})


# ---- what anyone may read


def known(request):
    """Known problems: the public problems, each with its workaround and where it stands."""
    return render(request, "work/known.html", {"items": service.known_problems()})


def upcoming(request):
    """Upcoming changes: the public items by the approved release each is planned for, then those not yet scheduled."""
    return render(request, "work/upcoming.html", service.upcoming())


def _public(pk) -> WorkItem:
    return get_object_or_404(service.public_items(), pk=pk)


def public_item(request, pk):
    it = _public(pk)
    return render(
        request,
        "work/public_item.html",
        {
            "item": it,
            "progress": service.progress(it),
            "following": service.follows(request.user, it),
            "followers": it.follows.count(),
            "can_see": service.can_see(request.user),
        },
    )


@login_required
@require_POST
def follow(request, pk):
    it = _public(pk)
    try:
        if request.POST.get("do") == "unfollow":
            service.unfollow(request.user, it)
            messages.success(request, "You no longer follow it.")
        else:
            organisation = getattr(request, "organisation", None)
            service.follow(request.user, it, organisation)
            messages.success(request, "Noted. You will be told when it is planned for a release, or if the CPUC decides not to do it.")
    except (service.WorkRefused, AccessDenied) as e:
        messages.error(request, str(e))
    return redirect("known:item", it.pk)

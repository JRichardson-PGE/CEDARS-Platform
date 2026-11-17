from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from cedars.core.errors import csrf_failure
from cedars.services.accounts import service as access
from cedars.services.accounts.service import AccessDenied
from cedars.services.feedback import service as requests

from . import forms, service
from .models import Assignment, Campaign, Result, ScriptVersion, TestCase

COOKIE_DAYS = 120
PER_PAGE = 50
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _require_on():
    if not service.enabled():
        raise Http404


def _require_lead(request):
    if not access.is_system_admin(request.user):
        raise Http404


# ---- the test lead's pages


@login_required
def manage(request):
    """Rounds of testing and the loaded scripts. Where testing is off, the page says so and offers nothing."""
    _require_lead(request)
    if not service.enabled():
        return render(request, "testing/manage.html", {"on": False})
    form = forms.CampaignForm(request.POST or None, initial={"starts_on": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        try:
            c = service.create_campaign(request.user, **form.cleaned_data)
        except (service.TestingRefused, AccessDenied) as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"{c.name} is open. Assign its scripts to testers below.")
            return redirect("testing:campaign", c.pk)
    campaigns = []
    for c in Campaign.objects.select_related("version"):
        live = c.assignments.filter(withdrawn_at__isnull=True)
        campaigns.append({"campaign": c, "testers": live.values("tester").distinct().count(), "assignments": live.count()})
    versions = [
        {"version": v, "scripts": v.scripts.count(), "tests": TestCase.objects.filter(script__version=v).count()}
        for v in ScriptVersion.objects.select_related("loaded_by")
    ]
    return render(request, "testing/manage.html", {"on": True, "form": form, "campaigns": campaigns, "versions": versions})


@login_required
def campaign(request, pk):
    _require_on()
    _require_lead(request)
    c = get_object_or_404(Campaign.objects.select_related("version", "created_by", "closed_by"), pk=pk)
    return _campaign_page(request, c)


def _campaign_page(request, c, assign_form=None, status=200):
    rows = []
    assignments = list(c.assignments.select_related("script", "tester", "withdrawn_by"))
    for script in c.version.scripts.all():
        mine = [a for a in assignments if a.script_id == script.pk]
        rows.append(
            {
                "script": script,
                "tests": script.tests.count(),
                "assignments": [{"a": a, "progress": service.progress(a)} for a in mine if not a.withdrawn_at],
                "withdrawn": [a for a in mine if a.withdrawn_at],
            }
        )
    return render(
        request,
        "testing/campaign.html",
        {
            "campaign": c,
            "rows": rows,
            "assign_form": assign_form or forms.AssignForm(c, auto_id="assign_%s"),
            "close_form": forms.OptionalReasonForm(auto_id="close_%s"),
            "reopen_form": forms.ReasonForm(auto_id="reopen_%s"),
        },
        status=status,
    )


@login_required
def results(request, pk):
    """A round's results for the test lead: the counts by script, then the results of one kind (failed and blocked first), which
    script they are in, who recorded them and what they said."""
    _require_on()
    _require_lead(request)
    c = get_object_or_404(Campaign.objects.select_related("version"), pk=pk)
    show = request.GET.get("show") if request.GET.get("show") in service.SHOW else "attention"
    scripts = list(c.version.scripts.all())
    script = next((s for s in scripts if str(s.pk) == request.GET.get("script")), None)
    page = Paginator(service.results(c, service.SHOW[show][1], script), PER_PAGE).get_page(request.GET.get("page"))
    reads = requests.can_answer(request.user)
    for r in page:
        r.notes = [(i + 1, note) for i, note in enumerate(r.step_notes or []) if note]
        finding = getattr(r, "finding", None)
        r.request = finding.feedback if finding else None
        r.request_open = reads and r.request is not None
    query = "&".join(part for part in (f"show={show}", f"script={script.pk}" if script else "") if part)
    return render(
        request,
        "testing/results.html",
        {
            "campaign": c,
            "summary": service.summary(c),
            "show": show,
            "shows": [(key, words) for key, (words, _) in service.SHOW.items()],
            "showing": service.SHOW[show][0].lower(),
            "scripts": scripts,
            "script": script,
            "page": page,
            "query": query,
        },
    )


@login_required
def results_download(request, pk):
    _require_on()
    _require_lead(request)
    c = get_object_or_404(Campaign.objects.select_related("version"), pk=pk)
    response = HttpResponse(service.results_workbook(request.user, c), content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="testing-results-{slugify(c.name) or c.pk}.xlsx"'
    return response


@login_required
@require_POST
def assign(request, pk):
    _require_on()
    _require_lead(request)
    c = get_object_or_404(Campaign, pk=pk)
    form = forms.AssignForm(c, request.POST, auto_id="assign_%s")
    if form.is_valid():
        tester = get_user_model().objects.filter(email__iexact=form.cleaned_data["email"].strip()).first()
        if tester is None:
            form.add_error("email", "No CEDARS account has that address. The tester signs up first; then assign the script.")
        else:
            try:
                service.assign(request.user, c, form.cleaned_data["script"], tester)
            except (service.TestingRefused, AccessDenied) as e:
                form.add_error(None, str(e))
            else:
                messages.success(request, f"{form.cleaned_data['script']} assigned to {tester.name or tester.email}. They have been told.")
                return redirect("testing:campaign", c.pk)
    return _campaign_page(request, c, assign_form=form)


def _campaign_action(request, pk, action, form_class, done):
    _require_on()
    _require_lead(request)
    c = get_object_or_404(Campaign, pk=pk)
    f = form_class(request.POST)
    try:
        action(request.user, c, f.cleaned_data["reason"] if f.is_valid() else "")
    except (service.TestingRefused, AccessDenied) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, done.format(name=c.name))
    return redirect("testing:campaign", c.pk)


@login_required
@require_POST
def close(request, pk):
    return _campaign_action(
        request, pk, service.close_campaign, forms.OptionalReasonForm, "{name} is closed. Its results are kept as they stand."
    )


@login_required
@require_POST
def reopen(request, pk):
    return _campaign_action(request, pk, service.reopen_campaign, forms.ReasonForm, "{name} is open again.")


@login_required
@require_POST
def withdraw(request, pk):
    _require_on()
    _require_lead(request)
    a = get_object_or_404(Assignment.objects.select_related("campaign", "script", "tester"), pk=pk)
    f = forms.ReasonForm(request.POST)
    try:
        service.withdraw(request.user, a, f.cleaned_data["reason"] if f.is_valid() else "")
    except (service.TestingRefused, AccessDenied) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{a.script} is withdrawn from {a.tester.name or a.tester.email}. Their results so far are kept.")
    return redirect("testing:campaign", a.campaign_id)


# ---- the tester's pages


@login_required
def mine(request):
    _require_on()
    rows = [{"a": a, "progress": service.progress(a)} for a in service.mine(request.user)]
    return render(request, "testing/mine.html", {"rows": rows})


def _keep_link(request, response, a):
    """Leave the run's link in this browser, so the run stays open while the site is signed in as a test account or signed out."""
    response.set_cookie(
        service.COOKIE,
        service.with_link(request.COOKIES.get(service.COOKIE, ""), a),
        max_age=COOKIE_DAYS * 86400,
        path="/testing/",
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="Lax",
    )
    return response


def _run(request, pk):
    """The assignment behind a run page and whether the tester is signed in as themselves, or None when this browser may not open it:
    it opens for the tester's own sign-in, or for a browser that holds the run's link."""
    a = (
        Assignment.objects.select_related("campaign", "campaign__version", "script", "tester")
        .filter(pk=pk, withdrawn_at__isnull=True)
        .first()
    )
    if a is None:
        return None, False
    own = request.user.is_authenticated and request.user.pk == a.tester_id
    if own or service.holds_link(request.COOKIES.get(service.COOKIE, ""), a):
        return a, own
    return None, False


def _from_this_site(request) -> bool:
    """Whether a form was sent from one of this site's own pages (its Origin, or its Referer where a browser sends no Origin).
    The forms of a test take this in place of the usual form token: that token changes with every sign-in and sign-out, and a
    tester signs in and out of the site in another tab between opening a test and saving its result. What lets a browser save is
    the run's cookie or the tester's own session, and browsers send neither with a form posted from another site (SameSite=Lax)."""
    origin = request.headers.get("Origin", "")
    if not origin:
        referer = request.headers.get("Referer", "")
        if not referer:
            return False
        origin = referer
    return urlsplit(origin).netloc == request.get_host()


def _not_yours(request):
    return render(request, "testing/not_yours.html", status=403)


def open_link(request, token):
    """A run link: keeps the link in this browser and goes to the run."""
    _require_on()
    a = service.assignment_of(token)
    if a is None:
        return _not_yours(request)
    return _keep_link(request, redirect("testing:run", a.pk), a)


def run(request, pk):
    _require_on()
    a, own = _run(request, pk)
    if a is None:
        return _not_yours(request)
    results = {r.test_id: r for r in Result.objects.filter(assignment=a)}
    drafted = service.drafted(a)
    sections: list[dict] = []
    for t in a.script.tests.all():
        if not sections or sections[-1]["title"] != t.section:
            sections.append({"title": t.section, "tests": []})
        sections[-1]["tests"].append({"test": t, "result": results.get(t.pk), "draft": t.pk in drafted})
    how = a.campaign.version.about.get("how")
    response = render(
        request,
        "testing/run.html",
        {
            "a": a,
            "own": own,
            "progress": service.progress(a),
            "sections": sections,
            "next": service.next_test(a),
            "how": how if isinstance(how, list) and all(isinstance(h, str) for h in how) else [],
            "link": request.build_absolute_uri(f"/testing/open/{service.run_token(a)}/") if own else "",
        },
    )
    return _keep_link(request, response, a) if own else response


@login_required
@require_POST
def new_link(request, pk):
    _require_on()
    a = get_object_or_404(Assignment.objects.select_related("campaign"), pk=pk, tester=request.user, withdrawn_at__isnull=True)
    a = service.new_link(request.user, a)
    messages.success(request, "Your run link was replaced. The old link no longer opens this run in any browser.")
    return _keep_link(request, redirect("testing:run", a.pk), a)


@csrf_exempt  # see _from_this_site
def test(request, pk, test_id):
    _require_on()
    if request.method == "POST" and not _from_this_site(request):
        return csrf_failure(request)
    a, own = _run(request, pk)
    if a is None:
        return _not_yours(request)
    tests = list(a.script.tests.all())
    t = next((x for x in tests if x.test_id == test_id), None)
    if t is None:
        raise Http404
    at = tests.index(t)
    result = Result.objects.filter(assignment=a, test=t).first()
    if request.method == "POST" and "discard" in request.POST:
        service.discard_draft(a, t)
        messages.success(request, f"Your unsaved changes to {t.test_id} were discarded.")
        return redirect("testing:test", a.pk, t.test_id)
    # what the tester typed and did not save comes back in the form; the saved result stays as it is until they save
    unsaved = service.draft_of(a, t) if a.campaign.is_open else None
    shown = unsaved or result
    form = forms.ResultForm(request.POST or None, initial={"outcome": shown.outcome, "comment": shown.comment} if shown else None)
    notes = (shown.step_notes if shown else []) or []
    if request.method == "POST":
        notes = [request.POST.get(f"note_{i + 1}", "") for i in range(len(t.steps))]
        if form.is_valid():
            try:
                service.record(a, t, outcome=form.cleaned_data["outcome"], comment=form.cleaned_data["comment"], step_notes=notes)
            except service.TestingRefused as e:
                form.add_error("comment" if "what happened" in str(e).lower() else None, str(e))
            else:
                if "next" in request.POST:
                    following = service.next_test(a, after=t)
                    if following is None:
                        messages.success(request, f"{t.test_id} saved. Every test in this script now has a result. Thank you.")
                        return redirect("testing:run", a.pk)
                    messages.success(request, f"{t.test_id} saved.")
                    return redirect("testing:test", a.pk, following.test_id)
                messages.success(request, f"{t.test_id} saved.")
                return redirect("testing:test", a.pk, t.test_id)
    notes = list(notes) + [""] * (len(t.steps) - len(notes))
    response = render(
        request,
        "testing/test.html",
        {
            "a": a,
            "t": t,
            "form": form,
            "result": result,
            "finding": service.finding_of(result) if result else None,
            "own": own,
            "unsaved": unsaved,
            "steps": [{"n": i + 1, "text": s, "note": notes[i]} for i, s in enumerate(t.steps)],
            "site": a.campaign.version.site,
            "signed_out": service.is_signed_out_test(t),
            "progress": service.progress(a),
            "position": at + 1,
            "count": len(tests),
            "previous": tests[at - 1] if at else None,
            "following": tests[at + 1] if at + 1 < len(tests) else None,
            "can_save": a.campaign.is_open,
        },
    )
    return _keep_link(request, response, a) if own else response


@csrf_exempt  # see _from_this_site
@require_POST
def draft(request, pk, test_id):
    """Keeps what the tester has typed so far (the test page's script sends the form as they type). Answers in JSON; nothing here
    is a result until the tester saves it."""
    _require_on()
    a, _ = _run(request, pk)
    if a is None or not _from_this_site(request):
        return JsonResponse({"saved": False, "why": "This run is not open in this browser."}, status=403)
    t = a.script.tests.filter(test_id=test_id).first()
    if t is None:
        raise Http404
    try:
        d = service.save_draft(
            a,
            t,
            outcome=request.POST.get("outcome", ""),
            comment=request.POST.get("comment", ""),
            step_notes=[request.POST.get(f"note_{i + 1}", "") for i in range(len(t.steps))],
        )
    except service.TestingRefused as e:
        return JsonResponse({"saved": False, "why": str(e)}, status=409)
    return JsonResponse({"saved": True, "draft": d is not None, "at": timezone.localtime().strftime("%-I:%M %p")})

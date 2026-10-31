from datetime import datetime, time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from cedars.core import health
from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service, tokens
from cedars.services.accounts.middleware import ORG_SESSION_KEY
from cedars.services.accounts.models import (
    AccessPolicy,
    AccessRequest,
    ApiToken,
    Grant,
    Membership,
    Organisation,
    PolicyChange,
    RecertificationItem,
    User,
)
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent

from . import forms


def _end_of_day(d):
    return timezone.make_aware(datetime.combine(d, time(23, 59, 59))) if d else None


def _done(request, ok_message, to):
    messages.success(request, ok_message)
    return redirect(to)


def _refused(request, e: AccessDenied, to):
    messages.error(request, str(e))
    return redirect(to)


def _org_or_404(request, code) -> Organisation:
    org = get_object_or_404(Organisation, code=code)
    if not (service.is_org_admin(request.user, org) or service.is_system_admin(request.user)):
        raise Http404
    return org


# --- the user's own account


@login_required
def account(request):
    user = request.user
    memberships = Membership.objects.filter(user=user, ended_at__isnull=True).select_related("organisation")
    rows = []
    for m in memberships:
        grants = m.grants.filter(status__in=Grant.OPEN).order_by("permission")
        rows.append((m, [(g, perms.label(g.permission)) for g in grants]))
    return render(
        request,
        "administration/account.html",
        {
            "rows": rows,
            "requests": AccessRequest.objects.filter(user=user).select_related("organisation")[:20],
            "assurance": request.assurance,
            "tokens": ApiToken.objects.filter(user=user).select_related("organisation")[:50],
            "token_form": forms.ApiTokenForm(organisations=request.user_organisations, max_days=tokens.max_days()),
        },
    )


@login_required
@require_POST
def token_create(request):
    """Make an API token and show its key once, on this response only (never stored, never put in a message or a redirect)."""
    form = forms.ApiTokenForm(request.POST, organisations=request.user_organisations, max_days=tokens.max_days())
    if not form.is_valid():
        messages.error(request, "Check the token form: " + "; ".join(f"{k}: {' '.join(v)}" for k, v in form.errors.items()))
        return redirect("administration:account")
    d = form.cleaned_data
    org = next((o for o in request.user_organisations if o.code == d["organisation"]), None)
    try:
        token, key = tokens.create_token(request.user, org, name=d["name"], scopes=d["scopes"], days=d["days"])
    except AccessDenied as e:
        return _refused(request, e, "administration:account")
    response = render(request, "administration/token_created.html", {"token": token, "key": key})
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def token_revoke(request, pk):
    token = get_object_or_404(ApiToken, pk=pk)
    from_org = request.POST.get("from") == "organisation" and token.organisation is not None
    back = f"/admin/organisations/{token.organisation.code}/" if from_org else "administration:account"
    if not tokens.can_revoke(request.user, token):
        raise Http404
    try:
        tokens.revoke_token(request.user, token, request.POST.get("reason", ""))
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, f"Revoked the token {token}.", back)


@login_required
@require_POST
def switch_organisation(request):
    code = request.POST.get("organisation", "")
    org = next((o for o in request.user_organisations if o.code == code), None)
    if org is None:
        messages.error(request, "You are not a member of that organisation.")
    else:
        request.session[ORG_SESSION_KEY] = org.pk
        messages.success(request, f"You are now acting for {org.code}.")
    nxt = request.POST.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "administration:account")


@login_required
def request_access(request):
    form = forms.RequestAccessForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            service.request_access(
                request.user, form.cleaned_data["organisation"], form.cleaned_data["permissions"], form.cleaned_data["message"]
            )
        except AccessDenied as e:
            return _refused(request, e, "administration:request_access")
        return _done(request, "Your request was sent to the organisation's administrators.", "administration:account")
    return render(request, "administration/request_access.html", {"form": form})


@login_required
@require_POST
def withdraw_request(request, pk):
    req = get_object_or_404(AccessRequest, pk=pk, user=request.user)
    try:
        service.withdraw_request(request.user, req)
    except AccessDenied as e:
        return _refused(request, e, "administration:account")
    return _done(request, "Request withdrawn.", "administration:account")


# --- organisations (PA admins and CPUC system administrators)


@login_required
def organisations(request):
    sysadmin = service.is_system_admin(request.user)
    orgs = Organisation.objects.all() if sysadmin else [o for o in request.user_organisations if service.is_org_admin(request.user, o)]
    if not sysadmin and not orgs:
        raise Http404
    rows = []
    for org in orgs:
        campaign = org.recertifications.filter(closed_at__isnull=True).first()
        last = org.recertifications.filter(closed_at__isnull=False).first()
        rows.append(
            {
                "org": org,
                "admins": service.org_admins(org).count(),
                "members": org.memberships.filter(ended_at__isnull=True).count(),
                "campaign": campaign,
                "undecided": campaign.items.filter(decision="").count() if campaign else 0,
                "last": last,
            }
        )
    return render(request, "administration/organisations.html", {"rows": rows, "sysadmin": sysadmin})


@login_required
def organisation_new(request):
    if not service.is_system_admin(request.user):
        raise Http404
    form = forms.OrganisationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            org = service.create_organisation(
                request.user, code=d["code"], name=d["name"], kind=d["kind"], pa_type=d["pa_type"], federated=d["federated"]
            )
        except AccessDenied as e:
            return _refused(request, e, "administration:organisations")
        return _done(request, f"{org.code} added. Assign its first administrator below.", f"/admin/organisations/{org.code}/")
    return render(request, "administration/organisation_new.html", {"form": form})


def _grantable_codes(user, org) -> list[str]:
    if not service.org_admins(org).exists():
        return [perms.ORG_ADMIN] if service.is_system_admin(user) else []
    codes = perms.module_permissions() + [perms.ORG_ADMIN, perms.PII]
    if org.kind == Organisation.Kind.CPUC:
        if service.is_system_admin(user):
            codes += list(perms.CPUC_ONLY)
        codes = [c for c in codes if c != perms.PII]
    return codes if service.is_org_admin(user, org) or (org.kind == "cpuc" and service.is_system_admin(user)) else []


@login_required
def organisation(request, code):
    org = _org_or_404(request, code)
    is_admin = service.is_org_admin(request.user, org)
    codes = _grantable_codes(request.user, org)
    memberships = org.memberships.filter(ended_at__isnull=True).select_related("user").order_by("user__email")
    members = []
    for m in memberships:
        grants = m.grants.filter(status__in=Grant.OPEN).order_by("permission")
        members.append((m, [(g, perms.label(g.permission)) for g in grants]))
    campaign = org.recertifications.filter(closed_at__isnull=True).first()
    items = []
    if campaign:
        items = [
            (i, perms.label(i.grant.permission)) for i in campaign.items.select_related("grant__membership__user").order_by("decision")
        ]
    return render(
        request,
        "administration/organisation.html",
        {
            "org": org,
            "is_admin": is_admin,
            "members": members,
            "grant_form": forms.GrantForm(codes=codes) if codes else None,
            "requests": [
                (r, [(c, perms.label(c)) for c in r.permissions])
                for r in org.access_requests.filter(status="pending").select_related("user")
            ],
            "campaign": campaign,
            "items": items,
            "recert_form": forms.RecertificationForm(),
            "no_admin": not service.org_admins(org).exists(),
            "tokens": ApiToken.objects.filter(organisation=org, revoked_at__isnull=True, expires_at__gt=timezone.now()).select_related(
                "user"
            )
            if is_admin
            else [],
        },
    )


@login_required
@require_POST
def grant(request, code):
    org = _org_or_404(request, code)
    back = f"/admin/organisations/{org.code}/"
    form = forms.GrantForm(request.POST, codes=_grantable_codes(request.user, org))
    if not form.is_valid():
        messages.error(request, "Check the grant form: " + "; ".join(f"{k}: {' '.join(v)}" for k, v in form.errors.items()))
        return redirect(back)
    d = form.cleaned_data
    user = User.objects.filter(email__iexact=d["email"]).first()
    if user is None:
        messages.error(request, f"There is no account for {d['email']}. Ask them to create one, then grant access.")
        return redirect(back)
    try:
        g = service.grant(request.user, user, org, d["permission"], expires_at=_end_of_day(d["expires"]), reason=d["reason"])
    except AccessDenied as e:
        return _refused(request, e, back)
    if g.status == Grant.Status.PENDING:
        return _done(request, f"{perms.label(g.permission)} for {user.email} is waiting for a data steward's approval.", back)
    return _done(request, f"Granted {perms.label(g.permission)} to {user.email}.", back)


@login_required
@require_POST
def revoke(request, pk):
    g = get_object_or_404(Grant.objects.select_related("membership__organisation", "membership__user"), pk=pk)
    back = request.POST.get("next") or f"/admin/organisations/{g.organisation.code}/"
    form = forms.ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give a reason for revoking the permission.")
        return redirect(back)
    try:
        service.revoke(request.user, g, form.cleaned_data["reason"])
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, f"Revoked {perms.label(g.permission)} from {g.user.email}.", back)


@login_required
@require_POST
def reinstate(request, pk):
    g = get_object_or_404(Grant, pk=pk)
    back = f"/admin/organisations/{g.organisation.code}/"
    try:
        service.reinstate(request.user, g, request.POST.get("reason", "certified late"))
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, f"Reinstated {perms.label(g.permission)} for {g.user.email}.", back)


@login_required
@require_POST
def end_membership(request, pk):
    m = get_object_or_404(Membership.objects.select_related("organisation", "user"), pk=pk)
    back = f"/admin/organisations/{m.organisation.code}/"
    form = forms.ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give a reason for removing the member.")
        return redirect(back)
    try:
        service.end_membership(request.user, m, form.cleaned_data["reason"])
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, f"{m.user.email} is no longer a member of {m.organisation.code}.", back)


@login_required
@require_POST
def decide_request(request, pk):
    req = get_object_or_404(AccessRequest.objects.select_related("organisation", "user"), pk=pk)
    back = f"/admin/organisations/{req.organisation.code}/"
    approve = request.POST.get("decision") == "approve"
    chosen = request.POST.getlist("permissions") if approve else None
    try:
        service.decide_request(request.user, req, approve, request.POST.get("note", ""), permission_codes=chosen)
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, f"{'Approved' if approve else 'Declined'} {req.user.email}'s request.", back)


@login_required
@require_POST
def open_recertification(request, code):
    org = _org_or_404(request, code)
    back = f"/admin/organisations/{org.code}/"
    form = forms.RecertificationForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a due date.")
        return redirect(back)
    try:
        service.open_recertification(request.user, org, _end_of_day(form.cleaned_data["due"]))
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, "Recertification started: confirm or remove each grant by the due date.", back)


@login_required
@require_POST
def recertify(request, pk):
    item = get_object_or_404(RecertificationItem.objects.select_related("campaign__organisation", "grant__membership__user"), pk=pk)
    back = f"/admin/organisations/{item.campaign.organisation.code}/#recertification"
    try:
        if request.POST.get("decision") == "remove":
            form = forms.ReasonForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Give a reason for removing the grant.")
                return redirect(back)
            service.remove_in_recertification(request.user, item, form.cleaned_data["reason"])
            msg = f"Removed {perms.label(item.grant.permission)} from {item.grant.user.email}."
        else:
            service.certify(request.user, item)
            msg = f"Certified {perms.label(item.grant.permission)} for {item.grant.user.email}."
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, msg, back)


# --- data stewards (decision G)


@login_required
def stewardship(request):
    if not service.is_steward(request.user):
        raise Http404
    pending = Grant.objects.filter(status=Grant.Status.PENDING).select_related("membership__user", "membership__organisation", "granted_by")
    return render(
        request,
        "administration/stewardship.html",
        {
            "pending": pending,
            "changes": PolicyChange.objects.filter(status=PolicyChange.Status.PROPOSED).select_related("proposed_by"),
            "stewards": service.active_stewards().select_related("membership__user"),
            "policy": AccessPolicy.current(),
            "change_form": forms.PolicyChangeForm(),
        },
    )


@login_required
@require_POST
def pii_decision(request, pk):
    g = get_object_or_404(Grant, pk=pk)
    try:
        if request.POST.get("decision") == "approve":
            service.approve_pii(request.user, g)
            msg = f"Approved PII access for {g.user.email} in {g.organisation.code}."
        else:
            service.decline_pii(request.user, g, request.POST.get("reason", "") or "declined by a data steward")
            msg = f"Declined PII access for {g.user.email}."
    except AccessDenied as e:
        return _refused(request, e, "administration:stewardship")
    return _done(request, msg, "administration:stewardship")


@login_required
@require_POST
def propose_change(request):
    form = forms.PolicyChangeForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a value and give a reason.")
        return redirect("administration:stewardship")
    try:
        service.propose_policy_change(request.user, "pii_requires_federated", form.cleaned_data["value"], form.cleaned_data["reason"])
    except AccessDenied as e:
        return _refused(request, e, "administration:stewardship")
    return _done(request, "Proposed. A second data steward must confirm it before it takes effect.", "administration:stewardship")


@login_required
@require_POST
def decide_change(request, pk):
    change = get_object_or_404(PolicyChange, pk=pk)
    try:
        if request.POST.get("decision") == "confirm":
            service.confirm_policy_change(request.user, change)
            msg = "Confirmed: the change is in effect."
        else:
            service.withdraw_policy_change(request.user, change, request.POST.get("reason", ""))
            msg = "Withdrawn."
    except AccessDenied as e:
        return _refused(request, e, "administration:stewardship")
    return _done(request, msg, "administration:stewardship")


# --- CPUC system administrators


@login_required
def access_settings(request):
    if not service.is_system_admin(request.user):
        raise Http404
    policy = AccessPolicy.current()
    form = forms.PolicyForm(
        request.POST or None,
        initial={
            "recertification_auto_suspend": policy.recertification_auto_suspend,
            "recertification_interval_days": policy.recertification_interval_days,
        },
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            for name in ("recertification_auto_suspend", "recertification_interval_days"):
                if getattr(policy, name) != d[name]:
                    service.set_policy(request.user, name, d[name], d["reason"])
        except AccessDenied as e:
            return _refused(request, e, "administration:settings")
        return _done(request, "Access settings saved.", "administration:settings")
    return render(request, "administration/settings.html", {"form": form, "policy": policy})


@login_required
def audit_log(request):
    sysadmin = service.is_system_admin(request.user)
    admin_orgs = [o for o in request.user_organisations if service.is_org_admin(request.user, o)]
    if not sysadmin and not admin_orgs:
        raise Http404
    qs = AuditEvent.objects.select_related("organisation")
    if not sysadmin:
        # an organisation's admins see what happened to its members and grants, and what its people did
        member_ids = Membership.objects.filter(organisation__in=admin_orgs).values("pk")
        grant_ids = Grant.objects.filter(membership__organisation__in=admin_orgs).values("pk")
        qs = (
            qs.filter(organisation__in=admin_orgs)
            | qs.filter(object_type="accounts.grant", object_id__in=[str(p["pk"]) for p in grant_ids])
            | qs.filter(object_type="accounts.membership", object_id__in=[str(p["pk"]) for p in member_ids])
        )
    form = forms.AuditFilterForm(request.GET or None)
    if form.is_valid():
        d = form.cleaned_data
        if d["action"]:
            qs = qs.filter(action__startswith=d["action"])
        if d["actor"]:
            qs = qs.filter(actor_label__icontains=d["actor"])
        if d["object_type"]:
            qs = qs.filter(object_type=d["object_type"])
    page = Paginator(qs.order_by("-at", "-id"), 50).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "administration/audit.html", {"page": page, "form": form, "query": query.urlencode()})


# --- statewide allocation versions (BR-06.7, BR-13.8)


def _require_sysadmin(request):
    if not service.is_system_admin(request.user):
        raise Http404


@login_required
def allocations(request):
    from cedars.services.registry.models import AllocationVersion

    _require_sysadmin(request)
    form = forms.AllocationUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        from cedars.services.registry import service as registry

        d = form.cleaned_data
        upload = d["file"]
        if upload.size > 5 * 1024 * 1024:
            messages.error(request, "That file is over 5 MB; a factor file is a few hundred rows.")
            return redirect("administration:allocations")
        try:
            v = registry.import_allocation_csv(
                request.user,
                upload.read(),
                label=d["label"],
                purpose=d["purpose"],
                source_name=upload.name,
                notes=d["notes"],
                filing_cycle=registry.ensure_cycle(d["cycle"]) if d["cycle"] else None,
            )
        except (ValueError, KeyError, UnicodeDecodeError) as e:
            messages.error(request, f"The file could not be read: {e}")
            return redirect("administration:allocations")
        except AccessDenied as e:
            return _refused(request, e, "administration:allocations")
        return _done(request, f"{v.label} loaded as a draft. Check it, then confirm it.", f"/admin/allocations/{v.pk}/")
    versions = AllocationVersion.objects.select_related("filing_cycle", "confirmed_by")
    return render(request, "administration/allocations.html", {"versions": versions, "form": form})


@login_required
def allocation(request, pk):
    from cedars.services.registry import service as registry
    from cedars.services.registry.models import AllocationVersion

    _require_sysadmin(request)
    v = get_object_or_404(AllocationVersion, pk=pk)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "confirm":
                registry.confirm_allocation(request.user, v)
                return _done(
                    request, f"{v.label} confirmed. Runs naming no version now use it for its years.", f"/admin/allocations/{v.pk}/"
                )
            if request.POST.get("action") == "discard":
                registry.discard_draft(request.user, v, request.POST.get("reason", ""))
                return _done(request, "Draft discarded.", "administration:allocations")
        except AccessDenied as e:
            return _refused(request, e, f"/admin/allocations/{v.pk}/")
    years = sorted(set(v.entries.values_list("year", flat=True)))
    return render(
        request,
        "administration/allocation.html",
        {"v": v, "issues": registry.check_allocation(v), "years": years, "count": v.entries.count()},
    )


@login_required
def allocation_csv(request, pk):
    from django.http import HttpResponse

    from cedars.services.registry import service as registry
    from cedars.services.registry.models import AllocationVersion

    _require_sysadmin(request)
    v = get_object_or_404(AllocationVersion, pk=pk)
    response = HttpResponse(registry.allocation_csv(v), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="statewide_factors_{v.pk}.csv"'
    return response


@login_required
def goal_sets(request):
    from cedars.services.registry import goals
    from cedars.services.registry.models import GoalSet

    _require_sysadmin(request)
    form = forms.GoalSetUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        upload, rules = d["file"], d["rules"]
        if upload.size > 1024 * 1024 or (rules and rules.size > 1024 * 1024):
            messages.error(request, "That file is over 1 MB; a decision's goal tables are a few dozen rows.")
            return redirect("administration:goal_sets")
        try:
            s = goals.import_goals(
                request.user,
                upload.read(),
                decision=d["decision"],
                adopted=d["adopted"],
                url=d["url"],
                rules=rules.read() if rules else None,
                source_name=upload.name,
                notes=d["notes"],
            )
        except (ValueError, KeyError, UnicodeDecodeError) as e:
            messages.error(request, f"The file could not be read: {e}")
            return redirect("administration:goal_sets")
        except AccessDenied as e:
            return _refused(request, e, "administration:goal_sets")
        return _done(
            request,
            f"{s.decision} loaded as a draft. Another administrator checks it against the decision and confirms it.",
            f"/admin/goals/{s.pk}/",
        )
    sets = [(s, goals.in_force_years(s)) for s in GoalSet.objects.select_related("confirmed_by")]
    return render(request, "administration/goal_sets.html", {"sets": sets, "form": form})


@login_required
def goal_set(request, pk):
    from cedars.services.registry import goals
    from cedars.services.registry.models import GoalSet

    _require_sysadmin(request)
    s = get_object_or_404(GoalSet, pk=pk)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "confirm":
                goals.confirm(request.user, s)
                return _done(request, f"{s.decision}'s goals are in force for the years they cover.", f"/admin/goals/{s.pk}/")
            if request.POST.get("action") == "discard":
                goals.discard(request.user, s, request.POST.get("reason", ""))
                return _done(request, "Draft discarded.", "administration:goal_sets")
        except AccessDenied as e:
            return _refused(request, e, f"/admin/goals/{s.pk}/")
    return render(
        request,
        "administration/goal_set.html",
        {
            "s": s,
            "issues": goals.check(s),
            "cycle_goals": s.goals.filter(kind="cycle"),
            "annual_goals": s.goals.filter(kind="annual"),
            "rules": s.counting_rules.all(),
            "in_force": goals.in_force_years(s),
        },
    )


@login_required
def goal_set_csv(request, pk):
    from django.http import HttpResponse

    from cedars.services.registry import goals
    from cedars.services.registry.models import GoalSet

    _require_sysadmin(request)
    s = get_object_or_404(GoalSet, pk=pk)
    response = HttpResponse(goals.goals_csv(s), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="goals_{s.decision.replace(".", "")}.csv"'
    return response


# --- job and system health (BR-12.2)

STATUS_CLASS = {health.OK: "ok", health.ATTENTION: "warn", health.FAILING: "bad"}


@login_required
def health_page(request):
    """Every registered health check (cedars/core/health.py): stalled and failed jobs, the eTRM sync, unread document text, emails.
    The same checks run every 10 minutes (`manage.py check_health`), which alerts system administrators and feeds CloudWatch."""
    if not service.is_system_admin(request.user):
        raise Http404
    results = health.run_all()
    counts = {s: sum(r.status == s for r in results) for s in (health.FAILING, health.ATTENTION, health.OK)}
    return render(
        request,
        "administration/health.html",
        {
            "results": [(r, STATUS_CLASS[r.status]) for r in results],
            "failing": counts[health.FAILING],
            "attention": counts[health.ATTENTION],
            "ok": counts[health.OK],
            "checked_at": timezone.now(),
        },
    )

from datetime import datetime, time
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from cedars.core import health, tables
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
    RoleTemplate,
    User,
)
from cedars.services.accounts.service import AccessDenied
from cedars.services.audit.models import AuditEvent

from . import forms


def _end_of_day(d):
    return timezone.make_aware(datetime.combine(d, time(23, 59, 59))) if d else None


def _next(request, default):
    """Where a form's `next` field sends the browser back to: a page on this site, else the default."""
    nxt = request.POST.get("next", "")
    ok = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return nxt if nxt and ok else default


def _done(request, ok_message, to):
    messages.success(request, ok_message)
    return redirect(to)


def _refused(request, e: AccessDenied, to):
    messages.error(request, str(e))
    return redirect(to)


def _org_or_404(request, code) -> Organisation:
    org = service.by_code_or_404(code)  # a former code finds the organization that had it
    if not (service.is_org_admin(request.user, org) or service.is_system_admin(request.user)):
        raise Http404
    return org


# --- the user's own account


@login_required
def account(request):
    from cedars.services.accounts.signals import ensure_email_record

    user = request.user
    ensure_email_record(user)  # so the email page can change it (a session from before this record was kept)
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
            "name_form": forms.NameForm(initial={"name": user.name}, auto_id="name_%s"),
            "can_close": service.can_close_own_account(user),
        },
    )


@login_required
@require_POST
def account_name(request):
    """A person sets the name the pages show for them."""
    form = forms.NameForm(request.POST)
    if not form.is_valid():
        messages.error(request, f"Check your name: {_form_problems(form)}")
        return redirect("administration:account")
    try:
        service.set_name(request.user, request.user, form.cleaned_data["name"])
    except AccessDenied as e:
        return _refused(request, e, "administration:account")
    return _done(
        request,
        "Your name is saved." if request.user.name else "Your name is cleared: pages show your email address.",
        "administration:account",
    )


@login_required
@require_POST
def account_close(request):
    """A community account closes itself: it is deactivated (never deleted), signed out everywhere, and only the CPUC can reopen it."""
    form = forms.ReasonForm(request.POST)
    if not form.is_valid() or request.POST.get("confirm") != "yes":
        messages.error(request, "Tick the box to confirm, and say why you are closing your account.")
        return redirect("administration:account")
    try:
        service.deactivate_user(request.user, request.user, form.cleaned_data["reason"], keep_session=request.session.session_key)
    except AccessDenied as e:
        return _refused(request, e, "administration:account")
    from django.contrib.auth import logout

    logout(request)
    messages.success(request, "Your account is closed. To use CEDARS again, ask the CPUC to reopen it.")
    return redirect("/")


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
    back = f"/admin/organizations/{token.organisation.code}/" if from_org else "administration:account"
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
        messages.error(request, "You are not a member of that organization.")
    else:
        request.session[ORG_SESSION_KEY] = org.pk
        messages.success(request, f"You are now acting for {org.code}.")
    return redirect(_next(request, "administration:account"))


@login_required
def request_access(request):
    """Ask an organization for access in two steps that work without scripts: choose the organization, then its roles (the role templates
    offered for it, in plain words) or single permissions behind a disclosure. The page says who decides: the organization's
    administrators, or the CPUC while it has none (UI review 3, U6)."""
    chosen = request.POST.get("organisation") or request.GET.get("organisation")
    org = service.requestable_organisations().filter(code=chosen).first() if chosen else None
    if org is None:
        pick = forms.ChooseOrganisationForm(request.GET if chosen else None)
        return render(request, "administration/request_access.html", {"pick": pick})
    templates = service.role_templates_for(org)
    form = forms.RequestAccessForm(request.POST or None, templates=templates, initial={"organisation": org.code})
    if request.method == "POST" and form.is_valid():
        try:
            service.request_access(request.user, org, form.cleaned_data["codes"], form.cleaned_data["message"])
        except AccessDenied as e:
            return _refused(request, e, f"{reverse('administration:request_access')}?organisation={org.code}")
        return _done(request, f"Your request was sent to {service.decider_words(org)}.", "administration:account")
    roles = [(t, perms.labels(t.permissions), next(cb for cb in form["roles"] if cb.data["value"] == str(t.pk))) for t in templates]
    singles = [(cb, perms.describe(cb.data["value"])) for cb in form["permissions"]]
    return render(
        request,
        "administration/request_access.html",
        {
            "org": org,
            "form": form,
            "roles": roles,
            "singles": singles,
            "singles_open": bool(form["permissions"].value()) or not templates,
            "cpuc_decides": service.cpuc_decides(org),
        },
    )


@login_required
@require_POST
def withdraw_request(request, pk):
    req = get_object_or_404(AccessRequest, pk=pk, user=request.user)
    try:
        service.withdraw_request(request.user, req)
    except AccessDenied as e:
        return _refused(request, e, "administration:account")
    return _done(request, "Request withdrawn.", "administration:account")


# --- organizations (PA admins and CPUC system administrators)


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
                "former": service.party_codes(org)[1:] if not org.successor_id else [],
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
        return _done(request, f"{org.code} added. Assign its first administrator below.", f"/admin/organizations/{org.code}/")
    return render(request, "administration/organisation_new.html", {"form": form})


def _grantable_codes(user, org) -> list[str]:
    if not service.org_admins(org).exists():
        return [perms.ORG_ADMIN] if service.is_system_admin(user) else []
    codes = perms.module_permissions() + [perms.ORG_ADMIN, perms.PII]
    if org.manages_allocation:
        codes.append(perms.ALLOCATION)
    if org.kind == Organisation.Kind.CPUC:
        if service.is_system_admin(user):
            codes += list(perms.CPUC_ONLY)
        codes = [c for c in codes if c != perms.PII]
    return codes if service.is_org_admin(user, org) or (org.kind == "cpuc" and service.is_system_admin(user)) else []


@login_required
def organisation(request, code):
    org = _org_or_404(request, code)
    if org.code != code:  # an old link, under a former code
        return redirect("administration:organisation", org.code)
    is_admin = service.is_org_admin(request.user, org)
    codes = _grantable_codes(request.user, org)
    # role templates bundle module permissions, which only the organization's own administrators grant
    templates = service.role_templates_for(org) if is_admin else []
    memberships = org.memberships.filter(ended_at__isnull=True).select_related("user").order_by("user__email")
    members = []
    for m in memberships:
        grants = m.grants.filter(status__in=Grant.OPEN).select_related("role_template", "granted_by").order_by("permission")
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
            "can_decide": service.can_decide_request(request.user, org),
            "members": members,
            "grant_form": forms.GrantForm(codes=codes, auto_id="grant_%s") if codes else None,
            "template_form": forms.TemplateGrantForm(templates=templates, auto_id="tpl_%s") if templates else None,
            "templates": templates,
            "requests": [
                (r, [(c, perms.label(c)) for c in r.permissions])
                for r in org.access_requests.filter(status="pending").select_related("user")
            ],
            "campaign": campaign,
            "items": items,
            "recert_form": forms.RecertificationForm(auto_id="recert_%s"),
            "details_form": forms.OrganisationDetailsForm(
                org=org,
                initial={"name": org.name, "pa_type": org.pa_type, "manages_allocation": org.manages_allocation},
                auto_id="details_%s",
            )
            if service.is_system_admin(request.user)
            else None,
            "history": service.code_history(org),
            "predecessors": service.party_members(org)[1:] if not org.successor_id else [],
            "code_forms": {
                "rename": forms.RenameForm(auto_id="rename_%s"),
                "former": forms.FormerCodeForm(auto_id="former_%s"),
                "merge": forms.MergeForm(org=org, auto_id="merge_%s"),
            }
            if service.is_system_admin(request.user) and not org.successor_id
            else None,
            "no_admin": not service.org_admins(org).exists(),
            "status_forms": {
                "federation": forms.FederationForm(
                    initial={"federated": org.federated, "provider": org.federation_provider}, auto_id="fed_%s"
                ),
                "retire": forms.ReasonForm(auto_id="retire_%s"),
            }
            if service.is_system_admin(request.user) and not org.successor_id and org.kind != Organisation.Kind.CPUC
            else None,
            "federation_waiting": service.federation_changes_waiting(org),
            "holds_pii": service.holds_pii_grants(org),
            "allocation_permission": perms.label(perms.ALLOCATION),
            "tokens": ApiToken.objects.filter(organisation=org, revoked_at__isnull=True, expires_at__gt=timezone.now()).select_related(
                "user"
            )
            if is_admin
            else [],
        },
    )


@login_required
@require_POST
def organisation_details(request, code):
    """A CPUC system administrator changes an organization's name or PA type (with a reason)."""
    org = service.by_code_or_404(code)
    if not service.is_system_admin(request.user):
        raise Http404
    back = f"/admin/organizations/{org.code}/"
    form = forms.OrganisationDetailsForm(request.POST, org=org)
    if not form.is_valid():
        problems = "; ".join(f"{form.fields[k].label if k in form.fields else k}: {' '.join(v)}" for k, v in form.errors.items())
        messages.error(request, "Check the details form: " + problems)
        return redirect(back)
    d = form.cleaned_data
    changes = {k: d[k] for k in service.ORGANISATION_DETAILS if k in d}
    try:
        service.change_organisation(request.user, org, reason=d["reason"], **changes)
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, f"{org.code}'s details saved.", back)


def _form_problems(form) -> str:
    return "; ".join(f"{form.fields[k].label if k in form.fields else k}: {' '.join(v)}" for k, v in form.errors.items())


def _code_change(request, code, form_class, what: str, change):
    """A CPUC system administrator's change to an organization's codes (rename, former code, merge): check the form, make the change
    through the accounts service, and go back to the organization's page under the code it has now."""
    org = service.by_code_or_404(code)
    if not service.is_system_admin(request.user):
        raise Http404
    kwargs = {"org": org} if form_class is forms.MergeForm else {}
    form = form_class(request.POST, **kwargs)
    if not form.is_valid():
        messages.error(request, f"Check the {what} form: {_form_problems(form)}")
        return redirect("administration:organisation", org.code)
    try:
        message, target = change(org, form.cleaned_data)
    except AccessDenied as e:
        messages.error(request, str(e))
        return redirect("administration:organisation", org.code)
    messages.success(request, message)
    return redirect("administration:organisation", target.code)


@login_required
@require_POST
def organisation_rename(request, code):
    """Give an organization a new code from a date (EBCE -> AVA): the old code stays in its history and still finds it."""

    def change(org, d):
        old = org.code
        service.rename_organisation(request.user, org, code=d["code"], name=d["name"], effective=d["effective"], reason=d["reason"])
        return f"{old} is now {org.code}. Its records filed as {old} stay as filed and count under {org.code}.", org

    return _code_change(request, code, forms.RenameForm, "rename", change)


@login_required
@require_POST
def organisation_former_code(request, code):
    """Record a code an organization used before its current one (for records filed under it)."""

    def change(org, d):
        row = service.add_former_code(request.user, org, code=d["code"], since=d["since"], until=d["until"], reason=d["reason"])
        return f"{row.code} recorded as {org.code}'s code {row.span}. Records filed as {row.code} now count under {org.code}.", org

    return _code_change(request, code, forms.FormerCodeForm, "former code", change)


@login_required
@require_POST
def organisation_merge(request, code):
    """Record that this organization and another are one party (the same PA loaded under an old and a new code)."""

    def change(org, d):
        source = d["source"]
        service.merge_organisation(request.user, source, into=org, effective=d["effective"], reason=d["reason"])
        return f"{source.code} merged into {org.code}: its records stay its own and count under {org.code}.", org

    return _code_change(request, code, forms.MergeForm, "merge", change)


@login_required
@require_POST
def organisation_sign_in(request, code):
    """Switch whether an organization's members sign in through its own directory (CPUC system administrators). With PII grants in it,
    a data steward confirms the change first."""

    def change(org, d):
        result = service.change_federation(request.user, org, federated=d["federated"], provider=d["provider"], reason=d["reason"])
        if isinstance(result, Organisation):
            how = f"through its own directory ({org.federation_provider})" if org.federated else "with a password and a second factor"
            return f"{org.code}'s members now sign in {how}.", org
        return (
            f"{org.code} has members with PII access, so a data steward must confirm this change on the stewardship page before it "
            "takes effect."
        ), org

    return _code_change(request, code, forms.FederationForm, "directory sign-in", change)


@login_required
@require_POST
def organisation_retire(request, code):
    """Retire an organization without merging it (CPUC system administrators): it stops filing; its data and history stay."""

    def change(org, d):
        if request.POST.get("action") == "reinstate":
            service.reinstate_organisation(request.user, org, d["reason"])
            return f"{org.code} is active again: its members act for it with the permissions they held.", org
        service.retire_organisation(request.user, org, d["reason"])
        return f"{org.code} is retired: it no longer files or appears in pickers. Its data and history stay.", org

    return _code_change(request, code, forms.ReasonForm, "retire", change)


@login_required
@require_POST
def grant(request, code):
    org = _org_or_404(request, code)
    back = f"/admin/organizations/{org.code}/"
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
def grant_template(request, code):
    """Grant a member every permission of a role template."""
    org = _org_or_404(request, code)
    back = f"/admin/organizations/{org.code}/"
    templates = service.role_templates_for(org)
    form = forms.TemplateGrantForm(request.POST, templates=templates)
    if not form.is_valid():
        messages.error(request, f"Check the role template form: {_form_problems(form)}")
        return redirect(back)
    d = form.cleaned_data
    user = User.objects.filter(email__iexact=d["email"]).first()
    if user is None:
        messages.error(request, f"There is no account for {d['email']}. Ask them to create one, then grant access.")
        return redirect(back)
    template = next(t for t in templates if str(t.pk) == d["template"])
    try:
        result = service.apply_role_template(request.user, user, org, template, expires_at=_end_of_day(d["expires"]), reason=d["reason"])
    except AccessDenied as e:
        return _refused(request, e, back)
    labels = ", ".join(perms.label(g.permission) for g in result.granted)
    msg = f"Granted {template.name} to {user.email}: {labels}."
    if result.already:
        msg += f" Already held: {', '.join(perms.label(p) for p in result.already)}."
    return _done(request, msg, back)


@login_required
@require_POST
def revoke(request, pk):
    g = get_object_or_404(Grant.objects.select_related("membership__organisation", "membership__user"), pk=pk)
    back = _next(request, f"/admin/organizations/{g.organisation.code}/")
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
    back = f"/admin/organizations/{g.organisation.code}/"
    try:
        service.reinstate(request.user, g, request.POST.get("reason", "certified late"))
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, f"Reinstated {perms.label(g.permission)} for {g.user.email}.", back)


@login_required
@require_POST
def end_membership(request, pk):
    m = get_object_or_404(Membership.objects.select_related("organisation", "user"), pk=pk)
    back = f"/admin/organizations/{m.organisation.code}/"
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
    back = f"/admin/organizations/{req.organisation.code}/"
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
    back = f"/admin/organizations/{org.code}/"
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
    back = f"/admin/organizations/{item.campaign.organisation.code}/#recertification"
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
            "changes": PolicyChange.objects.filter(status=PolicyChange.Status.PROPOSED)
            .exclude(setting=service.FEDERATION_CHANGE)
            .select_related("proposed_by"),
            "federation": service.federation_changes_waiting(),
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
            service.approve_pii(request.user, g, request.POST.get("reason", "").strip())
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
            if change.setting == service.FEDERATION_CHANGE:
                msg = f"Confirmed: {change.value['code']}'s directory sign-in is changed."
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


# --- people (CPUC system administrators)


PEOPLE_EVENTS = {
    "user.name_changed": "Name changed",
    "user.email_changed": "Email changed",
    "user.deactivated": "Deactivated",
    "user.reactivated": "Reactivated",
}


@login_required
def people(request):
    """Every account: find a person by name or email, and see whether they are active and where they are a member."""
    _require_sysadmin(request)
    form = forms.PeopleFilterForm(request.GET or None)
    qs = User.objects.all().order_by("email")
    if form.is_valid():
        d = form.cleaned_data
        if d["q"]:
            qs = qs.filter(Q(email__icontains=d["q"]) | Q(name__icontains=d["q"]))
        if d["status"] == "active":
            qs = qs.filter(is_active=True)
        elif d["status"] == "deactivated":
            qs = qs.filter(is_active=False)
        elif d["status"] == "community":
            qs = qs.exclude(pk__in=Membership.objects.filter(ended_at__isnull=True).values("user"))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    open_memberships = Membership.objects.filter(user__in=list(page.object_list), ended_at__isnull=True).select_related("organisation")
    orgs = {}
    for m in open_memberships:
        orgs.setdefault(m.user_id, []).append(m.organisation.code)
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "administration/people.html",
        {
            "form": form,
            "page": page,
            "rows": [(u, orgs.get(u.pk, [])) for u in page.object_list],
            "query": query.urlencode(),
            "total": qs.count(),
        },
    )


@login_required
def person(request, pk):
    """One account: its name, email, status and memberships, with name correction, deactivation and reactivation."""
    _require_sysadmin(request)
    u = get_object_or_404(User, pk=pk)
    memberships = Membership.objects.filter(user=u).select_related("organisation").order_by("ended_at", "organisation__code")
    events = AuditEvent.objects.filter(object_type="accounts.user", object_id=str(u.pk)).select_related("actor").order_by("-at", "-id")[:20]
    return render(
        request,
        "administration/person.html",
        {
            "person": u,
            "memberships": [(m, [perms.label(g.permission) for g in m.grants.filter(status__in=Grant.OPEN)]) for m in memberships],
            "name_form": forms.NameForm(initial={"name": u.name}, correcting=True, auto_id="name_%s"),
            "tokens": ApiToken.objects.filter(user=u, revoked_at__isnull=True, expires_at__gt=timezone.now()).count(),
            "events": [(e, PEOPLE_EVENTS.get(e.action, e.action)) for e in events],
            "is_self": u.pk == request.user.pk,
        },
    )


@login_required
@require_POST
def person_name(request, pk):
    _require_sysadmin(request)
    u = get_object_or_404(User, pk=pk)
    back = reverse("administration:person", args=[u.pk])
    form = forms.NameForm(request.POST, correcting=u.pk != request.user.pk)
    if not form.is_valid():
        messages.error(request, f"Check the name form: {_form_problems(form)}")
        return redirect(back)
    try:
        service.set_name(request.user, u, form.cleaned_data["name"], form.cleaned_data.get("reason", ""))
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, f"Name saved for {u.email}.", back)


@login_required
@require_POST
def person_deactivate(request, pk):
    _require_sysadmin(request)
    u = get_object_or_404(User, pk=pk)
    back = reverse("administration:person", args=[u.pk])
    form = forms.ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the reason for deactivating the account.")
        return redirect(back)
    if u.pk == request.user.pk:
        messages.error(request, "You cannot deactivate your own account here; ask another CPUC system administrator.")
        return redirect(back)
    try:
        service.deactivate_user(request.user, u, form.cleaned_data["reason"])
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, f"{u.email} is deactivated: signed out everywhere, API tokens revoked. Nothing was deleted.", back)


@login_required
@require_POST
def person_reactivate(request, pk):
    _require_sysadmin(request)
    u = get_object_or_404(User, pk=pk)
    back = reverse("administration:person", args=[u.pk])
    form = forms.ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the reason for reactivating the account.")
        return redirect(back)
    try:
        service.reactivate_user(request.user, u, form.cleaned_data["reason"])
    except AccessDenied as e:
        return _refused(request, e, back)
    return _done(request, f"{u.email} can sign in again, with the memberships and permissions they had.", back)


@login_required
def audit_log(request):
    sysadmin = service.is_system_admin(request.user)
    admin_orgs = [o for o in request.user_organisations if service.is_org_admin(request.user, o)]
    if not sysadmin and not admin_orgs:
        raise Http404
    qs = AuditEvent.objects.select_related("organisation")
    if not sysadmin:
        # an organization's admins see what happened to its members and grants, and what its people did
        member_ids = Membership.objects.filter(organisation__in=admin_orgs).values("pk")
        grant_ids = Grant.objects.filter(membership__organisation__in=admin_orgs).values("pk")
        qs = (
            qs.filter(organisation__in=admin_orgs)
            | qs.filter(object_type="accounts.grant", object_id__in=[str(p["pk"]) for p in grant_ids])
            | qs.filter(object_type="accounts.membership", object_id__in=[str(p["pk"]) for p in member_ids])
        )
    orgs = list(Organisation.objects.order_by("code")) if sysadmin else sorted(admin_orgs, key=lambda o: o.code)
    actions = list(qs.order_by("action").values_list("action", flat=True).distinct())
    form = forms.AuditFilterForm(
        request.GET or None,
        actions=actions,
        types=[t for t in qs.order_by("object_type").values_list("object_type", flat=True).distinct() if t],
        organisations=orgs,
    )
    if form.is_valid():
        d = form.cleaned_data
        if d["action"]:  # one action, or every action that starts so (a family from the list, "grant.")
            qs = qs.filter(action=d["action"]) if d["action"] in actions else qs.filter(action__startswith=d["action"])
        if d["actor"]:
            qs = qs.filter(actor_label__icontains=d["actor"])
        if d["object_type"]:
            qs = qs.filter(object_type=d["object_type"])
        if d["organisation"]:
            qs = qs.filter(organisation__code=d["organisation"])
        if d["since"]:
            qs = qs.filter(at__date__gte=d["since"])
        if d["until"]:
            qs = qs.filter(at__date__lte=d["until"])
    page = Paginator(qs.order_by("-at", "-id"), 50).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "administration/audit.html", {"page": page, "form": form, "query": query.urlencode()})


# --- role templates


TEMPLATE_EVENTS = {
    "role_template.created": "Added",
    "role_template.changed": "Changed",
    "role_template.retired": "Retired",
    "role_template.reinstated": "Offered again",
}


def _template_form_values(d) -> dict:
    return {"name": d["name"], "description": d["description"], "permissions": d["permissions"], "kinds": d["kinds"]}


@login_required
def role_templates(request):
    _require_sysadmin(request)
    form = forms.RoleTemplateForm(request.POST or None, auto_id="new_%s")
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            t = service.create_role_template(request.user, reason=d["reason"], **_template_form_values(d))
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            msg = f"Role template {t.name} added. Organization administrators can grant it now."
            return _done(request, msg, "administration:role_templates")
    rows = [
        (t, [perms.label(p) for p in t.permissions], [Organisation.Kind(k).label for k in t.kinds], t.grants.count())
        for t in RoleTemplate.objects.all()
    ]
    return render(request, "administration/role_templates.html", {"rows": rows, "form": form})


@login_required
def role_template(request, pk):
    _require_sysadmin(request)
    t = get_object_or_404(RoleTemplate, pk=pk)
    back = f"/admin/role-templates/{t.pk}/"
    if request.method == "POST" and request.POST.get("action") in ("retire", "reinstate"):
        reason = request.POST.get("reason", "")
        try:
            service.change_role_template(request.user, t, reason=reason, active=request.POST["action"] == "reinstate")
        except AccessDenied as e:
            return _refused(request, e, back)
        return _done(request, f"{t.name} {'retired' if request.POST['action'] == 'retire' else 'offered again'}.", back)
    form = forms.RoleTemplateForm(
        request.POST or None,
        auto_id="tpl_%s",
        initial={"name": t.name, "description": t.description, "permissions": t.permissions, "kinds": t.kinds},
    )
    form.fields["reason"].required = True
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            service.change_role_template(request.user, t, reason=d["reason"], **_template_form_values(d))
        except AccessDenied as e:
            messages.error(request, str(e))
        else:
            return _done(request, f"{t.name} saved. Grants it already made are unchanged.", back)
    grants = t.grants.filter(status__in=Grant.OPEN).select_related("membership__user", "membership__organisation")
    return render(
        request,
        "administration/role_template.html",
        {
            "t": t,
            "form": form,
            "grants": [
                (g, perms.label(g.permission))
                for g in grants.order_by("membership__organisation__code", "membership__user__email", "permission")
            ],
            "history": [
                (e, TEMPLATE_EVENTS.get(e.action, e.action))
                for e in AuditEvent.objects.filter(object_type="accounts.roletemplate", object_id=str(t.pk)).order_by("-at", "-id")[:20]
            ],
        },
    )


# --- statewide allocation versions


def _require_sysadmin(request):
    if not service.is_system_admin(request.user):
        raise Http404


def _allocation_manager(request) -> bool:
    from cedars.services.registry import service as registry

    return request.user.is_authenticated and registry.can_manage_allocation(request.user, getattr(request, "organisation", None))


def _allocation_versions(request):
    """The versions the visitor may read: every one signed in; the confirmed ones signed out (Jake, 2026-11-04: the factors are for
    everyone to view; a draft stays with the funding PAs and the CPUC, and everyone signed in, as before)."""
    from cedars.services.registry.models import AllocationVersion

    qs = AllocationVersion.objects.all()
    return qs if request.user.is_authenticated else qs.filter(status=AllocationVersion.Status.CONFIRMED)


def allocations(request):
    """Every statewide allocation version, for everyone (drafts signed in only); the managing organizations' holders and the CPUC load
    new ones."""

    can_manage = _allocation_manager(request)
    form = forms.AllocationUploadForm(request.POST or None, request.FILES or None) if can_manage else None
    if request.method == "POST":
        if not can_manage:
            raise Http404
        if form.is_valid():
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
                    organisation=request.organisation,
                )
            except (ValueError, KeyError, UnicodeDecodeError) as e:
                messages.error(request, f"The file could not be read: {e}")
                return redirect("administration:allocations")
            except AccessDenied as e:
                return _refused(request, e, "administration:allocations")
            note = ""
            if v.fuel_copied:
                n = v.fuel_copied
                years = f"{n} one-fuel program-year{'s' if n != 1 else ''}"
                note += f" On {years}, the unused fuel's blank factors were copied from the other fuel."
            if v.weighted_replaced:
                n = v.weighted_replaced
                note += f" The file's own weighted factors were replaced with the computed ones on {n} program-year{'s' if n != 1 else ''}."
            return _done(request, f"{v.label} loaded as a draft.{note} Check it, then confirm it.", f"/allocations/{v.pk}/")
    versions = _allocation_versions(request).select_related(
        "filing_cycle", "confirmed_by", "confirmed_organisation", "created_by", "created_organisation"
    )
    managers = Organisation.objects.filter(manages_allocation=True, active=True).order_by("code")
    return render(
        request,
        "administration/allocations.html",
        {
            "versions": versions,
            "form": form,
            "can_manage": can_manage,
            "managers": managers,
            "permission": perms.label(perms.ALLOCATION),
            # the latest confirmed version: its factors are a starting point for a new file (the template is blank)
            "example": _allocation_versions(request).filter(status="confirmed").order_by("-confirmed_at").first(),
        },
    )


def allocation(request, pk):
    from cedars.services.registry import service as registry

    v = get_object_or_404(
        _allocation_versions(request).select_related("created_by", "created_organisation", "confirmed_by", "confirmed_organisation"), pk=pk
    )
    can_manage = _allocation_manager(request)
    if request.method == "POST":
        if not can_manage:
            raise Http404
        org = getattr(request, "organisation", None)
        try:
            if request.POST.get("action") == "confirm":
                registry.confirm_allocation(request.user, v, organisation=org)
                return _done(request, f"{v.label} confirmed. Runs naming no version now use it for its years.", f"/allocations/{v.pk}/")
            if request.POST.get("action") == "discard":
                registry.discard_draft(request.user, v, request.POST.get("reason", ""), organisation=org)
                return _done(request, "Draft discarded.", "administration:allocations")
        except AccessDenied as e:
            return _refused(request, e, f"/allocations/{v.pk}/")
    years = sorted(set(v.entries.values_list("year", flat=True)))
    year, every = _allocation_year(request.GET.get("year", ""), years)
    query = request.GET.get("q", "").strip()[:100]
    entries = v.entries.prefetch_related("shares").order_by("year", "program_id", "pk")
    if year:
        entries = entries.filter(year=year)
    if query:
        entries = entries.filter(Q(program_id__icontains=query) | Q(program_name__icontains=query))
    page = Paginator(entries, ALLOCATION_ROWS).get_page(request.GET.get("page"))
    pas = list(registry.ALLOCATION_PAS)
    rows = []
    for e in page.object_list:
        by_pa = {s.funding_pa: s for s in e.shares.all()}
        rows.append({"entry": e, "shares": [by_pa.get(pa) for pa in pas]})
    here = reverse("administration:allocation", args=[v.pk])
    keep = f"&q={quote(query)}" if query else ""
    year_links = [{"label": str(y), "url": f"{here}?year={y}{keep}#factors-h", "on": y == year} for y in years]
    year_links.append({"label": "Every year", "url": f"{here}?year=all{keep}#factors-h", "on": every})
    pager_query = QueryDict(mutable=True)
    pager_query["year"] = str(year) if year else "all"
    if query:
        pager_query["q"] = query
    return render(
        request,
        "administration/allocation.html",
        {
            "v": v,
            "pas": pas,
            "ncols": (3 if every else 2) + 3 * len(pas),
            "rows": rows,
            "page": page,
            "year": year,
            "every": every,
            "today_year": timezone.localdate().year,
            # opened on the nearest year because the version has no factors for this one
            "nearest": year is not None and request.GET.get("year", "") != str(year) and year != timezone.localdate().year,
            "query": query,
            "pager_query": pager_query.urlencode(),
            "year_links": year_links,
            "applied": tables.chips(pager_query, [("q", "Program ID or name contains", query)], anchor="factors-h") if query else [],
            "issues": registry.check_allocation(v),
            "years": years,
            "count": v.entries.count(),
            "can_manage": can_manage,
            "history": registry.allocation_history(v),
        },
    )


# A version's factors are a few hundred rows (each statewide program for each year): the page shows one year at a time, paged.
ALLOCATION_ROWS = 50


def _allocation_year(asked: str, years: list[int]) -> tuple[int | None, bool]:
    """(year, every year) for the Factors table. "all" shows every year; a year the version has shows that year; anything else opens on
    the current year (Jake, 2026-11-11), or, when the version has no factors for it, the nearest year it has (the later of two as near)."""
    if asked == "all" or not years:
        return None, True
    if asked.isdigit() and int(asked) in years:
        return int(asked), False
    today = timezone.localdate().year
    return min(years, key=lambda y: (abs(y - today), -y)), False


def allocation_template(request):
    """The blank factor file a new version is loaded from (Jake, 2026-11-06): no weighted factor columns, since they are computed."""
    from django.http import HttpResponse

    from cedars.services.registry import service as registry

    response = HttpResponse(registry.allocation_template_csv(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="statewide_allocation_template.csv"'
    return response


def allocation_csv(request, pk):
    from django.http import HttpResponse

    from cedars.services.registry import service as registry

    v = get_object_or_404(_allocation_versions(request), pk=pk)
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
def goal_template_csv(request, which):
    from django.http import Http404, HttpResponse

    from cedars.services.registry import goals

    _require_sysadmin(request)
    if which not in ("goals", "rules"):
        raise Http404
    response = HttpResponse(goals.template_csv(which), content_type="text/csv; charset=utf-8")
    name = "goal_file_template.csv" if which == "goals" else "counting_rules_template.csv"
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


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


# --- job and system health

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

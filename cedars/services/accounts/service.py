"""Access rules (BR-01, D6, decisions F and G). Every change goes through these functions, each writes an audit event, and a refused
action raises AccessDenied with a sentence the page can show as it is.

Who may grant what:
  - an organisation's admins grant its module permissions and org.admin, never to themselves;
  - while an organisation has no admin, only a CPUC system administrator can grant its first one (D6), and only then;
  - claims.pii granted by an organisation's admin waits for any data steward's approval;
  - system.admin and pii.steward exist only in the CPUC organisation and are granted by a system administrator;
  - the last active data steward cannot be revoked, suspended or removed.
`actor=None` is the system itself (the bootstrap command and scheduled jobs).
"""

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from cedars.services import audit

from . import permissions as perms
from .models import (
    AccessPolicy,
    AccessRequest,
    Grant,
    Membership,
    Organisation,
    PolicyChange,
    RecertificationCampaign,
    RecertificationItem,
    User,
)

GRANT_FIELDS = ["membership", "permission", "status", "expires_at", "approved_by", "suspended_reason", "revoked_by", "revoke_reason"]


class AccessDenied(PermissionDenied):
    """A refused access change; str(e) is the plain-language reason."""


# --- reading


def cpuc() -> Organisation | None:
    return Organisation.objects.filter(kind=Organisation.Kind.CPUC).first()


def _effective(qs, now=None):
    now = now or timezone.now()
    return qs.filter(status=Grant.Status.ACTIVE, membership__ended_at__isnull=True).exclude(expires_at__lte=now)


def effective_permissions(user: User, organisation: Organisation) -> set[str]:
    if user is None or not user.is_active or organisation is None:
        return set()
    qs = Grant.objects.filter(membership__user=user, membership__organisation=organisation)
    return set(_effective(qs).values_list("permission", flat=True))


def has_permission(user: User, organisation: Organisation, permission: str, assurance: str | None = None) -> bool:
    """Whether the user holds the permission in the organisation now. claims.pii also checks the session's assurance when the CPUC
    requires federated sessions for PII (pass the session's assurance; None means "not in a session" and fails that check)."""
    if permission not in effective_permissions(user, organisation):
        return False
    if permission == perms.PII and AccessPolicy.current().pii_requires_federated:
        return organisation.federated and assurance == "federated"
    return True


def is_system_admin(user: User) -> bool:
    org = cpuc()
    return org is not None and has_permission(user, org, perms.SYSTEM_ADMIN)


def is_steward(user: User) -> bool:
    org = cpuc()
    return org is not None and has_permission(user, org, perms.STEWARD)


def is_org_admin(user: User, organisation: Organisation) -> bool:
    return has_permission(user, organisation, perms.ORG_ADMIN)


def org_admins(organisation: Organisation):
    return User.objects.filter(
        pk__in=_effective(Grant.objects.filter(membership__organisation=organisation, permission=perms.ORG_ADMIN)).values(
            "membership__user"
        )
    )


def organisations_of(user: User):
    """The organisations the user is an active member of (the ones they can switch to)."""
    return Organisation.objects.filter(memberships__user=user, memberships__ended_at__isnull=True, active=True).distinct()


def active_stewards():
    org = cpuc()
    if org is None:
        return Grant.objects.none()
    return _effective(Grant.objects.filter(membership__organisation=org, permission=perms.STEWARD))


# --- deciding


def can_grant(actor: User | None, user: User, organisation: Organisation, permission: str) -> tuple[bool, str]:
    """(allowed, reason refused) for actor granting permission to user in organisation."""
    if not perms.is_valid(permission):
        return False, f"{permission} is not a permission."
    if not organisation.active:
        return False, f"{organisation.code} is not active."
    if not user.is_active:
        return False, f"{user.email}'s account is deactivated."
    if permission in perms.CPUC_ONLY and organisation.kind != Organisation.Kind.CPUC:
        return False, f"{perms.label(permission)} exists only in the CPUC organisation."
    if permission == perms.PII and AccessPolicy.current().pii_requires_federated and not organisation.federated:
        return False, f"PII access needs a federated organisation, and {organisation.code} does not federate."
    if actor is None:
        return True, ""
    if actor.pk == user.pk:
        return False, "You cannot grant permissions to yourself."
    if permission in perms.CPUC_ONLY:
        return (True, "") if is_system_admin(actor) else (False, "Only a CPUC system administrator can grant this.")
    if permission == perms.ORG_ADMIN and not org_admins(organisation).exists():
        if is_system_admin(actor):
            return True, ""
        return False, f"{organisation.code} has no administrator yet; the CPUC assigns its first one."
    if is_org_admin(actor, organisation):
        return True, ""
    if permission == perms.ORG_ADMIN and is_system_admin(actor):
        return False, f"{organisation.code} already has an administrator; its administrators grant further ones."
    return False, f"Only {organisation.code}'s administrators can grant its permissions."


def _require_org_admin(actor: User | None, organisation: Organisation, what: str) -> None:
    if actor is None or is_org_admin(actor, organisation):
        return
    raise AccessDenied(f"Only {organisation.code}'s administrators can {what}.")


def _guard_last_steward(grant: Grant, what: str) -> None:
    if grant.permission == perms.STEWARD and grant.status == Grant.Status.ACTIVE and active_stewards().exclude(pk=grant.pk).count() == 0:
        raise AccessDenied(f"You cannot {what} the last data steward. Grant another steward first.")


# --- organisations and memberships


@transaction.atomic
def create_organisation(actor: User | None, *, code: str, name: str, kind: str, pa_type: str = "", federated: bool = False) -> Organisation:
    if actor is not None and not is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator can add an organisation.")
    org = Organisation.objects.create(code=code.strip().upper(), name=name.strip(), kind=kind, pa_type=pa_type, federated=federated)
    audit.record("organisation.created", obj=org, after=audit.snapshot(org), actor=actor)
    return org


@transaction.atomic
def ensure_membership(actor: User | None, user: User, organisation: Organisation) -> Membership:
    m = Membership.objects.filter(user=user, organisation=organisation, ended_at__isnull=True).first()
    if m is None:
        m = Membership.objects.create(user=user, organisation=organisation, started_by=actor)
        audit.record("membership.started", obj=m, after=audit.snapshot(m, ["user", "organisation"]), actor=actor)
    return m


@transaction.atomic
def end_membership(actor: User | None, membership: Membership, reason: str) -> Membership:
    """Demote a member to a community account: their grants are revoked with the membership; their own history stays."""
    _require_org_admin(actor, membership.organisation, "remove its members")
    if not reason.strip():
        raise AccessDenied("Give a reason for removing the member.")
    for g in membership.grants.filter(status__in=Grant.OPEN):
        _guard_last_steward(g, "remove")
    for g in membership.grants.filter(status__in=Grant.OPEN):
        _set_revoked(actor, g, reason)
    membership.ended_at, membership.ended_by, membership.end_reason = timezone.now(), actor, reason
    membership.save()
    audit.record("membership.ended", obj=membership, reason=reason, actor=actor)
    return membership


# --- grants


@transaction.atomic
def grant(actor: User | None, user: User, organisation: Organisation, permission: str, *, expires_at=None, reason: str = "") -> Grant:
    ok, why = can_grant(actor, user, organisation, permission)
    if not ok:
        raise AccessDenied(why)
    if permission == perms.STEWARD and expires_at is not None:
        raise AccessDenied("A data steward grant has no expiry; stewards are recertified instead.")
    membership = ensure_membership(actor, user, organisation)
    if membership.grants.filter(permission=permission, status__in=Grant.OPEN).exists():
        raise AccessDenied(f"{user.email} already has {perms.label(permission)} in {organisation.code}.")
    pending = permission == perms.PII and actor is not None
    g = Grant.objects.create(
        membership=membership,
        permission=permission,
        status=Grant.Status.PENDING if pending else Grant.Status.ACTIVE,
        granted_by=actor,
        grant_reason=reason,
        expires_at=expires_at,
    )
    audit.record("grant.created", obj=g, after=audit.snapshot(g, GRANT_FIELDS), reason=reason, actor=actor)
    return g


def _change(actor, g: Grant, action: str, reason: str = "", **fields) -> Grant:
    before = audit.snapshot(g, GRANT_FIELDS)
    for k, v in fields.items():
        setattr(g, k, v)
    g.save()
    audit.record(action, obj=g, before=before, after=audit.snapshot(g, GRANT_FIELDS), reason=reason, actor=actor)
    return g


def _set_revoked(actor, g: Grant, reason: str) -> Grant:
    status = Grant.Status.DECLINED if g.status == Grant.Status.PENDING else Grant.Status.REVOKED
    return _change(actor, g, "grant.revoked", reason, status=status, revoked_by=actor, revoked_at=timezone.now(), revoke_reason=reason)


@transaction.atomic
def revoke(actor: User | None, g: Grant, reason: str) -> Grant:
    g = Grant.objects.select_for_update().get(pk=g.pk)
    if g.status not in Grant.OPEN:
        raise AccessDenied("That grant is no longer open.")
    if g.permission in perms.CPUC_ONLY:
        if actor is not None and not is_system_admin(actor):
            raise AccessDenied("Only a CPUC system administrator can revoke this.")
    else:
        _require_org_admin(actor, g.organisation, "revoke its permissions")
    if not reason.strip():
        raise AccessDenied("Give a reason for revoking the permission.")
    _guard_last_steward(g, "revoke")
    return _set_revoked(actor, g, reason)


@transaction.atomic
def approve_pii(steward: User, g: Grant) -> Grant:
    if not is_steward(steward):
        raise AccessDenied("Only a data steward can approve PII access.")
    g = Grant.objects.select_for_update().get(pk=g.pk)
    if g.status != Grant.Status.PENDING:
        raise AccessDenied("That grant is not waiting for approval.")
    if g.membership.user_id == steward.pk:
        raise AccessDenied("You cannot approve your own PII access.")
    ok, why = can_grant(None, g.user, g.organisation, g.permission)
    if not ok:
        raise AccessDenied(why)
    return _change(steward, g, "grant.approved", status=Grant.Status.ACTIVE, approved_by=steward, approved_at=timezone.now())


@transaction.atomic
def decline_pii(steward: User, g: Grant, reason: str) -> Grant:
    if not is_steward(steward):
        raise AccessDenied("Only a data steward can decline PII access.")
    if g.status != Grant.Status.PENDING:
        raise AccessDenied("That grant is not waiting for approval.")
    return _change(
        steward,
        g,
        "grant.declined",
        reason,
        status=Grant.Status.DECLINED,
        revoked_by=steward,
        revoked_at=timezone.now(),
        revoke_reason=reason,
    )


# --- access requests (D6)


@transaction.atomic
def request_access(user: User, organisation: Organisation, permission_codes: list[str], message: str = "") -> AccessRequest:
    bad = [p for p in permission_codes if not perms.is_valid(p) or p in perms.CPUC_ONLY]
    if bad:
        raise AccessDenied(f"These cannot be requested: {', '.join(bad)}.")
    if AccessRequest.objects.filter(user=user, organisation=organisation, status=AccessRequest.Status.PENDING).exists():
        raise AccessDenied(f"You already have a request waiting with {organisation.code}.")
    req = AccessRequest.objects.create(user=user, organisation=organisation, permissions=sorted(set(permission_codes)), message=message)
    audit.record("access_request.created", obj=req, after={"permissions": req.permissions, "message": message}, actor=user)
    return req


@transaction.atomic
def decide_request(
    actor: User, req: AccessRequest, approve: bool, note: str = "", permission_codes: list[str] | None = None
) -> AccessRequest:
    """Approve (granting the asked-for permissions, or the subset the admin picks) or decline a request. Only the organisation's
    admins decide; the CPUC is not in the path (D6)."""
    _require_org_admin(actor, req.organisation, "decide its access requests")
    if req.status != AccessRequest.Status.PENDING:
        raise AccessDenied("That request has already been decided.")
    if approve:
        chosen = req.permissions if permission_codes is None else [p for p in permission_codes if p in req.permissions]
        ensure_membership(actor, req.user, req.organisation)
        for code in chosen:
            grant(actor, req.user, req.organisation, code, reason=f"access request {req.pk}")
    req.status = AccessRequest.Status.APPROVED if approve else AccessRequest.Status.DECLINED
    req.decided_by, req.decided_at, req.decision_note = actor, timezone.now(), note
    req.save()
    audit.record(f"access_request.{req.status}", obj=req, reason=note, actor=actor)
    return req


@transaction.atomic
def withdraw_request(user: User, req: AccessRequest) -> AccessRequest:
    if req.user_id != user.pk:
        raise AccessDenied("Only the person who asked can withdraw a request.")
    if req.status != AccessRequest.Status.PENDING:
        raise AccessDenied("That request has already been decided.")
    req.status, req.decided_at = AccessRequest.Status.WITHDRAWN, timezone.now()
    req.save()
    audit.record("access_request.withdrawn", obj=req, actor=user)
    return req


# --- recertification


@transaction.atomic
def open_recertification(actor: User | None, organisation: Organisation, due_at) -> RecertificationCampaign:
    _require_org_admin(actor, organisation, "start its recertification")
    if organisation.recertifications.filter(closed_at__isnull=True).exists():
        raise AccessDenied(f"{organisation.code} already has a recertification open.")
    campaign = RecertificationCampaign.objects.create(organisation=organisation, opened_by=actor, due_at=due_at)
    grants = Grant.objects.filter(membership__organisation=organisation, status=Grant.Status.ACTIVE, membership__ended_at__isnull=True)
    RecertificationItem.objects.bulk_create([RecertificationItem(campaign=campaign, grant=g) for g in grants])
    audit.record("recertification.opened", obj=campaign, after={"due_at": due_at.isoformat(), "grants": grants.count()}, actor=actor)
    return campaign


@transaction.atomic
def certify(actor: User, item: RecertificationItem) -> RecertificationItem:
    _require_org_admin(actor, item.campaign.organisation, "certify its grants")
    if item.campaign.closed_at is not None:
        raise AccessDenied("That recertification is closed.")
    now = timezone.now()
    item.decision, item.decided_by, item.decided_at = RecertificationItem.Decision.CERTIFIED, actor, now
    item.save()
    _change(actor, item.grant, "grant.certified", certified_by=actor, certified_at=now)
    return item


@transaction.atomic
def remove_in_recertification(actor: User, item: RecertificationItem, reason: str) -> RecertificationItem:
    if item.campaign.closed_at is not None:
        raise AccessDenied("That recertification is closed.")
    revoke(actor, item.grant, reason)
    item.decision, item.decided_by, item.decided_at = RecertificationItem.Decision.REMOVED, actor, timezone.now()
    item.save()
    return item


@transaction.atomic
def close_due_recertifications(now=None) -> list[RecertificationCampaign]:
    """Close every campaign past its due date (a scheduled job). Grants nobody certified are suspended when the policy says so; the last
    data steward is never suspended (it is logged instead)."""
    now = now or timezone.now()
    suspend = AccessPolicy.current().recertification_auto_suspend
    closed = []
    for campaign in RecertificationCampaign.objects.select_for_update().filter(closed_at__isnull=True, due_at__lte=now):
        for item in campaign.items.filter(decision="").select_related("grant"):
            g = item.grant
            if not suspend or g.status != Grant.Status.ACTIVE:
                continue
            try:
                _guard_last_steward(g, "suspend")
            except AccessDenied as e:
                audit.record("grant.suspension_skipped", obj=g, reason=str(e), actor=None)
                continue
            _change(
                None,
                g,
                "grant.suspended",
                "not certified by the due date",
                status=Grant.Status.SUSPENDED,
                suspended_at=now,
                suspended_reason=f"not certified in the recertification due {campaign.due_at:%Y-%m-%d}",
            )
        campaign.closed_at = now
        campaign.save()
        audit.record("recertification.closed", obj=campaign, actor=None)
        closed.append(campaign)
    return closed


@transaction.atomic
def reinstate(actor: User, g: Grant, reason: str) -> Grant:
    """Restore a suspended grant (an admin certifying it late)."""
    _require_org_admin(actor, g.organisation, "reinstate its grants")
    if g.status != Grant.Status.SUSPENDED:
        raise AccessDenied("Only a suspended grant can be reinstated.")
    return _change(
        actor,
        g,
        "grant.reinstated",
        reason,
        status=Grant.Status.ACTIVE,
        suspended_at=None,
        suspended_reason="",
        certified_by=actor,
        certified_at=timezone.now(),
    )


# --- policy (decision G: two stewards for rules that change everyone's PII access)


@transaction.atomic
def set_policy(actor: User | None, setting: str, value, reason: str = "") -> AccessPolicy:
    policy = AccessPolicy.current()
    if setting in AccessPolicy.STEWARD_SETTINGS:
        raise AccessDenied("This setting takes one data steward to propose and a second to confirm.")
    if not hasattr(policy, setting) or setting in ("id", "updated_at"):
        raise AccessDenied(f"{setting} is not an access setting.")
    if actor is not None and not is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator can change access settings.")
    before = {setting: getattr(policy, setting)}
    setattr(policy, setting, value)
    policy.save()
    audit.record("access_policy.changed", obj=policy, before=before, after={setting: value}, reason=reason, actor=actor)
    return policy


@transaction.atomic
def propose_policy_change(steward: User, setting: str, value, reason: str) -> PolicyChange:
    if not is_steward(steward):
        raise AccessDenied("Only a data steward can propose this change.")
    if setting not in AccessPolicy.STEWARD_SETTINGS:
        raise AccessDenied(f"{setting} is not a steward-controlled setting.")
    if not reason.strip():
        raise AccessDenied("Give a reason for the change.")
    change = PolicyChange.objects.create(setting=setting, value=value, reason=reason, proposed_by=steward)
    audit.record("policy_change.proposed", obj=change, after={setting: value}, reason=reason, actor=steward)
    return change


@transaction.atomic
def confirm_policy_change(steward: User, change: PolicyChange) -> AccessPolicy:
    if not is_steward(steward):
        raise AccessDenied("Only a data steward can confirm this change.")
    change = PolicyChange.objects.select_for_update().get(pk=change.pk)
    if change.status != PolicyChange.Status.PROPOSED:
        raise AccessDenied("That change is no longer waiting for confirmation.")
    if change.proposed_by_id == steward.pk:
        raise AccessDenied("A second data steward must confirm a change you proposed.")
    policy = AccessPolicy.current()
    before = {change.setting: getattr(policy, change.setting)}
    setattr(policy, change.setting, change.value)
    policy.save()
    change.status, change.decided_by, change.decided_at = PolicyChange.Status.CONFIRMED, steward, timezone.now()
    change.save()
    audit.record(
        "access_policy.changed", obj=policy, before=before, after={change.setting: change.value}, reason=change.reason, actor=steward
    )
    return policy


@transaction.atomic
def withdraw_policy_change(steward: User, change: PolicyChange, reason: str = "") -> PolicyChange:
    if not is_steward(steward):
        raise AccessDenied("Only a data steward can withdraw this change.")
    if change.status != PolicyChange.Status.PROPOSED:
        raise AccessDenied("That change is no longer waiting for confirmation.")
    change.status, change.decided_by, change.decided_at = PolicyChange.Status.WITHDRAWN, steward, timezone.now()
    change.save()
    audit.record("policy_change.withdrawn", obj=change, reason=reason, actor=steward)
    return change

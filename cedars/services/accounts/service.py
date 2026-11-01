"""Access rules (BR-01, D6, decisions F and G). Every change goes through these functions, each writes an audit event, and a refused
action raises AccessDenied with a sentence the page can show as it is.

Who may grant what:
  - an organization's admins grant its module permissions and org.admin, never to themselves;
  - while an organization has no admin, only a CPUC system administrator can grant its first one (D6), and only then;
  - claims.pii granted by an organization's admin waits for any data steward's approval;
  - system.admin and pii.steward exist only in the CPUC organization and are granted by a system administrator;
  - the last active data steward cannot be revoked, suspended or removed.
`actor=None` is the system itself (the bootstrap command and scheduled jobs).
"""

import re
from dataclasses import dataclass
from datetime import date

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
    OrganisationCode,
    PolicyChange,
    RecertificationCampaign,
    RecertificationItem,
    RoleTemplate,
    User,
)

GRANT_FIELDS = [
    "membership",
    "permission",
    "status",
    "expires_at",
    "approved_by",
    "suspended_reason",
    "revoked_by",
    "revoke_reason",
    "role_template",
]


class AccessDenied(PermissionDenied):
    """A refused access change; str(e) is the plain-language reason."""


# Each PA code in CEDARS' Programs list (2026-10-03) and its type. A PA added under one of these codes is typed on creation (importers,
# legacy loads); any other code is left for a CPUC system administrator to type on its organization page. Migration
# accounts/0003_pa_types typed the PAs already in the platform from this table.
KNOWN_PA_TYPES = {
    # investor-owned utilities
    "PGE": "IOU",
    "SCE": "IOU",
    "SCG": "IOU",
    "SDGE": "IOU",
    # regional energy networks
    "BAY": "REN",  # Bay Area Regional Energy Network (BayREN)
    "CCR": "REN",  # Central California Rural Regional Energy Network
    "IREN": "REN",  # Inland Regional Energy Network
    "RRN": "REN",  # Rural Regional Energy Network
    "SCR": "REN",  # Southern California Regional Energy Network (SoCalREN)
    "SDRN": "REN",  # San Diego Regional Energy Network
    "TCR": "REN",  # Tri-County Regional Energy Network (3C-REN)
    # community choice aggregators
    "AVA": "CCA",  # Ava Community Energy
    "CPSF": "CCA",  # CleanPowerSF
    "EBCE": "CCA",  # East Bay Community Energy (now Ava Community Energy)
    "LCE": "CCA",  # Lancaster Choice Energy
    "MCE": "CCA",
    "PCE": "CCA",  # Peninsula Clean Energy
    "RCEA": "CCA",  # Redwood Coast Energy Authority
    "SCP": "CCA",  # Sonoma Clean Power
    "SJCE": "CCA",  # San José Clean Energy
}


# The names CEDARS gives those PAs (its Programs list). A PA created from a filed code alone (legacy loads, importers) gets its name
# from here instead of repeating the code; migration accounts/0006 named the ones already in the platform that way.
KNOWN_PA_NAMES = {
    "PGE": "Pacific Gas and Electric Company",
    "SCE": "Southern California Edison",
    "SCG": "Southern California Gas Company",
    "SDGE": "San Diego Gas & Electric",
    "BAY": "Bay Area Regional Energy Network (BayREN)",
    "CCR": "Central California Rural Regional Energy Network",
    "IREN": "Inland Regional Energy Network",
    "RRN": "Rural Regional Energy Network",
    "SCR": "Southern California Regional Energy Network (SoCalREN)",
    "SDRN": "San Diego Regional Energy Network",
    "TCR": "Tri-County Regional Energy Network (3C-REN)",
    "AVA": "Ava Community Energy",
    "CPSF": "CleanPowerSF",
    "EBCE": "East Bay Community Energy",
    "LCE": "Lancaster Choice Energy",
    "MCE": "MCE",
    "PCE": "Peninsula Clean Energy",
    "RCEA": "Redwood Coast Energy Authority",
    "SCP": "Sonoma Clean Power",
    "SJCE": "San José Clean Energy",
}


def known_pa_name(code: str) -> str:
    """The name CEDARS gives a PA code, or the code itself for one it does not list."""
    c = (code or "").strip().upper()
    return KNOWN_PA_NAMES.get(c, c)


def known_pa_type(code: str) -> str:
    """The type of a PA code CEDARS uses ("IOU", "REN" or "CCA"), or "" for a code not in KNOWN_PA_TYPES."""
    return KNOWN_PA_TYPES.get((code or "").strip().upper(), "")


def pa_types() -> dict[str, str]:
    """Every typed PA's code and its type, for grouping data by PA type (the dashboards' PA type filter and view)."""
    return dict(Organisation.objects.filter(kind=Organisation.Kind.PA).exclude(pa_type="").values_list("code", "pa_type"))


# --- organization codes and their history
#
# An organization keeps its identity (its primary key) whatever its code: a rename closes the old code and opens the new one on a date
# (OrganisationCode), and everything filed under either stays the organization's. A code is one organization's for good. Where the same
# party was recorded twice (an old code's data loaded before the rename was known), the old organization is merged into the new: it
# keeps its own data and codes, points at its successor, and is counted under it. Data keeps the code as filed (versions, not edits);
# `code_map` / `current_code` read any code as its party's current one, which is what the dashboards group and filter by.

# Renames CEDARS has made: former code -> (new code, new name, first day of the new code, where it is recorded). A legacy load or a PA
# added under either code finds the one organization; migration accounts/0004 applied them to the organizations already in the platform.
KNOWN_RENAMES = {
    "EBCE": (
        "AVA",
        "Ava Community Energy",
        date(2024, 1, 1),
        "As of 2024, East Bay Community Energy (EBCE) changed its name to Ava Community Energy (AVA): budget filings, claims and monthly "
        "reports before 2024 are under EBCE, from 2024 under AVA (CEDARS changelog, 2025-02-27)",
    ),
}
CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,19}$")


def _norm(code) -> str:
    return (code or "").strip().upper()


def party(org: Organisation | None) -> Organisation | None:
    """The organization a merged one now counts under (its successor's successor, and so on); the organization itself otherwise."""
    seen = set()
    while org is not None and org.successor_id and org.pk not in seen:
        seen.add(org.pk)
        org = org.successor
    return org


def by_code(code: str, *, follow: bool = True) -> Organisation | None:
    """The organization a code names, current or former (e.g. EBCE finds Ava Community Energy). follow=True (the default) goes on to
    the party a merged organization counts under; follow=False stops at the organization that holds the code."""
    c = _norm(code)
    if not c:
        return None
    org = Organisation.objects.filter(code=c).first()
    if org is None:
        row = OrganisationCode.objects.filter(code=c).select_related("organisation").first()
        org = row.organisation if row else None
    return party(org) if follow else org


def by_code_or_404(code: str, *, follow: bool = False) -> Organisation:
    """by_code for a page's URL (by default the organization holding the code, so a merged one's own page still opens)."""
    from django.http import Http404

    org = by_code(code, follow=follow)
    if org is None:
        raise Http404
    return org


def party_members(org: Organisation) -> list[Organisation]:
    """The organization and every organization merged into it, directly or through another: one party."""
    org = party(org)
    successors = dict(Organisation.objects.filter(successor__isnull=False).values_list("pk", "successor_id"))
    members = {org.pk}
    changed = True
    while changed:
        changed = False
        for pk, succ in successors.items():
            if succ in members and pk not in members:
                members.add(pk)
                changed = True
    return [org, *Organisation.objects.filter(pk__in=members - {org.pk}).order_by("code")]


def party_codes(org: Organisation) -> list[str]:
    """Every code the party has used: its current code first, then the rest by when they were last in use (newest first)."""
    members = party_members(org)
    rows = OrganisationCode.objects.filter(organisation__in=members).order_by("-valid_to")
    codes = [members[0].code, *(m.code for m in members[1:]), *(r.code for r in rows)]
    return list(dict.fromkeys(codes))


def code_map() -> dict[str, str]:
    """Every code that is not its party's current code -> the party's current code (former codes and merged organizations' codes),
    e.g. {"EBCE": "AVA"}. Empty when nothing was ever renamed or merged."""
    orgs = {pk: (code, succ) for pk, code, succ in Organisation.objects.values_list("pk", "code", "successor_id")}

    def current(pk):
        seen = set()
        while orgs[pk][1] and pk not in seen:
            seen.add(pk)
            pk = orgs[pk][1]
        return orgs[pk][0]

    out = {code: current(pk) for pk, (code, _) in orgs.items()}
    out |= {code: current(pk) for code, pk in OrganisationCode.objects.values_list("code", "organisation_id")}
    return {k: v for k, v in out.items() if k != v}


def current_code(code: str, mapping: dict[str, str] | None = None) -> str:
    """A code as its party's current code (EBCE -> AVA); any other code as given (stripped)."""
    mapping = code_map() if mapping is None else mapping
    return mapping.get(_norm(code), (code or "").strip())


def code_history(org: Organisation) -> list[OrganisationCode]:
    """The party's codes in order of use, merged organizations' included (an organization added before code history was kept shows its
    one code as an unsaved row)."""
    members = party_members(org)
    rows = list(OrganisationCode.objects.filter(organisation__in=members).select_related("organisation", "recorded_by"))
    held = {r.organisation_id for r in rows if r.valid_to is None}
    rows += [OrganisationCode(organisation=m, code=m.code) for m in members if m.pk not in held and not m.successor_id]
    return sorted(rows, key=lambda r: (r.valid_from or date.min, r.valid_to or date.max))


def code_on(org: Organisation, day: date) -> str:
    """The party's code in effect on a day (EBCE for 2023, AVA for 2024); its current code when the history does not say."""
    for r in code_history(org):
        if (r.valid_from is None or r.valid_from <= day) and (r.valid_to is None or day < r.valid_to):
            return r.code
    return party(org).code


def _current_row(org: Organisation) -> OrganisationCode:
    row = org.codes.filter(valid_to__isnull=True).first()
    if row is None:  # an organization added before code history was kept
        row = OrganisationCode.objects.create(organisation=org, code=_norm(org.code))
    return row


def _check_new_code(code: str, org: Organisation | None) -> str:
    c = _norm(code)
    if not CODE_PATTERN.match(c):
        raise AccessDenied("A code is 1 to 20 letters, digits, hyphens or underscores, starting with a letter or digit.")
    holder = by_code(c, follow=False)
    if holder is not None and (org is None or holder.pk != org.pk):
        when = "its current code" if holder.code == c else "a former code of theirs"
        raise AccessDenied(f"{c} is {when}: {holder.code} ({holder.name}). A code stays with one organization for good.")
    return c


def _require_system_admin(actor: User | None, what: str) -> None:
    if actor is not None and not is_system_admin(actor):
        raise AccessDenied(f"Only a CPUC system administrator can {what}.")


def _effective_day(effective, after: date | None, what: str) -> date:
    day = effective or timezone.localdate()
    if day > timezone.localdate():
        raise AccessDenied(f"Record {what} once it has taken effect: the date cannot be in the future.")
    if after is not None and day <= after:
        raise AccessDenied(f"The date must be after {after:%Y-%m-%d}, when the current code came into use.")
    return day


@transaction.atomic
def rename_organisation(
    actor: User | None, org: Organisation, *, code: str, reason: str, effective: date | None = None, name: str = ""
) -> Organisation:
    """Give an organization a new code from a date (default today), and optionally a new name (a CPUC system administrator, with a
    reason). The old code is kept in its history and still finds it; nothing it filed changes."""
    _require_system_admin(actor, "rename an organization")
    if not reason.strip():
        raise AccessDenied("Give a reason for the rename.")
    if org.successor_id:
        raise AccessDenied(f"{org.code} was merged into {org.successor.code}; rename {org.successor.code} instead.")
    new = _check_new_code(code, org)
    if new == org.code:
        raise AccessDenied(f"{org.code} is already its code.")
    current = _current_row(org)
    day = _effective_day(effective, current.valid_from, "a rename")
    before = audit.snapshot(org, ["code", "name"])
    current.valid_to = day
    current.save(update_fields=["valid_to"])
    OrganisationCode.objects.create(organisation=org, code=new, valid_from=day, reason=reason.strip(), recorded_by=actor)
    org.code = new
    if name.strip():
        org.name = name.strip()
    org.save(update_fields=["code", "name"])
    after = audit.snapshot(org, ["code", "name"]) | {"former_code": before["code"], "effective": day.isoformat()}
    audit.record("organisation.renamed", obj=org, before=before, after=after, reason=reason, actor=actor)
    return org


@transaction.atomic
def add_former_code(
    actor: User | None, org: Organisation, *, code: str, until: date, reason: str, since: date | None = None
) -> OrganisationCode:
    """Record a code an organization used before, from `since` (blank: from the start) up to but not including `until`, so records
    filed under it count as the organization's (a CPUC system administrator, with a reason). A current code with no start then starts
    on `until`."""
    _require_system_admin(actor, "change an organization's codes")
    if not reason.strip():
        raise AccessDenied("Give a reason for the former code.")
    if org.successor_id:
        raise AccessDenied(f"{org.code} was merged into {org.successor.code}; record it on {org.successor.code} instead.")
    c = _check_new_code(code, org)
    if c == org.code:
        raise AccessDenied(f"{c} is its current code.")
    if since is not None and since >= until:
        raise AccessDenied("A former code's first day must be before its last.")
    if until > timezone.localdate():
        raise AccessDenied("A former code must have ended by today.")
    current = _current_row(org)
    if current.valid_from is not None and current.valid_from < until:
        raise AccessDenied(f"{org.code} has been in use since {current.valid_from:%Y-%m-%d}; a former code must end by then.")
    for r in org.codes.filter(valid_to__isnull=False):
        if (r.valid_from is None or r.valid_from < until) and (since is None or r.valid_to > since):
            raise AccessDenied(f"{r.code} was {org.code}'s code {r.span}; the two would overlap.")
    before = {"codes": [str(r) for r in code_history(org)]}
    if current.valid_from is None:
        current.valid_from = until
        current.save(update_fields=["valid_from"])
    row = OrganisationCode.objects.create(
        organisation=org, code=c, valid_from=since, valid_to=until, reason=reason.strip(), recorded_by=actor
    )
    after = {"codes": [str(r) for r in code_history(org)]}
    audit.record("organisation.code_added", obj=org, before=before, after=after, reason=reason, actor=actor)
    return row


@transaction.atomic
def merge_organisation(
    actor: User | None, source: Organisation, *, into: Organisation, reason: str, effective: date | None = None
) -> Organisation:
    """Record that `source` and `into` are the same party under an old and a new code (a CPUC system administrator, with a reason):
    `source`'s codes end on `effective` (default `into`'s first day under its code, else today), it becomes inactive and points at
    `into`, and its data, which stays its own, is counted under `into`. Its members must be moved or removed first."""
    _require_system_admin(actor, "merge organizations")
    if not reason.strip():
        raise AccessDenied("Give a reason for the merge.")
    into = party(into)
    if source.pk == into.pk:
        raise AccessDenied("An organization cannot be merged into itself.")
    if source.successor_id:
        raise AccessDenied(f"{source.code} was already merged into {source.successor.code}.")
    if source.kind != into.kind or source.kind == Organisation.Kind.CPUC:
        raise AccessDenied("Only two organizations of the same kind can be merged, and never the CPUC.")
    if source.memberships.filter(ended_at__isnull=True).exists():
        raise AccessDenied(f"{source.code} still has members. Move them to {into.code} or remove them first.")
    source_row, into_row = _current_row(source), _current_row(into)
    day = effective or into_row.valid_from or timezone.localdate()
    day = _effective_day(day, source_row.valid_from, "a merge")
    if into_row.valid_from is not None and into_row.valid_from > day:
        raise AccessDenied(f"{into.code} has been in use only since {into_row.valid_from:%Y-%m-%d}; the merge cannot take effect before.")
    before = {"source": audit.snapshot(source, ["code", "active"]), "into_codes": [str(r) for r in code_history(into)]}
    source_row.valid_to = day
    source_row.save(update_fields=["valid_to"])
    if into_row.valid_from is None:
        into_row.valid_from = day
        into_row.save(update_fields=["valid_from"])
    source.successor, source.merged_on, source.active = into, day, False
    source.save(update_fields=["successor", "merged_on", "active"])
    after = {
        "source": audit.snapshot(source, ["code", "active"]) | {"successor": into.code, "merged_on": day.isoformat()},
        "into_codes": [str(r) for r in code_history(into)],
    }
    audit.record("organisation.merged", obj=source, before=before, after=after, reason=reason, actor=actor)
    audit.record("organisation.merged", obj=into, before=before, after=after, reason=reason, actor=actor)
    return source


@transaction.atomic
def organisation_for_filed_code(code: str, actor: User | None = None) -> Organisation:
    """The program administrator a filed code names (legacy loads, importers): the party that has or had the code; for a code in
    KNOWN_RENAMES, the one organization under its old or new code (renamed or given the former code as needed); otherwise a new PA
    under the code (typed when CEDARS uses it). `actor` (a CPUC system administrator running an import, or None for the system) is
    recorded on what it adds."""
    c = _norm(code)
    org = by_code(c)
    if org is not None:
        return org
    old = c if c in KNOWN_RENAMES else next((o for o, (n, *_) in KNOWN_RENAMES.items() if n == c), None)
    if old is None:
        return create_organisation(actor, code=c, name=known_pa_name(c), kind=Organisation.Kind.PA)
    new, name, first_day, source = KNOWN_RENAMES[old]
    target = by_code(new) if c == old else by_code(old)
    if target is None:
        target = create_organisation(actor, code=new, name=name, kind=Organisation.Kind.PA)
        add_former_code(actor, target, code=old, until=first_day, reason=source)
    elif c == old:  # the new organization is here; the old code was never recorded
        add_former_code(actor, target, code=old, until=first_day, reason=source)
    else:  # the old organization is here; the rename was never recorded
        rename_organisation(actor, target, code=new, reason=source, effective=first_day, name=name if target.name == old else "")
    return target


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
    """Whether the user holds the permission in the organization now. claims.pii also checks the session's assurance when the CPUC
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


def holders(organisation: Organisation, permission: str):
    """The users who hold the permission in the organization now."""
    return User.objects.filter(
        pk__in=_effective(Grant.objects.filter(membership__organisation=organisation, permission=permission)).values("membership__user"),
        is_active=True,
    )


def org_admins(organisation: Organisation):
    return holders(organisation, perms.ORG_ADMIN)


def organisations_of(user: User):
    """The organizations the user is an active member of (the ones they can switch to)."""
    return Organisation.objects.filter(memberships__user=user, memberships__ended_at__isnull=True, active=True).distinct()


def active_stewards():
    org = cpuc()
    if org is None:
        return Grant.objects.none()
    return _effective(Grant.objects.filter(membership__organisation=org, permission=perms.STEWARD))


# --- deciding


def can_grant(actor: User | None, user: User, organisation: Organisation, permission: str) -> tuple[bool, str]:
    """(allowed, reason refused) for actor granting permission to user in organization."""
    if not perms.is_valid(permission):
        return False, f"{permission} is not a permission."
    if not organisation.active:
        return False, f"{organisation.code} is not active."
    if not user.is_active:
        return False, f"{user.email}'s account is deactivated."
    if permission in perms.CPUC_ONLY and organisation.kind != Organisation.Kind.CPUC:
        return False, f"{perms.label(permission)} exists only in the CPUC organization."
    if permission == perms.PII and AccessPolicy.current().pii_requires_federated and not organisation.federated:
        return False, f"PII access needs a federated organization, and {organisation.code} does not federate."
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


# --- organizations and memberships


@transaction.atomic
def create_organisation(actor: User | None, *, code: str, name: str, kind: str, pa_type: str = "", federated: bool = False) -> Organisation:
    if actor is not None and not is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator can add an organization.")
    code = _check_new_code(code, None)
    if kind == Organisation.Kind.PA and not pa_type:
        pa_type = known_pa_type(code)  # a CEDARS code is typed as CEDARS has it; any other is left for the CPUC to type
    org = Organisation.objects.create(code=code, name=name.strip(), kind=kind, pa_type=pa_type, federated=federated)
    OrganisationCode.objects.create(organisation=org, code=code, recorded_by=actor)
    audit.record("organisation.created", obj=org, after=audit.snapshot(org), actor=actor)
    return org


ORGANISATION_DETAILS = ["name", "pa_type"]


@transaction.atomic
def change_organisation(actor: User | None, org: Organisation, *, reason: str, **changes) -> Organisation:
    """Change an organization's name or PA type (a CPUC system administrator, with a reason). A PA type is required of a program
    administrator and refused for any other kind of organization."""
    if actor is not None and not is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator can change an organization's details.")
    unknown = set(changes) - set(ORGANISATION_DETAILS)
    if unknown:
        raise AccessDenied(f"{', '.join(sorted(unknown))} cannot be changed here.")
    if not reason.strip():
        raise AccessDenied("Give a reason for the change.")
    pa_type = changes.get("pa_type", org.pa_type)
    if org.kind == Organisation.Kind.PA and pa_type not in Organisation.PAType.values:
        raise AccessDenied("A program administrator needs a PA type: IOU, REN or CCA.")
    if org.kind != Organisation.Kind.PA and pa_type:
        raise AccessDenied("Only a program administrator has a PA type.")
    if "name" in changes and not changes["name"].strip():
        raise AccessDenied("The organization needs a name.")
    before = audit.snapshot(org, ORGANISATION_DETAILS)
    for k, v in changes.items():
        setattr(org, k, v.strip() if isinstance(v, str) else v)
    after = audit.snapshot(org, ORGANISATION_DETAILS)
    if after == before:
        return org
    org.save(update_fields=list(changes))
    audit.record("organisation.changed", obj=org, before=before, after=after, reason=reason, actor=actor)
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
def grant(
    actor: User | None,
    user: User,
    organisation: Organisation,
    permission: str,
    *,
    expires_at=None,
    reason: str = "",
    role_template: RoleTemplate | None = None,
) -> Grant:
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
        role_template=role_template,
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
def approve_pii(steward: User, g: Grant, reason: str = "") -> Grant:
    """A data steward's approval of PII access; the reason, if given, goes to the audit trail (every change audited)."""
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
    return _change(steward, g, "grant.approved", reason, status=Grant.Status.ACTIVE, approved_by=steward, approved_at=timezone.now())


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


# --- role templates (BR-01.3)

ROLE_TEMPLATE_FIELDS = ["name", "description", "permissions", "kinds", "active"]


def _template_values(name: str, permissions: list[str], kinds: list[str]) -> tuple[str, list[str], list[str]]:
    """The name, permissions and kinds checked and put in catalog order. A template bundles module permissions only: an organization's
    first administrator, PII access and the CPUC's own roles each need a decision of their own (D6, decision G), so they are granted
    one by one."""
    name = (name or "").strip()
    if not name:
        raise AccessDenied("Give the role template a name.")
    if len(name) > 80:
        raise AccessDenied("A role template's name has at most 80 characters.")
    catalog = perms.module_permissions()
    wrong = sorted(set(permissions) - set(catalog))
    if wrong:
        raise AccessDenied(f"A role template bundles module permissions only; {', '.join(wrong)} cannot be in one (grant it on its own).")
    if not permissions:
        raise AccessDenied("Choose at least one permission for the role template.")
    bad_kinds = sorted(set(kinds) - set(Organisation.Kind.values))
    if bad_kinds:
        raise AccessDenied(f"Not a kind of organization: {', '.join(bad_kinds)}.")
    return name, [c for c in catalog if c in set(permissions)], [k for k in Organisation.Kind.values if k in set(kinds)]


def _require_template_admin(actor: User | None) -> None:
    if actor is not None and not is_system_admin(actor):
        raise AccessDenied("Only a CPUC system administrator can define role templates.")


@transaction.atomic
def create_role_template(
    actor: User | None, *, name: str, permissions: list[str], description: str = "", kinds: list[str] = (), reason: str = ""
) -> RoleTemplate:
    _require_template_admin(actor)
    name, permissions, kinds = _template_values(name, list(permissions), list(kinds))
    if RoleTemplate.objects.filter(name__iexact=name).exists():
        raise AccessDenied(f"There is already a role template named {name}.")
    t = RoleTemplate.objects.create(name=name, description=description.strip(), permissions=permissions, kinds=kinds, created_by=actor)
    audit.record("role_template.created", obj=t, after=audit.snapshot(t, ROLE_TEMPLATE_FIELDS), reason=reason, actor=actor)
    return t


@transaction.atomic
def change_role_template(actor: User | None, t: RoleTemplate, *, reason: str, **changes) -> RoleTemplate:
    """Change a template's name, description, permissions or kinds, or retire it (active=False) or offer it again, with a reason. The
    grants it has already made do not change: a member who needs fewer permissions has them revoked one by one."""
    _require_template_admin(actor)
    unknown = set(changes) - set(ROLE_TEMPLATE_FIELDS)
    if unknown:
        raise AccessDenied(f"{', '.join(sorted(unknown))} cannot be changed here.")
    if not reason.strip():
        raise AccessDenied("Give a reason for the change.")
    t = RoleTemplate.objects.select_for_update().get(pk=t.pk)
    name, permissions, kinds = _template_values(
        changes.get("name", t.name), list(changes.get("permissions", t.permissions)), list(changes.get("kinds", t.kinds))
    )
    if RoleTemplate.objects.filter(name__iexact=name).exclude(pk=t.pk).exists():
        raise AccessDenied(f"There is already a role template named {name}.")
    before = audit.snapshot(t, ROLE_TEMPLATE_FIELDS)
    t.name, t.permissions, t.kinds = name, permissions, kinds
    if "description" in changes:
        t.description = changes["description"].strip()
    if "active" in changes:
        t.active = bool(changes["active"])
    after = audit.snapshot(t, ROLE_TEMPLATE_FIELDS)
    if after == before:
        return t
    t.save()
    action = "role_template.changed"
    if before["active"] != after["active"]:
        action = "role_template.retired" if not t.active else "role_template.reinstated"
    audit.record(action, obj=t, before=before, after=after, reason=reason, actor=actor)
    return t


def role_templates_for(organisation: Organisation) -> list[RoleTemplate]:
    """The templates offered when granting in this organization (active, and for its kind)."""
    return [t for t in RoleTemplate.objects.filter(active=True) if t.offered_for(organisation)]


@dataclass
class TemplateResult:
    template: RoleTemplate
    granted: list[Grant]
    already: list[str]  # permissions the member already held (left as they are)

    @property
    def pending(self) -> list[Grant]:
        return [g for g in self.granted if g.status == Grant.Status.PENDING]


@transaction.atomic
def apply_role_template(
    actor: User | None, user: User, organisation: Organisation, template: RoleTemplate, *, expires_at=None, reason: str = ""
) -> TemplateResult:
    """Grant a member every permission of a role template at once (making them a member if they are not one). The actor must be able to
    grant each one, or nothing is granted; permissions the member already holds are left as they are. One grant per permission, each
    recording the template, and one role_template.applied event naming them."""
    template = RoleTemplate.objects.get(pk=template.pk)  # as it stands now, not as the caller last read it
    if not template.offered_for(organisation):
        why = "is retired" if not template.active else f"is not offered for {organisation.get_kind_display().lower()} organizations"
        raise AccessDenied(f"The role template {template.name} {why}.")
    membership = Membership.objects.filter(user=user, organisation=organisation, ended_at__isnull=True).first()
    held = set(membership.grants.filter(status__in=Grant.OPEN).values_list("permission", flat=True)) if membership else set()
    wanted = [p for p in template.permissions if p not in held]
    refused = []
    for p in wanted:
        ok, why = can_grant(actor, user, organisation, p)
        if not ok:
            refused.append(why)
    if refused:
        raise AccessDenied(" ".join(dict.fromkeys(refused)))
    if not wanted:
        raise AccessDenied(f"{user.email} already has every permission of {template.name} in {organisation.code}.")
    text = reason.strip() or f"role template {template.name}"
    granted = [grant(actor, user, organisation, p, expires_at=expires_at, reason=text, role_template=template) for p in wanted]
    membership = granted[0].membership
    audit.record(
        "role_template.applied",
        obj=membership,
        after={
            "template": template.name,
            "template_id": template.pk,
            "granted": [g.permission for g in granted],
            "already_held": sorted(held & set(template.permissions)),
        },
        reason=reason,
        organisation=organisation,
        actor=actor,
    )
    return TemplateResult(template, granted, sorted(held & set(template.permissions)))


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
    """Approve (granting the asked-for permissions, or the subset the admin picks) or decline a request. Only the organization's
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

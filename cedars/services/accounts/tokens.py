"""API tokens (decision J): made by members for their own systems, revoked by them or their organization's admins, every change
audited. A key reads `cedars_<prefix>_<secret>`; only its SHA-256 is stored, so a lost key is revoked and replaced, never shown again."""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cedars.services import audit

from . import permissions as perms
from .models import ApiToken, Membership, Organisation, User
from .service import AccessDenied, has_permission, is_org_admin, is_system_admin

KEY_START = "cedars_"
LAST_USED_EVERY = timedelta(minutes=5)  # last_used_at is written at most this often, not on every call


def max_days() -> int:
    return int(getattr(settings, "CEDARS_API_TOKEN_MAX_DAYS", 365))


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@transaction.atomic
def create_token(user: User, organisation: Organisation | None, *, name: str, scopes: list[str], days: int) -> tuple[ApiToken, str]:
    """(token, key) for the user's own use. The key is returned once and never stored."""
    if not name.strip():
        raise AccessDenied("Give the token a name, e.g. the system that will use it.")
    unknown = sorted(set(scopes) - set(perms.MODULES))
    if unknown or not scopes:
        raise AccessDenied("Choose at least one module." if not scopes else f"Not modules: {', '.join(unknown)}.")
    if not 1 <= days <= max_days():
        raise AccessDenied(f"A token lasts between 1 and {max_days()} days.")
    if organisation is not None:
        if not Membership.objects.filter(user=user, organisation=organisation, ended_at__isnull=True).exists():
            raise AccessDenied(f"You are not a member of {organisation.code}.")
    elif scopes != ["calculations"]:
        raise AccessDenied("A token without an organization can reach only your own calculations.")
    prefix = secrets.token_hex(4)
    key = f"{KEY_START}{prefix}_{secrets.token_urlsafe(32)}"
    token = ApiToken.objects.create(
        user=user,
        organisation=organisation,
        name=name.strip(),
        prefix=prefix,
        key_sha256=_hash(key),
        scopes=sorted(set(scopes)),
        expires_at=timezone.now() + timedelta(days=days),
    )
    audit.record(
        "api_token.created",
        obj=token,
        organisation=organisation,
        after={"name": token.name, "prefix": prefix, "scopes": token.scopes, "expires_at": token.expires_at.isoformat()},
        actor=user,
    )
    return token, key


def can_revoke(actor: User, token: ApiToken) -> bool:
    if token.user_id == actor.pk or is_system_admin(actor):
        return True
    return token.organisation is not None and is_org_admin(actor, token.organisation)


@transaction.atomic
def revoke_token(actor: User, token: ApiToken, reason: str = "") -> ApiToken:
    if not can_revoke(actor, token):
        raise AccessDenied("Only the token's owner, their organization's admins or the CPUC can revoke it.")
    token = ApiToken.objects.select_for_update().get(pk=token.pk)
    if token.revoked_at:
        raise AccessDenied("That token is already revoked.")
    token.revoked_at, token.revoked_by, token.revoke_reason = timezone.now(), actor, reason
    token.save()
    audit.record("api_token.revoked", obj=token, organisation=token.organisation, reason=reason, actor=actor)
    return token


def authenticate(key: str) -> ApiToken | None:
    """The active token for a key, or None: unknown, revoked, expired, its user inactive, or its membership or organization ended."""
    if not key.startswith(KEY_START):
        return None
    token = ApiToken.objects.select_related("user", "organisation").filter(key_sha256=_hash(key)).first()
    now = timezone.now()
    if token is None or token.revoked_at or token.expires_at <= now or not token.user.is_active:
        return None
    if token.organisation is not None and not (
        token.organisation.active
        and Membership.objects.filter(user=token.user, organisation=token.organisation, ended_at__isnull=True).exists()
    ):
        return None
    if token.last_used_at is None or now - token.last_used_at > LAST_USED_EVERY:
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
    return token


def token_allows(token: ApiToken, permission: str) -> bool:
    """Whether a call made with the token may use a module permission: the user holds it in the token's organization now and the
    token's scopes include its module. Special permissions (PII, admin) are never reachable with a token."""
    module, _, _ = permission.partition(".")
    if permission in perms.SPECIAL or module not in token.scopes or token.organisation is None:
        return False
    return has_permission(token.user, token.organisation, permission)

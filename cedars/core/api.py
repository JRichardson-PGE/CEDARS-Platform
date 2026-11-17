"""What every API view shares: who the call acts for and whether it may use a permission. A call made with an API token acts for the
token's organization and reaches only its scopes; a session call acts for the session's active organization."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.plumbing import build_bearer_security_scheme_object
from rest_framework import permissions

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service as access
from cedars.services.accounts import tokens
from cedars.services.accounts.models import ApiToken


def token_of(request) -> ApiToken | None:
    return request.auth if isinstance(request.auth, ApiToken) else None


def organisation(request):
    return getattr(request._request, "organisation", None)


def allowed(request, permission: str) -> bool:
    """Whether this call may use a module permission in the organization it acts for."""
    token = token_of(request)
    if token is not None:
        return tokens.token_allows(token, permission)
    return access.has_permission(request.user, organisation(request), permission, getattr(request._request, "assurance", None))


def permissions_of(request) -> list[str]:
    token = token_of(request)
    org = organisation(request)
    held = access.effective_permissions(request.user, org) if org is not None else set()
    if token is not None:
        held = {p for p in held if p not in perms.SPECIAL and p.partition(".")[0] in token.scopes}
    return sorted(held)


class ModulePermission(permissions.BasePermission):
    """Use as `permission_classes = [ModulePermission.of("calculations.read")]`."""

    permission = ""

    @classmethod
    def of(cls, permission: str):
        return type(f"Requires_{permission.replace('.', '_')}", (cls,), {"permission": permission})

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and allowed(request, self.permission))


class TokenScheme(OpenApiAuthenticationExtension):
    target_class = "cedars.services.accounts.api_auth.TokenAuthentication"
    name = "token"

    def get_security_definition(self, auto_schema):
        return build_bearer_security_scheme_object(header_name="Authorization", token_prefix="Bearer")

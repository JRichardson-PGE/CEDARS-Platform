from django.conf import settings
from django.shortcuts import redirect

from .assurance import MFA, PASSWORD, session_assurance
from .service import organisations_of

ORG_SESSION_KEY = "cedars_organisation"


class ActiveOrganisationMiddleware:
    """request.organisation: the organization the user is acting for, switched in the session (BR-01.1), always one they are an active
    member of (None for a community account). request.assurance: how the session was established (assurance.py)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organisation = None
        request.assurance = ""
        request.user_organisations = []
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            request.assurance = session_assurance(request)
            orgs = list(organisations_of(user))
            chosen = request.session.get(ORG_SESSION_KEY)
            request.organisation = next((o for o in orgs if o.pk == chosen), orgs[0] if orgs else None)
            request.user_organisations = orgs
        return self.get_response(request)


def mfa_pending(request) -> bool:
    """True when the platform requires a second factor (CEDARS_REQUIRE_MFA) and this session was signed in with a password alone.
    The middleware below and the page header (core.context_processors) both ask this, so a laptop demo that turns the
    requirement off gets the full header too, not a "finish setting up" notice with nothing to finish."""
    user = getattr(request, "user", None)
    return (
        getattr(settings, "CEDARS_REQUIRE_MFA", True)
        and user is not None
        and user.is_authenticated
        and getattr(request, "assurance", "") == PASSWORD
    )


class RequireMFAMiddleware:
    """MFA for every account (decision F's baseline): a session signed in with a password alone reaches only the sign-in and
    second-factor pages until the user sets up a second factor, or proves the one they have."""

    OPEN_PREFIXES = ("/accounts/", "/static/", "/healthz")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if mfa_pending(request) and request.path not in ("/", "/about/") and not request.path.startswith(self.OPEN_PREFIXES):
            from allauth.mfa.utils import is_mfa_enabled

            target = "mfa_reauthenticate" if is_mfa_enabled(request.user) else "mfa_activate_totp"
            return redirect(f"{_url(target)}?next={request.get_full_path()}")
        return self.get_response(request)


def _url(name):
    from django.urls import reverse

    return reverse(name)


__all__ = ["ActiveOrganisationMiddleware", "RequireMFAMiddleware", "mfa_pending", "MFA", "ORG_SESSION_KEY"]

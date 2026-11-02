from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect

from .assurance import MFA, PASSWORD, session_assurance
from .service import filing_organisation, organisations_of

ORG_SESSION_KEY = "cedars_organisation"


class ActiveOrganisationMiddleware:
    """request.organisation: the organization the user is acting for, switched in the session (BR-01.1), always one they are an active
    member of (None for a community account). request.filing_organisation: the same when it files (a PA), else None, for the pages
    that show "your" filings, budgets and reports (service.filing_organisation). request.assurance: how the session was established
    (assurance.py)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organisation = None
        request.filing_organisation = None
        request.assurance = ""
        request.user_organisations = []
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            request.assurance = session_assurance(request)
            orgs = list(organisations_of(user))
            chosen = request.session.get(ORG_SESSION_KEY)
            request.organisation = next((o for o in orgs if o.pk == chosen), orgs[0] if orgs else None)
            request.user_organisations = orgs
            request.filing_organisation = filing_organisation(request.organisation)
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
    second-factor pages (and Help) until the user sets up a second factor, or proves the one they have. Every other page, public ones
    included, sends it to the set-up page: straight after signing in without a word (accounts.signals marks the sign-in), and after
    that with an error saying the second factor comes first (Jake, 2026-10-31). Runs after MessageMiddleware, which the error needs."""

    OPEN_PREFIXES = ("/accounts/", "/static/", "/healthz", "/help/")  # Help stays open: it explains the set-up
    FIRST_STOP = "cedars_mfa_first_stop"  # session key: signed in, not yet sent to the set-up page
    MESSAGE = "Set up your second factor first. CEDARS opens its other pages once your authenticator app is added."

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not mfa_pending(request):
            return self.get_response(request)
        if request.path.startswith(self.OPEN_PREFIXES):
            if request.path.startswith("/accounts/2fa/"):
                request.session.pop(self.FIRST_STOP, None)  # on the set-up pages: from here on, leaving them is an error
            return self.get_response(request)
        from allauth.mfa.utils import is_mfa_enabled

        if not request.session.pop(self.FIRST_STOP, False):
            from django.contrib import messages

            messages.error(request, self.MESSAGE)
        target = "mfa_reauthenticate" if is_mfa_enabled(request.user) else "mfa_activate_totp"
        return redirect(f"{_url(target)}?{urlencode({'next': request.get_full_path()})}")


def _url(name):
    from django.urls import reverse

    return reverse(name)


__all__ = ["ActiveOrganisationMiddleware", "RequireMFAMiddleware", "mfa_pending", "MFA", "ORG_SESSION_KEY"]

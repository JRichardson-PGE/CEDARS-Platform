"""Second-factor set-up without signing in three times (UAT F11).

django-allauth asks a session to reauthenticate before it may add an authenticator, and counts only a record marked `reauthenticated`
as recent, so a person who had just typed their password to sign in was asked for it again at once. The password they gave moments ago
proves the same thing: the sign-in record is marked as a reauthentication, within allauth's own timeout (ACCOUNT_REAUTHENTICATION_TIMEOUT).

Activating an authenticator proves possession of it (the form verified a code it produced), so the activation is recorded as a
second-factor authentication: the session is MFA-assured at once (accounts.assurance), instead of being sent to "confirm access" with
another code from the app it has just set up."""

from allauth.account.authentication import get_authentication_records
from allauth.account.internal.flows.login import record_authentication  # allauth keeps the writer internal; the reader is public
from allauth.account.signals import user_logged_in
from allauth.mfa.models import Authenticator
from allauth.mfa.signals import authenticator_added
from django.dispatch import receiver


@receiver(user_logged_in)
def a_fresh_password_counts_as_recent(sender, request, user, **kwargs):
    records = get_authentication_records(request, user)
    last = records[-1] if records else None
    if last and last.get("method") == "password" and not last.get("reauthenticated"):
        record_authentication(request, user, "password", reauthenticated=True)


@receiver(authenticator_added)
def an_activated_authenticator_is_a_second_factor(sender, request, user, authenticator, **kwargs):
    if authenticator.type in (Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN) and request is not None:
        record_authentication(request, user, "mfa", id=authenticator.pk, type=authenticator.type)

"""Second-factor set-up without signing in three times (UAT F11).

django-allauth asks a session to reauthenticate before it may add an authenticator, and counts only a record marked `reauthenticated`
as recent, so a person who had just typed their password to sign in was asked for it again at once. The password they gave moments ago
proves the same thing: the sign-in record is marked as a reauthentication, within allauth's own timeout (ACCOUNT_REAUTHENTICATION_TIMEOUT).

Activating an authenticator proves possession of it (the form verified a code it produced), so the activation is recorded as a
second-factor authentication: the session is MFA-assured at once (accounts.assurance), instead of being sent to "confirm access" with
another code from the app it has just set up.

A person's email address changes on allauth's email page (ACCOUNT_CHANGE_EMAIL: the new address takes over once verified). Every change
is audited, the old address is told, and the person's other sessions end (the receivers at the end)."""

from allauth.account.authentication import get_authentication_records
from allauth.account.internal.flows.login import record_authentication  # allauth keeps the writer internal; the reader is public
from allauth.account.signals import email_changed, user_logged_in
from allauth.mfa.models import Authenticator
from allauth.mfa.signals import authenticator_added
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import User


@receiver(user_logged_in)
def a_fresh_password_counts_as_recent(sender, request, user, **kwargs):
    records = get_authentication_records(request, user)
    last = records[-1] if records else None
    if last and last.get("method") == "password" and not last.get("reauthenticated"):
        record_authentication(request, user, "password", reauthenticated=True)


@receiver(user_logged_in)
def a_password_sign_in_goes_to_the_set_up_first(sender, request, user, **kwargs):
    """The first page after a password-only sign-in sends the person to the second-factor set-up without an error
    (RequireMFAMiddleware); only leaving the set-up page after that shows one."""
    from .assurance import PASSWORD, session_assurance
    from .middleware import RequireMFAMiddleware

    if session_assurance(request) == PASSWORD:
        request.session[RequireMFAMiddleware.FIRST_STOP] = True


@receiver(authenticator_added)
def an_activated_authenticator_is_a_second_factor(sender, request, user, authenticator, **kwargs):
    if authenticator.type in (Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN) and request is not None:
        record_authentication(request, user, "mfa", id=authenticator.pk, type=authenticator.type)


# --- a person's email address (Jake 2026-11-11)


def ensure_email_record(user) -> None:
    """Give an account allauth's record of its email address when it has none: an account made by the platform's operators (the
    bootstrap, a demo account) rather than through sign-up, whose address they set. Without it allauth's email page would take the
    unrecorded address for an unverified change and drop it. A sign-up always has its record (verified before it can sign in)."""
    from allauth.account.models import EmailAddress

    if user.email and not EmailAddress.objects.filter(user=user).exists():
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)


@receiver(user_logged_in)
def an_account_has_its_email_record(sender, request, user, **kwargs):
    ensure_email_record(user)


@receiver(pre_save, sender=User)
def remember_the_old_email(sender, instance, update_fields=None, **kwargs):
    instance._email_before = None
    if instance.pk and (update_fields is None or "email" in update_fields):
        old = User.objects.filter(pk=instance.pk).values_list("email", flat=True).first()
        if old is not None and old.strip().lower() != (instance.email or "").strip().lower():
            instance._email_before = old


@receiver(post_save, sender=User)
def audit_an_email_change(sender, instance, created, **kwargs):
    old = getattr(instance, "_email_before", None)
    if created or not old:
        return
    from cedars.services import audit

    audit.record("user.email_changed", obj=instance, before={"email": old}, after={"email": instance.email})
    instance._email_before = None
    instance._email_changed_from = old  # for the receiver below, when allauth had no record of the old address


@receiver(email_changed)
def after_an_email_change(sender, request, user, from_email_address, to_email_address, **kwargs):
    """Sign the person out everywhere else, and tell the old address when allauth could not (it tells it itself when it kept a record of
    it: ACCOUNT_EMAIL_NOTIFICATIONS)."""
    from allauth.account.adapter import get_adapter

    from .service import end_sessions

    keep = request.session.session_key if request is not None and hasattr(request, "session") else None
    end_sessions(user, keep=keep)
    old = getattr(user, "_email_changed_from", None)
    if from_email_address is None and old:
        get_adapter(request).send_notification_mail(
            "account/email/email_changed", user, context={"from_email": old, "to_email": to_email_address.email}, email=old
        )

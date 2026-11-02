import getpass

from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.accounts.models import Organisation, User

# A program administrator's filer: reads, submits and confirms in every module. Approving stays with the CPUC, and administering
# the organization is --org-admin.
SUBMITTER = [f"{m}.{a}" for m in perms.MODULES for a in ("read", "submit", "confirm")]


class Command(BaseCommand):
    help = (
        "Sandbox or developer database only: create (or refresh) a test account that signs in with a password, a member of one "
        "program administrator with read, submit and confirm in every module. Re-runnable: resets the password and adds missing grants. "
        "Audited as the system."
    )

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--org", default="PGE", help="the program administrator's code (default PGE)")
        parser.add_argument("--name", default="", help="the account's full name")
        parser.add_argument("--password", default="", help="the sign-in password (asked for when left out)")
        parser.add_argument("--org-admin", action="store_true", help="also make the account the organization's administrator")

    @transaction.atomic
    def handle(self, email, org, name, password, org_admin, **opts):
        if settings.ENVIRONMENT not in ("sandbox", "dev", "test"):
            raise CommandError(f"demo_account runs only in the sandbox or a developer database, not in {settings.ENVIRONMENT}.")
        organisation = service.by_code(org)
        if organisation is None:
            raise CommandError(f"There is no organization {org}. Run `manage.py seed_sandbox` first.")
        if organisation.kind != Organisation.Kind.PA:
            raise CommandError(f"{organisation.code} is not a program administrator.")
        if not password:
            password = getpass.getpass(f"Password for {email}: ")
        if not password:
            raise CommandError("A password is needed.")

        user = User.objects.filter(email__iexact=email).first()
        created = user is None
        if created:
            user = User.objects.create_user(email=email, name=name)
        elif name:
            user.name = name
        user.set_password(password)
        user.save()
        EmailAddress.objects.update_or_create(user=user, email=user.email, defaults={"verified": True, "primary": True})

        held = service.effective_permissions(user, organisation)
        wanted = SUBMITTER + ([perms.ORG_ADMIN] if org_admin else [])
        added = [p for p in wanted if p not in held]
        for code in added:
            service.grant(None, user, organisation, code, reason="demo account")
        self.stdout.write(
            f"{user.email}: {'created' if created else 'password reset'}, a member of {organisation.code} "
            f"({len(added)} permission{'s' if len(added) != 1 else ''} added). Sign in with the password given."
        )

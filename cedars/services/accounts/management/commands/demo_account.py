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
        "Sandbox or developer database only: create (or refresh) a test account that signs in with a password, a member of one or more "
        "program administrators (--org, repeatable, or every active PA of a --pa-type) with read, submit and confirm in every module. "
        "Re-runnable: resets the password and adds missing grants. Audited as the system."
    )

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--org", action="append", default=[], help="a program administrator's code; repeat for several (default PGE)")
        parser.add_argument("--pa-type", default="", help="also every active program administrator of this type (IOU, REN or CCA)")
        parser.add_argument("--name", default="", help="the account's full name")
        parser.add_argument("--password", default="", help="the sign-in password (asked for when left out)")
        parser.add_argument("--org-admin", action="store_true", help="also make the account each organization's administrator")

    @transaction.atomic
    def handle(self, email, org, pa_type, name, password, org_admin, **opts):
        if settings.ENVIRONMENT not in ("sandbox", "dev", "test"):
            raise CommandError(f"demo_account runs only in the sandbox or a developer database, not in {settings.ENVIRONMENT}.")
        organisations = []
        for code in org or ([] if pa_type else ["PGE"]):
            organisation = service.by_code(code)
            if organisation is None:
                raise CommandError(f"There is no organization {code}. Run `manage.py seed_sandbox` first.")
            if organisation.kind != Organisation.Kind.PA:
                raise CommandError(f"{organisation.code} is not a program administrator.")
            organisations.append(organisation)
        if pa_type:
            typed = Organisation.objects.filter(kind=Organisation.Kind.PA, pa_type=pa_type.upper(), active=True).order_by("code")
            if not typed:
                raise CommandError(f"There is no active program administrator of type {pa_type}.")
            organisations += [o for o in typed if o not in organisations]
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

        wanted = SUBMITTER + ([perms.ORG_ADMIN] if org_admin else [])
        added = 0
        for organisation in organisations:
            held = service.effective_permissions(user, organisation)
            for code in wanted:
                if code not in held:
                    service.grant(None, user, organisation, code, reason="demo account")
                    added += 1
        self.stdout.write(
            f"{user.email}: {'created' if created else 'password reset'}, a member of {', '.join(o.code for o in organisations)} "
            f"({added} permission{'s' if added != 1 else ''} added). Sign in with the password given."
        )

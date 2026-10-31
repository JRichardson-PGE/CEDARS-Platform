from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cedars.services.accounts import permissions as perms
from cedars.services.accounts import service
from cedars.services.accounts.models import User


class Command(BaseCommand):
    help = (
        "First run only: create the CPUC organization and its first system administrator, organization administrator and data steward "
        "(the platform refuses to run without a steward, so the first one comes from here). Audited as the system."
    )

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--name", default="")
        parser.add_argument("--steward", action="store_true", help="also make this person the first data steward")

    @transaction.atomic
    def handle(self, email, name, steward, **opts):
        if service.cpuc() is not None and service.org_admins(service.cpuc()).exists():
            raise CommandError("The CPUC organization already has an administrator; grant further ones in the platform.")
        org = service.cpuc() or service.create_organisation(None, code="CPUC", name="California Public Utilities Commission", kind="cpuc")
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            user = User.objects.create_user(email=email, name=name)
            user.set_unusable_password()
            user.save()
        for code in [perms.ORG_ADMIN, perms.SYSTEM_ADMIN] + ([perms.STEWARD] if steward else []):
            service.grant(None, user, org, code, reason="bootstrap")
        self.stdout.write(f"{email}: CPUC administrator{' and data steward' if steward else ''}. Set a password with the reset link.")

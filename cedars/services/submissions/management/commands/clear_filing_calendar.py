from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from cedars.services.accounts.service import AccessDenied
from cedars.services.submissions import service


class Command(BaseCommand):
    help = (
        "Empty the filing calendar of a sandbox or demo site: each set-up (budget filing, claim year, year of monthly reports) and each "
        "deadline added by hand is deleted when nothing was filed for it, else its deadlines are retired (their filings kept). "
        "Shows the plan; --apply makes the change."
    )

    def add_arguments(self, parser):
        parser.add_argument("--reason", required=True, help="kept with every deletion and retirement in the audit trail")
        parser.add_argument("--apply", action="store_true", help="make the change (without it, only the plan is shown)")

    def handle(self, reason, apply, **opts):
        if settings.ENVIRONMENT not in ("sandbox", "dev", "test"):
            raise CommandError(f"clear_filing_calendar runs only on a sandbox, demo or developer site, not in {settings.ENVIRONMENT}.")
        try:
            plan = service.clear_calendar(None, reason, apply=apply)
        except AccessDenied as e:
            raise CommandError(str(e)) from e
        verb = ("Deleted", "Retired") if apply else ("Would delete", "Would retire")
        for what, n in plan["deleted"]:
            self.stdout.write(f"{verb[0]}: {what} ({n} deadline{'' if n == 1 else 's'}, nothing filed)")
        for what, n in plan["retired"]:
            self.stdout.write(f"{verb[1]}: {what} ({n} deadline{'' if n == 1 else 's'}; filed, so kept and taken off the calendar)")
        total = sum(n for _, n in plan["deleted"]), sum(n for _, n in plan["retired"])
        self.stdout.write(f"{verb[0]} {total[0]} deadlines and {verb[1].lower()} {total[1]}.")
        if not apply:
            self.stdout.write("Nothing changed. Run again with --apply to make the change.")

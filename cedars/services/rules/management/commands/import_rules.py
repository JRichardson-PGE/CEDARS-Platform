from django.core.management.base import BaseCommand

from cedars.services.rules import service


class Command(BaseCommand):
    help = "Import CEDARS' claims or budget-filing rules from the pinned engine as a draft rule set (--publish to publish it)."

    def add_arguments(self, parser):
        parser.add_argument("code", choices=["claims", "budget_filing"])
        parser.add_argument("--as-of", help="YYYY-MM-DD: the version in effect then (default: the newest the engine ships)")
        parser.add_argument("--publish", action="store_true")

    def handle(self, code, as_of, publish, **opts):
        rs = service.import_from_engine(None, code, as_of)
        if publish:
            rs = service.publish(None, rs)  # the published set, so the line below says so (UAT 2, U14)
        self.stdout.write(f"{rs}: {rs.rules.count()} rules, {rs.get_status_display().lower()}")

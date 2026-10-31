from django.core.management.base import BaseCommand

from cedars.core import health


class Command(BaseCommand):
    help = (
        "Run every health check (BR-12.2), write one JSON line for CloudWatch's metric filters, and alert the CPUC's system "
        "administrators about each check that starts failing. Schedule every 10 minutes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--no-alert", action="store_true", help="only report; tell nobody")

    def handle(self, *args, no_alert=False, **options):
        results = health.run_all()
        self.stdout.write(health.summary_line(results))
        if not no_alert:
            health.alert(results)

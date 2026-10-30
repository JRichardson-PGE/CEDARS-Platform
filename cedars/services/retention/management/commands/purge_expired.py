from django.core.management.base import BaseCommand

from cedars.services.retention import service


class Command(BaseCommand):
    help = "Delete records whose retention period has ended (run daily). --dry-run counts without deleting."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, dry_run, **opts):
        run = service.purge(dry_run=dry_run)
        for record_class, c in run.counts.items():
            self.stdout.write(
                f"{record_class}: {'would delete' if dry_run else 'deleted'} {c['deleted']}, held {c['held']}, failed {c['failed']}"
            )

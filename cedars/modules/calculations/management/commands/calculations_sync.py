from django.core.management.base import BaseCommand

from cedars.modules.calculations import dispatch


class Command(BaseCommand):
    help = "Bring AWS Batch runs' records up to date: progress and receipts from storage, jobs Batch ended. Schedule every minute."

    def handle(self, *args, **options):
        self.stdout.write(f"{dispatch.sync_batch()} run(s) ended")

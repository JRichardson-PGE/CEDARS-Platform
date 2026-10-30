from django.core.management.base import BaseCommand

from cedars.services.accounts import service


class Command(BaseCommand):
    help = "Close recertifications past their due date, suspending uncertified grants when the access policy says so (run daily)."

    def handle(self, **opts):
        for campaign in service.close_due_recertifications():
            self.stdout.write(f"closed {campaign}")

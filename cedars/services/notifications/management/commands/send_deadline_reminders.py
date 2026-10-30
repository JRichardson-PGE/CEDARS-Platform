from django.core.management.base import BaseCommand

from cedars.services.notifications import service


class Command(BaseCommand):
    help = "Remind subscribers of deadlines due in 7 days and in 1 day (run daily; each reminder is sent once)."

    def handle(self, **opts):
        self.stdout.write(f"{service.send_deadline_reminders()} reminders sent")

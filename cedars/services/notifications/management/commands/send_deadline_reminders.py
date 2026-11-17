from django.core.management.base import BaseCommand

from cedars.services.notifications import service


class Command(BaseCommand):
    help = "Send each user's deadline reminders at the lead times they chose (run daily; each reminder is sent once per due date)."

    def handle(self, **opts):
        self.stdout.write(f"{service.send_deadline_reminders()} reminders sent")

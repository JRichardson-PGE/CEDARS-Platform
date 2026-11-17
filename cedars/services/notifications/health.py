"""The notification service's health check (cedars/core/health.py): emails that could not be sent."""

from django.utils import timezone

from cedars.core import health

from .models import Notification


def email() -> health.Result:
    hours = int(health.limit("failures").total_seconds() // 3600)
    failed = Notification.objects.filter(created_at__gte=timezone.now() - health.limit("failures")).exclude(email_error="")
    n = failed.count()
    result = health.Result("notifications.email", "Notifications", "Emails that could not be sent")
    if n:
        result.status = health.ATTENTION
        last = failed.order_by("-created_at").first()
        result.summary = f"{n} email{'s' if n != 1 else ''} failed in the last {hours} hours (each message is still on its user's page)."
        result.items = [health.Item("email-error", "Latest error", last.created_at, last.email_error[:300])]
    else:
        result.summary = f"No email failed in the last {hours} hours."
    return result


def register() -> None:
    health.register("notifications.email", email)

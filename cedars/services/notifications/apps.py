from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "cedars.services.notifications"
    label = "notifications"
    verbose_name = "Notifications"

    def ready(self):
        from django.db.models.signals import post_save

        from cedars.services.audit.models import AuditEvent

        from . import service

        post_save.connect(service.on_audit_event, sender=AuditEvent, dispatch_uid="notifications-from-audit")

        from cedars.services.retention.service import register_purger

        register_purger("notification", service.expired)

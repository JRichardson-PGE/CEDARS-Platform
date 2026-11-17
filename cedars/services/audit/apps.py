from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "cedars.services.audit"
    label = "audit"
    verbose_name = "Audit and change log"

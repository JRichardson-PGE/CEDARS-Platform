from django.apps import AppConfig


class FeedbackConfig(AppConfig):
    name = "cedars.services.feedback"
    label = "feedback"
    verbose_name = "Feedback"

    def ready(self):
        from cedars.services.retention.service import register_purger

        from . import service

        register_purger("feedback", service.expired)

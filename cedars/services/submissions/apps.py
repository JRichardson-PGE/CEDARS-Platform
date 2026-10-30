from django.apps import AppConfig


class SubmissionsConfig(AppConfig):
    name = "cedars.services.submissions"
    label = "submissions"
    verbose_name = "Submissions and workflow"

    def ready(self):
        from cedars.services.retention.service import register_purger

        from . import service

        register_purger("unsubmitted_draft", service.expired_drafts)

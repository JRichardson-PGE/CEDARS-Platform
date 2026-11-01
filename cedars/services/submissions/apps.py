from django.apps import AppConfig


class SubmissionsConfig(AppConfig):
    name = "cedars.services.submissions"
    label = "submissions"
    verbose_name = "Submissions and workflow"

    def ready(self):
        from cedars.services.retention.service import register_purger

        from . import service

        register_purger("unsubmitted_draft", service.expired_drafts)

        from cedars.core import queue as my_queue

        from . import queue

        # each user's queue (BR-09.5): what waits on them in the filings
        my_queue.register("reopen", "Correction requests to decide", queue.reopen_requests, action="Decide", order=10)
        my_queue.register("confirm", "Filings ready to confirm", queue.confirmations, action="Review and confirm", order=50)
        my_queue.register("reply", "Comments waiting for your reply", queue.replies, action="Read and reply", order=70)

        from django.conf import settings

        if settings.ENVIRONMENT == "sandbox":  # test filings, confirmed or not, go after the sandbox period; never in production
            register_purger("sandbox_data", service.expired_sandbox_filings)

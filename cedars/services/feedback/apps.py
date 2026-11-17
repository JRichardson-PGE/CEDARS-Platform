from django.apps import AppConfig


class FeedbackConfig(AppConfig):
    name = "cedars.services.feedback"
    label = "feedback"
    verbose_name = "Feedback"

    def ready(self):
        from cedars.services.retention.service import register_purger

        from . import service

        register_purger("feedback", service.expired)
        from cedars.core import queue as my_queue

        my_queue.register(
            "feedback_question",
            "Questions about your requests",
            service.questions_for,
            action="Reply",
            order=92,
            help="The CPUC asked you something about a request you sent. It waits here until you reply.",
        )
        my_queue.register(
            "feedback_inbox",
            "Requests to answer",
            service.to_answer,
            action="Open",
            order=94,
            help="Questions, problems, ideas and comments people sent from a page, waiting on the CPUC.",
        )

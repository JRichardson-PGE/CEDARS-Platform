from django.apps import AppConfig


class SpecificationsConfig(AppConfig):
    name = "cedars.services.specifications"
    label = "specifications"
    verbose_name = "Filing specifications"

    def ready(self):
        from cedars.services.rules import service as rules

        from . import service

        rules.register_layout_provider(service.rule_set_layout)

        from cedars.core import queue as my_queue
        from cedars.services.notifications import service as notifications

        from . import comments

        notifications.register_concerns("specifications.specificationversion", comments.concern)
        my_queue.register(
            "specification_comments",
            "Comments on proposed filing specifications",
            comments.unanswered,
            action="Read and answer",
            order=75,
        )

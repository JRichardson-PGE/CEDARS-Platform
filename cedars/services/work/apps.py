from django.apps import AppConfig


class WorkConfig(AppConfig):
    name = "cedars.services.work"
    label = "work"
    verbose_name = "Work items"

    def ready(self):
        from cedars.core import queue as my_queue

        from . import service

        my_queue.register(
            "work_approval",
            "Work items to approve",
            service.to_approve,
            action="Decide",
            order=96,
            help="A backlog manager proposed that a work item be declined or made public. It takes effect when the CPUC approves it.",
        )
        my_queue.register(
            "work_proposed",
            "Proposed work items",
            service.to_triage,
            action="Open",
            order=97,
            help="Work items proposed from people's requests, waiting to be accepted into the backlog or declined.",
        )

from django.apps import AppConfig


class TestingConfig(AppConfig):
    name = "cedars.services.testing"
    label = "testing"
    verbose_name = "Testing"

    def ready(self):
        from cedars.core import queue as my_queue

        from . import service

        my_queue.register(
            "testing",
            "Testing assigned to you",
            service.waiting_on,
            action="Continue testing",
            order=90,
            help="Test scripts a test lead assigned to you, until every test in them has a result or the round of testing closes.",
        )

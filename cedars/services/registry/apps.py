from django.apps import AppConfig


class RegistryConfig(AppConfig):
    name = "cedars.services.registry"
    label = "registry"
    verbose_name = "Program registry"

    def ready(self):
        from cedars.core import queue as my_queue

        from . import queue

        my_queue.register("program_change", "Program changes to decide", queue.change_requests, action="Decide", order=20)

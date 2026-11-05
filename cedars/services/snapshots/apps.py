from django.apps import AppConfig


class SnapshotsConfig(AppConfig):
    name = "cedars.services.snapshots"
    label = "snapshots"
    verbose_name = "Publication snapshots"

    def ready(self):
        from cedars.services.submissions.service import register_listener

        from . import service

        register_listener(service.on_published, service.on_unpublished)

        from cedars.core import queue as my_queue

        from . import queue

        my_queue.register("publication_hold", "Held publications to look into", queue.holds, action="Look into it", order=55)

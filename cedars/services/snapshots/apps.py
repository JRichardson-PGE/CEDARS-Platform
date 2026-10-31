from django.apps import AppConfig


class SnapshotsConfig(AppConfig):
    name = "cedars.services.snapshots"
    label = "snapshots"
    verbose_name = "Publication snapshots"

    def ready(self):
        from cedars.services.submissions.service import register_listener

        from . import service

        register_listener(service.on_published, service.on_unpublished)

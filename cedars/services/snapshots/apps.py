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

        from cedars.core import page_access
        from cedars.services.accounts import service as access

        # who the Help guides offer a link to the scan settings (UI review 3, U4): the page's own check
        page_access.register("snapshots:scan_settings", lambda r: access.is_system_admin(r.user), "CPUC system administrators")

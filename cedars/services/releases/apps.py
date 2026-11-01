from django.apps import AppConfig


class ReleasesConfig(AppConfig):
    name = "cedars.services.releases"
    label = "releases"
    verbose_name = "Release notes and limits"

    def ready(self):
        from cedars.core import health
        from cedars.services.notifications import service as notifications

        from . import notices
        from .health import check

        notifications.register_concerns("releases.releasenote", notices.concern)
        health.register("releases.notes", check)

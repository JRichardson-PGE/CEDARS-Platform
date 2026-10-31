from django.apps import AppConfig


class ImpactProfilesConfig(AppConfig):
    """The Impact profiles module: deemed load shapes synced from eTRM and processed into impact profiles, custom (PA-owned) impact
    profiles, and the profile library the engine reads (architecture, "The Impact profiles module, step by step")."""

    name = "cedars.modules.impact_profiles"
    label = "impact_profiles"
    verbose_name = "Impact profiles"

    def ready(self):
        from . import health

        health.register()  # stalled and failed jobs and the eTRM sync on the administrators' health page (BR-12.2, BR-11.3)
